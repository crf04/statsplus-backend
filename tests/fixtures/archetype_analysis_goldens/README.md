# Archetype analysis goldens

These files are golden outputs for the archetype matchup Analysis Run builder
(issue 70). They were generated **once** by running the **pre-refactor**
`analysis/nba-archetypes/scripts/archetype_matchups_2025_26.py` from the fixed
point `af5574d5d3a8e821bb63d21fc90684d5203aefed` against the `parity_fixture` in
`tests/test_nba_archetype_analysis_run.py`. They are checked in so the
post-refactor code can be proven behaviorally equivalent to what shipped before
the `AnalysisRunBuilder` refactor; they are not a spec, a seed, or a source of
truth for design.

## Contents

- `*.csv`: every auditable artifact dataframe the pipeline exposes, ordered as
  `GOLDEN_FRAMES` in the test module plus the heatmap matrices
  (`pts_per_min_heatmap.csv`, `volume_heatmap_<METRIC>.csv`).
- `dashboard_scalars.json`: diagnostics breakdown plus the scalar reliability
  and label values surfaced in the dashboard payload.
- `coverage.json`: the run coverage breakdown.
- `heatmap_limits.json`: the dashboard heatmap value limits.

## Provenance

The fixed point's monolithic `archetype_matchups_2025_26.py` contained the full
pre-refactor matchup-analysis implementation; the goldens capture that
implementation's outputs. The `parity_fixture` is deliberately sized so every
subtype x opponent cell exceeds the eligibility thresholds
(`MIN_CELL_PLAYERS=8`, `MIN_CELL_GAMES=20`,
`MIN_CELL_OFFENSIVE_TEAMS=5`), which the pre-refactor path needs to avoid its
degenerate empty-volume-reliability-merge path and produce non-degenerate
outputs for every auditable artifact. The default `synthetic_fixture` used by
the behavioral tests stays below those thresholds, where the canonical builder
still converges but the legacy script could not.

Do not regenerate these files with the refactored builder; the comparison only
means something if the stored values came from the pre-refactor implementation.
To regenerate deliberately, check out the fixed point and run its script
against `parity_fixture` with the same random seed, then re-verify every test in
this module.

## How the goldens enforce behavioral parity

The tests compare the refactored builder's artifacts against these files:

- Frame outputs are compared column-set-wise, row-count-wise, and per column
  with `np.allclose(rtol=1e-4, atol=1e-7)`; NaN patterns must match exactly, and
  non-numeric columns compare byte-for-byte. Categorical/dashboard values
  (diagnostics, subtype labels, coverage counters, heatmap limits) are compared
  directly or with the same tolerance.
- Orders are normalized before comparison, so the tests assert parity of content
  — not of a fragile row sequence.

Together these assert that the refactored `AnalysisRunBuilder` produces the same
observable artifacts and dashboard values as the pre-refactor implementation.
Unchanged source text is not the subject of the comparison.

## Why no duplicate legacy implementation is kept

The pre-refactor `archetype_matchups_2025_26.py` was refactored into the
deterministic, testable `AnalysisRunBuilder` in `matchup_analysis.py`; the
script now is only a data/IO shell around the builder. Keeping a copy of the
old monolithic implementation in the tree would be dead code: nothing imports
it, it is not a library seam, and it would decay independently of the live path.
Storing its frozen outputs instead pins the behavioral contract the refactor
must preserve, without maintaining a second implementation. The golden
comparison is the parity guarantee; a checked-in copy of the legacy source
would add maintenance surface without adding assurance.
