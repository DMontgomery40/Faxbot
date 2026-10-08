"""Finding a Direct recipient's certificate: DNS CERT and SRV records, and an anonymous LDAP search.

DNS (RFC 4398 CERT, RFC 2782 SRV) reuses the partner directory's standard-library
DNS client (``direct/dnstxt.py``: the system's resolvers, EDNS0, TCP on a
truncated answer). A CERT record of type PKIX (1) holds a DER certificate;
IPKIX (4) holds a URL, read over https only (``fetch``).

LDAP (RFC 4511), as the Direct reference implementation searches: the
directory is found by the SRV record ``_ldap._tcp.<domain>``; Faxbot binds
anonymously, reads the base names from the root entry's ``namingContexts``,
and searches each for ``(mail=<address>)`` (then the domain) returning
``userCertificate;binary`` and ``userCertificate``. Only that one search is
sent; replies are read with the same DER reader as signatures.

Every function raises ``LookupError`` when its service cannot be asked, so
``certificates.discover`` records it and tries the next source.
"""
import secrets
import socket
import ssl
import struct

from ..direct import dnstxt
from . import der


TYPE_CERT = 37
TYPE_SRV = 33
CERT_PKIX = 1
CERT_IPKIX = 4
MAX_CERTIFICATE = 64 * 1024


def _question(name, kind, identity):
    header = struct.pack('!HHHHHH', identity, 0x0100, 1, 0, 0, 1)
    question = dnstxt.encode_name(name) + struct.pack('!HH', kind, dnstxt.CLASS_IN)
    opt = b'\x00' + struct.pack('!HHIH', dnstxt.TYPE_OPT, dnstxt.EDNS_SIZE, 0, 0)
    return header + question + opt


def _answers(data, identity, name, kind):
    """(truncated, [rdata]) of records of ``kind`` in one answer; NXDOMAIN is none."""
    if len(data) < 12:
        raise dnstxt.DnsError('The answer is cut short.')
    answer_id, flags, questions, answers, _, _ = struct.unpack('!HHHHHH', data[:12])
    if answer_id != identity or not flags & 0x8000 or questions != 1:
        raise dnstxt.DnsError('The answer is not for this question.')
    asked, offset = dnstxt._read_name(data, 12)
    offset += 4
    if asked != name.rstrip('.').lower():
        raise dnstxt.DnsError('The answer is not for this question.')
    if flags & 0x0200:
        return True, []
    code = flags & 0x000F
    if code == 3:
        return False, []
    if code != 0:
        raise dnstxt.DnsError('The name server could not answer.')
    found = []
    for _ in range(answers):
        _, offset = dnstxt._read_name(data, offset)
        if offset + 10 > len(data):
            raise dnstxt.DnsError('The answer is cut short.')
        rtype, rclass, _, length = struct.unpack('!HHIH', data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + length]
        if len(rdata) != length:
            raise dnstxt.DnsError('The answer is cut short.')
        if rtype == kind and rclass == dnstxt.CLASS_IN:
            found.append((rdata, offset, data))
        offset += length
    return False, found


def records(name, kind, *, servers=None, timeout=dnstxt.TIMEOUT, udp=dnstxt._udp, tcp=dnstxt._tcp):
    """The rdata of every ``kind`` record at ``name`` (with its place in the answer). Raises LookupError."""
    servers = servers if servers is not None else dnstxt.nameservers()
    if not servers:
        raise LookupError('This computer has no DNS resolver set.')
    name = name.rstrip('.').lower()
    last = None
    for server in servers[:3]:
        identity = int.from_bytes(secrets.token_bytes(2), 'big') or 1
        packet = _question(name, kind, identity)
        try:
            truncated, found = _answers(udp(server, packet, timeout), identity, name, kind)
            if truncated:
                truncated, found = _answers(tcp(server, packet, timeout), identity, name, kind)
                if truncated:
                    raise dnstxt.DnsError('The answer is too large.')
            return found
        except (OSError, dnstxt.DnsError) as error:
            last = error
    raise LookupError(str(last) if isinstance(last, dnstxt.DnsError) else 'DNS could not be reached.')


def cert_records(name, **options):
    """[(certificate type, data)] of the CERT records at ``name`` (RFC 4398 2.1)."""
    found = []
    for rdata, _, _ in records(name, TYPE_CERT, **options):
        if len(rdata) < 5:
            continue
        kind, _, _ = struct.unpack('!HHB', rdata[:5])
        found.append((kind, rdata[5:]))
    return found


def srv_records(name, **options):
    """[(priority, weight, port, target)] of the SRV records at ``name``, lowest priority first."""
    found = []
    for rdata, offset, data in records(name, TYPE_SRV, **options):
        if len(rdata) < 7:
            continue
        priority, weight, port = struct.unpack('!HHH', rdata[:6])
        target, _ = dnstxt._read_name(data, offset + 6)
        if target:
            found.append((priority, -weight, port, target))
    return [(priority, -weight, port, target) for priority, weight, port, target in sorted(found)]


def dns_certificates(name, *, fetch=None, **options):
    """DER certificates published at ``name``: PKIX records, and IPKIX links read with ``fetch``."""
    found = []
    for kind, data in cert_records(name, **options):
        if kind == CERT_PKIX and data:
            found.append(data)
        elif kind == CERT_IPKIX and fetch is not None:
            url = data.decode('ascii', 'replace').strip()
            if url.startswith('https://'):
                try:
                    blob = fetch(url)
                except LookupError:
                    continue
                if 0 < len(blob) <= MAX_CERTIFICATE:
                    found.append(blob)
    return found


# LDAP ------------------------------------------------------------------------------------------------------------

def _message(message_id, operation):
    return der.sequence(der.encode_integer(message_id), operation)


def _bind():
    # BindRequest [APPLICATION 0]: version 3, empty name, simple authentication with an empty password (anonymous).
    return der.tlv(0x60, der.encode_integer(3) + der.encode_octets(b'') + der.tlv(0x80, b''))


def _filter_equal(attribute, value):
    # equalityMatch [3] AttributeValueAssertion
    return der.tlv(0xA3, der.encode_octets(attribute) + der.encode_octets(value))


def _search(base, scope, filter_bytes, attributes, *, size_limit=10, time_limit=4):
    # SearchRequest [APPLICATION 3]
    return der.tlv(0x63, der.encode_octets(base) + der.tlv(0x0A, bytes([scope])) + der.tlv(0x0A, b'\x00')
                   + der.encode_integer(size_limit) + der.encode_integer(time_limit) + der.tlv(0x01, b'\x00')
                   + filter_bytes + der.sequence(*(der.encode_octets(name) for name in attributes)))


def _present(attribute):
    return der.tlv(0x87, attribute.encode('ascii'))


class _Connection:
    def __init__(self, sock):
        self.sock, self.buffer, self.next_id = sock, b'', 1

    def send(self, operation):
        message_id = self.next_id
        self.next_id += 1
        self.sock.sendall(_message(message_id, operation))
        return message_id

    def receive(self):
        while True:
            try:
                node, end = der.read(self.buffer)
                self.buffer = self.buffer[end:]
                return node
            except der.DerError:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise LookupError('The directory closed the connection.')
                self.buffer += chunk
                if len(self.buffer) > 4 * 1024 * 1024:
                    raise LookupError('The directory answered too much.')

    def results(self, message_id):
        """[(dn, {attribute: [values]})] until SearchResultDone."""
        entries = []
        while True:
            message = self.receive()
            parts = message.children()
            if der.integer(parts[0]) != message_id:
                continue
            operation = parts[1]
            if operation.tag == 0x64:  # SearchResultEntry
                name, attributes = operation.children()[:2]
                found = {}
                for attribute in attributes.children():
                    kind, values = attribute.children()[:2]
                    found[kind.value.decode('utf-8', 'replace').lower()] = [value.value for value in values.children()]
                entries.append((name.value.decode('utf-8', 'replace'), found))
            elif operation.tag == 0x65:  # SearchResultDone
                code = der.integer(operation.children()[0])
                if code not in (0, 4):  # success, or the size limit was reached
                    raise LookupError('The directory refused the search.')
                return entries
            elif operation.tag == 0x61:  # BindResponse
                if der.integer(operation.children()[0]) != 0:
                    raise LookupError('The directory refused an anonymous sign-in.')
                return entries


def ldap_certificates(address, *, servers=None, connect=None, timeout=5.0, srv=None):
    """DER certificates an LDAP directory for the address's domain publishes for it (or its domain)."""
    domain = address.rpartition('@')[2]
    targets = (srv or (lambda name: srv_records(name, servers=servers)))(f'_ldap._tcp.{domain}')
    if not targets:
        raise LookupError('The domain publishes no directory.')
    last = None
    for _, _, port, host in targets[:3]:
        try:
            sock = (connect or socket.create_connection)((host, port), timeout)
        except OSError as error:
            last = error
            continue
        try:
            sock.settimeout(timeout)
            return _search_certificates(_Connection(sock), address, domain)
        except (OSError, der.DerError, IndexError) as error:
            last = error
        finally:
            sock.close()
    raise LookupError('The directory could not be asked.' if last is not None else 'No directory answered.')


def _search_certificates(connection, address, domain):
    connection.results(connection.send(_bind()))
    root = connection.results(connection.send(_search('', 0, _present('objectClass'), ['namingContexts'])))
    bases = [value.decode('utf-8', 'replace') for _, attributes in root
             for value in attributes.get('namingcontexts', ())] or ['']
    for subject in (address, domain):
        for base in bases[:5]:
            entries = connection.results(connection.send(_search(
                base, 2, _filter_equal('mail', subject), ['userCertificate;binary', 'userCertificate',
                                                           'userSMIMECertificate'])))
            found = [value for _, attributes in entries
                     for name in ('usercertificate;binary', 'usercertificate')
                     for value in attributes.get(name, ()) if 0 < len(value) <= MAX_CERTIFICATE]
            if found:
                return found
    return []


def https_fetch(url, *, timeout=10.0, limit=4 * 1024 * 1024):
    """Bytes at an https address (an IPKIX certificate or a trust bundle). Raises LookupError."""
    import httpx
    if not url.startswith('https://'):
        raise LookupError('Only https addresses are read.')
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, verify=ssl.create_default_context()) as client:
            response = client.get(url)
    except httpx.HTTPError:
        raise LookupError('The address could not be read.') from None
    if response.status_code != 200 or len(response.content) > limit:
        raise LookupError('The address did not return a usable file.')
    return response.content


def http_fetch(url, *, timeout=10.0, limit=8 * 1024 * 1024):
    """A certificate revocation list (CRL addresses are usually plain http). Raises LookupError."""
    import httpx
    if not url.startswith(('http://', 'https://')):
        raise LookupError('Only web addresses are read.')
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.get(url)
    except httpx.HTTPError:
        raise LookupError('The revocation list could not be read.') from None
    if response.status_code != 200 or len(response.content) > limit:
        raise LookupError('The revocation list could not be read.')
    return response.content
