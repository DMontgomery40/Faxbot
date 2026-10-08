"""A small DNS TXT client for trusted partner directories (D13), on the standard library only.

It asks the resolvers in ``/etc/resolv.conf`` one question (TXT, class IN,
recursion desired, EDNS0 with a 1232-byte answer size), checks that the answer
carries the question's ID and the same question, and falls back to TCP when
the answer is truncated (RFC 1035 4.2.2, RFC 7766). Each TXT record's
character-strings are joined into one value (a record longer than 255 bytes
is several strings, RFC 1035 3.3.14). Nothing else is followed or cached here;
``discovery.py`` checks each record's signature before anything is shown.
"""
import ipaddress
import secrets
import socket
import struct


TYPE_TXT = 16
TYPE_OPT = 41
CLASS_IN = 1
EDNS_SIZE = 1232
TIMEOUT = 3.0


class DnsError(RuntimeError):
    """The directory could not be asked, or its answer was not usable."""


def encode_name(name):
    labels = [label for label in name.rstrip('.').split('.')]
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise DnsError('Not a DNS name.')
    try:
        encoded = b''.join(bytes([len(label)]) + label.encode('ascii') for label in labels) + b'\x00'
    except UnicodeEncodeError:
        raise DnsError('Not a DNS name.') from None
    if len(encoded) > 255:
        raise DnsError('Not a DNS name.')
    return encoded


def query(name, identity):
    """One TXT question for ``name`` with an EDNS0 OPT record."""
    header = struct.pack('!HHHHHH', identity, 0x0100, 1, 0, 0, 1)
    question = encode_name(name) + struct.pack('!HH', TYPE_TXT, CLASS_IN)
    opt = b'\x00' + struct.pack('!HHIH', TYPE_OPT, EDNS_SIZE, 0, 0)
    return header + question + opt


def _read_name(data, offset):
    """(name in lower case, offset after it), following compression pointers (RFC 1035 4.1.4)."""
    labels, jumps, end = [], 0, None
    while True:
        if offset >= len(data):
            raise DnsError('The answer is cut short.')
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data) or jumps > 32:
                raise DnsError('The answer is not valid.')
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            jumps += 1
            continue
        if length & 0xC0:
            raise DnsError('The answer is not valid.')
        offset += 1
        if length == 0:
            break
        labels.append(data[offset:offset + length].decode('ascii', 'replace').lower())
        offset += length
    return '.'.join(labels), (end if end is not None else offset)


def parse(data, identity, name):
    """(truncated, records) from one answer; records are the TXT values, strings joined.

    An answer that is not for this question is refused. NXDOMAIN and an empty
    answer are no records.
    """
    if len(data) < 12:
        raise DnsError('The answer is cut short.')
    answer_id, flags, questions, answers, _, _ = struct.unpack('!HHHHHH', data[:12])
    if answer_id != identity or not flags & 0x8000:
        raise DnsError('The answer is not for this question.')
    truncated = bool(flags & 0x0200)
    code = flags & 0x000F
    if questions != 1:
        raise DnsError('The answer is not for this question.')
    asked, offset = _read_name(data, 12)
    if offset + 4 > len(data):
        raise DnsError('The answer is cut short.')
    kind, klass = struct.unpack('!HH', data[offset:offset + 4])
    offset += 4
    if asked != name.rstrip('.').lower() or kind != TYPE_TXT or klass != CLASS_IN:
        raise DnsError('The answer is not for this question.')
    if truncated:
        return True, []
    if code == 3:
        return False, []
    if code != 0:
        raise DnsError('The directory could not answer.')
    records = []
    for _ in range(answers):
        _, offset = _read_name(data, offset)
        if offset + 10 > len(data):
            raise DnsError('The answer is cut short.')
        kind, klass, _, length = struct.unpack('!HHIH', data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + length]
        offset += length
        if len(rdata) != length:
            raise DnsError('The answer is cut short.')
        if kind != TYPE_TXT or klass != CLASS_IN:
            continue  # A CNAME on the way, say.
        parts, at = [], 0
        while at < len(rdata):
            size = rdata[at]
            parts.append(rdata[at + 1:at + 1 + size])
            at += 1 + size
        if at != len(rdata):
            raise DnsError('The answer is not valid.')
        try:
            records.append(b''.join(parts).decode('ascii'))
        except UnicodeDecodeError:
            continue
    return False, records


def nameservers(path='/etc/resolv.conf'):
    found = []
    try:
        with open(path, encoding='ascii', errors='ignore') as handle:
            for line in handle:
                fields = line.split()
                if len(fields) >= 2 and fields[0] == 'nameserver':
                    try:
                        found.append(str(ipaddress.ip_address(fields[1].split('%')[0])))
                    except ValueError:
                        continue
    except OSError:
        pass
    return found


def _udp(server, packet, timeout):
    family = socket.AF_INET6 if ':' in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, 53))
        sock.send(packet)
        return sock.recv(65535)


def _tcp(server, packet, timeout):
    family = socket.AF_INET6 if ':' in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, 53))
        sock.sendall(struct.pack('!H', len(packet)) + packet)
        size = struct.unpack('!H', _exactly(sock, 2))[0]
        return _exactly(sock, size)


def _exactly(sock, size):
    data = b''
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise DnsError('The answer is cut short.')
        data += chunk
    return data


def txt_records(name, *, servers=None, timeout=TIMEOUT, udp=_udp, tcp=_tcp):
    """Every TXT value at ``name`` (strings joined); [] when there is none. Raises DnsError."""
    servers = servers if servers is not None else nameservers()
    if not servers:
        raise DnsError('This computer has no DNS resolver set.')
    name = name.rstrip('.').lower()
    last = None
    for server in servers[:3]:
        identity = int.from_bytes(secrets.token_bytes(2), 'big') or 1
        packet = query(name, identity)
        try:
            truncated, records = parse(udp(server, packet, timeout), identity, name)
            if truncated:
                truncated, records = parse(tcp(server, packet, timeout), identity, name)
                if truncated:
                    raise DnsError('The answer is too large.')
            return records
        except (OSError, DnsError) as error:
            last = error
            continue
    raise DnsError(str(last) if isinstance(last, DnsError) else 'The directory could not be reached.')

