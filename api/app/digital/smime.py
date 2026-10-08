"""S/MIME for Direct messages: sign, encrypt, decrypt, and check a signature (RFC 5751, RFC 5652).

Faxbot uses ``cryptography`` (already pinned) for signing (``PKCS7SignatureBuilder``),
encryption (``PKCS7EnvelopeBuilder``, AES-256-CBC) and decryption
(``pkcs7_decrypt_der``). ``cryptography`` cannot verify a CMS signature, so
``verify_detached`` reads the SignedData itself (``der.py``) and checks:

- the signer named by its issuer and serial number (or subject key identifier)
  is one of the certificates the signature carries;
- the content's digest equals the signed ``messageDigest`` attribute, and the
  ``contentType`` attribute says data;
- the signature over the DER signed attributes (tag set to SET, RFC 5652 5.4)
  verifies with the signer's public key (RSA PKCS #1 v1.5 or ECDSA);
- the digest is SHA-256 or stronger. SHA-1 is refused, as DirectTrust's
  certificate policy requires SHA-256 signatures.

Whether the signer's certificate is trusted, and for which address, is
``certificates.py``'s question. Everything here works on canonical MIME bytes
(CRLF line ends); a signed part is checked exactly as received.
"""
import base64
import hashlib
import re
import secrets

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.ciphers import algorithms
from cryptography.hazmat.primitives.serialization import pkcs7

from . import der


class SmimeError(ValueError):
    """The message's security could not be read or does not hold; one plain sentence."""


DATA = '1.2.840.113549.1.7.1'
SIGNED_DATA = '1.2.840.113549.1.7.2'
ENVELOPED_DATA = '1.2.840.113549.1.7.3'
CONTENT_TYPE = '1.2.840.113549.1.9.3'
MESSAGE_DIGEST = '1.2.840.113549.1.9.4'
SIGNING_TIME = '1.2.840.113549.1.9.5'
DIGESTS = {
    '2.16.840.1.101.3.4.2.1': hashes.SHA256, '2.16.840.1.101.3.4.2.2': hashes.SHA384,
    '2.16.840.1.101.3.4.2.3': hashes.SHA512,
}
SHA1 = '1.3.14.3.2.26'
RSA_SIGNATURES = {'1.2.840.113549.1.1.1', '1.2.840.113549.1.1.11', '1.2.840.113549.1.1.12', '1.2.840.113549.1.1.13'}
EC_SIGNATURES = {'1.2.840.10045.4.3.2', '1.2.840.10045.4.3.3', '1.2.840.10045.4.3.4', '1.2.840.10045.2.1'}
MICALG = {hashes.SHA256: 'sha-256', hashes.SHA384: 'sha-384', hashes.SHA512: 'sha-512'}
CRLF = b'\r\n'


def canonical(data):
    """Bytes with every line ending as CRLF (RFC 5751 3.1.1)."""
    return re.sub(rb'\r?\n', CRLF, re.sub(rb'\r(?!\n)', b'\n', data))


def _base64_lines(data):
    text = base64.b64encode(data)
    return CRLF.join(text[index:index + 76] for index in range(0, len(text), 76))


def boundary(kind='Part'):
    return f'----=_Faxbot{kind}_{secrets.token_hex(12)}'


# Signing and encryption -------------------------------------------------------------------------------------------

def sign_detached(content, certificate, key, chain=()):
    """The DER CMS SignedData of a detached SHA-256 signature over ``content`` (exact bytes)."""
    builder = pkcs7.PKCS7SignatureBuilder().set_data(content).add_signer(certificate, key, hashes.SHA256())
    for extra in chain:
        builder = builder.add_certificate(extra)
    return builder.sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.DetachedSignature,
                                                      pkcs7.PKCS7Options.Binary])


def signed_entity(entity, certificate, key, chain=()):
    """A multipart/signed entity carrying ``entity`` (canonical MIME bytes) and its signature."""
    entity = canonical(entity)
    signature = sign_detached(entity, certificate, key, chain)
    mark = boundary('Signed')
    head = (f'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; micalg=sha-256; '
            f'boundary="{mark}"').encode('ascii')
    body = CRLF.join([
        head, b'', b'--' + mark.encode('ascii'), entity, b'--' + mark.encode('ascii'),
        b'Content-Type: application/pkcs7-signature; name="smime.p7s"',
        b'Content-Transfer-Encoding: base64',
        b'Content-Disposition: attachment; filename="smime.p7s"', b'',
        _base64_lines(signature), b'--' + mark.encode('ascii') + b'--', b''])
    return body


def encrypt(entity, recipients, *, algorithm=algorithms.AES256):
    """The DER CMS EnvelopedData of ``entity`` for each recipient certificate (RSA key transport)."""
    if not recipients:
        raise SmimeError('There is no certificate to encrypt the message for.')
    builder = pkcs7.PKCS7EnvelopeBuilder().set_data(canonical(entity)).set_content_encryption_algorithm(algorithm)
    for certificate in recipients:
        builder = builder.add_recipient(certificate)
    return builder.encrypt(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])


def enveloped_entity(enveloped):
    return CRLF.join([
        b'Content-Type: application/pkcs7-mime; smime-type=enveloped-data; name="smime.p7m"',
        b'Content-Transfer-Encoding: base64',
        b'Content-Disposition: attachment; filename="smime.p7m"', b'',
        _base64_lines(enveloped), b''])


def decrypt(enveloped, certificate, key):
    """The decrypted content of DER EnvelopedData addressed to ``certificate``. Raises SmimeError."""
    try:
        return pkcs7.pkcs7_decrypt_der(enveloped, certificate, key, [])
    except (ValueError, TypeError) as error:
        raise SmimeError('The message was not encrypted for this organization\'s certificate.') from error


# Reading signed entities ------------------------------------------------------------------------------------------

def split_headers(entity):
    """(headers {lower name: value}, body bytes) of a canonical MIME entity."""
    entity = canonical(entity)
    head, separator, body = entity.partition(CRLF + CRLF)
    if not separator:
        if entity.startswith(CRLF):
            return {}, entity[2:]
        raise SmimeError('The message has no body.')
    headers = {}
    current = None
    for line in head.split(CRLF):
        if line[:1] in (b' ', b'\t') and current is not None:
            headers[current] += ' ' + line.strip().decode('latin-1')
            continue
        name, colon, value = line.partition(b':')
        if not colon:
            continue
        current = name.strip().lower().decode('latin-1')
        headers[current] = value.strip().decode('latin-1')
    return headers, body


def parameter(header, name):
    """A parameter of a structured header (``boundary``, ``smime-type``), unquoted; None when missing."""
    found = re.search(r';\s*' + re.escape(name) + r'\s*=\s*("([^"]*)"|[^;\s]+)', header or '', re.IGNORECASE)
    if found is None:
        return None
    return found.group(2) if found.group(2) is not None else found.group(1)


def media_type(header):
    return (header or 'text/plain').split(';', 1)[0].strip().lower()


def split_multipart(body, mark):
    """The exact bytes of each part between boundaries (RFC 2046 5.1.1: the CRLF before a delimiter is its own)."""
    delimiter = b'--' + mark.encode('latin-1')
    parts = []
    text = CRLF + body
    pieces = text.split(CRLF + delimiter)
    if len(pieces) < 3:
        raise SmimeError('The message parts could not be read.')
    for piece in pieces[1:]:
        if piece.startswith(b'--'):
            break
        line_end = piece.find(CRLF)
        if line_end < 0:
            raise SmimeError('The message parts could not be read.')
        if piece[:line_end].strip():
            raise SmimeError('The message parts could not be read.')
        parts.append(piece[line_end + 2:])
    else:
        raise SmimeError('The message parts are not closed.')
    return parts


def transfer_decode(headers, body):
    encoding = (headers.get('content-transfer-encoding') or '7bit').strip().lower()
    if encoding == 'base64':
        try:
            return base64.b64decode(re.sub(rb'\s+', b'', body), validate=True)
        except ValueError:
            raise SmimeError('A part of the message is not valid base64.') from None
    if encoding == 'quoted-printable':
        import quopri
        return quopri.decodestring(body)
    return body


def unwrap_signed(entity):
    """(signed content bytes, DER signature) from a multipart/signed or opaque signed-data entity."""
    headers, body = split_headers(entity)
    kind = media_type(headers.get('content-type'))
    if kind == 'multipart/signed':
        mark = parameter(headers['content-type'], 'boundary')
        if not mark:
            raise SmimeError('The signed message has no boundary.')
        parts = split_multipart(body, mark)
        if len(parts) != 2:
            raise SmimeError('A signed message has exactly two parts.')
        signature_headers, signature_body = split_headers(parts[1])
        if media_type(signature_headers.get('content-type')) not in ('application/pkcs7-signature',
                                                                     'application/x-pkcs7-signature'):
            raise SmimeError('The signature part is not an S/MIME signature.')
        return parts[0], transfer_decode(signature_headers, signature_body), False
    if kind in ('application/pkcs7-mime', 'application/x-pkcs7-mime') and \
            (parameter(headers.get('content-type'), 'smime-type') or '').lower() == 'signed-data':
        signed = transfer_decode(headers, body)
        return None, signed, True
    raise SmimeError('The message is not signed.')


# Verifying -------------------------------------------------------------------------------------------------------

class Signer:
    def __init__(self, certificate, certificates, digest, signing_time):
        self.certificate = certificate
        self.certificates = certificates  # every certificate the signature carried (for the chain)
        self.digest = digest
        self.signing_time = signing_time


def _algorithm(node):
    parts = node.children()
    return der.oid(parts[0])


def _signed_data(signature):
    info = der.parse(signature)
    parts = info.children()
    if der.oid(parts[0]) != SIGNED_DATA or len(parts) < 2 or parts[1].tag != 0xA0:
        raise SmimeError('The signature is not CMS signed data.')
    return parts[1].children()[0]


def _certificates(node):
    found = []
    for child in node.children():
        if child.tag == 0x30:
            try:
                found.append(x509.load_der_x509_certificate(child.raw))
            except ValueError:
                continue
    return found


def _matches(certificate, sid):
    if sid.tag == 0x30:
        issuer, serial = sid.children()[:2]
        return (certificate.issuer.public_bytes() == issuer.raw
                and certificate.serial_number == der.integer(serial))
    if sid.tag == 0x80:
        try:
            ski = certificate.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value.digest
        except x509.ExtensionNotFound:
            return False
        return ski == sid.value
    return False


def verify_detached(content, signature, *, opaque=False):
    """The ``Signer`` of a CMS signature over ``content``; raises SmimeError when it does not verify.

    ``opaque``: the signed data carries its own content (smime-type=signed-data); the content is returned as
    ``Signer.content``.
    """
    try:
        signed = _signed_data(signature)
        parts = signed.children()
        encapsulated = parts[2]
        embedded = None
        encap_parts = encapsulated.children()
        if len(encap_parts) > 1 and encap_parts[1].tag == 0xA0:
            embedded = der.octets(encap_parts[1].children()[0])
        if opaque:
            if embedded is None:
                raise SmimeError('The signed message carries no content.')
            content = embedded
        elif content is None:
            raise SmimeError('The signed content is missing.')
        certificates, signer_infos = [], None
        for node in parts[3:]:
            if node.tag == 0xA0:
                certificates = _certificates(node)
            elif node.tag == 0x31:
                signer_infos = node.children()
        if not signer_infos:
            raise SmimeError('The signature names no signer.')
        info = signer_infos[0].children()
        sid, digest_algorithm = info[1], _algorithm(info[2])
        index = 3
        attributes = None
        if info[index].tag == 0xA0:
            attributes = info[index]
            index += 1
        signature_algorithm = _algorithm(info[index])
        signature_value = der.octets(info[index + 1])
    except (der.DerError, IndexError) as error:
        raise SmimeError('The signature could not be read.') from error
    if digest_algorithm == SHA1:
        raise SmimeError('The message is signed with SHA-1, which Direct no longer accepts.')
    hash_type = DIGESTS.get(digest_algorithm)
    if hash_type is None:
        raise SmimeError('The message is signed with a digest Faxbot does not accept.')
    signer = next((certificate for certificate in certificates if _matches(certificate, sid)), None)
    if signer is None:
        raise SmimeError('The signature does not carry the signer\'s certificate.')
    digest = hashlib.new(hash_type.name, content).digest()
    signing_time = None
    if attributes is not None:
        found = {}
        try:
            for attribute in attributes.children():
                kind, values = attribute.children()[:2]
                found[der.oid(kind)] = values.children()[0]
        except (der.DerError, IndexError) as error:
            raise SmimeError('The signed attributes could not be read.') from error
        if MESSAGE_DIGEST not in found or der.octets(found[MESSAGE_DIGEST]) != digest:
            raise SmimeError('The message was changed after it was signed.')
        if CONTENT_TYPE in found and der.oid(found[CONTENT_TYPE]) != DATA:
            raise SmimeError('The signature is not over message data.')
        if SIGNING_TIME in found:
            signing_time = _time(found[SIGNING_TIME])
        covered = b'\x31' + attributes.raw[1:]
    else:
        covered = content
    key = signer.public_key()
    try:
        if isinstance(key, rsa.RSAPublicKey) and signature_algorithm in RSA_SIGNATURES:
            key.verify(signature_value, covered, padding.PKCS1v15(), hash_type())
        elif isinstance(key, ec.EllipticCurvePublicKey) and signature_algorithm in EC_SIGNATURES:
            key.verify(signature_value, covered, ec.ECDSA(hash_type()))
        else:
            raise SmimeError('The message is signed with a method Faxbot does not accept.')
    except InvalidSignature:
        raise SmimeError('The signature does not match the message.') from None
    result = Signer(signer, certificates, hash_type.name, signing_time)
    result.content = content
    return result


def _time(node):
    from datetime import datetime
    text = node.value.decode('ascii', 'replace')
    try:
        if node.tag == 0x17:  # UTCTime
            return datetime.strptime(text, '%y%m%d%H%M%SZ')
        if node.tag == 0x18:  # GeneralizedTime
            return datetime.strptime(text.split('.')[0].rstrip('Z'), '%Y%m%d%H%M%S')
    except ValueError:
        return None
    return None
