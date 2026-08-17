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

Run the legacy provider aggregate refresh and the ledger materializer through
their normal write seams. The parity command reads the legacy writer's stored
facts and their persisted immutable provider-window evidence, plus the
immutable candidate `PublicationVersion` rows; it does not accept
caller-authored materialization JSON. Legacy rows must carry a non-null exact
cutoff, manifest/Event Catalog publication ID and checksum, and provider
window identity. LeagueDashTeamStats and PBP totals are aggregate endpoints and
are not treated as membership evidence: production runs call the independent
NBA Stats TeamGameLog detail endpoint for traditional IDs and the independent
PBP Stats team game-log detail endpoint for assist IDs, compare each returned
set with immutable authority, and bind both aggregates to their own evidence.
Missing IDs, wrong same-count IDs, or an unavailable detail endpoint fail the
window closed; game-point equality alone is insufficient.
The `cutoff` must be the exact
aware immutable manifest cutoff, never a fabricated midnight: it is the same
cutoff both materializers ran at, and it is what the artifacts are bound to.
The refresh locks and snapshots the active manifest, `collect_before`,
`canonical_game_ledger` scope/schema-v1, Event Catalog identity/checksum, and
canonical-ledger pointer before provider I/O, then revalidates the same rows
after collection in the transaction that persists the legacy snapshots; any
authority, status, cutoff, or pointer drift fails the window closed.

## Bounded dual-run

```sh
./scripts/matchup_parity.py compare 2025-26 season \
  --database-url "$DATABASE_URL" \
  --cutoff 2025-11-01T00:00:00+00:00 \
  --publications-json publications.json
```

Run `l15` the same way for the exact-L15 window. The command resolves the
governed 30-team roster and exact per-team game sets from the checksummed
immutable Event Catalog publication bound to the active manifest — never the
mutable stored event table — and compares the stored legacy facts with those
candidate publications. It reads no provider and never reads or advances a
`PublicationPointer`.

`publications.json` maps both required ledger-owned streams for the selected
window to their inactive candidate publication IDs:

```json
{
  "traditional_opponent_season": "<publication id>",
  "assist_locations_season": "<publication id>"
}
```

## What the report proves

Each report covers one surface (`traditional` or `assist_locations`) in one
window and states: exact 30-team coverage, whether team identities
and exact governed game sets match (proven by byte-identical game-set
checksums), whether the two cutoffs align, whether deterministic rankings
(`1, 1, 3` ties) match per metric, and each surface's independent availability.
Integer counts compare exactly. Only floating denominators (effective team
minutes, with seconds normalized to minutes) and the per-48 rates recomputed
from counts and denominators use the single documented tolerance
(`MATCHUP_PARITY_TOLERANCE`, `1e-6`). The ledger payload's served per-48 and
competition-rank values must also match the values recomputed from its counts
and denominator; missing or incorrect served values are hard failures. Reports
retain exact legacy and ledger game-ID maps/checksums and both sides' manifest/
Event Catalog identities;
activation validates these fields against the candidate rather than trusting a
boolean status, and recomputes the report game-set and candidate payload
checksums before activation. A missing surface, a single missing
metric, an unavailable observation, an authority/scope/cutoff mismatch, an
integer or game-set failure, or a byte-contract failure is `failed` and cannot
be adjudicated. Only the documented floating semantic differences may be
`adjudication_required`; they are the sole differences an operator may approve.
Ranking differences are hard failures under deterministic #117 rankings.

The artifact is bound to the report's own surface, window, exact aware cutoff,
publication, payload checksum, and exact game-set/authority evidence. An L15
artifact can never authorize a Season stream. Activation deterministically
selects the newest fully valid artifact per required stream, ignoring
rejected/superseded historical reruns. All four selected Season+L15
traditional+assist artifacts must share one exact manifest/Event Catalog/cutoff
authority, and the candidate/artifact supplied for activation must be that
selected member; a one-window CLI run or one-stream activation attempt is
rejected. Both
`assist_locations_*` streams are parity-required for activation exactly like
the traditional and per-36 streams.

## Adjudication

```sh
./scripts/matchup_parity.py adjudicate "<artifact_id>" approved \
  --database-url "$DATABASE_URL" \
  --actor "operator@example.com" \
  --reason "dual-run reviewed against the governed game sets"
```

An approved soft-difference artifact is durable activation evidence; a rejected
artifact keeps the stream unactivable until a new dual-run records an exact or
approved report. Hard-failed evidence is never approvable, even if an operator
attempts to change its decision directly.

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
