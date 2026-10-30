# Issue 57 implementation handoff

Scope: [A5](https://github.com/DMontgomery40/Faxbot/issues/57), parent spec [48](https://github.com/DMontgomery40/Faxbot/issues/48), assessment and preservation ledger [46](https://github.com/DMontgomery40/Faxbot/issues/46). Dependency [49](https://github.com/DMontgomery40/Faxbot/issues/49) is closed. The README roadmap remains the status source.

## Bounded change on this branch

`SetupWizard.tsx` used the same `goTo` handler for Back and Next. Both invoked `saveStep`, which could write credentials, security or delivery settings on Back, and could prevent backward navigation for an invalid wizard draft.

Back now navigates without calling `saveStep`. Wizard drafts remain in memory. A forward action is labelled **Save and continue** on the four settings steps, and **Continue** after them. A successful save reloads the authoritative revision and baseline while retaining unsaved drafts from other steps. Reload still explicitly discards drafts. Trunk settings remain independently saved; Back refuses to unmount a dirty trunk editor and tells the reader to save or undo there. Save trunk settings, Apply and connect, and engine restart are unchanged.

Rendered tests cover a draft credential on Back and its later explicit save, backward navigation with an invalid draft, and retaining later drafts across an earlier step's save without including them in that save. Existing wizard callers' tests use the explicit button names. No API or CLI behavior changes in this navigation-only slice.

## Work still required to finish A5

1. Replace Delivery setup's Add or change a provider destination with focused per-connection connect, validate, review-effects and activate tasks. First install must become a checklist using those same tasks, rather than continuing to require unrelated setup steps.
2. Add stable detail pages for provider accounts, trunks and digital connections: sending/receiving directions, credentials with source and secret/environment restrictions, numbers, status and check results, charges/rate card, and dependencies. Implement `faxbot delivery connections show NAME` with matching facts and permissions.
3. Label shared-storage fields as provider-wide. Review delivery effects and activation timing explicitly. Keep trunk Save, Apply and connect and engine restart separate.
4. Audit all check labels before invocation and in results. Sinch's current wizard response already explains that it checks credential presence only, but its button still says “Check these credentials.” Distinguish presence from authenticated provider checks across all connection surfaces.
5. Verify each preservation-ledger row: S02 Phaxio credentials/callbacks/signatures; S03 Sinch URLs and incoming basic auth; S04 Documo base/sandbox; S05 HumbleFax receiving/poll/check; S06 eFax receiving/delete/notification secret; S07 SignalWire SMS/signing/polling as well as fax; S08 full trunk connection/number/network controls; S09 full fax optimization/compatibility controls; S10 engine/incoming secret generation and provider billing credentials. Retain validation, permissions, source, safe recovery, and save/activation semantics. A read-only replacement is insufficient.
6. Run the console/CLI parity and settings coverage gates, affected UI suites, typecheck and build after dependencies can be installed. Review the focused journey's loading, empty, denied, failed and saved states. Then run Docs Autopilot after integration as instructed by AGENTS.md; this branch does not edit generated operator guides.

## Verification and environment limit

The worktree volume had **13 GiB free**, below the requested 15 GiB floor. No dependency installation or build was performed. Neither `.venv` nor `api/admin_ui/node_modules` exists here.

- Before the fix, an offline Node harness executing the actual extracted navigation handler with mocked callbacks failed: Back called `saveStep` once.
- After the fix, the harness passed seven navigation cases (Back, successful/failed forward save, dirty trunk, loading, busy and action fence) plus the actual save handler's revisioned write/reload with later-draft isolation. This is a narrow handler check, not rendered acceptance.
- The installed TypeScript parser accepted all five changed TSX files before and after, with no emit. This verifies syntax only, not types or rendering.
- `npm --prefix api/admin_ui test -- --run --maxWorkers=2 --minWorkers=1 src/__tests__/setupWizard.test.tsx` could not start before implementation: `vitest: command not found`.
- The same command after implementation, including `settingsParity.test.tsx`, `efaxSettings.test.tsx` and `setupPacks.test.tsx`, also could not start for the same reason. New rendered tests remain unexecuted.
- `FAX_DISABLED=true python3 -m pytest -q api/tests/test_console_cli_parity.py api/tests/test_settings_coverage.py` could not start: `No module named pytest`. These gates remain unverified.
- `git diff --check` passed. README links were checked locally. All executable verification used mocks and no live services.

Do not mark issue 57 resolved or claim the rendered suites, CLI parity, settings coverage or full focused-connection workflow passed from this slice.
