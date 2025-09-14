# v3 GUI Implementation — Executive Summary

Purpose
- Provide a high-level, decision-ready summary of the Admin Console v3 work, risks, and success metrics.

Quick Wins (ship first)
- Keys Management: create/rotate/revoke with copy-once UI — enables safe API access immediately
- Send Fax (Outbound): simple form with PDF/TXT validation + status link — end-to-end usability fast
- Diagnostics (basic): health check + common pitfalls — reduces support friction
- Settings (security): enforce API key + HTTPS toggle + HIPAA help — prevents misconfigurations

Major Features (phased)
- Jobs List/Detail with failure-driven help — deeper operational visibility
- Inbound Inbox (when enabled): secure PDF access with retention — PHI-safe workflows
- Plugins Tab (feature-flag): discovery + config persistence — v3 architecture control surface

SIP Provider Plugin Focus (most common use case)
- Treat SIP trunk providers as capability-driven plugins:
  - T.38 fax over SIP → FaxPlugin (category: outbound)
  - SIP MESSAGE (SMS) → MessagingPlugin (category: messaging)
- UI isolation: only show SIP guides when SIP is selected; never leak Phaxio/Sinch guidance
- Config: surface AMI host/port/username/password, SIP credentials, UDPTL port guidance, and network isolation warnings

Success Metrics
- Time-to-first-fax (cloud): ≤ 5 minutes from landing page to sent fax
- Time-to-first-fax (SIP): ≤ 60 minutes with guided networking (non-expert)
- First-run errors: < 5% (guardrails + preflight)
- Support load: fallthrough rate drops via Diagnostics “actionable fixes”
- Docs engagement: Help/“Learn more” CTR indicates contextual clarity

Risk Mitigation
- Backend isolation banners; no cross-backend leakage
- HIPAA defaults on: API key required, HTTPS enforced, audit logging — with clear non-PHI toggles
- Schema-driven forms for plugins/storage to minimize drift
- Feature flags: Plugins tab only when server supports it
- Rollback playbook: keep JSON config writes atomic; no live apply in HIPAA mode

Rollout Plan
- Phase 1–3 (Foundation/Settings/Keys) → internal demo
- Phase 4–5 (Send/Jobs) → early doctor/pilot groups
- Phase 6–8 (Inbound/Diagnostics/Plugins) → general availability

Links
- Complete Plan: v3_admin_console_complete_plan.md
- Thoughts & Findings: v3_admin_console_thoughts.md
- SIP Provider Plugins (docs): /plugins/sip-provider-plugins.html

