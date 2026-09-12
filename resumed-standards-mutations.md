# Independent standards mutation review — backend issue 145

## Candidate and preservation

- Checkout: `/private/tmp/statsplus-145/resumed-review-standards`.
- HEAD and base: `e1db9337bf9fdfeac980a77841578e6e13440b75`; the complete candidate is the six modified files in `git diff e1db9337bf9fdfeac980a77841578e6e13440b75`. No candidate commits exist above the base in this isolated checkout.
- Exact diff SHA-256 before and after: `39b0f28f726d01a66d9e24fda53d93cc0cd555cd3d9aa0cdedad1b84ca764391`.
- Original diff: [resumed-standards-initial.diff](resumed-standards-initial.diff). Per-file hashes: [resumed-standards-initial-sha256.json](resumed-standards-initial-sha256.json).
- Mutations touched only production files in this isolated checkout. Each run restored their original bytes in a `finally` block, verified all six candidate files and the complete diff, and then reran the same selected tests on restored production code. No implementation fixes, commits, network provider calls, or external application writes.
- Runtime: Python 3.11.9. The existing `.venv` symlink points at the protected implementation checkout. It was used read-only: `-B`, `PYTHONDONTWRITEBYTECODE=1`, and a checkout-local `PYTHONPYCACHEPREFIX`. Bootstrap was not run because it installs into that shared environment. No dependency or `.env` writes were performed.
- Original fixtures inject the application dependency graph, mocked services, and local auth bypass. Mutation runs additionally loaded a temporary autouse guard refusing AF_INET/AF_INET6 socket connections. No reviewer subprocesses or delegated agents were used.
- Standards read: `AGENTS.md`, `docs/ARCHITECTURE.md` (especially runtime/error and game-log seams), `docs/API_DOCUMENTATION.md` (error and game-log contracts), `CONTRIBUTING.md`, and the supplied review procedure and unedited issue spec.

## Results

- Candidate baseline: `125 passed in 1.71s`.
- Guarded baseline: `125 passed in 2.95s`.
- 22 new/substantively changed test functions (33 collected cases), including all 7 malformed-filter parameter cases and all 6 season/nonfinite parameter cases. Every primary targeted test is recorded as FAILED in its mutant log; all 13 parameter cases fail individually.
- 30 independently applied mutants: 26 killed, 4 survived their selected tests. Every restored run passed.
- All primary per-test mutants 01–24 were killed. Extra mutants probe the breadth of security, bounds, and existing-contract assertions; surviving extras are recorded below rather than counted as proof of coverage.

## Material standards findings

1. **Unredacted caller-controlled parameter names** (`app/routes/game_routes.py:155`, constructed at `app/models/game_logs.py:208` and `:295`). The new publisher sanitizes values but copies the parameter verbatim. A request with `self_filters[token=review-synthetic-marker]=bad` returns that complete credential-shaped string in `details.filters[0].parameter`, although the existing sanitizer removes the synthetic marker. An uppercase credential-shaped stat with a well-formed range also survives in the parameter. A 600-character stat yields a parameter longer than 200 characters. This violates `docs/API_DOCUMENTATION.md:1292–1294`: “anything credential-shaped is redacted the same way the diagnostics are,” and the supplied issue’s “No credential, provider, or internal diagnostic material becomes reachable through the new field.” All markers here are synthetic.
2. **The new security tests do not establish their stated property** (`tests/test_errors.py:497–517`, and the added detail assertions throughout both files). Mutant 29 publishes the validation-library error type, location, and validation input (redacted), yet all 125 tests pass; mutant 26 removes the length cap and all 125 pass. The no-leak test checks only six substrings, while the detail helpers ignore additional fields. `AGENTS.md:29` requires “a test that fails for the behavior being changed”; `docs/API_DOCUMENTATION.md:1292–1293` promises “No validation-library context, raw input dump, or provider material,” and `:1279` promises length-bounded values. Tests must cover the allowed payload shape and boundary behavior to establish those guarantees.

## Surviving mutants and coverage limits

- **26-no-value-bound:** removing the 200-character limit passes both full files. No added test exercises an overlength value or redaction crossing the truncation boundary.
- **27-route-error-code:** all 13 modified parameterized route cases pass with `error.code="wrong_code"`. Their previous exact-envelope assertions included `code="invalid_input"`; the replacement assertions dropped it. The central-handler test still catches the same global route mutant (30), so this is a reduction in the parameterized cases’ coverage, not an undetected global code regression. Citation: supplied issue, “existing error-contract tests are extended rather than replaced.”
- **28-nonnumeric-grammar:** the new integer grammar test passes when alphabetic game counts are accepted as 3. Its docstring claims non-numeric rejection, but its inputs cover only whole numbers and fractional numbers.
- **29-sanitized-validation-input-dump:** all 125 tests pass with extra `validation` entries holding `type`, `loc`, and the full input after credential redaction/length truncation. These are validation diagnostics outside the allowed identifying facts. This is not equivalent to the candidate.
- **25-raw-input-dump:** the dedicated no-context/no-input test passes despite raw input publication. The full pair of files still kills the mutant through the separate credential-redaction test. Mutant 29 demonstrates the remaining gap when the dump respects credential redaction.
- Credential-shaped *parameter names* and their length are absent from the submitted tests; the candidate reproduction demonstrates the actual escape rather than only a hypothetical survivor.
- Additional review probes (no implementation changes) show uncovered self-filter cases: reversed `self_filters[PTS]=10,0` loses the zero upper bound through `filter(None, ...)` at `app/models/game_logs.py:244`; `self_filters[BOGUS]=a,2` adds a rejection named `self_filters[None]` because `ValidationInfo.data` lacks the failed stat at `:220`. The submitted new self-filter test exercises only `BOGUS=1,2`. See the reproduction log for exact responses. These are recorded as coverage gaps; this review does not replace the separate Spec review.
- Multi-invalid-bound example `minutes_filter=a,b` reports only `a`; the new part-identification test exercises one invalid bound only.

## Every changed test: mutation coverage

| Test | Primary killing mutant(s) |
| --- | --- |
| `tests/test_errors.py::test_game_logs_invalid_input_uses_central_handler` | 01-central-message |
| `tests/test_errors.py::test_game_logs_rejected_teams_against_names_parameter_and_values` | 02-teams-values |
| `tests/test_errors.py::test_game_logs_teams_against_rejection_carries_the_canonical_vocabulary` | 03-canonical-vocabulary, 04-legacy-aliases |
| `tests/test_errors.py::test_game_logs_rejected_opponent_tricode_names_parameter_and_value` | 05-opponent-details |
| `tests/test_errors.py::test_game_logs_rank_filter_rejection_reports_every_unusable_entry` | 06-ranks-first-only |
| `tests/test_errors.py::test_game_logs_model_level_alignment_failure_names_submitted_values` | 07-alignment-details |
| `tests/test_errors.py::test_game_logs_range_failures_name_their_submitted_values` | 07-alignment-details |
| `tests/test_errors.py::test_game_logs_playstyle_failures_use_the_separate_query_names` | 08-playstyle-min, 09-playstyle-max |
| `tests/test_errors.py::test_game_logs_scalar_parse_failures_name_parameter_and_value` | 10-scalar-details |
| `tests/test_errors.py::test_game_logs_known_failures_survive_other_unknown_failures` | 10-scalar-details |
| `tests/test_errors.py::test_game_log_validation_details_keep_known_and_skip_unknown` | 11-unknown-blanks-known |
| `tests/test_errors.py::test_game_logs_season_failure_names_parameter_and_value` | 12-season-details |
| `tests/test_errors.py::test_game_logs_minutes_part_failures_name_the_offending_part` | 13-minutes-part |
| `tests/test_errors.py::test_game_logs_self_filter_failures_name_parameter_and_value` | 14-self-stat |
| `tests/test_errors.py::test_game_logs_game_filter_failure_names_parameter_and_value` | 15-game-details |
| `tests/test_errors.py::test_game_logs_credential_looking_values_are_redacted_not_echoed` | 16-redaction |
| `tests/test_errors.py::test_game_logs_rejected_values_stay_identifiable` | 17-over-redaction |
| `tests/test_errors.py::test_game_logs_details_never_leak_pydantic_context_or_inputs` | 18-validation-dump |
| `tests/test_errors.py::test_game_log_validation_details_skip_unknown_internal_failures` | 19-unknown-details |
| `tests/test_game_logs.py::test_game_log_query_game_filter_keeps_the_http_integer_grammar` | 20-integer-string-grammar, 21-fraction-string-grammar, 22-fraction-number-grammar |
| `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters` | 23-http-malformed-details |
| `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service` | 24-season-nonfinite-details |

## Detailed mutations and logs

### 01-central-message

Replace the stable generic game-log error message with a different message.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-01-central-message.log) · [Restored log](resumed-standards-mutation-01-central-message-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_invalid_input_uses_central_handler`

Observed mutant summary:

```text
============================== 1 failed in 1.25s ===============================
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
             public_details=_game_log_validation_details(error, filters),
         ) from error
```

### 02-teams-values

Report accepted opponents along with rejected ones.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-02-teams-values.log) · [Restored log](resumed-standards-mutation-02-teams-values-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_rejected_teams_against_names_parameter_and_values`

Observed mutant summary:

```text
============================== 1 failed in 1.21s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
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

### 03-canonical-vocabulary

Omit the first canonical supported entry.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-03-canonical-vocabulary.log) · [Restored log](resumed-standards-mutation-03-canonical-vocabulary-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_teams_against_rejection_carries_the_canonical_vocabulary`

Observed mutant summary:

```text
============================== 1 failed in 1.24s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -526,7 +526,7 @@
                 + ", ".join(SUPPORTED_TEAM_FILTERS),
                 # The canonical vocabulary travels with the refusal, so a
                 # caller never needs a copy of these constants to recover.
-                supported_values=tuple(SUPPORTED_TEAM_FILTERS),
+                supported_values=tuple(SUPPORTED_TEAM_FILTERS[1:]),
                 supported_aliases=tuple(TEAM_FILTER_ALIASES),
             )
         return value
```

### 04-legacy-aliases

Omit the accepted legacy alias vocabulary.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-04-legacy-aliases.log) · [Restored log](resumed-standards-mutation-04-legacy-aliases-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_teams_against_rejection_carries_the_canonical_vocabulary`

Observed mutant summary:

```text
============================== 1 failed in 1.22s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -527,7 +527,7 @@
                 # The canonical vocabulary travels with the refusal, so a
                 # caller never needs a copy of these constants to recover.
                 supported_values=tuple(SUPPORTED_TEAM_FILTERS),
-                supported_aliases=tuple(TEAM_FILTER_ALIASES),
+                supported_aliases=None,
             )
         return value
```

### 05-opponent-details

Restore the base opponent validator: rejection has no actionable cause.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-05-opponent-details.log) · [Restored log](resumed-standards-mutation-05-opponent-details-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_rejected_opponent_tricode_names_parameter_and_value`

Observed mutant summary:

```text
============================== 1 failed in 1.23s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -434,10 +434,8 @@
             return None
         tricode = value.strip().upper()
         if tricode not in NBA_TEAM_TRICODES:
-            raise GameLogFilterError(
-                "opponent_tricode",
-                (value,),
-                f"opponent_tricode {value!r} is not an NBA team tricode",
+            raise ValueError(
+                f"opponent_tricode {value!r} is not an NBA team tricode"
             )
         return tricode
```

### 06-ranks-first-only

Stop exposing rejected ranks after the first unusable entry.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-06-ranks-first-only.log) · [Restored log](resumed-standards-mutation-06-ranks-first-only-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_rank_filter_rejection_reports_every_unusable_entry`

Observed mutant summary:

```text
============================== 1 failed in 1.24s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
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

### 07-alignment-details

Restore base model-level validation with no actionable alignment facts.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-07-alignment-details.log) · [Restored log](resumed-standards-mutation-07-alignment-details-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_model_level_alignment_failure_names_submitted_values`
- `tests/test_errors.py::test_game_logs_range_failures_name_their_submitted_values`

Observed mutant summary:

```text
============================== 2 failed in 1.18s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -534,22 +534,14 @@
     @model_validator(mode="after")
     def _check_rank_alignment(self) -> "GameLogQuery":
         if self.teams_against and len(self.teams_against) != len(self.rank_filter):
-            raise GameLogFilterError(
-                "rank_filter",
-                tuple(self.rank_filter),
-                "rank_filter must contain one rank per teams_against filter",
+            raise ValueError(
+                "rank_filter must contain one rank per teams_against filter"
             )
         if self.minutes_filter[0] > self.minutes_filter[1]:
-            raise GameLogFilterError(
-                "minutes_filter",
-                (f"{self.minutes_filter[0]},{self.minutes_filter[1]}",),
-                "minutes_filter min must not exceed minutes_filter max",
-            )
+            raise ValueError("minutes_filter min must not exceed minutes_filter max")
         if self.playstyle_range[0] > self.playstyle_range[1]:
-            raise GameLogFilterError(
-                "playstyle_RTG_max",
-                (_format_rating(self.playstyle_range[1]),),
-                "playstyle_RTG_min must not exceed playstyle_RTG_max",
+            raise ValueError(
+                "playstyle_RTG_min must not exceed playstyle_RTG_max"
             )
         return self
```

### 08-playstyle-min

Lose the HTTP name of the playstyle_RTG_min bound.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-08-playstyle-min.log) · [Restored log](resumed-standards-mutation-08-playstyle-min-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_playstyle_failures_use_the_separate_query_names`

Observed mutant summary:

```text
============================== 1 failed in 1.16s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -152,7 +152,7 @@
 
     if isinstance(cause, GameLogFilterError):
         facts = {
-            "parameter": cause.parameter,
+            "parameter": "playstyle_range" if cause.parameter == "playstyle_RTG_min" else cause.parameter,
             "values": [sanitize_public_value(value) for value in cause.values],
         }
         if cause.supported_values is not None:
```

### 09-playstyle-max

Lose the HTTP name of the playstyle_RTG_max bound.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-09-playstyle-max.log) · [Restored log](resumed-standards-mutation-09-playstyle-max-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_playstyle_failures_use_the_separate_query_names`

Observed mutant summary:

```text
============================== 1 failed in 1.21s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -152,7 +152,7 @@
 
     if isinstance(cause, GameLogFilterError):
         facts = {
-            "parameter": cause.parameter,
+            "parameter": "playstyle_range" if cause.parameter == "playstyle_RTG_max" else cause.parameter,
             "values": [sanitize_public_value(value) for value in cause.values],
         }
         if cause.supported_values is not None:
```

### 10-scalar-details

Omit actionable pydantic-native scalar failures.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-10-scalar-details.log) · [Restored log](resumed-standards-mutation-10-scalar-details-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_scalar_parse_failures_name_parameter_and_value`
- `tests/test_errors.py::test_game_logs_known_failures_survive_other_unknown_failures`

Observed mutant summary:

```text
============================== 2 failed in 1.53s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -168,7 +168,7 @@
     field = validation_error.get("loc", ())
     field = field[0] if field and isinstance(field[0], str) else None
     submitted = filters.get(field) if field in _UNTYPED_PARAMETER_NAMES else None
-    if isinstance(submitted, str):
+    if False:
         return {
             "parameter": field,
             "values": [sanitize_public_value(submitted)],
```

### 11-unknown-blanks-known

Discard known facts when any other failure lacks actionable details.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-11-unknown-blanks-known.log) · [Restored log](resumed-standards-mutation-11-unknown-blanks-known-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_log_validation_details_keep_known_and_skip_unknown`

Observed mutant summary:

```text
============================== 1 failed in 1.63s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -195,8 +195,9 @@
     for validation_error in error.errors():
         cause = (validation_error.get("ctx") or {}).get("error")
         facts = _game_log_rejected_filter(cause, validation_error, filters)
-        if facts is not None:
-            failed_filters.append(facts)
+        if facts is None:
+            return None
+        failed_filters.append(facts)
     if not failed_filters:
         return None
     return {"filters": failed_filters}
```

### 12-season-details

Restore base season validator without identifying facts.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-12-season-details.log) · [Restored log](resumed-standards-mutation-12-season-details-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_season_failure_names_parameter_and_value`

Observed mutant summary:

```text
============================== 1 failed in 2.21s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -328,27 +328,17 @@
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
+            raise ValueError(
                 "season_filter must end with the following calendar year's "
-                "final two digits",
+                "final two digits"
             )
         return season
```

### 13-minutes-part

Echo the entire minute range instead of only its unusable part.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-13-minutes-part.log) · [Restored log](resumed-standards-mutation-13-minutes-part-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_minutes_part_failures_name_the_offending_part`

Observed mutant summary:

```text
============================== 1 failed in 3.39s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -381,7 +381,7 @@
         except (TypeError, ValueError) as error:
             raise GameLogFilterError(
                 "minutes_filter",
-                (value[0],),
+                (",".join(map(str, value)),),
                 "minutes_filter values must be integers",
             ) from error
         try:
```

### 14-self-stat

Restore base self-stat validator without actionable facts.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-14-self-stat.log) · [Restored log](resumed-standards-mutation-14-self-stat-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_self_filter_failures_name_parameter_and_value`

Observed mutant summary:

```text
============================== 1 failed in 4.62s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -198,17 +198,11 @@
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
+            raise ValueError(
                 f"self_filter contains unsupported stat {stat!r}. Supported stats are: "
-                + ", ".join(SUPPORTED_SELF_FILTER_STATS),
+                + ", ".join(SUPPORTED_SELF_FILTER_STATS)
             )
         return stat
```

### 15-game-details

Exclude game_filter from native-parser detail translation.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-15-game-details.log) · [Restored log](resumed-standards-mutation-15-game-details-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_game_filter_failure_names_parameter_and_value`

Observed mutant summary:

```text
============================== 1 failed in 4.54s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -139,7 +139,7 @@
 #: rejection at the HTTP seam, the original submitted value in hand. Their
 #: accepted grammar is pydantic's, so acceptance is untouched.
 _UNTYPED_PARAMETER_NAMES = frozenset(
-    {"date_filter", "location_filter", "game_filter"}
+    {"date_filter", "location_filter"}
 )
```

### 16-redaction

Echo submitted text without diagnostic redaction.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-16-redaction.log) · [Restored log](resumed-standards-mutation-16-redaction-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_credential_looking_values_are_redacted_not_echoed`

Observed mutant summary:

```text
============================== 1 failed in 4.02s ===============================
```

Exact production mutation:

```diff
--- app/errors.py
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

### 17-over-redaction

Hide ordinary rejected inputs under a generic redaction marker.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-17-over-redaction.log) · [Restored log](resumed-standards-mutation-17-over-redaction-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_rejected_values_stay_identifiable`

Observed mutant summary:

```text
============================== 1 failed in 4.40s ===============================
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
+    return "[REDACTED]"
 
 
 def _log_application_error(
```

### 18-validation-dump

Publish the complete validation exception text.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-18-validation-dump.log) · [Restored log](resumed-standards-mutation-18-validation-dump-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_details_never_leak_pydantic_context_or_inputs`

Observed mutant summary:

```text
============================== 1 failed in 4.43s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -199,7 +199,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": failed_filters, "diagnostic": str(error)}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

### 19-unknown-details

Publish an empty details object when no actionable failure is known.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-19-unknown-details.log) · [Restored log](resumed-standards-mutation-19-unknown-details-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_log_validation_details_skip_unknown_internal_failures`

Observed mutant summary:

```text
E    +  where {} = <function _game_log_validation_details at 0x1211f4ea0>(1 validation error for GameLogQuery\nself_filters\n  Value error, self_filters must be a stat range mapping or a list of typed filters [type=value_error, input_value='oops', input_type=str]\n    For further information visit https://errors.pydantic.dev/2.13/v/value_error, filters={})
============================== 1 failed in 3.16s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -198,7 +198,7 @@
         if facts is not None:
             failed_filters.append(facts)
     if not failed_filters:
-        return None
+        return {}
     return {"filters": failed_filters}
```

### 20-integer-string-grammar

Use int() parsing before pydantic, wrongly rejecting whole-number decimal strings.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-20-integer-string-grammar.log) · [Restored log](resumed-standards-mutation-20-integer-string-grammar-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_game_log_query_game_filter_keeps_the_http_integer_grammar`

Observed mutant summary:

```text
============================== 1 failed in 2.59s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -322,6 +322,11 @@
     # can contain more than one constraint for the same stat.
     self_filters: list[SelfFilter] = Field(default_factory=list)
 
+    @field_validator("game_filter", mode="before")
+    @classmethod
+    def review_integer_parse(cls, value):
+        return None if value is None else int(value)
+
     @field_validator("season_filter", mode="before")
     @classmethod
     def normalize_season_filter(cls, value: Any) -> str:
```

### 21-fraction-string-grammar

Truncate fractional input via int(float(value)).

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-21-fraction-string-grammar.log) · [Restored log](resumed-standards-mutation-21-fraction-string-grammar-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_game_log_query_game_filter_keeps_the_http_integer_grammar`

Observed mutant summary:

```text
============================== 1 failed in 2.58s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -322,6 +322,11 @@
     # can contain more than one constraint for the same stat.
     self_filters: list[SelfFilter] = Field(default_factory=list)
 
+    @field_validator("game_filter", mode="before")
+    @classmethod
+    def review_integer_parse(cls, value):
+        return None if value is None else int(float(value))
+
     @field_validator("season_filter", mode="before")
     @classmethod
     def normalize_season_filter(cls, value: Any) -> str:
```

### 22-fraction-number-grammar

Truncate numeric fractional input while leaving string parsing unchanged.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-22-fraction-number-grammar.log) · [Restored log](resumed-standards-mutation-22-fraction-number-grammar-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_game_log_query_game_filter_keeps_the_http_integer_grammar`

Observed mutant summary:

```text
============================== 1 failed in 2.53s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -322,6 +322,11 @@
     # can contain more than one constraint for the same stat.
     self_filters: list[SelfFilter] = Field(default_factory=list)
 
+    @field_validator("game_filter", mode="before")
+    @classmethod
+    def review_integer_parse(cls, value):
+        return int(value) if isinstance(value, float) else value
+
     @field_validator("season_filter", mode="before")
     @classmethod
     def normalize_season_filter(cls, value: Any) -> str:
```

### 23-http-malformed-details

Restore original route behavior: generic message without public_details.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-23-http-malformed-details.log) · [Restored log](resumed-standards-mutation-23-http-malformed-details-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`

Observed mutant summary:

```text
============================== 7 failed in 1.65s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -131,7 +131,7 @@
         raise InvalidInputError(
             "One or more game log filters are invalid.",
             detail=error,
-            public_details=_game_log_validation_details(error, filters),
+            public_details=None,
         ) from error
```

### 24-season-nonfinite-details

Restore original route behavior for malformed season and nonfinite bounds.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-24-season-nonfinite-details.log) · [Restored log](resumed-standards-mutation-24-season-nonfinite-details-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

Observed mutant summary:

```text
============================== 6 failed in 1.51s ===============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -131,7 +131,7 @@
         raise InvalidInputError(
             "One or more game log filters are invalid.",
             detail=error,
-            public_details=_game_log_validation_details(error, filters),
+            public_details=None,
         ) from error
```

### 25-raw-input-dump

Publish raw pydantic inputs under an extra details key, without any validation-library marker.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-25-raw-input-dump.log) · [Restored log](resumed-standards-mutation-25-raw-input-dump-restored.log).

Selected tests:

- `tests/test_errors.py`
- `tests/test_game_logs.py`

Observed mutant summary:

```text
======================== 1 failed, 124 passed in 2.83s =========================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -199,7 +199,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": failed_filters, "input": [item["input"] for item in error.errors()]}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

### 26-no-value-bound

Remove the newly promised public scalar length limit.

- Mutant outcome: **SURVIVED**; exit 0.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-26-no-value-bound.log) · [Restored log](resumed-standards-mutation-26-no-value-bound-restored.log).

Selected tests:

- `tests/test_errors.py`
- `tests/test_game_logs.py`

Observed mutant summary:

```text
============================= 125 passed in 2.64s ==============================
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

### 27-route-error-code

Change the game-log error code without changing status, message, or details.

- Mutant outcome: **SURVIVED**; exit 0.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-27-route-error-code.log) · [Restored log](resumed-standards-mutation-27-route-error-code-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_route_returns_400_for_malformed_filters`
- `tests/test_game_logs.py::test_route_rejects_invalid_season_and_nonfinite_playstyle_before_service`

Observed mutant summary:

```text
============================== 13 passed in 1.77s ==============================
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
             public_details=_game_log_validation_details(error, filters),
```

### 28-nonnumeric-grammar

Accept non-numeric input as the default game count.

- Mutant outcome: **SURVIVED**; exit 0.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-28-nonnumeric-grammar.log) · [Restored log](resumed-standards-mutation-28-nonnumeric-grammar-restored.log).

Selected tests:

- `tests/test_game_logs.py::test_game_log_query_game_filter_keeps_the_http_integer_grammar`

Observed mutant summary:

```text
============================== 1 passed in 1.11s ===============================
```

Exact production mutation:

```diff
--- app/models/game_logs.py
+++ app/models/game_logs.py (mutant)
@@ -322,6 +322,13 @@
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

### 29-sanitized-validation-input-dump

Expose pydantic error type, location, and full validation input (credential-redacted) outside the documented actionable facts.

- Mutant outcome: **SURVIVED**; exit 0.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-29-sanitized-validation-input-dump.log) · [Restored log](resumed-standards-mutation-29-sanitized-validation-input-dump-restored.log).

Selected tests:

- `tests/test_errors.py`
- `tests/test_game_logs.py`

Observed mutant summary:

```text
============================= 125 passed in 2.45s ==============================
```

Exact production mutation:

```diff
--- app/routes/game_routes.py
+++ app/routes/game_routes.py (mutant)
@@ -199,7 +199,7 @@
             failed_filters.append(facts)
     if not failed_filters:
         return None
-    return {"filters": failed_filters}
+    return {"filters": failed_filters, "validation": [{"type": item["type"], "loc": item["loc"], "input": sanitize_public_value(item["input"])} for item in error.errors()]}
 
 
 @game_bp.route('/game_logs', methods=['GET'])
```

### 30-central-error-code

Verify the retained central-handler test rejects the route-only wrong-code mutant that the modified parameterized tests missed.

- Mutant outcome: **KILLED**; exit 1.
- Restoration: byte-for-byte original candidate; restored tests exit 0.
- [Mutant log](resumed-standards-mutation-30-central-error-code.log) · [Restored log](resumed-standards-mutation-30-central-error-code-restored.log).

Selected tests:

- `tests/test_errors.py::test_game_logs_invalid_input_uses_central_handler`

Observed mutant summary:

```text
============================== 1 failed in 1.12s ===============================
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
             public_details=_game_log_validation_details(error, filters),
```

## Reproduction and final verification

- [Candidate-only HTTP reproductions](resumed-standards-mutation-reproductions.log). Temporary probe sources are included in that log and removed from the checkout before the completion gate.
- [Final restored target suite](resumed-standards-mutation-final-restored.log).
- [Repository completion gate](resumed-standards-mutation-completion-gate.log). Exact invocation: `PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$PWD/.review_support/unused_cache" PYTHONPATH="$PWD/.review_support:$PWD" PYTEST_PLUGINS=offline_guard PYTEST_XDIST_AUTO_NUM_WORKERS=4 ./scripts/check.sh`. Worker count capped at four; default exclusions keep live/integration tests out. See the gate log for exact checks and limits.
- [Final preservation check](resumed-standards-mutation-preservation.log).
- Machine-readable mutation records with exact patches and selected commands: [resumed-standards-mutation-results.json](resumed-standards-mutation-results.json).

## Completed gate outcome

`./scripts/check.sh` exited **0** on the byte-restored candidate. Ruff passed. Pytest: **4,866 passed**, **62 subtests passed**, 161 warnings, **84.88% branch coverage** (62% required), 127.54 seconds. All 56 migrations applied to a disposable database; the second application was a no-op at version 56. Demo database validation passed. Live/provider-network and live-Postgres integration tests were excluded by the repository default configuration. Temporary review probes, harness scripts, and the network guard were removed before handoff; no review-owned services were started.
