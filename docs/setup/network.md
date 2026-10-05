# Network for fax over IP (T.38)

Faxbot's own fax engine sends faxes over a carrier SIP trunk in two ways: **T.38** (fax over IP, the fax data in its own packets) and **audio fax** (the fax tones carried as a phone call). Audio fax works on every network Faxbot has met. T.38 works only when the carrier's T.38 packets can come back to Faxbot, and that depends on the network Faxbot runs on. Faxbot checks the network, fixes what it can by itself, and tells you exactly what to change when it cannot. Audio fax keeps working meanwhile.

A network that allows T.38 is not the same as a carrier that accepts it: Faxbot tries T.38 first, and when the carrier declines it on a call, that fax goes through as audio (seen live with Telnyx on 4 October 2026, on calls from another fax service).

## Why the network matters

Faxbot's packets leave through every layer between Asterisk and the internet: Docker's own network, the virtual machine on a Mac, the Mac or computer, the router. Each layer may change the port number on the way out. Carriers send audio back to wherever Faxbot's audio came from, so audio works through any of them. Telnyx does not do that for T.38: it sends T.38 data to the port number Faxbot named in the call, so T.38 needs a network that keeps port numbers, or a router that forwards Faxbot's fax ports.

What Faxbot measured on 3 and 4 October 2026, by asking public address-lookup servers which port they saw:

| Where Faxbot runs | Port numbers on the way out | T.38 from Telnyx comes back? |
| --- | --- | --- |
| The Mac itself, home router | kept (40123 stays 40123) | yes |
| Colima, default network | changed, differently for each destination | no (live call, 3 October) |
| Colima with `--network-address` (the Mac's shared network) | changed, the same for every destination | no |
| Colima on the local network (bridged) | kept (4002 stays 4002) | yes (live 2-page fax, 4 October) |

## The check

Faxbot checks its network when it starts, on **Apply and connect**, every few minutes (the same interval as the internet address check, **Check the internet address every … minutes**) and when you select **Check again**. Find it under **Providers → your carrier → Network for fax over IP**, or on the command line:

```sh
faxbot providers trunk network status   # the last check
faxbot providers trunk network check    # check now
```

The check runs inside Faxbot, which shares its way to the internet with the fax engine. It learns:

- how your network treats port numbers: kept; changed the same way for every destination; changed for each destination; or unknown when the internet address lookup (STUN, UDP port 3478) is blocked;
- whether your internet provider shares one internet address among customers (carrier-grade NAT, seen as an address in 100.64.0.0/10 on the way out or a router whose own internet address differs from the one the internet sees);
- where Faxbot runs: Docker on a Mac through Colima (on its own private network, the Mac's shared network or directly on your local network), Docker Desktop on a Mac or Windows, a computer on your local network, a cloud server, or a server with its own internet address. It reads facts about the machine and the first routers on the way out, and changes nothing.

**System → Diagnostics** shows the same check as "Faxing over the internet", and the **Overview** lists "One network change would let faxes go over the internet; faxes still go through meanwhile" while Faxbot keeps T.38 off because of the network.

## What Faxbot does by itself

- **T.38 follows the network.** When the check says T.38 data cannot come back, Faxbot turns **Use T.38 fax over IP** off (saved by "system"), says why next to the switch, and sends audio fax. When a later check finds a network that allows it, Faxbot turns T.38 back on without anyone touching the switch. It never resends a fax. The check at start and the regular check act only when two checks in a row agree (the check at start confirms a changed answer a minute later); **Apply and connect** and **Check again** act at once. If you choose T.38 yourself on the same network (**Try T.38 again**), Faxbot leaves your choice alone until the network changes, and a choice of audio fax always stands.
- **Opening its fax ports on your router.** When Faxbot's computer sits directly behind a router that changes port numbers and Faxbot's fax ports are published (see below), Faxbot asks the router to forward them: PCP first, then NAT-PMP, then UPnP. Every port must come back with the same number, or Faxbot hands them all back. It renews them every half hour and closes them when it stops, and then tells the carrier its internet address and these exact ports. The switch **Let Faxbot open its fax ports on your router** turns this off (`faxbot providers trunk network router-ports off`, or the setting `sip_router_ports`). PCP and UPnP need this computer's own address on your network, which a container cannot see: set `FAXBOT_LAN_ADDRESS` in `.env`, as for the phone system file. NAT-PMP needs nothing. Tested against stand-in routers only; Faxbot has not opened ports on a real router.

## The fax ports

By default Faxbot publishes no ports: Asterisk starts every flow itself. When T.38 needs a forward, start Faxbot with its fixed fax ports, 40 UDP ports Asterisk then uses exactly (13 for T.38 and 27 for audio, 13 faxes at once):

```sh
docker compose -f docker-compose.yml -f docker-compose.fax-ports.yml up -d
```

Asterisk records the published range at start, and the network check reads it. Do not combine this file with `docker-compose.public.yml` or `docker-compose.phone-system.yml`, which publish their own range.

## What to do, by platform

The console and the command line show the same text with the commands for your platform.

### Colima on a Mac

Colima's default network changes port numbers, and so does its shared network. Put Colima directly on your local network instead (its bridged mode). Colima cannot switch an existing machine (it says "'network mode' cannot be updated after initial setup"), so delete the machine **without** `--data` and start it again in bridged mode with the same CPU, memory and disk. Colima 0.9 and later keep Docker's images and volumes when the machine is deleted, so Faxbot's faxes and settings stay. Faxbot stops for a few minutes, and the Mac may ask for your password once.

```sh
colima version   # 0.9 or later keeps your faxes when the machine is recreated
colima list      # the profile name (default unless you chose one), CPUS, MEMORY and DISK
colima delete default
colima start default --cpu 2 --memory 4 --disk 20 --network-address --network-mode bridged --network-interface "$(route -n get default | awk '/interface:/{print $2}')" --network-preferred-route
docker compose up -d   # in Faxbot's folder
```

Use your own profile name and sizes; the console fills in the CPU, memory and disk Faxbot sees. Recreating keeps the old disk size even when you ask for more. The interface part finds the Mac's network connection (for example en0). Never add `--data` to `colima delete`: it erases your faxes. Checked with Colima 0.9.1 on 4 October 2026: after the recreate, port 4002 stayed 4002 on the way out, data written before the delete was still there, and Faxbot turned T.38 back on by itself.

### Docker Desktop (Mac or Windows)

Docker Desktop changes port numbers on the way out, and Faxbot cannot see your router from inside it. Forward UDP ports 4000–4039 on your router to this computer, start Faxbot with the fax ports file, and enter your internet address under **Internet address**. With the address entered and the fax ports published, Faxbot turns T.38 on by itself. Not measured on Docker Desktop; based on Docker's documented networking.

### A computer on your local network (Linux)

Most home and office routers keep port numbers, so nothing is needed. If yours changes them, start Faxbot with the fax ports file; Faxbot then asks the router to open them. If the router refuses, turn on UPnP or NAT-PMP on it, or forward UDP ports 4000–4039 to this computer and enter your internet address under **Internet address**.

### A cloud server

A server with its own public address (or a cloud's one-to-one address) keeps port numbers, and Asterisk sends first, so the firewall lets the carrier's answers back in. A server without one (behind a cloud NAT gateway) changes them: give it a public address, open UDP ports 4000–4039 in its firewall or security group, start Faxbot with the fax ports file and enter the address under **Internet address**.

### Your internet provider shares your address

No router setting can bring T.38 data back through carrier-grade NAT. Faxbot sends audio fax; to use T.38, run Faxbot on a server with its own internet address (a rented virtual server).

### No internet address found

If a firewall limits outgoing traffic, let Faxbot reach `stun.cloudflare.com` (and `stun.telnyx.com` for Telnyx) on UDP port 3478.

## For the fax engine

Other parts of Faxbot ask one question: `sip_network.network_allows_t38(values)` returns True (T.38 can be tried), False (use audio fax) or None (not known yet; try T.38 and let the no-data-back rule switch new calls to audio after one failed call). It reads the stored check only.

## What has not been verified

- Docker Desktop, cloud servers and shared internet addresses: recognized from recorded examples, not measured on a real one.
- Router port opening: PCP, NAT-PMP and UPnP were tested against stand-in routers on this computer; the measured home router answers none of them, and no real router mapping was made.
- The check runs inside Faxbot and assumes the fax engine shares its network, as in the standard Docker Compose install.
