# Avaya IP Office and Aura: fax through your phone system

If your office already runs an Avaya phone system, Faxbot can connect to it on your local network instead of to a carrier. Faxbot becomes one more SIP trunk on the phone system. Faxes Faxbot sends go out over the phone system's own lines, and faxes to your fax numbers reach Faxbot through it. Your carrier, contract and numbers stay as they are.

Faxbot connects the way Avaya's own interoperability notes connect fax servers (XMedius, Multi-Tech FaxFinder, Dialogic):

- a SIP trunk between the phone system and Faxbot, with no registration;
- each side recognises the other by its address, so there is no username or password;
- T.38 fax with a fall back to G.711 audio;
- only the G.711 codecs, A-law first in the UK and Australia and µ-law first in the US. Faxbot picks the order from the installation country.

Avaya IP Office and Avaya Aura (Communication Manager with Session Manager) are both covered. Avaya Cloud Office customers are moving to RingCentral and are not a target; use RingCentral's own fax or a [carrier SIP trunk](sip-trunk.md).

## What you need

- A Linux computer on the same local network as the phone system, running Faxbot with Docker Compose, with a fixed address on that network (for example `192.168.1.20`).
- The phone system's address on that network: IP Office's LAN address, or Session Manager's SIP entity address for Aura.
- The fax numbers the phone system should send to Faxbot, and the fax number Faxbot's own faxes should show.
- Your Avaya administrator or partner, for the steps in [What your Avaya administrator sets](#what-your-avaya-administrator-sets).

The phone system and Faxbot recognise each other by address, so the phone system's own address has to reach Faxbot unchanged. Docker Engine on Linux does that. Docker Desktop (Mac and Windows) and Colima instead pass outside traffic through their own virtual machine, so Faxbot would see the virtual machine's address rather than the phone system's. **Check trunk status** says so when it sees that it is running there. This is Docker's documented behaviour, and it has not been tested against a real phone system. Avaya's notes for IP Office SIP extensions also state that "Connection of SIP extension devices from locations where Network Address Translation (NAT) is applied to the connection is not supported", so signing Faxbot in as an extension would not help either. Faxbot does not offer that.

## 1. Publish Faxbot on your local network

By default Faxbot publishes no SIP ports at all. A phone system has to send calls to Faxbot, so start it with the phone system file, which publishes SIP and a small range of media ports on your computer's local-network address only. Nothing is opened on any other address.

1. In `.env` next to `docker-compose.yml`, set this computer's address on the local network:

   ```env
   FAXBOT_LAN_ADDRESS=192.168.1.20
   ```

2. Start Faxbot with both files:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.phone-system.yml up -d
   ```

| Port | Protocol | What it carries |
| --- | --- | --- |
| 5060 | UDP and TCP | SIP calls between the phone system and Faxbot |
| 4000–4019 | UDP | Fax data: the first third is T.38, the rest is audio |

Every three media ports carry about one fax at a time. `FAXBOT_MEDIA_PORTS` in `.env` changes the range. Never set more than 100 ports. Docker starts one helper process for every published port and publishes the whole range before Asterisk starts. Asterisk then refuses a range wider than 100 ports and does not start.

| `FAXBOT_MEDIA_PORTS` | Faxes at once |
| --- | --- |
| `4000-4019` (default) | 6 |
| `4000-4031` | 10 |
| `4000-4059` | 20 |
| `4000-4099` (largest) | 33 |

On a Linux host you can also stop Docker from starting a helper process for every published port, which is what once used up a small host's memory:

1. In `/etc/docker/daemon.json`, set:

   ```json
   { "userland-proxy": false }
   ```

2. Restart Docker (`sudo systemctl restart docker`).

Docker's [daemon reference](https://docs.docker.com/reference/cli/dockerd/) lists this option as "Use userland proxy for loopback traffic (default true)". Its [host network page](https://docs.docker.com/engine/network/drivers/host/) notes that a bridge network otherwise creates a "userland-proxy" for each port. Both pages were read on 4 October 2026.

The setting covers every container on that Docker host. Docker describes it as being for loopback traffic, so calls from a phone system elsewhere on the network do not depend on it. Keep the range at or below 100 ports either way.

If the computer has a firewall, allow these ports from the phone system's address. Never combine this file with `docker-compose.public.yml`, which is for carriers that reach Faxbot from the internet.

Faxbot tells the phone system this address in every call. A phone system on a private network would otherwise be handed Docker's internal address, which it cannot reach.

## 2. Set up Faxbot

In the console, open **Settings** (or step 2 of the **Setup Wizard**) and, under **Carrier**, choose **Avaya IP Office** or **Avaya Aura** from **Your phone system**.

| Field | What to enter |
| --- | --- |
| Phone system address | IP Office's LAN address, or Session Manager's address for Aura |
| Port | Leave empty for 5060 |
| Transport | UDP by default; TCP if the phone system's line uses TCP (Aura entity links often do) |
| Codec order | Leave as the default: A-law first, or µ-law first in North America and Japan |
| Number format | **International, with + and the country code**, or **As a phone here dials it** when the phone system's short codes expect local dialling |
| Outside-line prefix | With **As a phone here dials it**: the digits the phone system needs before an outside number, such as `9`. At most four digits |
| Caller ID | The fax number your phone system shows for faxes Faxbot sends |
| Fax numbers your phone system sends to Faxbot | Your fax numbers. Ask the administrator to pass the full number through, not an extension |
| Use T.38 fax over IP | On. Turn it off only if the phone system offers no T.38 (see below) |

With **As a phone here dials it**, a UK installation dials `+44 1632 960123` as `01632960123` and an American number as `0016502530000`, with the outside-line prefix in front of each. A US installation dials `16502530000` and `011442079460000`. A number that would come to more than 20 digits is refused before any call is placed.

Then select **Apply and connect**. Under **Reaching Faxbot from your phone system**, Faxbot shows what to give your administrator, for example "Give your phone system administrator this address: 192.168.1.20, port 5060 (UDP or TCP), and media ports 4000–4019, enough for 6 faxes at once." Until Faxbot is published on your local network, the same place says "Your phone system cannot reach Faxbot yet, because Faxbot is not published on your local network." and shows the command from step 1.

**Check trunk status** then reads, one sentence per line:

- "Faxbot and your phone system recognise each other by address, so there is no registration; calls use UDP."
- "The phone system answered Faxbot's check in 3 ms." Faxbot checks the phone system every 25 seconds over UDP and every 30 seconds over TCP.
- "The trunk is ready." once the phone system answers.

If you keep settings in `.env` rather than the console, set `SIP_TRUNK_PRESET=avaya-ipoffice` (or `avaya-aura`), `SIP_TRUNK_AUTH=ip` and `SIP_TRUNK_HOST`. A phone system takes only sign-in by address. The other `SIP_TRUNK_*` settings are in [SIP trunk](sip-trunk.md#set-it-up).

From the command line:

```bash
faxbot trunk presets avaya-ipoffice          # the preset, the administrator's steps and the sources
faxbot trunk use avaya-ipoffice --host 192.168.1.5 --number-format local --prefix 9
faxbot trunk apply
faxbot trunk status
```

## What your Avaya administrator sets

Both lists come from Avaya's DevConnect application notes and say which notes each step comes from. **Settings** shows the same list under **What your Avaya administrator sets**, and `faxbot trunk presets avaya-ipoffice` prints it.

### IP Office (IP Office Manager)

From the XMedius XM Fax with IP Office 11.0 notes, sections 5.1–5.6:

1. **System → LAN1 (or LAN2) → VoIP**: tick **SIP Trunks Enable**.
2. **Line → New → SIP Line**. On the **SIP Line** tab, set **ITSP Domain Name** to Faxbot's address.
3. **Transport** tab: **ITSP Proxy Address** is Faxbot's address, **Layer 4 Protocol** UDP, **Send Port** and **Listen Port** 5060.
4. **SIP URI** tab: add one URI with its own **Incoming Group** and **Outgoing Group**. **Local URI** and **Contact** can be `*` so the fax numbers pass through.
5. **VoIP** tab: **Codec Selection** Custom with only **G.711 ALAW** and **G.711 ULAW**; tick **Re-invite Supported**; **Fax Transport Support** **T38 Fallback**; **DTMF Support** RFC2833/RFC4733; **Media Security** Disabled.
6. **T38 Fax** tab: keep **Use Default Values**.
7. **Incoming Call Route** on the carrier line: send each fax number to the Faxbot line.
8. Route calls arriving on the Faxbot line to the carrier line the way calls from a phone go out. If that needs an outside-line prefix such as 9, enter the same prefix in Faxbot.
9. Carrier line, **VoIP** tab: **Fax Transport Support** **T38 Fallback** (G.711 if the carrier has no T.38), and only G.711 codecs.

### Aura (Communication Manager and Session Manager)

From the Communication Manager 7, Session Manager 7 and SBCE 7 notes, and Avaya's T.38 interoperability requirements:

1. **Communication Manager**, `change ip-codec-set`: G.711A first (UK and Australia) or G.711MU first (US), and no G.729. On page 2: **FAX Mode** `t.38-standard`, **Redundancy** 0, **ECM** y, **Modem** off.
2. `change ip-network-region`: use that codec set.
3. `add signaling-group`: **Group Type** sip, to Session Manager over TCP or TLS, **Peer Detection Enabled** y with **Peer Server** SM, **DTMF over IP** rtp-payload.
4. `add trunk-group`: **Group Type** sip, **Service Type** tie, with enough members for the faxes sent at once.
5. Route the fax numbers to that trunk group.
6. **Session Manager** (System Manager → Elements → Routing):
   - add a **SIP Entity** for Faxbot with Faxbot's address;
   - add an **Entity Link** to it on UDP or TCP port 5060, with **Trust State** Trusted;
   - add a **Routing Policy** to Faxbot;
   - add **Dial Patterns** for the fax numbers.
7. If a Session Border Controller sits between them, tick **T.38 Support** in its interworking profile.

## What to ask your Avaya partner

- Which address Faxbot should use for the phone system, and whether the line runs over UDP or TCP.
- Whether **Fax Transport Support** offers **T38 Fallback** on your IP Office. Some Linux-based IP Office systems without IP500 V2 media resources may offer only G.711 fax. If so, choose G.711 there and turn off **Use T.38 fax over IP** in Faxbot.
- Whether every line a fax passes through, including the carrier line, keeps only G.711. G.729 breaks fax.
- Whether a fax line uses a SIP trunk channel licence for each fax at a time.
- How the carrier line sends your fax numbers (full number or extension), and which outside-line prefix calls from the Faxbot line need.
- For Aura, whether a Session Border Controller sits between Session Manager and Faxbot, and whether there is a second Session Manager. Faxbot recognises one address.

## Troubleshooting

- **"The phone system does not answer Faxbot's checks."** The phone system address, port or transport in Faxbot does not match the phone system's line, or a firewall drops port 5060.
- **A T.38 call is refused with "488 Not Acceptable Here"** (seen in the phone system's trace). Avaya's fix (knowledge article SOLN268281) is **ECM** n in the codec set.
- **The call connects but no fax data arrives.** Faxbot switches new calls to audio fax after a T.38 timeout, as with a carrier (see [audio fax](sip-trunk.md#when-t38-data-does-not-come-back-audio-fax)). Check that the media ports are published and allowed through any firewall.
- Fax problems on IP Office SIP lines are most often on the carrier line (no T.38, G.729 or jitter), not on the fax server's line.

## What Faxbot has verified

- **Tested:** the trunk Faxbot writes for both presets (exact configuration files in its test suite), and a loopback proof (`make native-proof`). In the proof, a second Asterisk stands in for the phone system on a separate network, behind a router that does what Docker does for the published ports. The proof sent and received a two-page fax both ways over T.38 and as audio. The phone system saw only Faxbot's local-network address, never Docker's internal one. It received the number in the format a phone dials it, with the outside-line prefix, and Faxbot recognised the phone system's calls by their address.
- **Not yet tested:** a real IP Office or Aura system, which needs a customer or partner lab. Also untested: T.38 on Linux-based IP Office, and Docker Desktop or Colima hiding the phone system's address. The second comes from Docker's documentation, and the host names Faxbot uses to recognise Docker Desktop and Colima were checked inside a Colima container.

## Sources

Read on 3 October 2026 unless noted.

- Avaya DevConnect, [XMedius XM Fax with Avaya IP Office 11.0 over SIP trunks](https://support.avaya.com/css/public/documents/101065243)
- Avaya, [Application Notes and Test Requirements for T.38 Fax Interoperability](https://support.avaya.com/css/public/documents/100172137), draft 1.1, 13 May 2013
- Avaya, [Communication Manager 7, Session Manager 7 and SBCE 7 with the BT Global Services SIP trunk](https://www.virginmediabusiness.co.uk/help/s/WSIPT_Avaya_CM7_SM7_ASBCE7.pdf)
- Avaya knowledge article [SOLN268281](https://support.avaya.com/kb/public/SOLN268281): inbound SIP fax to a third-party fax server fails with 488 Not Acceptable Here
- Avaya, [IP Office Platform 9.1 Third-Party SIP Extension Installation Notes](https://patton.com/files/support/kb/Avaya_IP_Office.pdf), issue 04a, 14 May 2015
