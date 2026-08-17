# Matchup materializer parity and legacy writer fencing

This runbook covers the bounded dual-run that proves the legacy provider-
aggregate matchup writer and the ledger materializer agree, the operator
decision that records that evidence, and the fence that stops the legacy writer
from competing with an activated ledger-owned stream. No provider call is made
by the parity command or the recomposition worker; both read only stored facts
and the immutable governed authority.

## Why parity is required

The ledger materializer derives every contracted non-shot opponent fact from
stored Canonical Game Ledger counts, while the legacy
`TeamMatchupRefreshService` aggregates NBA Stats and PBP totals endpoints for
the same Season and exact-L15 windows. Both produce the same disposable
`team_matchup_facts` surface rows, so their outputs never coexist in one stored
snapshot — a fact identity has no provider dimension and the second writer
replaces the first. Before the legacy writer is fenced and the ledger-owned
`traditional_opponent_*` and `assist_locations_*` streams are activated, the
two materializers must be shown to select the same 30 governed teams, the same
exact Season/L15 game sets, and the same contracted counts.

## Producing the two sides independently

Run each materializer into its own isolated store (or capture its output in
memory), then serialize each side as a materialization document:

```json
{
  "season": "2025-26",
  "window": "season",
  "cutoff": "2025-11-01T00:00:00+00:00",
  "facts": [
    {"team_id": 1, "base": "traditional", "stat_key": "OPP_REB",
     "raw_value": 10, "denominator_value": 48.0, "denominator_unit": "minutes"}
  ],
  "observations": [{"surface": "traditional", "status": "available"}],
  "game_ids_by_team": {"1": ["0022500001"]}
}
```

The `cutoff` must be the exact aware immutable manifest cutoff, never a
fabricated midnight: it is the same cutoff both materializers ran at, and it is
what the artifacts are bound to.

## Bounded dual-run

```sh
./scripts/matchup_parity.py compare 2025-26 season \
  --database-url "$DATABASE_URL" \
  --cutoff 2025-11-01T00:00:00+00:00 \
  --legacy-json legacy.json \
  --ledger-json ledger.json \
  --publications-json publications.json
```

Run `l15` the same way for the exact-L15 window. The command resolves the
governed 30-team roster and exact per-team game sets from the checksummed
immutable Event Catalog publication bound to the active manifest — never the
mutable stored event table — and compares the two documents. It reads no
provider and never reads or advances a `PublicationPointer`.

`publications.json` maps each ledger-owned stream to its inactive publication:

```json
{
  "traditional_opponent_season": {"publication_id": "<id>", "payload_checksum": "<sha256>"},
  "assist_locations_season": {"publication_id": "<id>", "payload_checksum": "<sha256>"}
}
```

## What the report proves

Each report covers one surface (`traditional` or `assist_locations`) in one
window and states: League-Complete 30-team coverage, whether team identities
and exact governed game sets match (proven by byte-identical game-set
checksums), whether the two cutoffs align, whether deterministic rankings
(`1, 1, 3` ties) match per metric, and each surface's independent availability.
Integer counts compare exactly. Only floating denominators (effective team
minutes, with seconds normalized to minutes) and the per-48 rates recomputed
from counts and denominators use the single documented tolerance
(`MATCHUP_PARITY_TOLERANCE`, `1e-6`). A missing surface, a single missing
metric, an unavailable observation, or any classified difference makes the
report `adjudication_required`; it is never exact.

The artifact is bound to the report's own surface, window, exact aware cutoff,
publication, and payload checksum. An L15 artifact can never authorize a Season
stream, and both `assist_locations_*` streams are parity-required for
activation exactly like the traditional and per-36 streams.

## Adjudication

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
