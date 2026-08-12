"""Production entry for the 2025-26 archetype matchup analysis.

A run id must be bound to the exact analysis code that executes, but a
disk-only snapshot cannot prove that because the entry script itself is loaded
before any snapshot can be taken. This launcher therefore establishes a
``current_code_revision`` snapshot before importing any analysis implementation
module, records it for the analysis script to bind the run id to, and imports
the analysis script as a normal module (rather than executing it as
``__main__``) so its loaded code is provable.

The launcher records its own loaded code with a trusted bootstrap before any
attributable logic runs: it reads this file once, compiles it once, records the
exact code object, and then executes that same object. The attributable body
below provably runs from that recorded object, never from whatever the shared
``__pycache__`` happens to hold after the fact. ``begin_load_proof`` then
captures ``code_revision``'s own bootstrap record and installs an import-time
recorder that captures every analysis module's executed code in-process, so
verification never has to trust a shared bytecode cache read after the fact.
Immediately before publication the analysis script calls
``verify_loaded_code_matches_disk``, which fails closed unless every loaded
analysis module's code — including this launcher's — provably matches its
current disk source.

Run from the scripts directory as ``python -m run_matchup_analysis`` (the
``-m`` form makes this launcher's own loaded code provable; running it as a
plain script is rejected before any analysis work because the launcher's loaded
bytecode cannot be proven that way).
"""

import os
import sys
from pathlib import Path

import code_revision as _code_revision

_LAUNCHER_FILE = Path(__file__).resolve()

if globals().get("__launcher_bootstrapped__") is None:
    # Trusted bootstrap: read this file once, compile it once, record the exact
    # code object, then execute that same object. The attributable body below
    # provably runs from the recorded bytecode, so a concurrent replacement of
    # the shared bytecode cache cannot make different code look like what ran.
    globals()["__launcher_bootstrapped__"] = True
    if getattr(globals().get("__spec__"), "name", None) != "run_matchup_analysis":
        raise RuntimeError(
            "The analysis launcher must run as ``python -m run_matchup_analysis`` "
            "so its trusted bootstrap records and executes one code object; "
            "running it as a plain script cannot prove which code ran"
        )
    _bootstrap_source = _LAUNCHER_FILE.read_bytes()
    _bootstrap_code = compile(
        _bootstrap_source,
        str(_LAUNCHER_FILE),
        "exec",
        dont_inherit=True,
        optimize=sys.flags.optimize,
    )
    _code_revision.record_bootstrap_code("run_matchup_analysis", _bootstrap_code)
    exec(_bootstrap_code, globals())
else:
    from code_revision import begin_load_proof, current_code_revision

    ANALYSIS_ROOT = _LAUNCHER_FILE.parents[1]

    # Loaded-code proof bootstrap: the launcher and code_revision recorded their
    # own loaded code at import time (read/compile once, record, execute the
    # same object), and the import recorder is installed here to capture every
    # analysis module's executed code.
    begin_load_proof(ANALYSIS_ROOT, launcher_file=_LAUNCHER_FILE)

    # Pre-load snapshot: taken before any analysis implementation module is
    # imported, so the run id is bound to the disk state the modules are about to
    # be loaded from rather than to whatever the disk holds after the build.
    os.environ["STATSPLUS_ANALYSIS_CODE_REVISION"] = current_code_revision(
        ANALYSIS_ROOT
    )

    import archetype_matchups_2025_26  # noqa: E402, F401  # executes the full analysis run
