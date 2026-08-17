# Matchup materializer parity and legacy writer fencing

This runbook covers the bounded dual-run that proves the legacy provider-
aggregate matchup writer and the ledger materializer agree, the operator
decision that records that evidence, and the fence that stops the legacy writer
from competing with an activated ledger-owned stream. No provider call is made
by the parity command or the recomposition worker; both read only stored facts
and the governed Event Catalog.

## Why parity is required

The ledger materializer derives every contracted non-shot opponent fact from
stored Canonical Game Ledger counts, while the legacy
`TeamMatchupRefreshService` aggregates NBA Stats and PBP totals endpoints for
the same Season and exact-L15 windows. Both write the disposable
`team_matchup_facts` read model. Before the legacy writer is fenced and the
ledger-owned `traditional_opponent_*` and `assist_locations_*` streams are
activated, the two materializers must be shown to select the same 30 governed
teams, the same exact Season/L15 game sets, and the same contracted counts.

## Bounded dual-run

With both materializers having written the same season and Eastern as-of, run
the comparator from the backend root:

```sh
./scripts/matchup_parity.py compare 2025-26 season \
  --database-url "$DATABASE_URL" --as-of 2026-04-15 \
  --record --stream-key traditional_opponent_season \
  --publication-id "<inactive publication id>" \
  --payload-checksum "<64-char sha256>"
```

Run `l15` the same way for the exact-L15 window (stream keys
`traditional_opponent_l15` and `assist_locations_l15`). The command re-derives
the legacy per-team game selection from the stored Event Catalog with the same
resolver the legacy writer uses, compares it against the ledger's persisted
`game_ids`, and prints the report.

The report states League-Complete 30-team coverage, whether team identities
and exact game sets match, whether the two cutoffs align, whether deterministic
rankings match, and each surface's availability on both sides. Integer counts
compare exactly. Only floating denominators (effective team minutes, with
seconds normalized to minutes) and the per-48 rates recomputed from counts and
denominators use the single documented tolerance (`MATCHUP_PARITY_TOLERANCE`,
`1e-6`). Every difference carries one classification from the closed
vocabulary in `app.services.matchup_parity`; a report with any difference is
`adjudication_required` and is never exact.

## Adjudication

Approve or reject a recorded artifact with the same flow as the semantic
parity reports:

```sh
./scripts/matchup_parity.py adjudicate "<artifact_id>" approved \
  --database-url "$DATABASE_URL" \
  --actor "operator@example.com" \
  --reason "dual-run reviewed against the governed game sets"
```

An approved artifact is durable activation evidence; a rejected artifact keeps
the stream unactivable until a new dual-run records an exact or approved
report.

## Fencing and activation handoff

Fencing is per stream and per surface. `TeamMatchupRepository.replace_snapshots`
(the legacy writer's path) enforces the injected `LegacyWriteFence` against the
exact ledger-owned stream key for each changed surface, so activating a Season
stream fences only that stream's writer and a traditional write never fences
assist locations. Once a stream is activated, the legacy provider-aggregate
writer for that surface fails closed with `legacy_write_fenced`.

NBA-owned shot zones, grouped shot types, and Synergy play types are not
fenced by ledger activation: their facts and observations are written through
`replace_governed_publication_snapshots`, which verifies the active publication
capability and bypasses the legacy fence. The request-time read fence is
symmetric — an activated ledger-owned stream serves only its immutable
Publication, an inactive stream is the only state that permits the legacy
repository fallback, and an NBA-owned stream never falls back to a PBP or
ledger fact.

Activation and rollback both move the fenced pointer atomically and preserve
the last-good Publication. Backend #87 consumes the inactive validated
Publications plus the recorded parity artifacts to perform database-first
activation.
