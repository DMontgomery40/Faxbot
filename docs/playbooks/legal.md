---
layout: default
title: Legal
parent: Operator Playbooks
nav_order: 3
permalink: /playbooks/legal/
---

# Legal Playbook

Goal
- Send filings/contracts securely and reliably.

Setup
- Backend: Phaxio or Sinch (cloud)
- Security: require API key; use HTTPS; enable audit logging if policy requires

Workflow
- Send via Admin Console → Send; track via Jobs
- Prefer PDFs; ensure readable resolution and file size under 10 MB

Reliability
- Keep Diagnostics green: HTTPS, callback reachability, API key enabled

Escalation
- If repeated failures occur, switch to an alternate backend or retry later; check provider status pages

