"""Signed manifests, envelope encryption and signed receipts for direct delivery.

Only ``cryptography`` primitives: Ed25519 signatures, X25519 key agreement,
HKDF-SHA256 key derivation and AES-256-GCM. Each document gets a fresh random
content key; that key is wrapped to the recipient's X25519 key with an
ephemeral key pair, and the manifest binds sender, recipient, message id and
the SHA-256 of the original bytes. Nothing here stores keys or contacts peers.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import re

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


SCHEME = 'x25519-hkdf-sha256-aes256gcm'
PROTOCOL = 1
MESSAGE_ID = re.compile(r'[a-f0-9]{32}')
_B64 = re.compile(r'[A-Za-z0-9_-]{43}')
_HEX64 = re.compile(r'[a-f0-9]{64}')
_NUMBER = re.compile(r'\+[1-9][0-9]{7,14}')
_MANIFEST_KEYS = frozenset({'version', 'message_id', 'created_at', 'sender', 'recipient', 'document', 'encryption'})
# A fax image: the exact TIFF a fax engine would send, with the planned fax facts beside it.
FAX_IMAGE = 'fax_image'
FAX_IMAGE_TYPE = 'image/tiff'
FAX_FACTS = frozenset({'resolution', 'x_dpi', 'y_dpi', 'width', 'compression', 'header_line'})
# T.30 page widths at 8 dots per millimetre (A4, B4, A3), vertical resolutions (standard, fine, superfine).
FAX_WIDTHS = (1728, 2048, 2432)
FAX_LINES = {98: 'standard', 196: 'fine', 391: 'superfine'}
FAX_COMPRESSIONS = ('MH', 'MR', 'MMR')


class DirectProtocolError(ValueError):
    """The message is malformed, not for us, or does not verify. ``reason`` is a stable code."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def unb64(text, *, length=None):
    if not isinstance(text, str) or re.fullmatch(r'[A-Za-z0-9_-]+', text) is None:
        raise DirectProtocolError('malformed', 'The message is not in the direct delivery format.')
    data = base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
    if length is not None and len(data) != length:
        raise DirectProtocolError('malformed', 'The message is not in the direct delivery format.')
    return data


def canonical(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode('ascii')


def timestamp(moment=None):
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_timestamp(value):
    if not isinstance(value, str) or re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value) is None:
        raise DirectProtocolError('malformed', 'The message is not in the direct delivery format.')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')


def _fax_facts(facts, pages):
    """Whether ``facts`` are well-formed planned fax facts for a fax image of ``pages`` pages."""
    if not isinstance(facts, dict) or set(facts) != FAX_FACTS or type(pages) is not int or pages < 1:
        return False
    if any(type(facts[name]) is not int for name in ('x_dpi', 'y_dpi', 'width')):
        return False
    return (facts['x_dpi'] == 204 and FAX_LINES.get(facts['y_dpi']) == facts['resolution']
            and facts['width'] in FAX_WIDTHS and facts['compression'] in FAX_COMPRESSIONS
            and isinstance(facts['header_line'], str) and 0 < len(facts['header_line']) <= 200)


def capabilities(*, fax_images, peer_calls, said_at=None):
    """What this installation accepts from one partner, as a signed statement carries it."""
    return {'fax_images': bool(fax_images), 'peer_calls': bool(peer_calls), 'said_at': said_at or timestamp()}


def parse_capabilities(value):
    """A partner's capabilities from a statement it signed: (fax_images, peer_calls, said_at), or None."""
    if (not isinstance(value, dict) or set(value) != {'fax_images', 'peer_calls', 'said_at'}
            or type(value['fax_images']) is not bool or type(value['peer_calls']) is not bool):
        return None
    try:
        return value['fax_images'], value['peer_calls'], parse_timestamp(value['said_at'])
    except DirectProtocolError:
        return None


def kind_of(manifest):
    """``fax_image`` or ``original``."""
    return FAX_IMAGE if manifest.get('kind') == FAX_IMAGE else 'original'


class Identity:
    """This installation's private keys; public halves are shared on its card."""

    def __init__(self, signing_private, exchange_private):
        self.signing = Ed25519PrivateKey.from_private_bytes(signing_private)
        self.exchange = X25519PrivateKey.from_private_bytes(exchange_private)
        raw = serialization.Encoding.Raw, serialization.PublicFormat.Raw
        self.signing_key = b64(self.signing.public_key().public_bytes(*raw))
        self.exchange_key = b64(self.exchange.public_key().public_bytes(*raw))

    @classmethod
    def generate(cls):
        raw = serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        return cls(Ed25519PrivateKey.generate().private_bytes(*raw), X25519PrivateKey.generate().private_bytes(*raw))

    def private_bytes(self):
        raw = serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        return self.signing.private_bytes(*raw), self.exchange.private_bytes(*raw)

    def sign(self, data):
        return b64(self.signing.sign(data))


def verify(signing_key, data, signature):
    try:
        Ed25519PublicKey.from_public_bytes(unb64(signing_key, length=32)).verify(unb64(signature, length=64), data)
    except (InvalidSignature, ValueError):
        raise DirectProtocolError('signature', 'The signature does not match the sender.') from None


def _kek(shared, ephemeral, recipient, message_id):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b'faxbot-direct-v1|' + ephemeral + recipient + message_id.encode('ascii')).derive(shared)


def _aad(manifest):
    return canonical({'message_id': manifest['message_id'], 'sender': manifest['sender']['signing_key'],
                      'recipient': manifest['recipient'], 'sha256': manifest['document']['sha256']})


def seal(identity, *, message_id, organization, fax_number, recipient_number, recipient_signing_key,
         recipient_exchange_key, document, pages=None, created_at=None, fax=None):
    """Encrypt ``document`` to the recipient; returns (manifest bytes, signature, ciphertext).

    ``fax`` makes it a fax image: ``document`` is the TIFF and ``fax`` holds the
    planned fax facts (``faximage.facts``), which the signed manifest carries.
    """
    content_key, nonce, key_nonce = AESGCM.generate_key(bit_length=256), os.urandom(12), os.urandom(12)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    recipient_public = unb64(recipient_exchange_key, length=32)
    shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient_public))
    manifest = {
        'version': PROTOCOL, 'message_id': message_id, 'created_at': created_at or timestamp(),
        'sender': {'organization': organization, 'signing_key': identity.signing_key, 'fax_number': fax_number},
        'recipient': {'fax_number': recipient_number, 'signing_key': recipient_signing_key},
        'document': {'media_type': 'application/pdf' if fax is None else FAX_IMAGE_TYPE,
                     'sha256': hashlib.sha256(document).hexdigest(), 'size': len(document), 'pages': pages},
    }
    if fax is not None:
        manifest.update(kind=FAX_IMAGE, fax=dict(fax))
    ciphertext = AESGCM(content_key).encrypt(nonce, document, _aad(manifest))
    wrapped = AESGCM(_kek(shared, ephemeral_public, recipient_public, message_id)).encrypt(
        key_nonce, content_key, canonical({'message_id': message_id}))
    manifest['encryption'] = {'scheme': SCHEME, 'ephemeral_key': b64(ephemeral_public), 'wrapped_key': b64(wrapped),
                              'key_nonce': b64(key_nonce), 'nonce': b64(nonce),
                              'ciphertext_sha256': hashlib.sha256(ciphertext).hexdigest()}
    encoded = canonical(manifest)
    return encoded, identity.sign(encoded), ciphertext


def parse_manifest(encoded):
    """Strictly validate the manifest shape; signature and recipient checks are separate."""
    try:
        if not isinstance(encoded, bytes) or len(encoded) > 16384:
            raise ValueError
        manifest = json.loads(encoded)
        if canonical(manifest) != encoded:
            raise ValueError
        image = manifest.get('kind') == FAX_IMAGE
        if (set(manifest) != _MANIFEST_KEYS | ({'kind', 'fax'} if image else set())
                or manifest['version'] != PROTOCOL or not MESSAGE_ID.fullmatch(manifest['message_id'])):
            raise ValueError
        if image and not _fax_facts(manifest['fax'], manifest['document']['pages']):
            raise ValueError
        sender, recipient = manifest['sender'], manifest['recipient']
        document, encryption = manifest['document'], manifest['encryption']
        if (set(sender) != {'organization', 'signing_key', 'fax_number'}
                or not isinstance(sender['organization'], str) or not 0 < len(sender['organization']) <= 200
                or not _B64.fullmatch(sender['signing_key'])
                or not (sender['fax_number'] is None or _NUMBER.fullmatch(sender['fax_number']))):
            raise ValueError
        if set(recipient) != {'fax_number', 'signing_key'} or not _NUMBER.fullmatch(recipient['fax_number']) \
                or not _B64.fullmatch(recipient['signing_key']):
            raise ValueError
        if (set(document) != {'media_type', 'sha256', 'size', 'pages'}
                or document['media_type'] != (FAX_IMAGE_TYPE if image else 'application/pdf')
                or not _HEX64.fullmatch(document['sha256']) or type(document['size']) is not int
                or not 0 < document['size'] <= 100 * 1024 * 1024
                or not (document['pages'] is None or (type(document['pages']) is int and 0 < document['pages'] <= 10000))):
            raise ValueError
        if (set(encryption) != {'scheme', 'ephemeral_key', 'wrapped_key', 'key_nonce', 'nonce', 'ciphertext_sha256'}
                or encryption['scheme'] != SCHEME or not _HEX64.fullmatch(encryption['ciphertext_sha256'])):
            raise ValueError
        parse_timestamp(manifest['created_at'])
        return manifest
    except (ValueError, TypeError, KeyError, RecursionError, DirectProtocolError):
        raise DirectProtocolError('malformed', 'The message is not in the direct delivery format.') from None


def open_document(identity, manifest, ciphertext):
    """Decrypt and check the original bytes against the signed digest and size."""
    encryption = manifest['encryption']
    if hashlib.sha256(ciphertext).hexdigest() != encryption['ciphertext_sha256']:
        raise DirectProtocolError('tampered', 'The document does not match its manifest.')
    ephemeral = unb64(encryption['ephemeral_key'], length=32)
    own = identity.exchange.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    try:
        shared = identity.exchange.exchange(X25519PublicKey.from_public_bytes(ephemeral))
        content_key = AESGCM(_kek(shared, ephemeral, own, manifest['message_id'])).decrypt(
            unb64(encryption['key_nonce'], length=12), unb64(encryption['wrapped_key']),
            canonical({'message_id': manifest['message_id']}))
        document = AESGCM(content_key).decrypt(unb64(encryption['nonce'], length=12), ciphertext, _aad(manifest))
    except (InvalidTag, ValueError):
        raise DirectProtocolError('tampered', 'The document could not be decrypted for this recipient.') from None
    if len(document) != manifest['document']['size'] or hashlib.sha256(document).hexdigest() != manifest['document']['sha256']:
        raise DirectProtocolError('tampered', 'The document does not match its manifest.')
    return document


def signed(identity, document):
    """A statement this installation signs: receipts, refusals and status answers."""
    encoded = canonical({**document, 'signer': identity.signing_key})
    return {'statement': encoded.decode('ascii'), 'signature': identity.sign(encoded)}


def check_signed(envelope, signing_key):
    """Verify a peer's signed statement and return its content."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get('statement'), str):
        raise DirectProtocolError('malformed', 'The answer is not a signed statement.')
    encoded = envelope['statement'].encode('ascii', 'strict')
    verify(signing_key, encoded, envelope.get('signature'))
    try:
        statement = json.loads(encoded)
    except ValueError:
        raise DirectProtocolError('malformed', 'The answer is not a signed statement.') from None
    if not isinstance(statement, dict) or statement.get('signer') != signing_key:
        raise DirectProtocolError('signature', 'The answer was not signed by the partner.')
    return statement


def card(identity, *, organization, fax_number, endpoint):
    """This installation's shareable, self-signed enrollment card."""
    body = {'faxbot_direct': PROTOCOL, 'organization': organization, 'fax_number': fax_number,
            'endpoint': endpoint, 'signing_key': identity.signing_key, 'exchange_key': identity.exchange_key}
    return {**body, 'signature': identity.sign(canonical(body))}


def check_card(document):
    """Validate a partner's card and its self-signature; returns the card fields."""
    if not isinstance(document, dict) or set(document) != {'faxbot_direct', 'organization', 'fax_number', 'endpoint',
                                                           'signing_key', 'exchange_key', 'signature'}:
        raise DirectProtocolError('malformed', 'This is not a Faxbot direct delivery card.')
    body = {key: value for key, value in document.items() if key != 'signature'}
    if (body['faxbot_direct'] != PROTOCOL or not isinstance(body['organization'], str)
            or not 0 < len(body['organization'].strip()) <= 200 or not isinstance(body['fax_number'], str)
            or not _NUMBER.fullmatch(body['fax_number']) or not isinstance(body['endpoint'], str)
            or re.fullmatch(r'https?://[^\s/?#]+(?:/[^\s?#]*)?', body['endpoint']) is None
            or len(body['endpoint']) > 512 or not _B64.fullmatch(str(body['signing_key']))
            or not _B64.fullmatch(str(body['exchange_key']))):
        raise DirectProtocolError('malformed', 'This is not a Faxbot direct delivery card.')
    verify(body['signing_key'], canonical(body), document['signature'])
    return body
