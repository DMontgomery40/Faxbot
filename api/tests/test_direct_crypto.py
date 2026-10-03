"""Direct delivery envelope, manifest, card and key file; cryptography primitives only."""
import json
import os
import stat

import pytest

from api.app.direct.crypto import (DirectProtocolError, Identity, canonical, card, check_card, check_signed,
                                   open_document, parse_manifest, seal, signed)
from api.app.direct.identity import IdentityUnavailable, load_identity


ORIGINAL = b'%PDF-1.7\n' + os.urandom(4096) + b'\n%%EOF\n'


def sealed(sender, recipient, **changes):
    arguments = dict(message_id='a' * 32, organization='Valley Hospital', fax_number='+15550100001',
                     recipient_number='+15550100002', recipient_signing_key=recipient.signing_key,
                     recipient_exchange_key=recipient.exchange_key, document=ORIGINAL, pages=2)
    arguments.update(changes)
    return seal(sender, **arguments)


def test_recipient_recovers_the_exact_original_bytes():
    sender, recipient = Identity.generate(), Identity.generate()
    manifest_bytes, signature, ciphertext = sealed(sender, recipient)
    manifest = parse_manifest(manifest_bytes)
    assert manifest['document']['size'] == len(ORIGINAL) and ORIGINAL not in ciphertext
    assert open_document(recipient, manifest, ciphertext) == ORIGINAL
    from api.app.direct.crypto import verify
    verify(sender.signing_key, manifest_bytes, signature)


def test_tampering_and_other_recipients_are_rejected():
    sender, recipient, stranger = Identity.generate(), Identity.generate(), Identity.generate()
    manifest_bytes, signature, ciphertext = sealed(sender, recipient)
    manifest = parse_manifest(manifest_bytes)
    flipped = bytearray(ciphertext)
    flipped[10] ^= 1
    with pytest.raises(DirectProtocolError) as error:
        open_document(recipient, manifest, bytes(flipped))
    assert error.value.reason == 'tampered'
    with pytest.raises(DirectProtocolError):
        open_document(stranger, manifest, ciphertext)
    # A manifest edited after signing no longer verifies.
    edited = canonical({**manifest, 'recipient': {**manifest['recipient'], 'fax_number': '+15550100009'}})
    from api.app.direct.crypto import verify
    with pytest.raises(DirectProtocolError) as error:
        verify(sender.signing_key, edited, signature)
    assert error.value.reason == 'signature'
    # A digest edited to match tampered bytes breaks the authenticated encryption.
    with pytest.raises(DirectProtocolError):
        open_document(recipient, {**manifest, 'document': {**manifest['document'], 'sha256': '0' * 64}}, ciphertext)


def test_manifest_must_be_canonical_and_complete():
    sender, recipient = Identity.generate(), Identity.generate()
    manifest_bytes, _, _ = sealed(sender, recipient)
    manifest = json.loads(manifest_bytes)
    with pytest.raises(DirectProtocolError):
        parse_manifest(json.dumps(manifest, indent=2).encode())
    del manifest['encryption']
    with pytest.raises(DirectProtocolError):
        parse_manifest(canonical(manifest))
    with pytest.raises(DirectProtocolError):
        parse_manifest(b'{"version": 1}')


def test_cards_and_statements_are_self_signed():
    identity = Identity.generate()
    document = card(identity, organization='County Clinic', fax_number='+15550100002', endpoint='https://clinic.example')
    assert check_card(document)['signing_key'] == identity.signing_key
    with pytest.raises(DirectProtocolError):
        check_card({**document, 'endpoint': 'https://attacker.example'})
    statement = signed(identity, {'type': 'receipt', 'message_id': 'b' * 32})
    assert check_signed(statement, identity.signing_key)['type'] == 'receipt'
    with pytest.raises(DirectProtocolError):
        check_signed(statement, Identity.generate().signing_key)


def test_key_file_is_private_created_once_and_never_replaced(tmp_path):
    path = tmp_path / 'keys' / '.direct-identity.key'
    with pytest.raises(IdentityUnavailable):
        load_identity(path)
    first = load_identity(path, create=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_identity(path, create=True).signing_key == first.signing_key
    assert first.signing_key not in path.read_text()  # Only private material is stored, never shared keys.
    path.chmod(0o644)
    with pytest.raises(IdentityUnavailable):
        load_identity(path)
