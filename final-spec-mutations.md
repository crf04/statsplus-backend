# Final independent SPEC review — issue 145

Three material spec findings remain.

1. **P1 — Splitting operands defeats credential redaction.** [game_routes.py:191](/tmp/statsplus-145/final-review-spec/app/routes/game_routes.py:191) sanitizes individual operands after splitting. `self_filters[PTS]=token='synthetic-left,synthetic-right'` returns both secret fragments; sanitizing the intact value produces `token=[REDACTED]`. `minutes_filter` also leaks the first fragment. Three HTTP reproduction cases fail the no-secret assertion. Violates the exact spec: “No credential, provider, or internal diagnostic material becomes reachable through the new field,” and [API_DOCUMENTATION.md:1295](/tmp/statsplus-145/final-review-spec/docs/API_DOCUMENTATION.md:1295), requiring credential-shaped material to receive diagnostic-equivalent redaction. Preserve credential context before publishing split values.

2. **P2 — Empty self-filter stats still identify an unsubmitted parameter.** [game_logs.py:338](/tmp/statsplus-145/final-review-spec/app/models/game_logs.py:338) and [:306](/tmp/statsplus-145/final-review-spec/app/models/game_logs.py:306) turn `self_filters[]=a,b` or `self_filters[]=bad` into parameter `self_filters`; the submitted key is `self_filters[]`. Two HTTP assertions fail; `self_filters[]=1,2` correctly retains the brackets. Violates “the parameter that failed and the offending values” and [API_DOCUMENTATION.md:1279](/tmp/statsplus-145/final-review-spec/docs/API_DOCUMENTATION.md:1279): “`self_filters[STAT]` with the actual stat.” Distinguish an empty submitted stat from absent internal attribution.

3. **P2 — Existing outer-envelope assertions remain weakened.** [test_game_logs.py:936](/tmp/statsplus-145/final-review-spec/tests/test_game_logs.py:936) and [:978](/tmp/statsplus-145/final-review-spec/tests/test_game_logs.py:978) compare only `response.get_json()["error"]`; the base compared the complete response. Adding an unexpected top-level field leaves all 13 cases passing. Violates “existing error-contract tests are extended rather than replaced” and the stable JSON shape in [API_DOCUMENTATION.md:42](/tmp/statsplus-145/final-review-spec/docs/API_DOCUMENTATION.md:42). Retain whole-response equality with the new details included.

Both affected files pass: **131 tests**. All **10 recent changed test functions / 21 cases** were independently mutation-tested: **16/19 valid mutants caught; three survived**. Every restoration passed. Full gate intentionally left to the parent. Candidate files and full diff preserved byte-for-byte.

## Scope and pinned evidence

- Worktree: `/tmp/statsplus-145/final-review-spec` (physical path `/private/tmp/statsplus-145/final-review-spec`), with all commands run at its root.
- HEAD and base: `e1db9337bf9fdfeac980a77841578e6e13440b75`. No intervening commits. Candidate is the six-file working-tree diff, not committed HEAD.
- Exact comparison: `git diff --binary e1db9337bf9fdfeac980a77841578e6e13440b75`; initial/final SHA-256: `a29232bf20e8a08c548f48f8d85cf94155ca22c7369f6645461fd1a5879773d8`.
- Read local AGENTS.md, CONTRIBUTING.md, and the relevant architecture and API sections; reviewed against the complete unedited authoritative issue supplied in the task. The prior review report was supporting evidence only. No issue fetch, network lookup, publishing, implementation fix, or subagent was used.
- Per the reviewer's assigned scope and the explicit gate exception, neither bootstrap nor the full completion gate was run. No opinion about the parent's still-independent gate is implied by this report.
- Interpreter: Python 3.11.9 from the existing `.venv`. Network disabled in test processes by an early `sitecustomize` guard replacing socket connection and DNS entry points; route tests use the existing injected mocked dependencies. Bytecode writes and pytest cache disabled; a dedicated bytecode prefix prevents stale imports during mutations.
- Temporary source mutations were isolated to this checkout and restored in `finally` before each restored test run. Every selected restored run passed. Every restoration compared all six candidate files and the complete binary diff against the initial bytes.
- No QA service was started. Temporary probe tests, mutation scripts, backups, and offline guard were removed after final verification. Only requested review artifacts and logs persist outside the checkout.

## Requirement assessment

| Exact authoritative requirement | Assessment |
|---|---|
| “A rejected game-log filter returns, in the error payload, the parameter that failed and the offending values.” | Implemented for the ordinary covered paths; empty-stat attribution is still wrong (finding 2). |
| “`teams_against` reports the unsupported entries; the supported vocabulary remains discoverable without being duplicated by callers.” | Implemented: unsupported entries only, canonical `SUPPORTED_TEAM_FILTERS`, and accepted `TEAM_FILTER_ALIASES` keys. Existing tests pass and the constants remain the source. |
| “No credential, provider, or internal diagnostic material becomes reachable through the new field.” | Not satisfied: credential parsing context is lost when operands are split (finding 1). Known typed facts and allowlisted native scalar failures otherwise avoid returning validation context. |
| “The documented error contract is updated, and existing error-contract tests are extended rather than replaced.” | Documentation updated; original root-envelope equality assertions are still lost (finding 3). Code, message, details structure, and inner-envelope checks are restored. |
| “The existing generic message remains for anything that has no caller-actionable detail.” | Implemented and exercised by unchanged error tests plus unknown-shape helper tests; unknown shapes produce no details, and mixed known/unknown retains the known facts. |

No additional material scope creep was found. This report addresses the assigned spec axis only.

## Baseline and final restored checks

Command for both runs:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$PWD/.final-spec-review/bytecode" PYTHONPATH="$PWD/.final-spec-review:$PWD" .venv/bin/python -m pytest tests/test_errors.py tests/test_game_logs.py -o addopts='' -p no:cacheprovider -q --tb=short
```

- [Initial baseline](final-spec-baseline.log): **131 passed**.
- [Final restored affected suite](final-spec-restored.log): **131 passed**.
- [Preservation evidence](final-spec-preservation.log): all six file hashes, exact diff hash, final status.

## Reproductions on the unmutated candidate

[Observation payloads](final-spec-probes.log) contain ten synthetic HTTP requests and actual response bodies, with intact-input sanitizer output for comparison. [Failing property checks](final-spec-reproductions.log) contain **5 expected failures and 11 passes** on the original candidate:

- Quoted token split in `self_filters[PTS]`: both synthetic secret fragments leak.
- Quoted password split in `self_filters[PTS]`: both synthetic secret fragments leak.
- Quoted token split in `minutes_filter`: the first fragment leaks.
- Empty-stat `self_filters[]=a,b`: parameter is `self_filters`, not `self_filters[]`.
- Empty-stat `self_filters[]=bad`: same incorrect parameter name.
- Empty-stat `self_filters[]=1,2`: control case passes, retaining `self_filters[]`.

All strings labelled as secrets are synthetic. These are production-property failures against the unchanged candidate, not mutation-test setup errors. No proposed fix is installed.

Other observed limits, not counted as additional main findings: validators stop at the first invalid minutes/playstyle operand or self-filter entry (`minutes_filter=a,b` reports only `a`; two invalid playstyle bounds report only the minimum; invalid PTS and REB self filters report only PTS). The original spec does not explicitly require exhaustive aggregation across all failures, so this is recorded without adding an exhaustiveness requirement. The ordinary BOGUS numeric case, zero upper bound, reversed playstyle bounds, and long parameter cap now behave as intended.

## Complete recent-test coverage matrix

The recent diff adds six test functions, changes two helper-call tests mechanically, and strengthens two parameterized route functions. All ten are included here; the two mechanical changes are covered as well. Earlier changed tests outside the recent diff were exercised in the 131-test run; their prior detailed mutation evidence remains in `/tmp/statsplus-145/resumed-spec-mutations.md`.

| Test function (prefix `tests/test_errors.py::` unless shown) | Cases | Mutants |
|---|---:|---|
| `test_game_logs_reversed_playstyle_range_names_submitted_bounds` | 1 (three HTTP scenarios) | 05, 06, 07b: each scenario individually reached and killed |
| `test_game_logs_rejected_self_filter_numeric_values_name_the_stat` | 1 | 02, 03 killed; 17 survived |
| `test_game_logs_rejected_self_filter_zero_bound_survives` | 1 | 04 killed |
| `test_game_logs_parameter_names_are_redacted_and_bounded_too` | 1 | 01 killed; 16 survived |
| `test_game_logs_published_values_are_bounded` | 1 | 08 killed; 16 survived |
| `test_game_logs_details_carry_only_the_documented_facts` | 1 | 09 and 10 killed; the dedicated test survives 19, while other selected route cases kill it |
| `test_game_log_validation_details_keep_known_and_skip_unknown` | 1 | 14 killed |
| `test_game_log_validation_details_skip_unknown_internal_failures` | 1 | 15 killed |
| `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters` | 7 | All cases kill 09, 10, 11, 12, 18, 19; all survive 13 |
| `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service` | 6 | All cases kill 09, 10, 11, 12, 18, 19; all survive 13 |

## Mutation outcomes and limits

**19 valid production mutants: 16 killed, 3 survived.** Every one of the 21 recent test cases has at least one relevant assertion failure against a valid production mutant. Full per-case outcomes and patches follow.

- Former survivor: global output cap removed → bounded-value test fails (08).
- Former survivor: numeric conversion details removed → new self-numeric test fails (03); reverting the relabel also fails (02).
- Former survivor: `invalid_input` code changed → all 13 route cases and the new documented-facts case fail (10).
- Unredacted parameter names → secret-redaction test fails (01).
- Dropped zero upper operand → zero-bound test fails (04).
- Unsubmitted defaults blamed → reversed-range test fails (05); omission of the maximum with both supplied fails the second request (06), and blaming minimum default for sole maximum fails the third request (07b).
- Extra entry validation metadata → new structure test and all 13 route cases fail (09).
- Changed stable message/inner envelope → all 13 route cases fail (11/12).

Survivors have different implications:

1. **13 outer envelope**: all 13 cases pass with an extra root field. This is the retained-test regression described in main finding 3; other whole-suite central-handler tests may still reject such a global mutation.
2. **16 parameter-only cap**: both the parameter-name and value-cap tests pass when only names bypass the cap. Current production does cap names (the 300-character probe confirms it); the new parameter test only exercises a short secret and does not establish its claimed length bound.
3. **17 nonfinite operands**: the `a,b` numeric test passes when `nan`/`inf` lose actionable details. Current production reports both correctly. The new test proves conversion failures but not the nonfinite branch.

Partial coverage gap: mutant 19 inserts `ctx` at the details root. The new documented-facts test passes because it checks only individual entries; all 13 route cases reject the added details-root key. Mutant 19 is therefore killed overall, not counted as a survivor.

A first draft of mutation 07 contained an invalid comprehension expression and caused a setup SyntaxError. It was immediately restored, and its selected test passed after restoration. It is **excluded** from valid-mutant and killed counts. Corrected valid mutation **07b** ran successfully and failed the intended assertion at `tests/test_errors.py:383`; its restoration passed. The invalid draft logs remain for audit transparency.

| ID | Defect | Mutant result | Restored | Logs |
|---|---|---|---|---|
| 01-raw-parameter | Remove parameter redaction | 1 failed | 1 passed | [mutant](final-spec-mutation-01-raw-parameter.log), [restored](final-spec-mutation-01-raw-parameter-restored.log) |
| 02-self-label | Restore lost-stat numeric attribution | 1 failed | 1 passed | [mutant](final-spec-mutation-02-self-label.log), [restored](final-spec-mutation-02-self-label-restored.log) |
| 03-self-numeric | Remove actionable numeric conversion failure | 1 failed | 1 passed | [mutant](final-spec-mutation-03-self-numeric.log), [restored](final-spec-mutation-03-self-numeric-restored.log) |
| 04-zero | Drop zero operand by truthiness | 1 failed | 1 passed | [mutant](final-spec-mutation-04-zero.log), [restored](final-spec-mutation-04-zero-restored.log) |
| 05-range-defaults | Blame unsubmitted default range bounds | 1 failed | 1 passed | [mutant](final-spec-mutation-05-range-defaults.log), [restored](final-spec-mutation-05-range-defaults-restored.log) |
| 06-range-both | Omit maximum only when both bounds were submitted | 1 failed | 1 passed | [mutant](final-spec-mutation-06-range-both.log), [restored](final-spec-mutation-06-range-both-restored.log) |
| 07-range-max-only | Drop sole submitted maximum and substitute minimum default | 1 error | 1 passed | [mutant](final-spec-mutation-07-range-max-only.log), [restored](final-spec-mutation-07-range-max-only-restored.log) |
| 08-no-value-cap | Remove public scalar output length cap | 1 failed | 1 passed | [mutant](final-spec-mutation-08-no-value-cap.log), [restored](final-spec-mutation-08-no-value-cap-restored.log) |
| 09-extra-metadata | Publish unapproved validation metadata on each detail entry | 14 failed | 14 passed | [mutant](final-spec-mutation-09-extra-metadata.log), [restored](final-spec-mutation-09-extra-metadata-restored.log) |
| 10-error-code | Change stable invalid_input code | 14 failed | 14 passed | [mutant](final-spec-mutation-10-error-code.log), [restored](final-spec-mutation-10-error-code-restored.log) |
| 11-error-message | Change stable generic game-log message | 13 failed | 13 passed | [mutant](final-spec-mutation-11-error-message.log), [restored](final-spec-mutation-11-error-message-restored.log) |
| 12-inner-envelope | Add unexpected inner error-envelope field | 13 failed | 13 passed | [mutant](final-spec-mutation-12-inner-envelope.log), [restored](final-spec-mutation-12-inner-envelope-restored.log) |
| 13-outer-envelope | Add unexpected root error-envelope field | 13 passed | 13 passed | [mutant](final-spec-mutation-13-outer-envelope.log), [restored](final-spec-mutation-13-outer-envelope-restored.log) |
| 14-known-unknown | Let unknown shape erase known failure details | 1 failed | 1 passed | [mutant](final-spec-mutation-14-known-unknown.log), [restored](final-spec-mutation-14-known-unknown-restored.log) |
| 15-unknown-only | Expose details for wholly unknown internal shape | 1 failed | 1 passed | [mutant](final-spec-mutation-15-unknown-only.log), [restored](final-spec-mutation-15-unknown-only-restored.log) |
| 16-parameter-cap | Retain redaction but remove cap only from parameter names | 2 passed | 2 passed | [mutant](final-spec-mutation-16-parameter-cap.log), [restored](final-spec-mutation-16-parameter-cap-restored.log) |
| 17-nonfinite-numeric | Remove details for nonfinite self-filter operands | 1 passed | 1 passed | [mutant](final-spec-mutation-17-nonfinite-numeric.log), [restored](final-spec-mutation-17-nonfinite-numeric-restored.log) |
| 18-missing-details | Omit details from malformed routes entirely | 13 failed | 13 passed | [mutant](final-spec-mutation-18-missing-details.log), [restored](final-spec-mutation-18-missing-details-restored.log) |
| 19-detail-root-metadata | Add metadata to details root | 13 failed, 1 passed | 14 passed | [mutant](final-spec-mutation-19-detail-root-metadata.log), [restored](final-spec-mutation-19-detail-root-metadata-restored.log) |
| 07b-range-max-only | Drop sole submitted maximum and substitute minimum default | 1 failed | 1 passed | [mutant](final-spec-mutation-07b-range-max-only.log), [restored](final-spec-mutation-07b-range-max-only-restored.log) |

## Exact mutation patches and individual test results

### 01-raw-parameter

Remove parameter redaction

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -187,7 +187,7 @@
         facts = {
             # Caller-supplied content can appear inside a parameter name
             # (a self_filter's stat), so it is redacted like the values.
-            "parameter": sanitize_public_value(cause.parameter),
+            "parameter": cause.parameter,
             "values": [sanitize_public_value(value) for value in cause.values],
         }
         if cause.supported_values is not None:
```

Selected nodes: `tests/test_errors.py::test_game_logs_parameter_names_are_redacted_and_bounded_too`

- `tests.test_errors::test_game_logs_parameter_names_are_redacted_and_bounded_too` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 02-self-label

Restore lost-stat numeric attribution

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -554,7 +554,7 @@
             try:
                 entry = _normalize_self_filter_entry(stat, raw)
             except ValidationError as error:
-                raise _relabel_self_filter_failure(stat, raw, error) from error
+                raise error
             normalized.append(entry)
         return normalized
 
```

Selected nodes: `tests/test_errors.py::test_game_logs_rejected_self_filter_numeric_values_name_the_stat`

- `tests.test_errors::test_game_logs_rejected_self_filter_numeric_values_name_the_stat` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 03-self-numeric

Remove actionable numeric conversion failure

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -226,11 +226,7 @@
         try:
             number = float(value)
         except (TypeError, ValueError) as error:
-            raise GameLogFilterError(
-                parameter,
-                (value,),
-                "self_filter values must be numbers",
-            ) from error
+            raise ValueError("self_filter values must be numbers") from error
         if not isfinite(number):
             raise GameLogFilterError(
                 parameter,
```

Selected nodes: `tests/test_errors.py::test_game_logs_rejected_self_filter_numeric_values_name_the_stat`

- `tests.test_errors::test_game_logs_rejected_self_filter_numeric_values_name_the_stat` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 04-zero

Drop zero operand by truthiness

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -252,7 +252,7 @@
                 tuple(
                     _format_rating(operand)
                     for operand in submitted
-                    if operand is not None
+                    if operand
                 ),
                 str(error),
             ) from error
```

Selected nodes: `tests/test_errors.py::test_game_logs_rejected_self_filter_zero_bound_survives`

- `tests.test_errors::test_game_logs_rejected_self_filter_zero_bound_survives` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 05-range-defaults

Blame unsubmitted default range bounds

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -167,7 +167,7 @@
             "values": [sanitize_public_value(str(bound))],
         }
         for parameter, bound in bounds
-        if args.getlist(parameter)
+        if True
     ]
 
 
```

Selected nodes: `tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds`

- `tests.test_errors::test_game_logs_reversed_playstyle_range_names_submitted_bounds` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 06-range-both

Omit maximum only when both bounds were submitted

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -167,7 +167,7 @@
             "values": [sanitize_public_value(str(bound))],
         }
         for parameter, bound in bounds
-        if args.getlist(parameter)
+        if args.getlist(parameter) and not (parameter == "playstyle_RTG_max" and args.getlist("playstyle_RTG_min"))
     ]
 
 
```

Selected nodes: `tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds`

- `tests.test_errors::test_game_logs_reversed_playstyle_range_names_submitted_bounds` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 07-range-max-only

Drop sole submitted maximum and substitute minimum default

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -167,7 +167,7 @@
             "values": [sanitize_public_value(str(bound))],
         }
         for parameter, bound in bounds
-        if args.getlist(parameter)
+        if args.getlist(parameter) if args.getlist("playstyle_RTG_min") else parameter == "playstyle_RTG_min"
     ]
 
 
```

Selected nodes: `tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds`

- `tests.test_errors::test_game_logs_reversed_playstyle_range_names_submitted_bounds` — **error**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 08-no-value-cap

Remove public scalar output length cap

```diff
--- app/errors.py (candidate)
+++ app/errors.py (mutant)
@@ -126,7 +126,7 @@
     sanitized = _sanitize_diagnostic_detail(value)
     if sanitized is None:
         return ""
-    return sanitized[:_PUBLIC_VALUE_MAX_LENGTH]
+    return sanitized
 
 
 def _log_application_error(
```

Selected nodes: `tests/test_errors.py::test_game_logs_published_values_are_bounded`

- `tests.test_errors::test_game_logs_published_values_are_bounded` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 09-extra-metadata

Publish unapproved validation metadata on each detail entry

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -237,7 +237,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": [dict(item, ctx="synthetic metadata") for item in failed_filters]}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

Selected nodes: `tests/test_errors.py::test_game_logs_details_carry_only_the_documented_facts`, `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

- `tests.test_errors::test_game_logs_details_carry_only_the_documented_facts` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — **failed**

Restored: exit 0; 14 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 10-error-code

Change stable invalid_input code

```diff
--- app/errors.py (candidate)
+++ app/errors.py (mutant)
@@ -197,7 +197,7 @@
     """The request could not be parsed or fails input validation."""
 
     status_code = 400
-    code = "invalid_input"
+    code = "wrong_code"
     default_message = "The request contains invalid input."
 
     def __init__(
```

Selected nodes: `tests/test_errors.py::test_game_logs_details_carry_only_the_documented_facts`, `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

- `tests.test_errors::test_game_logs_details_carry_only_the_documented_facts` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — **failed**

Restored: exit 0; 14 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 11-error-message

Change stable generic game-log message

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -129,7 +129,7 @@
         return player_name, GameLogQuery(**filters)
     except ValidationError as error:
         raise InvalidInputError(
-            "One or more game log filters are invalid.",
+            "Invalid filters.",
             detail=error,
             public_details=_game_log_validation_details(error, filters, request.args),
         ) from error
```

Selected nodes: `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — **failed**

Restored: exit 0; 13 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 12-inner-envelope

Add unexpected inner error-envelope field

```diff
--- app/errors.py (candidate)
+++ app/errors.py (mutant)
@@ -364,7 +364,7 @@
 ):
     """Build the one public JSON shape used for application errors."""
 
-    error: dict[str, Any] = {"code": code, "message": message}
+    error: dict[str, Any] = {"code": code, "message": message, "unexpected": "synthetic"}
     if details is not None:
         error["details"] = details
     return jsonify({"error": error}), status_code
```

Selected nodes: `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — **failed**

Restored: exit 0; 13 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 13-outer-envelope

Add unexpected root error-envelope field

```diff
--- app/errors.py (candidate)
+++ app/errors.py (mutant)
@@ -367,7 +367,7 @@
     error: dict[str, Any] = {"code": code, "message": message}
     if details is not None:
         error["details"] = details
-    return jsonify({"error": error}), status_code
+    return jsonify({"error": error, "unexpected": "synthetic"}), status_code
 
 
 def register_error_handlers(app: Flask) -> None:
```

Selected nodes: `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — **passed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — **passed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — **passed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — **passed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — **passed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — **passed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — **passed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — **passed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — **passed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — **passed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — **passed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — **passed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — **passed**

Restored: exit 0; 13 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 14-known-unknown

Let unknown shape erase known failure details

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -231,6 +231,8 @@
     for validation_error in error.errors():
         cause = (validation_error.get("ctx") or {}).get("error")
         facts = _game_log_rejected_filter(cause, validation_error, filters, args)
+        if facts is None:
+            return None
         if isinstance(facts, list):
             failed_filters.extend(facts)
         elif facts is not None:
```

Selected nodes: `tests/test_errors.py::test_game_log_validation_details_keep_known_and_skip_unknown`

- `tests.test_errors::test_game_log_validation_details_keep_known_and_skip_unknown` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 15-unknown-only

Expose details for wholly unknown internal shape

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -236,7 +236,7 @@
         elif facts is not None:
             failed_filters.append(facts)
     if not failed_filters:
-        return None
+        return {"filters": []}
     return {"filters": failed_filters}
 
 
```

Selected nodes: `tests/test_errors.py::test_game_log_validation_details_skip_unknown_internal_failures`

- `tests.test_errors::test_game_log_validation_details_skip_unknown_internal_failures` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 16-parameter-cap

Retain redaction but remove cap only from parameter names

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -187,7 +187,7 @@
         facts = {
             # Caller-supplied content can appear inside a parameter name
             # (a self_filter's stat), so it is redacted like the values.
-            "parameter": sanitize_public_value(cause.parameter),
+            "parameter": __import__("app.errors", fromlist=["_sanitize_diagnostic_detail"])._sanitize_diagnostic_detail(cause.parameter),
             "values": [sanitize_public_value(value) for value in cause.values],
         }
         if cause.supported_values is not None:
```

Selected nodes: `tests/test_errors.py::test_game_logs_parameter_names_are_redacted_and_bounded_too`, `tests/test_errors.py::test_game_logs_published_values_are_bounded`

- `tests.test_errors::test_game_logs_parameter_names_are_redacted_and_bounded_too` — **passed**
- `tests.test_errors::test_game_logs_published_values_are_bounded` — **passed**

Restored: exit 0; 2 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 17-nonfinite-numeric

Remove details for nonfinite self-filter operands

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -232,11 +232,7 @@
                 "self_filter values must be numbers",
             ) from error
         if not isfinite(number):
-            raise GameLogFilterError(
-                parameter,
-                (value,),
-                "self_filter values must be finite numbers",
-            )
+            raise ValueError("self_filter values must be finite numbers")
         return number
 
     @model_validator(mode="after")
```

Selected nodes: `tests/test_errors.py::test_game_logs_rejected_self_filter_numeric_values_name_the_stat`

- `tests.test_errors::test_game_logs_rejected_self_filter_numeric_values_name_the_stat` — **passed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 18-missing-details

Omit details from malformed routes entirely

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -131,7 +131,7 @@
         raise InvalidInputError(
             "One or more game log filters are invalid.",
             detail=error,
-            public_details=_game_log_validation_details(error, filters, request.args),
+            public_details=None,
         ) from error
 
 
```

Selected nodes: `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — **failed**

Restored: exit 0; 13 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 19-detail-root-metadata

Add metadata to details root

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -237,7 +237,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": failed_filters, "ctx": "synthetic metadata"}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

Selected nodes: `tests/test_errors.py::test_game_logs_details_carry_only_the_documented_facts`, `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

- `tests.test_errors::test_game_logs_details_carry_only_the_documented_facts` — **passed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]` — **failed**
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]` — **failed**
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]` — **failed**

Restored: exit 0; 14 passed. All candidate file bytes and the full initial diff matched before the restored run.

### 07b-range-max-only

Drop sole submitted maximum and substitute minimum default

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -167,7 +167,7 @@
             "values": [sanitize_public_value(str(bound))],
         }
         for parameter, bound in bounds
-        if args.getlist(parameter)
+        if (args.getlist(parameter) if args.getlist("playstyle_RTG_min") else parameter == "playstyle_RTG_min")
     ]
 
 
```

Selected nodes: `tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds`

- `tests.test_errors::test_game_logs_reversed_playstyle_range_names_submitted_bounds` — **failed**

Restored: exit 0; 1 passed. All candidate file bytes and the full initial diff matched before the restored run.

## Reproduction source (temporary, removed)

```python
import json
import pytest
from app.errors import sanitize_public_value

@pytest.mark.parametrize('query', [
    {'self_filters[PTS]': "token='synthetic-left,synthetic-right'"},
    {'self_filters[PTS]': 'password="synthetic-left,synthetic-right"'},
    {'self_filters[PTS]': 'nan,inf'},
    {'self_filters[PTS]': '2,0'},
    {'self_filters[]': 'a,b'},
    {'self_filters[PTS]': 'a,b', 'self_filters[REB]': 'c,d'},
    {'minutes_filter': 'a,b'},
    {'playstyle_RTG_min': 'a', 'playstyle_RTG_max': 'b'},
    {'self_filters[BOGUS]': 'a,b'},
    {'self_filters[' + 'A'*300 + ']': 'bad'},
])
def test_observe(client, query):
    response=client.get('/api/games/game_logs', query_string={'player_name':'LeBron James', **query})
    print(json.dumps({'query':query, 'status': response.status_code, 'payload':response.get_json(), 'whole_value_sanitized':{k:sanitize_public_value(v) for k,v in query.items()}}))
    assert response.status_code == 400

@pytest.mark.parametrize('parameter,raw', [
    ('self_filters[PTS]', "token='synthetic-left,synthetic-right'"),
    ('self_filters[PTS]', 'password="synthetic-left,synthetic-right"'),
    ('minutes_filter', "token='synthetic-left,synthetic-right'"),
])
def test_quoted_credentials_must_stay_redacted(client, parameter, raw):
    assert sanitize_public_value(raw) in ('token=[REDACTED]', 'password=[REDACTED]')
    response=client.get('/api/games/game_logs',query_string={'player_name':'LeBron James',parameter:raw})
    assert response.status_code==400
    assert 'synthetic-left' not in response.get_data(as_text=True)
    assert 'synthetic-right' not in response.get_data(as_text=True)

@pytest.mark.parametrize('raw', ['a,b', 'bad', '1,2'])
def test_empty_stat_retains_actual_parameter(client,raw):
    response=client.get('/api/games/game_logs',query_string={'player_name':'LeBron James','self_filters[]':raw})
    assert response.status_code==400
    assert response.get_json()['error']['details']['filters'][0]['parameter']=='self_filters[]'
```

## Candidate file fingerprints

| File | Initial and final SHA-256 |
|---|---|
| `app/errors.py` | `d596b7f49fdc43cae52a49858a8401218f42657ae533571a5e74b2c7d426534b` |
| `app/models/game_logs.py` | `f177c886f459f1d1b8d0f79f70e55abe1c7958edf291482e97a6d8865116cf37` |
| `app/routes/game_routes.py` | `649a0330f6b9c058f7fb69f515b9f1873f82b7bfb76c3ae3be6a076b21d0c9ae` |
| `docs/API_DOCUMENTATION.md` | `967a0cd1fb28f7834441a0f065b7cc2bb7597b22108802985cecb2e1b0e57970` |
| `tests/test_errors.py` | `4cd5d44783a21080cfee54fcf58442b8277d683a9ad7be2494e34ee6a4c1e6fb` |
| `tests/test_game_logs.py` | `3bd0868b1f8396ee01cd7cae103e9a7315e4b94c3e9cc36c87dd32f0d0c2385e` |
