# SIP trunk: bring your own carrier

Faxbot can send and receive ordinary faxes with its own fax engine (Asterisk with T.38) over a SIP trunk from a carrier you choose. You pay the carrier for call minutes and numbers instead of paying a fax service for each page. Senders and recipients keep using normal fax numbers; nothing changes for them.

You can use a trunk for sending only, receiving only, or both.

If your office has an Avaya phone system, Faxbot can fax through it instead of through its own carrier account: see [Avaya IP Office and Aura](avaya.md). For the UK and Australia, see [United Kingdom](#united-kingdom) and [Australia](#australia).

## Telnyx (recommended)

Telnyx documents T.38 fax on its SIP connections, so it is the carrier to start with.

### In the Telnyx portal

1. Create a SIP connection that uses **credentials** for authentication. Note its username and password: Faxbot needs the **SIP connection's** password (connection → **Authentication and routing**), not your Telnyx account password. A wrong one shows as "Telnyx refused the username or password. Use the SIP connection's password, not your Telnyx account password." in **Check trunk status**.
2. Give the connection an **outbound voice profile** so it can place calls.
3. Under the connection's codecs, keep only **G.711 U** and **G.711 A**.
4. Set **T.38 fax re-invite initiated by** to **Telnyx**. When you send a fax, Telnyx switches the call to T.38 as soon as the receiving machine answers. Faxbot also works with **Customer**, but then it waits about ten seconds before switching the call itself. This option does not affect faxes you receive: Faxbot switches those calls to T.38 itself.
5. Buy or port a number, assign it to the connection, and turn on **Enable T.38 Fax Gateway** for that number.
6. Leave **Encrypted Media (SRTP)** off: Telnyx does not support it with T.38. A credential connection has no inbound transport to choose: Telnyx sends incoming calls down the encrypted connection Faxbot registers over.

A Telnyx trial account can only call verified numbers until you upgrade it.

### In Faxbot

| Setting | Value |
| --- | --- |
| Carrier | Telnyx |
| How Faxbot signs in | Username and password |
| Server | Leave empty to use `sip.telnyx.com` |
| Port | Leave empty to use 5061 |
| Transport | Leave as the default, **Default: Encrypted (TLS)**. Choose **TCP** if the encrypted connection fails, and **UDP (older)** only as a last resort |
| Username and password | The SIP connection's credentials (connection → Authentication and routing), not your Telnyx account login |
| Caller ID | Your Telnyx number in international format, such as `+17205550100` |
| Fax numbers on this trunk | The same number, in the same format |
| Use T.38 fax over IP | On |

Faxbot registers with Telnyx using these credentials. Registration is what lets Telnyx deliver incoming faxes to Faxbot, so keep the username and password filled in even if you only receive. Then select **Apply and connect**; Faxbot saves the form, restarts Asterisk with the trunk and shows the trunk check.

## Choose a carrier

Faxbot has settings ready for these carriers. Each preset uses the carrier's own connection documentation, read on 2026-10-03.

| Carrier | How Faxbot signs in | What you enter | Notes |
| --- | --- | --- | --- |
| Telnyx | Username and password, or server IP address | Credentials from a Telnyx SIP connection | See [Telnyx (recommended)](#telnyx-recommended). |
| SignalWire | Username and password | Your space SIP domain, such as `example.sip.signalwire.com` | T.38 is not documented by the carrier; confirm it with SignalWire support and send test faxes first. SignalWire does not publish fixed signaling addresses. |
| Sinch | Username and password | Your trunk domain, such as `example.pstn.sinch.com` | T.38 is not documented by the carrier; confirm it with Sinch support and send test faxes first. Sinch asks every outgoing call for the trunk username and password; to receive, add a registered SIP endpoint with the same username and password. Sinch does not publish the addresses it sends calls from, so Faxbot does not offer IP sign-in for Sinch. |
| AnveoDirect | Server IP address only | Your server's public IP address in the AnveoDirect portal | AnveoDirect does not support registration. T.38 is not documented on its connection page; confirm it with AnveoDirect first. |
| Flowroute | Username and password, or server IP address | Credentials, or your eight-digit tech prefix for IP sign-in | Flowroute expects North American numbers as 1 plus ten digits; Faxbot formats them for you. |
| Gamma (UK) | Server IP address only | The SIP server address your Gamma reseller gives you; give them your static public address | Gamma lists T.38, and a phone system maker tested T.38 fax over it. See [United Kingdom](#united-kingdom). |
| BT One Voice (UK) | Server IP address only | The BT SIP server address from your turn-up sheet; give BT your static public address and port | BT turns T.38 into audio fax inside its network, so a new BT trunk starts with audio fax. See [United Kingdom](#united-kingdom). |
| Telstra SIP Connect (Australia) | Username and password, over TCP | The SIP domain from your Telstra order as the server, Telstra's SBC address as the outbound proxy | Telstra does not state T.38 support. See [Australia](#australia). |
| Another carrier | Either | The server, port and credentials your carrier gave you | Faxbot dials numbers in E.164 with a plus sign. |

Gamma, BT One Voice and Telstra SIP Connect offer **Number format**: **International, with + and the country code**, or **As a phone here dials it** (`01632960123` in the UK), for a carrier that wants national numbers. Their settings are tested against Faxbot's own Asterisk standing in for the carrier; none has been tested on a live trunk yet.

Carrier pages used for the presets:

- Telnyx: [sip.telnyx.com](https://sip.telnyx.com/), [voice.json](https://sip.telnyx.com/voice.json), [getting started](https://developers.telnyx.com/docs/voice/sip-trunking/get-started), [credential types](https://developers.telnyx.com/docs/voice/sip-trunking/authentication/credential-types), [caller ID policy](https://developers.telnyx.com/docs/voice/sip-trunking/configuration/caller-id-policy), [fax with T.38](https://support.telnyx.com/en/articles/1130672-fax-service-with-telnyx-via-t-38-or-g711), [IP addresses](https://developers.telnyx.com/docs/voice/sip-trunking/network-configuration/ip-whitelisting)
- SignalWire: [SIP trunking](https://signalwire.com/docs/platform/voice/sip/trunking), [bring your own carrier](https://signalwire.com/docs/platform/voice/sip/bring-your-own-carrier)
- Sinch: [Elastic SIP Trunking](https://developers.sinch.com/docs/est), [test plan](https://developers.sinch.com/docs/est/test-plan), [LiveKit guide](https://developers.sinch.com/docs/est/integration-guides/livekit), [Ribbon guide](https://developers.sinch.com/docs/est/integration-guides/ribbon-sbc)
- AnveoDirect: [FAQ](https://www.anveodirect.com/about/faq)
- Flowroute: [points of presence](https://developer.flowroute.com/docs/inbound-and-outbound-calling-with-flowroute-new-pops/), [IP authentication](https://support.bcmone.com/flowroute-support/docs/set-up-ip-based-authentication-for-outbound-calls), [faxing](https://flowroute.com/faxing/)
- Gamma: [Swyx interoperability sheet](https://service.swyx.net/hc/en-gb/articles/360010513919-SIP-Provider-Gamma-Telecom-UK), [Yeastar UK provider list](https://www.yeastar.com/itsp-partners/united-kingdom/), [SIP trunking](https://gammagroup.co/products/sip-trunking-call-management/)
- BT One Voice: [technical outline](https://www.globalservices.bt.com/static/assets/pdf/products/one_voice_sip_trunking/One_Voice_SIP_trunking_technical_Outline.pdf), [UK datasheet](https://www.globalservices.bt.com/static/assets/pdf/data_sheets/Product/one_voice_sip/bt_one_voice_sip_trunk_uk_datasheet.pdf)
- Telstra SIP Connect: [3CX setup guide](https://www.3cx.com/docs/sip-trunk/telstra-sip-connect-australia/), [Our Customer Terms, SIP Connect section](https://www.telstra.com.au/content/dam/tcom/personal/consumer-advice/pdf/business-a-full/sip-connect.pdf)

## United Kingdom

Openreach says "By 31 January 2027, all traditional phone lines will be going digital". Its [switch-off page](https://www.openreach.com/upgrading-the-UK-to-digital-phone-lines) lists alarms, CCTV, payment terminals, telecare devices and lift phones as things to check (read 3 October 2026). BT is plainer about fax:

- On **BT Digital Voice**, "Generally, fax is supported ... although it's not 100% guaranteed", and BT suggests sending no more than 10 pages at a time ([BT help](https://www.bt.com/help/landline/digital-voice--will-my-existing-phone-and-fax-machine-still-work)).
- On **BT Cloud Voice Express**, "some analogue devices which use a traditional phone line may no longer work, including: Tills, EPOS, Oyster, Fax machines", and "BT is not able to guarantee that all analogue devices will work with an ATA". BT advises "Changing to digital alternatives" ([BT Business help](https://business.bt.com/help/guides/getting-started-with-your-bt-business-products/using-your-cloud-voice-express-phone-service/)).

Faxbot is that digital alternative. Keep your fax numbers, and fax over a SIP trunk that carries T.38 or over a cloud fax provider, instead of a fax machine on a digital line.

| BT product | Use with Faxbot |
| --- | --- |
| **BT One Voice SIP Trunk** | The **BT One Voice** preset. BT recognises Faxbot by its address and port, with no registration, so Faxbot needs a host with a static public address (see [Server IP sign-in](#server-ip-sign-in-needs-a-public-host)). BT's technical outline says "T.38 Fax over IP is internally transcoded to Fax via G.711 pass-through", so a new BT trunk starts with audio fax and the switch says "Off: BT One Voice turns T.38 into audio fax inside its network, so Faxbot uses audio fax." **Try T.38 again** is there if you want it. A-law first in the UK. |
| **BT Cloud Voice SIP-T** | **Another carrier**, signing in with the username and password BT gives you. BT's [connectivity guide](https://business.bt.com/content/dam/bt-business/pdfs/help-and-support/phone-systems/connecting-phone-systems-direct-to-cloud-voice-sip/cv-sip-native-connectivity-customer-guide.pdf) says nothing about fax or T.38, so send test faxes first. |
| **BT Cloud Voice, Cloud Voice Express, Digital Voice, Cloud Work** | Not fax products, by BT's own account. Use a T.38 trunk (Gamma, or Telnyx with UK numbers) or a cloud fax provider such as eFax instead. |

**Gamma** is the main UK wholesale SIP provider and sells through resellers. It recognises the fax server by its public address (no registration). Its codecs include "T.38 for FAX Negotiation", and a phone system maker tested "T.38 Negotiation and FAX transmission" over it ([Swyx, updated 17 June 2024](https://service.swyx.net/hc/en-gb/articles/360010513919-SIP-Provider-Gamma-Telecom-UK)). Choose the **Gamma** preset, enter the SIP server address your reseller gives you, and give them your static public address. Yeastar's UK list also marks DIDlogic, Fuse2 and Sona for T.38; use **Another carrier** for those. **Telnyx** has UK numbers and T.38, and works from behind a router with no open ports, which makes it the quickest UK start.

No published price was found for Gamma, BT One Voice or Telstra SIP Connect, so their rate cards carry none: Spending says "No published price; add your rate" until you enter your own rate under **Tools → Delivery routes**. Gamma's own page gives only a range: £3 to £150 a month for each SIP channel, plus £50 to £150 a month service rental.

## Australia

- **Telstra SIP Connect**: the **Telstra SIP Connect** preset registers with a username and password over TCP.
  - Enter the SIP domain from your Telstra order as the server, Telstra's SBC address as **Outbound proxy**, and the authentication user ID and password Telstra gives you. These settings follow [3CX's Telstra guide](https://www.3cx.com/docs/sip-trunk/telstra-sip-connect-australia/).
  - Telstra does not state T.38 support anywhere Faxbot could read, so T.38 stays on and Faxbot switches new calls to audio fax by itself if T.38 fax data does not come back.
  - Telstra's customer terms say the charges "are set out in your application form", so no price is published.
- **Telnyx** has Australian numbers and is the quickest start, as in the UK.
- Yeastar's [Australian provider list](https://www.yeastar.com/itsp-partners/australia/) marks Aatrox Communications and Binary Elements for T.38, both with registration. Use **Another carrier** for them.

A-law comes first for UK and Australian installations; an Avaya phone system preset picks the order from the installation country.

## What you need from the carrier

- A SIP trunk or SIP connection with T.38 fax turned on.
- Either a SIP username and password, or your server's public IP address added to the carrier account.
- At least one phone number on the account if you want to receive faxes, or to send with your own caller ID.
- A caller ID number the carrier has assigned to you or verified for you.

## Set it up

1. In the console, open the **Setup Wizard**, choose **SIP trunk (Asterisk)** for sending, receiving or both, and select **Next**. The first time, select **Restart now** when Setup asks. The next step shows the trunk form; **Settings** shows the same form under **Carrier SIP trunk**.
2. Choose your carrier and how Faxbot signs in. The screen says which directions the trunk carries; a trunk that only receives needs no caller ID. Server, port and transport show the carrier's values in force (for example `sip.telnyx.com`, `5061`, **Default: Encrypted (TLS)**) until you type your own. Fill in the server if the carrier asks for one, then the username and password.
3. Enter your caller ID and the fax numbers the carrier sends to this trunk.
4. Select **Apply and connect**. Faxbot saves what you typed, writes the trunk for Asterisk, restarts Asterisk to load it and keeps checking ("Checking the carrier…") until the carrier answers Faxbot's check, for up to a minute, then shows the trunk check on the same screen: the transport Faxbot registered over, how quickly the carrier answers its checks, Faxbot's internet address and "No ports need to be opened or forwarded." From the command line, `faxbot providers trunk apply` does the same.
   When Asterisk already runs exactly these settings, nothing restarts and the result says "Saved. Asterisk already uses these settings." Until a trunk is set up for each direction that uses it, Faxbot's readiness and the Dashboard say "No SIP trunk is set up. Choose your carrier to start." (or "Some trunk settings are missing.").
5. **Check trunk status** repeats the check at any time. "The trunk is ready." means the carrier accepted Faxbot and answers its checks. The same check from the command line is `faxbot providers trunk status`.

### What Apply and connect does with Asterisk

In the Docker Compose install, Asterisk shares Faxbot's data folder and Docker starts it again whenever it stops, so Faxbot restarts it to load the trunk; nothing has to be typed on the server. Faxbot restarts it only when it is not already running exactly these settings, and only when no call is up: during a call, Apply and connect says "Saved. A call is in progress, so Asterisk keeps its current settings until you apply again after it ends." and changes nothing else. Faxbot restarts rather than reloads because a running Asterisk never reloads its transport settings (protocol, port, internet address), and a password change alone does not make it register again.

When Asterisk runs elsewhere and does not share Faxbot's data folder, Apply and connect writes the files and says "Saved for Asterisk. Restart the Asterisk service to use these settings." An Asterisk set up before this release may answer "Asterisk does not let Faxbot restart it yet"; restart the Asterisk service once and Faxbot can restart it from then on.

The behaviour rests on Asterisk 22 itself (read 2026-10-04 UTC): the manager `Command` action needs the `command` permission and `CoreShowChannels` the `system` or `reporting` permission (`main/manager.c`); `core stop gracefully` "Causes Asterisk to not accept new calls, and exit when all active calls have terminated normally" (`main/asterisk.c`); a transport is "not fully reloadable, not reloading: protocol, bind, TLS ..." unless `allow_reload` is set (`res/res_pjsip/config_transport.c`); and an outbound registration registers again on reload only when the registration itself changed (`res/res_pjsip_outbound_registration.c`), all at https://github.com/asterisk/asterisk/tree/22 . The actions are described at https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Actions/Command/ and https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Actions/PJSIPRegister/ .

**Check trunk status** answers in one sentence per line:

- how the carrier answered, for example "The carrier accepted Faxbot's registration over TLS." and "The carrier answered Faxbot's check in 38 ms.";
- Faxbot's internet address, which it learns itself with STUN (from the carrier's STUN server when there is one, compared with a second public STUN server), and whether your network keeps or changes port numbers on the way out;
- "No ports need to be opened or forwarded." for username and password sign-in;
- the newest call, in the same sentence Recent calls shows.

If you manage settings with an environment file instead of the console, set the `SIP_TRUNK_*` values below and run `docker compose run --rm api python -m app.sip_trunk write`, then restart Asterisk.

| Setting | Meaning |
| --- | --- |
| `SIP_TRUNK_PRESET` | `telnyx`, `signalwire`, `sinch`, `anveo`, `flowroute`, `gamma`, `bt-one-voice`, `telstra-sip-connect`, `avaya-ipoffice`, `avaya-aura` or `custom` |
| `SIP_TRUNK_AUTH` | `registration` (username and password) or `ip` |
| `SIP_TRUNK_HOST`, `SIP_TRUNK_PORT`, `SIP_TRUNK_TRANSPORT` | Leave empty to use the preset's server, port and transport |
| `SIP_TRUNK_USERNAME`, `SIP_TRUNK_PASSWORD` | Carrier credentials; the password is stored as a secret. The password is read from the environment at every start, also as `TELNYX_SIP_PASSWORD` or `TELNYX_PASS` |
| `SIP_TRUNK_OUTBOUND_PROXY` | Only if the carrier asks for one |
| `SIP_TRUNK_CALLER_ID` | Your carrier-authorized number, such as `+15551234567` |
| `SIP_TRUNK_DIDS` | Your fax numbers on this trunk, separated by commas |
| `SIP_T38_ENABLED` | `true` by default |
| `SIP_FAX_PREFERENCE_HEADER` | `true` by default; see below |
| `SIP_TRUNK_CODECS` | `ulaw`, `alaw` or both; leave empty for the preset (for Avaya and BT One Voice, the order the installation country uses) |
| `SIP_TRUNK_DIAL_FORMAT` | `e164` (`+441632960123`) or `local` (`01632960123`, as a phone at the installation dials it), where the preset offers the choice |
| `SIP_TRUNK_DIAL_PREFIX` | Up to four digits before a number dialled the local way, such as `9` for a phone system's outside line |
| `SIP_EXTERNAL_ADDRESS` | Leave empty: Faxbot finds its internet address itself. Only an override for a host whose public address you want to state |
| `SIP_PUBLIC_ADDRESS_CHECK_MINUTES` | How often Faxbot checks its internet address again; `5` by default, `0` turns it off. Read at the first start; after that it is a setting (see below) |

Asterisk reads the trunk when it starts. Faxbot writes it to `asterisk/pjsip.conf` inside the shared fax data folder; while that file exists it replaces the older `SIP_USERNAME`, `SIP_PASSWORD` and `SIP_SERVER` settings.

### Behind a router: nothing to open

With username and password sign-in you do not open, publish or forward any port, and the default Docker Compose file publishes none. Asterisk registers with the carrier over one encrypted connection (TLS on port 5061 for Telnyx) and keeps it alive with a keepalive every 30 seconds and a carrier check every 30 seconds (every 25 seconds over UDP, with registration renewed every two minutes, inside common router timeouts). The carrier sends incoming calls back over that connection. On every call Asterisk sends the first audio and T.38 packets itself, and a small audio keepalive every two seconds when nothing else is sent, so your router lets the carrier's answer back in on the same path. Leave **Internet address** empty; it is only an override for a host whose address you want to state yourself. If you enter one that differs from what Faxbot sees, **Check trunk status** says so.

Faxbot learns its internet address with STUN when you select **Apply and connect**, and again every five minutes. The interval is a setting: **Check the internet address every … minutes** under Providers → Carrier trunk in the console, or `faxbot system settings set sip_public_address_check_minutes=10`. `0` turns the repeat off, and a change applies from the next check without a restart. It tells the carrier that address only when your network keeps port numbers, because then the address and port are exactly right and the call does not depend on the carrier following Faxbot's packets. When your network changes port numbers, a public address with the wrong port would mislead the carrier, so Faxbot leaves it out and the carrier follows Faxbot's packets instead. Asterisk picks the address up when it starts; if your internet address changes later, **Check trunk status** says "Your internet address changed. Select Apply and connect so the carrier gets the new address." (or, for an Asterisk Faxbot does not manage, to restart the Asterisk service).

Encryption also hides the call setup from router features that rewrite it (often called SIP ALG). If **Check trunk status** keeps saying Faxbot is not registered over the encrypted connection, switch **Transport** to TCP, apply again and restart Asterisk.

This works with carriers that send their media back to wherever Faxbot's packets come from, which Telnyx does for audio. Whether Telnyx does the same for T.38 data is settled by your first test fax; once a call has shown it, **Check trunk status** says what it showed instead. When a carrier does not, the call connects but no fax data arrives, and Faxbot says so on that call: "The call connected but no fax data came back from the carrier." In that case, run Faxbot's fax engine on a host with a public address, or use a cloud fax provider.

### When T.38 data does not come back: audio fax

If a call switched to T.38 and no fax data came back ("The call connected but no fax data came back from the carrier."), the carrier is not sending T.38 data back to Faxbot's path, though it may still do so for audio. Faxbot then switches new calls to audio fax by itself, but only when the fax engine timed out waiting for the other side's first fax message (a plain hang-up, a busy line or the other side hanging up never switches it): it turns off **Use T.38 fax over IP** (saved by "system"), connects the trunk again once no call is up, and says next to the switch "Off: on 3 October a T.38 fax got no fax data back on this network, so Faxbot uses audio fax." with **Try T.38 again**. It never resends the failed fax; send it again when you are ready. When T.38 is off and the most recent T.38 call got no fax data back (for example, someone turned T.38 off by hand right after such a call), Faxbot takes that call as the reason and says so the same way. New calls stay audio: Faxbot declines the carrier's switch to T.38 and sends at up to 9600 bit/s with error correction, which survives a voice path better.

A new Telnyx trunk on a network that changes port numbers starts with audio fax for the same reason, because Telnyx's T.38 data was seen not to come back through such a network; the switch says "Off: your network changes port numbers, and Telnyx's T.38 fax data does not come back through such networks, so Faxbot uses audio fax." Once you choose T.38 yourself (**Try T.38 again**, the switch, or `faxbot providers trunk mode t38`), Faxbot leaves your choice alone until another T.38 call gets no fax data back. `faxbot providers trunk mode audio` turns audio fax on by hand, and `faxbot providers trunk status` says why audio fax is in use. With Telnyx you can also set **T.38 fax re-invite initiated by** to **Disabled** for audio fax.

### Server IP sign-in needs a public host

A carrier that signs in by IP address (AnveoDirect, Gamma, BT One Voice, or Telnyx and Flowroute set to IP sign-in) sends calls to a fixed public address, which a router does not pass on. When Faxbot sees it is behind a router, **Apply and connect** refuses that sign-in with "Your Faxbot runs behind a router, so sign in with a username and password; server IP sign-in needs a public address." Use it only on a host with its own public address, and start Compose with the public override, which publishes SIP and one 32-port media range that Asterisk then uses exactly:

```
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d
```

See [Asterisk and SIP](sip-asterisk.md#carriers-that-sign-in-by-ip-address) for the ports. Keep the Asterisk manager port, 5038, private.

**Check trunk status** says "Received faxes reach Faxbot: ready." when the trunk receives and the inbound secret Faxbot keeps is written where Asterisk reads it. **Apply and connect** also writes the inbound secret Asterisk sends with each received fax. Faxbot creates that secret when none is set, so there is nothing to choose; a secret set in **Inbound Receiving** or as `ASTERISK_INBOUND_SECRET` in `.env` is used instead. If a received fax cannot be handed to Faxbot, Recent calls says why and Faxbot brings the fax in once the cause is fixed (see [Receiving faxes](../operations/receiving.md#asterisk)).

## T.38

T.38 carries fax over the internet as fax data instead of as sound, which is much more reliable. Faxbot turns it on by default and uses redundancy to survive lost packets. When a fax arrives, Faxbot asks to switch the call to T.38; when it sends, it accepts the switch from the other side. If the far end cannot use T.38, the call continues as audio, which works on clean connections.

A carrier listing T.38 support does not guarantee every call completes as T.38. Faxbot records which one each call used, so you can check.

### Fax preference on outgoing calls

**Mark outgoing calls as fax when they start** adds a standard fax marker (RFC 6913) to each new outgoing call. Some carriers use it to pick a fax-capable route; others ignore it. It is on by default: it is a preference (`Accept-Contact`, never `Require`), so a carrier that does not know it still connects the call. Turn it off only if your carrier refuses calls that carry it. It only describes the call; Faxbot never places a second call because of it, and never resends a fax whose outcome is unknown.

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
| Gamma (GBP) | not published | not published | not published | not published |
| BT One Voice (GBP) | not published | not published | not published | not published |
| Telstra SIP Connect (AUD) | not published | not published | not published | not published |

An Avaya phone system has no rate card: the carrier behind it bills the calls, so add your own rate for it.

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
- otherwise, only when exactly one call has the same numbers and was answered and ended within 45 seconds of the Telnyx record, and that record fits no other call. If Faxbot did not learn the number a received call dialled, the caller's number must match instead; time alone never decides.

A record that could fit more than one call is never guessed. It stays unmatched, the call's cost stays unknown, and Spending counts it as "could not be matched".

Telnyx usually has a call's record within minutes. Until then the fax shows "Cost not reported yet." A record without a price stays unknown, never zero. Faxbot asks again with growing gaps, checks once more a day after the call to settle the charge, and stops asking after 7 days. If Telnyx later reports a different amount, the new amount replaces the old one and both are kept. A charge never changes a fax's delivery status, and a failed call that Telnyx charged for still counts toward that fax's cost.

Faxbot also reads the trunk's Telnyx records for the last two days once an hour, to find calls it has no record of, such as a received fax whose hand-over failed. Their charges count in Spending on their own line. A charge is shown on a received fax only when exactly one fax matches it by number and time. This needs the trunk's fax numbers and caller ID filled in.

To ask Telnyx straight away, select **Check Telnyx charges now** under Spending, or run `faxbot costs reconcile`.

## How Faxbot records each call

For every call on the trunk Faxbot keeps a call record:

- direction, your number, the other number and the carrier preset;
- when the call started, was answered and ended, and the connected seconds;
- the result: answered, busy, network busy, no answer, failed, or not known yet;
- whether the call used T.38, how many pages moved, and the other machine's station ID;
- the reason a call or fax failed, and one sentence about what happened.

A call whose outcome Faxbot cannot confirm stays "not known yet" and is never sent again automatically.

**Recent calls** under the trunk settings lists them, newest first, with that sentence (`faxbot providers trunk calls` prints the same list). When a fax over the trunk fails, **Jobs** shows the same sentence for that fax; when the other fax machine was the problem, Jobs says "The other fax machine answered but the fax did not finish." and Recent calls keeps the reason. The sentences you may see:

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
