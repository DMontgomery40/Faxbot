# Network for fax over IP (T.38)

Faxbot sends faxes over a carrier trunk in two ways:

- **Fax over IP (T.38)** sends the fax data in its own packets.
- **Audio fax** sends the fax tones as a phone call.

Audio fax works on every network that Faxbot has met. Fax over IP (T.38) works only when the carrier's fax data can come back to Faxbot. This depends on the network that Faxbot runs on.

Faxbot checks its network. It fixes what it can by itself. When it cannot fix the network, it tells you what to change. Audio fax keeps working meanwhile.

A network that allows fax over IP (T.38) does not mean that the carrier accepts it on every call. Faxbot tries fax over IP (T.38) first. When the carrier declines it on a call, that fax goes through as audio fax. Faxbot saw this live with Telnyx on 4 October 2026, on calls from another fax service.

## Why the network matters

Faxbot's packets go through several layers on their way to the internet: Docker's network, the virtual machine on a Mac, the computer and the router. Each layer can change the port number of a packet.

Carriers send audio back to the address and port that Faxbot's audio came from. So audio fax works through all of these layers. Telnyx does not do this for fax over IP (T.38). It sends the fax data to the port number that Faxbot names in the call. So fax over IP (T.38) needs one of these:

- a network that keeps port numbers, or
- a router that forwards Faxbot's fax ports.

Faxbot measured these networks on 3 and 4 October 2026. It asked public address-lookup servers which port they saw:

| Where Faxbot runs | Port numbers on the way out | Fax data from Telnyx comes back? |
| --- | --- | --- |
| The Mac itself, behind a home router | Kept (40123 stays 40123) | Yes |
| Colima, default network | Changed, differently for each destination | No (live call, 3 October) |
| Colima with `--network-address` (the Mac's shared network) | Changed, the same for every destination | No |
| Colima directly on the local network (bridged mode) | Kept (4002 stays 4002) | Yes (live 2-page fax, 4 October) |

## The network check

Faxbot does the network check at these times:

- when Faxbot starts,
- when you select **Apply and connect**,
- every few minutes, at the interval of **Check the internet address every … minutes**,
- when you select **Check again**.

The check is under **Providers → your carrier → Network for fax over IP**. On the command line:

```sh
faxbot providers trunk network status   # show the last check
faxbot providers trunk network check    # check now
```

The check runs inside Faxbot. In the standard Docker Compose installation, Faxbot uses the same way to the internet as its fax engine (Asterisk). The check finds out:

- How your network treats port numbers. They can be kept, changed the same way for every destination, or changed for each destination. The result is "unknown" when a firewall blocks the internet address lookup (STUN, UDP port 3478).
- Whether your internet provider shares one internet address among many customers (carrier-grade NAT). Faxbot sees this as an address in 100.64.0.0/10 on the way out. It also sees it when your router's own internet address is not the one the internet sees.
- Where Faxbot runs. The check knows five platforms: Docker on a Mac through Colima, Docker Desktop (Mac or Windows), a computer on your local network, a cloud server, and a server with its own internet address. For Colima, the check also finds the network: Colima's private network, the Mac's shared network, or directly on your local network.

The check reads facts about the computer and the first routers on the way out. It changes nothing.

**System → Diagnostics** shows the same check as "Faxing over the internet". While Faxbot keeps fax over IP (T.38) off because of the network, the **Overview** shows "One network change would let faxes go over the internet; faxes still go through meanwhile".

## What Faxbot does by itself

### Fax over IP (T.38) follows the network

- When the check finds that fax data cannot come back, Faxbot turns off **Use T.38 fax over IP**. The settings history shows the change as made by "system". Faxbot shows the reason next to the switch and sends audio fax.
- When a later check finds a network that allows fax over IP (T.38), Faxbot turns it on again. Nobody has to touch the switch.
- Faxbot never sends a fax again because of this.
- The check at start and the regular check act only when two checks in a row give the same answer. When the check at start finds a changed answer, it checks again one minute later.
- **Apply and connect** and **Check again** act at once.
- If you select **Try T.38 again** on the same network, Faxbot keeps your choice until the network changes.
- If you choose audio fax yourself, Faxbot always keeps that choice.

### Faxbot opens the fax ports on your router

Faxbot can ask your router to forward its fax ports. It does this only when all of these are true:

- the router directly in front of Faxbot's computer changes port numbers,
- the fax ports are published (see [The fax ports](#the-fax-ports)),
- you did not enter an internet address.

Faxbot tries three methods in this order: PCP, NAT-PMP and UPnP. Each port must open with the same number outside. If one port does not, Faxbot closes all of them. Faxbot renews the ports every half hour and closes them when it stops. While the ports are open, Faxbot tells the carrier its internet address and these exact ports.

To turn this off, clear **Let Faxbot open its fax ports on your router**. On the command line, use `faxbot providers trunk network router-ports off`. The setting is `sip_router_ports`.

PCP and UPnP need this computer's own address on your local network. Faxbot cannot see that address from inside a Docker container. Set `FAXBOT_LAN_ADDRESS` in `.env`, as for the phone system file. NAT-PMP does not need it.

Not verified: Faxbot was tested against stand-in routers only. It has not opened ports on a real router.

## The fax ports

By default, Faxbot publishes no ports. The fax engine starts every call's packets itself. When fax over IP (T.38) needs a forward, start Faxbot with its fax ports:

```sh
docker compose -f docker-compose.yml -f docker-compose.fax-ports.yml up -d
```

The fax ports are 40 UDP ports, 4000–4039. The fax engine uses exactly these ports: 13 for fax over IP (T.38) and 27 for audio. This is enough for 13 faxes at the same time.

The fax engine records the published ports when it starts, and the network check reads this record. Do not use this file together with `docker-compose.public.yml` or `docker-compose.phone-system.yml`. Those files publish their own ports.

## What to do on your platform

The console and the command line show the same instructions, with the commands for your platform.

### Colima on a Mac

Colima's default network changes port numbers. Its shared network does too. Put Colima directly on your local network instead (bridged mode).

Colima cannot change the network of an existing virtual machine. It says "'network mode' cannot be updated after initial setup". So you must delete the virtual machine and create it again:

1. Delete the virtual machine **without** `--data`. Colima 0.9 and later keep Docker's images and volumes, so Faxbot keeps its faxes and settings.
2. Start it again in bridged mode, with the same CPU, memory and disk.

Faxbot stops for a few minutes. Your Mac can ask for your password one time.

```sh
colima version   # 0.9 or later keeps your faxes when the machine is recreated
colima list      # the profile name (default unless you chose one), CPUS, MEMORY and DISK
colima delete default
colima start default --cpu 2 --memory 4 --disk 20 --network-address --network-mode bridged --network-interface "$(route -n get default | awk '/interface:/{print $2}')" --network-preferred-route
docker compose up -d   # in Faxbot's folder
```

- Use your own profile name and sizes. The console fills in the CPU, memory and disk that Faxbot sees.
- A new virtual machine keeps the old disk size, even when you ask for a larger disk.
- The `--network-interface` part finds your Mac's network connection, for example `en0`.
- Never add `--data` to `colima delete`. It erases your faxes.

Verified with Colima 0.9.1 on 4 October 2026. After the new virtual machine started, port 4002 stayed 4002 on the way out. Data written before the delete was still there. Faxbot turned fax over IP (T.38) on again by itself.

### Docker Desktop (Mac or Windows)

Docker Desktop changes port numbers. Faxbot cannot see your router from inside Docker Desktop. Do these steps:

1. Forward UDP ports 4000–4039 on your router to this computer.
2. Start Faxbot with the fax ports file.
3. Enter your internet address under **Internet address**.

When the address is entered and the fax ports are published, Faxbot turns fax over IP (T.38) on by itself.

Not verified: Faxbot was not measured on Docker Desktop. This section uses Docker's documentation.

### A computer on your local network (Linux)

Most home and office routers keep port numbers. Then you do not have to do anything.

If your router changes port numbers, start Faxbot with the fax ports file. Faxbot then asks your router to open the fax ports. If the router refuses, do one of these:

- Turn on UPnP or NAT-PMP on the router.
- Forward UDP ports 4000–4039 to this computer, and enter your internet address under **Internet address**.

### A cloud server

A server with its own internet address keeps port numbers. A cloud's one-to-one address does too. The fax engine sends the first packets, so the firewall lets the carrier's answers come back.

A server without its own internet address (for example, behind a cloud NAT gateway) changes port numbers. Do these steps:

1. Give the server its own internet address.
2. Open UDP ports 4000–4039 in its firewall or security group.
3. Start Faxbot with the fax ports file.
4. Enter the address under **Internet address**.

Not verified: Faxbot was not measured on a cloud server.

### Your internet provider shares your internet address

No router setting can bring fax data back through carrier-grade NAT. Faxbot sends audio fax. To use fax over IP (T.38), run Faxbot on a server with its own internet address, for example a rented virtual server.

### No internet address found

If a firewall limits outgoing traffic, let Faxbot reach `stun.cloudflare.com` on UDP port 3478. For Telnyx, also allow `stun.telnyx.com` on UDP port 3478.

## For developers

Other parts of Faxbot call `sip_network.network_allows_t38(values)`. It reads only the stored network check and returns:

- `True`: fax over IP (T.38) can be tried.
- `False`: use audio fax.
- `None`: not known yet. Try fax over IP (T.38). After one failed call, the no-data-back rule switches new calls to audio fax.

## What is not verified

- Docker Desktop, cloud servers and shared internet addresses: Faxbot recognizes them from recorded examples. Faxbot was not measured on a real one.
- Opening the fax ports on a router: PCP, NAT-PMP and UPnP were tested against stand-in routers on this computer only. The measured home router answers none of them. Faxbot made no port mapping on a real router.
- The check runs inside Faxbot. It assumes that the fax engine uses the same network, as in the standard Docker Compose installation.
