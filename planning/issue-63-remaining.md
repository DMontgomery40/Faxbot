# A10 implementation handoff — issue #63

This records the partial implementation on 29 October 2026. The README roadmap remains the shared status source. Scope comes from issues #63, #48 and #46; #54 and #55 describe its navigation and opportunities dependencies.

## Implemented in this slice

Invoice details use `#/savings/invoices?invoice=ID`, with the existing `#/costs/invoices` alias accepted. Details and correction-history totals are real links. A copied address opens that exact invoice version, including a superseded total; hash navigation follows Back and Forward. Closing removes only the invoice parameter. IDs are escaped, and unrelated pages cannot open an invoice through their query. The existing read endpoint and permission checks still apply.

Failed detail reads show the existing denied, unavailable or error state instead of spinning forever. General errors offer retry. Detail changes discard late answers from the previous read. Invoice mutations, file downloads, accounting and activation behavior are unchanged.

`faxbot savings invoices show ID` prints the record's console address. Its history includes the invoice IDs and the command to open an older total. JSON remains the existing API response.

## Verification limits

The volume had 13 GiB free, below the owner's 15 GiB threshold. No dependencies were installed and no build was run. This worktree has no Python virtual environment or console node_modules. Python reports no pytest, and the affected npm test command reports no vitest. No provider, fax, email or other external account operation was run.

Three dependency-free Node tests pass for the actual address helper: escaping/round trips and preserved query parameters, closing, and old/unrelated addresses. Rendered console tests and CLI integration tests were added but could not run here. Thus this slice is implemented in source, not fully accepted.

Once disk space and project-local dependencies are available, run these offline checks (at most two workers):

- `node --experimental-strip-types --test api/admin_ui/src/__tests__/invoiceAddress.node.test.mjs`
- `npm --prefix api/admin_ui test -- --run src/__tests__/chargesInvoices.test.tsx --maxWorkers=2`
- `make test-local PYTEST_ARGS='tests/test_charges_cli.py tests/test_invoices.py tests/test_console_cli_parity.py tests/test_settings_coverage.py'` using this worktree's own .venv and console dependencies. Do not supply a PostgreSQL network target.
- Console type check/build after the disk restriction clears. Check copied invoice URLs, history versions, Back/Forward, reload, missing and forbidden records, retry and downloads with mocked APIs. No live account is needed.

Generated CLI reference help does not change in this slice. Operator-guide changes belong to Docs Autopilot after integration, per AGENTS.md.

## Remaining acceptance work

1. Add invoice-period/account drill-down to actual charge and attribution records. Invoice reconciliation currently returns aggregates, not a record-level ledger. Include sent attempts, received faxes/calls and provider-only records, retaining the exact account and period used by attribution. Keep reported charges, estimates, unpriced records, plan fees, other currencies and residuals separate. A provider-only record must not invent a Faxbot fax link.
2. Make those records reachable from Invoices, Charges, Spending and Prices & plans, with usable account, number and fax destinations. Add corresponding CLI filters/detail commands where missing. Preserve permission gates and make links work with copied addresses, reload and browser history. Existing Charges is not an invoice-period charge ledger; simply linking to it would misrepresent coverage.
3. Integrate supported opportunities with their canonical setting changes. Persist the actual before/after change, actor, time, affected scope and source opportunity only after a successful supported save/apply. Preserve each setting's validation, permission and activation semantics; advisory scenarios must not acquire Apply actions.
4. Read subsequent eligible use after each recorded change, with an explicit “nothing measured yet” state when no qualifying evidence exists. Link the underlying evidence and expose the same facts in console and CLI. Configuration alone does not prove use or savings.
5. Keep money, billed units, time, pages, bytes and capacity in their own units and evidence windows. Reuse on a call already avoided must never add a second avoided-call saving. Add synthetic tests at the API/console/CLI seams for these boundaries, mixed currency, unknown prices and no post-change traffic.
6. Run the pending integration, parity and settings coverage checks above and the affected suites for each later slice. Update README status only after the relevant checks pass.
