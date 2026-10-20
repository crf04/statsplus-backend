# Projection archive retention

This runbook covers the first production run of projection archive retention
(crf04/statsplus-backend#502): a dry run, the owner's approval, the real run,
and a one-time `VACUUM FULL`. The job keeps three days of full archive evidence
plus everything a reader needs at any age, and writes a change-only
`projection_line_history` before it deletes anything. What it keeps and why is
in [ARCHITECTURE.md](ARCHITECTURE.md#projection-archive-retention).

The nightly cron (`scripts/nightly_refresh.py --hosted-only`) runs the same job
last, but skips it until one manual real run has succeeded. Deploying this
change therefore deletes nothing on its own.

Run every command from the repository root, in a shell whose `DATABASE_URL`
points at the target database. Never print the URL or the environment; the
command prints only its JSON report.

## 1. Deploy

Migration 074 runs before the new deployment starts. It creates
`projection_line_history` and `projection_retention_runs`, then builds five
indexes `CONCURRENTLY` after its transaction commits, so collector writes are
never blocked. A concurrent build waits for transactions already open on its
table and reads the whole table, so on the 2.4 GB observations table it can
take minutes; the deploy waits for it. If the build is interrupted, the next
`scripts/migrate.py` run drops the invalid index and rebuilds it.

## 2. Dry run

```bash
python scripts/projection_retention.py --dry-run > retention-dry-run.json
```

It writes nothing. Check the report:

- `outcome` is `succeeded`;
- `tables.<table>.<provider>` gives `before`, `delete` and `keep` for
  observations, generations, polls and snapshots; `prizepicks` shows
  `delete: 0`;
- `protected.<table>.<provider>` counts rows older than the window that are
  kept, by reason (`latest`, `closing_membership`, `active_snapshot`,
  `closing_replay`, `active`, `provider_health`, `referenced`);
- `history.<provider>` gives the history rows the real run would write
  (`rows`, of which `withdrawals` are lines a Complete board dropped) and
  remove (`removed`, rows a late observation makes repeats); `unkeyed` counts
  rows without a canonical player or statistic, which get no history. The
  first run derives history from every row of every age;
- `evidence_bytes.<provider>` gives the snapshot documents' bytes `before`,
  to `delete` and to `keep`, and `kept_old` the bytes of snapshots older than
  the window kept, by reason. That is the permanent payload cost.

The owner approves the real run on the issue, with the report attached.

## 3. Real run

```bash
python scripts/projection_retention.py --execute > retention-run.json
```

It writes all history first, then deletes in short batches under each
provider scope's lock, so the collector keeps running. Counts can exceed the
dry run's by what the collector wrote in between. Exit status 1 means
`outcome: failed`; `failure.reason` names the scope and the check, or the
error class. A failed batch rolled back; earlier batches stay committed and
were each checked. Fix the cause and rerun: the job is idempotent, and a second
run deletes nothing new and writes no repeated history.

Confirm:

```sql
SELECT run_id, trigger, outcome, started_at, finished_at
FROM projection_retention_runs ORDER BY run_id;
```

A second `--dry-run` straight after should report `delete` near zero.

## 4. One-time VACUUM FULL

Deleted rows free space inside the tables but do not shrink the volume.
Reclaim it once, at a quiet hour (about 07:00 to 09:00 UTC, before the 10:00
nightly run and away from games). `VACUUM FULL` takes an exclusive lock on each
table for its whole duration: Slate, Matchup and the collector wait on it. It
writes a compacted copy first, so the volume needs free space about the size of
what the table keeps.

```sql
SELECT relname, pg_size_pretty(pg_total_relation_size(oid))
FROM pg_class
WHERE relname IN ('projection_observations', 'projection_provider_snapshots');

VACUUM (FULL, ANALYZE) projection_observations;
VACUUM (FULL, ANALYZE) projection_provider_snapshots;
```

Run the size query again afterwards and record both sizes on the issue.
Ordinary autovacuum reuses the space each nightly run frees, so `VACUUM FULL`
is not repeated.

## What keeps growing

Retention bounds the rows nothing reads, not the archive. These accumulate
for good:

- the snapshots closing sets reference, with their `evidence_document`, and
  the observations and generations behind them (`closing_membership`);
- every started game's closing-replay input (`closing_replay`), mostly the
  same rows;
- every `prizepicks` row, until it writes Complete baselines;
- `projection_line_history`.

The first real dry run measures them: `protected` counts the rows and
`evidence_bytes.<provider>.kept_old` the document bytes kept by reason. Budget
the volume from those numbers; clearing old payloads is a separate owner
decision.
