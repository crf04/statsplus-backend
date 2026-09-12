Two P2 standards findings remain:

- **Long ordinary values lose the offending fragment.** [game_routes.py:214](/tmp/statsplus-145/review-3-standards/app/routes/game_routes.py:214) treats truncation as credential redaction. For both minutes and self filters, `"0" * 201 + ",bad"` publishes 200 zeros and omits `bad`. This violates the [contract that only unusable entries appear](/tmp/statsplus-145/review-3-standards/docs/API_DOCUMENTATION.md:1279). Distinguish redaction from truncation and add long ordinary-value cases.

- **Empty-key coverage misses malformed ranges.** [test_errors.py:537](/tmp/statsplus-145/review-3-standards/tests/test_errors.py:537) covers only `self_filters[]=a,b`. Reverting the separate malformed-range branch passes all **137 affected tests**, although `self_filters[]=a` loses its brackets. Add malformed and empty ranges, per [AGENTS.md’s failing-regression-test requirement](/tmp/statsplus-145/review-3-standards/AGENTS.md:29).

Both previous findings are resolved. All **9 recently changed test functions / 20 cases** failed under targeted mutations; **6 of 7 mutants were killed**. All 20 quoted-credential probes passed without leaks.

Restored checks: **137 tests**, plus **13 self-filter tests / 37 subtests**, passed. Candidate bytes and full diff match the initial snapshot exactly. No bootstrap or full gate ran.

[Detailed report, mutation patches, and logs](review-3-standards-mutations.md)