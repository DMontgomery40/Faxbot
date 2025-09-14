---
layout: default
title: Real Estate
parent: Operator Playbooks
nav_order: 2
permalink: /playbooks/real-estate/
---

# Real Estate Playbook

Goal
- Send and receive offer paperwork quickly without unnecessary friction.

Setup
- Backend: Phaxio or Sinch (cloud) for simplicity
- Security: enable API key; HTTPS strongly recommended

Sending
- Admin Console → Send: attach PDF and send to recipient
- Keep files small (under 10 MB)

Tracking
- Admin Console → Jobs: watch for Success/Failed; retry if needed

Common issues
- 401 Unauthorized: set and use API key
- Callback delays: confirm public URL reachability and correct callback path

When to consider SIP/Asterisk
- High‑volume offices wanting to reduce per‑fax costs and with in‑house telephony expertise

