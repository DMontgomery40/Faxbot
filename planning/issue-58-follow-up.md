# Issue 58: permission slice and remaining work

This branch addresses the generic Settings permission bug from issue 58, using
the change model in issue 48 and the preservation rule in issue 46 section 12.
Issue 49's six-area navigation is already present in this checkout.

The generic editor previously enabled edits whenever it had a revision, even
when `canWrite` was false. Its existing fieldset now also requires write
permission. Apply checks permission in its handler. Saving a recovery copy,
which sits outside that fieldset, has a disabled state and a handler guard.
Read-only accounts see the reason. Reload, redacted export and the separately
authorized restart action retain their existing permission boundaries.

Existing console tests that exercise edits now explicitly grant `canWrite`.
The existing email read-only case still exercises the default denied state.
New rendered tests cover omitted and false permissions, disabled generic
controls, blocked Apply and recovery-copy actions, and available Reload.

## Verification limits

The worktree's volume had 14 GiB available, below the owner's 15 GiB minimum
for installing dependencies or building. No dependencies were installed and
no build ran. Neither React/Vitest nor pytest is installed in this worktree.
The targeted Vitest command fails at startup with `vitest: command not found`;
Python reports `No module named pytest`. Thus the rendered tests, existing
console suites, CLI suites, settings coverage and console/CLI parity remain
unverified. A dependency-free Node assertion reproduced the original edit-gate
failure; a truth-table check of the actual edited expression passes after the
fix. This checks the gate only, not rendering or full acceptance.

## Work still required

1. Run the new rendered tests and affected console tests with at most two
   workers, then the console suite and type check when disk space and local
   dependencies permit. Run the offline settings coverage and console/CLI
   parity suites. Do not substitute static references for rendered evidence.
2. Give independent sections their own Save and Cancel. State which values
   each saves, which edits Cancel discards, and which earlier saves remain.
   Preserve partial-apply edits, revision conflicts and uncertain save recovery.
3. Protect unsaved edits on console navigation, browser Back and page close,
   and on Reload. Mark independently saved sections and pending activation.
4. Before delivery-affecting applies, review the actual changes, affected
   traffic and activation time. Keep rules' draft/check/publish and trunk
   Save, Apply and connect, and restart distinct.
5. Consolidate MCP.tsx and Settings' MCP section into one editor per value.
   Preserve active-versus-desired client configuration, checks, credentials,
   permission gates and restart behavior.
6. Audit owner-only and deployment-controlled reasons and change paths across
   generic and schema-driven controls, including dialogs rendered in portals.
7. Implement matching CLI change semantics in this ticket and regenerate the
   CLI reference. This slice changes no CLI behavior; the existing CLI already
   relies on server permission checks, including a restricted recovery-copy
   test in `test_cli.py`.

Review this list against all of issue 58's acceptance criteria before marking
the issue complete. Guides under `docs/` remain owned by Docs Autopilot and
were not hand-edited in this slice. No live services were used for validation.
