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
- Read relationships: `gh api repos/crf04/statsplus-backend/issues/<number> --jq '{parent: .parent_issue_url, children: .sub_issues_summary, deps: .issue_dependencies_summary}'`
- List open blockers: `gh api repos/crf04/statsplus-backend/issues/<number>/dependencies/blocked_by --jq '.[] | select(.state=="open") | "\(.repository.full_name)#\(.number) \(.title)"'`
- Comment: `gh issue comment <number> --repo crf04/statsplus-backend --body "..."`
- Label: `gh issue edit <number> --repo crf04/statsplus-backend --add-label "..." --remove-label "..."`
- Close: `gh issue close <number> --repo crf04/statsplus-backend --comment "..."`

## Product implementation packets

A child packet is attached to its parent as a native GitHub sub-issue, and
contains the backend entry points, evidence, contract slice, done-when
conditions, and exact local completion gate. Read the parent before pickup.

Start blockers are native GitHub issue dependencies, recorded only when work
cannot start. Both relationships work across repositories:

```bash
PARENT=$(gh issue view <number> --repo crf04/statsplus --json id -q .id)
gh api graphql -f query='mutation($p:ID!,$u:String!){
  addSubIssue(input:{issueId:$p, subIssueUrl:$u}){ clientMutationId }
}' -F p="$PARENT" -F u=https://github.com/crf04/statsplus-backend/issues/<number>
```

Never also write the relationship into the issue body. A skill or template
asking for a `## Parent`, `Part of ...`, or `## Blocked by` section is
superseded here: omit it. A text copy of a native edge drifts, and only the
native form can be queried. `crf04/statsplus` holds the reasoning in
`docs/adr/0001-native-issue-relationships.md`.

Merge ordering has no native equivalent and stays as text:
`Merge after: <owner/repo>#<number>`. It is not a start blocker.

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

Check the blocker condition with the `Read relationships` command above:
`issue_dependencies_summary.blocked_by` counts only blockers that are still
open, so `0` means every declared blocker has closed. Do not infer it from the
issue body.
