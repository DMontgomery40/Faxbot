"""A small ASN.1 BER/DER reader and writer for the pieces the digital routes parse themselves.

``cryptography`` signs, encrypts and decrypts S/MIME but cannot verify a CMS
signature, and Python's standard library has no LDAP client. Both need only
tag-length-value walking: CMS SignedData (RFC 5652) and LDAPv3 messages
(RFC 4511). Lengths may be definite or, for constructed values, indefinite
(BER, as some S/MIME producers stream them); everything a signature covers is
read back as the exact bytes received.
"""


class DerError(ValueError):
    """The bytes are not the ASN.1 structure expected."""


class Node:
    """One TLV: ``tag`` (the identifier octet), the whole encoding ``raw`` and the value bytes ``value``."""

    __slots__ = ('tag', 'raw', 'value', 'constructed')

    def __init__(self, tag, raw, value, constructed):
        self.tag, self.raw, self.value, self.constructed = tag, raw, value, constructed

    @property
    def number(self):
        return self.tag & 0x1F

    @property
    def tag_class(self):
        return self.tag & 0xC0

    def children(self):
        if not self.constructed:
            raise DerError('A primitive value has no parts.')
        return list(iterate(self.value))

    def __repr__(self):
        return f'Node(tag=0x{self.tag:02x}, length={len(self.value)})'


MAX_DEPTH = 64


def _length(data, offset):
    """(length or None for indefinite, offset after the length octets)."""
    if offset >= len(data):
        raise DerError('The value is cut short.')
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    if first == 0x80:
        return None, offset
    count = first & 0x7F
    if count > 4 or offset + count > len(data):
        raise DerError('The length is not usable.')
    return int.from_bytes(data[offset:offset + count], 'big'), offset + count


def read(data, offset=0, depth=0):
    """(Node, offset after it) for the TLV at ``offset``."""
    if depth > MAX_DEPTH:
        raise DerError('The value is nested too deeply.')
    start = offset
    if offset >= len(data):
        raise DerError('The value is cut short.')
    tag = data[offset]
    if tag & 0x1F == 0x1F:
        raise DerError('High tag numbers are not used here.')
    constructed = bool(tag & 0x20)
    length, offset = _length(data, offset + 1)
    if length is None:
        if not constructed:
            raise DerError('A primitive value cannot have an indefinite length.')
        begin = offset
        while True:
            if offset + 2 > len(data):
                raise DerError('The value is cut short.')
            if data[offset] == 0 and data[offset + 1] == 0:
                value = data[begin:offset]
                offset += 2
                break
            _, offset = read(data, offset, depth + 1)
        return Node(tag, bytes(data[start:offset]), bytes(value), True), offset
    end = offset + length
    if end > len(data):
        raise DerError('The value is cut short.')
    return Node(tag, bytes(data[start:end]), bytes(data[offset:end]), constructed), end


def parse(data):
    """The single TLV that is the whole of ``data``."""
    node, end = read(data, 0)
    if end != len(data):
        raise DerError('There is data after the value.')
    return node


def iterate(data):
    offset = 0
    while offset < len(data):
        node, offset = read(data, offset)
        yield node


def octets(node):
    """An OCTET STRING's bytes, joining a BER constructed string's pieces."""
    if node.tag == 0x04:
        return node.value
    if node.tag == 0x24:
        return b''.join(octets(child) for child in node.children())
    raise DerError('An octet string was expected.')


def oid(node):
    if node.tag != 0x06 or not node.value:
        raise DerError('An object identifier was expected.')
    value = node.value
    first = value[0]
    parts = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    number = 0
    for byte in value[1:]:
        number = (number << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(number)
            number = 0
    return '.'.join(str(part) for part in parts)


def integer(node):
    if node.tag not in (0x02, 0x0A) or not node.value:
        raise DerError('An integer was expected.')
    return int.from_bytes(node.value, 'big', signed=True)


# Writing (DER, definite lengths) ----------------------------------------------------------------------------------

def encode_length(length):
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, 'big')
    return bytes([0x80 | len(body)]) + body


def tlv(tag, value):
    return bytes([tag]) + encode_length(len(value)) + value


def encode_integer(number):
    length = max(1, (number.bit_length() + 8) // 8)
    return tlv(0x02, number.to_bytes(length, 'big', signed=True))


def encode_octets(value, tag=0x04):
    return tlv(tag, value if isinstance(value, bytes) else value.encode('utf-8'))


def sequence(*parts, tag=0x30):
    return tlv(tag, b''.join(parts))
