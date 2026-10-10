"""The caller-verification stamp on received faxes (N25): what the network asserted about who called, kept in the
received fax's import report, checked against your registered senders for received faxes. Synthetic numbers,
headers and a self-made certificate authority; not yet run against a real carrier's STIR/SHAKEN result.
"""
import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from app.inbound import caller_check, diversion

ROOT = Path(__file__).resolve().parents[2]
CALLER, DID = '+13035550150', '+13035550100'
AT = datetime(2026, 10, 10, 15, 0)


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _passport(attest='A', *, key=None, dest=DID, iat=None, url='https://cert.example.net/sp.pem'):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    header = {'alg': 'ES256', 'ppt': 'shaken', 'typ': 'passport', 'x5u': url}
    claims = {'attest': attest, 'dest': {'tn': [dest.lstrip('+')]}, 'orig': {'tn': CALLER.lstrip('+')},
              'iat': iat or int(AT.replace(tzinfo=timezone.utc).timestamp()), 'origid': 'synthetic'}
    signing = f'{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}'
    signature = b'\0' * 64
    if key is not None:
        r, s = utils.decode_dss_signature(key.sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
        signature = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
    return f'{signing}.{_b64(signature)};info=<{url}>;alg=ES256;ppt=shaken'


def _authority():
    """BF's synthetic STI-CA chain (test_forwarded_trust.chain): root, served chain and signing key."""
    from api.tests.test_forwarded_trust import chain
    root, middle, leaf, key = chain()
    return root, [leaf, middle], key


@pytest.mark.parametrize('headers, numbers, stamp', [
    ({'P-Asserted-Identity': [f'<sip:{CALLER}@carrier.example;user=phone;verstat=TN-Validation-Passed>']},
     [CALLER], caller_check.VERIFIED_REGISTERED),
    ({'From': [f'"Clinic" <sip:{CALLER}@carrier.example;verstat=TN-Validation-Passed>;tag=1']},
     [], caller_check.VERIFIED_UNREGISTERED),
    ({'P-Asserted-Identity': [f'<sip:{CALLER}@carrier.example;verstat=TN-Validation-Failed>']},
     [CALLER], caller_check.UNVERIFIED),
    ({'P-Asserted-Identity': [f'<sip:{CALLER}@carrier.example;verstat=No-TN-Validation>']}, [], caller_check.UNVERIFIED),
    ({}, [CALLER], caller_check.UNVERIFIED),
])
def test_the_carriers_verstat_and_your_list_decide_the_stamp(headers, numbers, stamp):
    found = caller_check.check(headers, caller=CALLER, did=DID, at=AT, numbers=numbers)
    assert found['stamp'] == stamp and found['registered'] == (CALLER in numbers)
    assert ('genuine' in found['sentence']) == (stamp == caller_check.VERIFIED_REGISTERED)


def test_nothing_asserted_and_no_registered_senders_means_no_stamp():
    assert caller_check.check({}, caller=CALLER, did=DID, at=AT) is None


def test_a_signed_attestation_a_counts_only_when_it_chains_to_an_authority_you_trust():
    ca, served, key = _authority()
    headers = {'Identity': [_passport('A', key=key)]}
    fetch = lambda url: served  # noqa: E731
    signed = caller_check.check(headers, caller=CALLER, did=DID, at=AT, trusted=[ca], fetch=fetch)
    assert signed['stamp'] == caller_check.VERIFIED_UNREGISTERED and signed['attest'] == 'A'
    assert 'attestation A, signed and checked by Faxbot' in signed['sentence']
    # No trusted authority, a B attestation, or a forged signature: unverified, with the reason.
    unanchored = caller_check.check(headers, caller=CALLER, did=DID, at=AT, fetch=fetch)
    assert unanchored['stamp'] == caller_check.UNVERIFIED and 'certificate authority' in unanchored['sentence']
    partial = caller_check.check({'Identity': [_passport('B', key=key)]}, caller=CALLER, did=DID, at=AT,
                                 trusted=[ca], fetch=fetch)
    assert partial['stamp'] == caller_check.UNVERIFIED and 'attestation B' in partial['sentence']
    forged = caller_check.check({'Identity': [_passport('A')]}, caller=CALLER, did=DID, at=AT, trusted=[ca],
                                fetch=fetch)
    assert forged['stamp'] == caller_check.UNVERIFIED and forged['signature'] == diversion.FAILED
    # A diversion PASSporT is not the caller's attestation.
    assert caller_check.shaken({'Identity': [_passport('A').replace('ppt=shaken', 'ppt=div')]}) is not None
    assert caller_check.shaken({'Identity': ['not a passport']}) is None


def test_asterisk_keeps_the_headers_for_a_verstat_or_any_identity_and_the_reader_takes_them():
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    context = dialplan.split('[faxbot-sip-headers]', 1)[1].split('\n\n', 1)[0]
    assert 'GotoIf($[${LEN(${PJSIP_HEADER(read,Identity,1)})} > 0]?keep)' in context
    assert 'verstat=' in context and 'Gosub(faxbot-sip-header,s,1(P-Asserted-Identity,1))' in context
    assert 'Gosub(faxbot-sip-header,s,1(From,1))' in context
    line = f'P-Asserted-Identity {base64.b64encode(b"<sip:+13035550150@c.example;verstat=TN-Validation-Passed>").decode()}'
    assert diversion.parse_header_lines(line)['P-Asserted-Identity'][0].endswith('TN-Validation-Passed>')


def test_the_stamp_is_kept_with_the_fax_and_your_list_is_an_audited_setting(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner
    from api.tests.test_access_management_http import B, BOOTSTRAP, ORIGIN, _environment
    from app.cli.main import app as cli_app
    from app.main import app
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as client:
        view = client.get('/caller-check/registered', headers=B).json()
        assert view['numbers'] == [] and view['sentence'].startswith('No registered senders yet')
        saved = client.put('/caller-check/registered', headers=B, json={'numbers': ['303-555-0150', '+13035550150']})
        assert saved.status_code == 200, saved.text
        assert saved.json()['numbers'] == [CALLER] and saved.json()['saved'].startswith('Saved. 1 registered sender')
        values = app.state.configuration_runtime.manager.store.read().desired.values
        assert values.received_registered_senders == CALLER
        assert client.put('/caller-check/registered', headers=B, json={'numbers': ['not a number']}).status_code == 400

        def run(*args):
            return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                      obj={'client_factory': lambda address, timeout: (client, False)},
                                      env={'COLUMNS': '220', 'TZ': 'UTC'})
        shown = run('providers', 'trunk', 'caller-check', 'show')
        assert shown.exit_code == 0 and CALLER in shown.stdout
        cleared = run('providers', 'trunk', 'caller-check', 'set')
        assert cleared.exit_code == 0 and 'No registered senders yet' in ' '.join(cleared.stdout.split())
    # The stamp travels in the import report the hand-over writes (read back by for_fax).
    import sqlalchemy as sa
    engine = sa.create_engine(f'sqlite:///{tmp_path / "stamp.db"}')
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE inbound_imports (inbound_fax_id TEXT, report TEXT, imported_at TIMESTAMP)')
        stamp = caller_check.check({'P-Asserted-Identity': ['<sip:x;verstat=TN-Validation-Passed>']}, caller=CALLER,
                                   did=DID, at=AT, numbers=[CALLER])
        connection.execute(sa.text('INSERT INTO inbound_imports VALUES (:id, :report, :at)'),
                           {'id': 'f1', 'report': json.dumps({'caller_check': stamp}), 'at': AT})
    assert caller_check.for_fax(engine, 'f1')['stamp'] == caller_check.VERIFIED_REGISTERED
    assert caller_check.for_fax(engine, 'missing') is None
    source = (ROOT / 'api' / 'app' / 'inbound' / 'http.py').read_text()
    assert "report['caller_check'] = stamp" in source and 'if diverted is not None or kept_headers:' in source
