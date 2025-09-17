---
layout: default
title: Tunnels for iOS & Remote Access
nav_order: 40
permalink: /networking/tunnels
---

# Tunnels for iOS & Remote Access

The iOS app requires a reachable URL for your Faxbot server. A tunnel provides secure connectivity from the app to your server even when it’s behind NAT.

Options
- WireGuard (HIPAA‑capable)
  - Use your own WG server/router (e.g., Firewalla). Restrict access with firewall rules.
  - Keep AMI/SIP/UDPTL private; the tunnel is for API/Admin UI/iOS connectivity.
- Tailscale (HIPAA‑capable)
  - Add your server to a Tailnet with ACLs. Treat Tailnet as a private network.
- Cloudflare Quick Tunnel (dev only)
  - Not HIPAA compliant (no BAA, ephemeral). Use only for non‑PHI testing.
  - For HIPAA, consider a Named Tunnel with Access policies and a BAA (Phase 2).

Admin Console
- Go to Settings → VPN Tunnel to configure provider and test connectivity.
- Generate a short‑lived iOS pairing code (no secrets in QR or UI).
- Terminal and Admin Actions remain local‑only; do not expose via tunnels.

Environment reference (examples)
```
TUNNEL_ENABLED=false
TUNNEL_PROVIDER=none   # none|cloudflare|wireguard|tailscale

# WireGuard
WIREGUARD_ENDPOINT=router.example.com:51820
WIREGUARD_SERVER_PUBLIC_KEY=...
WIREGUARD_CLIENT_IP=10.0.0.100/24
WIREGUARD_DNS=1.1.1.1

# Tailscale
TAILSCALE_AUTHKEY=tskey-...
TAILSCALE_HOSTNAME=faxbot-server
```

Security notes
- HIPAA posture disables Cloudflare Quick Tunnel in the UI.
- No PHI or secrets appear in logs/UI. Secrets are masked; pairing codes expire quickly.
- PUBLIC_API_URL should be HTTPS in production; do not rely on dev tunnels for PHI.

Troubleshooting
- Use Tools → Scripts & Tests to tail cloudflared logs (dev only) and run reachability checks.
- Ensure Ghostscript is installed; see readiness and diagnostics pages if tests fail.

