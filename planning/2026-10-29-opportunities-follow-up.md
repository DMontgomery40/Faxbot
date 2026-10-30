# Issue 55: missing-fact slice and remaining work

This branch adds the existing missing-fact advice read to Opportunities and to the combined `faxbot savings opportunities` report. It preserves the standalone Facts page and its portfolio planner. The embedded section has no apply action. It reports loading and failure separately, participates in the combined empty state, and accepts `section=facts` links. Both presentations show period, effort and unpriced benefits explicitly. The synthetic `opportunityFacts.json` fixture feeds console and CLI tests.

## Remaining acceptance work

Issue 55 is partial. Finish these items before closing it:

- Define one shared opportunity contract and aggregate all recommendation domains, missing facts, suggested packs and stored optional AI results. Preserve each source's permission and error boundaries; rule-based advice must work without AI.
- Group opportunities by the seven capability outcomes. Preserve existing section bookmarks and legacy commands. Renewal and POTS appear in the console but currently only as separate CLI subcommands, not in the combined report.
- Give every item its affected entity, observed problem, period, evidence, expected and cautious benefits (unknown where not supplied), effort and typed prerequisites. This slice adds period and effort only to missing-fact advice, without inventing cautious estimates.
- Connect every supported action to its canonical setting and add bidirectional capability/opportunity links. Keep scenarios advice-only. Keep the portfolio planner a named scenario tool; it remains on Facts to establish in this slice.
- Show suggested packs without bypassing their settings-write permission, preview, validation, revision or apply semantics. Optional AI analysis remains in the existing AnalysisCard.
- Extend the shared fixtures to the complete contract and run the backend, console, console/CLI parity and settings coverage suites. Check loading, empty, denied, failed, saved and activation states and browser layout when dependencies can be installed.

## Verification limits

The volume had only 13 GiB free at task start, below the owner's 15 GiB gate. No dependencies were installed and no build was run. There is no worktree virtualenv or UI node_modules. Targeted pytest and Vitest commands could not start because those tools are absent. Dependency-free Python syntax, report-registration and isolated formatter checks, plus `git diff --check`, are the checks available here; they do not establish runtime UI or full CLI acceptance. No live accounts, documents, calls, email or production services were exercised. Docs Autopilot remains a post-merge coordinator task.
