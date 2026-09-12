# Independent SPEC re-review — issue 145, revision 4

**No material SPEC findings.** The prior truncation defect is corrected: the comparison at [game_routes.py:197](/tmp/statsplus-145/review-4-spec/app/routes/game_routes.py:197) uses unbounded credential redaction, and both HTTP paths retain `bad` after 250 ordinary zeroes. Each self-filter rejection now uses its own raw context at [game_routes.py:228](/tmp/statsplus-145/review-4-spec/app/routes/game_routes.py:228). Malformed `self_filters[]` retains its actual brackets at [game_logs.py:315](/tmp/statsplus-145/review-4-spec/app/models/game_logs.py:315).

All **three newly added test functions catch their intended mutants**. The long-range defect also fails independent HTTP assertions for both minutes and self filters, preventing the test's first assertion from masking its second path. Every mutant was immediately restored; all selected restored checks pass.

The unmodified candidate passes **26 independent HTTP probes**, including both quoted credential forms, repeated self filters in both orders, long ordinary bounds, and four empty-stat forms. The final targeted suite passes **142 tests and 6 subtests**. Full-scope inspection found no material missing requirement, incorrect implementation, or scope creep.

All **496 tracked files, the complete diff, and Git status match the initial snapshot exactly**. No bootstrap, full gate, whole-tests-directory run, implementation fix, publishing, or subagents. The parent owns the full gate; its result is not attested here.

## Scope, authority, and pinned source

- Working checkout: `/tmp/statsplus-145/review-4-spec` (physical path `/private/tmp/statsplus-145/review-4-spec`). Review artifacts are only at the explicitly requested sibling report/log locations.
- HEAD/base: `e1db9337bf9fdfeac980a77841578e6e13440b75`; candidate is the six-file uncommitted diff. Exact comparison: `git diff HEAD --binary`. There are no candidate commits above this base; a three-dot committed-branch comparison would omit the candidate.
- Initial/final diff SHA-256: `cbdcad9257791373a8dbe3ff2c453635b14e4f74c2420ac9438a03b62575c376`.
- Authority: the user's UNEDITED AUTHORITATIVE SPEC, FULL DIFF, RECENT DIFF FROM PREVIOUS REVIEW SNAPSHOT, prior findings, and explicit review constraints. The code-review skill was read and its SPEC rubric applied without delegation, as requested. Owning `AGENTS.md`, `CONTRIBUTING.md`, relevant architecture/error/runtime/testing sections, and the API error/game-log contract were read.
- Reviewed all six changed files, including model acceptance, typed error facts, HTTP translation, common error envelope, documentation, and existing/new contract tests.
- Read [prior mutation evidence](review-3-spec-mutations.md). Unchanged tests' mutants were not rerun. The newly added malformed-range test was tested against its specific bracket-loss defect, closing the prior review's surviving branch; only that branch was mutated.
- The duplicate redaction-test definition was deleted without changing the surviving identical definition at `tests/test_errors.py:417`; it creates no newly changed executable test to mutation-test. The recent diff adds exactly three test functions and changes no other test bodies.
- Model/account routing is controlled by the invoking harness and is not independently attested here. No model launcher, account switch, or subagent was used.

## Full original scope assessment

| Authoritative spec text | Assessment and evidence |
|---|---|
| “A rejected game-log filter returns, in the error payload, the parameter that failed and the offending values.” | Implemented at the typed model/HTTP seam. Existing affected tests exercise all documented filter names, unusable entries, reversed ranges, zero operands, rank mismatch, native parser errors, and self-filter stats. The prior long ordinary lower-bound defect now passes both independent HTTP cases and the new regression test. |
| “`teams_against` reports the unsupported entries; the supported vocabulary remains discoverable without being duplicated by callers.” | `app/models/game_logs.py:580` canonicalizes aliases, retains only unsupported entries, and attaches backend-owned canonical values and alias keys. Existing tests verify the exact catalog contents, `Arc3Assists`, and `<10 Ft`; mixed supported/unsupported input reports only unusable entries. |
| “No credential, provider, or internal diagnostic material becomes reachable through the new field.” | `app/errors.py:117` shares diagnostic redaction before the public cap. `app/routes/game_routes.py:222` emits only explicit facts, with a scalar allowlist for native parser errors. Existing error tests exclude validation context/input dumps and provider diagnostics. All 12 independent quoted-credential probes pass, including lowercase, whitespace, empty, and unsupported self-filter keys. |
| “The documented error contract is updated, and existing error-contract tests are extended rather than replaced.” | `docs/API_DOCUMENTATION.md:1253` documents the details shape, parameter names, vocabulary discovery, redaction, and generic fallback. Both existing parameterized route contract tests still assert the entire outer response envelope plus details (13 cases). Their earlier mutation evidence remains applicable. |
| “The existing generic message remains for anything that has no caller-actionable detail.” | The route retains the generic message at `app/routes/game_routes.py:132`; unknown errors are skipped independently and return no details when all errors are unknown (`:268`). Existing known-plus-unknown and unknown-only tests pass. |

The scope is actionable rejection details, not a new validation policy. No exhaustive aggregation of every simultaneously invalid operand or repeated filter was assumed; the existing fail-first behavior remains. No new vocabulary endpoint or frontend implementation was required by the supplied spec. No material acceptance regression or unrelated product behavior was identified.

## Offline verification and exact commands

Used existing `.venv/bin/python` (Python 3.11.9), without installation or environment mutation. Flask fixtures inject mocked dependencies and synthetic local authentication. An early [socket guard](review-4-spec-logs/sitecustomize.py) rejects connect, connect_ex, create_connection, and getaddrinfo. Bytecode/cache writes were disabled and a dedicated bytecode prefix prevents stale mutated imports. No live service was started.

Baseline and final restored command, from the pinned checkout root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$PWD/.review4/bytecode" PYTHONPATH="$PWD/.review4:$PWD" .venv/bin/python -m pytest tests/test_errors.py tests/test_game_logs.py tests/test_self_filters.py -k 'not TestSelfFilters or typed_operator_domain' -o addopts='' -p no:cacheprovider -q --tb=short
```

Both [baseline](review-4-spec-logs/baseline.log) and [restored](review-4-spec-logs/restored.log): **142 passed, 11 deselected, 6 subtests passed**. This selects the error/game-log suites and only the two typed self-filter operator cases; it does not select the NLP self-filter class's other tests.

Independent probe command used the same environment, with:

```sh
.venv/bin/python -m pytest tests/test_review4_probes.py -o addopts='' -p no:cacheprovider -q -s --tb=short --junitxml=/tmp/statsplus-145/review-4-spec-logs/independent-probes.xml
```

The [temporary probe source](review-4-spec-logs/probes.txt) used the existing HTTP client fixtures. **26 passed**: [response log](review-4-spec-logs/independent-probes.log), [JUnit evidence](review-4-spec-logs/independent-probes.xml).

- Twelve credential cases: single-quoted token and double-quoted password containing a comma, across `minutes_filter`, `self_filters[PTS]`, `[pts]`, `[ PTS ]`, `[]`, and `[BOGUS]`. Both synthetic secret halves are absent and only the correctly redacted value appears.
- Four empty-stat forms: `a,b`, `a`, `1,2`, and empty string. Each names `self_filters[]` with appropriate rejected values.
- Two long-bound cases: 250 zeroes followed by `,bad`, under minutes and PTS self filters. Unbounded redaction leaves the original unchanged; public truncation differs; the response identifies only `bad`.
- Eight repeated-context cases: malformed or numeric ordinary rejection followed by quoted token/password with an overlapping fragment, plus the reverse order. The first rejected entry uses its own context; neither synthetic secret half leaks.

No parent gate outcome or live authenticated integration outcome is claimed.

## Newly changed test coverage

| New test | Source | Relevant mutant | Result |
|---|---|---|---|
| `test_game_logs_empty_stat_key_names_the_submitted_parameter_for_ranges` | `tests/test_errors.py:536` | 01: remove brackets only for a malformed empty-stat range | Fails; restored passes |
| `test_game_logs_long_ordinary_ranges_keep_the_invalid_operand` | `tests/test_errors.py:552` | 02: use capped sanitization to detect redaction | Fails; restored passes. Independent minute and self-filter probes each fail under this same mutant. |
| `test_game_logs_repeated_same_stat_keeps_ordinary_fragments` | `tests/test_errors.py:581` | 03: search every same-stat input for credential context | Fails; restored passes |

**3/3 mutants killed**, all by assertion failures, with no setup/collection errors. The mutation suite ran the three candidate tests plus ten independent cases: nine assertion failures under the relevant mutants; four reverse-order repeated-context controls appropriately remain passing. These controls are not surviving mutants. Every selected case passes after restoration.

## Mutation procedure and exact evidence

[Runner](review-4-spec-logs/mutate.py), [machine-readable matrix](review-4-spec-logs/mutations.json). Each mutation touched one runtime file in this checkout. The runner saved original bytes, changed the relevant behavior, ran selected tests, restored the bytes in `finally` immediately when the subprocess exited, verified every tracked hash and full diff, reran the restored selected tests, then verified preservation again. Candidate test assertions were never weakened or edited. The independent probe file was removed before the final targeted run.

### 01-empty-malformed-range

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -312,7 +312,7 @@
         )
     if len(parts) != 2:
         raise GameLogFilterError(
-            f"self_filters[{stat}]" if stat is not None else "self_filters",
+            f"self_filters[{stat}]" if stat else "self_filters",
             (str(raw),),
             f"self_filter for {stat!r} must contain min,max values",
         )
```

Mutant exit 1; restored exit 0. [Mutant log](review-4-spec-logs/01-empty-malformed-range.log), [restored log](review-4-spec-logs/01-empty-malformed-range-restored.log).

- `tests.test_errors::test_game_logs_empty_stat_key_names_the_submitted_parameter_for_ranges` — FAILED

### 02-truncation-is-redaction

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -194,7 +194,7 @@
                 candidate
                 for candidate in complete_values
                 if fragment in candidate
-                and redact_public_value(candidate) != candidate
+                and sanitize_public_value(candidate) != candidate
             ),
             None,
         )
```

Mutant exit 1; restored exit 0. [Mutant log](review-4-spec-logs/02-truncation-is-redaction.log), [restored log](review-4-spec-logs/02-truncation-is-redaction-restored.log).

- `tests.test_errors::test_game_logs_long_ordinary_ranges_keep_the_invalid_operand` — FAILED

- `tests.test_review4_probes::test_review4_long_bound[minutes_filter]` — FAILED

- `tests.test_review4_probes::test_review4_long_bound[self_filters[PTS]]` — FAILED

### 03-repeated-stat-context

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -225,7 +225,9 @@
             "parameter": sanitize_public_value(cause.parameter),
             "values": _redacted_split_values(
                 list(cause.values),
-                [cause.context] if isinstance(cause.context, str) else [],
+                ([raw for stat, raw in filters.get("self_filters", []) if cause.parameter == f"self_filters[{stat}]"]
+                 if cause.parameter.startswith("self_filters[")
+                 else [cause.context] if isinstance(cause.context, str) else []),
             ),
         }
         if cause.supported_values is not None:
```

Mutant exit 1; restored exit 0. [Mutant log](review-4-spec-logs/03-repeated-stat-context.log), [restored log](review-4-spec-logs/03-repeated-stat-context-restored.log).

- `tests.test_errors::test_game_logs_repeated_same_stat_keeps_ordinary_fragments` — FAILED

- `tests.test_review4_probes::test_review4_repeated_context[False-bad-token='bad,synthetic-right'-token=[REDACTED]]` — FAILED

- `tests.test_review4_probes::test_review4_repeated_context[False-0,bad-token='bad,synthetic-right'-token=[REDACTED]]` — FAILED

- `tests.test_review4_probes::test_review4_repeated_context[False-0,bad-password="bad,synthetic-right"-password=[REDACTED]]` — FAILED

- `tests.test_review4_probes::test_review4_repeated_context[False-0,bad-token='synthetic-left,bad'-token=[REDACTED]]` — FAILED

- `tests.test_review4_probes::test_review4_repeated_context[True-bad-token='bad,synthetic-right'-token=[REDACTED]]` — PASSED

- `tests.test_review4_probes::test_review4_repeated_context[True-0,bad-token='bad,synthetic-right'-token=[REDACTED]]` — PASSED

- `tests.test_review4_probes::test_review4_repeated_context[True-0,bad-password="bad,synthetic-right"-password=[REDACTED]]` — PASSED

- `tests.test_review4_probes::test_review4_repeated_context[True-0,bad-token='synthetic-left,bad'-token=[REDACTED]]` — PASSED

## Final preservation

[Initial fingerprints](review-4-spec-logs/initial-files.json), [final fingerprints](review-4-spec-logs/final-files.json), [preservation evidence](review-4-spec-logs/preservation.log), [initial complete diff](review-4-spec-logs/initial.diff), [final complete diff](review-4-spec-logs/final.diff), [initial status](review-4-spec-logs/initial-status.txt), [final status](review-4-spec-logs/final-status.txt).

All 496 tracked files match initial SHA-256 hashes.
Full binary diff matches byte-for-byte.
Git status matches byte-for-byte.
Diff SHA-256: cbdcad9257791373a8dbe3ff2c453635b14e4f74c2420ac9438a03b62575c376
Temporary review scripts and probes removed.
