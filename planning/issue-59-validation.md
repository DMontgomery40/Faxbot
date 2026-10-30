# Issue #59 validation

The address migration is implemented on this branch. Diagnostics, savings mechanism settings and results, setup packs, console buttons and Overview alerts now name the six-area pages directly. Queries for result parts and recommendation sections are retained. The console's moved-address and legacy-destination tables remain unchanged, so existing bookmarks still work. Historical documentation URL inputs in `docsLinks.ts` also remain supported.

Delivery setup and the CLI distinguish delivering received documents to staff by email, importing incoming documents and submitting outgoing faxes. Source direction values, permissions, HTTP methods, persistence and activation behavior are unchanged.

## Local evidence

The worktree volume had 13 GiB available, below the owner's 15 GiB minimum. No dependencies were installed and no build ran. Neither pytest nor the console's Node dependencies were present.

- `python3 -m unittest discover -s api/tests -p test_console_addresses.py`: failed before the migration, then passed all four checks. It checks canonical literal destinations against the navigation declaration, provider pages, shared JSON fixture destinations and the actual pure intake description function without importing the HTTP server.
- Changed Python files pass `ast.parse`; `git diff --check` passes.
- `python3 -m pytest -q api/tests/test_console_cli_parity.py api/tests/test_settings_coverage.py`: could not start (`No module named pytest`).
- `npm --prefix api/admin_ui test -- --run --maxWorkers=2 --minWorkers=1`: could not start (`vitest: command not found`).

These source checks do not prove rendered interactions or console/CLI parity. The current regression scan covers literal destinations and known navigation call sites; computed destinations still depend on the existing runtime suites.

## Remaining acceptance work

On this same branch, when disk space and project-local dependencies are available:

1. Run the console suite with at most two workers using the npm command above.
2. Run `make test-local PYTEST_ARGS='tests/test_console_addresses.py tests/test_console_cli_parity.py tests/test_settings_coverage.py tests/test_capabilities.py tests/test_savings_mechanisms.py tests/test_diagnostics_report.py tests/test_setup_plan_packs.py tests/test_setup_plan_http.py tests/test_setup_plan_cli.py tests/test_cli.py tests/test_intake_sources.py tests/test_sip_network.py tests/test_telnyx_t38.py'`, using only the synthetic/local fixtures and leaving external database URLs unset.
3. Regenerate the CLI reference with `make cli-docs` and confirm the changed help text. Its affected text was synchronized statically here; generator output is not verified.
4. Run the normal console typecheck/build after confirming the disk threshold is met.

No live accounts, services, messages, email or telephony were exercised. Operator guides remain owned by Docs Autopilot after integration; this task does not authorize running that external workflow.
