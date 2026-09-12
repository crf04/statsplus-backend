# Independent SPEC review — issue 145, revision 3

One material SPEC finding remains.

**P2 — Truncation hides the offending operand.** At [game_routes.py:214](/tmp/statsplus-145/review-3-spec/app/routes/game_routes.py:214), `sanitize_public_value(candidate) != candidate` conflates credential redaction with the 200-character cap. Submit `"0" * 250 + ",bad"` as either `minutes_filter` or `self_filters[PTS]`: the zero bound parses successfully, but the HTTP 400 details return `["0" * 200]`, omitting `bad`. Both independent HTTP assertions fail on the unmodified candidate. This violates “the parameter that failed and the offending values” and [API_DOCUMENTATION.md:1279](/tmp/statsplus-145/review-3-spec/docs/API_DOCUMENTATION.md:1279), which requires unusable submitted values only. Detect redaction independently of truncation before replacing rejected fragments with the complete value.

The three previous production/test-envelope findings are corrected in the exercised paths. All 12 credential probes pass across both quote kinds, minutes, and varied self-filter keys; all three empty-stat shapes preserve brackets. The restored affected checks pass: **139 tests, plus 6 subtests**.

All **9 recently changed test functions / 20 collected cases** were mutation-tested: **8 of 9 mutants caught**. The survivor removes brackets only in malformed-range handling; the new empty-stat test covers numeric failures but misses that branch. All 13 strengthened envelope cases reject an extra root field, and removing parameter-only truncation fails its dedicated test. Every restored run passes.

No bootstrap, full gate, whole-suite run, publishing, delegation, or implementation fix. All **496 tracked files** and the **complete diff** equal the initial snapshot byte-for-byte.

## Scope and authority

- Only `/tmp/statsplus-145/review-3-spec` was used as the working checkout; its physical path is `/private/tmp/statsplus-145/review-3-spec`.
- Pinned HEAD/base: `e1db9337bf9fdfeac980a77841578e6e13440b75`. The candidate is the six-file uncommitted working-tree diff. No intervening commits; comparison is `git diff HEAD --binary`, not a three-dot committed-branch comparison.
- Initial/final complete diff SHA-256: `5f029b6facc35a9a1fdb6169c17fad9a0cb65b94c265ac7a6d197222ad2b7d19`.
- Authority: the user's complete UNEDITED AUTHORITATIVE SPEC, FULL DIFF, RECENT DIFF, and review constraints. Read owning `AGENTS.md`, `CONTRIBUTING.md`, relevant architecture/runtime/test-seam sections, and API error/game-log contract sections. Read the code-review skill; applied only the assigned SPEC axis, with no delegation as explicitly required. Reviewed all six candidate files against the full supplied diff, rather than limiting code review to recent hunks.
- Prior mutation record `/tmp/statsplus-145/final-spec-mutations.md` supplied unchanged-test evidence; unchanged mutants were not repeated except the newly strengthened tests' explicitly requested behaviors.
- The requested account/model selection belongs to the invoking harness; this review launched no model/account CLI and cannot independently attest the account. No authentication configuration was changed.
- Tests used the pre-existing `.venv` interpreter (Python 3.11.9). Its symlink targets the existing shared environment; no bootstrap/install or writes to that environment were performed.
- An early `sitecustomize` guard rejected socket connect/connect_ex/create_connection/getaddrinfo. Existing Flask fixtures inject mocked dependencies and local synthetic authentication. No network/provider credentials were used. Bytecode/cache writes were disabled, with a dedicated prefix to prevent stale source imports during mutation tests. No QA service was started.
- No parent full gate or whole repository suite was run. The deliberate scope override in the user task controls over the standard completion-gate instruction.

## Full-scope requirement assessment

| Exact authoritative requirement | Assessment |
|---|---|
| “A rejected game-log filter returns, in the error payload, the parameter that failed and the offending values.” | Ordinary paths identify the parameter and rejected values. Recent complete-value handling loses a short invalid bound following a long valid one: main finding. |
| “`teams_against` reports the unsupported entries; the supported vocabulary remains discoverable without being duplicated by callers.” | Supported by implementation and passing affected tests: rejected entries only, backend canonical values and alias keys supplied as facts. |
| “No credential, provider, or internal diagnostic material becomes reachable through the new field.” | Prior comma-split credential defect is fixed in all tested forms. Typed facts/native scalar allowlist avoid exposing Pydantic diagnostics; all 12 additional quoted-credential cases pass. No additional material leak identified. |
| “The documented error contract is updated, and existing error-contract tests are extended rather than replaced.” | API document updated and whole outer envelope assertions restored in both parameterized route tests. All 13 cases kill the extra-root-field mutant. |
| “The existing generic message remains for anything that has no caller-actionable detail.” | Preserved by existing error behavior and the unknown-internal-shape tests in the affected run. Known facts survive a neighboring unknown failure. |

No additional material scope creep or acceptance regression was established. Exhaustive aggregation of every invalid operand/parameter is not an explicit requirement; existing fail-first validation behavior was not promoted into a new requirement. The duplicated same-name test definition at `tests/test_errors.py:417` and `:434` is recorded as a collection limit, not a separate material SPEC finding: Python replaces the first with the identical second, so it contributes one collected case.

## Baseline and restored checks

Exact command, run from the review checkout root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$PWD/.review3/bytecode" PYTHONPATH="$PWD/.review3:$PWD" .venv/bin/python -m pytest tests/test_errors.py tests/test_game_logs.py tests/test_self_filters.py -k 'not TestSelfFilters or typed_operator_domain' -o addopts='' -p no:cacheprovider -q --tb=short
```

The filter selects all affected error/game-log tests and only the two relevant typed self-filter operator cases. **139 passed, 11 deselected, 6 subtests passed** in both [baseline](review-3-spec-logs/baseline.log) and [restored run](review-3-spec-logs/probes-restored.log). The latter runs after removing all temporary probe definitions and restoring the candidate exactly. Each mutant also has a separate passing restored run over its selected tests.

## Independent candidate probes

[Exact temporary probe source](review-3-spec-logs/probes.txt), [runner](review-3-spec-logs/probe_runner.py), [HTTP payloads and assertion results](review-3-spec-logs/independent-probes.log), [JUnit results](review-3-spec-logs/independent-probes.xml).

Temporary probes were appended to `tests/test_errors.py` to use the real existing fixtures, run only with `-k test_review3_`, then removed in `finally` by restoring the original file bytes. **21 cases: 19 pass, 2 fail** on the unmutated candidate:

- 12 credential cases: `token='synthetic-left,synthetic-right'` and `password="synthetic-left,synthetic-right"`, each sent as `minutes_filter`, `self_filters[PTS]`, `self_filters[pts]`, `self_filters[ PTS ]`, `self_filters[]`, and `self_filters[BOGUS]`. Every response is 400; neither synthetic secret half leaks; published values contain only the correctly redacted complete credential.
- Three actual empty-stat cases: `self_filters[]=a,b`, `=bad`, and `=1,2`. Each reports `self_filters[]`.
- Four ordinary invalid-value controls pass: `minutes_filter=0,bad`, `self_filters[PTS]=0,bad`, `minutes_filter=a,20`, and `self_filters[PTS]=high,low`.
- Two normal-invalid-value failures: 250 zeroes followed by `,bad` under minutes and PTS self filters. The probes assert diagnostic sanitization leaves each intact input unchanged (there is no credential). Both actual 400 response payloads publish 200 zeroes as their sole offending value instead of `bad`. The long zero bound is accepted numerically; the rejected operand disappears solely because the complete string exceeds the public cap.

## Recent changed-test coverage

All seven test functions in recent `test_errors.py` hunks and both changed `test_game_logs.py` parameterized functions are covered. The same-name duplicate counts once because pytest collects it once.

| Test function | Collected cases | Relevant mutant(s) |
|---|---:|---|
| `test_game_logs_parameter_names_are_redacted_and_bounded_too` | 1 | 01 |
| `test_game_logs_published_parameter_names_are_bounded` | 1 | 02; retains redaction and removes only parameter truncation |
| `test_game_logs_split_credential_values_redact_the_complete_value` | 1 | 03, 07 |
| `test_game_logs_quoted_password_across_split_is_redacted` | 1 | 03, 07 |
| `test_game_logs_split_credential_in_minutes_is_redacted` | 1 | 03, 08 |
| `test_game_logs_ordinary_split_values_stay_identifiable` | 1 | 04 |
| `test_game_logs_empty_stat_key_names_the_submitted_parameter` | 1 | 05 killed; 09 survived |
| `test_route_returns_400_for_malformed_filters` | 7 | 06; every case killed |
| `test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service` | 6 | 06; every case killed |

## Mutation method and results

[Exact runner](review-3-spec-logs/mutate.py), [machine-readable matrix](review-3-spec-logs/mutations.json), [offline guard](review-3-spec-logs/sitecustomize.py). Every source mutation was confined to this review checkout, restored in `finally` immediately after the mutant test process exited, checked against all initial tracked-file hashes and the entire binary diff, and followed by a passing selected-test run. No candidate test assertions were weakened or edited during mutations. Every killed mutant failed an assertion; no setup/collection errors occurred.

**8 killed, 1 survived.** Each of the 20 recent collected test cases catches at least one directly relevant mutant. The survivor is an additional coverage probe, not a current production failure: reverting the two malformed-shape labels leaves `self_filters[]=a,b` working but breaks `self_filters[]=bad`. The independent unmutated `bad` probe passes, so the current implementation is correct for that path; adding that regression case would protect it.

### 01-parameter-redaction

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -239,7 +239,7 @@
         facts = {
             # Caller-supplied content can appear inside a parameter name
             # (a self_filter's stat), so it is redacted like the values.
-            "parameter": sanitize_public_value(cause.parameter),
+            "parameter": cause.parameter,
             "values": _redacted_split_values(
                 list(cause.values),
                 _complete_game_log_values(cause.parameter, filters),
```

- `tests.test_errors::test_game_logs_parameter_names_are_redacted_and_bounded_too` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/01-parameter-redaction.log), [restored log](review-3-spec-logs/01-parameter-redaction-restored.log).

### 02-parameter-only-cap

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -239,7 +239,7 @@
         facts = {
             # Caller-supplied content can appear inside a parameter name
             # (a self_filter's stat), so it is redacted like the values.
-            "parameter": sanitize_public_value(cause.parameter),
+            "parameter": __import__("app.errors", fromlist=["_sanitize_diagnostic_detail"])._sanitize_diagnostic_detail(cause.parameter),
             "values": _redacted_split_values(
                 list(cause.values),
                 _complete_game_log_values(cause.parameter, filters),
```

- `tests.test_errors::test_game_logs_published_parameter_names_are_bounded` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/02-parameter-only-cap.log), [restored log](review-3-spec-logs/02-parameter-only-cap-restored.log).

### 03-split-context

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -242,7 +242,7 @@
             "parameter": sanitize_public_value(cause.parameter),
             "values": _redacted_split_values(
                 list(cause.values),
-                _complete_game_log_values(cause.parameter, filters),
+                [],
             ),
         }
         if cause.supported_values is not None:
```

- `tests.test_errors::test_game_logs_split_credential_values_redact_the_complete_value` — FAILED (mutant caught)
- `tests.test_errors::test_game_logs_quoted_password_across_split_is_redacted` — FAILED (mutant caught)
- `tests.test_errors::test_game_logs_split_credential_in_minutes_is_redacted` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/03-split-context.log), [restored log](review-3-spec-logs/03-split-context-restored.log).

### 04-ordinary-fragments

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -220,7 +220,7 @@
             if complete is not None
             else sanitize_public_value(fragment)
         )
-    return list(dict.fromkeys(published))
+    return [sanitize_public_value(value) for value in complete_values] if complete_values else list(dict.fromkeys(published))
 
 
 def _game_log_rejected_filter(
```

- `tests.test_errors::test_game_logs_ordinary_split_values_stay_identifiable` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/04-ordinary-fragments.log), [restored log](review-3-spec-logs/04-ordinary-fragments-restored.log).

### 05-empty-relabel

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -339,7 +339,7 @@
         return error
     # An empty stat is still a submitted key value (``self_filters[]``);
     # only a missing stat (typed, non-HTTP calls) stays unnamed.
-    label = f"self_filters[{stat}]" if stat is not None else "self_filters"
+    label = f"self_filters[{stat}]" if stat else "self_filters"
     return GameLogFilterError(
         label,
         tuple(rejected_values),
```

- `tests.test_errors::test_game_logs_empty_stat_key_names_the_submitted_parameter` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/05-empty-relabel.log), [restored log](review-3-spec-logs/05-empty-relabel-restored.log).

### 06-outer-envelope

```diff
--- app/errors.py
+++ app/errors.py (mutant)
@@ -367,7 +367,7 @@
     error: dict[str, Any] = {"code": code, "message": message}
     if details is not None:
         error["details"] = details
-    return jsonify({"error": error}), status_code
+    return jsonify({"error": error, "unexpected": "synthetic"}), status_code
 
 
 def register_error_handlers(app: Flask) -> None:
```

- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — FAILED (mutant caught)
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/06-outer-envelope.log), [restored log](review-3-spec-logs/06-outer-envelope-restored.log).

### 07-self-only-context

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -180,7 +180,7 @@
     if parameter == "minutes_filter":
         raw = filters.get("minutes_filter")
         return [raw] if isinstance(raw, str) and raw else []
-    if parameter.startswith("self_filters["):
+    if False and parameter.startswith("self_filters["):
         stat = parameter[len("self_filters["):-1]
         return [
             raw
```

- `tests.test_errors::test_game_logs_split_credential_values_redact_the_complete_value` — FAILED (mutant caught)
- `tests.test_errors::test_game_logs_quoted_password_across_split_is_redacted` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/07-self-only-context.log), [restored log](review-3-spec-logs/07-self-only-context-restored.log).

### 08-minutes-only-context

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -177,7 +177,7 @@
 ) -> list[str]:
     """The complete submitted values a published fragment may come from."""
 
-    if parameter == "minutes_filter":
+    if False and parameter == "minutes_filter":
         raw = filters.get("minutes_filter")
         return [raw] if isinstance(raw, str) and raw else []
     if parameter.startswith("self_filters["):
```

- `tests.test_errors::test_game_logs_split_credential_in_minutes_is_redacted` — FAILED (mutant caught)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/08-minutes-only-context.log), [restored log](review-3-spec-logs/08-minutes-only-context-restored.log).

### 09-empty-malformed-range

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -299,13 +299,13 @@
         # An empty stat is still a submitted key value (``self_filters[]``);
         # only a missing stat (typed, non-HTTP calls) stays unnamed.
         raise GameLogFilterError(
-            f"self_filters[{stat}]" if stat is not None else "self_filters",
+            f"self_filters[{stat}]" if stat else "self_filters",
             (str(raw),),
             f"self_filter for {stat!r} must be a range or typed comparison",
         )
     if len(parts) != 2:
         raise GameLogFilterError(
-            f"self_filters[{stat}]" if stat is not None else "self_filters",
+            f"self_filters[{stat}]" if stat else "self_filters",
             (str(raw),),
             f"self_filter for {stat!r} must contain min,max values",
         )
```

- `tests.test_errors::test_game_logs_empty_stat_key_names_the_submitted_parameter` — PASSED (mutant survived)

Restored selected tests: exit 0. [Mutant log](review-3-spec-logs/09-empty-malformed-range.log), [restored log](review-3-spec-logs/09-empty-malformed-range-restored.log).

## Final preservation

[Initial fingerprints](review-3-spec-logs/initial-files.json), [preservation evidence](review-3-spec-logs/preservation.log), [initial full diff](review-3-spec-logs/initial.diff), [final full diff](review-3-spec-logs/final.diff). All 496 tracked files and the full diff match; no implementation changes remain. Temporary `.review3` scripts/guard were removed from the checkout after copying evidence into the requested log directory. Review artifacts only remain at the requested sibling report/log locations.
