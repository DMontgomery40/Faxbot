# Network for fax over IP (T.38)

Faxbot sends faxes over a carrier trunk in two ways. **Fax over IP (T.38)** carries the fax data in its own packets, while **audio fax** carries the fax tones as an ordinary phone call. Audio fax has worked on every network Faxbot has met so far. Fax over IP (T.38) only works when the carrier's fax data can find its way back to Faxbot, and whether it can depends on the network Faxbot runs on.

Faxbot checks its network, opens router ports when it can, and tells you what to change when it cannot. Audio fax keeps working in the meantime.

A network that allows fax over IP (T.38) doesn't guarantee that the carrier will accept it on every call. Faxbot tries fax over IP (T.38) first, and when the carrier declines it, that fax goes through as audio fax instead. Faxbot saw this live with Telnyx on 4 October 2026, on calls coming from another fax service.

## Why the network matters

On the way to the internet, Faxbot's packets pass through several layers: Docker's own network, the virtual machine on a Mac, the computer itself and finally the router. Any of these layers can change a packet's port number as it goes out.

Carriers send audio back to whatever address and port Faxbot's audio came from, so audio fax gets through all of these layers. Telnyx doesn't do the same for fax over IP (T.38): it sends the fax data to the port number Faxbot named when it set up the call. That means fax over IP (T.38) needs either a network that keeps port numbers, or a router that forwards Faxbot's fax ports.

On 3 and 4 October 2026, Faxbot measured four setups by asking public address-lookup servers which port they saw:

| Where Faxbot runs | Port numbers on the way out | Does fax data from Telnyx come back? |
| --- | --- | --- |
| The Mac itself, behind a home router | Kept (40123 stays 40123) | Yes |
| Colima, default network | Changed, differently for each destination | No (live call, 3 October) |
| Colima with `--network-address` (the Mac's shared network) | Changed, the same way for every destination | No |
| Colima directly on the local network (bridged mode) | Kept (4002 stays 4002) | Yes (live 2-page fax, 4 October) |

## The network check

Faxbot runs the network check when it starts, when you select **Apply and connect**, every few minutes (at the interval set by **Check the internet address every … minutes**) and whenever you select **Check again**. You'll find it under **Delivery setup → your carrier → Network for fax over IP**, and on the command line:

```sh
faxbot delivery providers trunk network status   # show the last check
faxbot delivery providers trunk network check    # check now
```

The check runs inside Faxbot. In the standard Docker Compose installation, Faxbot shares its route to the internet with its fax engine (Asterisk), so what Faxbot sees is what the fax engine gets. The check finds out three things:

- How your network treats port numbers: whether it keeps them, changes them the same way for every destination, or changes them for each destination. The answer is "unknown" when a firewall blocks the internet address lookup (STUN, UDP port 3478).
- Whether your internet provider shares one internet address among many customers (carrier-grade NAT). Faxbot recognizes this from an address in 100.64.0.0/10 on the way out, or from a router whose own internet address differs from the one the internet sees.
- Where Faxbot runs: Docker on a Mac through Colima, Docker Desktop on a Mac or Windows, a computer on your local network, a cloud server, or a server with its own internet address. For Colima it also tells which network the virtual machine uses: Colima's private network, the Mac's shared network, or directly your local network.

The check only reads facts about the computer and the first few routers on the way out; it doesn't change anything.

**Administration → System health** shows the result of the same check as "Faxing over the internet". While Faxbot keeps fax over IP (T.38) off because of the network, the **Overview** shows "One network change would let faxes go over the internet; faxes still go through meanwhile".

## What Faxbot does by itself

### Fax over IP (T.38) follows the network

When the check finds that the carrier's fax data can't come back, Faxbot turns off **Use T.38 fax over IP**, shows the reason next to the switch, and sends audio fax. The settings history records the change as made by "system". When a later check finds a network that allows fax over IP (T.38), Faxbot turns it back on, so nobody has to touch the switch. Faxbot never sends a fax a second time because of any of this.

To avoid flipping back and forth, the check at start and the regular check only act when two checks in a row agree. If the check at start sees a different answer from last time, it checks again a minute later before acting. **Apply and connect** and **Check again** act straight away.

Your own choice wins. If you select **Try T.38 again** on the same network, Faxbot leaves that alone until the network changes, and if you choose audio fax yourself, Faxbot always keeps it.

### Faxbot opens the fax ports on your router

When the router directly in front of Faxbot's computer changes port numbers, Faxbot can ask that router to forward its fax ports. It only does this when the fax ports are published (see [The fax ports](#the-fax-ports)) and you haven't entered an internet address yourself.

Faxbot tries three methods in turn: PCP, then NAT-PMP, then UPnP. Every port has to open with the same number on the outside; if even one doesn't, Faxbot closes them all again. It renews the ports every half hour, closes them when it stops, and while they are open it tells the carrier its internet address together with these exact ports.

You can turn this off by clearing **Let Faxbot open its fax ports on your router**, or with `faxbot delivery providers trunk network router-ports off`; the setting behind both is `sip_router_ports`.

PCP and UPnP need to know this computer's own address on your local network, which Faxbot can't see from inside a Docker container. Set `FAXBOT_LAN_ADDRESS` in `.env` for them, as you would for the phone system file. NAT-PMP works without it.

This has only been tested against stand-in routers. Faxbot has not yet opened ports on a real router.

## The fax ports

By default Faxbot publishes no ports at all, because the fax engine starts every call's packets itself. When fax over IP (T.38) needs a forward, start Faxbot with its fax ports:

```sh
docker compose -f docker-compose.yml -f docker-compose.fax-ports.yml up -d
```

The fax ports are 40 UDP ports, 4000–4039, and the fax engine uses exactly these: 13 for fax over IP (T.38) and 27 for audio, which is enough for 13 faxes at once. When the fax engine starts, it records the published ports, and the network check reads that record. Don't combine this file with `docker-compose.public.yml` or `docker-compose.phone-system.yml`, because those publish their own ports.

## What to do on your platform

The console and the command line show the same instructions, with the commands filled in for your platform.

### Colima on a Mac

Colima's default network changes port numbers, and so does its shared network. When Faxbot's network check says the carrier's fax data cannot come back, put Colima directly on your local network (bridged mode).

Colima can't change the network of an existing virtual machine (it says "'network mode' cannot be updated after initial setup"), so you delete the virtual machine and create it again. Delete it **without** `--data`: Colima 0.9 and later keep Docker's images and volumes, so Faxbot keeps its faxes and settings. Then start it again in bridged mode with the same CPU, memory and disk. Faxbot is down for a few minutes, and your Mac may ask for your password once.

```sh
colima version   # 0.9 or later keeps your faxes when the machine is recreated
colima list      # the profile name (default unless you chose one), CPUS, MEMORY and DISK
colima delete default
colima start default --cpu 2 --memory 4 --disk 20 --network-address --network-mode bridged --network-interface "$(route -n get default | awk '/interface:/{print $2}')" --network-preferred-route
docker compose up -d   # in Faxbot's folder
```

Use your own profile name and sizes; the console fills in the CPU, memory and disk that Faxbot sees. The new virtual machine keeps the old disk size even if you ask for a bigger one. The `--network-interface` part looks up your Mac's network connection, for example `en0`. Never add `--data` to `colima delete`, because that erases your faxes.

This was checked with Colima 0.9.1 on 4 October 2026. After the new virtual machine started, port 4002 stayed 4002 on the way out, data written before the delete was still there, and Faxbot turned fax over IP (T.38) back on by itself.

### Docker Desktop (Mac or Windows)

Docker Desktop changes port numbers, and Faxbot can't see your router from inside it. Forward UDP ports 4000–4039 on your router to this computer, start Faxbot with the fax ports file, and enter your internet address under **Internet address**. Once the address is entered and the fax ports are published, Faxbot can turn fax over IP (T.38) on.

Faxbot hasn't been measured on Docker Desktop; this advice is based on Docker's documentation.

### A computer on your local network (Linux)

Most home and office routers keep port numbers, so usually there is nothing to do. If yours changes them, start Faxbot with the fax ports file and Faxbot will ask the router to open them. If the router refuses, either turn on UPnP or NAT-PMP on the router, or forward UDP ports 4000–4039 to this computer and enter your internet address under **Internet address**.

### A cloud server

A server with its own internet address keeps port numbers, and so does a cloud's one-to-one address. Because the fax engine sends the first packets, the firewall lets the carrier's answers back in.

A server without its own internet address, for example one behind a cloud NAT gateway, changes port numbers. Give it its own internet address, open UDP ports 4000–4039 in its firewall or security group, start Faxbot with the fax ports file, and enter the address under **Internet address**.

Faxbot hasn't been measured on a cloud server.

### Your internet provider shares your internet address

No router setting can bring fax data back through carrier-grade NAT, so Faxbot sends audio fax. To use fax over IP (T.38), run Faxbot on a server with its own internet address, such as a rented virtual server.

### No internet address found

If a firewall limits outgoing traffic, let Faxbot reach `stun.cloudflare.com` on UDP port 3478, and for Telnyx also `stun.telnyx.com` on UDP port 3478.

## For developers

## What is not verified

Docker Desktop and cloud servers have not been measured. Opening fax ports on a router was tested only against stand-in routers; Faxbot has not opened ports on a real router. The check assumes the fax engine shares Faxbot's network, as it does in the standard Docker Compose installation.
