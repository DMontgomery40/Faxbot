"""Certificate discovery on the wire: DNS CERT and SRV answers, and an anonymous LDAP search, against stand-ins
that speak the real formats (RFC 1035, RFC 4398, RFC 2782, RFC 4511). Synthetic names and certificates only."""
import struct

import pytest
from cryptography.hazmat.primitives import serialization

from api.app.digital import certificates, der, lookup
from api.app.direct import dnstxt
from api.tests.digital_fixtures import RECIPIENT, certificate, key, pki


def _der(item):
    return item.public_bytes(serialization.Encoding.DER)


def answer(packet, records, *, kind, rcode=0):
    """A DNS answer to ``packet`` carrying ``records`` (rdata) of ``kind``, each named by a pointer to the question."""
    identity = packet[:2]
    question = packet[12:packet.index(b'\x00', 12) + 5]
    head = identity + struct.pack('!HHHHH', 0x8180 | rcode, 1, len(records), 0, 0)
    body = b''.join(b'\xc0\x0c' + struct.pack('!HHIH', kind, 1, 300, len(rdata)) + rdata for rdata in records)
    return head + question + body


def test_cert_records_carry_pkix_certificates_and_srv_names_the_directory():
    world = pki()
    cert_rdata = [struct.pack('!HHB', lookup.CERT_PKIX, 0, 0) + _der(world.recipient),
                  struct.pack('!HHB', lookup.CERT_PKIX, 0, 0) + _der(world.intermediate)]
    seen = []

    def udp(server, packet, timeout):
        seen.append(packet)
        return answer(packet, cert_rdata, kind=lookup.TYPE_CERT)
    found = lookup.dns_certificates('records.direct.hospital.example.net', servers=['192.0.2.53'], udp=udp)
    assert found == [_der(world.recipient), _der(world.intermediate)]
    name_end = 12 + len(dnstxt.encode_name('records.direct.hospital.example.net'))
    assert struct.unpack('!HH', seen[0][name_end:name_end + 4]) == (lookup.TYPE_CERT, 1)
    target = dnstxt.encode_name('ldap.hospital.example.net')

    def srv_udp(server, packet, timeout):
        return answer(packet, [struct.pack('!HHH', 10, 5, 389) + target], kind=lookup.TYPE_SRV)
    assert lookup.srv_records('_ldap._tcp.direct.hospital.example.net', servers=['192.0.2.53'],
                              udp=srv_udp) == [(10, 5, 389, 'ldap.hospital.example.net')]

    def nxdomain(server, packet, timeout):
        return answer(packet, [], kind=lookup.TYPE_CERT, rcode=3)
    assert lookup.dns_certificates('nobody.example.net', servers=['192.0.2.53'], udp=nxdomain) == []
    with pytest.raises(LookupError):
        lookup.dns_certificates('records.example.net', servers=[])


class FakeDirectory:
    """An LDAP server socket: anonymous bind, the root entry's namingContexts, and (mail=...) searches."""

    def __init__(self, entries):
        self.entries = entries   # mail -> [DER certificates]
        self.out, self.filters, self.closed = b'', [], False

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        message = der.parse(data)
        message_id, operation = message.children()[:2]
        number = der.integer(message_id)
        if operation.tag == 0x60:
            self.out += der.sequence(der.encode_integer(number), der.tlv(0x61, der.tlv(0x0A, b'\x00')
                                                                         + der.encode_octets(b'')
                                                                         + der.encode_octets(b'')))
            return
        base, scope = operation.children()[:2]
        search_filter = operation.children()[6]
        reply = b''
        if base.value == b'' and scope.value == b'\x00':
            reply += self._entry(number, '', {'namingContexts': [b'dc=hospital,dc=example,dc=net']})
        elif search_filter.tag == 0xA3:
            attribute, value = search_filter.children()
            self.filters.append((attribute.value.decode(), value.value.decode()))
            found = self.entries.get(value.value.decode(), [])
            if found:
                reply += self._entry(number, 'cn=records', {'userCertificate;binary': found})
        reply += der.sequence(der.encode_integer(number), der.tlv(0x65, der.tlv(0x0A, b'\x00')
                                                                  + der.encode_octets(b'') + der.encode_octets(b'')))
        self.out += reply

    @staticmethod
    def _entry(number, dn, attributes):
        parts = b''.join(der.sequence(der.encode_octets(name), der.tlv(0x31, b''.join(
            der.encode_octets(value) for value in values))) for name, values in attributes.items())
        return der.sequence(der.encode_integer(number), der.tlv(0x64, der.encode_octets(dn)
                                                                + der.sequence(parts)))

    def recv(self, size):
        data, self.out = self.out[:size], self.out[size:]
        return data

    def close(self):
        self.closed = True


def test_an_anonymous_ldap_search_finds_the_address_certificate_then_the_domain():
    world = pki()
    directory = FakeDirectory({'direct.hospital.example.net': [_der(world.domain_bound)]})
    connected = []

    def connect(address, timeout):
        connected.append(address)
        return directory
    found = lookup.ldap_certificates(RECIPIENT, connect=connect,
                                     srv=lambda name: [(0, 0, 389, 'ldap.hospital.example.net')])
    assert found == [_der(world.domain_bound)]
    assert connected == [('ldap.hospital.example.net', 389)]
    assert directory.filters == [('mail', RECIPIENT), ('mail', 'direct.hospital.example.net')]
    with pytest.raises(LookupError, match='publishes no directory'):
        lookup.ldap_certificates(RECIPIENT, srv=lambda name: [])


def test_a_missing_issuer_is_read_from_the_certificates_ca_issuers_address():
    world = pki()
    leaf_key = key()
    leaf = certificate('Hospital records', leaf_key, world.intermediate.subject, world.intermediate_key,
                       email=RECIPIENT, ca_issuers='http://ca.hospital.example.net/hisp-ca.cer')
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url == 'http://ca.hospital.example.net/hisp-ca.cer':
            return _der(world.intermediate)
        raise LookupError('no list')
    with pytest.raises(certificates.CertificateRefused):
        certificates.check(leaf, anchors=[world.anchor], address=RECIPIENT)
    checked = certificates.check(leaf, anchors=[world.anchor], address=RECIPIENT, crl_fetch=fetch)
    assert checked.path == (leaf, world.intermediate, world.anchor)
    assert 'http://ca.hospital.example.net/hisp-ca.cer' in fetched
