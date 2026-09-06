# Atomic publication repair groups

A publication stream normally advances on its own: evidence arrives, a
composition job runs, and the stream's pointer moves from the displaced
version to the replacement. The displaced version stays reachable as that
stream's rollback target.

Some repairs cannot work that way. When two streams published the *same*
defect — for example the 2025-26 opponent shot-zone Season and Last-15
publications, both rendered from NBA's broken Per48 mode — advancing one
before the other leaves the product reading a mixed pair, and the rollback
target the repair displaces is itself the broken publication nobody wants to
return to.

An **atomic repair group** is the operator's declaration that a set of
publications must move together and that their existing rollback targets are
to be discarded. This document describes declaring and inspecting a group.

## Declaring a group

A group is declared as part of creating the collection manifest, before any
residential evidence for that cutoff is collected:

```python
control.create_manifest(
    "2025-26",
    cutoff=cutoff,
    scopes=[
        "exact_shot_zones_opponent_season",
        "exact_shot_zones_opponent_l15",
        "canonical_game_ledger",
    ],
    collect_before=collect_before,
    repair_group={
        "reason": "Opponent zone Totals were published from the broken Per48 mode.",
        "members": [
            {
                "stream_key": "exact_shot_zones_opponent_season",
                "expected_publication_id": "<currently active publication>",
                "expected_fence": 7,
            },
            {
                "stream_key": "exact_shot_zones_opponent_l15",
                "expected_publication_id": "<currently active publication>",
                "expected_fence": 4,
            },
        ],
    },
)
```

The declaration is immutable. There is no edit path: a group that turns out
to be wrong is replaced by superseding the manifest and declaring a new one.

### Integrity

The canonical declaration is folded into the manifest checksum. The same
season, cutoff, scopes, and catalog binding produce a *different* manifest
depending on whether a group is declared and what it contains, so a group can
never be attached to, detached from, or swapped inside an already-published
manifest without changing its identity.

The declaration is persisted as `publication_repair_groups` (one row per
manifest) and `publication_repair_group_members` (one row per stream). Members
are normalized rows rather than a JSON blob so a later read never has to
search rendered payloads for a publication identifier, and so the database can
refuse to delete a publication a live group still guards.

### Rejected declarations

These are rejected before the manifest becomes active, so a manifest never
goes live around a group that could not be executed:

| Reason | Rejected because |
| --- | --- |
| `invalid_repair_group` | The declaration is not a `{"reason", "members"}` mapping. |
| `repair_group_reason_required` | The operator reason is empty or over 255 characters. |
| `invalid_repair_group_membership` | Fewer than 2 or more than 8 members. One stream is not a group. |
| `invalid_repair_group_member` | A member is missing fields, or its expected fence is not a non-negative integer. |
| `duplicate_repair_group_member` | A stream key appears twice. |
| `unknown_repair_group_member` | The stream is not a schedulable NBA publication stream, is not registered, or the named publication does not exist or belongs to another stream. |
| `ineligible_repair_group_member` | The manifest does not freeze that stream in its scopes, so it collects no evidence to repair it with. |
| `cross_season_repair_group_member` | A named publication belongs to a different season than the manifest. |
| `cross_cutoff_repair_group_member` | The named publications do not share one cutoff, or that cutoff is later than the manifest's. The displaced set must be a single coherent snapshot. |

## Reading a group

### Operator

`CollectionControlService.repair_group_state(manifest_id)` returns `None` for
an ordinary manifest, and otherwise the full declaration beside live pointer
state:

```json
{
  "group_id": "…",
  "manifest_id": "…",
  "season": "2025-26",
  "cutoff": "2026-08-11T00:00:00+00:00",
  "reason": "Opponent zone Totals were published from the broken Per48 mode.",
  "checksum": "…",
  "members": [
    {
      "stream_key": "exact_shot_zones_opponent_l15",
      "expected_publication_id": "…",
      "expected_fence": 4,
      "active_publication_id": "…",
      "fence": 4,
      "guard_satisfied": true
    }
  ],
  "stale_members": [],
  "promotable": true,
  "state": "waiting_for_grouped_execution"
}
```

`promotable` reports only that every declared guard still matches the live
pointer. If any member's active publication or fence has moved since the
declaration, `state` becomes `guard_stale`, `promotable` becomes `false`, and
the group cannot enter a promotable state: the rollback target the operator
agreed to discard is no longer the one that would actually be discarded.
Evidence completeness is a separate, later gate re-checked by the grouped
promotion itself.

### Collector

Authorized collector reads — `GET /api/collector/discovery` and
`GET /api/collector/manifest/<manifest_id>` — carry an additive `repair_group`
object, or `null`:

```json
{
  "group_id": "…",
  "reason": "Opponent zone Totals were published from the broken Per48 mode.",
  "checksum": "…",
  "members": ["exact_shot_zones_opponent_l15", "exact_shot_zones_opponent_season"],
  "execution": "grouped"
}
```

This deliberately does **not** widen collector permissions:

- `members` is filtered to the surfaces the caller's owner/provider/surface
  binding already authorizes. A collector authorized for only one member sees
  only that member, and the group is omitted entirely if it authorizes none.
- Expected publication identities and pointer fences are never included. They
  are operator control-plane state and no collector scope grants them.

Both routes render the same object from the same code path, so a collector
that polls discovery and then reads one manifest sees a consistent group.

## Waiting for grouped execution

A declared member's composition job is created and queued exactly as usual.
It is then held rather than promoted:

- The composition worker skips jobs whose stream belongs to a declared group
  for that manifest. It does not claim them, so they stay `queued` rather than
  stranding in `running`, and their `claimed_generation` stays `NULL`.
- `PublicationService.compose_from_observations` refuses a grouped member with
  `grouped_repair_pending` (HTTP `409 operation_conflict`). The refusal lives
  at the choke point, not only in the worker, so no caller of the independent
  path can advance half a group.

Manifests without a declared group, and unrelated composition jobs on the same
cutoff, keep their existing independent behavior.
