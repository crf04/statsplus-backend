# Issue 145 — review 3, independent Standards review

## Main report (under 400 words)

1. **P2 — Truncation causes ordinary rejected values to disappear.** `app/routes/game_routes.py:214` tests `sanitize_public_value(candidate) != candidate` to detect credential redaction, but that sanitizer also truncates. Through HTTP, both `minutes_filter` and `self_filters[PTS]` with `"0" * 201 + ",bad"` return `values=["0" * 200]`, omitting the rejected `bad` and reporting a valid numeric bound. `"0," + "A" * 300` similarly includes the valid `0,` prefix. Four independent candidate probes fail. Distinguish redaction from truncation before selecting the complete value, and add long ordinary split-value tests. Citation: `docs/API_DOCUMENTATION.md:1279–1283`, “only the unusable entries appear”; authoritative spec: “which parameter failed and which submitted values were unusable.”

2. **P2 — Empty-key coverage misses the malformed-range path.** `tests/test_errors.py:537–550` tests only `self_filters[]=a,b`, exercising relabeling. Reverting only `app/models/game_logs.py:308` from `if stat is not None` to `if stat` passes **all 137 affected tests**, yet HTTP `self_filters[]=a` and `self_filters[]=` lose their brackets and report `self_filters`. Independent probes fail for both; restoring the candidate passes. Parameterize the regression with malformed/empty ranges too. Citation: `AGENTS.md:29`, “Add or update a test that fails for the behavior being changed”; `docs/API_DOCUMENTATION.md:1279`, `self_filters[STAT]` with the actual stat.

The two previous findings are resolved: parameter-only truncation removal fails, and added outer validation metadata fails all 13 updated envelope cases. All **9 recently changed test functions / 20 collected cases** are killed by at least one targeted mutant. **7 distinct mutants: 6 killed, 1 survived** the existing tests. Final affected tests: **137 passed**; relevant self-filter checks: **13 passed / 37 subtests**. All 20 quoted-credential HTTP probes passed. Candidate bytes and full diff are unchanged. No bootstrap, full gate, whole suite, publishing, or delegation.

## Pinned scope and standards

- Work performed only in `/tmp/statsplus-145/review-3-standards` (physical path `/private/tmp/statsplus-145/review-3-standards`), plus the explicitly requested evidence exports beside this report.
- HEAD/base: `e1db9337bf9fdfeac980a77841578e6e13440b75`. The candidate is six uncommitted files. Exact diff command: `git diff HEAD --binary`; there are no candidate commits after HEAD. A three-dot commit comparison would omit the supplied working-tree candidate.
- Initial full diff SHA-256: `5f029b6facc35a9a1fdb6169c17fad9a0cb65b94c265ac7a6d197222ad2b7d19`.
- Sources: owning `AGENTS.md`, supplied global instructions, `CONTRIBUTING.md`, architecture runtime/error/game-log/test seams, API general error and game-log contracts, the supplied unedited authoritative spec, full/recent diffs, prior mutation report. The local code-review skill supplied the Standards and smell criteria; explicit single-reviewer/no-agent scope overrides its delegation procedure.
- Reviewed all production changes for parsing acceptance, typed/NLP compatibility, HTTP/model ownership, generic fallback, error-envelope preservation, centralized redaction, canonical vocabulary, and credential/provider boundaries. No further material design-smell or architecture finding was established. Tool-enforced issues were excluded as instructed; the duplicated identical test definition is not presented as a separate review finding. Its effective collected function was mutation-tested.
- Python 3.11.9 from the existing `.venv` executable. No environment installation, bootstrap, account switching, browser, provider call, or QA service was needed. No claim of independent authentication/account verification is made.

## Execution and offline isolation

Each pytest subprocess ran from the repository root through the saved runner, with socket connection, DNS, and datagram operations blocked before importing application code. Credential-related environment variables were stripped; dotenv loading and pytest plugin auto-loading were disabled. The existing `client`/dependency fixtures inject credential-free settings and services. Tests used `-B`, `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`, and a scratch basetemp under this checkout. Native pytest subtest support handled `test_self_filters.py`.

A production mutant was written only in this review worktree, tested, and restored from its original bytes in `finally` **before** the restored test run. Each restoration was byte-compared. Probe tests were temporarily appended only to `tests/test_errors.py` and also restored in `finally`. The full tracked-file snapshot includes the demo database. The parent owns the full completion gate; this review ran only the explicitly allowed two affected files and relevant self-filter cases.

## Verification evidence

- [baseline.log](review-3-standards-mutation-baseline.log)
- [candidate-probes.log](review-3-standards-mutation-candidate-probes.log)
- [candidate-probes-restored.log](review-3-standards-mutation-candidate-probes-restored.log)
- [07-empty-stat-malformed-shape-expanded.log](review-3-standards-mutation-07-empty-stat-malformed-shape-expanded.log)
- [07-empty-stat-malformed-shape-expanded-restored.log](review-3-standards-mutation-07-empty-stat-malformed-shape-expanded-restored.log)
- [07-empty-stat-malformed-shape-probes.log](review-3-standards-mutation-07-empty-stat-malformed-shape-probes.log)
- [07-empty-stat-malformed-shape-probes-restored.log](review-3-standards-mutation-07-empty-stat-malformed-shape-probes-restored.log)
- [self-filters.log](review-3-standards-mutation-self-filters.log)
- [counterfactual-redaction-only.log](review-3-standards-mutation-counterfactual-redaction-only.log)
- [final-restored.log](review-3-standards-mutation-final-restored.log)
- [preservation.log](review-3-standards-mutation-preservation.log)
- [initial.diff](review-3-standards-mutation-initial.diff)
- [initial-manifest.json](review-3-standards-mutation-initial-manifest.json)
- [results.json](review-3-standards-mutation-results.json)

### Candidate HTTP probes and reproduction

The review-only probe source is [probes.txt](review-3-standards-mutation-probes.txt). Its runner is [probe_runner.py](review-3-standards-mutation-probe_runner.py). The candidate produces **26 passes and 4 failures** across 30 cases:

- 20 passes: both single and double quotes, both `token` and `password`, across `minutes_filter`, `self_filters[PTS]`, a whitespace/lowercase valid stat, an unsupported stat, and the actual empty `self_filters[]` key. Each intact quoted value contains `synthetic-left,synthetic-right`. Assertions cover exact outer/error/detail envelopes and ensure neither synthetic credential fragment is reachable.
- 4 passes: actual empty brackets with `a`, `a,b`, `1,2`, and an empty range.
- 2 passes: ordinary short invalid `a,20`, for minutes and self filters.
- 4 failures: the two long ordinary input shapes described in finding 1, each on minutes and self filters. Exact output is retained in the candidate-probes log. The cause correctly carries the invalid fragment; the new HTTP presentation helper replaces it because shortening the whole input looks like redaction.

An independent temporary **counterfactual**, saved as [counterfactual-redaction-only.patch](review-3-standards-mutation-counterfactual-redaction-only.patch), replaces only the redaction-detection comparison with the unbounded diagnostic sanitizer, retaining the normal bounded publisher. It makes **all 30 probes pass**, including all credential cases. This is causal evidence, not an implementation change or proposed production coding style. Both production and test bytes were immediately restored, and the final **137-test run passed**. Driver: [followup.py](review-3-standards-mutation-followup.py).

### Existing empty-key coverage survivor

Mutant 07 changes only the `len(parts) != 2` label selection. The new `a,b` regression still passes because it follows `_relabel_self_filter_failure`, not that branch. The mutant then survives both complete allowed affected files (**137 passed**). The review-only HTTP empty-key matrix detects it: **2 failed / 2 passed**, versus **4 passed** immediately after restoration. This is a regression-test gap; the unmutated candidate currently preserves empty brackets. Driver: [empty_probe_runner.py](review-3-standards-mutation-empty_probe_runner.py).

## Every recent test: mutation mapping

| Recently changed test function | Mutation | Result |
| --- | --- | --- |
| `test_game_logs_parameter_names_are_redacted_and_bounded_too` (one effective collected definition) | 01 | Fails with parameter redaction removed |
| `test_game_logs_published_parameter_names_are_bounded` | 02 | Fails with parameter truncation alone removed; short credential-redaction test still passes |
| `test_game_logs_split_credential_values_redact_the_complete_value` | 03 | Fails when only fragments are sanitized |
| `test_game_logs_quoted_password_across_split_is_redacted` | 03 | Fails for double-quoted password across self-filter split |
| `test_game_logs_split_credential_in_minutes_is_redacted` | 03 | Fails for single-quoted token across minutes split |
| `test_game_logs_ordinary_split_values_stay_identifiable` | 04 | Fails when whole ordinary values replace offending parts |
| `test_game_logs_empty_stat_key_names_the_submitted_parameter` | 05 | Fails when relabeling drops actual empty brackets |
| `test_route_returns_400_for_malformed_filters` (7 cases) | 06 | Every case fails for outer validation metadata |
| `test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service` (6 cases) | 06 | Every case fails for outer validation metadata |

Mutant 07 is supplementary branch-focused coverage, not a claim that the newly added empty-key test never fails under mutation. Its primary relabeling mutant 05 is killed. Unchanged prior test mutants were not replayed; 01 tests the effective definition newly duplicated in RECENT DIFF, while 02 and 06 verify fixes to the prior findings.

## Exact mutations and logs

Harness: [mutations.py](review-3-standards-mutation-mutations.py); offline runner: [runner.py](review-3-standards-mutation-runner.py).

### 01-unredacted-parameter

- Outcome: **KILLED**; mutant exit 1, restored exit 0.
- [Mutant log](review-3-standards-mutation-01-unredacted-parameter.log) · [Restored log](review-3-standards-mutation-01-unredacted-parameter-restored.log) · [Patch](review-3-standards-mutation-01-unredacted-parameter.patch)
- Selected tests: `tests/test_errors.py::test_game_logs_parameter_names_are_redacted_and_bounded_too`.

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

### 02-parameter-only-no-cap

- Outcome: **KILLED**; mutant exit 1, restored exit 0.
- [Mutant log](review-3-standards-mutation-02-parameter-only-no-cap.log) · [Restored log](review-3-standards-mutation-02-parameter-only-no-cap-restored.log) · [Patch](review-3-standards-mutation-02-parameter-only-no-cap.patch)
- Selected tests: `tests/test_errors.py::test_game_logs_published_parameter_names_are_bounded`, `tests/test_errors.py::test_game_logs_parameter_names_are_redacted_and_bounded_too`.

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

### 03-fragment-only-redaction

- Outcome: **KILLED**; mutant exit 1, restored exit 0.
- [Mutant log](review-3-standards-mutation-03-fragment-only-redaction.log) · [Restored log](review-3-standards-mutation-03-fragment-only-redaction-restored.log) · [Patch](review-3-standards-mutation-03-fragment-only-redaction.patch)
- Selected tests: `tests/test_errors.py::test_game_logs_split_credential_values_redact_the_complete_value`, `tests/test_errors.py::test_game_logs_quoted_password_across_split_is_redacted`, `tests/test_errors.py::test_game_logs_split_credential_in_minutes_is_redacted`.

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -240,10 +240,7 @@
             # Caller-supplied content can appear inside a parameter name
             # (a self_filter's stat), so it is redacted like the values.
             "parameter": sanitize_public_value(cause.parameter),
-            "values": _redacted_split_values(
-                list(cause.values),
-                _complete_game_log_values(cause.parameter, filters),
-            ),
+            "values": [sanitize_public_value(value) for value in cause.values],
         }
         if cause.supported_values is not None:
             facts["supported_values"] = list(cause.supported_values)
```

### 04-whole-ordinary-values

- Outcome: **KILLED**; mutant exit 1, restored exit 0.
- [Mutant log](review-3-standards-mutation-04-whole-ordinary-values.log) · [Restored log](review-3-standards-mutation-04-whole-ordinary-values-restored.log) · [Patch](review-3-standards-mutation-04-whole-ordinary-values.patch)
- Selected tests: `tests/test_errors.py::test_game_logs_ordinary_split_values_stay_identifiable`.

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -211,7 +211,7 @@
                 candidate
                 for candidate in complete_values
                 if fragment in candidate
-                and sanitize_public_value(candidate) != candidate
+                and True
             ),
             None,
         )
```

### 05-empty-stat-relabel

- Outcome: **KILLED**; mutant exit 1, restored exit 0.
- [Mutant log](review-3-standards-mutation-05-empty-stat-relabel.log) · [Restored log](review-3-standards-mutation-05-empty-stat-relabel-restored.log) · [Patch](review-3-standards-mutation-05-empty-stat-relabel.patch)
- Selected tests: `tests/test_errors.py::test_game_logs_empty_stat_key_names_the_submitted_parameter`.

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

### 06-extra-outer-envelope

- Outcome: **KILLED**; mutant exit 1, restored exit 0.
- [Mutant log](review-3-standards-mutation-06-extra-outer-envelope.log) · [Restored log](review-3-standards-mutation-06-extra-outer-envelope-restored.log) · [Patch](review-3-standards-mutation-06-extra-outer-envelope.patch)
- Selected tests: `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`, `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`.

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

### 07-empty-stat-malformed-shape

- Outcome: **SURVIVED**; mutant exit 0, restored exit 0.
- [Mutant log](review-3-standards-mutation-07-empty-stat-malformed-shape.log) · [Restored log](review-3-standards-mutation-07-empty-stat-malformed-shape-restored.log) · [Patch](review-3-standards-mutation-07-empty-stat-malformed-shape.patch)
- Selected tests: `tests/test_errors.py::test_game_logs_empty_stat_key_names_the_submitted_parameter`.

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -305,7 +305,7 @@
         )
     if len(parts) != 2:
         raise GameLogFilterError(
-            f"self_filters[{stat}]" if stat is not None else "self_filters",
+            f"self_filters[{stat}]" if stat else "self_filters",
             (str(raw),),
             f"self_filter for {stat!r} must contain min,max values",
         )
```

## Final preservation

All **496 tracked files** match the initial SHA-256 manifest. The final full diff is byte-for-byte identical to the initial diff. All temporary source and test mutations have been reverted. The scratch directory is removed after exporting this evidence; the checkout retains exactly the original six modified candidate files. No services were started. The substantive-task workflow reflection found no new proposal beyond existing review instructions; no skills or external ledger were changed.
