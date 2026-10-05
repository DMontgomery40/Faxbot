"""Faxbot asks the router to open its fax ports: PCP, then NAT-PMP, then UPnP IGD.

Every router here is a fake on the loopback address that speaks the real wire
format (RFC 6887, RFC 6886, UPnP IGD over SSDP and SOAP). No real router is
asked for anything.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import socket
import struct
import threading

import pytest

from app import port_mapping
from app.port_mapping import Lease, Refused, Router

EXTERNAL = '198.51.100.7'
CLIENT = '192.168.1.20'


class FakeRouter:
    """A NAT-PMP and (optionally) PCP router on 127.0.0.1 that keeps the mappings it granted."""

    def __init__(self, *, pcp=True, natpmp=True, taken=(), refuse=None, external=EXTERNAL, lifetime=None):
        self.pcp, self.natpmp, self.taken, self.refuse = pcp, natpmp, set(taken), refuse
        self.external, self.lifetime = external, lifetime
        self.mappings, self.requests = {}, []
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.port = self.socket.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        try:
            while True:
                data, source = self.socket.recvfrom(2048)
                reply = self.answer(data)
                if reply:
                    self.socket.sendto(reply, source)
        except OSError:
            return

    def answer(self, data):
        version = data[0]
        if version == 2:
            self.requests.append('pcp')
            if not self.pcp:  # a NAT-PMP router answers in its own version: unsupported version
                return struct.pack('!BBHI', 0, 128 + (data[1] & 0x7f), 1, 0) if self.natpmp else None
            return self.pcp_answer(data)
        if version == 0 and self.natpmp:
            self.requests.append('natpmp')
            return self.natpmp_answer(data)
        return None

    def natpmp_answer(self, data):
        if data[1] == 0:
            return struct.pack('!BBHI4s', 0, 128, 0, 1, socket.inet_aton(self.external))
        _, _, _, internal, external, lifetime = struct.unpack('!BBHHHI', data[:12])
        if self.refuse == 'natpmp':
            return struct.pack('!BBHIHHI', 0, 129, 2, 1, internal, 0, 0)
        if lifetime == 0:
            self.mappings.pop(internal, None)
            return struct.pack('!BBHIHHI', 0, 129, 0, 1, internal, 0, 0)
        granted = internal + 1000 if internal in self.taken else external
        self.mappings[internal] = ('natpmp', granted, lifetime)
        return struct.pack('!BBHIHHI', 0, 129, 0, 1, internal, granted, self.lifetime or lifetime)

    def pcp_answer(self, data):
        lifetime, = struct.unpack('!I', data[4:8])
        client = socket.inet_ntoa(data[20:24])
        nonce, internal, external = data[24:36], *struct.unpack('!HH', data[40:44])
        prefer_failure = len(data) > 60 and data[60] == 2

        def reply(result, port=0, granted=0):
            header = struct.pack('!BBBBII12x', 2, 0x81, 0, result, granted, 1)
            body = nonce + struct.pack('!B3xHH', 17, internal, port) + b'\x00' * 10 + b'\xff\xff' + \
                socket.inet_aton(self.external)
            return header + body
        if self.refuse == 'pcp':
            return reply(2)
        if client != CLIENT:
            return reply(12)  # ADDRESS_MISMATCH
        if lifetime == 0:
            self.mappings.pop(internal, None)
            return reply(0)
        if internal in self.taken:
            return reply(11) if prefer_failure else reply(0, internal + 1000, lifetime)
        self.mappings[internal] = ('pcp', external, lifetime)
        return reply(0, external, self.lifetime or lifetime)

    def close(self):
        self.socket.close()


@pytest.fixture
def router():
    made = []

    def make(**options):
        made.append(FakeRouter(**options))
        return made[-1]
    yield make
    for item in made:
        item.close()


def client_for(fake, **options):
    return Router('127.0.0.1', port=fake.port, ssdp_port=1, client=options.pop('client', CLIENT),
                  in_container=True, **options)


def test_pcp_comes_first_and_maps_every_port_to_the_same_number(router):
    fake = router()
    lease, reasons = client_for(fake).open(4000, 4002, lifetime=600)
    assert reasons is None and (lease.method, lease.first, lease.last, lease.external_ip) == ('pcp', 4000, 4002, EXTERNAL)
    assert fake.mappings == {port: ('pcp', port, 600) for port in (4000, 4001, 4002)}
    assert lease.renew_at == pytest.approx(lease.granted_at + 300)
    Router('127.0.0.1', port=fake.port, client=CLIENT, in_container=True).close(lease)
    assert fake.mappings == {}


def test_a_nat_pmp_router_answers_pcp_in_its_own_version_and_nat_pmp_maps_the_computer_that_asked(router):
    fake = router(pcp=False)
    lease, reasons = client_for(fake, client=None).open(4000, 4001)
    assert reasons is None and lease.method == 'natpmp' and lease.external_ip == EXTERNAL
    assert 'pcp' not in fake.requests  # PCP needs the computer's own address, which a container cannot see
    assert set(fake.mappings) == {4000, 4001}
    renewed = client_for(fake, client=None).renew(lease, lifetime=1200)
    assert renewed.lifetime == 1200 and fake.mappings[4000][2] == 1200


def test_a_port_the_router_gives_elsewhere_is_handed_back_and_nothing_stays_open(router):
    fake = router(pcp=False, taken={4001})
    lease, reasons = client_for(fake, client=None).open(4000, 4002)
    assert lease is None and reasons == ['pcp_no_address', 'port_taken', 'upnp_no_address']
    assert fake.mappings == {}


def test_pcp_prefer_failure_refusal_falls_back_to_nat_pmp_which_also_refuses(router):
    fake = router(taken={4001})
    lease, reasons = client_for(fake).open(4000, 4002)
    assert lease is None and reasons[:2] == ['pcp_11', 'port_taken'] and fake.mappings == {}


def test_a_silent_router_or_a_wrong_address_never_raises(router):
    fake = router(pcp=False, natpmp=False)
    lease, reasons = Router('127.0.0.1', port=fake.port, ssdp_port=fake.port, client=CLIENT, in_container=True,
                            send=lambda *a, **k: port_mapping.exchange(*a, **{**k, 'tries': 1, 'wait': 0.05})
                            ).open(4000, 4001)
    assert lease is None and reasons == ['pcp_no_answer', 'natpmp_no_answer', 'upnp_no_answer']
    mismatch = router()
    lease, reasons = client_for(mismatch, client='192.168.1.99').open(4000, 4000)
    assert lease.method == 'natpmp' and reasons is None  # PCP said ADDRESS_MISMATCH; NAT-PMP maps the sender


def test_the_pcp_request_is_the_rfc_6887_map_layout():
    request = port_mapping.pcp_request(CLIENT, 4000, 4000, 3600, bytes(range(12)))
    assert len(request) == 24 + 36 + 4
    assert request[:2] == b'\x02\x01' and struct.unpack('!I', request[4:8])[0] == 3600
    assert request[8:24] == b'\x00' * 10 + b'\xff\xff' + socket.inet_aton(CLIENT)
    assert request[24:36] == bytes(range(12)) and request[36] == 17
    assert struct.unpack('!HH', request[40:44]) == (4000, 4000) and request[60:64] == b'\x02\x00\x00\x00'


# -- UPnP IGD ------------------------------------------------------------------------------------------------

DESCRIPTION = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device><deviceList><device><deviceList><device>
<serviceList><service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
<controlURL>/ctl/IPConn</controlURL></service></serviceList></device></deviceList></device></deviceList></device></root>
"""


class FakeIgd:
    """An SSDP answer on a UDP port and the description and SOAP control on an HTTP port, all on 127.0.0.1."""

    def __init__(self, *, permanent_only=False, location_host='127.0.0.1', fault=None):
        self.mappings, self.permanent_only, self.fault = {}, permanent_only, fault
        igd = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.reply(200, DESCRIPTION)

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length'])).decode()
                action = self.headers['SOAPAction'].strip('"').split('#')[1]
                args = dict(re.findall(r'<(New\w+)>([^<]*)</', body))
                if action == 'GetExternalIPAddress':
                    return self.reply(200, f'<s:Envelope><s:Body><u:r><NewExternalIPAddress>{EXTERNAL}'
                                           '</NewExternalIPAddress></u:r></s:Body></s:Envelope>')
                if action == 'AddPortMapping':
                    if igd.fault or (igd.permanent_only and args['NewLeaseDuration'] != '0'):
                        code = igd.fault or 725
                        return self.reply(500, f'<s:Envelope><s:Body><s:Fault><detail><UPnPError><errorCode>{code}'
                                               '</errorCode></UPnPError></detail></s:Fault></s:Body></s:Envelope>')
                    igd.mappings[int(args['NewExternalPort'])] = (args['NewInternalClient'],
                                                                  int(args['NewLeaseDuration']))
                if action == 'DeletePortMapping':
                    igd.mappings.pop(int(args['NewExternalPort']), None)
                self.reply(200, '<s:Envelope><s:Body/></s:Envelope>')

            def reply(self, status, text):
                data = text.encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ssdp.bind(('127.0.0.1', 0))
        self.location = f'http://{location_host}:{self.http.server_address[1]}/rootDesc.xml'
        threading.Thread(target=self.answer, daemon=True).start()

    def answer(self):
        try:
            while True:
                data, source = self.ssdp.recvfrom(2048)
                if data.startswith(b'M-SEARCH'):
                    self.ssdp.sendto(f'HTTP/1.1 200 OK\r\nLOCATION: {self.location}\r\n'
                                     'ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n'.encode(), source)
        except OSError:
            return

    def close(self):
        self.ssdp.close()
        self.http.shutdown()


@pytest.fixture
def igd():
    made = []

    def make(**options):
        made.append(FakeIgd(**options))
        return made[-1]
    yield make
    for item in made:
        item.close()


def upnp_only(igd_router, nat):
    """PCP and NAT-PMP answered by a router that speaks neither, then UPnP."""
    return Router('127.0.0.1', port=nat.port, ssdp_port=igd_router.ssdp.getsockname()[1], client=CLIENT,
                  in_container=True)


def test_upnp_maps_each_port_to_this_computer_and_removes_them(router, igd):
    gateway, silent = igd(), router(pcp=False, natpmp=False)
    client = upnp_only(gateway, silent)
    client.send = lambda payload, address, **kw: port_mapping.exchange(payload, address, **{**kw, 'tries': 1,
                                                                                            'wait': 0.05})
    lease, reasons = client.open(4000, 4002, lifetime=900)
    assert reasons is None and lease.method == 'upnp' and lease.external_ip == EXTERNAL
    assert gateway.mappings == {port: (CLIENT, 900) for port in (4000, 4001, 4002)}
    assert lease.control_url.endswith('/ctl/IPConn')
    client.close(lease)
    assert gateway.mappings == {}


def test_a_router_that_only_keeps_permanent_mappings_gets_them_and_faxbot_still_removes_them(router, igd):
    gateway, silent = igd(permanent_only=True), router(pcp=False, natpmp=False)
    client = upnp_only(gateway, silent)
    client.send = lambda payload, address, **kw: port_mapping.exchange(payload, address, **{**kw, 'tries': 1,
                                                                                            'wait': 0.05})
    lease, _ = client.open(4000, 4001)
    assert lease.lifetime == 0 and gateway.mappings == {4000: (CLIENT, 0), 4001: (CLIENT, 0)}
    client.close(lease)
    assert gateway.mappings == {}


def test_upnp_faults_and_descriptions_elsewhere_are_refusals(router, igd):
    silent = router(pcp=False, natpmp=False)
    faulty = upnp_only(igd(fault=718), silent)
    faulty.send = lambda payload, address, **kw: port_mapping.exchange(payload, address, **{**kw, 'tries': 1,
                                                                                            'wait': 0.05})
    assert faulty.open(4000, 4001)[1][-1] == 'upnp_718'
    # An answer pointing at another address is never followed.
    elsewhere = igd(location_host='192.0.2.1')
    assert port_mapping.upnp_find('127.0.0.1', port=elsewhere.ssdp.getsockname()[1]) is None


def test_a_lease_survives_a_restart_as_a_record():
    lease = Lease('pcp', '192.168.1.1', 4000, 4039, EXTERNAL, 3600, 1791075343.0, nonce='00' * 12, client=CLIENT)
    assert Lease.from_dict(lease.as_dict()) == lease
    assert Lease.from_dict({'method': 'pcp'}) is None
    with pytest.raises(Refused):
        port_mapping.natpmp_external('127.0.0.1', send=lambda *a, **k: struct.pack('!BBHI4s', 0, 128, 3, 0, b'\0' * 4))
