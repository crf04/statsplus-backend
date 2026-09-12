# Issue 145 verification evidence

Backend-only work for crf04/statsplus-backend#145. Final revision `ecd5bf1c9fdd3c0aaf2b84af846e02a7ab7bdd5f`, base `e1db9337bf9fdfeac980a77841578e6e13440b75`; implementation `/Users/chrisfu/statsplus-issue-145`, branch `fix/145-game-log-filter-details`.

Implemented by the original OpenCode `opencode-go/glm-5.3-flash` session. Fresh Astra high Standards and Spec reviews used T3 instance `codex_work` (work account), via its configured Codex CLI and shadow home. `review-4-*-result.md` are the final review verdicts. Earlier reports document resolved findings and mutation coverage; they are not current failures.

## Checks

- Backend: `./scripts/check.sh` — 4,881 tests, 62 subtests, 84.91% coverage; lint, migrations and demo fixture validation passed. Full output in `gate-final.log`.
- Coordination: `STATSPLUS_BACKEND_ROOT=/Users/chrisfu/statsplus-issue-145 STATSPLUS_FRONTEND_ROOT=/Users/chrisfu/statsplus-145-frontend-qa python3 scripts/check.py` — 4 tests, 13 endpoint contracts passed. Coordination revision `54a4cb6fcb78b29d8ccc65c9859d54f50ee30ca5`. Frontend clean after transient launch activity; final fingerprint is unchanged.
- QA: `node .agents/skills/verify-statsplus/scripts/control-statsplus.mjs --backend /Users/chrisfu/statsplus-issue-145 --frontend /Users/chrisfu/statsplus-145-frontend-qa --out /tmp/statsplus-145/qa-final < /tmp/statsplus-145/qa-journey.jsonl`.
- Production: same helper with `--environment production --backend /Users/chrisfu/statsplus-backend --frontend /Users/chrisfu/statsplus-145-frontend-qa --out /tmp/statsplus-145/production-resumed`, using the same executed journey. Local backend is credential source only, not deployed revision evidence.

[Final QA verdict](qa-final/verdict.md) and [production compatibility verdict](production-resumed/verdict.md) are separate. Both use actual authenticated frontend transport, with the unchanged frontend `96d6b965b81c25c45d5872db7e76d0e0f4bb8a09`. Unsupported-opponent links return 400 and withhold logs; valid last-five links return populated game logs and survive reload. Snapshot `3fe69266e135d9ed9e6f671d01e7034c7d529a6ee3a9fa6395851b003ee222f5`. Command hashes verified, screenshots inspected, owned QA services cleaned up.

## Limits

The frontend currently displays the unchanged generic message; structured details/discovery/redaction are proved through offline HTTP tests and mutations. No frontend code changed. Browser checks are desktop-only; this is a backend PR. The deployed Railway backend SHA is unknown, and production compatibility does not prove undeployed changes. QA disables Redis, paid AI fallback, DFS and injury-provider integrations; Google sign-in popup is not exercised.

Only sanitized browser evidence, source diffs, test output using synthetic credential markers, and review reports are included. Authentication storage, request headers, credentials, runtime service logs and raw model session exports are excluded. Videos remain local; saved JSONL, hashes and session results provide action/outcome evidence. Original backend issue is the authoritative spec; no new tracker hierarchy was published.
