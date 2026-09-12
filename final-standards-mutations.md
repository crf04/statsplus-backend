# Issue 145 — final independent Standards review and mutation evidence

## Pinned candidate and scope

- Worktree: `/tmp/statsplus-145/final-review-standards` (physical path `/private/tmp/statsplus-145/final-review-standards`).
- Base and HEAD: `e1db9337bf9fdfeac980a77841578e6e13440b75`; no commits after the base. Candidate is the complete six-file uncommitted diff, not only the recent fixes.
- Review diff: `git diff e1db9337bf9fdfeac980a77841578e6e13440b75`; the user explicitly includes working-tree changes, so a three-dot HEAD comparison would omit this candidate.
- Sources read: local `AGENTS.md`, supplied global AGENTS instructions, `CONTRIBUTING.md`, architecture runtime/error/game-log/test seams, API general error and game-log contracts, the unedited authoritative issue in the assignment, full candidate diff, recent diff, and prior mutation report. No network retrieval or subagents.
- No implementation fixes or publishing. Mutations were applied one at a time and production bytes restored immediately in `finally`, before the restored test run. Every restoration was byte-compared.
- Parent owns bootstrap and completion gate. Neither was duplicated; no independent full-gate claim is made.

## Main findings

1. **P2 — Full error-envelope coverage is still weakened.** `tests/test_game_logs.py:936` and `:978` now compare only `response.get_json()["error"]`, whereas the original tests compared the entire response. Mutant 17 adds validation type/location beside `error`, exclusively on game-log detail responses, and **all 131 affected tests pass**. Restore whole-response assertions, including the added details. Exact issue requirement: “existing error-contract tests are extended rather than replaced.” `docs/API_DOCUMENTATION.md:42`: “Expected application failures use one stable JSON shape”; `:1295`: “No validation-library context, raw input dump, or provider material is ever included.” This is a regression-test defect, not a claim that the restored candidate currently emits the mutant payload.
2. **P2 — Parameter-length guarantee has no failing regression test.** `tests/test_errors.py:416` names redaction and bounds but submits only a short parameter. Mutant 16 preserves credential redaction while bypassing truncation only for parameter names; **all 131 tests pass**. Add a long caller-controlled self-filter stat through HTTP and assert its published parameter is bounded. `docs/API_DOCUMENTATION.md:1293`: “Parameter names are redacted and length-bounded the same way values are”; `AGENTS.md:29`: “Add or update a test that fails for the behavior being changed.” The candidate implementation does truncate names; this finding concerns the newly documented boundary's missing regression coverage.

No additional material implementation/architecture or smell-baseline finding was established. In particular, the previously demonstrated parameter credential leak is fixed, public facts reuse the centralized sanitizer, Pydantic context stays private, known/unknown failures are separated, provider boundaries are unchanged, canonical team vocabulary comes from the existing catalog, and generic messages remain. Minor annotation/comment/formatting issues were not promoted into material findings.

## Results and coverage

- Untouched target baseline: **131 passed** (`tests/test_errors.py tests/test_game_logs.py`).
- **10 recent new/changed test functions, 21 collected cases**, including both signature-only helper-test updates and all 13 parameterized HTTP cases, each killed by at least one independent targeted production mutant.
- **17 mutants: 14 killed, 3 survived.** Every restored test run passed. Detailed patches, selected test IDs, commands, observed failures and logs follow.
- All 13 route parameter cases fail for each of changed stable code, extra details metadata, changed generic message, and absent details.
- Additional affected self-filter suite: **13 passed, 37 subtests passed** (`tests/test_self_filters.py`).
- Final restored target suite: **131 passed**. Supplementary outer-envelope mutant also followed by a restored **131-pass** run.
- Nine HTTP probes on the actual candidate passed in one review-only test: long parameter, credential-shaped parameters with malformed and numeric ranges, both numeric self-filter operands invalid, zero upper bound, each singly submitted reversed playstyle bound, credential-shaped team value, and long team value. They check exact outer/error/details keys, allowed fact keys, string types, 200-character limits and absence of a synthetic secret. Probe source is saved with the evidence and removed from the checkout.
- Network was blocked at Python socket connect/connect_ex/create_connection/getaddrinfo/sendto; tests use injected credential-free app dependencies. Harness subprocess environment strips provider/database credential settings. No services were started, no databases were replaced, and bytecode/pytest cache writes were disabled.

## Every recent test: mutation mapping

| Test function | Primary mutation(s) |
| --- | --- |
| `test_game_logs_reversed_playstyle_range_names_submitted_bounds` | 04, 05, 06 (each of its three submitted-bound scenarios protected) |
| `test_game_logs_rejected_self_filter_numeric_values_name_the_stat` | 02 |
| `test_game_logs_rejected_self_filter_zero_bound_survives` | 03 |
| `test_game_logs_parameter_names_are_redacted_and_bounded_too` | 01 (redaction killed; separate bounds gap in 16) |
| `test_game_logs_published_values_are_bounded` | 07 |
| `test_game_logs_details_carry_only_the_documented_facts` | 08 |
| `test_game_log_validation_details_keep_known_and_skip_unknown` | 09 |
| `test_game_log_validation_details_skip_unknown_internal_failures` | 10 |
| `test_route_returns_400_for_malformed_filters` (7 cases) | 11, 12, 13, 14; every case fails |
| `test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service` (6 cases) | 11, 12, 13, 14; every case fails |

## Survivors and interpretation

- **15-nonnumeric-grammar** repeats the previous survivor: accepting alphabetic `game_filter` as 3 passes the unchanged integer-grammar test alone. Its docstring mentions non-numeric rejection but inputs still cover only whole and fractional numbers. This is retained as a coverage limitation, not a new production/acceptance finding, and not claimed to survive the entire repository gate.
- **16-parameter-only-no-cap** passes both full affected files. The shared value cap is now tested (07), but parameter-specific application of that cap is not.
- **17-extra-outer-envelope** passes both full affected files. Extra metadata *inside* details is now caught (12); metadata outside `error` is still ignored by the revised assertions.
- The previous full-payload sanitized validation dump mutant is now killed by all 13 exact inner-error assertions (12), and the previous wrong-code survivor is likewise killed by all 13 (11).

## Evidence index

- Full initial diff SHA-256: `a29232bf20e8a08c548f48f8d85cf94155ca22c7369f6645461fd1a5879773d8`. [Initial candidate diff](final-standards-mutation-initial.diff) · [Preservation manifest](final-standards-mutation-preservation.log).
- [baseline log](final-standards-mutation-baseline.log).
- [final-restored log](final-standards-mutation-final-restored.log).
- [candidate-probes log](final-standards-mutation-candidate-probes.log).
- [self-filters log](final-standards-mutation-self-filters.log).
- [Machine-readable results](final-standards-mutation-results.json). Harness sources: [primary](final-standards-mutation-harness.py), [supplementary](final-standards-mutation-supplementary.py), [offline guard](final-standards-mutation-offline-guard.py), [candidate probes](final-standards-mutation-candidate-probes.py).

## Detailed mutations

### 01-unredacted-parameter

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-01-unredacted-parameter.log) · [Restored log](final-standards-mutation-01-unredacted-parameter-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_parameter_names_are_redacted_and_bounded_too`

Observed failures:

```text
tests/test_errors.py::test_game_logs_parameter_names_are_redacted_and_bounded_too FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_parameter_names_are_redacted_and_bounded_too
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
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

### 02-missing-numeric-self-filter

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-02-missing-numeric-self-filter.log) · [Restored log](final-standards-mutation-02-missing-numeric-self-filter-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_rejected_self_filter_numeric_values_name_the_stat`

Observed failures:

```text
tests/test_errors.py::test_game_logs_rejected_self_filter_numeric_values_name_the_stat FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_rejected_self_filter_numeric_values_name_the_stat
```

Exact production mutation:

```diff
--- app/models/game_logs.py
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

### 03-drop-zero-upper

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-03-drop-zero-upper.log) · [Restored log](final-standards-mutation-03-drop-zero-upper-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_rejected_self_filter_zero_bound_survives`

Observed failures:

```text
tests/test_errors.py::test_game_logs_rejected_self_filter_zero_bound_survives FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_rejected_self_filter_zero_bound_survives
```

Exact production mutation:

```diff
--- app/models/game_logs.py
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

### 04-blame-default-bounds

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-04-blame-default-bounds.log) · [Restored log](final-standards-mutation-04-blame-default-bounds-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds`

Observed failures:

```text
tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -167,7 +167,7 @@
             "values": [sanitize_public_value(str(bound))],
         }
         for parameter, bound in bounds
-        if args.getlist(parameter)
+        if True
     ]
 
 
```

### 05-drop-submitted-max

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-05-drop-submitted-max.log) · [Restored log](final-standards-mutation-05-drop-submitted-max-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds`

Observed failures:

```text
tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -167,7 +167,7 @@
             "values": [sanitize_public_value(str(bound))],
         }
         for parameter, bound in bounds
-        if args.getlist(parameter)
+        if args.getlist(parameter) and parameter == "playstyle_RTG_min"
     ]
 
 
```

### 06-drop-submitted-min

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-06-drop-submitted-min.log) · [Restored log](final-standards-mutation-06-drop-submitted-min-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds`

Observed failures:

```text
tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_reversed_playstyle_range_names_submitted_bounds
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -167,7 +167,7 @@
             "values": [sanitize_public_value(str(bound))],
         }
         for parameter, bound in bounds
-        if args.getlist(parameter)
+        if args.getlist(parameter) and parameter == "playstyle_RTG_max"
     ]
 
 
```

### 07-remove-output-cap

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-07-remove-output-cap.log) · [Restored log](final-standards-mutation-07-remove-output-cap-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_published_values_are_bounded`

Observed failures:

```text
tests/test_errors.py::test_game_logs_published_values_are_bounded FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_published_values_are_bounded - As...
```

Exact production mutation:

```diff
--- app/errors.py
+++ app/errors.py (mutant)
@@ -126,7 +126,7 @@
     sanitized = _sanitize_diagnostic_detail(value)
     if sanitized is None:
         return ""
-    return sanitized[:_PUBLIC_VALUE_MAX_LENGTH]
+    return sanitized
 
 
 def _log_application_error(
```

### 08-entry-validation-metadata

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-08-entry-validation-metadata.log) · [Restored log](final-standards-mutation-08-entry-validation-metadata-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_details_carry_only_the_documented_facts`

Observed failures:

```text
tests/test_errors.py::test_game_logs_details_carry_only_the_documented_facts FAILED [100%]
FAILED tests/test_errors.py::test_game_logs_details_carry_only_the_documented_facts
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -237,7 +237,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": [dict(fact, type="value_error", loc=["teams_against"], input="NotAFilter") for fact in failed_filters]}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

### 09-unknown-blanks-known

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-09-unknown-blanks-known.log) · [Restored log](final-standards-mutation-09-unknown-blanks-known-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_log_validation_details_keep_known_and_skip_unknown`

Observed failures:

```text
tests/test_errors.py::test_game_log_validation_details_keep_known_and_skip_unknown FAILED [100%]
FAILED tests/test_errors.py::test_game_log_validation_details_keep_known_and_skip_unknown
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -235,6 +235,8 @@
             failed_filters.extend(facts)
         elif facts is not None:
             failed_filters.append(facts)
+        else:
+            return None
     if not failed_filters:
         return None
     return {"filters": failed_filters}
```

### 10-unknown-details

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-10-unknown-details.log) · [Restored log](final-standards-mutation-10-unknown-details-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_log_validation_details_skip_unknown_internal_failures`

Observed failures:

```text
tests/test_errors.py::test_game_log_validation_details_skip_unknown_internal_failures FAILED [100%]
FAILED tests/test_errors.py::test_game_log_validation_details_skip_unknown_internal_failures
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -236,7 +236,7 @@
         elif facts is not None:
             failed_filters.append(facts)
     if not failed_filters:
-        return None
+        return {"filters": [{"parameter": "self_filters", "values": ["oops"]}]}
     return {"filters": failed_filters}
 
 
```

### 11-wrong-error-code

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-11-wrong-error-code.log) · [Restored log](final-standards-mutation-11-wrong-error-code-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`
- `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

Observed failures:

```text
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0] FAILED [  7%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1] FAILED [ 15%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2] FAILED [ 23%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3] FAILED [ 30%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4] FAILED [ 38%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5] FAILED [ 46%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6] FAILED [ 53%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0] FAILED [ 61%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1] FAILED [ 69%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2] FAILED [ 76%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3] FAILED [ 84%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4] FAILED [ 92%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5] FAILED [100%]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -128,7 +128,9 @@
     try:
         return player_name, GameLogQuery(**filters)
     except ValidationError as error:
-        raise InvalidInputError(
+        class WrongCode(InvalidInputError):
+            code = "wrong_code"
+        raise WrongCode(
             "One or more game log filters are invalid.",
             detail=error,
             public_details=_game_log_validation_details(error, filters, request.args),
```

### 12-extra-details-metadata

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-12-extra-details-metadata.log) · [Restored log](final-standards-mutation-12-extra-details-metadata-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`
- `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

Observed failures:

```text
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0] FAILED [  7%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1] FAILED [ 15%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2] FAILED [ 23%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3] FAILED [ 30%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4] FAILED [ 38%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5] FAILED [ 46%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6] FAILED [ 53%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0] FAILED [ 61%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1] FAILED [ 69%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2] FAILED [ 76%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3] FAILED [ 84%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4] FAILED [ 92%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5] FAILED [100%]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -237,7 +237,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": failed_filters, "validation": [{"type": item["type"], "loc": item["loc"], "input": sanitize_public_value(item["input"])} for item in error.errors()]}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

### 13-wrong-generic-message

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-13-wrong-generic-message.log) · [Restored log](final-standards-mutation-13-wrong-generic-message-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`
- `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

Observed failures:

```text
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0] FAILED [  7%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1] FAILED [ 15%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2] FAILED [ 23%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3] FAILED [ 30%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4] FAILED [ 38%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5] FAILED [ 46%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6] FAILED [ 53%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0] FAILED [ 61%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1] FAILED [ 69%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2] FAILED [ 76%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3] FAILED [ 84%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4] FAILED [ 92%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5] FAILED [100%]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -129,7 +129,7 @@
         return player_name, GameLogQuery(**filters)
     except ValidationError as error:
         raise InvalidInputError(
-            "One or more game log filters are invalid.",
+            "Bad game-log request.",
             detail=error,
             public_details=_game_log_validation_details(error, filters, request.args),
         ) from error
```

### 14-missing-filter-details

- Outcome: **KILLED**, exit 1; restored exit 0.
- [Mutant log](final-standards-mutation-14-missing-filter-details.log) · [Restored log](final-standards-mutation-14-missing-filter-details-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`
- `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

Observed failures:

```text
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0] FAILED [  7%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1] FAILED [ 15%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2] FAILED [ 23%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3] FAILED [ 30%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4] FAILED [ 38%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5] FAILED [ 46%]
tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6] FAILED [ 53%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0] FAILED [ 61%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1] FAILED [ 69%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2] FAILED [ 76%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3] FAILED [ 84%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4] FAILED [ 92%]
tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5] FAILED [100%]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_filters0]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_filters1]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_filters2]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_filters3]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_filters4]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_filters5]
FAILED tests/test_game_logs.py::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_filters6]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_filters0]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_filters1]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_filters2]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_filters3]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_filters4]
FAILED tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_filters5]
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -131,7 +131,7 @@
         raise InvalidInputError(
             "One or more game log filters are invalid.",
             detail=error,
-            public_details=_game_log_validation_details(error, filters, request.args),
+            public_details=None,
         ) from error
 
 
```

### 15-nonnumeric-grammar

- Outcome: **SURVIVED**, exit 0; restored exit 0.
- [Mutant log](final-standards-mutation-15-nonnumeric-grammar.log) · [Restored log](final-standards-mutation-15-nonnumeric-grammar-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_game_log_query_game_filter_keeps_the_http_integer_grammar`

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -367,6 +367,13 @@
     # can contain more than one constraint for the same stat.
     self_filters: list[SelfFilter] = Field(default_factory=list)
 
+    @field_validator("game_filter", mode="before")
+    @classmethod
+    def review_integer_parse(cls, value):
+        if isinstance(value, str) and value.isalpha():
+            return 3
+        return value
+
     @field_validator("season_filter", mode="before")
     @classmethod
     def normalize_season_filter(cls, value: Any) -> str:
```

### 16-parameter-only-no-cap

- Outcome: **SURVIVED**, exit 0; restored exit 0.
- [Mutant log](final-standards-mutation-16-parameter-only-no-cap.log) · [Restored log](final-standards-mutation-16-parameter-only-no-cap-restored.log).

Selected tests:

- `tests/test_errors.py`
- `tests/test_game_logs.py`

Exact production mutation:

```diff
--- app/routes/game_routes.py
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

### 17-extra-outer-envelope

- Outcome: **SURVIVED**, exit 0; restored exit 0.
- [Mutant log](final-standards-mutation-17-extra-outer-envelope.log) · [Restored log](final-standards-mutation-17-extra-outer-envelope-restored.log).

Selected tests:

- `tests/test_errors.py`
- `tests/test_game_logs.py`

Exact production mutation:

```diff
--- app/errors.py
+++ app/errors.py (mutant)
@@ -367,7 +367,7 @@
     error: dict[str, Any] = {"code": code, "message": message}
     if details is not None:
         error["details"] = details
-    return jsonify({"error": error}), status_code
+    return jsonify({"error": error, **({"validation": {"type": "value_error", "loc": ["teams_against"]}} if details is not None and "filters" in details else {})}), status_code
 
 
 def register_error_handlers(app: Flask) -> None:
```


## Final cleanup

Review-only harness, probe, and network-guard files were removed from the checkout after copying evidence. No review-owned services exist. Final full candidate diff and each of its six files match the initial bytes exactly. The worktree retains only the six original candidate modifications.
