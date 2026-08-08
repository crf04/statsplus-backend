# Security Policy

## Responsible Disclosure

Please report suspected vulnerabilities privately before opening a public issue.
Send a concise report with reproduction steps, affected endpoints or files, and
any logs that do not contain secrets to the repository owner or maintainer.

Do not include live API keys, Firebase tokens, service-account JSON, private keys,
or user data in reports. If a proof of concept needs credentials, use throwaway
test credentials and revoke them after disclosure.

## Secret Handling

Do not commit service-account JSON files, `.env` files, database dumps with user
data, API keys, Firebase private keys, tokens, or local cloud credential files.
Runtime credentials should be supplied only through environment variables or the
deployment provider's secret store.

Use `.env.example` for variable names and safe defaults only. Keep real local
values in `.env` or another gitignored file.

Every pull request and push to `main` or `master` runs Gitleaks against the full
repository history. A finding fails the `Security / Scan repository history for
secrets` check; inspect the workflow log for the file and rule. Treat a matched
credential as compromised even if it was removed in a later commit: revoke or
rotate it first, then remove it from history when appropriate. Use a narrowly
scoped `.gitleaks.toml` allowlist only for a confirmed false positive, and note
the reason in the pull request.

## Dependency maintenance

Dependabot checks the root Python dependency inputs and GitHub Actions weekly.
Its grouped pull requests must update both the direct requirement pins and the
generated `requirements-lock.txt`; regenerate the lock with the command in
`CONTRIBUTING.md`, review release notes for breaking changes, and run
`./scripts/check.sh` before merging.

The `Security` workflow audits the hashed lock file with `pip-audit` on every
pull request, push to the default branches, and weekly schedule. A known
vulnerability fails the `Security / Audit locked Python dependencies` check and
lists its advisory and fixed versions in the log. Prefer upgrading to a fixed
version. If no fix exists and the vulnerable code is demonstrably unreachable,
document the risk and mitigation before adding a specific advisory ignore; do
not disable the audit or use a blanket ignore.

GitHub Actions are pinned to immutable commit SHAs with release comments.
Dependabot keeps those pins current. When updating an action manually, verify
the commit against the upstream release tag and preserve the version comment.

## Authentication and authorization

Firebase Admin is the trust boundary for protected routes. If Firebase Admin
cannot initialize, protected routes fail closed and return `503`; missing or
invalid tokens return `401`. They do not fall back to an anonymous or synthetic
user by default.

For credential-free local development, `FIREBASE_ADMIN_DISABLED=true` enables an
explicit synthetic `dev-user` bypass. This setting is local-only: it is rejected
unless `FLASK_ENV=development` (or the app is running tests), and it must never
be enabled in a deployed environment.

Admin endpoints require a verified Firebase ID token with at least one of these
custom claims:

- `admin=true`
- `role=admin`
- `roles` containing `admin`

The following mutation or administrative routes are admin-only:

- `POST /api/data/update_database`
- `POST /api/data/fetch_players_with_teams`
- `PUT /api/data/player_PBP`
- `PUT /api/data/opponent_PBP`
- `GET /api/data/fetch_playtypes`
- `PUT /api/players/fetch`
- `GET /api/user/admin/stats`

Treat all data refresh routes as potentially destructive: they call external
providers and may replace local tables. Do not expose them without Firebase
credentials and claim-based authorization.

## Deployment checklist

- Set `FLASK_ENV=production` in deployed environments.
- Set `CORS_ALLOWED_ORIGINS` to the exact deployed frontend origin allowlist;
  production rejects the local default and wildcard origins.
- Provide Firebase Admin credentials through the deployment secret manager.
- Leave `FIREBASE_ADMIN_DISABLED=false` or unset.
- Verify that a non-admin token receives `403` from admin-only routes and that
  missing or invalid tokens receive `401` from protected routes.
- Never paste Firebase tokens, service-account JSON, private keys, or user data
  into issue reports, logs, or documentation.
