# v3 Admin Console — Thoughts & Findings

Purpose
- Track concerns, architecture notes, and GUI-first violations observed during v3 work.
- Serve as a running risk/mitigation log for the Admin Console implementation.

Initial Concerns about Codebase State
- Mixed branches: prior work landed on docs-jekyll-site; development did not contain several planning docs.
- Duplicate docs in repo (compiled _site, duplicate filenames with " 2"): created confusion in nav and contributor workflow.
- Some endpoints were documented but feature-gated; UI must handle “disabled” states gracefully.

Architecture Issues Found
- Plugin config persistence: plan assumes `PUT /plugins/{id}/config` persists to atomic JSON; live-apply is deferred. UI must communicate that changes apply during maintenance windows.
- Inbound PDF access: tokenized access vs API-key gating must be clearly separated; HIPAA banners required when INBOUND_ENABLED + PUBLIC_HTTPS disabled.
- Distributed rate-limits: node-local limiter means multi-instance users rely on edge rate limiting; Diagnostics should explain this.

GUI‑First Violations Identified
- Historical CLI-only workflows (environment-only config) without an equivalent UI path.
- Keys lifecycle steps not fully represented in UI (rotate/revoke copy-only flows).
- Troubleshooting guidance mixed across backends (needs isolation per selected backend).

Implementation Strategy Notes
- Schema-driven forms for plugins and storage configuration to reduce drift.
- Single source for feature flags (prefer /health extension or presence of /plugins) to drive nav.
- Copy short, with Learn more links; no secrets in logs/network panel.
- Always include actionable error states (map 400/401/404/413/415 to plain-English guidance).

Open Questions
- Should the console support applying plugin config live with guarded rollback? Current plan: defer to maintenance windows.
- Do we need a lightweight server endpoint `/diagnostics` to aggregate non-sensitive checks for the console?

Next Steps
- Land the “Settings” + “Keys” phases, then iterate on Send/Jobs to unblock end-to-end use.
- Align copy with docs URLs now that the docs site nav is clean.

