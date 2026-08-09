# Triage labels

| Canonical role    | Tracker label     | Meaning                                |
| ----------------- | ----------------- | -------------------------------------- |
| `needs-triage`    | `needs-triage`    | Maintainer needs to evaluate the issue |
| `needs-info`      | `needs-info`      | Waiting for more information           |
| `ready-for-agent` | `ready-for-agent` | Fully specified and ready for an agent |
| `ready-for-human` | `ready-for-human` | Requires human implementation          |
| `wontfix`         | `wontfix`         | Will not be actioned                    |

Keep these labels mutually exclusive. Swap the current triage label during
state transitions instead of stacking labels. Remove the triage label when
successfully closing an implementation issue; retain `wontfix` when closing an
issue as not planned.
