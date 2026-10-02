# Faxbot refresh progress

## Goal

Completed and verified product by October 7, 2026, 10:00 AM Mountain. Keep the familiar UI as a starting point; upgrades are permitted. Complete RBAC is required. Preserve compatibility with the existing TestFlight iOS app. No demo-plus-backlog interpretation. Preserve Docs Autopilot / OpenAPI / Redocly / MkDocs / Mike and make updates run on every main commit as requested.

## Execution

- Integration worktree: `/Users/davidmontgomery/.codex/worktrees/faxbot-refresh/Faxbot`.
- Integration branch: `feat/faxbot-refresh`.
- Base: `7e0c15fa` (matches fetched origin/main).
- Goal mode: active; not complete.
- User-selected worker model: `gpt-6.1-sol`, reasoning effort `xhigh`.
- Primary agent owns architecture, state/recovery design, complex cross-module changes and context-heavy decisions. Bounded implementers and independent reviewers receive sufficient context through task briefs and evidence pointers.
- No application implementation or deployment has occurred in the first assessment/setup steps.

## Completed discovery

1. Initial main assessment: `initial-assessment.html`. The original temp report was visually checked in a browser. Its findings apply to main; newer branches require reconciliation.
2. Isolated main baseline: 27 Python tests passed in disabled mode. Real conversion and provider delivery were not covered.
3. Direct content probe: normal.txt produced a 1,571-byte PDF; contest.txt produced a 19-byte placeholder with FAX_DISABLED=false.
4. Fetched and inspected remote branch identities. development, iOS, desktop and docs contain material work absent from main.
5. Installed only the additional Matt Pocock workflow skills needed for evaluation/execution: wayfinder, implement-spec, to-spec, to-tickets, implement, tdd. Source pinned to mattpocock/skills commit d81f3a183412e71a5b1e84ca21bc1a35eea03a60; preexisting skills preserved.
6. Two Sol 6.1 xhigh read-only inventory agents mapped contracts and deployment/docs. Their completed reports will be linked alongside this file.

## Design review and next implementation plan

`../superpowers/specs/2026-10-02-faxbot-refresh-design.md` is a proposed concrete design, opened to the user. An asynchronous question requests written-design approval, especially dedicated self-hosted deployment, retained workflows, and test interfaces. User replied with three clarifications: RBAC must be finished, UI upgrades are welcome (the prior UI is liked but not frozen), and the iOS app exists in TestFlight and is fine-ish. Those changes are incorporated in the written design. No deployment-model change was requested. A concrete first-slice implementation plan remains to be reviewed.

Once reviewed, create a concrete task plan/graph, preserve the user's selected hybrid primary/subagent execution method, and begin the first complete document-integrity slice. Do not redispatch completed inventory work after compaction.

## Important discovered constraints

- Main's docs jobs do not implement the requested every-main-commit behavior. Docs Autopilot automatically creates plans on development; later API generation was disabled; Mike publishes the mkdocs branch. Restore actual generation and publication rather than hand-editing generated output.
- Current Node MCP tools have a duplicate default export that fails parsing.
- Mobile source exists on origin/iOS. Do not label it missing merely because it is absent from main.
- Existing client, provider, tunnel, pairing, migration and distribution claims require the explicit capability inventory before implementation sequencing.
- External provider accounts, controlled destinations, signing/distribution access, actual deployment identity, and container runtime availability must be established before their release gates can pass. No fabricated or simulator-only operational success.

## Skill/process choices

Matt's implement-spec supplies the whole-spec task-graph pattern; Superpowers SDD supplies bounded briefs and per-task independent review. Do not run two competing orchestration loops. Start with one implementation writer at a time while the monolithic backend is shared; parallelize read-only discovery/review and later only genuinely isolated work. Keep the user's explicit model choice over generic skill tier advice. Primary handling of context-heavy work follows the user's instruction.

Wayfinder is available for unresolved large decisions; do not make a planning-only loop the deliverable. Completion means implementation, validation, release and transferability evidence, not closed planning tickets.
