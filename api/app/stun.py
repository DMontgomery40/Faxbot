"""Learn Faxbot's internet address with STUN, so nobody has to type it.

A small RFC 5389 client: one Binding Request per server from one local UDP
socket, reading XOR-MAPPED-ADDRESS (MAPPED-ADDRESS from older servers). Asking
two servers from the same socket also shows how the network treats port
numbers, which decides whether the carrier can be told an exact media address
or has to follow Faxbot's packets:

``preserved``   every server saw the local port: the network keeps port numbers.
``consistent``  both servers saw the same, different port.
``changes``     the servers saw different ports, or the only answer differs.

Nothing here opens a port or keeps state. Errors never raise; an unanswered
probe returns a result with no address.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import secrets
import socket
import struct
import time


MAGIC = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
MAPPED_ADDRESS = 0x0001
XOR_MAPPED_ADDRESS = 0x0020
# The carrier's own STUN server first (Telnyx documents stun.telnyx.com:3478 UDP
# for SIP clients behind NAT), then a second server only to compare port numbers.
CARRIER_SERVERS = {'telnyx': 'stun.telnyx.com:3478'}
COMPARISON_SERVER = 'stun.cloudflare.com:3478'
FALLBACK_SERVER = 'stun.l.google.com:19302'


def servers_for(preset):
    """The STUN servers Faxbot asks for this carrier preset, carrier first."""
    first = CARRIER_SERVERS.get(preset)
    return (first, COMPARISON_SERVER) if first else (COMPARISON_SERVER, FALLBACK_SERVER)


def binding_request(transaction):
    if len(transaction) != 12:
        raise ValueError('A STUN transaction ID has 12 bytes.')
    return struct.pack('!HHI', BINDING_REQUEST, 0, MAGIC) + transaction


def parse_binding_response(data, transaction):
    """The (address, port) a STUN server saw, or None for anything but a matching IPv4 answer."""
    if len(data) < 20:
        return None
    kind, length, magic = struct.unpack('!HHI', data[:8])
    if kind != BINDING_SUCCESS or magic != MAGIC or data[8:20] != transaction or len(data) < 20 + length:
        return None
    found = {}
    offset, end = 20, 20 + length
    while offset + 4 <= end:
        attribute, size = struct.unpack('!HH', data[offset:offset + 4])
        value = data[offset + 4:offset + 4 + size]
        if len(value) < size:
            return None
        if attribute in (MAPPED_ADDRESS, XOR_MAPPED_ADDRESS) and size >= 8 and value[1] == 0x01:
            port, raw = struct.unpack('!H4s', value[2:8])
            if attribute == XOR_MAPPED_ADDRESS:
                port ^= MAGIC >> 16
                raw = bytes(a ^ b for a, b in zip(raw, struct.pack('!I', MAGIC)))
            found[attribute] = (str(ipaddress.IPv4Address(raw)), port)
        offset += 4 + size + (-size % 4)
    return found.get(XOR_MAPPED_ADDRESS) or found.get(MAPPED_ADDRESS)


@dataclass(frozen=True)
class Probe:
    """What the STUN servers saw. ``mapped`` holds (server, port or None) pairs in the order asked."""
    public_ip: str | None
    local_ip: str | None
    local_port: int | None
    mapped: tuple = ()
    probed_at: float = 0.0

    @property
    def answered(self):
        return [port for _, port in self.mapped if port is not None]

    @property
    def behind_nat(self):
        """True or False once a server answered; None when Faxbot could not tell."""
        if self.public_ip is None or self.local_ip is None:
            return None
        return self.public_ip != self.local_ip

    @property
    def ports(self):
        answered = self.answered
        if not answered:
            return None
        if all(port == self.local_port for port in answered):
            return 'preserved'
        if len(answered) > 1 and len(set(answered)) == 1:
            return 'consistent'
        return 'changes'

    def as_dict(self):
        return {'public_ip': self.public_ip, 'local_ip': self.local_ip, 'local_port': self.local_port,
                'mapped_ports': [port for _, port in self.mapped], 'behind_nat': self.behind_nat,
                'ports': self.ports, 'probed_at': self.probed_at}


def _address(server, resolve):
    host, _, port = server.rpartition(':')
    try:
        for family, _, _, _, address in resolve(host, int(port), socket.AF_INET, socket.SOCK_DGRAM):
            if family == socket.AF_INET:
                return address[0], int(port)
    except (OSError, ValueError):
        return None
    return None


def _local_ip(target):
    """The address this host uses toward ``target`` (no packet is sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(target)
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def probe(servers, *, attempts=3, wait=0.7, resolve=socket.getaddrinfo, now=time.time):
    """Ask each server once (``attempts`` tries, ``wait`` seconds apart) from one local socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mapped, public_ip, local_ip = [], None, None
    try:
        sock.bind(('0.0.0.0', 0))
        local_port = sock.getsockname()[1]
        for server in servers:
            target = _address(server, resolve)
            if target is None:
                mapped.append((server, None))
                continue
            local_ip = local_ip or _local_ip(target)
            transaction = secrets.token_bytes(12)
            seen = None
            for _ in range(attempts):
                try:
                    sock.sendto(binding_request(transaction), target)
                except OSError:
                    break
                deadline = time.monotonic() + wait
                while seen is None and time.monotonic() < deadline:
                    sock.settimeout(max(deadline - time.monotonic(), 0.01))
                    try:
                        data, source = sock.recvfrom(2048)
                    except (socket.timeout, OSError):
                        break
                    if source == target:
                        seen = parse_binding_response(data, transaction)
                if seen is not None:
                    break
            mapped.append((server, seen[1] if seen else None))
            if seen and public_ip is None:
                public_ip = seen[0]
    except OSError:
        local_port = None
    finally:
        sock.close()
    return Probe(public_ip=public_ip, local_ip=local_ip, local_port=local_port, mapped=tuple(mapped),
                 probed_at=now())


def address_sentence(result, *, carrier='the carrier'):
    """One plain sentence about Faxbot's internet address and how the network treats port numbers."""
    if result is None or result.public_ip is None:
        return (f'Faxbot could not learn its internet address, so {carrier} must follow Faxbot\'s packets; '
                'the first test fax shows whether it does.')
    if result.behind_nat is False:
        return f'Faxbot\'s internet address is {result.public_ip}, and Faxbot is not behind a router.'
    if result.ports == 'preserved':
        return f'Faxbot\'s internet address is {result.public_ip}, and your network keeps port numbers.'
    return (f'Faxbot\'s internet address is {result.public_ip}; your network changes port numbers, so {carrier} '
            'has to follow Faxbot\'s packets, and the first test fax shows whether it does.')
