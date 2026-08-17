# Canonical ledger correction operations

This runbook covers a corrected, accepted PBP observation after the original
game has entered the Canonical Game Ledger. Provider calls are not made by the
recomposition worker; it reads only the accepted ledger and the active
manifest/Event Catalog governance.

## Detect and enqueue

An accepted observation with a new typed or raw checksum atomically replaces
the complete game envelope. The replacement transaction also records the
source observation and enqueues one idempotent job per ledger-derived stream in
`composition_jobs`. Each job retains `trigger_game_id`, affected team IDs,
source observation IDs, the corrected ledger checksum, an exact game-set
checksum, and `recomposition_reason=correction`.

An identical checksum replay is a no-op: it does not replace facts, add
observation evidence, create a second job, or advance a publication pointer.

The lineage columns on `composition_jobs` describe only work still waiting to
be composed.  Once a stream succeeds, those pending fields are cleared; the
immutable `publication_observations` rows attached to the composed publication
retain the source lineage for audit and replay.  Scheduled reconciliation can
therefore distinguish a completed correction from a new pending source.

## Targeted rebuild

Run the normal worker from the backend root:

```sh
./scripts/ledger_refresh.py 2025-26 --database-url "$DATABASE_URL" --compose-only
```

The worker resolves the job's cutoff and manifest, validates the exact governed
game and team sets, and recomposes the affected Season and exact-L15 windows.
The matchup read model keeps facts for teams outside the correction target;
each rebuilt fact and surface observation records its exact selected game IDs,
game-set checksum, corrected ledger checksum, source observation lineage,
cutoff, and recomposition reason.

If a worker fails, the affected jobs become `failed` with a bounded retryable
error and an open `collection_reconciliation_items` row. The last complete
active Publication and pointer remain readable. Retry the job through the
existing composition retry operation, or let scheduled reconciliation enqueue
accepted evidence again. A failed job is never treated as a successful
publication.

## Diagnostics

Inspect these records together when investigating a correction:

- `canonical_game_ledger_games` and `canonical_game_ledger_raw_rows` for the
  current typed/raw checksums;
- `canonical_game_ledger_observation_evidence` and
  `collection_observations` for source lineage;
- `composition_jobs` for target teams, cutoff, attempts, and retry state;
- `collection_reconciliation_items` for failed recompositions;
- `publication_pointers`, `publication_versions`, and
  `publication_observations` for the active payload, fence, and evidence.

Use the persisted `game_ids`, `game_set_checksum`, and `ledger_checksum` on
`team_matchup_facts`/`team_matchup_surface_observations` to confirm whether an
exact L15 selection changed or only a selected aggregate changed.

## Rollback

Rollback is a publication-level operation and must be performed with an
operator reason and the current stream fence. It creates a rollback version,
copies the prior publication's provenance, and moves the active pointer
atomically. It does not delete the corrected ledger evidence. After rollback,
resolve the reconciliation item only after the corrected source and targeted
recomposition have been reviewed; a subsequent retry can safely advance the
pointer again because publication composition is checksum-idempotent.
