# Issue 145 — independent Standards re-review 4

## Main report (under 400 words)

**No remaining material Standards findings.** Both prior findings are resolved. No material Fowler-baseline smell was established.

The redaction decision at `app/routes/game_routes.py:197` now compares unbounded redaction with the raw value. HTTP requests containing 250 ordinary zeroes followed by `,bad` retain `bad` for both minutes and self filters. Context at `app/routes/game_routes.py:228` belongs to the rejected entry, preventing another repeated same-stat input from replacing its ordinary value. Actual empty brackets survive malformed and empty ranges at `app/models/game_logs.py:315`.

All **three newly added tests** independently fail under their corresponding production mutation and pass immediately after restoration. The formerly surviving malformed-range mutation now fails the added test at `tests/test_errors.py:536`. Supplementary HTTP probes demonstrate that the same mutant loses brackets for both `a` and the empty string. This resolves the regression-test requirement in `AGENTS.md:29` and the actual-stat/unusable-value contract in `docs/API_DOCUMENTATION.md:1279–1283`.

**Verification:** 140 affected HTTP/model tests passed initially; the restored run passed **153 tests and 37 subtests**, including self-filter compatibility. All **30 HTTP probes passed**, including 20 quoted-credential cases. Three distinct mutants were killed; none survived the corresponding new test. No unchanged prior-test mutations were replayed.

All 496 tracked-file hashes and the complete diff match the initial snapshot. No implementation fixes, bootstrap, full gate, whole-directory test run, publishing, or subagents. The parent owns the completion gate.

## Pinned scope and standards

- Worktree: `/tmp/statsplus-145/review-4-standards` (physical path `/private/tmp/statsplus-145/review-4-standards`). Only the explicitly requested report/evidence exports were written outside it.
- HEAD/base: `e1db9337bf9fdfeac980a77841578e6e13440b75`. Candidate consists of six uncommitted modified files; there are no candidate commits beyond HEAD. Exact full diff command: `git diff HEAD --binary`; a three-dot commit comparison would omit this working-tree candidate.
- Full diff SHA-256: `cbdcad9257791373a8dbe3ff2c453635b14e4f74c2420ac9438a03b62575c376`.
- Python: existing `.venv`, Python 3.11.9, matching `runtime.txt`. No bootstrap/install performed.
- Sources read: owning `AGENTS.md`; supplied global instructions; complete `CONTRIBUTING.md`; architecture runtime/error ownership, game-log request flow, test seams and known synchronous model seam; API general errors and game-log contract; supplied unedited authoritative issue, full diff and recent diff; prior mutation report and evidence; local code-review skill and its supplied smell baseline.
- Standards citations: `AGENTS.md:28–33` requires reproduction, failing regression tests, minimal changes and documentation; `AGENTS.md:38–44` requires offline tests, protected demo data and authentication boundaries; `CONTRIBUTING.md` requires mocked integrations and same-change documentation; `docs/ARCHITECTURE.md:9–24` assigns HTTP shaping to adapters and prohibits publishing internal `detail`; `docs/ARCHITECTURE.md:4374–4399` describes injected test boundaries; `docs/API_DOCUMENTATION.md:1247–1295` defines the generic-plus-actionable-details contract.
- Reviewed full scope: centralized handler and optional details, typed validators and unchanged accepted grammar, canonical team vocabulary/aliases, separate playstyle parameter attribution, self-filter typed/NLP compatibility, unknown-error fallback, structured error envelope, scalar redaction/bounding, credential context, docs and extended existing tests. No auth/provider/database/runtime assembly changes were introduced. Read-only fixture bytes were included in preservation checks.
- The issue explicitly authorizes publishing sanitized submitted values. The pre-existing general “closed-vocabulary” description is therefore not treated as a reason to prohibit this requested behavior. No requirement for frontend work or a new vocabulary endpoint was invented. Tool-enforced style issues were excluded.

## Offline execution and restoration

Every pytest process ran from the pinned repository root through the saved `runner.py`, which strips credential-related environment variables, disables dotenv and plugin autoloading, and blocks socket connections, DNS and datagrams before importing application code. Existing app/dependency fixtures supply offline services and test auth settings. No real authentication session, provider access or integration verification is claimed.

The runner used `-B`, `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, and a scratch basetemp under this worktree. Each production mutation was saved as a patch, exercised, and restored from exact original bytes in `finally` before its restored test run. Temporary HTTP probe additions were similarly restored. Source preservation was checked against every tracked file, including the public demo database, and the initial full diff.

Initial command: `.venv/bin/python -B .review4/runner.py tests/test_errors.py tests/test_game_logs.py` — **140 passed**.

Final restored command: `.venv/bin/python -B .review4/runner.py tests/test_errors.py tests/test_game_logs.py tests/test_self_filters.py` — **153 passed, 37 subtests passed**.

## Every recent test: mutation mapping

| Newly changed test | Production defect reintroduced | Mutant → restored |
| --- | --- | --- |
| `test_game_logs_long_ordinary_ranges_keep_the_invalid_operand` (`tests/test_errors.py:552`) | 01: use bounded sanitization to decide whether whole context was redacted | 1 failed → 1 passed |
| `test_game_logs_empty_stat_key_names_the_submitted_parameter_for_ranges` (`tests/test_errors.py:536`) | 02: use truthiness instead of `stat is not None` in malformed-range labeling | 1 failed → 1 passed |
| `test_game_logs_repeated_same_stat_keeps_ordinary_fragments` (`tests/test_errors.py:581`) | 03: gather all same-stat raw inputs instead of the failing entry's own context | 1 failed → 1 passed |

The recent deletion removes an identical duplicate definition, leaving one unchanged effective parameter-name redaction test. This was verified by AST inspection; its unchanged prior mutation was not replayed. Prior mutation 07 is repeated only to test the newly added regression that specifically closes its survivor gap; the other unchanged prior mutants were not rerun.

01 returns 200 zeroes instead of `bad`; 02 returns `self_filters` instead of `self_filters[]`; 03 returns `token=[REDACTED]` instead of the first entry's ordinary `bad`. All failures are value assertions through the real Flask HTTP seam, not syntax/import failures.

### Supplemental probes

The 30-case HTTP matrix uses prior review probe expectations on the revised candidate (source preserved as `probes.txt`):

- 20 quoted-credential cases: `token` and `password`, both quote styles, across minutes and self filters with canonical, whitespace/lowercase, unsupported and empty stat keys. Exact public envelopes match, and neither synthetic secret fragment appears.
- Four empty-key shapes: `a`, `a,b`, `1,2`, and the empty string preserve actual brackets and expected values.
- Six ordinary range cases: minutes/self filters each with a long invalid upper value, a long valid lower value followed by `bad`, and a short invalid lower value.

All 30 pass. Reapplying mutation 01 to the six ordinary probes gives **4 failed / 2 passed**; restoring gives **6 passed**. This independently reaches both arms even though the committed test stops at its first failed assertion. Reapplying mutation 02 to the four empty-key probes gives **2 failed / 2 passed**, specifically for `a` and the empty string; restoring gives **4 passed**. These are supplementary runs of the same three distinct mutants, not additional mutants.

## Evidence index

- [pin.json](review-4-standards-mutation-pin.json)
- [initial.diff](review-4-standards-mutation-initial.diff)
- [initial-manifest.json](review-4-standards-mutation-initial-manifest.json)
- [baseline.log](review-4-standards-mutation-baseline.log)
- [results.json](review-4-standards-mutation-results.json)
- [01-truncation-is-redaction.patch](review-4-standards-mutation-01-truncation-is-redaction.patch)
- [01-truncation-is-redaction.log](review-4-standards-mutation-01-truncation-is-redaction.log)
- [01-truncation-is-redaction-restored.log](review-4-standards-mutation-01-truncation-is-redaction-restored.log)
- [02-empty-key-malformed-range.patch](review-4-standards-mutation-02-empty-key-malformed-range.patch)
- [02-empty-key-malformed-range.log](review-4-standards-mutation-02-empty-key-malformed-range.log)
- [02-empty-key-malformed-range-restored.log](review-4-standards-mutation-02-empty-key-malformed-range-restored.log)
- [03-cross-entry-context.patch](review-4-standards-mutation-03-cross-entry-context.patch)
- [03-cross-entry-context.log](review-4-standards-mutation-03-cross-entry-context.log)
- [03-cross-entry-context-restored.log](review-4-standards-mutation-03-cross-entry-context-restored.log)
- [candidate-probes.log](review-4-standards-mutation-candidate-probes.log)
- [candidate-probes-restored.log](review-4-standards-mutation-candidate-probes-restored.log)
- [01-truncation-both-arms.log](review-4-standards-mutation-01-truncation-both-arms.log)
- [01-truncation-both-arms-restored.log](review-4-standards-mutation-01-truncation-both-arms-restored.log)
- [02-empty-key-empty-value.log](review-4-standards-mutation-02-empty-key-empty-value.log)
- [02-empty-key-empty-value-restored.log](review-4-standards-mutation-02-empty-key-empty-value-restored.log)
- [final-restored.log](review-4-standards-mutation-final-restored.log)
- [preservation.log](review-4-standards-mutation-preservation.log)
- [test-definition-check.log](review-4-standards-mutation-test-definition-check.log)
- [runner.py](review-4-standards-mutation-runner.py)
- [mutations.py](review-4-standards-mutation-mutations.py)
- [probes.txt](review-4-standards-mutation-probes.txt)

To reproduce the driver, copy the exported runner and mutation harness back to `.review4/runner.py` and `.review4/mutations.py` in this pinned checkout, along with initial manifest/diff snapshots. The harness reads the prior probe source path shown in its code. The logged commands show the actual scratch locations used in this run.

## Final preservation and limits

All 496 tracked file SHA-256 values match the initial manifest, and the final `git diff HEAD --binary` matches byte-for-byte. Only the original six candidate files remain modified. Review scratch files were removed after evidence export. No QA services were started. Full completion-gate results remain the parent's responsibility.

The required workflow reflection found no new candidate beyond already documented review instructions; no skill or external ledger was modified.
