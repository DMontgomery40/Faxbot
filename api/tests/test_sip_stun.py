"""STUN: Faxbot learns its internet address and how the network treats port numbers.

Every exchange here stays on this computer: canned responses for the parser
and a STUN server on the loopback address for the probe.
"""
import socket
import struct
import threading

import pytest

from app import stun


TRANSACTION = bytes(range(12))


def response(attributes, *, transaction=TRANSACTION, kind=stun.BINDING_SUCCESS):
    body = b''.join(struct.pack('!HH', attribute, len(value)) + value + b'\0' * (-len(value) % 4)
                    for attribute, value in attributes)
    return struct.pack('!HHI', kind, len(body), stun.MAGIC) + transaction + body


def xor_mapped(address, port):
    raw = bytes(a ^ b for a, b in zip(socket.inet_aton(address), struct.pack('!I', stun.MAGIC)))
    return stun.XOR_MAPPED_ADDRESS, struct.pack('!BBH', 0, 1, port ^ (stun.MAGIC >> 16)) + raw


def mapped(address, port):
    return stun.MAPPED_ADDRESS, struct.pack('!BBH', 0, 1, port) + socket.inet_aton(address)


def test_binding_request_is_the_rfc_5389_header():
    request = stun.binding_request(TRANSACTION)
    assert request == bytes.fromhex('000100002112a442') + TRANSACTION
    with pytest.raises(ValueError):
        stun.binding_request(b'short')


def test_xor_mapped_address_wins_and_legacy_servers_still_work():
    both = response([mapped('192.0.2.1', 1), (0x8022, b'synthetic-server'), xor_mapped('198.51.100.7', 61001)])
    assert stun.parse_binding_response(both, TRANSACTION) == ('198.51.100.7', 61001)
    assert stun.parse_binding_response(response([mapped('203.0.113.9', 5004)]), TRANSACTION) == ('203.0.113.9', 5004)


@pytest.mark.parametrize('data', [
    response([xor_mapped('198.51.100.7', 1)], transaction=bytes(12)),  # someone else's transaction
    response([xor_mapped('198.51.100.7', 1)], kind=0x0111),            # an error response
    response([]),                                                       # no address at all
    response([(stun.XOR_MAPPED_ADDRESS, b'\0\x02' + b'\0' * 18)]),     # IPv6 only
    b'\0' * 12, response([xor_mapped('198.51.100.7', 1)])[:-3],        # truncated
])
def test_anything_but_a_matching_ipv4_answer_is_ignored(data):
    assert stun.parse_binding_response(data, TRANSACTION) is None


@pytest.mark.parametrize('local,public,ports,behind,kind', [
    ('172.18.0.5', '198.51.100.7', (40000, 40000), True, 'preserved'),
    ('172.18.0.5', '198.51.100.7', (61001, 61001), True, 'consistent'),
    ('172.18.0.5', '198.51.100.7', (61001, 61002), True, 'changes'),
    ('172.18.0.5', '198.51.100.7', (61001, None), True, 'changes'),
    ('198.51.100.7', '198.51.100.7', (40000, None), False, 'preserved'),
    ('172.18.0.5', None, (None, None), None, None),
])
def test_probe_results_classify_address_and_port_behavior(local, public, ports, behind, kind):
    result = stun.Probe(public_ip=public, local_ip=local, local_port=40000,
                        mapped=tuple(zip(('a', 'b'), ports)))
    assert (result.behind_nat, result.ports) == (behind, kind)
    assert result.as_dict()['mapped_ports'] == list(ports)


def test_each_carrier_preset_asks_its_own_stun_server_first():
    assert stun.servers_for('telnyx') == ('stun.telnyx.com:3478', 'stun.cloudflare.com:3478')
    assert stun.servers_for('custom') == ('stun.cloudflare.com:3478', 'stun.l.google.com:19302')


class LoopbackServer:
    """A STUN server on 127.0.0.1 that reports the source it saw, optionally with another port."""

    def __init__(self, *, port_offset=0, answer=True):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('127.0.0.1', 0))
        self.socket.settimeout(5)
        self.port_offset, self.answer = port_offset, answer
        self.requests = 0
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return f'127.0.0.1:{self.socket.getsockname()[1]}'

    def serve(self):
        try:
            while True:
                data, source = self.socket.recvfrom(2048)
                self.requests += 1
                if self.answer:
                    reply = response([xor_mapped(source[0], source[1] + self.port_offset)], transaction=data[8:20])
                    self.socket.sendto(reply, source)
        except OSError:
            return

    def close(self):
        self.socket.close()


def test_probe_asks_every_server_from_one_local_port():
    first, second = LoopbackServer(), LoopbackServer()
    try:
        result = stun.probe((first.address, second.address), now=lambda: 1791075343.0)
    finally:
        first.close()
        second.close()
    assert result.public_ip == result.local_ip == '127.0.0.1'
    assert [port for _, port in result.mapped] == [result.local_port, result.local_port]
    assert (result.behind_nat, result.ports, result.probed_at) == (False, 'preserved', 1791075343.0)


def test_probe_sees_a_network_that_changes_port_numbers():
    first, second = LoopbackServer(port_offset=1), LoopbackServer(port_offset=2)
    try:
        result = stun.probe((first.address, second.address))
    finally:
        first.close()
        second.close()
    assert result.ports == 'changes' and result.answered == [result.local_port + 1, result.local_port + 2]


def test_a_silent_or_unknown_server_leaves_no_address_and_never_raises():
    silent = LoopbackServer(answer=False)
    try:
        result = stun.probe((silent.address, 'stun.invalid:3478'), attempts=2, wait=0.1)
    finally:
        silent.close()
    assert result.public_ip is None and result.ports is None and result.behind_nat is None
    assert silent.requests == 2
    assert stun.address_sentence(result, carrier='Telnyx') == (
        "Faxbot could not learn its internet address, so Telnyx must follow Faxbot's packets; "
        'the first test fax shows whether it does.')
