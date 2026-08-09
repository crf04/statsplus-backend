# Issue tracker: GitHub

`crf04/statsplus` owns observable product outcomes and their complete acceptance
criteria. `crf04/statsplus-backend` owns focused backend implementation packets
and internal maintenance without an observable product outcome.

Tracker publication requires explicit user authorization. Use the `gh` CLI and
always pass `--repo crf04/statsplus-backend` for operations in this repository.

## Conventions

- Create: `gh issue create --repo crf04/statsplus-backend --label needs-triage`
- Read: `gh issue view <number> --repo crf04/statsplus-backend --comments`
- List: `gh issue list --repo crf04/statsplus-backend --state open`
- Comment: `gh issue comment <number> --repo crf04/statsplus-backend --body "..."`
- Label: `gh issue edit <number> --repo crf04/statsplus-backend --add-label "..." --remove-label "..."`
- Close: `gh issue close <number> --repo crf04/statsplus-backend --comment "..."`

## Product implementation packets

A child packet links its parent with `Part of crf04/statsplus#<number>` and
contains the backend entry points, evidence, contract slice, done-when
conditions, and exact local completion gate. Read the parent before pickup.

Use `Blocked by: <owner/repo>#<number>` only when work cannot start. Express
merge ordering separately as `Merge after: <owner/repo>#<number>`.

A pull request closes only its child issue:

```text
Closes #<child issue number>
Part of crf04/statsplus#<parent issue number>
```

Leave the parent open for integrated verification. Never point `Closes`,
`Fixes`, or `Resolves` at the parent in a pull request or commit message.

## Pickup gate

Start work only when the packet names its owner and entry points, contract
slice, evidence, done-when conditions, and `./scripts/check.sh` completion gate;
has no unresolved start blocker; and its parent is neither `needs-info` nor
`wontfix`.
