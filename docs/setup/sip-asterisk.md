# Asterisk and SIP

## Overview
- Faxbot's own fax engine: Asterisk sends and receives faxes over a SIP trunk from your carrier, with T.38 fax over IP.
- No per-fax cloud charges; your carrier bills the calls by the minute.
- No router changes: you do not open, publish or forward any port. [Carrier SIP trunk](sip-trunk.md) covers choosing and setting up the trunk in the console.
- Receiving: see [Receiving faxes](../operations/receiving.md#asterisk). The dialplan reports each fax with `ASTERISK_INBOUND_SECRET`, and the fax image must be inside the data folder (`/faxdata/inbound/`).

## What Is SIP? (Crash Course)
- SIP (Session Initiation Protocol): the signaling that sets up calls over the internet.
- SIP trunk: your account with a carrier that connects calls to the telephone network.
- DID: a phone number on that account. Faxes to it reach Faxbot; it is also your caller ID.
- T.38: fax over IP, carried in UDPTL packets; more reliable than fax over a voice codec.
- AMI (Asterisk Manager Interface): how the Faxbot API starts fax calls and hears their results.
- E.164: the international number format, for example `+15551234567`.

## Requirements
- A SIP trunk that supports T.38 (Telnyx is the tested preset).
- Docker and Docker Compose.
- Nothing else on your network: no public address, no port forwarding, no firewall rule for incoming traffic.

## Networking: nothing to open

Faxbot behaves like a phone behind a router, not like a server:

- **Signaling.** Asterisk registers with the carrier using your SIP username and password, over one encrypted (TLS) connection by default, and keeps that connection alive with keepalives and regular carrier checks. The carrier sends incoming calls back over the same connection, so nothing has to reach in.
- **Address.** Faxbot learns its own internet address with STUN; nobody types it. **Check trunk status** shows the address and whether your network keeps or changes port numbers. When it keeps them, Asterisk advertises that exact address from its next start, with its own container subnet as the local network (the image includes `iproute2` to read it); when it changes them, Asterisk advertises nothing extra and the carrier follows Faxbot's packets.
- **Fax data.** On every call, in both directions, Asterisk sends the first audio and T.38 packets itself. Your router then lets the carrier's answer back in on the same path. Carriers built for this (Telnyx is one) send their media wherever Faxbot's packets come from.
- **Docker.** The default `docker-compose.yml` publishes no SIP or media port. Inside the container Asterisk uses UDP 4000 to 4499 for T.38 and 4500 to 4999 for audio, but only for packets it starts. The manager port, 5038, stays private on the Compose network.

Faxbot's loopback proof (`make native-proof`) shows that both directions work when Faxbot's address in the call setup cannot be reached, as long as the carrier sends its media back to the path Faxbot's packets came from. It also runs Faxbot behind a router container that hides it the way a home router does: Faxbot registers through the router over UDP or TCP, the stand-in carrier sends its incoming call down that registration, and a two-page T.38 fax goes through in both directions, including through a router that changes port numbers. If your carrier only sends media to the address in the call setup, the call connects but no fax data arrives. Advertising the exact internet address does not reliably fix that: when the carrier's first packet reaches your router before Faxbot's, the router can give Faxbot's own flow a different port. Faxbot then shows "The call connected but no fax data came back from the carrier." for that call. In that case, run Faxbot's fax engine on a host with a public address, or use a cloud fax provider.

### Carriers that sign in by IP address

AnveoDirect, and Telnyx or Flowroute when set to IP sign-in, send calls to a fixed public address, which a router does not pass on. Use them only on a host with its own public address. Start Compose with the public override:

```
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
```

It publishes SIP on 5060 (UDP and TCP) and 5061 (TCP), plus one 32-port media range, 4000 to 4031 UDP. Asterisk then uses exactly that range: the first third for T.38 and the rest for audio, enough for about ten calls at once. The range stays small because Docker starts one helper process per published port, and a 1000-port range used up a 4 GB host. Open the same ports in the host firewall.

## Configure API and Asterisk separately

On an existing installation, select SIP for the desired outbound direction and edit AMI/station/header fields in Settings. Apply with the loaded revision, complete any pending installation-wide restart and confirm active identity. These canonical API values are not replaced by later `.env` edits.

The trunk itself is set up on the **Carrier SIP trunk** screen: **Apply and connect** writes the trunk file Asterisk loads at start and, in the Docker Compose install, restarts Asterisk to load it once no call is up (see [Carrier SIP trunk](sip-trunk.md)). For first bootstrap, the API names below can also be supplied in `.env`:
```
FAX_BACKEND=sip

# AMI (used by API to originate fax calls)
ASTERISK_AMI_HOST=asterisk
ASTERISK_AMI_PORT=5038
ASTERISK_AMI_USERNAME=api
ASTERISK_AMI_PASSWORD=change_me_safe

# Presentation
FAX_LOCAL_STATION_ID=+15551234567
FAX_HEADER=Your Org Name
```

## Start Services
```
docker compose up -d --build
```
- The API listens on `8080`. Asterisk publishes nothing; the API reaches its manager port over the Compose network.

## How It Works
1. API converts input file to PDF, then to fax-optimized TIFF (Ghostscript).
2. Durable acceptance records a ready or held job and its immutable configuration; held work is never automatically dispatched.
3. An issued SIP attempt originates directly to `PJSIP/<destination>@trunk-endpoint`; on answer the dedicated `faxbot-send` context runs `SendFAX()` without another `Dial`.
4. The hangup handler emits a result with both JobID and AttemptID. AMI acceptance is not delivery; the matching issued attempt and original captured provider must authenticate the result. Real trunk fax delivery remains an operational check.

## Logs & Debugging
- API: `docker compose logs -f api`
- Asterisk: `docker compose logs -f asterisk`
- Inside Asterisk shell: `docker exec -it <asterisk_container> asterisk -rvvv`
  - Check module load: `module show like fax`
  - Registration: `pjsip show registrations`
  - Call flow: watch for `SendFAX` and `FaxResult` events; `pjsip set logger on` shows the SIP messages.

## Common Pitfalls
- T.38 disabled at the carrier → turn on the carrier's T.38 gateway for your number.
- Calls connect but no fax data arrives → the carrier did not send its media back to Faxbot's path. Use a host with a public address, or a cloud fax provider.
- Wrong credentials → **Check trunk status** on the Carrier SIP trunk screen says the carrier rejected the username or password.
- Ghostscript missing → required conversion fails honestly; install it before submitting. No stub or placeholder counts as prepared fax content.

## Test Send
```bash
curl -X POST http://localhost:8080/fax \
  -H "X-API-Key: your_secure_api_key" \
  -F to=+15551234567 \
  -F file=@./example.pdf
```

=== "Console"

    - Open Admin Console → Send Fax  
    - Enter a valid E.164 number; attach a small PDF  
    - Watch Jobs for status; see Asterisk logs if calls fail

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
- Always ask your provider to confirm T.38 support and sign a BAA if you’ll transmit PHI.
- Typical US costs (ballpark): local DID ~$0.5–$2/mo; outbound ~$0.005–$0.02/min. Verify current pricing pages.

## Understanding the Asterisk Configuration
- Faxbot renders the whole trunk (`pjsip.conf`) from its settings; **Apply and connect** writes it to `<FAX_DATA_DIR>/asterisk/pjsip.conf` and Asterisk loads it at start. At each start Asterisk also records the trunk it loaded (`pjsip.conf.started`) and the time it started (`engine-started`) in that folder, so Faxbot can tell whether a restart is needed and that it may restart this Asterisk. Without a trunk, Asterisk starts offline with no registration.
- `asterisk/etc/asterisk/templates/manager.conf.template` uses `${ASTERISK_AMI_USERNAME}` as the user section and `${ASTERISK_AMI_PASSWORD}` for the secret. Ensure these match the API’s active canonical AMI credentials. Manager access is disabled when Asterisk deployment AMI credentials are omitted; supplying both renders the account.
- When the passwords differ, Faxbot still starts so you can fix it from the console. The dashboard, Settings, readiness (`/health/ready`), Diagnostics and **Check trunk status** say "Faxbot can't sign in to its fax engine. Check that the Asterisk manager password matches.", new faxes are refused with that sentence (held test faxes are still accepted), and Faxbot keeps trying to sign in. When Asterisk is not running at all, the sentence is "Faxbot can't reach its fax engine. Check that the Asterisk service is running."
- `modules.conf` skips modules a fax server does not use, and small files such as `cdr.conf` and `pjproject.conf` stand in for configuration those modules would otherwise report missing, so a healthy start logs no ERROR lines. An ERROR in the Asterisk log means something needs attention, such as a received fax that could not be handed to Faxbot.
- The dedicated `faxbot-send` context executes `SendFAX()` and emits the terminal result from its hangup handler. The older `faxout` context remains for compatibility; the new originate path does not use it.
- The API listens for that event via AMI to update job status.

## Minimal Telephony Glossary
- SIP: signaling protocol for VoIP calls.
- SIP Trunk: your carrier connection for inbound/outbound PSTN calls.
- DID: a phone number; also your caller ID.
- T.38: fax-over-IP protocol (preferred over G.711 for reliable faxing).
- UDPTL: the packets T.38 travels in; Asterisk sends them first, so no port has to be opened.
- AMI: Asterisk Manager Interface; API uses it to originate calls and receive events.
