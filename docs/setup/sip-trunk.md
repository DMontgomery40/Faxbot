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
6. Under the connection's inbound settings, set **SIP Transport Protocol** to **TLS**, the same encrypted connection Faxbot registers over, so Telnyx sends incoming calls down it. Leave **Encrypted Media (SRTP)** off: Telnyx does not support it with T.38.

A Telnyx trial account can only call verified numbers until you upgrade it.

### In Faxbot

| Setting | Value |
| --- | --- |
| Carrier | Telnyx |
| How Faxbot signs in | Username and password |
| Server | Leave empty to use `sip.telnyx.com` |
| Port | Leave empty to use 5061 |
| Transport | Leave as the default, **Encrypted (recommended)**. Choose **TCP** if the encrypted connection fails, and **UDP (older)** only as a last resort |
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
| Another carrier | Either | The server, port and credentials your carrier gave you | Faxbot dials numbers in E.164 with a plus sign. |

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

1. In the console, open the **Setup Wizard**, choose **SIP trunk (Asterisk)** for sending, receiving or both, and select **Next**. The first time, select **Restart now** when Setup asks. The next step shows the trunk form; **Settings** shows the same form under **Carrier SIP trunk**.
2. Choose your carrier and how Faxbot signs in. Fill in the server if the carrier asks for one, then the username and password.
3. Enter your caller ID and the fax numbers the carrier sends to this trunk.
4. Select **Save trunk settings**, then **Apply to Asterisk**, then restart the Asterisk service (for example `docker compose restart asterisk`).
5. Select **Check trunk status**. "The trunk is ready." means the carrier accepted Faxbot and answers its checks. The same check from the command line is `faxbot trunk status`.

**Check trunk status** answers in one sentence per line:

- how the carrier answered, for example "The carrier accepted Faxbot's registration over TLS." and "The carrier answered Faxbot's check in 38 ms.";
- Faxbot's internet address, which it learns itself with STUN (from the carrier's STUN server when there is one, compared with a second public STUN server), and whether your network keeps or changes port numbers on the way out;
- "No ports need to be opened or forwarded." for username and password sign-in;
- the newest call, in the same sentence Recent calls shows.

If you manage settings with an environment file instead of the console, set the `SIP_TRUNK_*` values below and run `docker compose run --rm api python -m app.sip_trunk write`, then restart Asterisk.

| Setting | Meaning |
| --- | --- |
| `SIP_TRUNK_PRESET` | `telnyx`, `signalwire`, `sinch`, `anveo`, `flowroute` or `custom` |
| `SIP_TRUNK_AUTH` | `registration` (username and password) or `ip` |
| `SIP_TRUNK_HOST`, `SIP_TRUNK_PORT`, `SIP_TRUNK_TRANSPORT` | Leave empty to use the preset's server, port and transport |
| `SIP_TRUNK_USERNAME`, `SIP_TRUNK_PASSWORD` | Carrier credentials; the password is stored as a secret. The password is read from the environment at every start, also as `TELNYX_SIP_PASSWORD` or `TELNYX_PASS` |
| `SIP_TRUNK_OUTBOUND_PROXY` | Only if the carrier asks for one |
| `SIP_TRUNK_CALLER_ID` | Your carrier-authorized number, such as `+15551234567` |
| `SIP_TRUNK_DIDS` | Your fax numbers on this trunk, separated by commas |
| `SIP_T38_ENABLED` | `true` by default |
| `SIP_FAX_PREFERENCE_HEADER` | `false` by default; see below |
| `SIP_TRUNK_CODECS` | `ulaw`, `alaw` or both; leave empty for the preset |
| `SIP_EXTERNAL_ADDRESS` | Leave empty: Faxbot finds its internet address itself. Only an override for a host whose public address you want to state |
| `SIP_PUBLIC_ADDRESS_CHECK_MINUTES` | How often Faxbot checks its internet address again; `5` by default, `0` turns it off |

Asterisk reads the trunk when it starts. Faxbot writes it to `asterisk/pjsip.conf` inside the shared fax data folder; while that file exists it replaces the older `SIP_USERNAME`, `SIP_PASSWORD` and `SIP_SERVER` settings.

### Behind a router: nothing to open

With username and password sign-in you do not open, publish or forward any port, and the default Docker Compose file publishes none. Asterisk registers with the carrier over one encrypted connection (TLS on port 5061 for Telnyx) and keeps it alive with a keepalive every 30 seconds and a carrier check every 30 seconds (every 25 seconds over UDP, with registration renewed every two minutes, inside common router timeouts). The carrier sends incoming calls back over that connection. On every call Asterisk sends the first audio and T.38 packets itself, and a small audio keepalive every two seconds when nothing else is sent, so your router lets the carrier's answer back in on the same path. Leave **Internet address** empty; it is only an override for a host whose address you want to state yourself. If you enter one that differs from what Faxbot sees, **Check trunk status** says so.

Faxbot learns its internet address with STUN when you select **Apply to Asterisk**, and again every five minutes (`SIP_PUBLIC_ADDRESS_CHECK_MINUTES`; `0` turns the repeat off). It tells the carrier that address only when your network keeps port numbers, because then the address and port are exactly right and the call does not depend on the carrier following Faxbot's packets. When your network changes port numbers, a public address with the wrong port would mislead the carrier, so Faxbot leaves it out and the carrier follows Faxbot's packets instead. Asterisk picks the address up when it starts; if your internet address changes later, **Check trunk status** says "Your internet address changed. Restart the Asterisk service so the carrier gets the new address."

Encryption also hides the call setup from router features that rewrite it (often called SIP ALG). If **Check trunk status** keeps saying Faxbot is not registered over the encrypted connection, switch **Transport** to TCP, apply again and restart Asterisk.

This works with carriers that send their media back to wherever Faxbot's packets come from, which Telnyx does for audio. Whether Telnyx does the same for T.38 data is settled by your first test fax. When a carrier does not, the call connects but no fax data arrives, and Faxbot says so on that call: "The call connected but no fax data came back from the carrier." In that case, run Faxbot's fax engine on a host with a public address, or use a cloud fax provider.

### When T.38 data does not come back: audio fax

If a call switched to T.38 and Faxbot says "The call connected but no fax data came back from the carrier.", the carrier is not sending T.38 data back to Faxbot's path, though it may still do so for audio. **Check trunk status** then offers **Use audio fax for new calls** (or run `faxbot trunk mode audio`). It turns off **Use T.38 fax over IP**, applies the trunk, and asks you to restart the Asterisk service. New calls then stay audio: Faxbot declines the carrier's switch to T.38 and sends at up to 9600 bit/s with error correction, which survives a voice path better. Faxbot never changes this by itself and never resends the failed fax; send it again when you are ready. `faxbot trunk mode t38` switches back. With Telnyx you can also set **T.38 fax re-invite initiated by** to **Disabled** for audio fax.

### Server IP sign-in needs a public host

A carrier that signs in by IP address (AnveoDirect, or Telnyx and Flowroute set to IP sign-in) sends calls to a fixed public address, which a router does not pass on. When Faxbot sees it is behind a router, **Apply to Asterisk** refuses that sign-in with "Your Faxbot runs behind a router, so sign in with a username and password; server IP sign-in needs a public address." Use it only on a host with its own public address, and start Compose with the public override, which publishes SIP and one 32-port media range that Asterisk then uses exactly:

```
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
```

See [Asterisk and SIP](sip-asterisk.md#carriers-that-sign-in-by-ip-address) for the ports. Keep the Asterisk manager port, 5038, private.

**Apply to Asterisk** also writes the inbound secret Asterisk sends with each received fax. Faxbot creates that secret when none is set, so there is nothing to choose; a secret set in **Inbound Receiving** or as `ASTERISK_INBOUND_SECRET` in `.env` is used instead. If a received fax cannot be handed to Faxbot, Recent calls says why and Faxbot brings the fax in once the cause is fixed (see [Receiving faxes](../operations/receiving.md#asterisk)).

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

### How actual charges arrive (Telnyx)

The figures above are estimates. With a Telnyx trunk, Faxbot can also read what Telnyx actually charged for each call, sent or received, and show it in **Tools → Delivery routes**, in **Job Details** and in the Inbox.

1. In the Telnyx portal, create an API key (**Account settings → Keys & credentials → API keys**).
2. Add it to `.env` and restart:

   ```env
   TELNYX_API_KEY=KEY...
   ```

   ```bash
   docker compose up -d
   ```

The key is read at every start and never shown in the console. Faxbot only reads billing records with it; it never places calls or changes your Telnyx account.

About once a minute, Faxbot asks Telnyx for the billing records (detail records) of finished calls and matches each one to its own call record:

- by the call's SIP Call-ID, which Asterisk records for every call;
- otherwise, only when exactly one call has the same numbers and was answered and ended within 45 seconds of the Telnyx record, and that record fits no other call.

A record that could fit more than one call is never guessed. It stays unmatched, the call's cost stays unknown, and Spending counts it as "could not be matched".

Telnyx usually has a call's record within minutes. Until then the fax shows "Cost not reported yet." A record without a price stays unknown, never zero. Faxbot asks again with growing gaps, checks once more a day after the call to settle the charge, and stops asking after 7 days. If Telnyx later reports a different amount, the new amount replaces the old one and both are kept. A charge never changes a fax's delivery status, and a failed call that Telnyx charged for still counts toward that fax's cost.

To ask Telnyx straight away, select **Check Telnyx charges now** under Spending, or run `faxbot routing reconcile`.

## How Faxbot records each call

For every call on the trunk Faxbot keeps a call record:

- direction, your number, the other number and the carrier preset;
- when the call started, was answered and ended, and the connected seconds;
- the result: answered, busy, network busy, no answer, failed, or not known yet;
- whether the call used T.38, how many pages moved, and the other machine's station ID;
- the reason a call or fax failed, and one sentence about what happened.

A call whose outcome Faxbot cannot confirm stays "not known yet" and is never sent again automatically.

**Recent calls** under the trunk settings lists them, newest first, with that sentence (`faxbot trunk calls` prints the same list). When a fax over the trunk fails, **Jobs** shows the same sentence for that fax; when the other fax machine was the problem, Jobs says "The other fax machine answered but the fax did not finish." and Recent calls keeps the reason. The sentences you may see:

| Sentence | What it means |
| --- | --- |
| Sent: 2 pages confirmed by the receiving machine. / Received: 2 pages. | The fax went through. |
| The call connected but no fax data came back from the carrier. | The carrier never sent fax data back to Faxbot's path; the network, not the other fax machine, failed. This is the pattern of a carrier that does not follow Faxbot's packets. |
| The call connected but no sound came back from the carrier. | The call stayed audio and not one audio packet arrived. |
| The call connected but the other end did not answer as a fax machine. | Sound came back, but no fax signal: often a person or a voice line answered. |
| The other fax machine answered but the fax failed: … | The network worked; the fax machines did not finish. The reason is the fax engine's own. |
| A fax call from +1 303 … came in, but no pages arrived. | Someone called your fax number and no page was received, so the Inbox has nothing; the Dashboard's inbound card names the newest such call from the last day. |
| The number was busy. / Nobody answered the call. | The call never connected. |

Faxbot tells "no fax data came back" apart from a fax failure by the result of the call: no page and no station ID from the other machine, and either no audio packet at all or a fax engine ending that means nothing ever arrived (a first-message timeout, or the other side hanging up first). After a switch to T.38, Asterisk no longer counts audio packets, so only the fax engine's ending decides. Delivery routes use the connected seconds to estimate what each fax cost.
