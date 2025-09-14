---
layout: default
title: Hospital (HIPAA)
parent: Operator Playbooks
nav_order: 1
permalink: /playbooks/hospital/
---

# Hospital Playbook (HIPAA)

Audience
- Non‑technical staff and IT who must send PHI securely.

Goal
- Send a document to a known fax number in under 5 minutes with safe defaults.

Steps
1) Open Admin Console → Setup Wizard
   - Backend: Phaxio (Recommended)
   - Security Profile: HIPAA
   - Enter API key/secret from the Phaxio console
   - Set Callback URL to `<PUBLIC_API_URL>/phaxio-callback`
   - Apply & Reload
2) Public URL
   - If needed, create a temporary tunnel for testing (Cloudflare)
   - Update `PUBLIC_API_URL` to the HTTPS URL
3) Send
   - Admin Console → Send → select your PDF or TXT → enter destination and send
4) Verify
   - Admin Console → Jobs: confirm it shows In Progress → Success/Failed

Tips
- Only PDF and TXT are allowed; scan/Image → convert to PDF first
- Keep file under 10 MB (default limit); split if necessary

If it fails
- Use Diagnostics → follow links for fix steps (callback reachability, signature verification, HTTPS)

Compliance
- BAA with Phaxio
- `PHAXIO_VERIFY_SIGNATURE=true`, `ENFORCE_PUBLIC_HTTPS=true`, `API_KEY` required
- Audit logging enabled per policy

