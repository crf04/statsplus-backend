"""Code-revision snapshot for one Analysis Run.

The matchup script must bind a run id to the exact analysis code Python
actually loaded, so the revision snapshot has to be captured before the
implementation modules are imported and re-checked immediately before
publication. This module is deliberately stdlib-only and free of any analysis
code, so importing it can never load the modules whose revision it records.
"""

import hashlib
import json
import subprocess
from pathlib import Path


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
