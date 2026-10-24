"""Choose what ``scripts/check-changed.sh`` runs for the current diff.

The diff is everything that differs from ``git merge-base origin/master HEAD``:
commits, staged and unstaged edits, and untracked files. Python files under
app/, tests/ and scripts/ are linted. Tests are selected by imports: a test is
affected when it imports, or names in a string such as a ``patch`` target, a
changed module or any app/scripts module that transitively imports one, itself
or through a conftest fixture it requests. Imports every test runs (conftest
module level, hooks, autouse fixtures) count only when they name a changed
module directly. A changed data file (docs, workflows, JSON) selects the tests
whose source names it. When a change can reach tests in ways these rules do not
show (conftest, test helpers and fixtures, dependencies, configuration,
migrations, package ``__init__`` files, other Python files, data files that
app/ or scripts/ code names), the plan is the full suite instead.

Prints one ``key=value`` line per fact for the shell script to read.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

BASE_REF = "origin/master"
LINT_ROOTS = ("app/", "tests/", "scripts/")

# Changes the import mapping cannot see; each runs the full suite.
FULL_SUITE = (
    (re.compile(r"(^|/)conftest\.py$"), "conftest.py changed"),
    (re.compile(r"^tests/fixtures/"), "test fixtures changed"),
    (re.compile(r"^tests/(.+/)?(?!test_)[^/]+\.py$"), "test helper changed"),
    (re.compile(r"^requirements[^/]*\.txt$"), "requirements changed"),
    (re.compile(r"^runtime\.txt$"), "runtime.txt changed"),
    (re.compile(r"^(pytest\.ini|pyproject\.toml)$"), "pytest/ruff configuration changed"),
    (re.compile(r"^scripts/check\.sh$"), "scripts/check.sh changed"),
    (re.compile(r"^(app/migrations\.py|scripts/migrate\.py)$"), "migrations changed"),
    (re.compile(r"(^|/)__init__\.py$"), "package __init__.py changed"),
)

# Changes that also need check.sh's migration and demo-database steps.
MIGRATION_STEPS = re.compile(
    r"^(app/migrations\.py|scripts/migrate\.py|scripts/validate_demo_db\.py|nba_play_types\.db)$"
)

TEST_FILE = re.compile(r"^tests/(.+/)?test_[^/]+\.py$")
MODULE_FILE = re.compile(r"^(app|scripts)/.+\.py$")


@dataclass
class Plan:
    mode: str  # "none", "affected" or "full"
    reason: str
    lint: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    migrations: bool = False


def module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _string_module(value: str, known: set[str]) -> str | None:
    """The module a string names: a file path, or the longest module prefix of a
    dotted target such as ``"app.services.x.Thing"`` in ``patch``."""
    if MODULE_FILE.match(value):
        return module_name(value)
    if re.fullmatch(r"(app|scripts)(\.\w+)+", value):
        parts = value.split(".")
        for end in range(len(parts), 1, -1):
            if ".".join(parts[:end]) in known:
                return ".".join(parts[:end])
    return None


def references(tree: ast.AST, package: str, known: set[str]) -> set[str]:
    """Modules a syntax tree imports or names in a string literal.

    Only the named module counts, not its parent packages: every submodule
    import runs ``app/__init__.py``, and following that would select the whole
    suite for any change. A changed ``__init__.py`` runs the full suite instead.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".")
                base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join([*base_parts, node.module] if node.module else base_parts)
            else:
                base = node.module or ""
            # `from pkg import mod` names the submodule; `from mod import thing`
            # names the module.
            for alias in node.names:
                submodule = f"{base}.{alias.name}"
                names.add(submodule if submodule in known else base)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            module = _string_module(node.value, known)
            if module:
                names.add(module)
    return names


def _parse(root: Path, rel: str) -> tuple[ast.Module, str]:
    tree = ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
    module = module_name(rel)
    return tree, module if rel.endswith("__init__.py") else module.rpartition(".")[0]


def _is_fixture(node: ast.AST) -> bool:
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    return any("fixture" in ast.unparse(decorator) for decorator in node.decorator_list)


def _requested_names(tree: ast.AST) -> set[str]:
    """Fixture names a test file can request: parameters and strings
    (``usefixtures``)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


@dataclass
class _Conftest:
    directory: str  # tests under this directory see its fixtures
    always: set[str]  # imports every test runs: module level, hooks, autouse fixtures
    fixtures: dict[str, tuple[set[str], set[str]]]  # name -> (imports, requested fixtures)


def _conftest(root: Path, rel: str, known: set[str]) -> _Conftest:
    tree, package = _parse(root, rel)
    always: set[str] = set()
    fixtures: dict[str, tuple[set[str], set[str]]] = {}
    for node in tree.body:
        refs = references(node, package, known)
        if _is_fixture(node):
            fixtures[node.name] = (refs, {arg.arg for arg in node.args.args})
            if any("autouse=True" in ast.unparse(d) for d in node.decorator_list):
                always |= refs
        else:
            always |= refs
    return _Conftest(PurePosixPath(rel).parent.as_posix() + "/", always, fixtures)


def _fixture_refs(conftests: list[_Conftest], requested: set[str]) -> set[str]:
    refs: set[str] = set()
    seen: set[str] = set()
    pending = list(requested)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for conftest in conftests:
            if name in conftest.fixtures:
                imports, dependencies = conftest.fixtures[name]
                refs |= imports
                pending.extend(dependencies)
    return refs


def affected_tests(root: Path, changed_modules: set[str], data_files: set[str]) -> list[str]:
    """Test files that reach a changed module through imports, their own or
    those of the conftest fixtures they use, or that name a changed data file."""
    # Test modules are graph nodes too: tests import helpers (tests/support)
    # and other test files, and reach app code through them.
    tests = [path for path in root.glob("tests/**/*.py") if path.name != "conftest.py"]
    modules = sorted([*root.glob("app/**/*.py"), *root.glob("scripts/*.py"), *tests])
    known = {module_name(path.relative_to(root).as_posix()) for path in modules} | set(changed_modules)
    graph: dict[str, set[str]] = {}
    for path in modules:
        rel = path.relative_to(root).as_posix()
        tree, package = _parse(root, rel)
        graph[module_name(rel)] = references(tree, package, known)

    reaches = set(changed_modules)
    grew = True
    while grew:
        grew = False
        for module, refs in graph.items():
            if module not in reaches and refs & reaches:
                reaches.add(module)
                grew = True

    conftests = [
        _conftest(root, path.relative_to(root).as_posix(), known)
        for path in sorted(root.glob("tests/**/conftest.py"))
    ]
    selected = []
    for path in sorted(root.glob("tests/**/test_*.py")):
        rel = path.relative_to(root).as_posix()
        tree, package = _parse(root, rel)
        visible = [c for c in conftests if rel.startswith(c.directory)]
        refs = references(tree, package, known)
        refs |= _fixture_refs(visible, _requested_names(tree))
        # What every test runs (conftest module imports, hooks, autouse
        # fixtures) only imports most of the app; it counts when it names a
        # changed module itself, not when it reaches one transitively, or every
        # change would select the whole suite.
        always = set().union(*(c.always for c in visible))
        names_data = any(PurePosixPath(data).name in path.read_text(encoding="utf-8") for data in data_files)
        if module_name(rel) in reaches or refs & reaches or always & changed_modules or names_data:
            selected.append(rel)
    return selected


def plan(root: Path, changed: list[str]) -> Plan:
    """Decide the checks for ``changed`` paths (deleted paths included)."""
    present = [p for p in changed if (root / p).exists()]
    lint = [p for p in present if p.endswith(".py") and p.startswith(LINT_ROOTS)]
    migrations = any(MIGRATION_STEPS.match(p) for p in changed)
    if not changed:
        return Plan("none", "no files changed")

    code = None
    data_files = set()
    for path in changed:
        for pattern, why in FULL_SUITE:
            if pattern.search(path):
                return Plan("full", f"{why}: {path}", lint, migrations=migrations)
        if TEST_FILE.match(path) or MODULE_FILE.match(path):
            continue
        if path.endswith(".py"):
            return Plan("full", f"no import mapping for {path}", lint, migrations=migrations)
        if path.startswith("app/"):
            return Plan("full", f"app data file changed: {path}", lint, migrations=migrations)
        if code is None:
            sources = [*root.glob("app/**/*"), *root.glob("scripts/*")]
            code = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sources if p.is_file())
        # Code names Markdown only in prose; no app or script reads it.
        if not path.endswith(".md") and PurePosixPath(path).name in code:
            return Plan("full", f"app or scripts code names {path}", lint, migrations=migrations)
        data_files.add(path)

    changed_modules = {module_name(p) for p in changed if TEST_FILE.match(p) or MODULE_FILE.match(p)}
    tests = [p for p in affected_tests(root, changed_modules, data_files) if (root / p).exists()]
    if not tests and not lint and not migrations:
        return Plan("none", "no test imports or names the changed files")
    return Plan(
        "affected", f"{len(tests)} test file(s) import or name the changed files", lint, tests, migrations
    )


def changed_files(root: Path) -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if line]

    base = git("merge-base", BASE_REF, "HEAD")[0]
    tracked = git("diff", "--name-only", "--no-renames", base)
    untracked = git("ls-files", "--others", "--exclude-standard")
    return sorted(set(tracked) | set(untracked))


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    changed = changed_files(root)
    result = plan(root, changed)
    print(f"changed={len(changed)}")
    print(f"mode={result.mode}")
    print(f"reason={result.reason}")
    print(f"migrations={int(result.migrations)}")
    for path in result.lint:
        print(f"lint={path}")
    for path in result.tests:
        print(f"test={path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
