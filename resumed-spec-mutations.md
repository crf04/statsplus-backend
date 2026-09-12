# Independent issue 145 SPEC review: mutation evidence

## Candidate and isolation

- Checkout: `/tmp/statsplus-145/resumed-review-spec` (physical path `/private/tmp/statsplus-145/resumed-review-spec`).
- HEAD and base: `e1db9337bf9fdfeac980a77841578e6e13440b75`. The candidate is the full uncommitted six-file diff; no prior summaries or commit 6545f833 were treated as authority.
- Diff command: `git diff e1db9337bf9fdfeac980a77841578e6e13440b75`.
- Initial binary diff SHA-256: `39b0f28f726d01a66d9e24fda53d93cc0cd555cd3d9aa0cdedad1b84ca764391`.
- Python: 3.11.9. The pre-existing `.venv` symlink points to the implementation checkout. Its interpreter/dependencies were read only, with `PYTHONDONTWRITEBYTECODE=1` and `-B`; no bootstrap/package installation was performed because it would write into that protected shared environment.
- Production mutations were confined to this isolated checkout, made sequentially, and restored from byte snapshots in `finally` before running each restored test. All six candidate files and the entire binary diff were verified byte-for-byte after the mutation series.
- The existing fixtures inject mock dependencies and synthetic authentication. A temporary pytest plugin additionally replaced socket connect/connect_ex/create_connection with an assertion, preventing network provider calls. No real credentials were used. The public demo database was not modified.
- Sources read: supplied unedited authoritative issue, AGENTS.md, docs/ARCHITECTURE.md (runtime, request flow and test seams), docs/API_DOCUMENTATION.md (error/game-log/filter contracts), CONTRIBUTING.md. No external issue fetch or external write.

## Results

- Baseline: **125 passed** across `tests/test_errors.py` and `tests/test_game_logs.py`.
- Inventory: **22 new or substantively changed test functions, 33 collected cases** (19 error functions, one grammar function, seven malformed-filter cases, six season/rating cases). AST comparison against the base verifies that every changed test function appears in the mutation matrix.
- **26 targeted production mutants killed**, covering every changed function and every parameterized case. These produced 37 expected assertion failures in total; no collection/setup errors. Every corresponding restored run passed.
- **3 additional coverage mutants survived**; they are reported below, separately from successful targeted mutation checks.
- Logs record the exact pytest command, outcome and exact mutation against the original candidate, followed by a separate restored-run log. The mutation runner log is [resumed-spec-mutation-runner.log](resumed-spec-mutation-runner.log).
- Baseline log: [resumed-spec-mutation-baseline.log](resumed-spec-mutation-baseline.log).

## Confirmed spec defects and reproduction evidence

All requests below used `GET /api/games/game_logs` with `player_name=LeBron James`, real Flask route parsing and the existing mocked dependency fixture. All returned 400 before calling the game service. Synthetic credential-shaped text only.

1. **P1 — Credential-shaped material bypasses sanitization through `parameter`.** `app/routes/game_routes.py:155` publishes `cause.parameter` directly, while `app/models/game_logs.py:208` and `:292` can incorporate untrusted stat names. `self_filters[token=synthetic-secret]=bad-range` returns `parameter: "self_filters[token=synthetic-secret]"` verbatim. A valid-shaped `1,2` range similarly exposes an uppercased secret in the parameter while its `values` is redacted. Issue: **“No credential, provider, or internal diagnostic material becomes reachable through the new field.”** API documentation `:1292–1294`: **“anything credential-shaped is redacted the same way the diagnostics are.”** A 1000-character stat also produces a 1014-character parameter despite its value being bounded to 200 characters.

2. **P2 — Numeric failures can name a nonexistent self-filter parameter.** At `app/models/game_logs.py:220`, `info.data.get("stat")` becomes `None` when validation of the submitted stat failed. `self_filters[BOGUS]=a,b` returns a BOGUS-stat refusal plus numeric refusals for `self_filters[None]`, values `a` and `b`. The caller never supplied that parameter. Issue: **“A rejected game-log filter returns, in the error payload, the parameter that failed and the offending values.”** API `:1278`: **“self_filters[STAT] with the actual stat.”**

3. **P2 — A reversed rating range can blame a value never submitted.** `app/models/game_logs.py:549–552` always selects maximum after defaulting and normalization. Submitting only `playstyle_RTG_min=201` yields `playstyle_RTG_max` / `["200"]`, omitting the submitted `201` and instead reporting the service default. Issue Outcome: **“which parameter failed and which submitted values were unusable.”** API `:1278–1279`: **“values lists the unusable submitted values.”**

4. **P2 — A zero upper bound is lost from the rejected self-filter range.** `app/models/game_logs.py:244` uses truthiness to include `value2`. `self_filters[PTS]=2,0` returns values `["2.0"]`; the upper bound `0` making the range reversed disappears. The identifying facts no longer describe the submitted range. Same issue Outcome and API `:1278–1279` citations as above.

Full response evidence: [resumed-spec-mutation-probes.log](resumed-spec-mutation-probes.log). These nine temporary observation probes passed because they asserted rejection and no service access while capturing the actual defects; they are not claimed as passing correctness tests and were removed afterwards.

## Surviving mutants and coverage gaps

- **27-length-bound:** Removing `[:_PUBLIC_VALUE_MAX_LENGTH]` from `sanitize_public_value` leaves all **125 tests passing**. No changed test submits overlength values. The current implementation does bound individual values, but its bound is unprotected and dynamic parameter names remain unbounded.
- **28-self-numeric:** Reverting both `SelfFilter.normalize_numeric_value` typed failures to ordinary `ValueError` leaves all **125 tests passing**. Existing tests still reject invalid input but do not require actionable details for malformed/nonfinite self-filter numbers. The newly added self-filter detail test covers only an unsupported stat with valid numeric bounds; it misses the wrong-parameter and zero-bound cases above.
- **29-lost-code-assertion:** Changing `InvalidInputError.code` to `wrong_code` leaves all **13 cases in the two changed route parameterizations passing**. Before the candidate, both groups asserted an exact envelope including `invalid_input`; the candidate removes that assertion at `tests/test_game_logs.py:932–940` and `:975–986`. This is a regression in those tests relative to the issue’s **“existing error-contract tests are extended rather than replaced”** requirement and API `:1251` (`code invalid_input`). The whole suite still has separate central-handler code checks: this scoped survivor does not mean the wrong code survives the full suite.
- Fail-fast coverage limit observed, but not counted as an additional main finding: `minutes_filter=a,b` publishes only `a`; two invalid playstyle bounds publish only the first; invalid PTS and REB self-filter entries publish only PTS. No changed test checks complete reporting of multiple invalid bounds or multiple self-filter entries. The rank-specific test does check all invalid rank entries.
- Credential coverage exercises a team **value** only; no changed test puts credential-shaped data into the dynamic self-filter **parameter**. The response-shape helper maps entries into a dict, so duplicate parameter entries can also overwrite one another in assertions.

## Complete changed-test coverage matrix

| Test function | Targeted mutants |
|---|---|
| `tests/test_errors.py::test_game_logs_invalid_input_uses_central_handler` | 01-central |
| `tests/test_errors.py::test_game_logs_rejected_teams_against_names_parameter_and_values` | 02-team-values |
| `tests/test_errors.py::test_game_logs_teams_against_rejection_carries_the_canonical_vocabulary` | 03-vocabulary, 04-aliases |
| `tests/test_errors.py::test_game_logs_rejected_opponent_tricode_names_parameter_and_value` | 05-opponent |
| `tests/test_errors.py::test_game_logs_rank_filter_rejection_reports_every_unusable_entry` | 06-all-ranks |
| `tests/test_errors.py::test_game_logs_model_level_alignment_failure_names_submitted_values` | 07-alignment |
| `tests/test_errors.py::test_game_logs_range_failures_name_their_submitted_values` | 08-reversed-minutes |
| `tests/test_errors.py::test_game_logs_playstyle_failures_use_the_separate_query_names` | 09-playstyle-min, 10-playstyle-max |
| `tests/test_errors.py::test_game_logs_scalar_parse_failures_name_parameter_and_value` | 11-date, 12-location |
| `tests/test_errors.py::test_game_logs_known_failures_survive_other_unknown_failures` | 13-mixed-native |
| `tests/test_errors.py::test_game_log_validation_details_keep_known_and_skip_unknown` | 14-mixed-unknown |
| `tests/test_errors.py::test_game_logs_season_failure_names_parameter_and_value` | 15-season |
| `tests/test_errors.py::test_game_logs_minutes_part_failures_name_the_offending_part` | 16-minutes-part |
| `tests/test_errors.py::test_game_logs_self_filter_failures_name_parameter_and_value` | 17-self-stat |
| `tests/test_errors.py::test_game_logs_game_filter_failure_names_parameter_and_value` | 18-game-filter |
| `tests/test_errors.py::test_game_logs_credential_looking_values_are_redacted_not_echoed` | 19-redaction |
| `tests/test_errors.py::test_game_logs_rejected_values_stay_identifiable` | 20-identifiable |
| `tests/test_errors.py::test_game_logs_details_never_leak_pydantic_context_or_inputs` | 21-context |
| `tests/test_errors.py::test_game_log_validation_details_skip_unknown_internal_failures` | 22-unknown |
| `tests/test_game_logs.py::test_game_log_query_game_filter_keeps_the_http_integer_grammar` | 23-grammar-whole, 24-grammar-fraction |
| `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters` | 25-malformed-cases |
| `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service` | 26-season-rating-cases |

## Mutation runs

| ID | Production defect reintroduced | Mutant result | Restored result | Logs |
|---|---|---|---|---|
| 01-central | Central handler retains generic message | 1 failed | 1 passed | [mutant](resumed-spec-mutation-01-central.log), [restored](resumed-spec-mutation-01-central-restored.log) |
| 02-team-values | Exclude honored team names from unsupported entries | 1 failed | 1 passed | [mutant](resumed-spec-mutation-02-team-values.log), [restored](resumed-spec-mutation-02-team-values-restored.log) |
| 03-vocabulary | Publish authoritative canonical vocabulary | 1 failed | 1 passed | [mutant](resumed-spec-mutation-03-vocabulary.log), [restored](resumed-spec-mutation-03-vocabulary-restored.log) |
| 04-aliases | Publish accepted legacy aliases | 1 failed | 1 passed | [mutant](resumed-spec-mutation-04-aliases.log), [restored](resumed-spec-mutation-04-aliases-restored.log) |
| 05-opponent | Typed opponent rejection identifies submitted value | 1 failed | 1 passed | [mutant](resumed-spec-mutation-05-opponent.log), [restored](resumed-spec-mutation-05-opponent-restored.log) |
| 06-all-ranks | Report all unusable ranks | 1 failed | 1 passed | [mutant](resumed-spec-mutation-06-all-ranks.log), [restored](resumed-spec-mutation-06-all-ranks-restored.log) |
| 07-alignment | Alignment refusal carries submitted ranks | 1 failed | 1 passed | [mutant](resumed-spec-mutation-07-alignment.log), [restored](resumed-spec-mutation-07-alignment-restored.log) |
| 08-reversed-minutes | Reversed minutes refusal carries submitted pair | 1 failed | 1 passed | [mutant](resumed-spec-mutation-08-reversed-minutes.log), [restored](resumed-spec-mutation-08-reversed-minutes-restored.log) |
| 09-playstyle-min | Low rating parse refusal names HTTP minimum | 1 failed | 1 passed | [mutant](resumed-spec-mutation-09-playstyle-min.log), [restored](resumed-spec-mutation-09-playstyle-min-restored.log) |
| 10-playstyle-max | High rating nonfinite refusal names HTTP maximum | 1 failed | 1 passed | [mutant](resumed-spec-mutation-10-playstyle-max.log), [restored](resumed-spec-mutation-10-playstyle-max-restored.log) |
| 11-date | Native date parse failure retains identifying facts | 1 failed | 1 passed | [mutant](resumed-spec-mutation-11-date.log), [restored](resumed-spec-mutation-11-date-restored.log) |
| 12-location | Native location parse failure retains identifying facts | 1 failed | 1 passed | [mutant](resumed-spec-mutation-12-location.log), [restored](resumed-spec-mutation-12-location-restored.log) |
| 13-mixed-native | One native failure must not blank typed team facts | 1 failed | 1 passed | [mutant](resumed-spec-mutation-13-mixed-native.log), [restored](resumed-spec-mutation-13-mixed-native-restored.log) |
| 14-mixed-unknown | Unknown failure must not blank known facts | 1 failed | 1 passed | [mutant](resumed-spec-mutation-14-mixed-unknown.log), [restored](resumed-spec-mutation-14-mixed-unknown-restored.log) |
| 15-season | Season parse failure names submitted value | 1 failed | 1 passed | [mutant](resumed-spec-mutation-15-season.log), [restored](resumed-spec-mutation-15-season-restored.log) |
| 16-minutes-part | Minutes failure excludes valid bound | 1 failed | 1 passed | [mutant](resumed-spec-mutation-16-minutes-part.log), [restored](resumed-spec-mutation-16-minutes-part-restored.log) |
| 17-self-stat | Unsupported self stat remains actionable | 1 failed | 1 passed | [mutant](resumed-spec-mutation-17-self-stat.log), [restored](resumed-spec-mutation-17-self-stat-restored.log) |
| 18-game-filter | Native game count failure retains identifying facts | 1 failed | 1 passed | [mutant](resumed-spec-mutation-18-game-filter.log), [restored](resumed-spec-mutation-18-game-filter-restored.log) |
| 19-redaction | Credential values must not echo | 1 failed | 1 passed | [mutant](resumed-spec-mutation-19-redaction.log), [restored](resumed-spec-mutation-19-redaction-restored.log) |
| 20-identifiable | Ordinary rejected values must remain identifiable | 1 failed | 1 passed | [mutant](resumed-spec-mutation-20-identifiable.log), [restored](resumed-spec-mutation-20-identifiable-restored.log) |
| 21-context | Never expose Pydantic diagnostics | 1 failed | 1 passed | [mutant](resumed-spec-mutation-21-context.log), [restored](resumed-spec-mutation-21-context-restored.log) |
| 22-unknown | Unknown internal failures have no details | 1 failed | 1 passed | [mutant](resumed-spec-mutation-22-unknown.log), [restored](resumed-spec-mutation-22-unknown-restored.log) |
| 23-grammar-whole | Preserve whole-number string grammar | 1 failed | 1 passed | [mutant](resumed-spec-mutation-23-grammar-whole.log), [restored](resumed-spec-mutation-23-grammar-whole-restored.log) |
| 24-grammar-fraction | Reject fractional inputs instead of truncating | 1 failed | 1 passed | [mutant](resumed-spec-mutation-24-grammar-fraction.log), [restored](resumed-spec-mutation-24-grammar-fraction-restored.log) |
| 25-malformed-cases | Every changed malformed-filter case catches absent actionable details | 7 failed | 7 passed | [mutant](resumed-spec-mutation-25-malformed-cases.log), [restored](resumed-spec-mutation-25-malformed-cases-restored.log) |
| 26-season-rating-cases | Every changed season/rating case catches absent actionable details | 6 failed | 6 passed | [mutant](resumed-spec-mutation-26-season-rating-cases.log), [restored](resumed-spec-mutation-26-season-rating-cases-restored.log) |
| 27-length-bound | Coverage probe: removing published-value bound | 125 passed | 125 passed | [mutant](resumed-spec-mutation-27-length-bound.log), [restored](resumed-spec-mutation-27-length-bound-restored.log) |
| 28-self-numeric | Coverage probe: reverting self numeric typed failures | 125 passed | 125 passed | [mutant](resumed-spec-mutation-28-self-numeric.log), [restored](resumed-spec-mutation-28-self-numeric-restored.log) |
| 29-lost-code-assertion | Coverage probe: malformed and season/rating groups still require invalid_input code | 13 passed | 13 passed | [mutant](resumed-spec-mutation-29-lost-code-assertion.log), [restored](resumed-spec-mutation-29-lost-code-assertion-restored.log) |

## Exact mutations and per-case outcomes

### 01-central

Central handler retains generic message

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
             public_details=_game_log_validation_details(error, filters),
         ) from error
```

Mutated run:
- `tests.test_errors::test_game_logs_invalid_input_uses_central_handler` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 02-team-values

Exclude honored team names from unsupported entries

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -520,7 +520,7 @@
         if unsupported:
             raise GameLogFilterError(
                 "teams_against",
-                tuple(unsupported),
+                tuple(value),
                 f"teams_against contains unsupported filters: {unsupported}. "
                 "Supported filters are: "
                 + ", ".join(SUPPORTED_TEAM_FILTERS),
```

Mutated run:
- `tests.test_errors::test_game_logs_rejected_teams_against_names_parameter_and_values` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 03-vocabulary

Publish authoritative canonical vocabulary

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -526,7 +526,7 @@
                 + ", ".join(SUPPORTED_TEAM_FILTERS),
                 # The canonical vocabulary travels with the refusal, so a
                 # caller never needs a copy of these constants to recover.
-                supported_values=tuple(SUPPORTED_TEAM_FILTERS),
+                supported_values=(),
                 supported_aliases=tuple(TEAM_FILTER_ALIASES),
             )
         return value
```

Mutated run:
- `tests.test_errors::test_game_logs_teams_against_rejection_carries_the_canonical_vocabulary` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 04-aliases

Publish accepted legacy aliases

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -527,7 +527,7 @@
                 # The canonical vocabulary travels with the refusal, so a
                 # caller never needs a copy of these constants to recover.
                 supported_values=tuple(SUPPORTED_TEAM_FILTERS),
-                supported_aliases=tuple(TEAM_FILTER_ALIASES),
+                supported_aliases=(),
             )
         return value
```

Mutated run:
- `tests.test_errors::test_game_logs_teams_against_rejection_carries_the_canonical_vocabulary` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 05-opponent

Typed opponent rejection identifies submitted value

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -434,11 +434,7 @@
             return None
         tricode = value.strip().upper()
         if tricode not in NBA_TEAM_TRICODES:
-            raise GameLogFilterError(
-                "opponent_tricode",
-                (value,),
-                f"opponent_tricode {value!r} is not an NBA team tricode",
-            )
+            raise ValueError(f"opponent_tricode {value!r} is not an NBA team tricode")
         return tricode
 
     @field_validator("playstyle_range", mode="before")
```

Mutated run:
- `tests.test_errors::test_game_logs_rejected_opponent_tricode_names_parameter_and_value` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 06-all-ranks

Report all unusable ranks

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -413,7 +413,7 @@
         if invalid:
             raise GameLogFilterError(
                 "rank_filter",
-                tuple(invalid),
+                tuple(invalid[:1]),
                 f"rank_filter contains invalid entries: {invalid!r}",
             )
         return ranks
```

Mutated run:
- `tests.test_errors::test_game_logs_rank_filter_rejection_reports_every_unusable_entry` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 07-alignment

Alignment refusal carries submitted ranks

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -534,11 +534,7 @@
     @model_validator(mode="after")
     def _check_rank_alignment(self) -> "GameLogQuery":
         if self.teams_against and len(self.teams_against) != len(self.rank_filter):
-            raise GameLogFilterError(
-                "rank_filter",
-                tuple(self.rank_filter),
-                "rank_filter must contain one rank per teams_against filter",
-            )
+            raise ValueError("rank_filter must contain one rank per teams_against filter")
         if self.minutes_filter[0] > self.minutes_filter[1]:
             raise GameLogFilterError(
                 "minutes_filter",
```

Mutated run:
- `tests.test_errors::test_game_logs_model_level_alignment_failure_names_submitted_values` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 08-reversed-minutes

Reversed minutes refusal carries submitted pair

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -540,11 +540,7 @@
                 "rank_filter must contain one rank per teams_against filter",
             )
         if self.minutes_filter[0] > self.minutes_filter[1]:
-            raise GameLogFilterError(
-                "minutes_filter",
-                (f"{self.minutes_filter[0]},{self.minutes_filter[1]}",),
-                "minutes_filter min must not exceed minutes_filter max",
-            )
+            raise ValueError("minutes_filter min must not exceed minutes_filter max")
         if self.playstyle_range[0] > self.playstyle_range[1]:
             raise GameLogFilterError(
                 "playstyle_RTG_max",
```

Mutated run:
- `tests.test_errors::test_game_logs_range_failures_name_their_submitted_values` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 09-playstyle-min

Low rating parse refusal names HTTP minimum

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -453,11 +453,7 @@
         try:
             low = float(value[0])
         except (TypeError, ValueError) as error:
-            raise GameLogFilterError(
-                "playstyle_RTG_min",
-                (value[0],),
-                "playstyle_RTG_min must be a finite number",
-            ) from error
+            raise ValueError("playstyle_RTG_min must be a finite number") from error
         try:
             high = float(value[1])
         except (TypeError, ValueError) as error:
@@ -467,11 +463,7 @@
                 "playstyle_RTG_max must be a finite number",
             ) from error
         if not isfinite(low):
-            raise GameLogFilterError(
-                "playstyle_RTG_min",
-                (value[0],),
-                "playstyle_RTG_min must be a finite number",
-            )
+            raise ValueError("playstyle_RTG_min must be a finite number")
         if not isfinite(high):
             raise GameLogFilterError(
                 "playstyle_RTG_max",
```

Mutated run:
- `tests.test_errors::test_game_logs_playstyle_failures_use_the_separate_query_names` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 10-playstyle-max

High rating nonfinite refusal names HTTP maximum

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -461,11 +461,7 @@
         try:
             high = float(value[1])
         except (TypeError, ValueError) as error:
-            raise GameLogFilterError(
-                "playstyle_RTG_max",
-                (value[1],),
-                "playstyle_RTG_max must be a finite number",
-            ) from error
+            raise ValueError("playstyle_RTG_max must be a finite number") from error
         if not isfinite(low):
             raise GameLogFilterError(
                 "playstyle_RTG_min",
@@ -473,11 +469,7 @@
                 "playstyle_RTG_min must be a finite number",
             )
         if not isfinite(high):
-            raise GameLogFilterError(
-                "playstyle_RTG_max",
-                (value[1],),
-                "playstyle_RTG_max must be a finite number",
-            )
+            raise ValueError("playstyle_RTG_max must be a finite number")
         return (low, high)
 
     @field_validator("self_filters", mode="before")
```

Mutated run:
- `tests.test_errors::test_game_logs_playstyle_failures_use_the_separate_query_names` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 11-date

Native date parse failure retains identifying facts

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -139,7 +139,7 @@
 #: rejection at the HTTP seam, the original submitted value in hand. Their
 #: accepted grammar is pydantic's, so acceptance is untouched.
 _UNTYPED_PARAMETER_NAMES = frozenset(
-    {"date_filter", "location_filter", "game_filter"}
+    {'location_filter', 'game_filter'}
 )
```

Mutated run:
- `tests.test_errors::test_game_logs_scalar_parse_failures_name_parameter_and_value` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 12-location

Native location parse failure retains identifying facts

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -139,7 +139,7 @@
 #: rejection at the HTTP seam, the original submitted value in hand. Their
 #: accepted grammar is pydantic's, so acceptance is untouched.
 _UNTYPED_PARAMETER_NAMES = frozenset(
-    {"date_filter", "location_filter", "game_filter"}
+    {'game_filter', 'date_filter'}
 )
```

Mutated run:
- `tests.test_errors::test_game_logs_scalar_parse_failures_name_parameter_and_value` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 13-mixed-native

One native failure must not blank typed team facts

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -194,6 +194,8 @@
     failed_filters = []
     for validation_error in error.errors():
         cause = (validation_error.get("ctx") or {}).get("error")
+        if not isinstance(cause, GameLogFilterError):
+            return None
         facts = _game_log_rejected_filter(cause, validation_error, filters)
         if facts is not None:
             failed_filters.append(facts)
```

Mutated run:
- `tests.test_errors::test_game_logs_known_failures_survive_other_unknown_failures` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 14-mixed-unknown

Unknown failure must not blank known facts

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -195,6 +195,8 @@
     for validation_error in error.errors():
         cause = (validation_error.get("ctx") or {}).get("error")
         facts = _game_log_rejected_filter(cause, validation_error, filters)
+        if facts is None:
+            return None
         if facts is not None:
             failed_filters.append(facts)
     if not failed_filters:
```

Mutated run:
- `tests.test_errors::test_game_log_validation_details_keep_known_and_skip_unknown` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 15-season

Season parse failure names submitted value

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -328,28 +328,16 @@
         """Require one canonical NBA season label (for example, ``2024-25``)."""
 
         if not isinstance(value, str):
-            raise GameLogFilterError(
-                "season_filter",
-                (value,),
-                "season_filter must be a string in YYYY-YY format",
-            )
+            raise ValueError("season_filter must be a string in YYYY-YY format")
         season = value.strip()
         match = re.fullmatch(r"([0-9]{4})-([0-9]{2})", season)
         if match is None:
-            raise GameLogFilterError(
-                "season_filter",
-                (value,),
-                "season_filter must use YYYY-YY format",
-            )
+            raise ValueError("season_filter must use YYYY-YY format")
         start_year = int(match.group(1))
         expected_suffix = f"{(start_year + 1) % 100:02d}"
         if match.group(2) != expected_suffix:
-            raise GameLogFilterError(
-                "season_filter",
-                (value,),
-                "season_filter must end with the following calendar year's "
-                "final two digits",
-            )
+            raise ValueError("season_filter must end with the following calendar year's "
+                "final two digits")
         return season
 
     @field_validator("minutes_filter", mode="before")
```

Mutated run:
- `tests.test_errors::test_game_logs_season_failure_names_parameter_and_value` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 16-minutes-part

Minutes failure excludes valid bound

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -381,7 +381,7 @@
         except (TypeError, ValueError) as error:
             raise GameLogFilterError(
                 "minutes_filter",
-                (value[0],),
+                (f"{value[0]},{value[1]}",),
                 "minutes_filter values must be integers",
             ) from error
         try:
```

Mutated run:
- `tests.test_errors::test_game_logs_minutes_part_failures_name_the_offending_part` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 17-self-stat

Unsupported self stat remains actionable

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -198,18 +198,10 @@
     def normalize_stat(cls, value: Any) -> str:
         stat = str(value).strip().upper()
         if not stat:
-            raise GameLogFilterError(
-                "self_filters[]",
-                (value,),
-                "self_filter stat must not be empty",
-            )
+            raise ValueError("self_filter stat must not be empty")
         if stat not in SUPPORTED_SELF_FILTER_STATS:
-            raise GameLogFilterError(
-                f"self_filters[{stat}]",
-                (value,),
-                f"self_filter contains unsupported stat {stat!r}. Supported stats are: "
-                + ", ".join(SUPPORTED_SELF_FILTER_STATS),
-            )
+            raise ValueError(f"self_filter contains unsupported stat {stat!r}. Supported stats are: "
+                + ", ".join(SUPPORTED_SELF_FILTER_STATS))
         return stat
 
     @field_validator("value", "value2", mode="before")
```

Mutated run:
- `tests.test_errors::test_game_logs_self_filter_failures_name_parameter_and_value` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 18-game-filter

Native game count failure retains identifying facts

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -139,7 +139,7 @@
 #: rejection at the HTTP seam, the original submitted value in hand. Their
 #: accepted grammar is pydantic's, so acceptance is untouched.
 _UNTYPED_PARAMETER_NAMES = frozenset(
-    {"date_filter", "location_filter", "game_filter"}
+    {'location_filter', 'date_filter'}
 )
```

Mutated run:
- `tests.test_errors::test_game_logs_game_filter_failure_names_parameter_and_value` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 19-redaction

Credential values must not echo

```diff
--- app/errors.py (candidate)
+++ app/errors.py (mutant)
@@ -123,7 +123,7 @@
     is stripped here too, and the value is length-bounded.
     """
 
-    sanitized = _sanitize_diagnostic_detail(value)
+    sanitized = str(value)
     if sanitized is None:
         return ""
     return sanitized[:_PUBLIC_VALUE_MAX_LENGTH]
```

Mutated run:
- `tests.test_errors::test_game_logs_credential_looking_values_are_redacted_not_echoed` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 20-identifiable

Ordinary rejected values must remain identifiable

```diff
--- app/errors.py (candidate)
+++ app/errors.py (mutant)
@@ -126,7 +126,7 @@
     sanitized = _sanitize_diagnostic_detail(value)
     if sanitized is None:
         return ""
-    return sanitized[:_PUBLIC_VALUE_MAX_LENGTH]
+    return "[REDACTED]"
 
 
 def _log_application_error(
```

Mutated run:
- `tests.test_errors::test_game_logs_rejected_values_stay_identifiable` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 21-context

Never expose Pydantic diagnostics

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -199,7 +199,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": failed_filters, "diagnostic": str(error)}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

Mutated run:
- `tests.test_errors::test_game_logs_details_never_leak_pydantic_context_or_inputs` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 22-unknown

Unknown internal failures have no details

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -198,7 +198,7 @@
         if facts is not None:
             failed_filters.append(facts)
     if not failed_filters:
-        return None
+        return {"filters": [], "diagnostic": str(error)}
     return {"filters": failed_filters}
```

Mutated run:
- `tests.test_errors::test_game_log_validation_details_skip_unknown_internal_failures` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 23-grammar-whole

Preserve whole-number string grammar

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -322,6 +322,11 @@
     # can contain more than one constraint for the same stat.
     self_filters: list[SelfFilter] = Field(default_factory=list)
 
+    @field_validator("game_filter", mode="before")
+    @classmethod
+    def mutation_parse_game_filter(cls, value):
+        return None if value is None else int(value)
+
     @field_validator("season_filter", mode="before")
     @classmethod
     def normalize_season_filter(cls, value: Any) -> str:
```

Mutated run:
- `tests.test_game_logs::test_game_log_query_game_filter_keeps_the_http_integer_grammar` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 24-grammar-fraction

Reject fractional inputs instead of truncating

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -322,6 +322,11 @@
     # can contain more than one constraint for the same stat.
     self_filters: list[SelfFilter] = Field(default_factory=list)
 
+    @field_validator("game_filter", mode="before")
+    @classmethod
+    def mutation_parse_game_filter(cls, value):
+        return None if value is None else int(float(value))
+
     @field_validator("season_filter", mode="before")
     @classmethod
     def normalize_season_filter(cls, value: Any) -> str:
```

Mutated run:
- `tests.test_game_logs::test_game_log_query_game_filter_keeps_the_http_integer_grammar` — failed

Restoration: exit 0; all 1 selected cases passed after byte restoration.

### 25-malformed-cases

Every changed malformed-filter case catches absent actionable details

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -131,7 +131,7 @@
         raise InvalidInputError(
             "One or more game log filters are invalid.",
             detail=error,
-            public_details=_game_log_validation_details(error, filters),
+            public_details=None,
         ) from error
```

Mutated run:
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_details0]` — failed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_details1]` — failed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_details2]` — failed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_details3]` — failed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_details4]` — failed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_details5]` — failed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_details6]` — failed

Restoration: exit 0; all 7 selected cases passed after byte restoration.

### 26-season-rating-cases

Every changed season/rating case catches absent actionable details

```diff
--- app/routes/game_routes.py (candidate)
+++ app/routes/game_routes.py (mutant)
@@ -131,7 +131,7 @@
         raise InvalidInputError(
             "One or more game log filters are invalid.",
             detail=error,
-            public_details=_game_log_validation_details(error, filters),
+            public_details=None,
         ) from error
```

Mutated run:
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_details0]` — failed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_details1]` — failed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_details2]` — failed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_details3]` — failed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_details4]` — failed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_details5]` — failed

Restoration: exit 0; all 6 selected cases passed after byte restoration.

### 27-length-bound

Coverage probe: removing published-value bound

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

Mutated run:
- All 125 cases passed; see linked log for full run.

Restoration: exit 0; all 125 selected cases passed after byte restoration.

### 28-self-numeric

Coverage probe: reverting self numeric typed failures

```diff
--- app/models/game_logs.py (candidate)
+++ app/models/game_logs.py (mutant)
@@ -221,17 +221,9 @@
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
-            raise GameLogFilterError(
-                parameter,
-                (value,),
-                "self_filter values must be finite numbers",
-            )
+            raise ValueError("self_filter values must be finite numbers")
         return number
 
     @model_validator(mode="after")
```

Mutated run:
- All 125 cases passed; see linked log for full run.

Restoration: exit 0; all 125 selected cases passed after byte restoration.

### 29-lost-code-assertion

Coverage probe: malformed and season/rating groups still require invalid_input code

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

Mutated run:
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&minutes_filter=not-a-range-expected_details0]` — passed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&location_filter=home-expected_details1]` — passed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&teams_against[]=OPP_PTS-expected_details2]` — passed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=XXX-expected_details3]` — passed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&opponent_tricode=-expected_details4]` — passed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&date_filter=not-a-date-expected_details5]` — passed
- `tests.test_game_logs::test_route_returns_400_for_malformed_filters[player_name=LeBron%20James&game_filter=0-expected_details6]` — passed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=-expected_details0]` — passed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=potato-expected_details1]` — passed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[season_filter=2024-27-expected_details2]` — passed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=nan-expected_details3]` — passed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_max=inf-expected_details4]` — passed
- `tests.test_game_logs::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service[playstyle_RTG_min=-inf-expected_details5]` — passed

Restoration: exit 0; all 13 selected cases passed after byte restoration.

## Final verification

- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/.review-spec:$PWD" PYTEST_PLUGINS=offline_guard ./scripts/check.sh` — **exit 0**. Ruff passed; **4866 tests and 62 subtests passed**, 161 warnings, **84.88% branch-aware coverage** (62% floor). Fresh temporary database migration applied 56 migrations; the second migration run was a no-op; demo database validation passed.
- Full gate log: [resumed-spec-mutation-check.log](resumed-spec-mutation-check.log).
- No implementation fixes, external writes, reviewer spawning, or live provider calls were performed. Only the explicitly requested review report/log artifacts persist outside the isolated checkout.
- All targeted restored tests passed after each individual mutation. Both requested files also passed together after each full-file coverage experiment; the final full repository gate ran against the restored candidate.
- The initial six candidate files remain byte-identical. Final binary diff SHA-256: `39b0f28f726d01a66d9e24fda53d93cc0cd555cd3d9aa0cdedad1b84ca764391`, identical to the initial value.
- Temporary observation tests, mutation harness, backups and offline guard were removed after verification. Final `git status --porcelain=v1` lists exactly the six initial candidate modifications, with no untracked review files. No review-owned QA service remains running.
- These are spec-axis findings only. The successful gate demonstrates existing automated checks pass; it does not resolve the response defects reproduced above.

Final preservation evidence: [resumed-spec-mutation-preservation.log](resumed-spec-mutation-preservation.log).
