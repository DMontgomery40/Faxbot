# A12 preservation validation — complete source-check slice

Issue [#64](https://github.com/DMontgomery40/Faxbot/issues/64) implements the preservation requirement from [#48](https://github.com/DMontgomery40/Faxbot/issues/48), based on the assessment and appendices in [#46](https://github.com/DMontgomery40/Faxbot/issues/46). This branch delivers the source-check foundation, not the completed overhaul acceptance.

## Confirmed gap and change

The existing settings test accepted field names in any non-test screen file. The console/CLI parity test likewise treated disconnected TSX files as request callers. Deleting a page's use of a component could leave these checks green. Navigation coverage did not enumerate the 72 nested control families. This checkout also still maps `delivery/change` to `admin/setup`, whose renderer is the full `SetupWizard`; the focused connection-change prerequisite cannot be treated as shipped merely because A12 names it.

`api/admin_ui/src/__tests__/controlLedger.json` now lists O01–O36 and S01–S36, their current canonical entry pages, frozen page gates and the source components cited by the assessment. The console ledger test checks completeness, canonical address resolution, page gates, safe denial and application source reachability. Broad families span more than their entry page: the recorded page is not a claim that all their controls render there.

`api/tests/console_reachable.cjs` follows used value imports from `main.tsx`. An orphan file, an unused import and a type-only reference cannot preserve a control. Both settings coverage and method/path console/CLI parity now use this inventory. Regression tests leave an unused import behind and remove a home while retaining its source. No operator API, console editor or CLI command changes in this slice.

This is conservative static reachability. It can include a component in an unexecuted conditional or an unused function within a reached file. It does not bind an individual action or field to the page that renders it, or prove mutation permissions, secret handling, accepted values, save semantics or activation. Keep the existing rendered and HTTP/CLI behavior tests.

## Local validation

All tests use synthetic fixtures, local SQLite and mock HTTP handlers. No real providers, accounts, fax calls, email or production services are used. PostgreSQL variants without a disposable local database are skipped. Dependency installation uses worktree-local environments; the host Python 3.14 could not install pinned psycopg2-binary without pg_config, so validation uses a local Python 3.11 instead.

The initial parity/settings baseline passed with 27 tests passed and four PostgreSQL variants skipped. The targeted console run (`npx vitest run src/__tests__/controlLedger.test.tsx src/__tests__/navigation.test.tsx src/__tests__/capabilities.test.tsx src/__tests__/settingsParity.test.tsx --maxWorkers=2 --minWorkers=1`) passed all 204 tests. MSW reported unmatched mock handlers in existing suites; those requests were intercepted, not sent to a real provider.

The final backend command was `env -u FAXBOT_SCHEMA_TEST_POSTGRES_URL make test-local VENV=.venv311 PYTEST_ARGS='tests/test_console_cli_parity.py tests/test_settings_coverage.py tests/test_capabilities.py tests/test_cli.py::test_committed_command_reference_is_current'`: 50 passed, 14 skipped (PostgreSQL variants). `npm --prefix api/admin_ui run build` passed typechecking and the production build, with the existing large-chunk advisory. The final browser-only ledger rerun passed 74 tests after its source assertions moved to Python. The new source-family test was additionally run explicitly with the reachability regression test: both passed.

A synthetic before/after comparison confirmed that the original scanner counted an orphan screen's `POST /items`; the new scanner counts only the reachable `GET /items`. `node --test api/tests/console_reachable.test.cjs` passes and is also invoked by the Python parity suite. Intermediate harness failures (an initialization-order error and address-prefix assumptions) were corrected. The build caught missing Node type declarations in the browser test; moving source reachability assertions into the Python parity test avoids adding a browser dependency on Node APIs.

`PYTHONPATH=api FAX_DISABLED=true .venv311/bin/python -m app.cli.reference`, followed by `cmp`, confirmed the committed CLI reference matches the command definitions. `FAX_DISABLED=true .venv311/bin/python scripts/docs_ai/generate_reference.py --output .validation64/generated` succeeded without modifying generated product pages. No runtime surface changed, so no generated reference change is needed.

## Remaining acceptance work

1. Extend the ledger to bind each nested action and public configuration field to its actual permitted editor, including conditional provider and schema-generated controls. Connect mechanism catalogue entries and their prerequisites to those same editor contracts. The existing capability contract tests are retained, not replaced by a claim of full per-control coverage.
2. Prove that removing a particular field, mutation, permission check or section from its editor fails the relevant ledger/behavior check. Current mutation checks cover removed entry pages and source reachability only. Audit preservation of validation, secret handling, save/cancel and activation through those behavior tests.
3. Walk through all 18 tasks in #46 section 10 in a real browser, with synthetic API answers, at desktop and phone widths and using the keyboard. Record fixture, actions, observations and evidence for each task. No real-browser walkthrough was performed in this source-check slice. Component tests do not satisfy that criterion.
4. Fix and record any failures from that walkthrough locally; this task does not authorize filing GitHub issues. Reconcile README claims and regenerate references for any resulting runtime changes. Run the combined SQLite/PostgreSQL, console and strict documentation gates on the integrated overhaul as #48 specifies.
5. After the authorized integration, run Docs Autopilot on merged code with the pre-wave base and review its proposal. It was not run here: this task forbids merges, external account operations and PR creation. No merge or remote write is part of this branch.

The README keeps A12 acceptance unfinished. Existing product limitations and the separate enterprise testing boundary remain in effect.
