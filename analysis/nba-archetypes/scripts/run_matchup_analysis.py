"""Production entry for the 2025-26 archetype matchup analysis.

A run id must be bound to the exact analysis code that executes, but a
disk-only snapshot cannot prove that because the entry script itself is loaded
before any snapshot can be taken. This launcher therefore establishes a
``current_code_revision`` snapshot before importing any analysis implementation
module, records it for the analysis script to bind the run id to, and imports
the analysis script as a normal module (rather than executing it as
``__main__``) so its loaded code is provable.

The supported launch boundary is plain script execution from the scripts
directory: ``python run_matchup_analysis.py``. Plain script execution ignores
``__pycache__``: the ``__main__`` module is always compiled by the interpreter
from its disk source, never from a shared bytecode cache, so no stale
pre-bootstrap bytecode can execute before the code-revision proof is
established. The ``-m`` form is rejected before any analysis work, because it
would load this launcher through the normal import machinery, which may execute
a valid stale ``.pyc`` before the launcher could verify itself — a
self-bootstrap cannot prove pre-check code the import machinery already ran.

The launcher records its own loaded code before any attributable logic runs:
as a plain script, the currently executing frame's code object is the exact
module code the interpreter compiled from disk source, and that actual launcher
frame code is recorded as the launcher's loaded-code evidence.
``code_revision`` is source-loaded the same way — read from disk, compiled
exactly once, and executed exactly once into a fresh module before any
attributable analysis import, with that exact code object recorded — so its
shared bytecode cache is never consulted either. ``begin_load_proof`` then
installs an import-time recorder that captures every analysis module's executed
code in-process, so verification never has to trust a shared bytecode cache
read after the fact. Immediately before publication the analysis script calls
``verify_loaded_code_matches_disk``, which fails closed unless every loaded
analysis module's code — including this launcher's — provably matches its
current disk source.
"""

import os
import sys
import types
from pathlib import Path

# Reject the ``-m`` launch form (and any import-machinery load) before any
# attributable work: the import machinery may execute a valid stale ``.pyc`` of
# this file before the launcher could verify itself, so no self-bootstrap can
# prove the pre-check code it already ran. Plain script execution never consults
# ``__pycache__``, which is why it is the supported boundary.
if getattr(globals().get("__spec__"), "name", None) == "run_matchup_analysis":
    raise RuntimeError(
        "The analysis launcher must run as a plain script ``python "
        "run_matchup_analysis.py``; the ``-m`` form may execute a valid stale "
        "bytecode cache before the code-revision proof can run"
    )

_LAUNCHER_FILE = Path(__file__).resolve()
_CODE_REVISION_FILE = _LAUNCHER_FILE.parent / "code_revision.py"

if globals().get("__launcher_bootstrapped__") is None:
    # Trusted bootstrap, reached only on the first execution of this file as a
    # plain script. The interpreter compiled the code currently executing from
    # disk source (never from ``__pycache__``), so the actual launcher frame
    # code is recorded as the loaded-code evidence for this module.
    globals()["__launcher_bootstrapped__"] = True
    _launcher_frame_code = sys._getframe().f_code

    # Source-load code_revision exactly once: read its disk source, compile it
    # once, and execute that same object into a fresh module before any
    # attributable analysis import. The import machinery (and any shared
    # bytecode cache it would consult) is never used for code_revision, so no
    # stale pre-bootstrap code of code_revision can execute. The exact executed
    # object is recorded as its loaded-code evidence.
    _code_revision_module = types.ModuleType("code_revision")
    _code_revision_module.__file__ = str(_CODE_REVISION_FILE)
    _code_revision_source = _CODE_REVISION_FILE.read_bytes()
    _code_revision_code = compile(
        _code_revision_source,
        str(_CODE_REVISION_FILE),
        "exec",
        dont_inherit=True,
        optimize=sys.flags.optimize,
    )
    # code_revision's own trusted bootstrap re-reads and re-executes its source;
    # pre-setting its flag here makes the fresh module run its attributable body
    # exactly once from the recorded object above.
    _code_revision_module.__dict__["__code_revision_bootstrapped__"] = True
    exec(_code_revision_code, _code_revision_module.__dict__)
    sys.modules["code_revision"] = _code_revision_module
    _code_revision_module.record_bootstrap_code(
        "code_revision", _code_revision_code
    )
    _code_revision_module.record_bootstrap_code("__main__", _launcher_frame_code)

    from code_revision import begin_load_proof, current_code_revision  # noqa: E402

    ANALYSIS_ROOT = _LAUNCHER_FILE.parents[1]

    # Loaded-code proof bootstrap: the launcher and code_revision recorded their
    # own loaded code (the exact objects they execute), and the import recorder
    # is installed here to capture every analysis module's executed code.
    begin_load_proof(ANALYSIS_ROOT, launcher_file=_LAUNCHER_FILE)

    # Pre-load snapshot: taken before any analysis implementation module is
    # imported, so the run id is bound to the disk state the modules are about to
    # be loaded from rather than to whatever the disk holds after the build.
    os.environ["STATSPLUS_ANALYSIS_CODE_REVISION"] = current_code_revision(
        ANALYSIS_ROOT
    )

    import archetype_matchups_2025_26  # noqa: E402, F401  # executes the full analysis run
