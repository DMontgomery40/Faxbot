---
layout: default
title: iOS
parent: Apps
nav_order: 1
permalink: /apps/ios
---

# iOS App

The iOS app lets you send faxes and check statuses from your phone. It connects to your own Faxbot server.

Key points
- A tunnel is required for the iOS app to reach your server (LAN-only setups won’t work off-network).
- HIPAA users should use a HIPAA-capable tunnel (WireGuard or Tailscale). Avoid Cloudflare Quick Tunnels for PHI.
- Pairing uses a short-lived code generated from the Admin Console. No secrets are embedded in QR codes.

Setup overview
1. Open Admin Console → Settings → VPN Tunnel.
2. Choose a provider:
   - WireGuard (HIPAA-capable): connect to your existing WG server (e.g., Firewalla).
   - Tailscale (HIPAA-capable): join your Tailnet with an appropriately scoped key.
   - Cloudflare Quick Tunnel (dev only): non-PHI trials; not HIPAA-compliant.
3. Click “Generate iOS Pairing Code” and enter the code in the iOS app.

Learn more
- Tunnels guide: {{ site.baseurl }}/networking/tunnels
- Admin Console local-only Terminal: {{ site.baseurl }}/local_admin_console

