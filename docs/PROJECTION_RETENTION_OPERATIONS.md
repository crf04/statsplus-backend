# Projection archive retention

This runbook covers the first production run of projection archive retention
(crf04/statsplus-backend#502): a dry run, the owner's approval, the real run,
and a one-time `VACUUM FULL`. The job keeps three days of full archive evidence
plus everything a reader needs at any age. What it keeps and why is
in [ARCHITECTURE.md](ARCHITECTURE.md#projection-archive-retention).

The nightly cron (`scripts/nightly_refresh.py --hosted-only`) runs the same job
last, but skips it until a manual real run of the current retention policy
version has succeeded. Every run's report records its `policy_version`; the
nightly reads the newest successful manual run's report, and a #502 report,
which has no such field, counts as policy 1. Deploying a new policy version
therefore deletes nothing on its own: it waits for the owner's approval of a
dry run and a manual real run, as below.

| Policy | Issue | What it deletes beyond the window |
|---|---|---|
| 1 | #502 | rows no reader needs; every started game keeps its closing-replay input |
| 2 | #554 | also a frozen game's replay input: it keeps only its closing memberships |

## Rolling out policy 2 (crf04/statsplus-backend#554)

The #554 deploy changes the Game Logs posted line to read a frozen provider
from its closing set. A process still on the old code replays the archive
instead, and once policy 2 prunes a frozen game's replay input that replay can
return a different line (an old process returned 27.5 before the prune and
26.5 after, while the new reader returns the frozen 26.5 throughout). So:

1. Deploy the change to every backend service (web, workers, crons) and
   confirm none still runs the old commit before going further. The nightly
   keeps skipping meanwhile, with `the newest successful manual run applied
   policy 1, not 2` in its log.
2. Run the dry run (section 2). Its report shows `policy_version: 2`.
3. The owner approves the real run on the issue, with the report attached.
4. Run `--execute` (section 3). From then on the nightly applies policy 2.

Sections 1 and 4 below describe the #502 first run; the migration and the
`VACUUM FULL` are not repeated.

Run every command from the repository root, in a shell whose `DATABASE_URL`
points at the target database. Never print the URL or the environment; the
command prints only its JSON report.

## 1. Deploy

Migration 075 runs before the new deployment starts. It creates
`projection_retention_runs`, then builds five
indexes `CONCURRENTLY` after its transaction commits, so collector writes are
never blocked. A concurrent build waits for transactions already open on its
table and reads the whole table, so on the 2.4 GB observations table it can
take minutes; the deploy waits for it. If the build is interrupted, the next
`scripts/migrate.py` run drops the invalid index and rebuilds it. Two
migrators at once are safe: the second waits for the first's build.

## 2. Dry run

```bash
python scripts/projection_retention.py --dry-run > retention-dry-run.json
```

It writes nothing, and on PostgreSQL reads one read-only snapshot, whose time
it prints as `snapshot_at`. Check the report:

- `outcome` is `succeeded`;
- `policy_version` is the version the owner is approving;
- `tables.<table>.<provider>` gives `before`, `delete` and `keep` for
  observations, generations, polls and snapshots; `prizepicks` shows
  `delete: 0`;
- `protected.<table>.<provider>` counts rows older than the window that are
  kept, by reason (`latest`, `closing_membership`, `active_snapshot`,
  `closing_replay`, `active`, `provider_health`, `window`);
- `evidence_bytes.<provider>` gives the snapshot documents' bytes `before`,
  to `delete` and to `keep`, and `kept_old` the bytes of snapshots older than
  the window kept, by reason. That is the permanent payload cost.

The owner approves the real run on the issue, with the report attached.

## 3. Real run

```bash
python scripts/projection_retention.py --execute > retention-run.json
```

It deletes in short batches under each provider scope's lock, so the
collector keeps running. Counts can exceed the
dry run's by what the collector wrote in between. Exit status 1 means
`outcome: failed`; `failure.reason` names the scope and the check, or the
error class. A failed batch rolled back; earlier batches stay committed and
were each checked. Fix the cause and rerun: the job is idempotent, and a second
run deletes nothing new.

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
- the closing-replay input (`closing_replay`) of started games that have no
  closing set for their provider, and of games yet to start. A game keeps it
  only until it freezes: a frozen game is read from its set and keeps only its
  memberships (crf04/statsplus-backend#554). A provider that wrote no Complete
  board since a game's last baseline keeps every Partial board since then for
  the games it has not frozen;
- every `prizepicks` row, until it writes Complete baselines.

Closing memberships are also the line history kept for retros, with the
sportsbook `closing_lines` table, which retention never touches. There is no
change-by-change line history.

The first real dry run measures them: `protected` counts the rows and
`evidence_bytes.<provider>.kept_old` the document bytes kept by reason. Budget
the volume from those numbers; clearing old payloads is a separate owner
decision.
