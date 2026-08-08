# StatsPlus agent guide

## Start here

Run `./scripts/bootstrap.sh` once, then use `./scripts/check.sh` as the completion gate for every change.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing route or service interfaces, database access, authentication, caching, natural-language parsing, or either external NBA provider. It records the non-obvious runtime seams and safe test surfaces.

## Change loop

1. Reproduce the requested behavior at the narrowest real seam.
2. Add or update a test that fails for the behavior being changed.
3. Make the smallest coherent implementation change.
4. Run `./scripts/check.sh`; completion means both Ruff and the full pytest suite pass.
5. Update the relevant README or `docs/` reference when configuration, routes, auth, or query behavior changes.

## Repository constraints

- Use Python 3.11. Run commands from the repository root because the default SQLite URL and prompt paths are relative.
- Keep automated tests offline and credential-free. Patch provider calls and create apps with `SKIP_FIREBASE_INIT` and `SKIP_TABLE_CREATE` where appropriate.
- Treat `nba_play_types.db` as a public demo fixture. Preserve it unless the task explicitly requires refreshing demo data, and never add real user records.
- `stats.nba.com` calls go through `nba_api`; `api.pbpstats.com` calls use the shared requests session. Keep their timeouts and health signals distinct.
- Routes under `/api/data` can replace local tables. Exercise their service methods with mocks or a temporary database during tests.
- Preserve the app-factory entry point (`app.create_app`) and the production WSGI contract (`wsgi:app`).

## Security contract

- Protected routes must fail closed when Firebase Admin is unavailable. The only credential-free bypass is the explicit `FIREBASE_ADMIN_DISABLED=true` setting in development/tests; it must be rejected outside a local/test environment and must never be enabled in deployments.
- Admin authorization accepts only verified Firebase custom claims `admin=true`, `role=admin`, or `roles` containing `admin`.
- Treat every `/api/data/*` endpoint and `PUT /api/players/fetch` as admin-only. Preserve their public methods: `POST /api/data/update_database`, `POST /api/data/fetch_players_with_teams`, `PUT` for both PBP routes, and `GET /api/data/fetch_playtypes`.
- Keep docs and curl examples aligned with these auth requirements and methods when changing routes.
