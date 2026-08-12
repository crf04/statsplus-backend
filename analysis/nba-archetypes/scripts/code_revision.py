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

The supported launch boundary is plain script execution of the launcher
(``python run_matchup_analysis.py``), which ignores ``__pycache__``: the
``__main__`` module is always compiled from disk source, never from a shared
bytecode cache, so no stale pre-bootstrap bytecode can execute before the proof
is established. The launcher records its own actual executing frame code (the
exact module code the interpreter compiled from source) and source-loads this
module exactly once — read from disk, compiled once, and executed once into a
fresh module before any attributable analysis import, with that exact code
object recorded — so this module's shared bytecode cache is never consulted by
the supported path either.

Loaded-code evidence is process-local and captured the moment each module's
code object starts executing. Analysis modules are recorded by an import
finder installed by ``begin_load_proof``: the finder fetches each module's code
object exactly once, records that same object, and executes that same object
(instead of asking the loader a second time), so nothing can record one code
object while a different one runs. When this module is imported directly
(outside the launcher) its own trusted bootstrap reads its source once,
compiles it once, records the resulting code object, and then executes that
same object, so the code that runs is provably the recorded code no matter what
the shared ``__pycache__`` holds. Verification never reads a shared cache file
after the fact, so another process replacing a cache file cannot make different
loaded code look like the disk source. This module is deliberately stdlib-only
and free of any analysis code, so importing it can never load the modules whose
revision it records.
"""

import hashlib
import importlib.abc
import importlib.machinery
import json
import subprocess
import sys
import types
from pathlib import Path

# The production analysis script imported by the launcher. Provenance requires
# it to be loaded as a module (not executed as ``__main__``) so its loaded
# bytecode is provable against disk.
_ANALYSIS_ENTRY_MODULE = "archetype_matchups_2025_26"

_THIS_FILE = Path(__file__).resolve()

# Process-local evidence of the code each analysis module actually executed.
# ``name -> code object``: recorded by the proof finder at the moment a module
# is imported (the entry and its imports), or by the bootstrap of this module
# and the launcher, which read/compile once, record the exact code object, and
# then execute that same object. Verification reads only this in-memory
# evidence; it never re-reads a shared ``__pycache__`` file another process
# could replace after the fact.
_LOADED_CODE: dict = {}

if globals().get("__code_revision_bootstrapped__") is None:
    # Trusted bootstrap: this module's own code is already being executed by
    # the import machinery (possibly from a shared bytecode cache another
    # process could replace), so the code object it runs is read once, compiled
    # once, recorded as its loaded-code evidence, and then that same object is
    # executed. The attributable logic below provably runs from the recorded
    # object, never from whatever the shared cache happens to hold.
    globals()["__code_revision_bootstrapped__"] = True
    _bootstrap_source = _THIS_FILE.read_bytes()
    _bootstrap_code = compile(
        _bootstrap_source,
        str(_THIS_FILE),
        "exec",
        dont_inherit=True,
        optimize=sys.flags.optimize,
    )
    exec(_bootstrap_code, globals())
    _LOADED_CODE["code_revision"] = _bootstrap_code
else:
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

    _LAUNCHER_FILE: Path | None = None
    _LAUNCHER_KEY: str | None = None
    _PROOF_BEGUN = False

    class _RecordingLoader(importlib.abc.Loader):
        """Loader wrapper that records and executes the exact same code object.

        The wrapped loader's ``get_code`` is called exactly once; the code object
        it returns is recorded as the module's loaded-code evidence and then that
        same object is executed (as a source loader would: ``exec(code,
        module.__dict__)``). The wrapped ``exec_module`` is never invoked, so a
        second loader fetch cannot record one code object while a different one
        executes.
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
            code = self._loader.get_code(module.__name__)
            self._record(module.__name__, code)
            exec(code, module.__dict__)

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

        Under plain script execution the launcher runs as ``__main__`` with a
        spec whose name is ``__main__``, so the evidence key is the spec name
        when present; a module without a spec (for example the source-loaded
        ``code_revision`` module) falls back to its module name.
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

    def _record_loaded_code(name, code) -> None:
        if code is not None:
            _LOADED_CODE[name] = code

    def record_bootstrap_code(key, code) -> None:
        """Record the exact code object a bootstrap module executes.

        The launcher's trusted bootstrap records the exact code objects it
        executes before any attributable logic runs: its own actual launcher
        frame code (the module code the interpreter compiled from disk source,
        since plain script execution ignores ``__pycache__``) and the exact
        object it compiled and executed when source-loading this module. The
        evidence is immutable process-local memory captured before any
        attributable logic runs — never inferred from a shared bytecode cache
        after the fact.
        """
        _LOADED_CODE[key] = code

    def begin_load_proof(analysis_dir, launcher_file=None) -> None:
        """Establish process-local loaded-code evidence before analysis imports.

        Called by the launcher as its first action, before any analysis
        implementation module is imported. The launcher recorded its own actual
        executing frame code (compiled from disk source, since plain script
        execution ignores ``__pycache__``) and source-loaded this module exactly
        once, recording the exact code object it executes; a missing record
        means that trusted bootstrap did not run and fails closed. Every later
        analysis import is recorded in-memory by the proof finder at import time.
        Idempotent, so a second call cannot double-install the finder.
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
            key = _evidence_key(launcher)
            if key not in _LOADED_CODE:
                raise RuntimeError(
                    "Cannot prove the loaded code of the launcher: it must run "
                    "as a plain script ``python run_matchup_analysis.py`` so its "
                    "actual frame code is recorded and verified"
                )
            _LAUNCHER_KEY = next(
                (key for key, module in sys.modules.items() if module is launcher),
                None,
            )
        if "code_revision" not in _LOADED_CODE:
            raise RuntimeError(
                "Cannot prove the loaded code of code_revision: it was not "
                "source-loaded exactly once into a module before attributable "
                "analysis imports"
            )
        sys.meta_path.insert(0, _LoadProofFinder(root, _record_loaded_code))
        _PROOF_BEGUN = True

    def _verify_module_loaded_code_matches_disk(module) -> None:
        """Fail closed unless a loaded analysis module provably matches its disk source.

        The module's code object was recorded in this process the moment it began
        executing; if the disk source has since been edited (or another process
        swapped the shared bytecode cache), recompiling the current disk source
        yields a different code object and the comparison fails. The disk source
        is compiled with the module's own ``__file__`` as the filename so the
        freshly compiled code's ``co_filename`` equals the filename the recorded
        code was compiled under (the launcher running as a plain script may have
        been invoked with a relative path, and its frame code's ``co_filename``
        is that same relative path).
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
        (``python run_matchup_analysis.py``), which establishes import-time,
        process-local evidence for its own loaded code and every analysis module it
        imports. Every analysis module loaded from under the analysis root is then
        checked module by module against the code this process actually executed,
        and any module whose loaded code cannot be proven to equal its current disk
        source (including the launcher itself, whose actual frame code was recorded
        at bootstrap) aborts publication.
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
