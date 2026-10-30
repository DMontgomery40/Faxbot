# Issue 60 implementation handoff

This change delivers a mailbox detail foundation, not completion of A7. The README roadmap remains the capability status source.

A mailbox name links to `#/delivery/mailboxes?mailbox=ID`. The address can be copied or reopened and uses browser history. The page reuses the mailbox rename/enable editor, filters number mappings to that mailbox, scopes the sending-rule editor to its ID and filters acknowledgement-target rows. The installation target is labelled installation-wide. Missing mailboxes do not fall back to another mailbox or expose its editors. Existing list addresses and editors remain available.

`faxbot delivery mailboxes show NAME_OR_ID` reads mailbox metadata, number mappings, configured provider and email summaries, published sending rules and the acknowledgement target/backup, with JSON available. Failures from optional sources remain unavailable, rather than pretending that a rule or target is absent. CLI output is inspection only; the existing commands still perform all changes.

Preservation: appendix A O26 and appendix B S17–S20 in issue 46 were checked. S17 and S19 reuse the same mutation components, permissions and versions. S18's original number list/editor remains and is reused for scoped mappings. S20 remains on the mailbox list unchanged, as do sending identity, reply-number overrides, send-only numbers and access grants in their existing homes. No schema, transport, provider or activation behavior changes.

## Remaining work for issue 60

- Build a number detail address and keyboard-operable list links. Add `faxbot delivery numbers show` in the same change. Assemble connection/account, every conditional inbound mapping, mailbox and staff email on that page.
- Resolve the actual effective receiving rules, conditional account/sender/time matches, per-rule email overrides and inheritance. Current provider/email summaries are configuration summaries, not proof of successful delivery. Distinguish missing permission, unavailable data, unset configuration and an observed failed stage.
- Add mailbox import sources, effective access and contextual access-grant links, eligible owners, sending identity and effective inherited values. Backup and acknowledgement targets exist; a mailbox's general owner summary does not.
- Add number blocking, number moves, recent activity and open problems with observed evidence. Include working links to the canonical controls and preserve every permission and save/activation boundary.
- Move the remaining stacked mailbox-list panels into their canonical homes, retaining installation-wide acknowledgement and uncertain-send controls. The list's existing panels are deliberately retained in this foundation to avoid dropping settings before the full migration.
- Complete console/CLI data parity for these additional details, including rule-specific staff email overrides, account identity, rule draft/check/publish status and effective values. The CLI prints published rules and exposes the full rules response in JSON; the console also offers draft editing.
- Add navigation Back/Forward and copied-address integration checks, denied/failing-source and settings-only scenarios, plus tests for each added diagnostic and mutation boundary. Run console/CLI parity and settings coverage again.

Validation uses synthetic fixtures, in-process HTTP clients and mocked console requests. No live fax, email, provider or production validation is required or authorized by this task.

## Validation in this worktree

The new mailbox-address and CLI tests failed before implementation (missing links, missing-record fallback and missing `show`). The pagination regression failed before the pagination fix. Initial intermediate checks also caught an incorrectly placed mailbox condition and test expectations that assumed settings permission; these were corrected. Superseded overlapping runs were stopped rather than treated as passing evidence.

The broad console run passed 326 tests:

```sh
cd api/admin_ui
npx vitest run src/__tests__/resourceAccess.test.tsx src/__tests__/navigation.test.tsx src/__tests__/navigationCoverage.test.ts src/__tests__/policyRefresh.test.tsx src/__tests__/providerRulesWiring.test.tsx src/__tests__/workScreen.test.tsx --maxWorkers=2 --minWorkers=1
```

After the pagination fix, the affected suites passed all 40 tests:

```sh
cd api/admin_ui
npx vitest run src/__tests__/mailboxDetail.test.tsx src/__tests__/resourceAccess.test.tsx src/__tests__/policyRefresh.test.tsx src/__tests__/providerRulesWiring.test.tsx --maxWorkers=1 --minWorkers=1
npx tsc --noEmit
npm run build
```

The type check and production build passed. The build warns about a large bundle. Console tests report existing MUI warnings and unmatched mock requests; MSW blocks those requests instead of contacting a service.

The backend run passed 161 tests, with four PostgreSQL variants skipped because no disposable database URL was configured:

```sh
make test-local VENV=.venv/test PYTEST_ARGS='tests/test_cli_mailbox_detail.py tests/test_cli.py tests/test_cli_work.py tests/test_cli_rules.py tests/test_settings_coverage.py tests/test_console_cli_parity.py'
make cli-docs VENV=.venv/test
git diff --check
```

The generated CLI reference and whitespace check passed. Dependencies were installed locally after the disk check showed 162 GiB free. The initial Python 3.14 install failed on the pinned PostgreSQL driver's missing wheel; the Python 3.11 virtual environment installed successfully. No live provider, email, fax or production tests were run.
