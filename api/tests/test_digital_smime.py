"""S/MIME for Direct: sign, encrypt, decrypt and verify, with trust checked against synthetic anchors."""
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from api.app.digital import certificates, smime
from api.tests.digital_fixtures import RECIPIENT, SENDER, pem_key, pki


INNER = (b'Content-Type: text/plain; charset=utf-8\r\n\r\nA synthetic Direct message.\r\n')


def test_sign_encrypt_decrypt_verify_round_trip():
    world = pki()
    signed = smime.signed_entity(INNER, world.sender, world.sender_key, chain=(world.intermediate,))
    enveloped = smime.encrypt(signed, [world.recipient])
    entity = smime.enveloped_entity(enveloped)
    headers, body = smime.split_headers(entity)
    assert smime.parameter(headers['content-type'], 'smime-type') == 'enveloped-data'
    opened = smime.decrypt(smime.transfer_decode(headers, body), world.recipient, world.recipient_key)
    content, signature, opaque = smime.unwrap_signed(opened)
    assert content == INNER and not opaque
    signer = smime.verify_detached(content, signature)
    assert signer.certificate == world.sender and signer.digest == 'sha256'
    checked = certificates.check(signer.certificate, anchors=[world.anchor], intermediates=signer.certificates,
                                 address=SENDER, purpose='sign')
    assert checked.bound == 'address' and checked.path[-1] == world.anchor


def test_a_changed_message_or_wrong_key_is_refused():
    world = pki()
    signed = smime.signed_entity(INNER, world.sender, world.sender_key)
    content, signature, _ = smime.unwrap_signed(signed)
    with pytest.raises(smime.SmimeError, match='changed after it was signed'):
        smime.verify_detached(content.replace(b'synthetic', b'tampered!'), signature)
    enveloped = smime.encrypt(signed, [world.recipient])
    with pytest.raises(smime.SmimeError, match='not encrypted for'):
        smime.decrypt(enveloped, world.sender, world.sender_key)


def test_an_untrusted_or_misbound_certificate_is_refused():
    world = pki()
    with pytest.raises(certificates.CertificateRefused, match='not issued by any authority'):
        certificates.check(world.rogue, anchors=[world.anchor], intermediates=[world.intermediate],
                           address=RECIPIENT)
    with pytest.raises(certificates.CertificateRefused, match='not issued for'):
        certificates.check(world.recipient, anchors=[world.anchor], intermediates=[world.intermediate],
                           address='someone@else.example.net')
    later = datetime.now(timezone.utc) + timedelta(days=800)
    with pytest.raises(certificates.CertificateRefused, match='expired'):
        certificates.check(world.recipient, anchors=[world.anchor], intermediates=[world.intermediate],
                           address=RECIPIENT, now=later)
    with pytest.raises(certificates.CertificateRefused, match='No trust bundle'):
        certificates.check(world.recipient, anchors=[], address=RECIPIENT)
    domain = certificates.check(world.domain_bound, anchors=[world.anchor], intermediates=[world.intermediate],
                                address=RECIPIENT)
    assert domain.bound == 'domain'


def test_trust_bundles_read_as_pkcs7_and_pem():
    from cryptography.hazmat.primitives.serialization import pkcs7
    world = pki()
    bundle = pkcs7.serialize_certificates([world.anchor, world.rogue_anchor], serialization.Encoding.DER)
    assert set(certificates.load_certificates(bundle)) == {world.anchor, world.rogue_anchor}
    pem = pkcs7.serialize_certificates([world.anchor], serialization.Encoding.PEM)
    assert certificates.load_certificates(pem) == [world.anchor]
    assert certificates.load_certificates(certificates.pem([world.anchor])) == [world.anchor]
    with pytest.raises(certificates.CertificateRefused):
        certificates.load_certificates(b'not a certificate')


def test_a_revoked_certificate_is_refused_and_an_unreadable_list_is_noted():
    from cryptography.hazmat.primitives import hashes
    from api.tests.digital_fixtures import certificate, key
    world = pki()
    leaf_key = key()
    leaf = certificate('Revoked', leaf_key, world.intermediate.subject, world.intermediate_key, email=RECIPIENT,
                       crl='http://crl.example.net/ca.crl')
    now = datetime.now(timezone.utc)
    crl = (x509.CertificateRevocationListBuilder().issuer_name(world.intermediate.subject)
           .last_update(now - timedelta(hours=1)).next_update(now + timedelta(days=1))
           .add_revoked_certificate(x509.RevokedCertificateBuilder().serial_number(leaf.serial_number)
                                    .revocation_date(now - timedelta(minutes=5)).build())
           .sign(world.intermediate_key, hashes.SHA256()))
    certificates._CRLS.clear()
    with pytest.raises(certificates.CertificateRefused, match='revoked'):
        certificates.check(leaf, anchors=[world.anchor], intermediates=[world.intermediate], address=RECIPIENT,
                           crl_fetch=lambda url: crl.public_bytes(serialization.Encoding.DER))
    certificates._CRLS.clear()

    def unreachable(url):
        raise LookupError('no answer')
    checked = certificates.check(leaf, anchors=[world.anchor], intermediates=[world.intermediate],
                                 address=RECIPIENT, crl_fetch=unreachable)
    assert checked.notes


@pytest.mark.skipif(shutil.which('openssl') is None, reason='openssl is not installed here')
def test_a_signature_made_by_openssl_verifies(tmp_path):
    """An independent producer (OpenSSL's cms -sign) checked by Faxbot's verifier, and the reverse."""
    world = pki()
    (tmp_path / 'content.txt').write_bytes(INNER)
    (tmp_path / 'signer.pem').write_bytes(world.sender.public_bytes(serialization.Encoding.PEM))
    (tmp_path / 'signer.key').write_text(pem_key(world.sender_key))
    result = subprocess.run(['openssl', 'cms', '-sign', '-binary', '-in', str(tmp_path / 'content.txt'),
                             '-signer', str(tmp_path / 'signer.pem'), '-inkey', str(tmp_path / 'signer.key'),
                             '-md', 'sha256', '-outform', 'DER', '-out', str(tmp_path / 'sig.der')],
                            capture_output=True)
    if result.returncode != 0:
        pytest.skip('this openssl has no cms command')
    signature = (tmp_path / 'sig.der').read_bytes()
    signer = smime.verify_detached(INNER, signature, opaque=False)
    assert signer.certificate == world.sender
    # The reverse: OpenSSL verifies Faxbot's detached signature.
    ours = smime.sign_detached(INNER, world.sender, world.sender_key)
    (tmp_path / 'ours.der').write_bytes(ours)
    (tmp_path / 'anchor.pem').write_bytes(world.anchor.public_bytes(serialization.Encoding.PEM)
                                          + world.intermediate.public_bytes(serialization.Encoding.PEM))
    check = subprocess.run(['openssl', 'cms', '-verify', '-binary', '-inform', 'DER', '-in', str(tmp_path / 'ours.der'),
                            '-content', str(tmp_path / 'content.txt'), '-CAfile', str(tmp_path / 'anchor.pem'),
                            '-purpose', 'any', '-out', '/dev/null'], capture_output=True)
    assert check.returncode == 0, check.stderr.decode()
