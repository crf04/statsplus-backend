"""Production entry for the 2025-26 archetype matchup analysis.

A run id must be bound to the exact analysis code that executes, but a
disk-only snapshot cannot prove that because the entry script itself is loaded
before any snapshot can be taken. This launcher therefore establishes a
``current_code_revision`` snapshot before importing any analysis implementation
module, records it for the analysis script to bind the run id to, and imports
the analysis script as a normal module (rather than executing it as
``__main__``) so its loaded code is provable. ``begin_load_proof`` runs first:
it captures this launcher's own loaded code (and ``code_revision``'s) at the
earliest moment and installs an import-time recorder that captures every
analysis module's executed code in-process, so verification never has to trust
a shared bytecode cache read after the fact. Immediately before publication the
analysis script calls ``verify_loaded_code_matches_disk``, which fails closed
unless every loaded analysis module's code — including this launcher's —
provably matches its current disk source.

Run from the scripts directory as ``python -m run_matchup_analysis`` (the
``-m`` form makes this launcher's own loaded code provable; running it as a
plain script is rejected before any analysis work because the launcher's loaded
bytecode is not recoverable that way).
"""

import os
from pathlib import Path

from code_revision import begin_load_proof, current_code_revision

ANALYSIS_ROOT = Path(__file__).resolve().parents[1]

# Loaded-code proof bootstrap: the launcher and code_revision are already
# loaded, so their evidence is captured now (before any analysis module is
# imported or any disk edit could desynchronize the snapshot), and the import
# recorder is installed to capture every analysis module's executed code.
begin_load_proof(ANALYSIS_ROOT, launcher_file=Path(__file__).resolve())

# Pre-load snapshot: taken before any analysis implementation module is
# imported, so the run id is bound to the disk state the modules are about to
# be loaded from rather than to whatever the disk holds after the build.
os.environ["STATSPLUS_ANALYSIS_CODE_REVISION"] = current_code_revision(
    ANALYSIS_ROOT
)

import archetype_matchups_2025_26  # noqa: E402, F401  # executes the full analysis run
