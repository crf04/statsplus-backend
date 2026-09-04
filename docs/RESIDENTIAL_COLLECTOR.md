# Residential NBA Collector

The collector is a one-shot, pull-only package. It runs from a Windows
Scheduled Task and never starts Flask, listens for inbound traffic, reads
Postgres credentials, or becomes a product composition authority.

## Configuration and credential boundary

Set `COLLECTOR_RAILWAY_URL`, `COLLECTOR_ENVIRONMENT`,
`COLLECTOR_IDENTITY_ID`, `COLLECTOR_OUTBOX_PATH`, `COLLECTOR_LOG_PATH`, and
`COLLECTOR_RELEASE_VERSION` in the task's protected environment. Production
endpoints must use HTTPS. The long-lived machine secret belongs in Windows
Credential Manager under `StatsPlus/Collector/<identity-id>`; it is read only
for the short-lived `/api/collector/token` exchange and is never written to
the environment file, SQLite, logs, or a release directory.

Validate without contacting Railway:

```powershell
./scripts/validate_collector_config.ps1 -RailwayUrl https://railway.example `
  -Environment production -IdentityId residential-pc-1 `
  -InstallRoot C:\StatsPlus\Collector -PythonExe C:\Python311\python.exe
```

The CLI has no database option by design:

```powershell
python -m statsplus_collector validate-config
python -m statsplus_collector status
python -m statsplus_collector release
python -m statsplus_collector run
```

For a credential-free rehearsal, set the secret only in the process environment
and use `COLLECTOR_ENVIRONMENT=historical_rehearsal` with a fake HTTPS-capable
control server. The fake server and provider fixtures are test seams, not
production defaults.

## One invocation and exit outcomes

An invocation acquires a SQLite process lease, checks the 30-day bound, drains
newest-cutoff outbox items, exchanges a short-lived scoped token, polls
discovery, validates each unexpired authorized Bootstrap Request/Manifest,
executes every ready NBA scope, spools each normalized response atomically, and
uploads only after the local durable write. An item is deleted only after a
Railway receipt whose checksum matches the outbox item.

Bootstrap discovery preserves catalog dependencies within a cutoff: the Event
Catalog is offered before the Athlete Catalog, because athlete publication is
validated against completed governed event evidence. The schedule collector
selects canonical Regular Season (`002`) game IDs from ScheduleLeagueV2's
mixed-phase response; optional display labels do not override that identity.

The process uses these stable outcomes:

| Code | Meaning | Scheduler action |
| ---: | --- | --- |
| `0` | `no_work` or `complete` | Finish the task; no unconditional repetition |
| `10` | `retry` — Railway/provider transient or pending outbox | Retry every 30 minutes, no longer than six hours |
| `11` | `busy` — another invocation owns the lease | Let the existing invocation finish; a later scheduled run catches up |
| `20` | `non_retryable` — auth, schema, identity, invariant, conflict, or hard-limit failure | Stop and alert an operator |

`synergy:l15` is represented as `provider_window_unsupported` and is never
attempted. Playoffs, Play-In, and cross-phase responses are rejected before
spooling. Provider timeouts/schema drift, wrong environment, revoked/expired
tokens, duplicate checksum conflicts, and disk-limit failures are stable safe
codes; player facts and provider response bodies do not enter diagnostics.

## SQLite Outbox and recovery

The outbox is a WAL-mode SQLite file with full synchronous writes. It stores
only compressed normalized envelopes plus bounded routing metadata. It orders
newer cutoffs first, rejects a hard-limit write rather than deleting current
work, measuring the allocated database plus WAL footprint with page overhead,
and refuses to silently discard unsent entries older than 30 days. Aged entries
are marked for operator attention and skipped while the newer-cutoff-first
drain continues; only a server-governed obsolete cutoff may remove an unsent
row. Use the governed cutoff with the repository's `prune_obsolete` recovery
operation, never filesystem deletion.
The collector invokes that operation only from Railway discovery's
`obsolete_before_cutoff`; local age alone can alert but never delete.
SQLite cannot atomically roll back filesystem allocation after commit. The
repository therefore treats `COLLECTOR_OUTBOX_MAX_BYTES` as a conservative
allocation budget above its fixed empty-schema baseline: `max_page_count`, a
reserved worst-case frame allowance, pre-write WAL truncation, and fail-closed
post-write verification keep every accepted write inside that budget. The
SHM mapping is included in footprint checks but contains no durable facts.
Restarting the process reopens the same file and replays pending items. A stale
process lease expires and can be recovered; a live lease prevents overlapping
instances. The product database remains Railway Postgres.

## Windows installation and operations

Run `install_collector.ps1` under a dedicated non-admin account. It registers
one task with:

- 4:00 AM Central daily trigger and a startup catch-up trigger;
- `StartWhenAvailable`, battery-safe behavior, and `MultipleInstances=IgnoreNew`;
- a limited (non-admin) principal and an eight-hour measured hung-process
  ceiling;
- rotating logs under the installation root;
- conditional retries only for exit `10`/`11`, every 30 minutes for at most six
  hours, implemented by `collector_task_wrapper.ps1`.

Install with `-WhatIf` first, then provision the secret in Credential Manager
and run a foreground historical rehearsal. `upgrade_collector.ps1` copies an
immutable staged release and preserves the previous version. After the
compatible Railway reader is deployed, perform one bounded foreground run and
run `promote_collector.ps1`, which checks configuration, protected credential
availability, release checksum, and the full compatibility rehearsal before it
enables the named task. Promotion also requires checksum-pinned result evidence
from an isolated HTTPS non-production Railway rehearsal covering credential,
authentication, discovery, status, and ingestion. `rollback_collector.ps1` disables the task and switches
only to the previous immutable release; verify its `python -m statsplus_collector release`
checksum before enabling the task. It never performs an unattended update.

Keep Windows Update active hours around the collection window, enable firmware
AC recovery to restore the prior power state, and keep WakeToRun enabled. A
private-network always-on device may provide manual Wake-on-LAN recovery; the
Railway service never opens an inbound connection to the PC.

## Opponent shot zones: Totals, not the provider's rate

The NBA shot-location endpoint offers a `Per48` per-mode, and for opponent
zones it is unusable. It divides each zone by a different hidden slice of
minutes, so the numbers it returns are not true per-48 rates and the
distortion differs per team -- which changes competitive ordering rather than
merely the display scale. Field-goal percentage looked correct throughout,
because makes and attempts share the same defective denominator, and that
concealed the defect for a long time.

The collector therefore requests `per_mode_detailed="Totals"` and the backend
derives every published value as:

```
published_per_48 = zone_total * 48 / team_minutes_for_the_identical_window
```

The minutes come from the opponent TeamStats row for that exact window, not
from the shot-location response's own arithmetic. The scope carries
`value_mode: "totals_with_minutes"`; an observation still labelled `per48`
cannot authorize a publication.

### Exact reconciliation

Each shot-location read is paired with an opponent TeamStats read using the
identical season, phase, team, date boundary, Last-N-Games and cutoff, so the
two responses describe one window. Two integer-count identities must then hold
exactly -- there is no tolerance band, because these are counts:

```
Restricted Area + In The Paint (Non-RA) + Mid-Range + Corner 3
    + Above the Break 3 + Backcourt == opponent TeamStats total   (FGM and FGA)

Corner 3 == Left Corner 3 + Right Corner 3                        (FGM and FGA)
```

Backcourt and the Corner 3 sides exist only to make those equations closeable.
They are retained on the observation as evidence and never become published
zones; publications continue to expose exactly the five canonical zones.

The second identity is also the clearest signature of the old defect: under
`Totals` the sides are exact components of the combined corner, while under
the endpoint's `Per48` the combined value sits near their mean.

### One paired refetch

The shot-location and TeamStats reads are two separate requests, so a mismatch
can simply mean the provider updated between them. The first mismatch refetches
the **complete pair** once. A coherent second result is accepted. A second
mismatch is rejected rather than stored as acceptable evidence, and is
recorded in the collector's bounded status as `value_invariant_failed` with a
`detail` naming the team, window, failing equation, expected, observed and
residual. That status is what the operator reads, and it is capped at 160
characters, so a diagnostic can never become a channel for provider payload.

### Validated at two boundaries

The collector refuses inconsistent evidence before it uploads. The backend then
repeats the identical check against the immutable recorded observation before
composing. A collector's success is a claim, not proof: an obsolete or
tampered-with collector cannot publish the broken rate scale, because central
validation re-derives the equations from the stored evidence rather than
trusting the upload.

## Provider compatibility probes

The default rehearsal runs deterministic sanitized recorded-shape schedule/roster, Synergy, grouped
shot-type, and exact-zone responses through the same normalizers used by the
live adapter. Opponent Season and exact-L15 calls must show explicit
`team_id`, Regular Season, date/last-15 parameters in the recorded request.
Every row must have canonical identity, finite non-negative values, makes no
greater than attempts, and the exact registered category/zone coverage. NBA
provider-authored taxonomy labels are retained; public Synergy L15 is not
probed as a supported surface.
Pass `--live` only for the separate explicit NBA endpoint compatibility check;
promotion uses the deterministic offline gate plus the isolated Railway evidence.
The Railway gate obtains a server-issued synthetic validation manifest, uploads
one sanitized gzip envelope, replays the identical client observation ID, and
requires the same durable receipt. Validation observations are persisted for
audit but never enqueue or advance a product Publication.

## Release identity

`python -m statsplus_collector release` emits a deterministic SHA-256 over the staged
files and their relative names. Railway should record that version/checksum.
Keep old release directories until the rehearsal and rollback drill pass.
