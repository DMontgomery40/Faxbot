# Issue #61: delivered slice and remaining work

This branch consolidates the existing document lifetime editors on Administration → Documents & retention. Received files retain their own lifetime and download-link expiry. Sent files and case originals retain their shared lifetime; case originals age from last addition or send. Provider download links and cleanup frequency remain separate settings. API and request limits move to Security; the read-only database description moves to System health. Connection settings link to the canonical document page. No configuration schema, default, API permission or activation rule changes.

The matching CLI read is `faxbot admin settings documents`; edits still use the existing revision-aware `faxbot admin settings set`. Masked export help points to `faxbot admin backup FOLDER`, without changing machine-readable export output. The existing personal menu already contains password, own sessions, own keys, appearance and sign-out.

## Precisely what remains

- NPI relevance and discovery: retain the existing `admin/npi` address and permissions, add an integrations catalogue/search entry, and surface the NPI editor from relevant organization/recipient setup. Establish relevance from explicit configuration; do not infer a compliance mode or hide an existing record. The current unconditional Installation entry remains.
- Prerequisite #58 is not implemented in this checkout: generic Settings' `canEdit` ignores `canWrite`, the page uses a shared Apply, and unsaved navigation lacks protection. Implement its read-only reasons, independent save/cancel and impact review before claiming #61's full preservation acceptance. This slice preserves the existing behavior, including owner locks, and does not claim those gaps fixed.
- Perform the remaining full rendered administration acceptance walkthrough after #58, including scoped permission states, save/cancel/activation and NPI navigation. Extend coverage beyond the existing static settings-name register to enforce canonical homes for every setting family.

No live provider, telephony, email or external account validation is needed for this slice. Automated checks use synthetic fixtures and in-process mocks.

## Validation

- Before implementation, the three A9 console regressions failed at the misplaced controls; the new CLI regression failed because `documents` did not exist.
- `make test-local PYTEST_ARGS='tests/test_cli.py::test_documents_lifetimes_and_masked_export_help tests/test_console_cli_parity.py tests/test_settings_coverage.py'`: 28 passed, four PostgreSQL cases skipped because no disposable PostgreSQL URL was supplied. SQLite cases ran locally.
- `make test-local PYTEST_ARGS='tests/test_cli.py::test_committed_command_reference_is_current'`: one passed. `make cli-docs` regenerated the checked-in reference.
- Navigation: 40 passed; address coverage: 239 passed. The first combined console run had one ambiguous owner-lock test selector (two fields now have the value 60); it was corrected to select the download-link field by its label.
- Case packet console tests: seven passed, including the updated canonical page name.
- `npm --prefix api/admin_ui run build`: passed (TypeScript and Vite); Vite reports the existing large bundle warning.
- `git diff --check`: passed.

Dependencies were installed only in this worktree (`npm ci`, Python 3.11 `.venv`). The first pip attempt used the system Python 3.14 and failed for lack of a psycopg2 wheel/pg_config; the Python 3.11 install succeeded. Tests sent no real faxes or email. MSW logged missing mock handlers for unrelated reads; those requests were intercepted rather than sent to services.
- Final settings-suite run: 67 passed, one unrelated mocked email-delivery test exceeded its 20-second timeout (it passed in the preceding full run). Retrying the email-delivery group, owner-only controls and all A9 regressions with one worker and `--testTimeout=60000` passed all nine selected tests (59 unselected). No product timeout was changed.

Reproduction commands for the console checks:

```sh
npm --prefix api/admin_ui test -- --run src/__tests__/settingsParity.test.tsx src/__tests__/navigation.test.tsx src/__tests__/navigationCoverage.test.ts --maxWorkers=2 --minWorkers=1
npm --prefix api/admin_ui test -- --run src/__tests__/settingsParity.test.tsx --maxWorkers=1 --minWorkers=1
npm --prefix api/admin_ui test -- --run src/__tests__/settingsParity.test.tsx --maxWorkers=1 --minWorkers=1 --testTimeout=60000 -t 'Settings email delivery|A9 document settings homes|Owner-only settings everywhere'
npm --prefix api/admin_ui test -- --run src/__tests__/caseLedger.test.tsx --maxWorkers=1 --minWorkers=1
```
