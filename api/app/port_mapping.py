"""Ask the router to open Faxbot's fax ports: PCP first, then NAT-PMP, then UPnP IGD.

When Faxbot's computer sits directly on a local network whose router changes
port numbers, the carrier's T.38 data comes back only if the router forwards
Faxbot's fixed fax ports (``docker-compose.fax-ports.yml`` publishes them on
the computer). Many routers let a program on the local network ask for that:

- PCP (RFC 6887): a MAP request per port, with the PREFER_FAILURE option so
  the router either gives exactly that port or nothing.
- NAT-PMP (RFC 6886): a UDP mapping request per port; the router maps the
  computer the request came from, which is why it also works from a container.
- UPnP IGD (WANIPConnection or WANPPPConnection): found with an SSDP search sent
  straight to the router (multicast does not leave Docker's network), then
  AddPortMapping per port.

Every port must be mapped to the same number outside, because Asterisk names
its own port numbers in the call; anything less is handed back at once. Leases
last an hour and are renewed at half time; Faxbot closes them when it stops.
PCP and UPnP name the computer that receives the ports, which a container cannot
see: they need ``FAXBOT_LAN_ADDRESS`` (as for the phone system file) or Faxbot
running outside a container; NAT-PMP needs nothing.

Nothing here raises to the caller or logs addresses; every exchange has a short
timeout. Tests talk to fake routers on the loopback address.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import http.client
import ipaddress
import os
import re
import secrets
import socket
import struct
import time
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

NATPMP_PORT = PCP_PORT = 5351
SSDP_PORT = 1900
LIFETIME = 3600
DESCRIPTION = 'Faxbot fax ports'
METHODS = ('pcp', 'natpmp', 'upnp')
_MAX_BODY = 64 * 1024
_PCP_VERSION, _PCP_MAP, _PCP_PREFER_FAILURE, _UDP = 2, 1, 2, 17
_SERVICES = ('urn:schemas-upnp-org:service:WANIPConnection:2', 'urn:schemas-upnp-org:service:WANIPConnection:1',
             'urn:schemas-upnp-org:service:WANPPPConnection:1')


class Refused(Exception):
    """The router answered but would not map a port; ``reason`` is a short code, never an address."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Lease:
    """Ports the router opened for Faxbot: ``first``..``last`` outside go to the same ports on this computer."""
    method: str
    gateway: str
    first: int
    last: int
    external_ip: str | None
    lifetime: int
    granted_at: float
    nonce: str = ''
    client: str = ''
    control_url: str = ''
    service: str = ''
    ports: list = field(default_factory=list)

    @property
    def renew_at(self):
        """Half the lifetime from when it was granted; never for a permanent lease (lifetime 0)."""
        if not self.lifetime:
            return float('inf')
        return self.granted_at + max(self.lifetime, 120) / 2

    def as_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        try:
            return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})
        except (TypeError, KeyError):
            return None


# -- one UDP exchange ------------------------------------------------------------------------------------------

def exchange(payload, address, *, tries=3, wait=0.25, accept=None):
    """Send ``payload`` and return the first answer from ``address`` (``accept`` filters), or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for attempt in range(tries):
            try:
                sock.sendto(payload, address)
            except OSError:
                return None
            deadline = time.monotonic() + wait * (2 ** attempt)
            while time.monotonic() < deadline:
                sock.settimeout(max(deadline - time.monotonic(), 0.01))
                try:
                    data, source = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    break
                if source[0] == address[0] and (accept is None or accept(data)):
                    return data
        return None
    finally:
        sock.close()


# -- NAT-PMP ---------------------------------------------------------------------------------------------------

def natpmp_external(gateway, *, port=NATPMP_PORT, send=exchange):
    """The router's own internet address by NAT-PMP, or None when it does not answer."""
    data = send(b'\x00\x00', (gateway, port), accept=lambda d: len(d) >= 12 and d[:2] == b'\x00\x80')
    if not data:
        return None
    if struct.unpack('!H', data[2:4])[0] != 0:
        raise Refused('natpmp_refused')
    return socket.inet_ntoa(data[8:12])


def natpmp_map(gateway, internal, external, lifetime, *, port=NATPMP_PORT, send=exchange):
    """(external port, lifetime) for one UDP mapping; lifetime 0 removes it. None: no answer."""
    request = struct.pack('!BBHHHI', 0, 1, 0, internal, external, lifetime)
    data = send(request, (gateway, port),
                accept=lambda d: len(d) >= 16 and d[:2] == b'\x00\x81' and struct.unpack('!H', d[8:10])[0] == internal)
    if not data:
        return None
    result, = struct.unpack('!H', data[2:4])
    if result != 0:
        raise Refused('natpmp_refused')
    _, mapped, granted = struct.unpack('!HHI', data[8:16])
    return mapped, granted


# -- PCP -------------------------------------------------------------------------------------------------------

def _mapped_v6(address):
    return b'\x00' * 10 + b'\xff\xff' + socket.inet_aton(address)


def pcp_request(client, internal, external, lifetime, nonce, *, prefer_failure=True):
    """A PCP MAP request for one UDP port (RFC 6887 sections 7.1, 11.1 and 13.2)."""
    header = struct.pack('!BBHI', _PCP_VERSION, _PCP_MAP, 0, lifetime) + _mapped_v6(client)
    body = nonce + struct.pack('!B3xHH', _UDP, internal, external) + _mapped_v6('0.0.0.0')
    option = struct.pack('!BBH', _PCP_PREFER_FAILURE, 0, 0) if prefer_failure else b''
    return header + body + option


def pcp_map(gateway, client, internal, external, lifetime, nonce, *, port=PCP_PORT, send=exchange):
    """(external address, external port, lifetime) for one UDP mapping; lifetime 0 removes it.

    None: no answer. Raises Refused('pcp_unsupported') when a NAT-PMP-only router
    answers in its own version, and Refused('pcp_<code>') for any other refusal.
    """
    request = pcp_request(client, internal, external, lifetime, nonce)

    def accept(data):
        return len(data) >= 4 and (data[0] == 0 or (data[1] == 0x80 | _PCP_MAP and data[24:36] == nonce))
    data = send(request, (gateway, port), accept=accept)
    if not data:
        return None
    if data[0] != _PCP_VERSION:
        raise Refused('pcp_unsupported')
    result = data[3]
    if result == 1:
        raise Refused('pcp_unsupported')
    if result == 5:  # UNSUPP_OPTION: ask once more without PREFER_FAILURE and check the port ourselves
        data = send(pcp_request(client, internal, external, lifetime, nonce, prefer_failure=False), (gateway, port),
                    accept=accept)
        if not data:
            return None
        result = data[3]
    if result != 0:
        raise Refused(f'pcp_{result}')
    granted, = struct.unpack('!I', data[4:8])
    assigned_port, = struct.unpack('!H', data[42:44])
    assigned = socket.inet_ntoa(data[56:60]) if data[44:56] == b'\x00' * 10 + b'\xff\xff' else None
    return assigned, assigned_port, granted


# -- UPnP IGD --------------------------------------------------------------------------------------------------

def _http(method, url, body=None, headers=None, timeout=2.0):
    """(status, text) of one request, read up to 64 KB; never follows a redirect."""
    parts = urlsplit(url)
    if parts.scheme != 'http' or not parts.hostname:
        return None
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        connection.request(method, parts.path + (f'?{parts.query}' if parts.query else '') or '/', body=body,
                           headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(_MAX_BODY).decode('utf-8', 'replace')
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


SEARCH = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\n'
          'ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n').encode()


def upnp_find(gateway, *, port=SSDP_PORT, send=exchange, fetch=_http):
    """(control URL, service type) of the router's port mapping service, or None.

    Only a description served by the router itself is read, so an answer can
    never send Faxbot to another address.
    """
    data = send(SEARCH, (gateway, port), accept=lambda d: d.startswith(b'HTTP/1.1 200'))
    if not data:
        return None
    found = re.search(rb'^location:\s*(\S+)\s*$', data, re.IGNORECASE | re.MULTILINE)
    if not found:
        return None
    location = found.group(1).decode('ascii', 'replace')
    if urlsplit(location).hostname != gateway:
        return None
    answer = fetch('GET', location)
    if not answer or answer[0] != 200:
        return None
    try:
        root = ElementTree.fromstring(answer[1])
    except ElementTree.ParseError:
        return None
    base = next((element.text for element in root.iter() if element.tag.endswith('URLBase') and element.text), location)
    for wanted in _SERVICES:
        for service in root.iter():
            if not service.tag.endswith('service'):
                continue
            values = {child.tag.rsplit('}', 1)[-1]: (child.text or '').strip() for child in service}
            if values.get('serviceType') == wanted and values.get('controlURL'):
                control = urljoin(base, values['controlURL'])
                if urlsplit(control).hostname == gateway:
                    return control, wanted
    return None


def upnp_call(control, service, action, arguments, *, post=_http):
    """The response arguments of one SOAP action; raises Refused('upnp_<code>') on a fault."""
    body = ''.join(f'<{name}>{value}</{name}>' for name, value in arguments)
    envelope = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>')
    answer = post('POST', control, envelope.encode(), {'Content-Type': 'text/xml; charset="utf-8"',
                                                       'SOAPAction': f'"{service}#{action}"'})
    if not answer:
        raise Refused('upnp_no_answer')
    code = re.search(r'<(?:\w+:)?errorCode>\s*(\d+)\s*<', answer[1])
    if answer[0] != 200 or code:
        raise Refused(f'upnp_{code.group(1) if code else answer[0]}')
    # Out arguments are flat elements such as <NewExternalIPAddress>198.51.100.7</NewExternalIPAddress>.
    return {name: value.strip() for name, value in re.findall(r'<(?:\w+:)?(New\w+)>([^<]*)<', answer[1])}


def upnp_map(control, service, client, port, lifetime, *, post=_http):
    arguments = [('NewRemoteHost', ''), ('NewExternalPort', port), ('NewProtocol', 'UDP'), ('NewInternalPort', port),
                 ('NewInternalClient', client), ('NewEnabled', 1), ('NewPortMappingDescription', DESCRIPTION),
                 ('NewLeaseDuration', lifetime)]
    try:
        upnp_call(control, service, 'AddPortMapping', arguments, post=post)
        return lifetime
    except Refused as refused:
        if refused.reason != 'upnp_725' or not lifetime:  # OnlyPermanentLeasesSupported
            raise
    arguments[-1] = ('NewLeaseDuration', 0)
    upnp_call(control, service, 'AddPortMapping', arguments, post=post)
    return 0


def upnp_unmap(control, service, port, *, post=_http):
    upnp_call(control, service, 'DeletePortMapping',
              [('NewRemoteHost', ''), ('NewExternalPort', port), ('NewProtocol', 'UDP')], post=post)


# -- the whole range ---------------------------------------------------------------------------------------------

def client_address():
    """The computer's own address on the local network from FAXBOT_LAN_ADDRESS, or None."""
    try:
        return str(ipaddress.IPv4Address(os.environ.get('FAXBOT_LAN_ADDRESS', '').strip()))
    except ValueError:
        return None


def _local_toward(gateway):
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((gateway, NATPMP_PORT))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


class Router:
    """The calls Faxbot makes to one router; tests point ``port`` and ``ssdp_port`` at fake routers."""

    def __init__(self, gateway, *, port=NATPMP_PORT, ssdp_port=SSDP_PORT, send=exchange, http=_http, client=None,
                 in_container=None):
        self.gateway, self.port, self.ssdp_port, self.send, self.http = gateway, port, ssdp_port, send, http
        container = (os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv')) \
            if in_container is None else in_container
        self.client = client or client_address() or (None if container else _local_toward(gateway))

    def _release(self, method, ports, *, nonce=b'', control='', service='', client=None):
        """Delete each mapping; a router that stops answering is not asked about the rest."""
        client = client or self.client
        for port in ports:
            try:
                if method == 'pcp':
                    answer = pcp_map(self.gateway, client, port, 0, 0, nonce, port=self.port, send=self.send)
                elif method == 'natpmp':
                    answer = natpmp_map(self.gateway, port, 0, 0, port=self.port, send=self.send)
                else:
                    answer = upnp_unmap(control, service, port, post=self.http) or True
            except Refused as refused:
                if refused.reason == 'upnp_no_answer':
                    return
                continue
            if answer is None:
                return

    def _pcp(self, first, last, lifetime):
        if not self.client:
            raise Refused('pcp_no_address')
        nonce, done, external, granted = secrets.token_bytes(12), [], None, lifetime
        try:
            for port in range(first, last + 1):
                answer = pcp_map(self.gateway, self.client, port, port, lifetime, nonce, port=self.port,
                                 send=self.send)
                if answer is None:
                    raise Refused('pcp_no_answer')
                done.append(port)
                if answer[1] != port:
                    raise Refused('port_taken')
                external, granted = answer[0] or external, min(granted, answer[2])
        except Refused:
            self._release('pcp', done, nonce=nonce)
            raise
        return Lease('pcp', self.gateway, first, last, external, granted, time.time(), nonce=nonce.hex(),
                     client=self.client)

    def _natpmp(self, first, last, lifetime):
        external = natpmp_external(self.gateway, port=self.port, send=self.send)
        if external is None:
            raise Refused('natpmp_no_answer')
        done, granted = [], lifetime
        try:
            for port in range(first, last + 1):
                answer = natpmp_map(self.gateway, port, port, lifetime, port=self.port, send=self.send)
                if answer is None:
                    raise Refused('natpmp_no_answer')
                done.append(port)
                if answer[0] != port:
                    raise Refused('port_taken')
                granted = min(granted, answer[1])
        except Refused:
            self._release('natpmp', done)
            raise
        return Lease('natpmp', self.gateway, first, last, external, granted, time.time())

    def _upnp(self, first, last, lifetime):
        if not self.client:
            raise Refused('upnp_no_address')
        found = upnp_find(self.gateway, port=self.ssdp_port, send=self.send, fetch=self.http)
        if not found:
            raise Refused('upnp_no_answer')
        control, service = found
        external = upnp_call(control, service, 'GetExternalIPAddress', [], post=self.http).get('NewExternalIPAddress')
        done, granted, asked = [], lifetime, lifetime
        try:
            for port in range(first, last + 1):
                got = upnp_map(control, service, self.client, port, asked, post=self.http)
                done.append(port)
                # A router that keeps only permanent mappings gets permanent ones for every port.
                asked, granted = (0, 0) if got == 0 else (asked, min(granted, got))
        except Refused:
            self._release('upnp', done, control=control, service=service)
            raise
        return Lease('upnp', self.gateway, first, last, external or None, granted, time.time(), client=self.client,
                     control_url=control, service=service)

    def open(self, first, last, *, lifetime=LIFETIME):
        """(Lease, None) when the router opened every port 1:1, else (None, the reasons it did not, in order)."""
        reasons = []
        for method in METHODS:
            try:
                return getattr(self, f'_{method}')(first, last, lifetime), None
            except Refused as refused:
                reasons.append(refused.reason)
            except Exception:
                reasons.append(f'{method}_failed')
        return None, reasons

    def renew(self, lease, *, lifetime=LIFETIME):
        """The same lease with a new lifetime, or None when the router no longer grants it (it is then closed)."""
        try:
            if lease.method == 'pcp':
                granted = lifetime
                for port in range(lease.first, lease.last + 1):
                    answer = pcp_map(self.gateway, lease.client, port, port, lifetime, bytes.fromhex(lease.nonce),
                                     port=self.port, send=self.send)
                    if answer is None or answer[1] != port:
                        raise Refused('renew_failed')
                    granted = min(granted, answer[2])
            elif lease.method == 'natpmp':
                granted = lifetime
                for port in range(lease.first, lease.last + 1):
                    answer = natpmp_map(self.gateway, port, port, lifetime, port=self.port, send=self.send)
                    if answer is None or answer[0] != port:
                        raise Refused('renew_failed')
                    granted = min(granted, answer[1])
            else:
                granted = lifetime if lease.lifetime else 0
                for port in range(lease.first, lease.last + 1) if granted else ():
                    granted = min(granted, upnp_map(lease.control_url, lease.service, lease.client, port, granted,
                                                    post=self.http))
        except (Refused, ValueError, OSError):
            self.close(lease)
            return None
        lease.lifetime, lease.granted_at = granted, time.time()
        return lease

    def close(self, lease):
        """Ask the router to close every port of ``lease``; never raises."""
        try:
            self._release(lease.method, range(lease.first, lease.last + 1), nonce=bytes.fromhex(lease.nonce or ''),
                          control=lease.control_url, service=lease.service, client=lease.client or None)
        except Exception:
            pass
