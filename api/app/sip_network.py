"""Fax over IP on real networks: where Faxbot runs, whether T.38 fax data can come back, and what to do.

T.38 fax data reaches Faxbot only when the carrier can send it to the address
and port Faxbot names in the call. A network that keeps port numbers (a home
router, a server with its own address, Colima directly on the local network)
makes that exact. A network that changes them (Colima's built-in network, the
Mac's shared network, Docker Desktop, some routers) makes the carrier's T.38
data miss Faxbot, while audio fax keeps working because carriers send audio
back to wherever Faxbot's audio came from (measured with Telnyx, 2026-10-03/04).

The check looks from where the fax engine's packets leave: in the Compose
install the API shares the Compose network with Asterisk, so both pass the
same Docker host, virtual machine and router. It combines the STUN probe
(``stun.py``) with what a container can see about its host, all read-only:

- the Linux kernel's name (Docker Desktop runs a "linuxkit" or WSL kernel);
- the machine's maker (an Apple virtual machine is Colima or Docker Desktop on a Mac);
- Lima's host name ``host.lima.internal`` (Colima);
- a cloud provider's metadata address (169.254.169.254 answers on cloud servers only);
- the first routers on the way out, read from the socket error queue the way
  ``tracepath`` does, which needs no special rights.

Measured from a container on Colima 0.9.1 (2026-10-04): the built-in network
answers no traceroute beyond the virtual machine and changes the port per
destination; the Mac's shared network answers from 192.168.64.1 and changes the
port the same way for every destination; on the local network (bridged) the
next router is the home router and the port is kept.

Each check is stored with its time in ``<FAX_DATA_DIR>/asterisk/network-check``
so every screen, the command line and the fax engine read the same answer.
``network_allows_t38`` is the one question the fax engine asks. Nothing here
opens a port, changes a router or raises.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time

from . import port_mapping, sip_fax_mode, sip_trunk

OPEN, BLOCKED, UNKNOWN = 'open', 'blocked', 'unknown'
# The fixed media range docker-compose.fax-ports.yml publishes: Asterisk uses its
# first third for T.38 and the rest for audio (asterisk/start.sh), 13 faxes at once.
FAX_PORTS = (4000, 4039)
FAX_PORTS_FILE = 'docker-compose.fax-ports.yml'
FAX_PORTS_COMMAND = f'docker compose -f docker-compose.yml -f {FAX_PORTS_FILE} up -d'
# Where the traceroute probes are aimed (they stop after a few routers).
TRACE_TARGET = ('1.1.1.1', 33434)
TRACE_HOPS = 6
METADATA_ADDRESS = ('169.254.169.254', 80)
# The networks macOS gives virtual machines on its shared network (Virtualization.framework, socket_vmnet).
MAC_SHARED_NETWORKS = (ipaddress.ip_network('192.168.64.0/24'), ipaddress.ip_network('192.168.105.0/24'))
# Internet providers that share one address among customers number their side from here (RFC 6598).
SHARED_ADDRESS_SPACE = ipaddress.ip_network('100.64.0.0/10')
# Home and office networks behind a router (RFC 1918).
LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
CLOUD_VENDORS = ('amazon ec2', 'google', 'digitalocean', 'hetzner', 'linode', 'akamai', 'vultr', 'scaleway',
                 'alibaba cloud', 'tencent cloud', 'openstack')
# How long one look at the host (traceroute, names, metadata) serves later checks; Check again looks afresh.
DISCOVERY_SECONDS = 600
# The check at start waits for Asterisk and the network to settle first.
START_DELAY_SECONDS = 15


# -- what a container can see about where it runs ------------------------------------------------

@dataclass(frozen=True)
class Discovery:
    """Read-only facts about the host; empty when Faxbot could not look."""
    kernel: str = ''
    vendor: str = ''
    product: str = ''
    lima: bool = False
    in_container: bool = False
    # The routers on the way out, nearest first; None where one did not answer. Empty: not looked.
    hops: tuple = ()
    cloud_metadata: bool = False
    cpus: int | None = None
    memory_gib: int | None = None
    disk_gib: int | None = None


IP_RECVERR = getattr(socket, 'IP_RECVERR', 11)
MSG_ERRQUEUE = getattr(socket, 'MSG_ERRQUEUE', 0x2000)
_ICMP_TIME_EXCEEDED, _ICMP_UNREACHABLE = 11, 3


def parse_hop(ancillary):
    """(address, ICMP type) of the router that answered a probe, from IP_RECVERR ancillary data; or None.

    Linux delivers ``struct sock_extended_err`` (16 bytes) followed by the
    answering router's ``sockaddr_in``, whose address starts at byte 20.
    """
    for level, kind, data in ancillary:
        if level != socket.IPPROTO_IP or kind != IP_RECVERR or len(data) < 24:
            continue
        _errno, origin, icmp_type = struct.unpack('=IBB', data[:6])
        if origin == 2 and icmp_type in (_ICMP_TIME_EXCEEDED, _ICMP_UNREACHABLE):  # SO_EE_ORIGIN_ICMP
            return socket.inet_ntoa(data[20:24]), icmp_type
    return None


def trace(target=TRACE_TARGET, hops=TRACE_HOPS, wait=1.0):
    """The first routers toward ``target``, nearest first (None where one did not answer).

    One UDP probe per hop count, all sent at once; each router that drops a
    probe reports back, and Linux files that report in the probe socket's
    error queue. Empty on systems without that queue (macOS, Windows).
    """
    if not sys.platform.startswith('linux'):
        return ()
    sockets = []
    try:
        for ttl in range(1, hops + 1):
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append(probe)
            probe.setsockopt(socket.IPPROTO_IP, IP_RECVERR, 1)
            probe.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            probe.setblocking(False)
            probe.sendto(b'faxbot', (target[0], target[1] + ttl))
        found = [None] * hops
        pending = set(range(hops))
        deadline = time.monotonic() + wait
        while pending and time.monotonic() < deadline:
            for index in sorted(pending):
                try:
                    _, ancillary, _, _ = sockets[index].recvmsg(64, 512, MSG_ERRQUEUE)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    pending.discard(index)
                    continue
                pending.discard(index)
                hop = parse_hop(ancillary)
                if hop:
                    found[index] = hop[0]
            time.sleep(0.02)
        return tuple(found)
    except OSError:
        return ()
    finally:
        for probe in sockets:
            probe.close()


def _read(path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return ''


def _resolves(name):
    try:
        socket.getaddrinfo(name, None, socket.AF_INET)
        return True
    except OSError:
        return False


def _metadata_answers():
    try:
        with socket.create_connection(METADATA_ADDRESS, timeout=0.5):
            return True
    except OSError:
        return False


def _sizes():
    """CPUs, memory and disk of the machine Docker runs in (the virtual machine on a Mac), rounded up."""
    cpus = os.cpu_count()
    memory = disk = None
    for line in _read('/proc/meminfo').splitlines():
        if line.startswith('MemTotal:'):
            memory = math.ceil(int(line.split()[1]) / (1024 * 1024))
    try:
        stats = os.statvfs('/')
        disk = math.ceil(stats.f_blocks * stats.f_frsize / 1024 ** 3)
    except (OSError, AttributeError):
        pass
    return cpus, memory, disk


def _test_mode():
    return os.environ.get('FAXBOT_TEST_MODE', '').lower() in {'1', 'true', 'yes'}


def discover_live():
    """Look at the host now. Tests never reach the network: under FAXBOT_TEST_MODE this returns nothing."""
    if _test_mode():
        return Discovery()
    try:
        release = os.uname().release
    except (AttributeError, OSError):
        release = ''
    cpus, memory, disk = _sizes()
    return Discovery(kernel=release, vendor=_read('/sys/class/dmi/id/sys_vendor'),
                     product=_read('/sys/class/dmi/id/product_name'), lima=_resolves('host.lima.internal'),
                     in_container=os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv'),
                     hops=trace(), cloud_metadata=_metadata_answers(), cpus=cpus, memory_gib=memory, disk_gib=disk)


_discovered = {}


async def discover(*, fresh=False):
    """What Faxbot can see about its host, looked at once every few minutes; never raises."""
    cached = _discovered.get('value')
    if cached and not fresh and time.monotonic() - cached[0] < DISCOVERY_SECONDS:
        return cached[1]
    try:
        found = await asyncio.wait_for(asyncio.to_thread(discover_live), 10)
    except Exception:
        found = Discovery()
    _discovered['value'] = (time.monotonic(), found)
    return found


# -- classification --------------------------------------------------------------------------------

def _address(text):
    try:
        return ipaddress.IPv4Address(text)
    except (TypeError, ValueError):
        return None


def _local(address):
    """An address on a home or office network behind a router."""
    return address is not None and any(address in network for network in LOCAL_NETWORKS)


def _public(address):
    """An address on the internet: not local, not the provider's shared space, not loopback or link-local."""
    return (address is not None and not _local(address) and address not in SHARED_ADDRESS_SPACE
            and not (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified))


def outside_hops(found: Discovery):
    """The routers past this computer: in a container the first hop is the Docker host itself."""
    hops = tuple(found.hops)
    return hops[1:] if found.in_container and hops else hops


def port_behavior(probe):
    """'kept', 'changed_same' (one changed port for every destination), 'changed_per_destination',
    'changed' (one answer, changed), or None when no STUN server answered."""
    if probe is None or not probe.answered:
        return None
    if probe.ports == 'preserved':
        return 'kept'
    if probe.ports == 'consistent':
        return 'changed_same'
    return 'changed_per_destination' if len(set(probe.answered)) > 1 else 'changed'


def shared_address(found: Discovery, probe):
    """Whether the internet provider shares one internet address among customers (carrier-grade NAT).

    Seen as a router in 100.64.0.0/10 on the way out. A VPN that numbers its
    own network from the same block (Tailscale) can look the same.
    """
    addresses = [_address(hop) for hop in outside_hops(found)] + [_address(probe.public_ip if probe else None)]
    return any(address is not None and address in SHARED_ADDRESS_SPACE for address in addresses)


def platform(found: Discovery, probe=None):
    """Where Faxbot runs: colima_user, colima_shared, colima_bridged, colima, docker_desktop_mac,
    docker_desktop_windows, docker_desktop, cloud, public_host, linux_lan or unknown."""
    kernel = found.kernel.lower()
    apple = 'apple' in found.vendor.lower()
    if 'microsoft' in kernel or 'wsl' in kernel:
        return 'docker_desktop_windows'
    if 'linuxkit' in kernel:
        return 'docker_desktop_mac' if apple else 'docker_desktop'
    outside = outside_hops(found)
    first = outside[0] if outside else None
    if found.lima:
        if not found.hops:
            # No traceroute here: the port behavior tells Colima's three networks apart.
            return {'kept': 'colima_bridged', 'changed_same': 'colima_shared',
                    'changed_per_destination': 'colima_user'}.get(port_behavior(probe), 'colima')
        address = _address(first)
        if address is None:
            return 'colima_user'
        return 'colima_shared' if any(address in network for network in MAC_SHARED_NETWORKS) else 'colima_bridged'
    vendor = f'{found.vendor} {found.product}'.lower()
    if found.cloud_metadata or any(name in vendor for name in CLOUD_VENDORS):
        return 'cloud'
    if probe is not None and probe.public_ip and probe.behind_nat is False:
        return 'public_host'
    address = _address(first)
    if _public(address):
        return 'public_host'
    if _local(address):
        return 'linux_lan'
    return 'unknown'


def verdict(probe, ports, shared, typed='', observed=None, *, published=None, mapped=None):
    """(t38, why): whether the carrier's T.38 fax data can come back to Faxbot, and the reason.

    OPEN: public, ports_kept, typed (an address typed in, on a network that keeps
    port numbers), forwarded (an address typed in while Faxbot's fax ports are
    published, which is the operator's word that the router forwards them),
    router_mapped (Faxbot opened its fax ports on the router) or t38_worked (the
    newest T.38 call went through here).
    BLOCKED: ports_change, shared_address, or behind_another_router (the router
    opened the ports, but another router in front of it changes port numbers).
    UNKNOWN: no_address, typed_differs (the typed address is not the one STUN
    sees), or typed on a network that changes port numbers without the fax
    ports published.
    """
    worked = bool(observed and observed.get('t38_ok'))
    seen = probe.public_ip if probe is not None else None
    if typed:
        if seen and seen != typed:
            return UNKNOWN, 'typed_differs'
        if ports == 'kept':
            return OPEN, 'typed'
        if published:
            return OPEN, 'forwarded'
        if ports is not None:
            return (OPEN, 't38_worked') if worked else (UNKNOWN, 'typed')
    if not seen:
        return (OPEN, 't38_worked') if worked else (UNKNOWN, 'no_address')
    if probe.behind_nat is False:
        return OPEN, 'public'
    if ports == 'kept':
        return OPEN, 'ports_kept'
    state = (mapped or {}).get('state')
    if state == 'open':
        return OPEN, 'router_mapped'
    if worked:
        return OPEN, 't38_worked'
    if shared:
        return BLOCKED, 'shared_address'
    if state == 'behind_another_router':
        return BLOCKED, 'behind_another_router'
    return BLOCKED, 'ports_change'


def assess(values, probe, found: Discovery, observed=None, *, mapping=None, published=None, now=time.time):
    """One network check, ready to store."""
    ports = port_behavior(probe)
    shared = shared_address(found, probe) or bool((mapping or {}).get('shared'))
    typed = '' if _phone_system(values) else values.sip_external_address
    t38, why = verdict(probe, ports, shared, typed, observed, published=published, mapped=mapping)
    outside = outside_hops(found)
    router = next((hop for hop in outside if _local(_address(hop))), None)
    return {'checked_at': round(now(), 3), 'platform': platform(found, probe), 'ports': ports,
            'internet_address': probe.public_ip if probe else None,
            'behind_router': probe.behind_nat if probe else None, 'shared_address': shared,
            'router': router, 'hops': list(outside), 't38': t38, 'why': why, 'typed_address': typed or None,
            'fax_ports': f'{published[0]}-{published[1]}' if published else None,
            'router_ports': mapping or None,
            'cpus': found.cpus, 'memory_gib': found.memory_gib, 'disk_gib': found.disk_gib}


# -- the stored check ---------------------------------------------------------------------------------

def check_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'network-check'


def read_check(values):
    """The last stored check, or None."""
    try:
        record = json.loads(check_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get('t38') in (OPEN, BLOCKED, UNKNOWN) else None


def write_check(values, record):
    path = check_path(values)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sip_trunk._write_private(path, json.dumps(record) + '\n')
    return record


def _phone_system(values):
    preset = sip_trunk.PRESETS.get(values.sip_trunk_preset)
    return bool(preset and preset.phone_system)


def applies(values):
    """Whether the network check means anything here: a carrier trunk (a phone system is on the local network)."""
    return sip_trunk.configured(values) and values.sip_trunk_preset in sip_trunk.PRESETS and not _phone_system(values)


def previous_verdict(values):
    """What the network allowed before this check: the stored check, else what the last probe recorded."""
    record = read_check(values)
    if record:
        return record['t38']
    earlier = sip_trunk.read_public_address(values) or {}
    if not earlier.get('ip'):
        return None
    return OPEN if earlier.get('ports_preserved') else BLOCKED


def network_allows_t38(values) -> bool | None:
    """Whether T.38 fax data can come back to Faxbot through this network, from the last stored check.

    True: the network keeps port numbers, Faxbot has its own internet address,
    its fax ports are opened or forwarded on the router, or the newest T.38
    call went through; a phone system on the local network is always True.
    True means T.38 can be tried, not that the carrier accepts it on every call.
    False: the network changes port numbers, so the carrier's T.38 data cannot
    find Faxbot; use audio fax. None: Faxbot cannot tell (no trunk, no check
    yet, the address lookup is blocked, or a typed address on a network that
    changes port numbers); try T.38, and the no-data-back rule in sip_fax_mode
    moves new calls to audio after one failed call.

    Synchronous and cheap: reads one small file, no network and no database.
    """
    if not sip_trunk.configured(values) or values.sip_trunk_preset not in sip_trunk.PRESETS:
        return None
    if _phone_system(values):
        return True
    record = read_check(values)
    if record is None:
        return None
    return {OPEN: True, BLOCKED: False}.get(record['t38'])


# -- Faxbot's fax ports: published on this computer, opened on the router ---------------------------------

_RANGE = re.compile(r'([0-9]{4,5})-([0-9]{4,5})', re.ASCII)


def media_ports_path(values) -> Path:
    """Written by the Asterisk container at start when a Compose file publishes its media range."""
    return Path(values.fax_data_dir) / 'asterisk' / 'media-ports'


def read_media_ports(values):
    """(first, last) of the media range a Compose file publishes on this computer, or None."""
    try:
        record = json.loads(media_ports_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    found = _RANGE.fullmatch(str(record.get('media_ports', ''))) if isinstance(record, dict) else None
    if not found or not 1024 <= int(found.group(1)) <= int(found.group(2)) <= 65535:
        return None
    return int(found.group(1)), int(found.group(2))


def lease_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'router-ports'


def read_lease(values):
    """The ports the router opened for Faxbot, as recorded, or None."""
    try:
        return port_mapping.Lease.from_dict(json.loads(lease_path(values).read_text(encoding='utf-8')))
    except (OSError, ValueError, AttributeError):
        return None


def _keep_lease(values, lease):
    path = lease_path(values)
    if lease is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sip_trunk._write_private(path, json.dumps(lease.as_dict()) + '\n')


def _gateway(found, where):
    """The router in front of this computer, when nothing else that changes ports sits in between."""
    if where not in ('linux_lan', 'colima_bridged', 'unknown'):
        return None
    outside = outside_hops(found)
    first = outside[0] if outside else None
    return first if _local(_address(first)) else None


def map_ports(values, probe, found, *, router=None, now=time.time):
    """Open, renew or close Faxbot's fax ports on the router as this check needs; returns {state, ...}.

    Faxbot asks only when the router changes port numbers, the fax ports are
    published on this computer, nobody typed an internet address (then the
    person forwards them), and the router is the next one out. States: open,
    refused, behind_another_router, off, not_needed, no_fax_ports, no_router.
    Blocking; never raises.
    """
    router = router or port_mapping.Router
    lease = read_lease(values)
    published = read_media_ports(values)
    where = platform(found, probe)
    gateway = _gateway(found, where)
    needed = bool(applies(values) and probe is not None and probe.public_ip and probe.behind_nat
                  and port_behavior(probe) not in (None, 'kept') and not values.sip_external_address)
    state = ('off' if not values.sip_router_ports else 'not_needed' if not needed
             else 'no_fax_ports' if not published else 'no_router' if not gateway else None)
    try:
        if state:
            if lease:
                router(lease.gateway).close(lease)
                _keep_lease(values, None)
            return {'state': state}
        first, last = published
        client = router(gateway)
        if lease and (lease.gateway, lease.first, lease.last) != (gateway, first, last):
            client.close(lease)
            lease = None
        if lease and now() >= lease.renew_at:
            lease = client.renew(lease)
        reasons = None
        if lease is None:
            lease, reasons = client.open(first, last)
        _keep_lease(values, lease)
        ports = f'{first}-{last}'
        if lease is None:
            return {'state': 'refused', 'ports': ports, 'reasons': reasons or []}
        if lease.external_ip and lease.external_ip != probe.public_ip:
            # The router's own internet address is not the one the internet sees: another router (or the
            # internet provider's shared address) sits in front, so the opened ports would lead nowhere.
            client.close(lease)
            _keep_lease(values, None)
            outer = _address(lease.external_ip)
            return {'state': 'behind_another_router', 'ports': ports,
                    'shared': bool(outer is not None and outer in SHARED_ADDRESS_SPACE)}
        return {'state': 'open', 'ports': ports, 'method': lease.method}
    except Exception:
        return {'state': 'refused', 'ports': f'{published[0]}-{published[1]}' if published else None,
                'reasons': ['failed']}


def close_router_ports(values):
    """Close the ports the router opened for Faxbot (at stop); never raises."""
    lease = read_lease(values)
    if lease is None:
        return False
    try:
        port_mapping.Router(lease.gateway).close(lease)
    finally:
        _keep_lease(values, None)
    return True


# -- sentences -----------------------------------------------------------------------------------------

PLATFORM_TEXT = {
    'colima_user': 'Faxbot runs in Docker on this Mac, on a private network inside the Mac.',
    'colima_shared': "Faxbot runs in Docker on this Mac, on the Mac's shared network.",
    'colima_bridged': 'Faxbot runs in Docker on this Mac, directly on your local network.',
    'colima': 'Faxbot runs in Docker on this Mac.',
    'docker_desktop_mac': 'Faxbot runs in Docker Desktop, an app on your Mac.',
    'docker_desktop_windows': 'Faxbot runs in Docker Desktop, an app on your Windows computer.',
    'docker_desktop': 'Faxbot runs in Docker Desktop.',
    'cloud': 'Faxbot runs on a cloud server.',
    'public_host': 'Faxbot runs on a server with its own internet address.',
    'linux_lan': 'Faxbot runs on a computer on your local network, behind your router.',
}
NOT_CHECKED = 'Faxbot has not checked this network yet.'
# For the Overview and System diagnostics, read by an office administrator; the trunk page has the details.
OFFICE_TEXT = {
    OPEN: 'Your network is ready for faxing over the internet.',
    BLOCKED: 'Your network needs one change so faxes can go over the internet. The carrier page shows what to do.',
    UNKNOWN: 'Faxbot cannot tell yet whether your network is ready for faxing over the internet. Faxes still go through.',
}
PHONE_SYSTEM_TEXT = 'Your phone system is on your local network, so fax over IP needs no network check.'
AUDIO_MEANWHILE = 'Audio fax keeps working meanwhile.'
# Live 2026-10-04: with the network open, Telnyx still refused T.38 on calls from another fax service.
TRIES_FIRST = 'Faxbot tries fax over IP (T.38) first. When the carrier declines it, the fax goes through as audio.'


def _ports_text(record=None):
    published = (record or {}).get('fax_ports') or f'{FAX_PORTS[0]}-{FAX_PORTS[1]}'
    first, _, last = published.partition('-')
    return f'{first}–{last}'


def carrier_name(values):
    preset = sip_trunk.PRESETS.get(values.sip_trunk_preset)
    return preset.label if preset and preset.id != 'custom' else 'the carrier'


def verdict_text(record, carrier='the carrier'):
    """One sentence: whether T.38 can work through this network, and why."""
    if not record:
        return NOT_CHECKED
    why, can = record.get('why'), 'Fax over IP (T.38) can work here, because'
    texts = {
        'public': f'{can} this server has its own internet address.',
        'ports_kept': f'{can} your network keeps port numbers unchanged.',
        'typed': (f'{can} your network keeps port numbers unchanged.' if record['t38'] == OPEN else
                  "Fax over IP (T.38) works here only if your router forwards Faxbot's fax ports to this computer."),
        'forwarded': f"{can} your router forwards Faxbot's fax ports.",
        'router_mapped': f"{can} your router passes Faxbot's fax ports through.",
        't38_worked': f'{can} a T.38 fax already went through on this network.',
        'typed_differs': (f'The address you entered is not the one Faxbot sees ({record.get("internet_address")}), '
                          'so fax over IP (T.38) may not work.'),
        'shared_address': ('Fax over IP (T.38) cannot work here, because your internet provider shares your internet '
                           'address.'),
        'behind_another_router': ('Fax over IP (T.38) cannot work here yet: another router in front of yours changes '
                                  'port numbers.'),
        'no_address': ('Faxbot could not find its internet address, so it cannot tell yet whether fax over IP (T.38) '
                       'works here.'),
    }
    if why == 'ports_change':
        if carrier == 'Telnyx':
            # Measured 2026-10-03: Telnyx follows Faxbot's audio to a changed port, never its T.38 data.
            return ('Fax over IP (T.38) cannot work here: your network changes port numbers, which Telnyx cannot '
                    'handle for fax over IP.')
        return 'Fax over IP (T.38) most likely cannot work here, because your network changes port numbers.'
    return texts.get(why, NOT_CHECKED)


def router_sentence(record):
    """One sentence about Faxbot's fax ports on the router, or None when there is nothing to say."""
    mapping = (record or {}).get('router_ports') or {}
    state, ports = mapping.get('state'), _ports_text(record)
    if state == 'open':
        return (f'Faxbot opened UDP ports {ports} on your router so fax data can come back. It renews them while it '
                'runs and closes them when it stops.')
    if state == 'refused':
        return f'Your router did not open UDP ports {ports} for Faxbot.'
    if state == 'behind_another_router':
        return f'Your router opened UDP ports {ports}, but another router sits in front of it, so Faxbot closed them.'
    if state == 'off' and record and record['t38'] != OPEN:
        return 'Faxbot does not ask your router to open ports, because that is turned off.'
    return None


_INTERFACE = '"$(route -n get default | awk \'/interface:/{print $2}\')"'


def colima_steps(record):
    """Recreate Colima directly on the local network, keeping its size and Faxbot's data (Colima 0.9 or later)."""
    size = ''.join(f' --{flag} {record[key]}' for flag, key in (('cpu', 'cpus'), ('memory', 'memory_gib'),
                                                                 ('disk', 'disk_gib')) if record.get(key))
    return ['colima version   # 0.9 or later keeps your faxes when the machine is recreated',
            'colima list', 'colima delete default',
            f'colima start default{size} --network-address --network-mode bridged '
            f'--network-interface {_INTERFACE} --network-preferred-route',
            'docker compose up -d']


def fix(record):
    """What the operator does when Faxbot cannot fix the network itself: {text, steps, note}, or None.

    Commands go in ``steps``, one per line, so the sentence around them stays plain.
    """
    if not record or record['t38'] == OPEN:
        return None
    why, where = record.get('why'), record.get('platform')
    ports, published = _ports_text(record), bool(record.get('fax_ports'))
    mapping = (record.get('router_ports') or {}).get('state')
    address = 'enter your internet address under Internet address'
    command = [] if published else [FAX_PORTS_COMMAND]
    restart = '' if published else 'restart Faxbot with its fax ports (the command below), '

    def answer(text, steps=(), note=None):
        return {'text': text, 'steps': list(steps), 'note': note}
    if why == 'shared_address':
        return answer('No router setting can change this. To use fax over IP (T.38), run Faxbot on a server with its '
                      'own internet address.')
    if why == 'no_address':
        return answer('If a firewall limits outgoing traffic, let Faxbot reach stun.cloudflare.com on UDP port 3478.')
    if why == 'typed_differs':
        return answer('Empty the Internet address box so Faxbot uses the address it found, or correct it.')
    if where in ('colima_user', 'colima_shared', 'colima'):
        return answer('Connect Docker on this Mac directly to your local network with the commands below. Faxbot keeps '
                      'its faxes and settings, Docker stops for a few minutes, and your Mac may ask for its password '
                      'once.', colima_steps(record),
                      'Before you start: the second command lists the name and sizes to use (the name is default unless '
                      "you chose one). The disk keeps its old size. Run the last command in Faxbot's folder. Never add "
                      '--data to the delete command, because that erases your faxes.')
    if why == 'typed':
        return answer(f'Forward UDP ports {ports} on your router to this computer and restart Faxbot with its fax ports '
                      '(the command below).', [FAX_PORTS_COMMAND])
    if why == 'behind_another_router':
        return answer(f'Forward UDP ports {ports} on the router in front of yours too, then {address}.')
    if where == 'cloud':
        return answer(f'Give the server its own public address, open UDP ports {ports} in its firewall, {restart}'
                      f'and {address}.', command)
    if where == 'public_host':
        return answer(f"Open UDP ports {ports} in the server's firewall, {restart}and {address}.", command)
    if where and where.startswith('docker_desktop'):
        return answer(f'Forward UDP ports {ports} on your router to this computer, {restart}and {address}.', command)
    vm = where == 'colima_bridged'
    if mapping == 'refused':
        target = 'the address the command below lists' if vm else 'this computer'
        return answer(f'Turn on UPnP or NAT-PMP on your router so Faxbot can open UDP ports {ports} itself. Or forward '
                      f'those ports to {target} and {address}.', ['colima list'] if vm else [])
    if not published and mapping != 'off':
        target = 'the address the second command lists' if vm else 'this computer'
        return answer('Restart Faxbot with its fax ports (the first command below), and Faxbot asks your router to open '
                      f'them. If the router refuses, forward UDP ports {ports} to {target} and {address}.',
                      [FAX_PORTS_COMMAND] + (['colima list'] if vm else []))
    if vm and published:
        return answer(f'Forward UDP ports {ports} on your router to the address the command below lists, then '
                      f'{address}.', ['colima list'])
    if vm:
        return answer(f'Forward UDP ports {ports} on your router to the address the first command below lists, restart '
                      f'Faxbot with its fax ports (the second command), and {address}.',
                      ['colima list', FAX_PORTS_COMMAND])
    return answer(f'Forward UDP ports {ports} on your router to this computer, {restart}and {address}.', command)


def action(values):
    """What Faxbot did to T.38 because of the network ('turned_off' or 'turned_on', with its time), or (None, None)."""
    record = sip_fax_mode.read(values)
    if not record or record.get('reason') != sip_fax_mode.NETWORK:
        return None, None
    if record['mode'] == 'audio' and not values.sip_t38_enabled:
        return 'turned_off', record.get('at')
    if record['mode'] == 't38' and values.sip_t38_enabled:
        return 'turned_on', record.get('at')
    return None, None


def action_sentence(done, day=''):
    """One sentence for what Faxbot did; ``day`` is the date in the reader's own words."""
    when = f'On {day}, Faxbot' if day else 'Faxbot'
    if done == 'turned_off':
        return f'{when} switched new calls to audio fax.'
    if done == 'turned_on':
        return f'{when} switched new calls back to fax over IP (T.38), because your network allows it now.'
    return None


def _iso(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(float(epoch), timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'


def report(values):
    """Everything the console and the command line show about the network for fax over IP."""
    if not applies(values):
        return {'applies': False, 'checked': False, 't38': None, 'text': (
            PHONE_SYSTEM_TEXT if _phone_system(values) else 'No SIP trunk is set up. Choose your carrier to start.')}
    record = read_check(values)
    carrier = carrier_name(values)
    done, done_at = action(values)
    remedy = fix(record)
    return {
        'applies': True, 'checked': record is not None,
        'checked_at': _iso(record.get('checked_at')) if record else None,
        'platform': record.get('platform') if record else None,
        'platform_text': PLATFORM_TEXT.get(record.get('platform')) if record else None,
        'ports': record.get('ports') if record else None,
        'internet_address': record.get('internet_address') if record else None,
        'shared_address': bool(record and record.get('shared_address')),
        't38': record['t38'] if record else UNKNOWN,
        'why': record.get('why') if record else None,
        'text': verdict_text(record, carrier),
        'office_text': OFFICE_TEXT[record['t38']] if record else NOT_CHECKED,
        'tries_text': TRIES_FIRST if record and record['t38'] == OPEN else None,
        'fix_text': remedy['text'] if remedy else None,
        'fix_steps': remedy['steps'] if remedy else [],
        'fix_note': remedy['note'] if remedy else None,
        'audio_text': AUDIO_MEANWHILE if record and record['t38'] != OPEN else None,
        't38_enabled': bool(values.sip_t38_enabled),
        'action': done, 'action_at': done_at,
        'fax_ports': (record or {}).get('fax_ports') or f'{FAX_PORTS[0]}-{FAX_PORTS[1]}',
        'fax_ports_published': bool(record and record.get('fax_ports')),
        'router_ports_enabled': bool(values.sip_router_ports),
        'router_state': ((record or {}).get('router_ports') or {}).get('state'),
        'router_text': router_sentence(record),
    }


# -- running a check -----------------------------------------------------------------------------------

def _observed(records):
    from .sip_http import _observed as observed
    return observed(records)


def _records_for(runtime):
    from .sip_calls import SipCallRecords
    try:
        return SipCallRecords(runtime.manager.store.engine)
    except Exception:
        return None


def record_check(values, probe, found, records=None, mapping=None):
    """Assess and store a check; also records the probe for Asterisk's next start.

    The stored check counts how many checks in a row gave the same answer
    (``count``) and keeps what the network allowed before that run of answers
    (``changed_from``). ``advertise_changed`` says whether the address Asterisk
    names at its next start changed.
    """
    before = read_check(values)
    check = assess(values, probe, found, _observed(records) if records is not None else None, mapping=mapping,
                   published=read_media_ports(values))
    if before and before['t38'] == check['t38']:
        check['count'], check['changed_from'] = int(before.get('count') or 1) + 1, before.get('changed_from')
    else:
        check['count'], check['changed_from'] = 1, (before['t38'] if before else previous_verdict(values))
    write_check(values, check)
    check['advertise_changed'] = False
    if probe is not None and probe.public_ip and not values.sip_external_address:
        # Ports the router opened 1:1 make Faxbot's internet address and port numbers exact.
        exact = True if check['why'] == 'router_mapped' else None
        check['advertise_changed'] = sip_trunk.write_public_address(values, probe, exact=exact)
    return check


_locks = {}


def _lock():
    loop = asyncio.get_running_loop()
    found = _locks.get(id(loop))
    if found is None or found[0] is not loop:
        found = _locks[id(loop)] = (loop, asyncio.Lock())
    return found[1]


async def run_check(runtime, records=None, *, fresh=True, unattended=False):
    """Check the network now, store it, and turn T.38 on or off for new calls when the check says so.

    ``unattended`` (the check at start and the periodic one) switches only when
    two checks in a row gave the same answer; Apply and Check again switch at
    once. Returns {check, switched (None, 't38' or 'audio'), awaiting (a switch
    waiting for the next check), engine (what Asterisk did, or None)}, or None
    when there is no carrier trunk. Never resends a fax.
    """
    from .config_runtime import run_lifecycle_step
    from .sip_http import _load_into_engine, probe_network
    async with _lock():
        values = await run_lifecycle_step(lambda: runtime.manager.store.read().active.values)
        if not applies(values):
            await run_lifecycle_step(lambda: close_router_ports(values))
            return None
        probe = await probe_network(values.sip_trunk_preset, fresh=fresh)
        found = await discover(fresh=fresh)
        records = records if records is not None else _records_for(runtime)
        mapping = await run_lifecycle_step(lambda: map_ports(values, probe, found))
        check = await run_lifecycle_step(lambda: record_check(values, probe, found, records, mapping))
        decision = await run_lifecycle_step(lambda: sip_fax_mode.network_decision(
            values, check['t38'], previous=check['changed_from'], records=records))
        awaiting = None
        if unattended and decision and check['count'] < 2:
            awaiting, decision = decision, None
        engine = None
        if decision:
            engine = await sip_fax_mode.switch(runtime, decision == 't38', sip_fax_mode.NETWORK, network=check['t38'])
        elif check['advertise_changed'] and (check['why'] == 'router_mapped'
                                             or (mapping or {}).get('state') in ('refused', 'behind_another_router')):
            # Asterisk names the opened ports only after a restart, which waits until no call is up.
            if await run_lifecycle_step(lambda: sip_trunk.engine_managed(values)):
                engine = await _load_into_engine(values)
        return {'check': check, 'switched': decision, 'awaiting': awaiting, 'engine': engine}


# How long the start check waits before confirming an answer that differs from the stored one.
CONFIRM_SECONDS = 60


async def check_at_start(runtime, *, delay=START_DELAY_SECONDS, confirm=CONFIRM_SECONDS, keep=True):
    """The check at every start, once Asterisk and the network have settled; never raises.

    When the answer differs from the last stored one, a second check a minute
    later confirms it before T.38 is switched. Then (``keep``) the task keeps
    the router's ports renewed and closes them when Faxbot stops.
    """
    if delay and _test_mode():
        return None
    outcome = None
    try:
        await asyncio.sleep(delay)
        outcome = await run_check(runtime, unattended=True)
        if outcome and outcome.get('awaiting'):
            await asyncio.sleep(confirm)
            outcome = await run_check(runtime, unattended=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger(__name__).warning('Faxbot could not check its network for fax over IP.')
    if keep:
        await keep_router_ports(runtime)
    return outcome


async def keep_router_ports(runtime, *, idle=600):
    """Renew the ports the router opened for Faxbot at half their lifetime; close them when Faxbot stops."""
    from .config_runtime import run_lifecycle_step

    def current():
        return runtime.manager.store.read().active.values
    try:
        while True:
            values = await run_lifecycle_step(current)
            lease = await run_lifecycle_step(lambda: read_lease(values))
            await asyncio.sleep(idle if lease is None else min(idle, max(30.0, lease.renew_at - time.time())))
            lease = await run_lifecycle_step(lambda: read_lease(values))
            if lease is not None and time.time() >= lease.renew_at:
                try:
                    await run_check(runtime, unattended=True)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.getLogger(__name__).warning('Faxbot could not renew its fax ports on the router.')
    finally:
        try:
            values = current()
            await asyncio.wait_for(asyncio.to_thread(close_router_ports, values), 5)
        except BaseException:
            pass
