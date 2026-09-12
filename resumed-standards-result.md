Two material standards findings:

- **P1 — Caller-controlled parameter names bypass redaction.** [game_routes.py:155](/tmp/statsplus-145/resumed-review-standards/app/routes/game_routes.py:155) publishes `cause.parameter` unchanged. Reproduced: `self_filters[token=review-synthetic-marker]=bad` echoes the synthetic token in `details.parameter`. This violates [API_DOCUMENTATION.md:1292](/tmp/statsplus-145/resumed-review-standards/docs/API_DOCUMENTATION.md:1292): “anything credential-shaped is redacted the same way the diagnostics are,” and the issue’s prohibition on exposing credentials through the new field.

- **P2 — Security-contract tests leave material gaps.** [test_errors.py:497](/tmp/statsplus-145/resumed-review-standards/tests/test_errors.py:497) checks forbidden substrings but permits extra validation diagnostics. A mutant publishing validation type, location, and redacted input passes all 125 tests; removing the length limit also passes all 125. These miss the documented no-diagnostics and bounded-value guarantees. [AGENTS.md:29](/tmp/statsplus-145/resumed-review-standards/AGENTS.md:29) requires “a test that fails for the behavior being changed.”

Mutation testing covered all **22 changed test functions / 33 cases**. Every primary mutation was caught; all restored tests passed. Across 30 mutants, 26 were killed and four survived.

The completion gate passed: **4,866 tests, 84.88% branch coverage**, migration replay, and fixture validation. Candidate bytes and full diff were preserved exactly.

[Detailed mutation report and logs](resumed-standards-mutations.md)