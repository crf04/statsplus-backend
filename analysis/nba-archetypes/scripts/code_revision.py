"""Code-revision snapshot and import-time loaded-code proof for one Analysis Run.

The matchup script must bind a run id to the exact analysis code Python
actually loaded. A disk-only git snapshot cannot prove that: the entry script
and ``code_revision`` itself are already loaded before any snapshot can be
taken, so a post-load edit would let both snapshots identify newer disk content
while older bytecode executes. The launcher therefore establishes a pre-load
snapshot and calls ``begin_load_proof`` before importing any analysis module;
immediately before publication the script calls
``verify_loaded_code_matches_disk``, which fails closed unless the code every
analysis module actually executed (recorded in-process at import time) provably
matches the current disk source it is attributed to.

Loaded-code evidence is process-local and captured at import time: an import
finder installed by ``begin_load_proof`` records each analysis module's executed
code object in memory the moment it is imported, and the already-loaded launcher
and this module have their evidence captured from the bytecode cache their own
imports just wrote, before any analysis work happens. Verification never reads a
shared ``__pycache__`` file after the fact, so another process replacing a cache
file cannot make different loaded code look like the disk source. This module is
deliberately stdlib-only and free of any analysis code, so importing it can
never load the modules whose revision it records.
"""

import hashlib
import importlib.abc
import importlib.machinery
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


# Process-local evidence of the code each analysis module actually executed.
# ``name -> code object``: recorded by the proof finder at the moment a module
# is imported (the entry and its imports), or captured from the bytecode cache
# their own imports just wrote when ``begin_load_proof`` runs (the launcher and
# this module, which are already loaded). Verification reads only this in-memory
# evidence; it never re-reads a shared ``__pycache__`` file another process could
# replace after the fact.
_LOADED_CODE: dict = {}
_LAUNCHER_FILE: Path | None = None
_LAUNCHER_KEY: str | None = None
_PROOF_BEGUN = False


class _RecordingLoader(importlib.abc.Loader):
    """Loader wrapper that records the code object a module actually executed.

    All other loader behavior is delegated to the wrapped loader, so wrapping
    never changes what gets imported; only the executed code object is captured
    (in process-local memory, at import time) for later verification.
    """

    def __init__(self, loader, record):
        object.__setattr__(self, "_loader", loader)
        object.__setattr__(self, "_record", record)

    def __getattr__(self, name):
        return getattr(self._loader, name)

    def create_module(self, spec):
        create = getattr(self._loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module):
        # Record the code object that is about to execute *before* exec runs:
        # the module's own top level may verify its loaded code (the analysis
        # entry script does), and its evidence must already be available then.
        try:
            code = self._loader.get_code(module.__name__)
        except Exception:
            code = None
        self._record(module.__name__, code)
        self._loader.exec_module(module)


class _LoadProofFinder(importlib.abc.MetaPathFinder):
    """Import finder that wraps analysis modules to record their loaded code.

    Only modules whose source lives under the analysis root are wrapped; every
    other import (third-party libraries, stdlib) is returned untouched. The
    recorder is installed by ``begin_load_proof`` before the entry script is
    imported, so every analysis module's executed code object is captured at the
    moment it is imported, synchronously in this process.
    """

    def __init__(self, root, record):
        self._root = root
        self._record = record

    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.origin is None:
            return spec
        origin = Path(spec.origin).resolve()
        if not (origin == self._root or self._root in origin.parents):
            return spec
        loader = getattr(spec, "loader", None)
        if loader is None or not hasattr(loader, "exec_module") or not hasattr(
            loader, "get_code"
        ):
            return spec
        spec.loader = _RecordingLoader(loader, self._record)
        return spec


def _evidence_key(module) -> str:
    """The key a module's loaded-code evidence is recorded under.

    Under ``python -m`` the launcher is executed as ``__main__`` but was
    imported under its real name, so the module's ``__spec__.name`` is the
    import name the bytecode cache and evidence are keyed by; a directly
    executed script has no spec and falls back to its module name.
    """
    spec = getattr(module, "__spec__", None)
    if spec is not None and spec.name:
        return spec.name
    return module.__name__


def _find_loaded_module(path) -> object | None:
    """The loaded module whose source file is ``path``, if any."""
    for module in list(sys.modules.values()):
        source = getattr(module, "__file__", None)
        if source and Path(source).resolve() == path:
            return module
    return None


def _capture_loaded_code(module, *, require_cache=False) -> None:
    """Record the code object a module that is already loaded actually executed.

    The bytecode cache this module's own import just wrote holds the loaded
    code even if the disk source has since been edited, so it is the only
    trustworthy evidence for a bootstrap module. When ``require_cache`` is set
    (the launcher), its absence fails closed: recompiling the current disk
    source would prove whatever the disk now holds rather than what loaded, and
    the launcher is precisely the module whose loaded code must be proven.
    """
    name = _evidence_key(module)
    code = None
    cached = getattr(module, "__cached__", None)
    if cached and Path(cached).exists():
        try:
            code = _code_from_bytecode_cache(Path(cached))
        except (OSError, ValueError, SyntaxError, UnicodeError):
            code = None
    if code is None and not require_cache:
        loader = getattr(module, "__loader__", None)
        if loader is not None and hasattr(loader, "get_code"):
            try:
                code = loader.get_code(name)
            except Exception:
                code = None
    if code is None:
        raise RuntimeError(
            f"Cannot prove the loaded code of {module.__name__}: no loaded-code "
            "evidence is recoverable for the launcher bootstrap; run the "
            "analysis as ``python -m run_matchup_analysis`` so its own loaded "
            "code is provable"
        )
    _LOADED_CODE[name] = code


def _record_loaded_code(name, code) -> None:
    if code is not None:
        _LOADED_CODE[name] = code


def begin_load_proof(analysis_dir, launcher_file=None) -> None:
    """Establish process-local loaded-code evidence before analysis imports.

    Called by the launcher as its first action, before any analysis
    implementation module is imported. The launcher and this module (already
    loaded) have their evidence captured now, while the disk still holds what
    was loaded; every later analysis import is recorded in-memory by the proof
    finder at import time. Idempotent, so the ``-m`` launcher re-execution
    cannot double-install the finder.
    """
    global _PROOF_BEGUN, _LAUNCHER_FILE, _LAUNCHER_KEY
    if _PROOF_BEGUN:
        return
    root = Path(analysis_dir).resolve()
    if launcher_file is not None:
        _LAUNCHER_FILE = Path(launcher_file).resolve()
        launcher = _find_loaded_module(_LAUNCHER_FILE)
        if launcher is None:
            raise RuntimeError(
                "Cannot locate the loaded launcher module; refusing to build "
                "an unattributable run"
            )
        # The launcher's loaded code must be proven from its own bytecode cache,
        # not recompiled from whatever the disk holds now; a plain ``python
        # run_matchup_analysis.py`` has no recoverable evidence and fails closed.
        _capture_loaded_code(launcher, require_cache=True)
        _LAUNCHER_KEY = next(
            (key for key, module in sys.modules.items() if module is launcher),
            None,
        )
    this_module = sys.modules.get(__name__)
    if this_module is not None:
        _capture_loaded_code(this_module)
    sys.meta_path.insert(0, _LoadProofFinder(root, _record_loaded_code))
    _PROOF_BEGUN = True


def _verify_module_loaded_code_matches_disk(module) -> None:
    """Fail closed unless a loaded analysis module provably matches its disk source.

    The module's code object was recorded in this process at import time; if the
    disk source has since been edited (or another process swapped the shared
    bytecode cache), recompiling the current disk source yields a different code
    object and the comparison fails.
    """
    source_file = Path(module.__file__)
    try:
        loaded_code = _LOADED_CODE[_evidence_key(module)]
        disk_code = _code_from_source_file(source_file)
    except KeyError as error:
        raise RuntimeError(
            f"Cannot prove the loaded code of {module.__name__}: no import-time "
            "evidence was captured for it; refusing to attribute a run to "
            "unprovable code"
        ) from error
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

    The analysis entry script must be loaded as a module through the launcher
    (``python -m run_matchup_analysis``), which establishes import-time,
    process-local evidence for its own loaded code and every analysis module it
    imports. Every analysis module loaded from under the analysis root is then
    checked module by module against the code this process actually executed,
    and any module whose loaded code cannot be proven to equal its current disk
    source (including the launcher itself, whose evidence was captured at
    bootstrap) aborts publication.
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
    if not _PROOF_BEGUN:
        raise RuntimeError(
            "No import-time loaded-code proof was established; the analysis "
            "entry script must run through its launcher so its loaded code is "
            "provable"
        )
    checked = []
    for name, module in list(sys.modules.items()):
        source = getattr(module, "__file__", None)
        if source is None:
            continue
        source_path = Path(source).resolve()
        if root not in source_path.parents and source_path != root:
            continue
        _verify_module_loaded_code_matches_disk(module)
        checked.append(name)
    required = [_ANALYSIS_ENTRY_MODULE, "code_revision"]
    if _LAUNCHER_KEY is not None:
        required.append(_LAUNCHER_KEY)
    missing = [name for name in required if name not in checked]
    if missing:
        raise RuntimeError(
            "Cannot prove the loaded code of: "
            + ", ".join(sorted(missing))
            + "; refusing to attribute a run to unprovable code"
        )
