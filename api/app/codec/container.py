"""The payload container: what a set of payload pages carries, before error correction.

Layout (all integers big-endian)::

    clear header (12 bytes, or 40 when encrypted)
      0  4  magic b'FXBC'
      4  1  format version (1)
      5  1  flags: bit 0 = encrypted (AES-256-GCM)
      6  1  compression: 0 none, 1 deflate (zlib), 2 zstd
      7  1  reserved, 0
      8  4  body length in bytes
      -- encrypted only --
     12 16  PBKDF2 salt
     28 12  AES-GCM nonce
    body
      plain:     the inner record
      encrypted: AES-256-GCM(key, nonce, inner record, associated data = clear header)
                 followed by its 16-byte tag (inside the body length)

    inner record
      0 32  SHA-256 of the original document
     32  1  content type: 1 PDF, 2 UTF-8 text
     33  4  original length in bytes
     37  1  name length n (0..120), then n bytes of UTF-8 file name
      .  .  the compressed document

The document's hash lives inside the encrypted body, so an encrypted payload
does not let anyone confirm a guess at its contents. The key is derived from
the secret the partners shared out of band with PBKDF2-HMAC-SHA256
(200,000 iterations, the salt above), so a long random key and a passphrase
both work. Decoding refuses anything above ``MAX_DOCUMENT_BYTES`` before it
allocates or decompresses.
"""
from dataclasses import dataclass
import hashlib
import struct
import zlib

MAGIC = b'FXBC'
VERSION = 1
FLAG_ENCRYPTED = 1
NONE, DEFLATE, ZSTD = 0, 1, 2
CONTENT_TYPES = {1: 'application/pdf', 2: 'text/plain'}
CONTENT_IDS = {value: key for key, value in CONTENT_TYPES.items()}
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_NAME_BYTES = 120
PBKDF2_ITERATIONS = 200_000
CLEAR_HEADER = struct.Struct('>4sBBBBI')
INNER_HEADER = struct.Struct('>32sBIB')


class ContainerError(ValueError):
    """The payload is not a usable Faxbot container (one sentence, safe to show)."""


def zstd_available():
    try:
        import zstandard  # noqa: F401
    except ImportError:
        return False
    return True


def _compress(data, method):
    if method == NONE:
        return data
    if method == DEFLATE:
        return zlib.compress(data, 9)
    if method == ZSTD:
        import zstandard
        return zstandard.ZstdCompressor(level=19, write_content_size=True, write_checksum=False).compress(data)
    raise ContainerError('This payload uses a compression Faxbot does not know.')


def _decompress(data, method, expected):
    if method == NONE:
        result = bytes(data)
    elif method == DEFLATE:
        decompressor = zlib.decompressobj()
        result = decompressor.decompress(data, MAX_DOCUMENT_BYTES + 1)
        if decompressor.unconsumed_tail:
            raise ContainerError('The encoded document is larger than Faxbot accepts.')
    elif method == ZSTD:
        try:
            import zstandard
        except ImportError:
            raise ContainerError('This Faxbot cannot unpack zstd payloads; install the zstandard package.') from None
        try:
            result = zstandard.ZstdDecompressor().decompress(data, max_output_size=MAX_DOCUMENT_BYTES)
        except zstandard.ZstdError:
            raise ContainerError('The encoded document could not be unpacked.') from None
    else:
        raise ContainerError('This payload uses a compression Faxbot does not know.')
    if len(result) != expected:
        raise ContainerError('The encoded document is not the length its header states.')
    return result


def derive_key(secret, salt):
    if not isinstance(secret, str) or len(secret.strip()) < 8:
        raise ContainerError('A shared key has at least 8 characters.')
    return hashlib.pbkdf2_hmac('sha256', secret.strip().encode('utf-8'), salt, PBKDF2_ITERATIONS, 32)


def key_fingerprint(secret):
    """Eight hex characters that identify a shared key without revealing it."""
    digest = hashlib.sha256(b'faxbot-codec-key\x00' + secret.strip().encode('utf-8')).hexdigest()
    return digest[:8]


@dataclass(frozen=True)
class Document:
    data: bytes
    content_type: str
    name: str = ''

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


def pack(document, *, compression=None, secret=None, salt=None, nonce=None):
    """The container bytes for a document.

    ``compression`` defaults to the smallest of zstd (when installed) and
    deflate. ``salt`` and ``nonce`` are random unless given (tests fix them so
    the output is deterministic; production never reuses a nonce).
    """
    import os
    data = bytes(document.data)
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise ContainerError('The document is empty or larger than Faxbot accepts.')
    content = CONTENT_IDS.get(document.content_type)
    if content is None:
        raise ContainerError('Only PDF and plain-text documents can be encoded.')
    name = (document.name or '').encode('utf-8')[:MAX_NAME_BYTES]
    name = name.decode('utf-8', 'ignore').encode('utf-8')
    if compression is None:
        candidates = [DEFLATE] + ([ZSTD] if zstd_available() else [])
        packed = {method: _compress(data, method) for method in candidates}
        compression = min(candidates, key=lambda method: (len(packed[method]), -method))
        body = packed[compression]
    else:
        body = _compress(data, compression)
    if len(body) >= len(data) and compression != NONE:
        compression, body = NONE, data
    inner = INNER_HEADER.pack(hashlib.sha256(data).digest(), content, len(data), len(name)) + name + body
    flags = 0
    extra = b''
    if secret is not None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt = salt if salt is not None else os.urandom(16)
        nonce = nonce if nonce is not None else os.urandom(12)
        if len(salt) != 16 or len(nonce) != 12:
            raise ContainerError('The salt is 16 bytes and the nonce 12.')
        flags |= FLAG_ENCRYPTED
        extra = salt + nonce
        length = len(inner) + 16
        header = CLEAR_HEADER.pack(MAGIC, VERSION, flags, compression, 0, length) + extra
        inner = AESGCM(derive_key(secret, salt)).encrypt(nonce, inner, header)
    else:
        header = CLEAR_HEADER.pack(MAGIC, VERSION, flags, compression, 0, len(inner))
    return header + inner


def header_length(prefix):
    """Total container length from its first bytes, or None when they are not a container."""
    if len(prefix) < CLEAR_HEADER.size:
        return None
    magic, version, flags, _, _, length = CLEAR_HEADER.unpack_from(prefix)
    if magic != MAGIC or version != VERSION:
        return None
    if length > MAX_DOCUMENT_BYTES + 4096:
        raise ContainerError('The encoded document is larger than Faxbot accepts.')
    return CLEAR_HEADER.size + (28 if flags & FLAG_ENCRYPTED else 0) + length


def is_encrypted(container):
    return len(container) >= CLEAR_HEADER.size and bool(container[5] & FLAG_ENCRYPTED)


def unpack(container, *, secrets=()):
    """The original document, after checking its SHA-256.

    ``secrets`` are the shared keys to try for an encrypted payload, in order.
    """
    total = header_length(container)
    if total is None:
        raise ContainerError('These pages do not carry a Faxbot document.')
    if len(container) < total:
        raise ContainerError('The encoded document is incomplete.')
    magic, version, flags, compression, _, length = CLEAR_HEADER.unpack_from(container)
    offset = CLEAR_HEADER.size
    if flags & FLAG_ENCRYPTED:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, nonce = container[offset:offset + 16], container[offset + 16:offset + 28]
        header = bytes(container[:offset + 28])
        body = bytes(container[offset + 28:offset + 28 + length])
        inner = None
        for secret in secrets:
            try:
                inner = AESGCM(derive_key(secret, salt)).decrypt(nonce, body, header)
                break
            except (InvalidTag, ContainerError):
                continue
        if inner is None:
            if not secrets:
                raise ContainerError('The document is encrypted and no shared key is set for this sender.')
            raise ContainerError('The document is encrypted with a key this Faxbot does not have.')
    else:
        inner = bytes(container[offset:offset + length])
    if len(inner) < INNER_HEADER.size:
        raise ContainerError('The encoded document is incomplete.')
    digest, content, original_length, name_length = INNER_HEADER.unpack_from(inner)
    if content not in CONTENT_TYPES:
        raise ContainerError('The encoded document is not a PDF or a text file.')
    if original_length == 0 or original_length > MAX_DOCUMENT_BYTES or name_length > MAX_NAME_BYTES:
        raise ContainerError('The encoded document is larger than Faxbot accepts.')
    start = INNER_HEADER.size
    name = inner[start:start + name_length].decode('utf-8', 'replace')
    data = _decompress(inner[start + name_length:], compression, original_length)
    if hashlib.sha256(data).digest() != digest:
        raise ContainerError('The decoded document does not match its fingerprint.')
    return Document(data, CONTENT_TYPES[content], name)
