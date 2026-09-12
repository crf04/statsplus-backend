Four material spec findings:

1. **P1 — Credentials bypass redaction through `parameter`.** [game_routes.py:155](/tmp/statsplus-145/resumed-review-spec/app/routes/game_routes.py:155) publishes caller-controlled self-filter names unsanitized. `self_filters[token=synthetic-secret]=bad-range` echoes that secret verbatim. Violates issue #145: “No credential, provider, or internal diagnostic material becomes reachable through the new field,” and API documentation lines 1292–1294 requiring credential-shaped text to be redacted.

2. **P2 — Numeric failures identify a nonexistent parameter.** [game_logs.py:220](/tmp/statsplus-145/resumed-review-spec/app/models/game_logs.py:220): `self_filters[BOGUS]=a,b` reports numeric failures under `self_filters[None]`. Violates “the parameter that failed and the offending values” and API documentation line 1278: `self_filters[STAT]` must contain “the actual stat.”

3. **P2 — Range failures can blame an unsubmitted default.** [game_logs.py:550](/tmp/statsplus-145/resumed-review-spec/app/models/game_logs.py:550): submitting only `playstyle_RTG_min=201` reports `playstyle_RTG_max` with value `200`. The submitted `201` disappears. Violates the issue’s “which submitted values were unusable” and API documentation lines 1278–1279.

4. **P2 — Zero disappears from rejected self-filter ranges.** [game_logs.py:244](/tmp/statsplus-145/resumed-review-spec/app/models/game_logs.py:244) excludes `value2` by truthiness. `self_filters[PTS]=2,0` returns only `["2.0"]`, losing the zero upper bound. Violates the same submitted-values requirement.

Mutation review covered **all 22 changed test functions / 33 cases**: 26 targeted mutants were caught; three additional mutants survived, exposing gaps in length bounds, numeric self-filter details, and retained error-code assertions. Every restored test run passed.

The completion gate passed: **4,866 tests, 62 subtests, 84.88% coverage**. Candidate files and diff remain byte-for-byte unchanged.

[Detailed mutation report and logs](resumed-spec-mutations.md).