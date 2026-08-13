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
python -m app.collector validate-config
python -m app.collector status
python -m app.collector release
python -m app.collector run
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
work, and refuses to silently discard unsent entries older than 30 days.
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
explicitly enable the task. `rollback_collector.ps1` switches only to the
previous immutable release; verify its `python -m app.collector release`
checksum before enabling the task. It never performs an unattended update.

Keep Windows Update active hours around the collection window, enable firmware
AC recovery to restore the prior power state, and keep WakeToRun enabled. A
private-network always-on device may provide manual Wake-on-LAN recovery; the
Railway service never opens an inbound connection to the PC.

## Provider compatibility probes

The rehearsal must run sanitized recorded schedule/roster, Synergy, grouped
shot-type, and exact-zone responses through the same normalizers used by the
live adapter. Opponent Season and exact-L15 calls must show explicit
`team_id`, Regular Season, date/last-15 parameters in the recorded request.
Every row must have canonical identity, finite non-negative values, makes no
greater than attempts, and the exact registered category/zone coverage. NBA
provider-authored taxonomy labels are retained; public Synergy L15 is not
probed as a supported surface.

## Release identity

`python -m app.collector release` emits a deterministic SHA-256 over the staged
files and their relative names. Railway should record that version/checksum.
Keep old release directories until the rehearsal and rollback drill pass.
