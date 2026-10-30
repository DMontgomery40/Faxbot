# Issue 62 implementation and validation record

The README roadmap remains the status source. This record describes the partial
change on the issue branch; it does not claim that issue 62 is complete.

Read issues 62, 48 and 46 (including the preservation appendices), and the
prerequisite tickets 50, 54 and 58. Confirmed that Destinations mounted a
FormDialog containing independent child editors. Its Cancel could not undo their
saved changes. Case packets opened the same dialog.

The slice replaces that wrapper with a recipient page at
`#/recipients/list?recipient=<encoded number>`. Desktop and mobile detail actions
are native links. Lookup opens that address, and the page follows address changes.
Case packets link to it. The heading receives focus. All existing child panel
mounts and their write permission inputs remain. Identity/routing has its own Save,
keeps the page open, retains the returned resource version for later saves, reports
success, and clears that success indication when those fields change. There is no
outer Cancel. The command `faxbot recipients show NUMBER` prints the corresponding
console address in human output; JSON output remains the existing API response.

## Validation limits

The volume had 14 GiB free at the initial check. Under the task's 15 GiB rule no
dependencies were installed and no build was attempted. This worktree has neither
console node_modules nor a Python virtual environment. The installed Node is 22,
while CONTRIBUTING specifies Node 24. No other checkout's environment was used.

Before the implementation, the new isolated address test command failed because
Vitest was absent. After implementation, this affected-suite command also failed
before collection for the same reason:

```
npm --prefix api/admin_ui test -- --run src/__tests__/recipientAddress.test.ts src/__tests__/deliveryScreens.test.tsx src/__tests__/capacity.test.tsx src/__tests__/deliveredCost.test.tsx src/__tests__/numbersRecipients.test.tsx --maxWorkers=2 --minWorkers=1
```

Python could not report a pytest version before implementation, and the following
command failed before collection after implementation (`No module named pytest`):

```
PYTHONDONTWRITEBYTECODE=1 FAX_DISABLED=true FAX_DATA_DIR=./faxdata DATABASE_URL=sqlite:///./test_faxbot_ci.db python3 -m pytest -q api/tests/test_cli.py api/tests/test_console_cli_parity.py api/tests/test_settings_coverage.py
```

In-memory checks using the already installed TypeScript parser and Python AST
parser passed. Isolated execution of the address helpers passed six TypeScript and
two Python cases. Those checks import no application and use no network. Static
comparison with HEAD confirms every previous recipient child panel is still
mounted. These are syntax/helper/preservation checks, not a React type check,
rendered verification or console/CLI parity proof. git diff --check also passed.

## Remaining acceptance work

- Replace the broad capabilities section with the full identity/numbers, history
  and cost, effective route policy, agreements, partner state, case documents and
  capabilities organization. Current partner/case links are general destinations,
  not recipient-filtered records. Current history and route data remain the same
  data that the old dialog showed.
- Integrate recipient-specific capability applicability and met/unmet
  prerequisites from the capabilities contract, including recipient agreement
  states and direct links to each canonical control. The retained panels expose
  their existing controls; their presence is not that evaluation contract.
- Complete the unsaved-edit guard from issue 58 for identity/routing and every
  independently saved child editor, including Back, reload and cross-page links.
  This slice states save boundaries but does not protect drafts from navigation.
- Complete explicit saved/loading/empty/denied/failure and recovery coverage for
  every section. The parent now shows loading and read-only explanations, but
  retaining child behavior does not prove the full state matrix.
- With sufficient disk and project-local dependencies, run the affected suites
  above, console type check/build, console/CLI parity and settings coverage. Review
  all O09–O17 mutations with mocked providers, keyboard-only navigation, copied
  links, Back/Forward/reload and mobile layouts. The added rendered tests are
  unrun. No live fax, email, partner, account or production validation is needed
  or authorized by this task.
