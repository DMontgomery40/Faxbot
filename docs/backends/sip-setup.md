---
layout: default
title: SIP/Asterisk Setup
parent: Backends
nav_order: 3
permalink: /backends/sip-setup.html
---

# SIP/Asterisk Setup

## Overview
- Self-hosted backend using Asterisk and a SIP trunk with T.38 fax.
- Send-only. You don't need to accept inbound faxes to send.
- Full control, no per-fax cloud charges (you still pay your trunk provider).
- Requires some networking setup; this guide assumes minimal prior knowledge.

## What Is SIP? (Crash Course)
- SIP (Session Initiation Protocol): signaling protocol to set up calls over the internet.
- SIP Trunk: your account/connection to a carrier that places calls to the PSTN.
- DID (Direct Inward Dialing): a phone number you can buy from a SIP provider. For sending only, a DID is optional but recommended so your caller ID is valid.
- T.38: a protocol for fax over IP using UDPTL; more reliable than voice codecs for fax.
- UDPTL: the transport for T.38; you must open/forward a port range for it.
- AMI (Asterisk Manager Interface): how the API tells Asterisk to start a fax call.
- E.164: the international phone number format (e.g., `+15551234567`). Use E.164 for destinations and caller IDs when possible.

## Requirements
- A SIP trunk that supports T.38 over UDPTL
- A public/static IP or NAT configured to forward required ports (below)
- Docker and Docker Compose

## Networking
- SIP signaling: `5060/tcp+udp`
- AMI (Manager): `5038/tcp` (internal only; do not expose publicly)
- T.38 UDPTL media: `4000–4999/udp`
- If behind NAT, forward 5060 (tcp+udp) and 4000–4999/udp to the Asterisk host. Keep 5038 internal.

Tips:
- Many home routers call this "port forwarding" or "virtual servers".
- If your provider supports registration, the trunk will stay up behind NAT; still forward UDPTL.

Why port forwarding is needed (simple analogy):
- Think of your router like an office front desk. Internet calls arrive at the front desk but don't know which room (device) to go to.
- Port forwarding is the instruction to the front desk: "When fax data comes for room 4000–4999 (UDPTL), send it to the Asterisk machine."
- Without it, the fax data can't reach your server, and calls fail or time out.
- If you use a cloud VM instead of your home network, you don't need a home router—just open those ports in the VM's firewall.

How to set up port forwarding on a typical home router:
1) Find your router brand/model (sticker on the device) and log into its admin page (often 192.168.0.1 or 192.168.1.1).
2) Reserve a fixed LAN IP for the machine running Asterisk (DHCP reservation), e.g., 192.168.1.50.
3) Create port forward rules:
   - UDP 4000–4999 → 192.168.1.50
   - UDP 5060 and TCP 5060 → 192.168.1.50
4) Save and reboot if required.
5) On your server firewall, also allow those ports.

If you're on CGNAT (carrier-grade NAT) or can't port forward:
- Use a cloud VM (e.g., small Linux instance) with a public IP and open the same ports there.
- Or choose the Phaxio cloud backend instead of SIP.

## Configure Environment
Edit `.env`:
```
FAX_BACKEND=sip

# AMI (used by API to originate fax calls)
ASTERISK_AMI_HOST=asterisk
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USERNAME=api
ASTERISK_AMI_PASSWORD=change_me_safe

# SIP trunk (from your provider)
SIP_USERNAME=your_username
SIP_PASSWORD=your_password
SIP_SERVER=sip.provider.example
SIP_FROM_USER=+15551234567
SIP_FROM_DOMAIN=sip.provider.example

# Presentation
FAX_LOCAL_STATION_ID=+15551234567
FAX_HEADER=Your Org Name
```

## Start Services
```
docker compose up -d --build
```
- API on `8080`, Asterisk on `5060/udp`, `5060/tcp`, AMI on `5038`, UDPTL `4000–4999/udp`.

## How It Works
1. API converts input file to PDF, then to fax-optimized TIFF (Ghostscript).
2. API creates a job and originates a Local channel via AMI.
3. Asterisk dials your SIP trunk; on answer, executes `SendFAX()` in T.38.
4. Asterisk emits `UserEvent(FaxResult, ...)`; API updates job status.

## Logs & Debugging
- API: `docker compose logs -f api`
- Asterisk: `docker compose logs -f asterisk`
- Inside Asterisk shell: `docker exec -it <asterisk_container> asterisk -rvvv`
  - Check module load: `module show like fax`
  - Call flow: watch for `SendFAX` and `FaxResult` events

## Common Pitfalls
- T.38 disabled at provider → enable UDPTL and verify `udptl.conf` range.
- NAT issues → enable `rtp_symmetric`, `force_rport`, correct `match` and `from_domain`.
- Wrong credentials → check `pjsip.conf` generated from templates (envsubst in `start.sh`).
- Ghostscript missing → API warns and stubs conversion; install for production.
- CGNAT / no port forwarding → use a cloud VM or Phaxio backend.

## Test Send
```
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: your_secure_api_key" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```

## Choosing a SIP Provider (T.38)
Pick one of these two (beginner-friendly):

1) Telnyx — developer‑friendly, good for small deployments
- Self‑serve signup, clear portal
- T.38 support documented; confirm for your region and use case
- HIPAA/BAA: contact Telnyx to sign a BAA before handling PHI
- Links: Telnyx Voice/Numbers pricing, HIPAA resources
  - https://telnyx.com/

2) Flowroute — flexible SIP trunking for small scale
- Self‑serve signup, good DID inventory
- T.38 support documented; confirm with support for your route
- HIPAA/BAA: contact Flowroute to discuss BAA before handling PHI
- Link: https://flowroute.com/pricing-details/

Notes:
- Always ask your provider to confirm T.38 support and sign a BAA if you'll transmit PHI.
- Typical US costs (ballpark): local DID ~$0.5–$2/mo; outbound ~$0.005–$0.02/min. Verify current pricing pages.

## TLS Signaling & VPN Examples (Advanced)

### PJSIP TLS Transport (example)
```
[transport-tls]
type=transport
protocol=tls
bind=0.0.0.0:5061
method=tlsv1_2
local_net=10.0.0.0/8
cert_file=/etc/asterisk/keys/asterisk.pem
priv_key_file=/etc/asterisk/keys/asterisk.key
ca_list_file=/etc/asterisk/keys/ca.crt
external_media_address=<public_ip>
external_signaling_address=<public_ip>
```

Then reference `transport=transport-tls` in your trunk endpoint/registration if your provider supports TLS.

### Site‑to‑Site VPN (WireGuard sketch)
- Provision a WireGuard tunnel between your Asterisk host and the SIP provider's VPN endpoint.
- Route provider IP ranges through the WG interface; restrict firewall to permit SIP/T.38 only via the tunnel.
- Example (Asterisk side `/etc/wireguard/wg0.conf`):
```
[Interface]
PrivateKey = <your_private_key>
Address = 10.7.0.2/32

[Peer]
PublicKey = <provider_public_key>
Endpoint = <provider_vpn_host>:51820
AllowedIPs = <provider_sip_subnets>
PersistentKeepalive = 25
```

Restart Asterisk with `external_*` addresses set to the tunnel's public IP if required.

## Understanding the Asterisk Configuration
- `asterisk/etc/asterisk/templates/pjsip.conf.template` is rendered from your `.env` at container start.
- `asterisk/etc/asterisk/templates/manager.conf.template` uses `${ASTERISK_AMI_USERNAME}` as the user section and `${ASTERISK_AMI_PASSWORD}` for the secret. Ensure these match your API env so AMI auth succeeds.
- The `faxout` dialplan in `extensions.conf` uses `SendFAX()` with T.38 and emits `UserEvent(FaxResult)` on completion.
- The API listens for that event via AMI to update job status.

## Minimal Telephony Glossary
- SIP: signaling protocol for VoIP calls.
- SIP Trunk: your carrier connection for inbound/outbound PSTN calls.
- DID: a phone number; optional for send-only but helpful for caller ID.
- T.38: fax-over-IP protocol (preferred over G.711 for reliable faxing).
- UDPTL: transport used by T.38; requires UDP port range open.
- AMI: Asterisk Manager Interface; API uses it to originate calls and receive events.
