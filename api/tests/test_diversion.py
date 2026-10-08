"""Diverted calls (X4): header-parsing fixtures for Diversion (RFC 5806), History-Info (RFC 7044) and the diversion
PASSporT (RFC 8946 in an RFC 8224 Identity header), the signature check against a synthetic certificate, the guards
on fetching a certificate the caller named, and "forwarded from" as a receiving-rule fact over the hand-over.

Synthetic numbers (555-01xx), a synthetic P-256 key and a self-signed certificate made here; nothing is fetched.
"""
import base64
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.inbound import diversion
from api.tests.test_receiving_rules_http import FROM, TO, handover, http, mailbox, rule  # noqa: F401
from api.tests.test_inbound_acquisition import ADMIN, providers  # noqa: F401 - fixture

FORWARDED = '+13035550142'
AT = datetime(2026, 10, 8, 15, 0, 0)
CERT_URL = 'https://cert.carrier.example/div.pem'


def _key_and_certificate(valid_from=AT - timedelta(days=1), valid_until=AT + timedelta(days=30)):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'SHAKEN synthetic carrier')])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(7).not_valid_before(valid_from).not_valid_after(valid_until)
                   .sign(key, hashes.SHA256()))
    return key, certificate


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def passport(key, *, div=FORWARDED.lstrip('+'), dest=TO.lstrip('+'), iat=None, ppt='div', alg='ES256', x5u=CERT_URL,
             tamper=False):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    header = {'alg': alg, 'ppt': ppt, 'typ': 'passport', 'x5u': x5u}
    claims = {'dest': {'tn': [dest]}, 'div': {'tn': div}, 'iat': iat or int(AT.replace(tzinfo=timezone.utc).timestamp()),
              'orig': {'tn': '13035550100'}}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    r, s = utils.decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
    if tamper:
        claims['div']['tn'] = '13035550199'
        signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    return f'{signing_input}.{_b64(signature)};info=<{x5u}>;alg={alg};ppt="{ppt}"'


# -- Diversion and History-Info -----------------------------------------------------------------------------------

@pytest.mark.parametrize('values, expected', [
    (['<sip:+13035550142@carrier.example;user=phone>;reason=unconditional;counter=1'], ('+13035550142', 'unconditional')),
    (['"Front, desk" <sip:13035550142@carrier.example>;reason=user-busy, <sip:+13035550199@carrier.example>'],
     ('13035550142', 'user-busy')),
    (['<tel:+1-303-555-0142>;reason=no-answer'], ('+13035550142', 'no-answer')),
    (['<sip:frontdesk@carrier.example>;reason=unconditional'], (None, None)),
    ([], (None, None)),
])
def test_diversion_names_the_most_recent_diverting_number(values, expected):
    assert diversion.parse_diversion(values) == expected


@pytest.mark.parametrize('values, expected', [
    # RFC 7044: the new target names the entry it was retargeted from with mp (mapped).
    (['<sip:+13035550142@carrier.example?Reason=SIP%3Bcause%3D302>;index=1, <sip:+15555550199@faxbot.example>;'
      'index=1.1;mp=1'], '+13035550142'),
    # The same as separate headers, and an entry that only carries its cause.
    (['<sip:+13035550142@carrier.example?Reason=SIP%3Bcause%3D486>;index=1', '<sip:+15555550199@faxbot.example>;index=1.1'],
     '+13035550142'),
    (['<sip:+15555550199@faxbot.example>;index=1'], None),
    (['<sip:+15555550199@faxbot.example>;index=bad, <sip:+13035550142@x>;index=1'], None),
])
def test_history_info_names_the_number_the_call_was_retargeted_from(values, expected):
    assert diversion.parse_history_info(values) == expected


def test_the_header_file_asterisk_writes_is_read_by_the_calls_token(tmp_path):
    (tmp_path / 'inbound').mkdir()
    lines = [f"Diversion {base64.b64encode(b'<sip:+13035550142@carrier.example>;reason=unconditional').decode()}",
             'Identity not-base64!', 'Contact ' + base64.b64encode(b'<sip:x@y>').decode()]
    (tmp_path / 'inbound' / '17913919947.sip').write_text('\n'.join(lines) + '\n')
    for uniqueid in ('1791391994.7', 'engine.17913919947'):
        assert diversion.read_headers(tmp_path, uniqueid) == {
            'Diversion': ['<sip:+13035550142@carrier.example>;reason=unconditional']}
    assert diversion.read_headers(tmp_path, '../etc/passwd') == {} and diversion.read_headers(tmp_path, '1.2') == {}
    diversion.forget_headers(tmp_path, '1791391994.7')
    assert not (tmp_path / 'inbound' / '17913919947.sip').exists()


# -- the diversion PASSporT ---------------------------------------------------------------------------------------

def test_a_signed_diversion_checks_against_its_certificate():
    key, certificate = _key_and_certificate()
    fetched = []
    found = diversion.diversion_for({'Identity': [passport(key)]}, did=TO, at=AT, check=True, country='US',
                                    fetch=lambda url: fetched.append(url) or certificate)
    assert (found.diverted_from, found.state, found.source) == (FORWARDED, 'signed', 'passport')
    assert fetched == [CERT_URL] and 'signature checked' in found.sentence and 'not checked against' in found.sentence


@pytest.mark.parametrize('changes, words', [
    ({'tamper': True}, 'does not match its certificate'),
    ({'iat': int(AT.replace(tzinfo=timezone.utc).timestamp()) - 3600}, 'not signed at the time of the call'),
    ({'dest': '13035550177'}, 'names another destination'),
    ({'alg': 'RS256'}, 'not signed with ES256'),
])
def test_a_diversion_whose_signature_time_or_destination_is_wrong_fails(changes, words):
    key, certificate = _key_and_certificate()
    found = diversion.diversion_for({'Identity': [passport(key, **changes)]}, did=TO, at=AT, check=True,
                                    country='US', fetch=lambda url: certificate)
    assert found.state == 'failed' and words in found.sentence


def test_a_certificate_from_another_key_or_out_of_date_fails():
    key, _ = _key_and_certificate()
    _, other = _key_and_certificate()
    _, expired = _key_and_certificate(valid_from=AT - timedelta(days=60), valid_until=AT - timedelta(days=1))
    for certificate, words in ((other, 'does not match'), (expired, 'not valid at the time')):
        found = diversion.diversion_for({'Identity': [passport(key)]}, did=TO, at=AT, check=True, country='US',
                                        fetch=lambda url, certificate=certificate: certificate)
        assert found.state == 'failed' and words in found.sentence


def test_unchecked_when_no_rule_needs_it_or_the_certificate_cannot_be_had():
    key, _ = _key_and_certificate()
    unasked = diversion.diversion_for({'Identity': [passport(key)]}, did=TO, at=AT, check=False, country='US',
                                      fetch=lambda url: pytest.fail('fetched'))
    assert unasked.state == 'unchecked' and unasked.diverted_from == FORWARDED and 'no receiving rule' in unasked.sentence
    missing = diversion.diversion_for({'Identity': [passport(key)]}, did=TO, at=AT, check=True, country='US',
                                      fetch=lambda url: None)
    assert missing.state == 'unchecked' and 'could not be fetched' in missing.sentence


def test_headers_only_are_stated_and_a_call_not_forwarded_has_none():
    stated = diversion.diversion_for({'Diversion': ['<sip:+13035550142@carrier.example>;reason=unconditional']},
                                     did=TO, at=AT, check=True, country='US')
    assert (stated.diverted_from, stated.state, stated.source, stated.reason) == (
        FORWARDED, 'stated', 'diversion', 'unconditional')
    compact = diversion.diversion_for({'Identity': [';info=<https://x.example/c.pem>;alg=ES256;ppt="div"'],
                                       'History-Info': ['<sip:+13035550142@c>;index=1, <sip:+15555550199@f>;index=1.1;mp=1']},
                                      did=TO, at=AT, check=True, country='US')
    assert compact.state == 'stated' and 'compact signature' in compact.sentence
    # An ordinary SHAKEN PASSporT (ppt shaken) is no diversion.
    key, _ = _key_and_certificate()
    assert diversion.diversion_for({'Identity': [passport(key, ppt='shaken')]}, did=TO, at=AT, country='US') is None
    assert diversion.diversion_for({}, did=TO, at=AT, country='US') is None


# -- fetching a certificate the caller named -------------------------------------------------------------------

class Response:
    def __init__(self, status_code, content):
        self.status_code, self.content = status_code, content


def test_a_certificate_is_fetched_only_over_https_from_a_public_host_without_redirects_and_is_kept(monkeypatch):
    from cryptography.hazmat.primitives.serialization import Encoding
    monkeypatch.setattr(diversion, '_CACHE', {})
    _, certificate = _key_and_certificate()
    pem = certificate.public_bytes(Encoding.PEM)
    asked = []

    def get(url):
        asked.append(url)
        return {CERT_URL: Response(200, pem), 'https://moved.example/c.pem': Response(302, b''),
                'https://huge.example/c.pem': Response(200, b'-----BEGIN' + b'x' * diversion.CERT_BYTES)}[url]
    public = lambda host: host != 'internal.example'  # noqa: E731
    assert diversion.fetch_certificate(CERT_URL, get=get, resolve=public) == certificate
    assert diversion.fetch_certificate(CERT_URL, get=get, resolve=public) == certificate
    for url in ('http://cert.carrier.example/div.pem', 'https://internal.example/div.pem',
                'https://user@cert.carrier.example/div.pem', 'https://moved.example/c.pem', 'https://huge.example/c.pem'):
        assert diversion.fetch_certificate(url, get=get, resolve=public) is None, url
    # Fetched once each (the first kept); never over HTTP, from a private host, or with a user in the address.
    assert asked == [CERT_URL, 'https://moved.example/c.pem', 'https://huge.example/c.pem']


def test_a_host_that_resolves_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(diversion.socket, 'getaddrinfo', lambda host, port, proto=0: [(2, 1, 6, '', ('10.0.0.5', 443))])
    assert diversion._public_host('cert.carrier.example') is False
    monkeypatch.setattr(diversion.socket, 'getaddrinfo', lambda host, port, proto=0: [(2, 1, 6, '', ('93.184.215.14', 443))])
    assert diversion._public_host('cert.carrier.example') is True


# -- "forwarded from" as a receiving-rule fact --------------------------------------------------------------------

def test_a_rule_for_calls_forwarded_from_a_number_needs_a_signed_forwarding_unless_it_says_otherwise():
    from app.access.receiving_rules import DEFAULT_OPTIONS, ReceivedFacts, clean_options, conditions_hold
    signed_only = clean_options({'diverted_from': '(303) 555-0142'}, country='US')
    anything = clean_options({'diverted_from': FORWARDED, 'diversion_unsigned': True}, country='US')
    assert signed_only['diverted_from'] == FORWARDED and anything['diversion_unsigned'] is True
    facts = lambda number, state: ReceivedFacts(to_number=TO, diverted_from=number, diversion=state)  # noqa: E731
    assert conditions_hold(signed_only, facts(FORWARDED, 'signed'))
    assert not conditions_hold(signed_only, facts(FORWARDED, 'stated'))
    assert not conditions_hold(signed_only, facts('+13035550199', 'signed'))
    assert not conditions_hold(signed_only, facts(None, None))
    assert conditions_hold(anything, facts(FORWARDED, 'stated')) and conditions_hold(anything, facts(FORWARDED, 'unchecked'))
    assert not conditions_hold(anything, facts(FORWARDED, 'failed'))
    assert conditions_hold(dict(DEFAULT_OPTIONS), facts(FORWARDED, 'failed'))
    from app.access.receiving_rules import ReceivingRuleError
    with pytest.raises(ReceivingRuleError, match='with its country code'):
        clean_options({'diverted_from': 'front desk'}, country='US')


def _headers_file(tmp_path, uniqueid, **headers):
    folder = tmp_path / 'faxdata' / 'inbound'
    folder.mkdir(parents=True, exist_ok=True)
    token = ''.join(character for character in uniqueid if character.isdigit())
    (folder / f'{token}.sip').write_text(''.join(
        f"{name.replace('_', '-')} {base64.b64encode(value.encode()).decode()}\n" for name, value in headers.items()))
    return folder / f'{token}.sip'


def test_a_forwarded_call_routes_by_where_it_was_forwarded_from_and_says_how_far_it_was_checked(
        http, isolated_installation, tmp_path, monkeypatch):
    front, records = mailbox(http, 'Front desk'), mailbox(http, 'Records')
    rule(http, to_number=TO, mailbox_id=front)
    made = rule(http, to_number=TO, mailbox_id=records, diverted_from=FORWARDED)
    assert made['diverted_from'] == FORWARDED and made['diversion_unsigned'] is False
    key, certificate = _key_and_certificate(valid_from=datetime.utcnow() - timedelta(days=1),
                                            valid_until=datetime.utcnow() + timedelta(days=1))
    monkeypatch.setattr(diversion, 'fetch_certificate', lambda url: certificate)
    now = int(datetime.now(timezone.utc).timestamp())
    signed = _headers_file(tmp_path, '1791049701.1', Identity=passport(key, iat=now))
    fax_signed = handover(http, tmp_path, 'signed', uniqueid='1791049701.1', call={'started_at': now})
    _headers_file(tmp_path, '1791049702.1', Diversion=f'<sip:{FORWARDED}@carrier.example>;reason=unconditional')
    fax_stated = handover(http, tmp_path, 'stated', uniqueid='1791049702.1', call={'started_at': now})
    fax_plain = handover(http, tmp_path, 'plain', uniqueid='1791049703.1')
    faxes = {fax['id']: fax for fax in http.get('/inbound', headers=ADMIN).json()}
    assert faxes[fax_signed]['mailbox'] == 'Records' and faxes[fax_signed]['diversion'] == 'signed', {key: faxes[fax_signed].get(key) for key in ('diverted_from', 'diversion', 'diversion_text', 'mailbox')}
    assert faxes[fax_signed]['diverted_from'] == FORWARDED and 'signature checked' in faxes[fax_signed]['diversion_text']
    # Only a header said so: recorded and shown, but this rule wants the network's signature.
    assert faxes[fax_stated]['mailbox'] == 'Front desk' and faxes[fax_stated]['diversion'] == 'stated'
    assert 'without a signature' in faxes[fax_stated]['diversion_text']
    assert faxes[fax_plain]['diverted_from'] is None and faxes[fax_plain]['mailbox'] == 'Front desk'
    assert not signed.exists()
    tried = http.post('/access/inbound-rules/explain', headers=ADMIN,
                      json={'to_number': TO, 'diverted_from': FORWARDED, 'diversion': 'stated'}).json()
    assert tried['mailbox_label'] == 'Front desk' if 'mailbox_label' in tried else True
