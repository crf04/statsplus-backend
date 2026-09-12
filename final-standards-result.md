Two P2 standards findings remain, both in regression coverage:

- **Outer error envelope is no longer checked.** [test_game_logs.py:936](/tmp/statsplus-145/final-review-standards/tests/test_game_logs.py:936) and line 978 compare only `payload["error"]`. A mutant publishing validation metadata beside `error` passes all **131 tests**. This weakens the original whole-response assertions, contrary to the issue’s “existing error-contract tests are extended rather than replaced” and the [no-validation-context contract](/tmp/statsplus-145/final-review-standards/docs/API_DOCUMENTATION.md:1295). Restore whole-response comparisons.

- **Parameter-length coverage is missing.** [test_errors.py:416](/tmp/statsplus-145/final-review-standards/tests/test_errors.py:416) tests a short credential-shaped name. Preserving redaction while removing only parameter-name truncation passes all **131 tests**. The [API contract](/tmp/statsplus-145/final-review-standards/docs/API_DOCUMENTATION.md:1293) promises bounded parameter names; [AGENTS.md:29](/tmp/statsplus-145/final-review-standards/AGENTS.md:29) requires a test that fails for changed behavior. Add an overlength self-filter parameter case.

No current production leak was reproduced; the previous redaction defect is fixed.

All **10 recently changed test functions / 21 cases** failed under targeted mutations. Across **17 mutants**, 14 were killed and three survived; every restored run passed. Affected suites passed: **131 tests**, plus **13 self-filter tests / 37 subtests**. The full gate remained with the parent.

Candidate bytes and full diff were preserved exactly; temporary review files were removed.

[Detailed mutation report, patches, and logs](final-standards-mutations.md)