"""Production entry for the 2025-26 archetype matchup analysis.

A run id must be bound to the exact analysis code that executes, but a
disk-only snapshot cannot prove that because the entry script itself is loaded
before any snapshot can be taken. This launcher therefore establishes a
``current_code_revision`` snapshot before importing any analysis implementation
module, records it for the analysis script to bind the run id to, and imports
the analysis script as a normal module (rather than executing it as
``__main__``) so its loaded bytecode is provable. Immediately before
publication the analysis script calls ``verify_loaded_code_matches_disk``,
which fails closed unless every loaded analysis module's bytecode provably
matches its current disk source.

Run from the scripts directory as ``python -m run_matchup_analysis`` (or
``python run_matchup_analysis.py``; the ``-m`` form also makes this launcher's
own loaded code provable).
"""

import os
from pathlib import Path

from code_revision import current_code_revision

ANALYSIS_ROOT = Path(__file__).resolve().parents[1]

# Pre-load snapshot: taken before any analysis implementation module is
# imported, so the run id is bound to the disk state the modules are about to
# be loaded from rather than to whatever the disk holds after the build.
os.environ["STATSPLUS_ANALYSIS_CODE_REVISION"] = current_code_revision(
    ANALYSIS_ROOT
)

import archetype_matchups_2025_26  # noqa: E402, F401  # executes the full analysis run
