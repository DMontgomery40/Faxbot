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
import socket
import struct
import sys
import time

from . import sip_fax_mode, sip_trunk

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


def verdict(probe, ports, shared, typed='', observed=None):
    """(t38, why): whether the carrier's T.38 fax data can come back to Faxbot, and the reason.

    OPEN: public, ports_kept, typed (an address typed in, on a network that keeps
    port numbers) or t38_worked (the newest T.38 call went through here).
    BLOCKED: ports_change or shared_address. UNKNOWN: no_address, typed_differs
    (the typed address is not the one STUN sees), or typed on a network that
    changes port numbers (it works only if the router forwards Faxbot's fax
    ports, which Faxbot cannot see).
    """
    worked = bool(observed and observed.get('t38_ok'))
    if typed and ports == 'kept':
        return (OPEN, 'typed') if probe.public_ip == typed else (UNKNOWN, 'typed_differs')
    if typed and ports is not None:
        return (OPEN, 't38_worked') if worked else (UNKNOWN, 'typed')
    if probe is None or not probe.public_ip:
        return (OPEN, 't38_worked') if worked else (UNKNOWN, 'no_address')
    if probe.behind_nat is False:
        return OPEN, 'public'
    if ports == 'kept':
        return OPEN, 'ports_kept'
    if worked:
        return OPEN, 't38_worked'
    return BLOCKED, ('shared_address' if shared else 'ports_change')


def assess(values, probe, found: Discovery, observed=None, *, now=time.time):
    """One network check, ready to store."""
    ports = port_behavior(probe)
    shared = shared_address(found, probe)
    typed = '' if _phone_system(values) else values.sip_external_address
    t38, why = verdict(probe, ports, shared, typed, observed)
    outside = outside_hops(found)
    router = next((hop for hop in outside if _local(_address(hop))), None)
    return {'checked_at': round(now(), 3), 'platform': platform(found, probe), 'ports': ports,
            'internet_address': probe.public_ip if probe else None,
            'behind_router': probe.behind_nat if probe else None, 'shared_address': shared,
            'router': router, 'hops': list(outside), 't38': t38, 'why': why, 'typed_address': typed or None,
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
    or the newest T.38 call went through; a phone system on the local network
    is always True. False: the network changes port numbers, so the carrier's
    T.38 data cannot find Faxbot; use audio fax. None: Faxbot cannot tell (no
    trunk, no check yet, the address lookup is blocked, or a typed address on a
    network that changes port numbers); try T.38, and the no-data-back rule in
    sip_fax_mode moves new calls to audio after one failed call.

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


# -- sentences -----------------------------------------------------------------------------------------

PLATFORM_TEXT = {
    'colima_user': "Faxbot runs in Colima on a Mac, on Colima's built-in network.",
    'colima_shared': "Faxbot runs in Colima on a Mac, on the Mac's shared network.",
    'colima_bridged': 'Faxbot runs in Colima on a Mac, directly on your local network.',
    'colima': 'Faxbot runs in Colima on a Mac.',
    'docker_desktop_mac': 'Faxbot runs in Docker Desktop, an app on your Mac.',
    'docker_desktop_windows': 'Faxbot runs in Docker Desktop, an app on your Windows computer.',
    'docker_desktop': 'Faxbot runs in Docker Desktop.',
    'cloud': 'Faxbot runs on a cloud server.',
    'public_host': 'Faxbot runs on a server with its own internet address.',
    'linux_lan': 'Faxbot runs on a computer on your local network, behind your router.',
}
NOT_CHECKED = 'Faxbot has not checked this network yet.'
PHONE_SYSTEM_TEXT = 'Your phone system is on your local network, so fax over IP needs no network check.'
AUDIO_MEANWHILE = 'Audio fax keeps working meanwhile.'
FAX_PORTS_TEXT = f'{FAX_PORTS[0]}–{FAX_PORTS[1]}'


def carrier_name(values):
    preset = sip_trunk.PRESETS.get(values.sip_trunk_preset)
    return preset.label if preset and preset.id != 'custom' else 'the carrier'


def _possessive(carrier):
    return "the carrier's" if carrier == 'the carrier' else f"{carrier}'s"


def verdict_text(record, carrier='the carrier'):
    """One sentence: whether T.38 fax data can come back, and why."""
    if not record:
        return NOT_CHECKED
    why, theirs = record.get('why'), _possessive(carrier)
    texts = {
        'public': f'Faxbot has its own internet address, so {theirs} T.38 fax data can come back to it.',
        'ports_kept': f'Your network keeps port numbers, so {theirs} T.38 fax data can come back to Faxbot.',
        'typed': (f'Your network keeps port numbers, and {carrier} sends fax data to the address you entered.'
                  if record['t38'] == OPEN else
                  'Your network changes port numbers, so T.38 fax data comes back only if your router forwards '
                  "Faxbot's fax ports to the address you entered."),
        'typed_differs': (f'The address you entered is not the one Faxbot sees from the internet '
                          f'({record.get("internet_address")}), so {theirs} T.38 fax data may not come back.'),
        't38_worked': f'A T.38 fax went through on this network, so {theirs} T.38 fax data comes back to Faxbot.',
        'shared_address': ('Your internet provider shares your internet address with other customers, so T.38 fax '
                           'cannot work here.'),
        'no_address': 'Faxbot could not find its internet address, so it cannot tell yet whether T.38 fax works here.',
    }
    if why == 'ports_change':
        if carrier == 'Telnyx':
            # Measured 2026-10-03: Telnyx follows Faxbot's audio to a changed port, never its T.38 data.
            return ('Your network changes port numbers, and Telnyx does not follow such changes for T.38 fax data, '
                    'so it cannot come back to Faxbot.')
        return f'Your network changes port numbers, so {theirs} T.38 fax data most likely cannot come back to Faxbot.'
    return texts.get(why, NOT_CHECKED)


_INTERFACE = '"$(route -n get default | awk \'/interface:/{print $2}\')"'


def colima_steps(record):
    """Recreate Colima directly on the local network, keeping its size and Faxbot's data (Colima 0.9 or later)."""
    size = ''.join(f' --{flag} {record[key]}' for flag, key in (('cpu', 'cpus'), ('memory', 'memory_gib'),
                                                                 ('disk', 'disk_gib')) if record.get(key))
    return ['colima list', 'colima delete default',
            f'colima start default{size} --network-address --network-mode bridged '
            f'--network-interface {_INTERFACE} --network-preferred-route',
            'docker compose up -d']


def fix(record):
    """What the operator does when Faxbot cannot fix the network itself: {text, steps, note}, or None."""
    if not record or record['t38'] == OPEN:
        return None
    why, where, address = record.get('why'), record.get('platform'), record.get('internet_address')
    typed = f', and enter your internet address, {address}, under Internet address' if address else \
        ', and enter your internet address under Internet address'
    forward = f'forward UDP ports {FAX_PORTS_TEXT} on your router to'
    start = 'start Faxbot with its fax ports using the command below'
    if why == 'shared_address':
        return {'text': ('No router setting can change this. To use T.38, run Faxbot on a server with its own '
                         'internet address, such as a rented virtual server.'), 'steps': [], 'note': None}
    if why == 'no_address':
        return {'text': ('If a firewall limits outgoing traffic, let Faxbot reach stun.cloudflare.com on UDP port 3478 '
                         'so it can learn its internet address.'), 'steps': [], 'note': None}
    if why == 'typed_differs':
        return {'text': 'Empty the Internet address box so Faxbot uses the address it found, or correct the address.',
                'steps': [], 'note': None}
    if where in ('colima_user', 'colima_shared', 'colima'):
        return {'text': ('Move Colima onto your office network with the commands below. Your faxes and settings '
                         'stay; everything in Colima pauses for a few minutes, and your Mac may ask for its password '
                         'once.'),
                'steps': colima_steps(record),
                'note': ('Use the name and sizes the first command shows for Faxbot (default when it has no name), '
                         'and run the last command in Faxbot\'s folder. Never add --data to the delete command: it '
                         'erases your faxes. Needs Colima 0.9 or later (colima version).')}
    if why == 'typed':
        return {'text': (f'Check that your router forwards UDP ports {FAX_PORTS_TEXT} to this computer and that Faxbot '
                         'runs with its fax ports (the command below), then turn T.38 on.'),
                'steps': [FAX_PORTS_COMMAND], 'note': None}
    if where == 'cloud':
        return {'text': (f'Give the server its own public address, open UDP ports {FAX_PORTS_TEXT} in its firewall or '
                         f'security group, and {start}.'), 'steps': [FAX_PORTS_COMMAND], 'note': None}
    if where == 'public_host':
        return {'text': f'Open UDP ports {FAX_PORTS_TEXT} in the server\'s firewall, {start}{typed}.',
                'steps': [FAX_PORTS_COMMAND], 'note': None}
    if where == 'colima_bridged':
        return {'text': (f'Your router changes port numbers: {forward} Colima\'s address (the ADDRESS column of '
                         f'colima list), {start}{typed}.'), 'steps': [FAX_PORTS_COMMAND], 'note': None}
    if where and where.startswith('docker_desktop'):
        return {'text': f'Docker Desktop changes port numbers: {forward} this computer, {start}{typed}.',
                'steps': [FAX_PORTS_COMMAND], 'note': None}
    return {'text': f'Your router changes port numbers: {forward} the computer that runs Faxbot, {start}{typed}.',
            'steps': [FAX_PORTS_COMMAND], 'note': None}


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
        return f'{when} switched new calls back to T.38 fax because your network allows it now.'
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
        'fix_text': remedy['text'] if remedy else None,
        'fix_steps': remedy['steps'] if remedy else [],
        'fix_note': remedy['note'] if remedy else None,
        'audio_text': AUDIO_MEANWHILE if record and record['t38'] != OPEN else None,
        't38_enabled': bool(values.sip_t38_enabled),
        'action': done, 'action_at': done_at,
        'fax_ports': f'{FAX_PORTS[0]}-{FAX_PORTS[1]}',
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


def record_check(values, probe, found, records=None):
    """Assess, store and return (check, previous verdict); also records the probe for Asterisk's next start."""
    previous = previous_verdict(values)
    check = assess(values, probe, found, _observed(records) if records is not None else None)
    write_check(values, check)
    if probe is not None and probe.public_ip and not values.sip_external_address:
        sip_trunk.write_public_address(values, probe)
    return check, previous


async def run_check(runtime, records=None, *, fresh=True):
    """Check the network now, store it, and turn T.38 on or off for new calls when the check says so.

    Returns {check, switched (None, 't38' or 'audio'), engine (what Asterisk did, or None)}, or None
    when there is no carrier trunk. Never resends a fax.
    """
    from .config_runtime import run_lifecycle_step
    from .sip_http import probe_network
    values = await run_lifecycle_step(lambda: runtime.manager.store.read().active.values)
    if not applies(values):
        return None
    probe = await probe_network(values.sip_trunk_preset, fresh=fresh)
    found = await discover(fresh=fresh)
    records = records if records is not None else _records_for(runtime)
    check, previous = await run_lifecycle_step(lambda: record_check(values, probe, found, records))
    decision = await run_lifecycle_step(
        lambda: sip_fax_mode.network_decision(values, check['t38'], previous=previous, records=records))
    engine = None
    if decision:
        engine = await sip_fax_mode.switch(runtime, decision == 't38', sip_fax_mode.NETWORK, network=check['t38'])
    return {'check': check, 'switched': decision, 'engine': engine}


async def check_at_start(runtime, *, delay=START_DELAY_SECONDS):
    """The check at every start, once Asterisk and the network have settled; never raises."""
    if delay and _test_mode():
        return None
    try:
        await asyncio.sleep(delay)
        return await run_check(runtime)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.getLogger(__name__).warning('Faxbot could not check its network for fax over IP.')
        return None
