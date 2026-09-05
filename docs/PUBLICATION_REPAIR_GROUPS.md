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

`GET /api/admin/collection/manifests/<manifest_id>/repair-group`, backed by
`CollectionControlService.repair_group_state(manifest_id)`, returns the full
declaration beside live pointer state. An ordinary manifest has no group, and
the route answers `404` with detail `repair_group_not_found`:

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
  "promoted_at": null,
  "promotable": true,
  "state": "waiting_for_grouped_execution"
}
```

`state` is one of exactly three values:

| `state` | `promotable` | Meaning |
| --- | --- | --- |
| `waiting_for_grouped_execution` | `true` | Every declared guard still matches the live pointer. |
| `guard_stale` | `false` | At least one member's active publication or fence moved after the declaration; `stale_members` names them. |
| `promoted` | `false` | The declaration was consumed by a successful promotion; `promoted_at` is set. |

`promotable` reports only that the group has not been promoted and that every
declared guard still matches the live pointer. A stale guard means the
rollback target the operator agreed to discard is no longer the one that would
actually be discarded. Evidence completeness is a separate, later gate
re-checked by the grouped promotion itself.

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

`execution` is `"grouped"` while the declaration is still waiting, and
`"promoted"` once a successful promotion has consumed it -- at which point the
members publish independently again. Those are the only two values.

This deliberately does **not** widen collector permissions:

- `members` is filtered to the surfaces the caller's owner/provider/surface
  binding already authorizes. A collector authorized for only one member sees
  only that member, and `repair_group` is `null` if it authorizes none.
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
  at the choke point and not only in the worker, so the scheduled path cannot
  advance half a group.

This covers the scheduled composition path, which is the one that would
otherwise promote a member on its own. It is not a lock on the pointer. An
explicit operator action that advances a stream by another route -- notably
`POST /api/admin/collection/streams/<stream_key>/activate`, which promotes a
candidate directly -- still moves the pointer. That does not half-repair the
group silently: the member's guard no longer matches its declaration, so
`repair_group_state` reports `guard_stale` and the promotion refuses with
`repair_group_guard_stale` rather than discarding a rollback target the
operator never agreed to discard.

Manifests without a declared group, and unrelated composition jobs on the same
cutoff, keep their existing independent behavior.

## Promoting the group

Once every member has complete evidence for the manifest's season and cutoff,
an operator promotes the whole group as one change:

```http
POST /api/admin/collection/manifests/<manifest_id>/repair-group/promote
{"reason": "replace the broken opponent zone pair"}
```

```json
{
  "job_id": "…",
  "repair_group_id": "…",
  "manifest_id": "…",
  "discarded_publications": [
    {"stream_key": "exact_shot_zones_opponent_season",
     "publication_id": "…", "fence": 7, "cutoff": "2026-08-10T00:00:00+00:00"}
  ],
  "published_publications": [
    {"stream_key": "exact_shot_zones_opponent_season",
     "publication_id": "…", "fence": 8}
  ]
}
```

The whole operation is one database transaction, in two phases:

1. **Prove.** Every member's pointer is locked (`SELECT … FOR UPDATE`, in
   stream-key order so concurrent operators queue rather than deadlock) and
   its declared active publication identity *and* fence are rechecked against
   the live row. Then every replacement is composed from accepted evidence and
   validated. Nothing has been written yet.
2. **Publish.** Only after all members pass does any pointer move. Each member
   advances its fence to its new active publication, the displaced version is
   marked `superseded`, and the pointer's `previous_publication_id` is set to
   `NULL`.

The held composition jobs are marked `succeeded` and the group records
`promoted_at`, which releases its members: later cutoffs publish
independently again, and the group cannot be promoted twice
(`repair_group_already_promoted`).

### Why the rollback target is cleared

An ordinary publication leaves the version it displaced as the stream's
rollback target. A repair must not: the displaced version *is* the defect, and
rolling back to it would silently restore the bug the repair just removed.

So after a successful repair, `POST /api/admin/collection/streams/<stream_key>/rollback`
returns `409 operation_conflict` with detail `rollback_unavailable`. That is
the intended state, not a failure. Rollback becomes available again for a
stream as soon as a later ordinary publication establishes a previous version
that is actually trustworthy.

### Exceptional behavior

Every one of these leaves the complete prior publication state untouched --
active pointers, previous pointers, version status, the composition jobs, the
group's `promoted_at`, and the operator audit all roll back together:

| Reason | Meaning |
| --- | --- |
| `repair_group_not_found` | The manifest declares no group (`404`). |
| `repair_group_already_promoted` | The declaration was already consumed (`409`). |
| `repair_group_guard_stale` | A member's active publication or fence moved after the declaration (`409`). |
| `repair_group_manifest_inactive` | The declaring manifest was superseded by a later one, so its catalog binding no longer governs (`409`). Passing `collect_before` is *not* this state -- a promotion happens after collection closes, so an active manifest past its collection deadline still promotes. |
| `incomplete_publication`, `base_incomplete` | A member has no, or partial, evidence for this season and cutoff. |
| `publication_candidate_invalid` | A member's replacement failed validation. |
| any composition or infrastructure failure | Including a failure between two members' pointer updates. |

A failed grouped repair is confined to its own transaction: unrelated
publication work on the same cutoff still completes. The declaration survives
a failure, so the operator can fix the cause and retry the same promotion.

### The audit

One `AuditEvent` records the whole repair, with
`action = publication.repair_group.promote`, the operator's reason, and
details naming the group, its declared reason, every discarded publication
identity and fence, and every publication that replaced them. It commits with
the pointer changes or not at all.

## Running the 2025-26 opponent-zone repair

The repair this machinery was built for. Order matters: the known-wrong data
stays live until the last step, so keep the window short.

1. **Deploy the backend** only when an operator is ready to follow through.
   Deploying alone changes nothing a user sees.
2. **Record the current identities.** For both
   `exact_shot_zones_opponent_season` and `exact_shot_zones_opponent_l15`,
   note the active publication id and pointer fence. These become the group's
   guards; if either moves before promotion the repair refuses rather than
   discarding something the operator did not agree to discard.
3. **Create the manifest with the group**, naming both streams, both
   identities and fences, and a reason. The declaration is immutable.
4. **Collect both windows** from the residential collector. Evidence arrives
   independently; neither member composes on its own.
5. **Promote.**
   `POST /api/admin/collection/manifests/<id>/repair-group/promote`. Both
   replacements validate and both pointers advance in one transaction, or
   nothing changes.
6. **Verify the data.** Boston's Restricted Area opponent FGA should read
   about 18.3 per 48, not 137.1. Check **all 30 teams in both windows** -- a
   plausible sample can conceal a league-wide ranking defect, because the
   Per48 distortion was team-specific.
7. **Verify the product.** Matchups and Team Profile keep their existing
   payload shape, so a corrected value, a corrected rank, and a corrected
   league-relative comparison are what should change. No frontend deployment
   is involved.

After step 5 both streams report `rollback_unavailable` until a later valid
publication establishes a trustworthy previous version. That is the intended
outcome: the versions the repair displaced are the defective ones, and routine
recovery must not be able to restore them.
