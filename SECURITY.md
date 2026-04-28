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
