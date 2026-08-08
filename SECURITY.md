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
- Provide Firebase Admin credentials through the deployment secret manager.
- Leave `FIREBASE_ADMIN_DISABLED=false` or unset.
- Verify that a non-admin token receives `403` from admin-only routes and that
  missing or invalid tokens receive `401` from protected routes.
- Never paste Firebase tokens, service-account JSON, private keys, or user data
  into issue reports, logs, or documentation.
