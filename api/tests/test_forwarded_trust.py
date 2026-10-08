"""Certificate authorities you trust for forwarded calls (X4): a forwarding is verified only when the carrier's
signing certificate chains to one. Synthetic root, intermediate and signing certificates made here (the shape of a
STIR/SHAKEN chain: an STI-CA root, an intermediate, and a signing certificate with no web-server fields)."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.inbound import diversion, trust
from api.tests.test_diversion import AT, CERT_URL, FORWARDED, TO, passport
from api.tests.test_receiving_rules_http import handover, http, mailbox, rule  # noqa: F401
from api.tests.test_inbound_acquisition import ADMIN, providers, rows  # noqa: F401 - fixture


def _certificate(name, key, issuer, issuer_key, *, ca, path=None, start=None, end=None):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID
    start = start or datetime(2026, 1, 1)
    end = end or datetime(2027, 12, 31)
    builder = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
               .issuer_name(issuer).public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(start).not_valid_after(end)
               .add_extension(x509.BasicConstraints(ca=ca, path_length=path), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.KeyUsage(not ca, False, False, False, False, ca, ca, False, False), critical=True))
    if issuer_key is not key:
        builder = builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                                        critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def chain():
    """(root, intermediate, signing certificate, signing key): a synthetic STI-CA chain."""
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    root_key, middle_key, leaf_key = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Synthetic STI-CA Root')])
    root = _certificate('Synthetic STI-CA Root', root_key, root_name, root_key, ca=True, path=1)
    middle = _certificate('Synthetic STI-CA Intermediate', middle_key, root.subject, root_key, ca=True, path=0)
    leaf = _certificate('SHAKEN 1234', leaf_key, middle.subject, middle_key, ca=False)
    return root, middle, leaf, leaf_key


def pem(*certificates):
    from cryptography.hazmat.primitives.serialization import Encoding
    return ''.join(certificate.public_bytes(Encoding.PEM).decode('ascii') for certificate in certificates)


def test_a_forwarding_signed_under_a_trusted_authority_is_verified_and_others_are_not():
    root, middle, leaf, key = chain()
    other_root, _, _, _ = chain()
    headers = {'Identity': [passport(key)]}
    check = lambda trusted, tamper=False: diversion.diversion_for(  # noqa: E731
        {'Identity': [passport(key, tamper=tamper)]} if tamper else headers, did=TO, at=AT, check=True, country='US',
        fetch=lambda url: [leaf, middle], trusted=trusted)
    verified = check([root])
    assert verified.state == 'signed' and 'is verified' in verified.sentence
    elsewhere = check([other_root])
    assert elsewhere.state == 'unanchored' and 'which no certificate authority you trust issued' in elsewhere.sentence
    none = check([])
    assert none.state == 'unanchored' and 'you trust no certificate authority' in none.sentence
    # A wrong signature fails whatever you trust; so does a chain whose intermediate is missing.
    assert check([root], tamper=True).state == 'failed'
    missing = diversion.diversion_for(headers, did=TO, at=AT, check=True, country='US', fetch=lambda url: [leaf],
                                      trusted=[root])
    assert missing.state == 'unanchored'


def test_only_certificate_authorities_are_kept_each_once_and_one_is_removed_by_its_fingerprint():
    root, middle, leaf, _ = chain()
    values = type('Values', (), {'stir_trust_anchors': ''})()
    values.stir_trust_anchors = trust.added(values, pem=pem(root, middle, leaf),
                                            today=datetime(2026, 10, 8, tzinfo=timezone.utc))
    found = trust.anchors(values)
    assert [anchor.name for anchor in found] == ['Synthetic STI-CA Root', 'Synthetic STI-CA Intermediate']
    assert {anchor.source for anchor in found} == {'pasted'} and found[0].added_on == '2026-10-08'
    assert trust.added(values, pem=pem(root)) == values.stir_trust_anchors  # each once
    with pytest.raises(trust.TrustRefused, match='not certificate authorities'):
        trust.added(values, pem=pem(leaf))
    with pytest.raises(trust.TrustRefused, match='No certificate was found'):
        trust.added(values, pem='hello')
    with pytest.raises(trust.TrustRefused, match='at least 8'):
        trust.removed(values, 'ab')
    values.stir_trust_anchors = trust.removed(values, found[1].fingerprint[:12])
    assert [anchor.name for anchor in trust.anchors(values)] == ['Synthetic STI-CA Root']
    assert trust.certificates(values) == [root]


class Response:
    def __init__(self, status_code, content):
        self.status_code, self.content = status_code, content


def test_a_list_at_an_address_is_read_once_over_https_also_as_json():
    root, middle, _, _ = chain()
    values = type('Values', (), {'stir_trust_anchors': ''})()
    listed = json.dumps({'trustList': [pem(root), pem(middle)]}).encode()
    value = trust.added(values, url='https://trust.carrier.example/sti-ca.json',
                        get=lambda url: Response(200, listed))
    values.stir_trust_anchors = value
    assert {anchor.source for anchor in trust.anchors(values)} == {'https://trust.carrier.example/sti-ca.json'}
    assert len(trust.anchors(values)) == 2
    with pytest.raises(trust.TrustRefused, match='https://'):
        trust.added(values, url='http://trust.carrier.example/list.pem', get=lambda url: Response(200, listed))
    with pytest.raises(trust.TrustRefused, match='answered 302'):
        trust.added(values, url='https://moved.example/list.pem', get=lambda url: Response(302, b''))
    with pytest.raises(trust.TrustRefused, match='not both'):
        trust.added(values, pem=pem(root), url='https://trust.carrier.example/list.pem')


def test_trusting_authorities_is_an_audited_settings_change_and_verified_forwardings_route(
        http, isolated_installation, tmp_path, monkeypatch):
    from api.tests.test_diversion import _headers_file
    root, middle, leaf, key = chain()
    assert http.get('/admin/forwarded-trust', headers=ADMIN).json()['anchors'] == []
    audits = len(rows(isolated_installation, 'access_audit'))
    refused = http.post('/admin/forwarded-trust', headers=ADMIN, json={'pem': pem(leaf)})
    assert refused.status_code == 409 and 'not certificate authorities' in refused.json()['detail']
    added = http.post('/admin/forwarded-trust', headers=ADMIN, json={'pem': pem(root)})
    assert added.status_code == 200, added.text
    assert [anchor['name'] for anchor in added.json()['anchors']] == ['Synthetic STI-CA Root']
    assert added.json()['sentence'] == 'You trust 1 certificate authority for forwarded calls.'
    assert len(rows(isolated_installation, 'access_audit')) > audits
    # A forwarding signed under it is verified, so the default "forwarded from" rule takes it.
    front, records = mailbox(http, 'Front desk'), mailbox(http, 'Records')
    rule(http, to_number=TO, mailbox_id=front)
    rule(http, to_number=TO, mailbox_id=records, diverted_from=FORWARDED)
    monkeypatch.setattr(diversion, 'fetch_chain', lambda url: [leaf, middle])
    now = datetime.now(timezone.utc)
    signed = passport(key, iat=int(now.timestamp()))
    _headers_file(tmp_path, '1791049801.1', Identity=signed)
    fax = handover(http, tmp_path, 'verified', uniqueid='1791049801.1', call={'started_at': int(now.timestamp())})
    found = {item['id']: item for item in http.get('/inbound', headers=ADMIN).json()}[fax]
    assert (found['mailbox'], found['diversion']) == ('Records', 'signed'), found
    removed = http.delete(f"/admin/forwarded-trust/{added.json()['anchors'][0]['short']}", headers=ADMIN)
    assert removed.status_code == 200 and removed.json()['anchors'] == []


def test_the_command_line_lists_adds_and_removes_trusted_authorities(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    root, middle, _, _ = chain()
    certificates = tmp_path / 'sti-ca.pem'
    certificates.write_text(pem(root, middle))
    for client in _serve(monkeypatch, tmp_path):
        cli = Cli(client)
        empty = cli('numbers', 'forwarded-trust', 'list')
        assert empty.exit_code == 0 and 'You trust no certificate authority for forwarded calls yet' in empty.stdout
        added = cli('numbers', 'forwarded-trust', 'add', str(certificates))
        assert added.exit_code == 0, (added.stdout, added.stderr)
        assert 'You trust 2 certificate authorities for forwarded calls.' in added.stdout
        assert 'Synthetic STI-CA Root' in added.stdout
        both = cli('numbers', 'forwarded-trust', 'add', str(certificates), '--url', 'https://x.example/l.pem')
        assert both.exit_code != 0 and 'not both' in both.stderr
        short = cli.json('numbers', 'forwarded-trust', 'list')['anchors'][0]['short']
        removed = cli('numbers', 'forwarded-trust', 'remove', short)
        assert removed.exit_code == 0 and 'You trust 1 certificate authority' in removed.stdout
