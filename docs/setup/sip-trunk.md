# SIP trunk: bring your own carrier

Faxbot can send and receive ordinary faxes with its own fax engine (Asterisk with T.38) over a SIP trunk from a carrier you choose. You pay the carrier for call minutes and numbers instead of paying a fax service for each page. Senders and recipients keep using normal fax numbers; nothing changes for them.

You can use a trunk for sending only, receiving only, or both.

## Telnyx (recommended)

Telnyx documents T.38 fax on its SIP connections, so it is the carrier to start with.

### In the Telnyx portal

1. Create a SIP connection that uses **credentials** for authentication. Note its username and password.
2. Give the connection an **outbound voice profile** so it can place calls.
3. Under the connection's codecs, keep only **G.711 U** and **G.711 A**.
4. Set **T.38 fax re-invite initiated by** to **Telnyx**. When you send a fax, Telnyx switches the call to T.38 as soon as the receiving machine answers. Faxbot also works with **Customer**, but then it waits about ten seconds before switching the call itself. This option does not affect faxes you receive: Faxbot switches those calls to T.38 itself.
5. Buy or port a number, assign it to the connection, and turn on **Enable T.38 Fax Gateway** for that number.

A Telnyx trial account can only call verified numbers until you upgrade it.

### In Faxbot

| Setting | Value |
| --- | --- |
| Carrier | Telnyx |
| How Faxbot signs in | Username and password |
| Server | Leave empty to use `sip.telnyx.com` |
| Port | Leave empty to use 5060 |
| Transport | Leave as the default, UDP |
| Username and password | The connection's credentials |
| Caller ID | Your Telnyx number in international format, such as `+17205550100` |
| Fax numbers on this trunk | The same number, in the same format |
| Use T.38 fax over IP | On |

Faxbot registers with Telnyx using these credentials. Registration is what lets Telnyx deliver incoming faxes to Faxbot, so keep the username and password filled in even if you only receive. Then select **Save trunk settings**, **Apply to Asterisk**, restart the Asterisk service, and select **Check trunk status**.

## Choose a carrier

Faxbot has settings ready for these carriers. Each preset uses the carrier's own connection documentation, read on 2026-10-03.

| Carrier | How Faxbot signs in | What you enter | Notes |
| --- | --- | --- | --- |
| Telnyx | Username and password, or server IP address | Credentials from a Telnyx SIP connection | See [Telnyx (recommended)](#telnyx-recommended). |
| SignalWire | Username and password | Your space SIP domain, such as `example.sip.signalwire.com` | T.38 is not documented by the carrier; confirm it with SignalWire support and send test faxes first. SignalWire does not publish fixed signaling addresses. |
| Sinch | Username and password | Your trunk domain, such as `example.pstn.sinch.com` | T.38 is not documented by the carrier; confirm it with Sinch support and send test faxes first. Sinch asks every outgoing call for the trunk username and password; to receive, add a registered SIP endpoint with the same username and password. Sinch does not publish the addresses it sends calls from, so Faxbot does not offer IP sign-in for Sinch. |
| AnveoDirect | Server IP address only | Your server's public IP address in the AnveoDirect portal | AnveoDirect does not support registration. T.38 is not documented on its connection page; confirm it with AnveoDirect first. |
| Flowroute | Username and password, or server IP address | Credentials, or your eight-digit tech prefix for IP sign-in | Flowroute expects North American numbers as 1 plus ten digits; Faxbot formats them for you. |
| Another carrier | Either | The server, port and credentials your carrier gave you | Faxbot sends numbers exactly as they were entered. |

Carrier pages used for the presets:

- Telnyx: [sip.telnyx.com](https://sip.telnyx.com/), [voice.json](https://sip.telnyx.com/voice.json), [getting started](https://developers.telnyx.com/docs/voice/sip-trunking/get-started), [credential types](https://developers.telnyx.com/docs/voice/sip-trunking/authentication/credential-types), [caller ID policy](https://developers.telnyx.com/docs/voice/sip-trunking/configuration/caller-id-policy), [fax with T.38](https://support.telnyx.com/en/articles/1130672-fax-service-with-telnyx-via-t-38-or-g711), [IP addresses](https://developers.telnyx.com/docs/voice/sip-trunking/network-configuration/ip-whitelisting)
- SignalWire: [SIP trunking](https://signalwire.com/docs/platform/voice/sip/trunking), [bring your own carrier](https://signalwire.com/docs/platform/voice/sip/bring-your-own-carrier)
- Sinch: [Elastic SIP Trunking](https://developers.sinch.com/docs/est), [test plan](https://developers.sinch.com/docs/est/test-plan), [LiveKit guide](https://developers.sinch.com/docs/est/integration-guides/livekit), [Ribbon guide](https://developers.sinch.com/docs/est/integration-guides/ribbon-sbc)
- AnveoDirect: [FAQ](https://www.anveodirect.com/about/faq)
- Flowroute: [points of presence](https://developer.flowroute.com/docs/inbound-and-outbound-calling-with-flowroute-new-pops/), [IP authentication](https://support.bcmone.com/flowroute-support/docs/set-up-ip-based-authentication-for-outbound-calls), [faxing](https://flowroute.com/faxing/)

## What you need from the carrier

- A SIP trunk or SIP connection with T.38 fax turned on.
- Either a SIP username and password, or your server's public IP address added to the carrier account.
- At least one phone number on the account if you want to receive faxes, or to send with your own caller ID.
- A caller ID number the carrier has assigned to you or verified for you.

## Set it up

1. In the console, open **Settings**, choose **SIP/Asterisk** as the provider, and find **Carrier SIP trunk**. The setup wizard shows the same form.
2. Choose your carrier and how Faxbot signs in. Fill in the server if the carrier asks for one, then the username and password.
3. Enter your caller ID and the fax numbers the carrier sends to this trunk.
4. Select **Save trunk settings**, then **Apply to Asterisk**, then restart the Asterisk service (for example `docker compose restart asterisk`).
5. Select **Check trunk status**. "The trunk is ready." means the carrier accepted Faxbot and answers its checks.

If you manage settings with an environment file instead of the console, set the `SIP_TRUNK_*` values below and run `docker compose run --rm api python -m app.sip_trunk write`, then restart Asterisk.

| Setting | Meaning |
| --- | --- |
| `SIP_TRUNK_PRESET` | `telnyx`, `signalwire`, `sinch`, `anveo`, `flowroute` or `custom` |
| `SIP_TRUNK_AUTH` | `registration` (username and password) or `ip` |
| `SIP_TRUNK_HOST`, `SIP_TRUNK_PORT`, `SIP_TRUNK_TRANSPORT` | Leave empty to use the preset's server, port and transport |
| `SIP_TRUNK_USERNAME`, `SIP_TRUNK_PASSWORD` | Carrier credentials; the password is stored as a secret |
| `SIP_TRUNK_OUTBOUND_PROXY` | Only if the carrier asks for one |
| `SIP_TRUNK_CALLER_ID` | Your carrier-authorized number, such as `+15551234567` |
| `SIP_TRUNK_DIDS` | Your fax numbers on this trunk, separated by commas |
| `SIP_T38_ENABLED` | `true` by default |
| `SIP_FAX_PREFERENCE_HEADER` | `false` by default; see below |
| `SIP_TRUNK_CODECS` | `ulaw`, `alaw` or both; leave empty for the preset |
| `SIP_EXTERNAL_ADDRESS` | Your public IP address, only when Asterisk is behind a router or firewall |

Asterisk reads the trunk when it starts. Faxbot writes it to `asterisk/pjsip.conf` inside the shared fax data folder; while that file exists it replaces the older `SIP_USERNAME`, `SIP_PASSWORD` and `SIP_SERVER` settings.

Open UDP 5060 (or the carrier's port) and UDP 4000 to 4999 to the Asterisk host: T.38 uses 4000 to 4499 and audio uses 4500 to 4999. Keep the Asterisk manager port, 5038, private.

If Asterisk is behind a router or firewall, or runs in Docker with port publishing, enter your public IP address under **Public IP address**. Without it, the carrier is told a private address and calls connect with no fax data. On a server with a public address you can leave it empty.

**Apply to Asterisk** also saves the inbound secret from **Inbound Receiving**, so Asterisk can report received faxes to Faxbot.

## T.38

T.38 carries fax over the internet as fax data instead of as sound, which is much more reliable. Faxbot turns it on by default and uses redundancy to survive lost packets. When a fax arrives, Faxbot asks to switch the call to T.38; when it sends, it accepts the switch from the other side. If the far end cannot use T.38, the call continues as audio, which works on clean connections.

A carrier listing T.38 support does not guarantee every call completes as T.38. Faxbot records which one each call used, so you can check.

### Fax preference on outgoing calls

**Mark outgoing calls as fax when they start** adds a standard fax marker (RFC 6913) to each new outgoing call. Some carriers use it to pick a fax-capable route; others ignore it. It is off by default. It only describes the call; Faxbot never places a second call because of it, and never resends a fax whose outcome is unknown.

## Caller ID

Faxbot sends only the caller ID you enter, and you should enter only a number your carrier has assigned to you or verified for you. Carriers reject calls with numbers they have not authorized. Faxbot never presents a number you do not control.

The fax header and station ID that appear on the received pages are separate settings.

## Keep your fax number

To receive faxes on a number you already have, either:

- **Port it** to your new carrier with the carrier's porting process. Everyone keeps dialing the same number, and you stop paying the old provider once the port completes.
- **Forward it** from your current provider to a number on the new trunk. Forwarding can add a charge for the forwarded leg, and some providers bill two or three legs for one forwarded call, so check before relying on it.

For cheap number hosting and mobile numbers, see [Fax numbers on SIP](sip-numbers.md).

## What a call costs

Carriers bill fax calls like voice calls: by connected minutes, rounded up to the carrier's billing increment, plus a monthly fee for each number. There is no per-page charge on the trunk. Faxbot's starting rate cards (`config/rate_cards.json`) list each preset carrier's advertised prices, advertised on 2026-10-03:

| Carrier | Outbound per minute | Inbound per minute | Number per month | Billing increment |
| --- | --- | --- | --- | --- |
| Telnyx | from $0.005 | from $0.0032 | $1.00 | whole minutes, one-minute minimum |
| SignalWire | $0.008 | $0.0066 | $0.50 | not published |
| Sinch | not published | not published | not published | not published |
| AnveoDirect | about $0.00186, varies by number | $0.004 | $0.15, plus $0.25 setup | outbound per second; inbound not published |
| Flowroute | $0.00833 | $0.005 | $1.00 | outbound 6 seconds; inbound whole minutes |

Rounding applies to each call separately. With whole-minute billing, a 59-second call and a 61-second call are billed as three minutes, not two. A fax of 20 pages usually takes 10 to 11 minutes on the line.

Check your carrier account for your actual rates before you rely on these figures.

## How Faxbot records each call

For every call on the trunk Faxbot keeps a call record:

- direction, your number, the other number and the carrier preset;
- when the call started, was answered and ended, and the connected seconds;
- the result: answered, busy, network busy, no answer, failed, or not known yet;
- whether the call used T.38, how many pages moved, and the other machine's station ID;
- the reason a call or fax failed.

A call whose outcome Faxbot cannot confirm stays "not known yet" and is never sent again automatically.

**Recent calls** under the trunk settings lists them, newest first. Delivery routes use the connected seconds to estimate what each fax cost.
