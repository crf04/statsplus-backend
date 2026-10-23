"""What scripts/check-changed.sh checks for a given diff.

Builds a small repository tree, hands ``plan`` concrete changed paths, and
compares the chosen mode, lint files and test files with literal values.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.changed_scope import changed_files, plan

TREE = {
    "app/__init__.py": "from app.routes import top\n\n\ndef create_app():\n    return top\n",
    "app/migrations.py": "",
    "app/config/statistic_catalog.yaml": "",
    "app/services/__init__.py": "from app.services.other import OTHER\n",
    "app/services/base.py": "VALUE = 1\n",
    "app/services/middle.py": "from app.services.base import VALUE\n",
    "app/services/other.py": "OTHER = 2\n",
    "app/services/proxy.py": "from app.services import middle\n",
    "app/services/global_cfg.py": "CFG = 4\n",
    "app/services/store.py": "STORE = 3\n",
    "app/services/routed.py": "ROUTED = 5\n",
    "app/services/helped.py": "HELPED = 6\n",
    "app/services/prompt.py": 'PROMPT = "prompts/system.txt"\n',
    "app/data.py": 'DB = "nba_play_types.db"  # see docs/GUIDE.md\n',
    "app/routes/__init__.py": "",
    "app/routes/top.py": "from ..services import middle\n",
    "scripts/__init__.py": "",
    "scripts/tool.py": "from app.services import other\n",
    "scripts/validate_demo_db.py": "",
    "tests/__init__.py": "",
    "tests/conftest.py": (
        "import pytest\n\nfrom app.services import global_cfg\n\n\n"
        "@pytest.fixture(autouse=True)\ndef reset():\n    from app.services import proxy\n\n\n"
        "@pytest.fixture\ndef app_factory():\n    from app import create_app\n\n    return create_app\n\n\n"
        "@pytest.fixture\ndef client(app_factory):\n    return app_factory()\n\n\n"
        "@pytest.fixture\ndef db():\n    from app.services import store\n\n    return store\n"
    ),
    "tests/routes/conftest.py": (
        "import pytest\n\n\n"
        "@pytest.fixture\ndef routed_fixture():\n    from app.services import routed\n\n    return routed\n\n\n"
        "@pytest.fixture\ndef client(client):\n    return client\n"
    ),
    "tests/support/__init__.py": "",
    "tests/support/world.py": "from app.services.helped import HELPED\n",
    "tests/fixtures/sample.json": "{}",
    "tests/test_base.py": "from app.services.base import VALUE\n",
    "tests/test_top.py": "from app.routes.top import middle\n",
    "tests/test_patched.py": 'from unittest.mock import patch\n\npatch("app.services.middle.VALUE")\n',
    "tests/test_other.py": "from app.services.other import OTHER\n",
    "tests/test_tool.py": 'import subprocess\n\nsubprocess.run(["python", "scripts/tool.py"])\n',
    "tests/routes/test_nested.py": "import app.services.other\n",
    "tests/test_client.py": "def test_route(client):\n    assert client\n",
    "tests/test_world.py": "from tests.support.world import HELPED\n",
    "tests/test_shared.py": "from app.services.store import STORE as SHARED\n",
    "tests/routes/test_reuses.py": "from tests.test_shared import SHARED\n",
    "tests/test_workflow.py": 'WORKFLOW = ".github/workflows/tests.yml"\nGUIDE = "GUIDE.md"\n',
    "tests/test_marked.py": 'import pytest\n\npytestmark = pytest.mark.usefixtures("db")\n',
    "tests/test_pkg.py": "from app.services import store\n",
    "tests/routes/test_routed.py": "def test_x(routed_fixture):\n    assert routed_fixture\n",
    "tests/routes/test_overridden.py": "def test_x(client):\n    assert client\n",
    "tests/test_outside.py": "def test_x(routed_fixture):\n    assert routed_fixture\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, text in TREE.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_module_change_selects_tests_that_import_it_directly_or_through_app(repo: Path) -> None:
    result = plan(repo, ["app/services/base.py"])

    assert (result.mode, result.lint, result.migrations) == ("affected", ["app/services/base.py"], False)
    assert result.tests == [
        "tests/routes/test_overridden.py",
        "tests/test_base.py",
        "tests/test_client.py",
        "tests/test_patched.py",
        "tests/test_top.py",
    ]


ALL_TESTS = [
    "tests/routes/test_nested.py",
    "tests/routes/test_overridden.py",
    "tests/routes/test_reuses.py",
    "tests/routes/test_routed.py",
    "tests/test_base.py",
    "tests/test_client.py",
    "tests/test_marked.py",
    "tests/test_other.py",
    "tests/test_outside.py",
    "tests/test_patched.py",
    "tests/test_pkg.py",
    "tests/test_shared.py",
    "tests/test_tool.py",
    "tests/test_top.py",
    "tests/test_workflow.py",
    "tests/test_world.py",
]


@pytest.mark.parametrize("module", ["app/services/proxy.py", "app/services/global_cfg.py"])
def test_module_every_test_imports_through_conftest_selects_all(repo: Path, module: str) -> None:
    # proxy: autouse fixture; global_cfg: conftest module-level import.
    assert plan(repo, [module]).tests == ALL_TESTS


def test_module_reached_through_autouse_import_only_transitively_selects_its_users(repo: Path) -> None:
    # The autouse fixture imports proxy -> middle -> base; that alone selects nothing.
    assert "tests/test_other.py" not in plan(repo, ["app/services/base.py"]).tests


def test_fixture_requested_by_parameter_or_usefixtures_selects_the_test(repo: Path) -> None:
    # test_reuses reaches store only through another test module, test_shared.
    assert plan(repo, ["app/services/store.py"]).tests == [
        "tests/routes/test_reuses.py",
        "tests/test_marked.py",
        "tests/test_pkg.py",
        "tests/test_shared.py",
    ]


def test_nested_conftest_fixture_selects_only_tests_under_its_directory(repo: Path) -> None:
    assert plan(repo, ["app/services/routed.py"]).tests == ["tests/routes/test_routed.py"]


def test_module_reached_through_a_test_helper_selects_the_test(repo: Path) -> None:
    assert plan(repo, ["app/services/helped.py"]).tests == ["tests/test_world.py"]


def test_scripts_module_selects_tests_that_import_or_run_it(repo: Path) -> None:
    assert plan(repo, ["app/services/other.py"]).tests == [
        "tests/routes/test_nested.py",
        "tests/test_other.py",
        "tests/test_tool.py",
    ]
    assert plan(repo, ["scripts/tool.py"]).tests == ["tests/test_tool.py"]


def test_changed_test_file_runs_alone(repo: Path) -> None:
    result = plan(repo, ["tests/test_other.py"])

    assert (result.mode, result.lint, result.tests) == (
        "affected",
        ["tests/test_other.py"],
        ["tests/test_other.py"],
    )


def test_deleted_module_still_selects_its_importers_but_is_not_linted(repo: Path) -> None:
    (repo / "app/services/base.py").unlink()

    result = plan(repo, ["app/services/base.py"])

    assert (result.mode, result.lint) == ("affected", [])
    assert result.tests == [
        "tests/routes/test_overridden.py",
        "tests/test_base.py",
        "tests/test_client.py",
        "tests/test_patched.py",
        "tests/test_top.py",
    ]


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("tests/conftest.py", "conftest.py changed: tests/conftest.py"),
        ("tests/support/world.py", "test helper changed: tests/support/world.py"),
        ("tests/fixtures/sample.json", "test fixtures changed: tests/fixtures/sample.json"),
        ("requirements-lock.txt", "requirements changed: requirements-lock.txt"),
        ("runtime.txt", "runtime.txt changed: runtime.txt"),
        ("pytest.ini", "pytest/ruff configuration changed: pytest.ini"),
        ("pyproject.toml", "pytest/ruff configuration changed: pyproject.toml"),
        ("scripts/check.sh", "scripts/check.sh changed: scripts/check.sh"),
        ("app/services/__init__.py", "package __init__.py changed: app/services/__init__.py"),
        ("app/config/statistic_catalog.yaml", "app data file changed: app/config/statistic_catalog.yaml"),
        ("prompts/system.txt", "app or scripts code names prompts/system.txt"),
        ("gunicorn.conf.py", "no import mapping for gunicorn.conf.py"),
    ],
)
def test_changes_the_mapping_cannot_see_run_the_full_suite(repo: Path, path: str, reason: str) -> None:
    result = plan(repo, ["docs/ARCHITECTURE.md", path])

    assert (result.mode, result.reason, result.tests, result.migrations) == ("full", reason, [], False)


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("app/migrations.py", "migrations changed: app/migrations.py"),
        ("scripts/migrate.py", "migrations changed: scripts/migrate.py"),
        ("nba_play_types.db", "app or scripts code names nba_play_types.db"),
    ],
)
def test_migration_change_runs_full_suite_and_migration_steps(repo: Path, path: str, reason: str) -> None:
    result = plan(repo, [path])

    assert (result.mode, result.reason, result.migrations) == ("full", reason, True)


def test_demo_database_validator_change_runs_migration_steps(repo: Path) -> None:
    result = plan(repo, ["scripts/validate_demo_db.py"])

    assert (result.mode, result.tests, result.migrations) == ("affected", [], True)


def test_empty_diff_runs_nothing(repo: Path) -> None:
    result = plan(repo, [])

    assert (result.mode, result.lint, result.tests, result.migrations) == ("none", [], [], False)


def test_changed_test_module_selects_tests_that_import_it(repo: Path) -> None:
    assert plan(repo, ["tests/test_shared.py"]).tests == [
        "tests/routes/test_reuses.py",
        "tests/test_shared.py",
    ]


def test_data_file_selects_tests_that_name_it(repo: Path) -> None:
    result = plan(repo, [".github/workflows/tests.yml", "docs/GUIDE.md"])

    assert (result.mode, result.lint, result.tests) == ("affected", [], ["tests/test_workflow.py"])


def test_docs_no_test_names_run_nothing(repo: Path) -> None:
    result = plan(repo, ["README.md", "docs/SETTINGS.md", ".github/workflows/security.yml"])

    assert (result.mode, result.lint, result.tests, result.migrations) == ("none", [], [], False)


def test_changed_files_include_commits_staged_unstaged_untracked_and_deletions(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    for name in ("committed.py", "staged.py", "unstaged.py", "deleted.py", "untouched.py"):
        (tmp_path / name).write_text("", encoding="utf-8")
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "base")
    git("update-ref", "refs/remotes/origin/master", "HEAD")

    (tmp_path / "committed.py").write_text("x = 1\n", encoding="utf-8")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-am", "change")
    (tmp_path / "staged.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "staged.py")
    (tmp_path / "unstaged.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "deleted.py").unlink()
    (tmp_path / "untracked.py").write_text("", encoding="utf-8")

    assert changed_files(tmp_path) == [
        "committed.py",
        "deleted.py",
        "staged.py",
        "unstaged.py",
        "untracked.py",
    ]
