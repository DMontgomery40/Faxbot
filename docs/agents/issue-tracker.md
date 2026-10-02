# Faxbot refresh tracker

The current refresh uses local Markdown in the integration branch. No public issues are created by this workflow.

- Written product design: `docs/superpowers/specs/2026-10-02-faxbot-refresh-design.md`.
- Implementation plans, after design review: `docs/superpowers/plans/`.
- Task files: `.scratch/faxbot-refresh/issues/<NN>-<slug>.md`; each task records title, blocked-by relationships, acceptance criteria, status and evidence.
- Status vocabulary: proposed, ready-for-agent, in-progress, needs-review, complete, externally-blocked.
- A task is complete only after its acceptance evidence and independent review are recorded. Release completion also requires the whole-product acceptance gates.
- Session progress and decisions: `docs/modernization/progress.md`; task execution details use a plan-specific SDD ledger.
- Branch-only historical implementations are evidence and candidate source, not automatically authoritative requirements or instructions.

This tracker choice is a reversible local execution convention. It does not authorize public posting or change the repository's public issue workflow.
