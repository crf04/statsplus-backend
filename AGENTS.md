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

Keep backend-only work in this repository. When an outcome changes a
frontend-visible API, authentication flow, or error contract, work through the
`crf04/statsplus` coordination repository first. Read its agent guide,
architecture map, and workflow; agree on the boundary contract before
implementation, then keep the backend branch, issue, commits, tests, and pull
request here.

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
