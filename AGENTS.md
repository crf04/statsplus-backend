# StatsPlus agent guide

## Start here

Run `./scripts/bootstrap.sh` once; it enforces the Python version pinned in
`runtime.txt`. Run `./scripts/check.sh` as the completion gate for every
change.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing runtime seams,
database access, authentication, caching, parsing, or providers. Read
[docs/API_DOCUMENTATION.md](docs/API_DOCUMENTATION.md) before changing a
public route, request, response, error, or authorization contract.

## Cross-repository coordination

Start every observable product outcome in the `crf04/statsplus` coordination
repository, including backend-only outcomes. Keep the focused backend
implementation issue, branch, commits, tests, and pull request here. Internal
maintenance without an observable product outcome may start in this repository.

Before implementing a linked product outcome, read its parent issue and the
coordination repository's agent guide, architecture map, and workflow. Agree on
the boundary contract there before changing a frontend-visible API,
authentication flow, or error contract.

## Change loop

1. Reproduce the requested behavior at the narrowest real seam.
2. Add or update a test that fails for the behavior being changed.
3. Make the smallest coherent implementation change.
4. Update the relevant documentation when configuration or public behavior
   changes.
5. Run `./scripts/check.sh`; completion means the entire command passes.

## Repository constraints

- Run commands from the repository root; database and prompt defaults are
  relative.
- Keep automated tests offline and credential-free by injecting or patching
  provider boundaries.
- Treat `nba_play_types.db` as a public, read-only demo fixture containing no
  real user records.
- Exercise data-replacement services with mocks or a temporary database.
- Treat authentication and data-update routes as security boundaries; verify
  their behavior against the authoritative architecture and API documents.

## Agent skills

### Issue tracker

Use this repository for backend implementation packets and internal maintenance.
See [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md) before creating,
picking up, linking, or closing an issue or pull request.

### Triage labels

Use the five mutually exclusive canonical triage labels. See
[docs/agents/triage-labels.md](docs/agents/triage-labels.md).
