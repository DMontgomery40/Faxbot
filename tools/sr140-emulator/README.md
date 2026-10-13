# SR140 stand-in: test Faxbot as RightFax's fax gateway

This is a throwaway stand-in for OpenText RightFax's Brooktrout SR140. It places one fax call the way an SR140 with a Primary Gateway does:

- an INVITE goes straight to the gateway's address, with no registration;
- the dialled number is in the Request-URI;
- the call answers the gateway's digest challenge;
- the fax is audio first, then T.38.

It then sends a synthetic two-page TIFF. Use it to check Faxbot's fax server gateway (`api/app/fax_servers.py`, **Delivery setup → Carrier trunk → Fax servers that send through Faxbot**) before a real RightFax server is available.

It runs Faxbot's own Asterisk image with its own small configuration (`etc/`) on the host's network. It is not RightFax, and it has not been compared with a real SR140's SIP traces. What it proves is that Faxbot takes an authenticated SIP/T.38 fax call from an enrolled address, refuses everything else, and sends the fax on.

## Where to run it

Run it on a Linux machine on the same network as the Faxbot under test, but never on the Faxbot host itself. Faxbot recognises a fax server by its source address. A call from the Faxbot host to its own published port arrives through Docker's proxy with another address, and Faxbot rightly refuses it.

For the partner install (LXC 4300 at 192.168.68.230), these hosts work:

- another LXC or VM on the same Proxmox network;
- the Mac's acceptance VM, which is bridged on en8 and has its own address on the network.

Enrol the address of the host the stand-in runs on.

## On the Faxbot under test (the gateway)

1. Deploy a build with this branch. The Asterisk image changes: it gains the `faxbot-fax-server` script and `extensions-fax-server.conf`.
2. In `.env`, set `FAXBOT_LAN_ADDRESS=192.168.68.230`. Set `FAXBOT_FAX_SERVER_PORT` only when 5060 is already published on that address.
3. Add `-f docker-compose.fax-server.yml` to the compose command. The overlay publishes SIP for fax servers on that address only, mapped to Asterisk's own port 5080. Media uses the range another file already publishes (on the partner, `partner-media-ports.yml`, UDP 6000–6039). That file must also set `FAXBOT_MEDIA_PORTS=6000-6039`, so that Asterisk uses exactly that range; start.sh warns when it doesn't.
4. Enrol the stand-in's host. Keep the user name and password it prints, because the password is shown only once:

   ```sh
   faxbot delivery providers trunk fax-servers add "SR140 stand-in" <stand-in host address> \
     --countries US --per-hour 5 --per-day 20 --email <your address>
   ```

5. Load it into the fax engine. Apply restarts Asterisk once no call is up:

   ```sh
   faxbot delivery providers trunk apply
   ```

## On the stand-in's host

```sh
cd tools/sr140-emulator
export GATEWAY=192.168.68.230 SR140_USERNAME=faxserver-xxxxxxxx SR140_PASSWORD='<the password>'
docker compose up -d --build        # or SR140_IMAGE=<an existing Faxbot Asterisk image> docker compose up -d --no-build
docker compose logs -f sr140        # "SR140 stand-in: calls go to 192.168.68.230 port 5060 as faxserver-..."
```

The build compiles Asterisk, which takes about ten minutes on one core. To reuse an image already built on that host instead, set `SR140_IMAGE` and run `docker compose up -d --no-build`.

`fax/test-fax.tiff` is a synthetic two-page fax. To make another, run `python3 make_test_tiff.py fax/other.tiff 3` (it needs Pillow), then pass its name as the third argument to `send.sh`.

## The checks

Each command waits for its call to end. It then prints the stand-in's result line and the SIP answers it received. Numbers are dialled as RightFax would dial them.

| Command | Expected on the stand-in | Expected in Faxbot (`faxbot delivery providers trunk fax-servers faxes`) |
|---|---|---|
| `./send.sh 17208565062` | `result=SUCCESS pages=2 mode=T38` | **Sending**, then **Delivered** (acceptance receives it over Telnyx); the fax is in Sent under "Fax server SR140 stand-in" |
| `./send.sh 17208565062 faxbot-gateway-wrong` | `result=NOT_ANSWERED`, `SIP/2.0 401` | nothing (the call never reached the gateway) |
| `./send.sh 17208565062 faxbot-gateway-none` | `result=NOT_ANSWERED`, `SIP/2.0 401` | nothing |
| `./send.sh 911` | `result=NOT_ANSWERED`, `SIP/2.0 403` | **Turned away**: an emergency or short number |
| `./send.sh 19005550100` | `SIP/2.0 403` | **Turned away**: the dialing guard never dials premium-rate numbers |
| `./send.sh 011442079460000` | `SIP/2.0 403` | **Turned away**: the server may not send to GB |
| `./send.sh 13038790349` (the partner's own Telnyx number) | `result=SUCCESS` | not listed: it is an ordinary received fax in the partner's Received |
| `./send.sh 17208565062` six times within an hour, with `--per-hour 5` | the sixth gets `SIP/2.0 486` | the sixth is **Turned away** |

The first row places a real call over Telnyx and is billed. Run only the rows you need.

To check the failure notice, send to a number that is busy or doesn't answer. When the fax fails, its row says **Failed**, and the contact you set gets one email that names the number and the reason.

## Clean up

```sh
docker compose down
faxbot delivery providers trunk fax-servers remove "SR140 stand-in"
faxbot delivery providers trunk apply
```

## What this does not cover

- A real SR140's SIP behaviour: its T.38 version (Dialogic notes that a T.38 version 3 offer was refused with 488 by one carrier), its re-INVITE timing, and whether its Primary Gateway mode answers a digest challenge exactly as Asterisk does.
- RightFax's own status for the fax. RightFax shows "sent" once Faxbot has the pages, and the delivery result is only in Faxbot and in the notice.
- A repeated INVITE with the same Call-ID. Faxbot's tests cover it (`api/tests/test_fax_servers.py`); Asterisk can't be made to send one.
