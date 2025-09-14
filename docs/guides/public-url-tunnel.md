---
layout: default
title: Create a Public URL (Tunnels)
parent: Guides
nav_order: 5
permalink: /guides/public-url-tunnel/
---

# Create a Public URL (Tunnels)

Why
- Cloud backends (Phaxio) must fetch your PDFs and post callbacks. During testing, use a tunnel to expose a local server securely.

Options
- Cloudflare Tunnel (`cloudflared`) — preferred
- ngrok — alternative

One‑command helper
- Use `scripts/setup-phaxio-tunnel.sh`
  - Finds/starts a tunnel
  - Sets `PUBLIC_API_URL` and `PHAXIO_CALLBACK_URL`
  - Restarts the API container if Docker is available

Manual steps
1) Start your tunnel to `http://localhost:8080`
2) Set `PUBLIC_API_URL` to the https tunnel URL
3) Set `PHAXIO_CALLBACK_URL` to `$PUBLIC_API_URL/phaxio-callback`
4) Apply changes in the Admin Console and verify in Diagnostics

Notes
- For production, use a real HTTPS domain and TLS termination

