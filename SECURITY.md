# Security Notes

Do not commit service-account JSON files, `.env` files, database dumps with user data, or API keys.

Before making this repository public:

1. Revoke and rotate any Firebase service-account key that was previously committed.
2. Publish from a clean branch or rewritten history that no longer contains the committed credential JSON.
3. Run a secret scan against the final public branch.

Runtime credentials should be supplied only through environment variables or your deployment provider's secret store.
