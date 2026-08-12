"""Code-revision snapshot for one Analysis Run.

The matchup script must bind a run id to the exact analysis code Python
actually loaded. A disk-only git snapshot cannot prove that: the entry script
and ``code_revision`` itself are already loaded before any snapshot can be
taken, so a post-load edit would let both snapshots identify newer disk content
while older bytecode executes. The launcher therefore establishes a pre-load
snapshot and, immediately before publication, this module fails closed unless
the bytecode actually loaded for every analysis module provably matches the
disk source it is attributed to (via the ``__pycache__`` bytecode cache written
when each module was imported). This module is deliberately stdlib-only and
free of any analysis code, so importing it can never load the modules whose
revision it records.
"""

import hashlib
import json
import marshal
import subprocess
import sys
import types
from pathlib import Path

# The production analysis script imported by the launcher. Provenance requires
# it to be loaded as a module (not executed as ``__main__``) so its loaded
# bytecode is provable against disk.
_ANALYSIS_ENTRY_MODULE = "archetype_matchups_2025_26"


def _revision_digest(*parts) -> str:
    """Deterministic SHA-256 over JSON-serializable ``parts``."""
    document = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def current_code_revision(analysis_dir=None) -> str:
    """Current git revision of the analysis code, including working-tree state.

    A run cannot be attributed to its exact code without a revision, so an
    undeterminable revision aborts instead of silently recording ``unknown``
    and letting distinct code share one run id. Uncommitted changes to tracked
    files and the contents of untracked analysis files also feed the revision,
    so dirty analysis code never receives the clean HEAD identity.

    ``analysis_dir`` defaults to this package's parent (the analysis root whose
    tracked and untracked files make up the analyzed code); callers can point
    it at any git worktree for deterministic, offline testing.
    """
    root = (
        Path(analysis_dir)
        if analysis_dir is not None
        else Path(__file__).resolve().parents[1]
    )
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        tracked_diff = subprocess.check_output(
            ["git", "diff", "HEAD", "--", "."],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "."],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception as error:
        raise RuntimeError(
            "Could not determine the analysis code revision; refusing to build "
            "an unattributable run"
        ) from error
    untracked_entries = []
    for relative in untracked.splitlines():
        path = root / relative
        try:
            untracked_entries.append(
                (relative, hashlib.sha256(path.read_bytes()).hexdigest())
            )
        except OSError:
            untracked_entries.append((relative, "unreadable"))
    if tracked_diff or untracked_entries:
        return _revision_digest(("analysis-code", head, tracked_diff, untracked_entries))
    return head


def _code_from_source_file(path):
    """Freshly compile the current disk source of ``path`` for comparison."""
    return compile(
        path.read_bytes(),
        str(path),
        "exec",
        dont_inherit=True,
        optimize=sys.flags.optimize,
    )


def _code_from_bytecode_cache(path):
    """The exact code object that was loaded, recovered from its bytecode cache.

    ``importlib`` writes the marshaled code object that was just executed into
    ``__pycache__`` at import time, so the cache holds the loaded bytecode even
    if the disk source has since changed. Both timestamp- and hash-based cache
    formats carry the code object after their header, and both headers are the
    same size (magic and flags, then either timestamp-plus-size or the source
    hash).
    """
    data = path.read_bytes()
    if len(data) < 16:
        raise ValueError(f"Bytecode cache is truncated: {path}")
    return marshal.loads(data[16:])


def _code_objects_equal(a, b) -> bool:
    """Structural equality of two code objects, recursing into nested code.

    ``marshal.dumps`` is not used because the same source compiled twice
    marshals differently (string interning memoization) even when the code is
    identical; comparing the semantic attributes instead is exact and immune to
    that artifact.
    """
    if a is b:
        return True
    if not isinstance(a, types.CodeType) or not isinstance(b, types.CodeType):
        return False
    if len(a.co_consts) != len(b.co_consts):
        return False
    for left, right in zip(a.co_consts, b.co_consts):
        if isinstance(left, types.CodeType) or isinstance(right, types.CodeType):
            if not _code_objects_equal(left, right):
                return False
        elif left != right:
            return False
    return (
        a.co_code == b.co_code
        and a.co_names == b.co_names
        and a.co_varnames == b.co_varnames
        and a.co_freevars == b.co_freevars
        and a.co_cellvars == b.co_cellvars
        and a.co_argcount == b.co_argcount
        and a.co_posonlyargcount == b.co_posonlyargcount
        and a.co_kwonlyargcount == b.co_kwonlyargcount
        and a.co_nlocals == b.co_nlocals
        and a.co_stacksize == b.co_stacksize
        and a.co_flags == b.co_flags
        and a.co_firstlineno == b.co_firstlineno
        and a.co_linetable == b.co_linetable
        and a.co_exceptiontable == b.co_exceptiontable
        and a.co_filename == b.co_filename
        and a.co_name == b.co_name
        and a.co_qualname == b.co_qualname
    )


def _verify_module_loaded_code_matches_disk(module) -> None:
    """Fail closed unless a loaded analysis module provably matches its disk source.

    The module's bytecode cache holds the code object that was actually loaded;
    if the disk source has been edited since load, recompiling it now yields a
    different code object and the comparison fails.
    """
    cached = module.__cached__
    if not cached or not Path(cached).exists():
        raise RuntimeError(
            f"Cannot prove the loaded code of {module.__name__}: no bytecode "
            "cache was written for it; refusing to attribute a run to "
            "unprovable code"
        )
    source_file = Path(module.__file__)
    try:
        loaded_code = _code_from_bytecode_cache(Path(cached))
        disk_code = _code_from_source_file(source_file)
    except (OSError, ValueError, SyntaxError, UnicodeError) as error:
        raise RuntimeError(
            f"Cannot prove the loaded code of {module.__name__} against disk"
        ) from error
    if not _code_objects_equal(loaded_code, disk_code):
        raise RuntimeError(
            f"The loaded code of {module.__name__} does not match its current "
            "disk source; refusing to attribute a run to a mixed code snapshot"
        )


def verify_loaded_code_matches_disk(analysis_dir=None) -> None:
    """Fail closed unless the loaded analysis code provably matches the disk.

    The analysis entry script must be loaded as a module (through the launcher)
    rather than executed directly as ``__main__``: ``__main__`` bytecode is not
    recoverable, so a directly executed entry script could not be proven against
    the disk it would be attributed to. Every analysis module loaded from under
    the analysis root is then checked module by module, and any module whose
    loaded bytecode cannot be proven to equal its current disk source aborts
    publication.
    """
    root = (
        Path(analysis_dir).resolve()
        if analysis_dir is not None
        else Path(__file__).resolve().parents[1]
    )
    entry = sys.modules.get(_ANALYSIS_ENTRY_MODULE)
    if (
        entry is None
        or getattr(entry, "__file__", None) is None
        or root not in Path(entry.__file__).resolve().parents
    ):
        raise RuntimeError(
            f"The analysis entry script must run through its launcher so its "
            f"loaded code is provable; {_ANALYSIS_ENTRY_MODULE} is not loaded "
            "as a module from the analysis root"
        )
    checked = []
    for name, module in list(sys.modules.items()):
        source = getattr(module, "__file__", None)
        # The entry mechanism's own ``__main__`` is the thin launcher; its
        # loaded bytecode is not recoverable and it carries no analysis logic.
        # Match on ``module.__name__`` because ``__mp_main__`` aliases the
        # launcher's module object under a different sys.modules key.
        if source is None or module.__name__ == "__main__":
            continue
        source_path = Path(source).resolve()
        if root not in source_path.parents and source_path != root:
            continue
        _verify_module_loaded_code_matches_disk(module)
        checked.append(name)
    missing = [
        name for name in (_ANALYSIS_ENTRY_MODULE, "code_revision") if name not in checked
    ]
    if missing:
        raise RuntimeError(
            "Cannot prove the loaded code of: "
            + ", ".join(sorted(missing))
            + "; refusing to attribute a run to unprovable code"
        )
