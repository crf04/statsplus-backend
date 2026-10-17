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
| `player_assist_locations` | ledger | yes |
| `rebound_types_season` | ledger | no (no legacy writer exists) |
| `rebound_types_l15` | ledger | no (no legacy writer exists) |
| `player_rebound_types` | ledger | no (no legacy writer exists) |
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

"Fenced" means the nightly refresh refuses the table. "Dropped" means migration
`048_drop_legacy_ranking_tables` removed the storage, so there is no row left to
read or roll back to. "Reader cut over" says whether the surface that reads it
now serves the publication instead — a fenced table whose reader is *not* cut
over is frozen, not superseded.

| Legacy table | Stream | Activated in production | Nightly refresh | Reader cut over | Reader(s) |
| --- | --- | --- | --- | --- | --- |
| `general_opponent_stats` | `traditional_opponent_season` | yes | dropped (#199) | yes — superseded | none |
| `player_per36_stats` | `player_per36` | yes | fenced | yes — superseded | `PlayerService._per36_frame`, only when the publication does not serve |
| `team_play_types` | `synergy_play_types_opponent_season` | yes | dropped (#199) | yes — superseded | none |
| `catch_and_shoot`, `pullups`, `less_than_10_ft` | `grouped_shot_types_opponent_season` | yes | dropped (#199) | yes — superseded | none |
| `opp_shooting_zone` | `exact_shot_zones_opponent_season` | yes | fenced | yes — superseded | none |
| `processed_team_assists` | `assist_locations_season` | yes | dropped (#199) | yes — superseded | none |
| `pbp_opponent_stats` | `assist_locations_season` | yes | fenced | n/a (refresh input) | `DataService.process_assist_data` input |
| `player_play_types` | `synergy_play_types` | no | still refreshed | n/a | NL parser player names; player HTTP reads use Athlete Catalog + Player Diet (#231) |
| `player_shooting_zones` | `exact_shot_zones` | no | still refreshed | yes — publication-first | `PlayerService._zone_shooting_frame`, only while the stream is disabled |
| `processed_player_assists` | `player_assist_locations` (ledger-composed) | no | not collected by `update_database` (#231); activation also fences the separate nightly Diet refresh's `assist_locations` base | yes — superseded | none |
| `pbp_player_stats` | `player_assist_locations` (ledger-composed) | no | not collected by `update_database` (#231); activation also fences the separate nightly Diet refresh's `assist_locations` base | n/a (retired refresh input) | none |
| `player_information` | — (no stream) | n/a | always refreshed | n/a | player name resolution, `GameService` allowed tables |

No **frozen** row remains: the Team Profile read cutover (crf04/statsplus#45)
moved `GET /api/teams/stats` onto the Season publications, so its Traditional,
Playtypes, Shooting Type, Zone Shooting, and Assists categories now serve the
same values the Matchups Defense Sheet does. `stats_freshness` advancing after
a nightly refresh still does **not** cover the fenced tables — it records only
the tables that were actually published, which excludes every fenced row.

The fenced rows stay in the refresh code and in this table on purpose: they
are refused per night rather than deleted, so a rollback that disables a
stream restores that table's refresh with no code change. The **dropped** rows
are the exception and are one-way: their collectors are deleted and their
storage is gone, so disabling a stream does not bring them back. Removing a
legacy writer is the separately approved cleanup below.

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

### Activating `player_assist_locations`

The player assist-location Diet stream is ledger-composed, like `player_per36`
and `player_game_logs`, but it is not part of the Matchups parity cohort and
has no ledger-parity artifact: `activate_stream` does not list it in
`parity_stream`, so its activation body needs only `reason` and
`candidate_publication_id`. The operator sequence:

1. Deploy. `register_default_streams` reconciles the registry row on boot.
2. Run `PublicationService.reconcile_pending(season=..., cutoff=<latest ledger
   cutoff>)`, or wait for the next accepted game to enqueue the composition
   job.
3. Run `scripts/ledger_refresh.py <season> --compose-only` to compose the
   inactive candidate from already-accepted ledger evidence.
4. Run `scripts/player_assist_parity.py --season <season> --players <ids>` to
   compare the candidate against the legacy `player_diet_facts` rows before
   activating. Expect the sampled players to be `exact`. A league-wide run
   (no `--players`) exits 1 with `candidate_only` rows and that is not a
   defect: the legacy table holds at most the 500 players the PBP totals
   response returns, while the ledger covers every player who appeared, so
   the candidate is a strict superset. Only `differs` or `legacy_only` rows
   block activation. The 2026-09-11 rehearsal against the production snapshot
   read 2412/2412 legacy rows exact and 174 candidate-only rows.
5. `POST /admin/collection/streams/player_assist_locations/activate` with the
   candidate's `candidate_publication_id`.
6. Activation also fences the nightly Diet refresh's `assist_locations` base
   (`PlayerDietService.refresh` skips it once `is_activated` reads true), and
   Targets with an assist-location Qualifier become result-cache eligible
   automatically.

### Activating the Rebound Types streams

`rebound_types_season`, `rebound_types_l15` and `player_rebound_types`
(crf04/statsplus#234, backend #446) are ledger-composed like the assist-location
streams, but they have no legacy source, so there is nothing to run a parity
artifact against. They are outside the Matchups parity cohort
(`MATCHUP_REQUIRED_STREAMS` does not name them and activating the traditional
and assist streams never waits on them), and their activation body needs only
`reason` and `candidate_publication_id`. The evidence is an identity: in every
window, each team's six slices add up to the active traditional
`OPP_REB` count. The operator sequence, after the frontend decoder that accepts
both Matchup shapes is merged **and promoted in Vercel**:

Every command below is complete as written; run them from the repository root
(`python -m scripts.<name>` needs no `PYTHONPATH`). Set the target once:

```bash
export DATABASE_URL='postgresql://...'        # the production database (read the value, never paste it into a ticket)
export SEASON=2025-26
export API=https://<backend host>              # the deployed backend
export TOKEN='<an admin Firebase ID token>'    # Authorization: Bearer for the admin routes
```

1. **Deploy.** Migrations `069_ledger_rebound_types` (six nullable fact columns)
   and `070_player_diet_rebound_types_base` (the Diet base constraints) apply at
   boot, and `register_default_streams` registers the three streams disabled.
   Nothing user-visible changes: no stored fact has the counts yet, and a
   disabled stream's base is never served, so each Matchup lists `rebound_types`
   as `missing` with empty rows.
2. **Backfill** the counts from the archived raw rows. No provider is called and
   no composition job is queued:

   ```bash
   python -m scripts.ledger_rebound_types_backfill "$SEASON" --database-url "$DATABASE_URL" --dry-run
   python -m scripts.ledger_rebound_types_backfill "$SEASON" --database-url "$DATABASE_URL"
   python -m scripts.ledger_rebound_types_backfill "$SEASON" --database-url "$DATABASE_URL"
   ```

   The dry run reports `updated` equal to the number of games; the second run
   writes them (the same `updated`, `rejected: []`, `not_observed: 0`); the third
   reports `updated: 0`. Each exits 0 (exit 2 means `rejected` is non-empty or
   migration 069 has not applied). The command is idempotent because the counts
   are a pure function of the archive and only differing columns are written, so
   it can be rerun after any interruption. A game in `rejected` has archived
   parts that do not add up (or, for ordinary PBP evidence, no split at all):
   nothing is written for it and the streams below stay unavailable until it is
   resolved.
3. **Compose** the candidates. The cutoff is the newest active manifest's:

   ```bash
   CUTOFF=$(psql "$DATABASE_URL" -Atc "SELECT replace(to_char(cutoff AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'), ' ', 'T') || '+00:00' FROM collection_manifests WHERE season = '$SEASON' AND status = 'active' ORDER BY cutoff DESC LIMIT 1")
   echo "$CUTOFF"
   python -m scripts.collection_maintenance --database-url "$DATABASE_URL" --season "$SEASON" --cutoff "$CUTOFF"
   python -m scripts.ledger_refresh "$SEASON" --database-url "$DATABASE_URL" --compose-only
   ```

   `collection_maintenance.py` is the deterministic backstop that enqueues the
   composition jobs the backfill deliberately does not (it also runs the usual
   reconciliation and retention); `ledger_refresh.py --compose-only` composes the
   queued jobs from already-accepted ledger evidence. `echo` must print one
   timestamp; if it prints nothing there is no active manifest to compose
   against. A job that fails `rebound_type_evidence_incomplete` means a game has
   not derived the counts or a team's slices do not add up to `OPP_REB`.
4. **Produce the identity evidence** and record its output on
   crf04/statsplus#234:

   ```bash
   python -m scripts.rebound_types_activation_evidence --season "$SEASON" --database-url "$DATABASE_URL" | tee rebound-evidence.json
   ```

   It decodes each candidate through the strict read-side decoder and checks, for
   every team in both windows, the same 30 teams and game IDs against the
   ACTIVE `traditional_opponent_*` publication, the six counts summing to its
   `counts.rebounds`, and the six per-48 values summing to its `per48.rebounds`.
   Exit 0 with `passed: true` and `teams_checked: 60` is the evidence; any
   failure names the team, window and the two counts. A candidate composed at a
   different cutoff than the active traditional publication fails
   `game_set_mismatch`: rerun step 3, do not work around it.
5. **Activate**, Season before L15, then the Diet. Activation re-runs the same
   identity inside its own transaction against the traditional publication it
   locks, so a candidate that fails it, a league short of 30 teams, a missing
   active traditional publication, or a traditional pointer that moved since the
   evidence (`traditional_publication_id` names the one the evidence used) is
   refused:

   ```bash
   for WINDOW in season l15; do
     CANDIDATE=$(jq -r ".windows.$WINDOW.candidate_publication_id" rebound-evidence.json)
     TRADITIONAL=$(jq -r ".windows.$WINDOW.traditional_publication_id" rebound-evidence.json)
     curl -fsS -X POST "$API/api/admin/collection/streams/rebound_types_$WINDOW/activate" \
       -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
       -d "{\"reason\": \"Rebound Types identity evidence passed (statsplus#234)\", \"candidate_publication_id\": \"$CANDIDATE\", \"traditional_publication_id\": \"$TRADITIONAL\"}"
   done
   DIET=$(psql "$DATABASE_URL" -Atc "SELECT publication_id FROM publication_versions WHERE stream_key = 'player_rebound_types' AND season = '$SEASON' AND status = 'candidate' ORDER BY cutoff DESC, version DESC LIMIT 1")
   curl -fsS -X POST "$API/api/admin/collection/streams/player_rebound_types/activate" \
     -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d "{\"reason\": \"Rebound Types diet after the team streams (statsplus#234)\", \"candidate_publication_id\": \"$DIET\"}"
   ```

   The strict decoders (`decode_team_window`, `decode_player_diet`) must also
   accept the exact candidate before its pointer moves. `curl -f` fails the loop
   on any refusal; the refusal codes are `rebound_identity_failed`,
   `rebound_identity_evidence_unavailable` and `traditional_pointer_moved`.

Do steps 2 to 5 promptly after the deploy. The collection lifecycle treats every
stream frozen into a manifest as owed an active publication at the cycle cutoff
(the same rule that applied to `player_assist_locations` before its activation),
so until the three streams are activated it reports them as `missing` for the
open cycle, and a composition job that fails `rebound_type_evidence_incomplete`
because the backfill has not run raises the lifecycle's job-failure alert. Run the
backfill before the next accepted game queues composition.

The Matchup serves the base from an activated publication only: while a stream is
disabled the base is listed as unavailable even though the ledger-materialized
facts exist, because there is no legacy source and no identity evidence until
activation. Activating these streams fences no writer and changes no other base,
row or market.

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
| `publication_family_coupled` | 409 | This stream belongs to a coupled publication family; use the family route below. |

### Coupled publication families

`traditional_opponent_season` and `traditional_opponent_l15` are one product
fact: the Opposing Team Profile, Team Filters, and the Matchups Defense Sheet
would otherwise observe two rendered generations of the same surface at once.
Both the per-stream rollback route above and `activate_stream` therefore
refuse them with `publication_family_coupled` once the family is bound, and
recovery goes through the family route, which moves both windows or neither.

**Do not invoke that route for `traditional_opponent` before reading
"Recovering the traditional-opponent family to v1" below.** This release reads
only v2, so rolling the family back to v1 requires restoring the retained
dual-format release — master merge commit
`88945eb1f2238744ce768424f2eb9710b95e9ce5` (PR #239), Railway deployment
`fd8d71b3-58cf-418c-8af2-4e28299d4820` — *first*. Invoked under this release
the route refuses with `publication_format_unsupported` and moves nothing.

```
POST /admin/collection/publication-rebuilds/<family>/rollback
```

The same coupling applies in the forward direction: after the initial ledger
cutover binds each pointer, the only way to advance this family is a durable
Publication Rebuild, never a per-stream activation.

### Recovering the traditional-opponent family to v1

The deployed code reads **only** the v2 traditional-opponent format. Recovery
to v1 is therefore code first, data second, and must be done in this order:

| Step | Action |
| --- | --- |
| 1 | Restore the retained dual-format application release: master merge commit `88945eb1f2238744ce768424f2eb9710b95e9ce5` (PR #239), Railway deployment `fd8d71b3-58cf-418c-8af2-4e28299d4820`. That release reads both v1 and v2. |
| 2 | Under that release, roll the family back atomically: `POST /admin/collection/publication-rebuilds/traditional_opponent/rollback`. |

Attempting step 2 first is refused, not half-completed. Under the strict
release the family rollback fails with `publication_format_unsupported` before
either pointer moves, because activating a pair the running code cannot read
would take the Opposing Team Profile, Team Filters, and the Matchups Defense
Sheet down rather than restore them.

The retained dual-format release must not be pruned while any v1 publication
is still a possible rollback destination. The v1 pair remains the
`previous_publication_id` of both pointers; this contraction modifies and
deletes nothing — neither the immutable v1 payloads nor their audit evidence.

| Code | HTTP | What it means |
| --- | --- | --- |
| `publication_format_unsupported` | 400 | The target pair is in a format this release does not read. Restore the retained dual-format release first. |
| `publication_family_coupled` | 409 | A per-stream operation would split the family. |
| `publication_family_authority_mismatch` | 400 | The target pair does not rest on one season, cutoff, and authority. |

## Driving a publication rebuild

Starting a rebuild records the approved intent and returns `202`; it does not
execute it, exactly as accepting an observation enqueues a composition job
without composing it. A worker pass drives the durable row through
`composing`, `validating`, and `promoting`:

```
python scripts/publication_rebuild.py --database-url … --family traditional_opponent
python scripts/publication_rebuild.py --database-url … --status <rebuild_id>
```

The pass is restart-safe. A worker that dies mid-phase leaves a row whose
lease expires; the next pass reclaims it and resumes from the phase the row
records, and every state write is fenced by the writer's claimed generation,
so a revived worker can never overwrite the successor that took over.

## Starting a new season

The published season is the season of the active `player_game_logs`
Publication; readers follow it (`research_season`). A new season reaches the
site only through one operator cutover, never by composition alone.

**Hold.** Composition for an enabled stream activates a candidate of season S
only when the stream has no active Publication yet, or S is the published
season (while no season is published, only the stream's own current season).
Any other season is stored as an inactive, *held* candidate: the pointer does
not move, the composition job succeeds (it is not retried), and every hold —
including an identical recomposition — writes an audit event
`publication.held` with the stream, season, published season, candidate,
cutoff, and `composition_job_id`, so each job's held outcome is traceable.
Only the newest governed cutoff of a season stays held: a newer candidate
supersedes earlier ones, and a delayed earlier-cutoff composition is itself
superseded rather than displacing the newer one. Same-season auto-advance is
unchanged, and the hold applies to `player_game_logs` itself.

**Collect.** Two scheduled jobs build a new season's held candidates:

- `player-shooting-refresh` (`python scripts/player_shooting_refresh.py <season>`,
  every 5 minutes) requests the day's Event and Athlete Catalogs and issues the
  day's manifest for 03:45-11:00 Central. It copies every scope except its own
  two player-shooting streams forward from the season's current manifest, so
  seed the season once with a manifest whose scopes include
  `canonical_game_ledger` and every collector stream (Synergy, shot types and
  shot zones, player and opponent). `canonical_game_ledger` is what lets the
  ledger collect: `LedgerRuntime.read_for_collection` refuses a manifest
  without it.
- `ledger-refresh` (settings in `railway.ledger.json`, `LEDGER_SEASON=<season>`,
  plus `DATABASE_URL`) runs `scripts/ledger_refresh.py --compose` at 10:15,
  12:15 and 14:15 UTC, inside that window. It accepts the completed games up to
  the manifest cutoff from pbpstats.com, with the NBA live-data CDN as fallback;
  Railway egress reaches both, so it needs no residential proxy. It then
  composes the ledger streams (`player_game_logs`, `player_per36`,
  `traditional_opponent_*`, `assist_locations_*`, `player_assist_locations`).
  For a season other than the published one these are held, as below. Season
  streams compose once all thirty teams have a completed game; L15 streams
  compose once every team has fifteen. A rerun in the same window fetches only
  games not yet stored; stored games are rechecked for stat corrections once a
  day while recent and once a week after that (`daily_recheck_days`,
  `weekly_recheck_days`). The same service then runs
  `scripts/rotation_refresh.py` (at most 60 games a run), which stores each
  completed regular-season game's starters and on-court stints for
  `GET /api/games/rotation` (crf04/statsplus#223); it runs whatever the ledger
  step's outcome, and the run fails if either step failed.

`statsplus-nightly-refresh` writes the legacy `player_game_logs` tables for the
calendar season. No reader uses them while the stream is enabled, and the
cutover does not read them.

**Verify.** Before the cutover, check the held candidates against NBA.com's
figures for the same games (read-only; no writes, pointer moves, or artifacts):

```
python scripts/season_verify.py --season 2026-27 [--database-url …] \
  [--proxy-url "$COLLECTOR_NBA_PROXY_URL"] [--output report.json]
```

For each ledger stream it takes the newest held candidate (the one the dry run
would move) and bounds NBA.com to the US-Eastern date of the latest game its
bound manifest and Event Catalog govern (never the payload's own games), Regular
Season: `player_game_logs` per player-game against PlayerGameLogs, `player_per36`
against LeagueDashPlayerStats `Per36`, and `traditional_opponent_*` team counts
against LeagueDashTeamStats `Opponent` totals (`LastNGames=15` for L15). It
reports rows compared, exact and within-tolerance matches, ledger-only and
NBA-only identities, governed games the payload lacks or that it holds without
governance (either fails the stream), and the ten largest mismatches with both values. Assist
streams have no NBA.com reference and are reported as `no_reference`. It exits 1
unless every reference stream matches every identity (`--threshold` lowers the
required match rate). NBA.com games outside the governed set are dropped from
the game-log reference; an aggregate whose NBA.com games-played exceeds the
governed count is reported `window_incomparable` (also exit 1) rather than as
stat mismatches, and a null NBA.com statistic aborts the run (exit 2). stats.nba.com blocks datacenter IPs: run it from a
residential machine or pass `--proxy-url`, which is never printed. Attach the
report to the cutover issue.

**Cutover.** Once the new season's collection is composing nightly:

1. Plan it, which writes nothing:

   ```
   POST /admin/collection/seasons/2026-27/cutover
   {"dry_run": true}
   ```

   The response lists every enabled stream with its current season,
   publication, and fence, the held candidate it would move to (or `null`),
   per-team completed-game counts for the season, and `refusal` — the code the
   real cutover would refuse with, or `null`.
2. Run it with the fences the plan reported:

   ```
   POST /admin/collection/seasons/2026-27/cutover
   {"reason": "open 2026-27", "expected_fences": {"player_game_logs": 41, ...}}
   ```

   It first proves every candidate it would move complete and bound: a
   verified manifest and Event Catalog identity shared with the
   `player_game_logs` candidate, a governed roster of all thirty teams, ledger
   lineage that is exactly the authority's governed games (and still the
   accepted evidence), and a payload whose decoder accepts it and covers
   exactly the governed games (for `player_game_logs`, rows for both teams of
   every governed game) — the checks composition and operator activation already
   apply. Then, in one transaction, it moves `player_game_logs` and every
   other enabled stream that has a held candidate of the season to its latest
   one, with
   pointer history and activation evidence for each move, under one audit
   event `season.cutover` whose `operator_job_id` is the cutover id. Stream rows
   and pointers are locked in key order, as composition locks them, so a
   concurrent composition either finishes first or waits for the cutover.
3. Confirm the moved streams read the new season and the unmoved ones read
   `window_not_ready` (below).

The parity gate (`require_completed_regular_season`) belongs to
`activate_stream` and is unchanged for 2025-26; the cutover does not use it.

| Code | HTTP | What it means |
| --- | --- | --- |
| `cutover_player_game_logs_candidate_required` | 400 | `player_game_logs` has no held candidate of the season. Nothing can move without it. |
| `publication_governance_unavailable` | 400 | A candidate has no verifiable manifest and Event Catalog binding, or its governance cannot be resolved (including an authority that does not govern all thirty teams). |
| `cutover_authority_mismatch` | 400 | A candidate rests on a different manifest or Event Catalog than the `player_game_logs` candidate, usually because it was composed on another night. Let the next composition re-hold it. |
| `publication_game_set_mismatch` | 400 | A candidate's lineage or payload games differ from the governed games (for example, composed before the ledger caught up). |
| `ledger_provenance_*` | 400 | A ledger candidate's evidence is no longer the accepted source. |
| `publication_checksum_mismatch` / `publication_candidate_invalid` | 400 | A candidate's bytes or decoded payload are not sound. |
| `stale_composition` | 409 | A moving pointer advanced since the dry run. Plan again. |

Every refusal moves nothing, and the admin route names it in
`error.details.reason`.

**After the cutover.** A stream with no candidate of the new season — typically
the `*_l15` windows until every team has 15 games — stays on its old
Publication, and its first new-season candidate activates on its own. Until
then readers report it `missing` with `unavailable_reason: "window_not_ready"`
instead of `publication_season_mismatch`. Where the nightly composition has
already stored a new-season Last-15 observation, the Matchup keeps reporting
that observation's `insufficient_governed_games`.

The `traditional_opponent` family is deliberately split during this window
(Season on the new season, L15 on the old). Family rollback still works: in a
split it rolls back only the window on the newest season, so the family
returns to its pre-cutover pair. Once the L15 window has joined, family
rollback moves both windows back one generation as usual, which returns L15 to
the old season and the family to the split. A Publication Rebuild needs both windows on
one season and refuses with `publication_family_authority_mismatch` until the
family reunites.

**Revert.**

```
POST /admin/collection/seasons/2026-27/cutover/<cutover_id>/revert
{"reason": "2026-27 opened too early"}
```

returns every stream the cutover moved to its exact pre-cutover state in one
transaction, under the audit event `season.cutover_revert`: the pointer names
the same active and previous publications as before the cutover (with a new
fence and a history row), the promoted candidates are superseded, and their
pointer-history rows are revoked. The reverted season therefore keeps no
retained authority — an explicit-season read of it is unavailable — and
retention stops keeping its payload. While a cutover is still reversible,
`prune_history` keeps every publication its revert would restore (kept reason
`cutover_restore`). Before writing anything the revert checks that each
restoration target still exists intact, and otherwise refuses with
`cutover_restore_unavailable` (409). It refuses with `cutover_superseded` (409) once any
stream that was enabled at the cutover has moved since — including the first
new-season L15 activation and the next nightly advance — so revert promptly or
recover per family instead. An unknown cutover id is `cutover_not_found` (404).

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

- drop or truncate `player_per36_stats`, `opp_shooting_zone`, or
  `pbp_opponent_stats`;
- remove the legacy writer code paths that are now fenced;
- enable any Playoff or Play-In behavior.

The six nightly ranking tables are the one completed exception, and they are
now gone. Under #199 `general_opponent_stats`, `catch_and_shoot`, `pullups`,
`less_than_10_ft`, `team_play_types`, and `processed_team_assists` are refused
at the publication boundary unconditionally, their collectors are deleted, and
migration `048_drop_legacy_ranking_tables` drops the storage — the activation
fence no longer decides their fate. The refresh refuses them with the reason
`retired_table`, ahead of and independently of any activation check, so a
revived collector never reaches the publisher. `opp_shooting_zone` is **not**
part of that drop: it is fenced, not retired.

One consequence for operators: `LegacyParityDiagnosticReader` no longer offers
a `traditional_opponent` read at all. With `general_opponent_stats` dropped
there is nothing to compare a candidate against, so the materialization records
an unavailable parity report for `traditional_opponent_season` — the same
treatment `traditional_opponent_l15` already had — rather than raising on a
missing table. `player_game_logs` and `player_per36` parity are unaffected.

Removal happens only under a separately approved cleanup issue, after the
activated streams have served a full cycle and rollback is no longer expected
to need the legacy read path.
