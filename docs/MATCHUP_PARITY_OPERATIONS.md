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

Prepare the immutable candidates first. This is a database-only operation and
does not enable a stream or move a publication pointer:

```sh
./scripts/matchup_parity.py prepare \
  --database-url "$DATABASE_URL" \
  --season 2025-26 \
  --manifest-id "<exact completed-season manifest id>" \
  --output parity-preparation.json
```

On the residential collection host, collect independent NBA evidence. The
adapter makes explicit `LeagueDashPlayerStats` Regular Season requests for
both `PerMode=Per36` diagnostic rates and `PerMode=Totals` raw integer counts
and minutes; it records the clients' complete wire parameters in the envelope:

```sh
./scripts/matchup_parity.py collect-per36 \
  --database-url "$DATABASE_URL" \
  --season 2025-26 \
  --manifest-id "<same manifest id>" \
  --output per36-provider-evidence.json
```

Ingest that immutable envelope with `capture-per36` below, using the prepared
`player_per36` publication ID, then run `compare`. No operator synthesizes raw
counts, rates, game IDs, request parameters, or checksums.

```sh
./scripts/matchup_parity.py compare \
  --database-url "$DATABASE_URL" \
  --season 2025-26 \
  --manifest-id "<exact manifest id>" \
  --actor "operator@example.com" \
  --output parity-summary.json \
  --target candidate \
  --per36-capture-id "<scoped per-36 capture id>"
```

One invocation runs both Season and exact-L15 matchup windows plus the player
Season per-36 comparison. The command resolves the governed 30-team roster and exact per-team game sets from the checksummed
immutable Event Catalog publication bound to the active manifest — never the
mutable stored event table — and compares the stored legacy facts with those
candidate publications. It reads no provider and never reads or advances a
`PublicationPointer`. The explicit manifest does not disambiguate competing
authority: if more than one active schema-v1 canonical-ledger manifest with a
complete bound Event Catalog qualifies for the Season and cutoff, preflight
fails closed.
For issue #117, authority is a completed 2025-26 Regular Season: exactly 30
teams, 82 completed non-postponed games per team, and 1,230 unique games.
`complete: true` on a partial or midseason catalog is not sufficient.

The command composes all five inactive candidate publications from the exact
governed ledger inside its bounded transaction. Operators do not supply
candidate IDs.
Assist-location primitives are optional and scoped by window: missing evidence
produces a bound, empty `unavailable` candidate and a pending artifact for only
that assist window. Traditional Season/L15, player per-36, and an independently
healthy assist window still compose and commit in the same bounded run.
Stdout includes a clearly labeled protected-operator section with every exact
Season/L15 game ID by team. Do not paste that section into trackers; the JSON
summary remains bounded to team IDs, counts, checksums, and artifact IDs.

`--per36-capture-id` identifies an append-only diagnostic capture. It must be
bound to the same candidate checksum, manifest, Event Catalog, Season game
set, request checksum, and exact Season provider window. The legacy
`player_per36_stats` table is not authority and is never read by this command.

Create that capture through the audited operator command, never by inserting
database rows manually:

```sh
./scripts/matchup_parity.py capture-per36 \
  --database-url "$DATABASE_URL" \
  --season 2025-26 \
  --manifest-id "<exact manifest id>" \
  --publication-id "<composed player_per36 candidate id>" \
  --actor "operator@example.com" \
  --input per36-provider-evidence.json \
  --output per36-capture-receipt.json
```

The bounded input contains only `rows`, `provider_window_identity`,
`request_checksum`, and `game_set_checksum`. The command validates the active
manifest, Event Catalog, candidate publication, raw evidence schema and exact
checksums, then creates an immutable source observation, capture artifact and
audit event in one transaction. Its receipt contains IDs and checksums only.
`provider_window_identity.transport_requests` must contain both adapter-captured
`LeagueDashPlayerStats` request descriptors: `player_per36_stats` with
`PerMode=Per36` and `player_totals_stats` with `PerMode=Totals`. Both carry the
exact complete canonical wire parameter map, including empty/default values,
`LeagueID`, `Season`, `SeasonType`, and `MeasureType=Base`. Missing keys, extra
keys, type changes, and default changes are rejected. The request checksum
hashes both complete descriptors; a caller-synthesized date window or selected
parameter summary is not accepted.

## What the report proves

Each report covers one surface (`traditional` or `assist_locations`) in one
window and states: exact 30-team coverage, whether team identities
and exact governed game sets match (proven by byte-identical game-set
checksums), whether the two cutoffs align, whether deterministic rankings
(`1, 1, 3` ties) match per metric, and each surface's independent availability.
Integer counts compare exactly. Only floating denominators (effective team
minutes, with seconds normalized to minutes) and the per-48 rates recomputed
from counts and denominators use the single documented tolerance
(`MATCHUP_PARITY_TOLERANCE`, `1e-9`). The ledger payload's served per-48 and
competition-rank values must also match the values recomputed from its counts
and denominator; missing or incorrect served values are hard failures. Reports
retain exact legacy and ledger game-ID maps/checksums and both sides' manifest/
Event Catalog identities;
activation validates these fields against the candidate rather than trusting a
boolean status, and recomputes the report game-set and candidate payload
checksums before activation. A missing surface, a single missing
metric, an unavailable observation, an authority/scope/cutoff mismatch, an
integer or game-set failure, or a byte-contract failure is `failed` and cannot
be approved. It remains `pending_adjudication` until an operator records an
audited rejection; rejection records actor, timestamp, and reason. Required
denominator/rate mismatches likewise cannot be approved; provider rounding is
retained only as diagnostic context.
Ranking differences are hard failures under deterministic #117 rankings.
Well-formed per-36 identity, raw-count, game-count, team-identity, or minute
differences are likewise durable blocking differences: the command persists
them row by row as `pending_adjudication` and exits `2`. Malformed or unbound
per-36 capture evidence is invalid input, is rolled back, and exits `3`.

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
approved report. A report with no differences is recorded `exact`
automatically. Every difference artifact starts `pending_adjudication`.
Hard-failed evidence may be audited as rejected but is never approvable, even
if an operator attempts to change its decision directly.

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
activation. See
[DATABASE_FIRST_ACTIVATION.md](DATABASE_FIRST_ACTIVATION.md) for the
activation order, the evidence each stream requires, rollback, and what stays
in place until a separate approved cleanup.
