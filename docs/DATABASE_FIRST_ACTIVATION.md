# Database-first Matchups activation, rollback, and cleanup

How an operator turns a validated inactive Publication into the served source
for a Matchups surface, how to reverse that, and what stays deliberately
undone until a separate approved cleanup.

This is the runbook [MATCHUP_PARITY_OPERATIONS.md](MATCHUP_PARITY_OPERATIONS.md)
hands off to. That document ends once approved parity artifacts exist and the
legacy writers are fenced; everything below starts from that state.

Activation is per stream. Nothing here composes data, and nothing here is
reversible by editing a table: every transition goes through an audited
operator command that moves the fenced pointer atomically.

## Before you start

Activation refuses to run without the evidence #117 produced. Confirm you have,
for the stream you are about to activate:

- the exact candidate `publication_id` and its payload checksum;
- the parity `artifact_id`, with status `exact` or decision `approved` (an
  artifact is approvable only when its differences are all soft — including
  in-bound `official_scorekeeper_correction` differences recorded under the
  parent-approved rule — see `MATCHUP_PARITY_OPERATIONS.md`);
- the manifest and Event Catalog identities both are bound to;
- proof that the production pointer has not moved since the artifact was cut.

A parity artifact that was rejected, superseded, cut against a different
season, cutoff, or publication, or whose checksum no longer matches the
candidate, cannot authorize activation. Recomposing a candidate changes its
checksum and invalidates the artifact, so a correction means a new parity run
and a new adjudication.

The ledger-owned streams activate as one governed cohort. Activation of any
member requires a validated artifact for every sibling — traditional
Season and L15 and `player_per36`, plus assist Season and L15 whenever the
bound candidate carries assist primitives — all on a single manifest and
catalog authority, against a complete 82-game Regular Season. A partial cohort
fails closed with `ledger_parity_cohort_incomplete`.

## What is and is not ledger-owned

| Stream key | Owner | Activation fences the legacy writer? |
| --- | --- | --- |
| `traditional_opponent_season` | ledger | yes |
| `traditional_opponent_l15` | ledger | yes |
| `assist_locations_season` | ledger | yes |
| `assist_locations_l15` | ledger | yes |
| `player_per36` | ledger | yes |
| `exact_shot_zones_opponent_*` | nba | no |
| `grouped_shot_types_opponent_*` | nba | no |
| `synergy_play_types_opponent_season` | nba | no |
| `synergy_play_types_opponent_l15` | nba | never activates; `provider_window_unsupported` |

NBA-owned surfaces are written through the governed publication capability and
are not touched by the legacy write fence. Activating a ledger stream must
never disable an NBA-owned writer.

That column describes the governed publication writers. It does not describe
the nightly *legacy* refresh: `DataService` checks the fence for the legacy
opponent tables against their NBA-owned streams as well, so enabling
`synergy_play_types_opponent_season`, `grouped_shot_types_opponent_season`, or
`exact_shot_zones_opponent_season` refuses the corresponding legacy table in
the nightly refresh. See the inventory below.

`traditional_opponent` and `assist_locations` are legacy aggregate keys, not
activation targets. Activation rejects them with `stream_unavailable`; use the
explicit Season or L15 key.

`player_game_logs` is parity-gated in the same way and needs the same evidence
fields, but it is not part of the Matchups cohort: its database-first path and
response parity were completed separately, and activating it neither requires
nor is required by the streams above.

## What activation does to the nightly legacy refresh

`DataService.update_all_data` (the `update_database` durable job and the
Nightly Refresh `stats` step) collects every legacy table in memory, then
partitions the collected frames against the fence before publishing:

- a table whose stream is activated is **refused and skipped** — it is never
  written, and the refusal is logged as `legacy_write_fenced` with the table
  and the stream whose activation fenced it;
- every remaining table is published in one atomic set with the stats
  freshness completion, exactly as before.

One activated stream therefore fences only its own table(s). It cannot abort
the publication of a table that is still the only source for its readers, and
the refresh reports success. The authoritative fence check still runs inside
the publication transaction, on the connection that swaps the tables, so a
stream activated between the partition and the swap still fails that
publication closed rather than overwriting a publication.

When *every* collected table is fenced, the refresh publishes nothing,
logs that it published nothing, and still succeeds: no retry can change the
outcome, and failing would fail the whole nightly unit. Stats freshness is
deliberately **not** advanced in that case, because no legacy table was
written.

A fence that cannot be read (an unregistered stream, an unreachable control
plane) is not a refusal: it fails the whole refresh closed, as before.

### Inventory of the `update_database` tables

Production state is the 2026-08-24 activation cycle recorded on backend #87.

"Fenced" means the nightly refresh refuses the table. "Reader cut over" says
whether the surface that reads it now serves the publication instead — a
fenced table whose reader is *not* cut over is frozen, not superseded.

| Legacy table | Stream | Activated in production | Nightly refresh | Reader cut over | Reader(s) |
| --- | --- | --- | --- | --- | --- |
| `general_opponent_stats` | `traditional_opponent_season` | yes | fenced | yes — superseded | legacy parity snapshot only |
| `player_per36_stats` | `player_per36` | yes | fenced | yes — superseded | `PlayerService._per36_frame`, only when the publication does not serve |
| `team_play_types` | `synergy_play_types_opponent_season` | yes | fenced | yes — superseded | none |
| `catch_and_shoot`, `pullups`, `less_than_10_ft` | `grouped_shot_types_opponent_season` | yes | fenced | yes — superseded | none |
| `opp_shooting_zone` | `exact_shot_zones_opponent_season` | yes | fenced | yes — superseded | none |
| `processed_team_assists` | `assist_locations_season` | yes | fenced | yes — superseded | none |
| `pbp_opponent_stats` | `assist_locations_season` | yes | fenced | n/a (refresh input) | `DataService.process_assist_data` input |
| `player_play_types` | `synergy_play_types` | no | still refreshed | n/a | `PlayerService.get_all_players`, player play-type profile, NL parser player names |
| `player_shooting_zones` | `exact_shot_zones` | no | still refreshed | n/a | player zone-shooting profile |
| `processed_player_assists` | `player_assist_locations` | no | still refreshed | n/a | player assist-location profile |
| `pbp_player_stats` | `player_assist_locations` | no | still refreshed | n/a | `DataService.process_assist_data` input |
| `player_information` | — (no stream) | n/a | always refreshed | n/a | player name resolution, `GameService` allowed tables, `database_utils` |

No **frozen** row remains: the Team Profile read cutover (crf04/statsplus#45)
moved `GET /api/teams/stats` onto the Season publications, so its Traditional,
Playtypes, Shooting Type, Zone Shooting, and Assists categories now serve the
same values the Matchups Defense Sheet does. `stats_freshness` advancing after
a nightly refresh still does **not** cover the fenced tables — it records only
the tables that were actually published, which excludes every fenced row.

The fenced rows stay in the refresh code and in this table on purpose: they
are refused per night rather than deleted, so a rollback that disables a
stream restores that table's refresh with no code change. Removing a legacy
writer is the separately approved cleanup below.

Team Filter rankings and the Team Profile categories read the traditional,
shot-type, shot-zone, play-type, and assist-location **publications**, not
these tables; the legacy tables above are read only by the surfaces named in
the last column.

## Activating one stream

```
POST /admin/collection/streams/<stream_key>/activate
```

Admin authentication is required. The body carries the evidence:

| Field | Required | Meaning |
| --- | --- | --- |
| `reason` | yes | Why this stream is being activated now. Recorded as immutable evidence. |
| `season` | yes for ledger streams | Season the candidate belongs to. |
| `cutoff` | yes for ledger streams | ISO-8601 with a timezone. A naive timestamp is rejected. |
| `artifact_id` | yes for ledger streams | The adjudicated parity artifact. |
| `candidate_publication_id` | yes | The exact inactive candidate being promoted. |

The call returns `202` with a `job_id`. Activation is recorded once per
stream: repeating it with the same candidate returns
`activation_already_recorded` rather than advancing a second generation.

Activate in dependency order and one stream at a time, confirming each before
the next. Season before L15 within a surface, and traditional before assist
locations, so a failure leaves the smallest possible mixed state.

### Enabling an NBA-owned stream for its first collection

NBA-owned streams are registered disabled, and ingestion refuses observations
for a disabled stream with `provider_not_registered`. Their first candidate
therefore cannot exist before the stream is enabled. For an NBA-owned
`snapshot_replace` stream that has no active pointer yet, activation accepts a
body carrying only `reason`:

```
POST /admin/collection/streams/synergy_play_types_opponent_season/activate
{"reason": "enable for first collection"}
```

The response reads `enabled: true` and no pointer moves; public reads for the
surface stay `missing` until composition produces and activates a candidate.
This is an enable, not a promotion. It applies to nothing else: a ledger stream
and an NBA-owned stream that is already bound to a publication both still
require `candidate_publication_id`, and every parity gate below is unchanged.

### When activation refuses

Every one of these is the gate working. None should be worked around.

| Code | HTTP | What it means |
| --- | --- | --- |
| `ledger_parity_evidence_required` | 400 | A parity-gated stream is missing or has a blank `season`, `cutoff`, `artifact_id`, or `candidate_publication_id`, or the cutoff has no timezone. |
| `ledger_parity_pending` | 400 | Artifact is not `exact` and not `approved`, or is bound to a different candidate or checksum. |
| `ledger_parity_hard_failure` | 400 | Artifact records a blocking difference. |
| `ledger_parity_cohort_incomplete` | 400 | A sibling stream lacks validated evidence, the authorities disagree, or the season is not a complete 82-game Regular Season. |
| `ledger_provenance_not_accepted` | 400 | Candidate cites provenance the ledger has not accepted. |
| `publication_candidate_required` | 400 | No `candidate_publication_id`. |
| `publication_candidate_invalid` | 400 | Candidate is not a promotable candidate for this stream. |
| `publication_checksum_mismatch` | 400 | Candidate payload changed after the artifact was cut. Re-run parity. |
| `stream_unavailable` | 400 | Legacy aggregate key, or a stream that cannot be activated. |
| `activation_already_recorded` | 400 | This activation is already recorded. |
| `stream_not_found` | 404 | Unknown stream key. |

## Confirming an activation

After each stream, verify before moving on:

1. The pointer names the intended publication and the stream reads `enabled`.
2. A public Matchups read for that surface reports the ledger publication as
   its source, with a real `retrieved_at` and `publication_id` — not
   `legacy_database`.
3. Sibling streams that were not activated still read from their previous
   source, with independent freshness. Activation of one surface must not
   change another's provenance.
4. The legacy writer for that surface now fails closed. `DataService` and
   `TeamMatchupRefreshService` raise `legacy_write_fenced` for the activated
   stream, while NBA-owned writers still succeed.
5. Public responses still decode for existing clients across the
   success, degraded, missing, and stale cases.

The authenticated Slate → Matchup → Selection journey is the end-to-end check
that the activated generation serves consistently.

## Rolling back

```
POST /admin/collection/streams/<stream_key>/rollback
```

| Field | Required | Meaning |
| --- | --- | --- |
| `reason` | yes | Non-empty; `reason_required` otherwise. |
| `expected_fence` | recommended | The fence value you observed. Guards against rolling back a pointer that moved underneath you. |

Rollback returns the stream to the immediately previous complete Publication
under the existing fence. It deliberately does **not**:

- disable the stream;
- un-fence any legacy writer;
- restore provider fallback for a ledger-owned surface.

A rolled-back stream is still database-first, serving its prior publication.
If the prior publication is itself unusable, the correct move is a governed
recomposition and a new parity run, not re-enabling the legacy writer.

| Code | HTTP | What it means |
| --- | --- | --- |
| `rollback_unavailable` | 409 | No previous complete publication to return to. |
| `stale_composition` | 409 | `expected_fence` does not match; re-read and retry. |
| `publication_checksum_mismatch` | 400 | Target publication payload does not match its checksum. |
| `reason_required` | 400 | Empty reason. |

## Evidence that must exist before production activation

These are operator runs against real infrastructure. Each writes a report that
is the record of the run; none of them can be replaced by the offline test
suite, and the suite cannot substitute for them.

| Evidence | Command |
| --- | --- |
| Historical Rehearsal over the final seven Eastern dates of the completed season, in chronological cutoff order, against an isolated database | `scripts/database_first_rehearsal.py --database-url … --season 2025-26 --report …` |
| Deterministic failure drills — outage, duplicate delivery, reboot and Outbox replay, expired credentials, provider failure, alert recovery | `scripts/database_first_drills.py --database-url … --report …` |
| Isolated Postgres restore proving ledger completeness, pointer state, audit evidence, idempotent replay, and measured recovery time | `scripts/database_first_drills.py --production-database-url … --restored-database-url … --restore-expectations … --report …` |
| Production-like Matchups benchmark with retained query plans | `scripts/benchmark_matchups.py --database-url … --game-id … --iterations 20 --report …` |

The rehearsal runs against an isolated restore or disposable database and must
leave production pointers byte-for-byte unchanged; its report records whether
that held. The benchmark must show zero request-time provider calls, bounded
indexed queries, and a p95 under one second within 10% of the recorded
baseline.

## Cleanup — deliberately not now

Legacy tables stay present and readable after activation. They are the
diagnostic and rollback-investigation surface, and #117's fencing makes them
inert for writes rather than absent.

Do not, in this activation:

- drop or truncate `general_opponent_stats`, `player_per36_stats`,
  `processed_team_assists`, or `pbp_opponent_stats`;
- remove the legacy writer code paths that are now fenced;
- enable any Playoff or Play-In behavior.

The six nightly ranking tables are the one completed exception. Under #199
`general_opponent_stats`, `catch_and_shoot`, `pullups`, `less_than_10_ft`,
`team_play_types`, and `processed_team_assists` are refused at the publication
boundary unconditionally, and their collectors are deleted — the activation
fence no longer decides their fate. Their rows stay in place and readable; the
drop migration waits on `GET /api/teams/stats` losing its dependency on them.
One consequence for operators: `LegacyParityDiagnosticReader` now compares the
ledger for `traditional_opponent` against a permanently frozen pre-cutover
`general_opponent_stats` snapshot rather than a refreshed one, and still raises
`required legacy parity table … is unavailable` if that table is missing, so
treat a divergence there as evidence about the frozen snapshot until the drop
retires the diagnostic.

Removal happens only under a separately approved cleanup issue, after the
activated streams have served a full cycle and rollback is no longer expected
to need the legacy read path.
