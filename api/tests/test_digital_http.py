"""Digital routes over HTTP, and received Direct messages filed once into a mailbox through the import contract.

The running application (TestClient) with a fake HISP (aiosmtpd with STARTTLS and sign-in) and a fake HISP
mailbox (IMAP over TLS). Synthetic people, 555 numbers and example addresses only; nothing contacts a real HISP,
FHIR server or the NPI registry.
"""
import asyncio

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.main import app
from api.app.digital import certificates, direct_message
from api.app.digital.worker import DigitalWorker
from api.tests.digital_fixtures import (RECIPIENT, SENDER, FakeHisp, Party, client_context, direct_message_from,
                                        hisp_settings, pem_cert, pem_key, pki, synthetic_pdf)
from api.tests.imap_fake import FakeImap
from api.tests.test_access_management_http import B, ORIGIN, _environment
from api.tests.test_work_http import hold_worker, mailbox


def idle(*args, **kwargs):
    return asyncio.sleep(3600)


@pytest.fixture
def client(monkeypatch, tmp_path):
    _environment(monkeypatch, tmp_path)
    monkeypatch.setenv('INBOUND_ENABLED', 'true')
    monkeypatch.setenv('FAX_DEFAULT_COUNTRY', 'US')
    # The lifespan's own digital work waits; each test runs the worker itself.
    monkeypatch.setattr('app.digital.http.repeat', idle)
    hold_worker(monkeypatch)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def generation(client):
    return client.get('/digital/accounts', headers=B).json()['generation']


def add_hisp(client, settings, credentials, key='hisp'):
    return client.post('/digital/accounts', headers=B, json={
        'key': key, 'provider': 'hisp', 'label': 'Synthetic HISP', 'settings': settings, 'credentials': credentials,
        'expected_generation': generation(client)})


def load_bundle(client, key='hisp'):
    response = client.post(f'/digital/accounts/{key}/trust-bundle', headers=B, json={
        'content': certificates.pem([pki().anchor]).decode()})
    assert response.status_code == 200, response.text
    return response.json()


def test_hisp_accounts_keep_secrets_write_only_and_stay_out_of_the_fax_accounts(client):
    settings, credentials = hisp_settings()
    response = add_hisp(client, settings, credentials)
    assert response.status_code == 200, response.text
    (account,) = response.json()['accounts']
    assert set(account['secrets_set']) == {'password', 'private_key'}
    assert credentials['password'] not in response.text and 'PRIVATE KEY' not in response.text
    assert account['health']['state'] == 'not_set_up' and 'trust bundle' in account['health']['sentence']
    assert account['certificate']['sentence'].startswith(SENDER)
    assert account['plan'].startswith('$16.58 a month')
    account = load_bundle(client)['accounts'][0]
    assert account['health']['state'] == 'ready' and account['trust_bundle']['anchors'] == 1
    fax_accounts = client.get('/admin/providers/accounts', headers=B).json()['accounts']
    assert 'hisp' not in {item['key'] for item in fax_accounts}
    off = client.patch('/digital/accounts/hisp', headers=B, json={'enabled': False,
                                                                  'expected_generation': generation(client)})
    assert off.json()['accounts'][0]['health']['state'] == 'off'


def test_refusals_are_one_plain_sentence(client):
    settings, credentials = hisp_settings()
    assert 'kept for Faxbot itself' in add_hisp(client, settings, credentials, key='phaxio').json()['detail']
    other = pem_key(pki().recipient_key)
    response = add_hisp(client, settings, {**credentials, 'private_key': other})
    assert response.status_code == 400 and response.json()['detail'] == ('The private key does not belong to '
                                                                         'this certificate.')
    response = add_hisp(client, {**settings, 'smtp_port': 70000}, credentials)
    assert response.status_code == 400 and 'port number' in response.json()['detail']
    stale = client.post('/digital/accounts', headers=B, json={'key': 'x', 'provider': 'hisp', 'settings': {},
                                                             'credentials': {}, 'expected_generation': -1})
    assert stale.status_code == 409


def test_a_fhir_client_gets_a_signing_key_and_serves_its_public_keys_without_a_key(client):
    added = client.post('/digital/accounts', headers=B, json={
        'key': 'fhir-hospital', 'provider': 'fhir', 'settings': {'client_id': 'faxbot-county-clinic'},
        'expected_generation': generation(client)})
    assert added.status_code == 200, added.text
    assert added.json()['accounts'][0]['health']['state'] == 'not_set_up'
    assert added.json()['accounts'][0]['plan'] == 'No charge for each message.'
    made = client.post('/digital/accounts/fhir-hospital/signing-key', headers=B, json={
        'algorithm': 'ES384', 'expected_generation': generation(client)})
    view = made.json()['accounts'][0]
    assert view['health']['state'] == 'ready' and view['secrets_set'] == ['signing_key']
    (jwk,) = view['public_keys']['keys']
    assert jwk['kty'] == 'EC' and jwk['alg'] == 'ES384' and 'd' not in jwk
    public = TestClient(app, base_url=ORIGIN).get('/digital/jwks/fhir-hospital')
    assert public.status_code == 200 and public.json()['keys'][0]['kid'] == jwk['kid']
    assert TestClient(app, base_url=ORIGIN).get('/digital/jwks/nobody').status_code == 404


def test_recipient_addresses_are_added_confirmed_withdrawn_and_audited(client):
    number = '303-555-0142'
    response = client.post(f'/digital/recipients/{number}', headers=B, json={
        'kind': 'direct', 'address': 'Records@Direct.Hospital.Example.net'})
    assert response.status_code == 200, response.text
    (address,) = response.json()['addresses']
    assert address['address'] == 'records@direct.hospital.example.net' and address['state'] == 'suggested'
    assert response.json()['sentence'].startswith('Faxes to this number go only by fax')
    path = f"/digital/recipients/{number}/addresses/{address['id']}"
    confirmed = client.post(path, headers=B, json={'action': 'confirm'}).json()
    assert confirmed['addresses'][0]['state'] == 'confirmed'
    assert confirmed['sentence'] == ('Faxes to this number may go as Direct message to '
                                     'records@direct.hospital.example.net when that is the better route.')
    again = client.post(path, headers=B, json={'action': 'confirm'})
    assert again.status_code == 400 and again.json()['detail'] == 'This address is already confirmed.'
    withdrawn = client.post(path, headers=B, json={'action': 'withdraw', 'note': 'They left the network.'}).json()
    assert [item['action'] for item in withdrawn['addresses'][0]['history']] == ['withdrawn', 'confirmed',
                                                                                 'suggested']
    engine = app.state.configuration_runtime.manager.store.engine
    with engine.connect() as connection:
        audited = connection.execute(sa.text(
            "SELECT details FROM access_audit WHERE operation = 'digital.address'")).scalars().all()
    assert len(audited) == 3 and all('They left' not in item for item in audited)
    bad = client.post(f'/digital/recipients/{number}', headers=B, json={'kind': 'fhir',
                                                                       'address': 'http://fhir.example.net/r4'})
    assert bad.status_code == 400 and 'https://' in bad.json()['detail']


def test_the_nppes_lookup_files_suggestions_only(client, monkeypatch):
    document = {'results': [{'number': '1234567893', 'basic': {'organization_name': 'SYNTHETIC CLINIC'},
                             'addresses': [{'fax_number': '303-555-0142', 'address_purpose': 'LOCATION'}],
                             'endpoints': [{'endpointType': 'DIRECT',
                                            'endpoint': 'records@direct.clinic.example.net'}]}]}
    monkeypatch.setattr('app.routing.nppes._fetch', lambda params, timeout=10.0: document)
    response = client.post('/digital/recipients/+13035550142/nppes', headers=B, json={'npi': '1234567893'})
    assert response.status_code == 200, response.text
    (address,) = response.json()['addresses']
    assert address['state'] == 'suggested' and address['source'] == 'nppes' and address['npi'] == '1234567893'
    assert response.json()['nppes_sentence'].startswith('NPPES lists these')


def test_a_received_direct_message_is_filed_once_and_answered_processed_then_dispatched(client, tmp_path):
    imap = FakeImap(tmp_path, password=FakeHisp.PASSWORD, user=SENDER)
    hisp = FakeHisp(tmp_path / 'hisp', cert=imap.cert, key=tmp_path / 'imap-key.pem')
    try:
        box = mailbox(client, 'Records', '+15550100001')
        settings, credentials = hisp_settings(hisp, imap, receives=True, mailbox_id=box['id'])
        assert add_hisp(client, settings, credentials).status_code == 200
        load_bundle(client)
        world = pki()
        hospital = Party(RECIPIENT, world.recipient, world.recipient_key, chain=(world.intermediate,))
        document = synthetic_pdf('Synthetic discharge summary')
        raw = direct_message_from(hospital, world.sender, document)
        imap.add(raw)
        runtime = app.state.configuration_runtime
        engine = runtime.manager.store.engine
        transport = direct_message.Transport(ssl_context=client_context(imap.cert), crl_fetch=_no_crl)
        worker = DigitalWorker(engine, values=lambda: runtime.manager.store.read().active.values,
                               access=lambda: app.state.access_runtime, transport=transport)
        worker.step()
        imap.add(raw)            # the HISP delivers the same message again
        worker.step()
        with engine.connect() as connection:
            imports = connection.execute(sa.text(
                "SELECT account, state FROM inbound_imports WHERE account = 'import:digital:hisp'")).all()
        assert imports == [('import:digital:hisp', 'received')]
        received = client.get('/digital/messages?direction=in', headers=B).json()['messages']
        assert [(item['state'], item['counterpart']) for item in received] == [('filed', RECIPIENT)]
        # As the security agent: processed once, then dispatched once, each signed and encrypted for the sender.
        assert len(hisp.messages) == 2 and all(recipients == [RECIPIENT] for _, recipients, _ in hisp.messages)
        dispositions = []
        for _, _, notice in hisp.messages:
            headers, body = direct_message.smime.split_headers(notice)
            opened = direct_message.smime.decrypt(direct_message.smime.transfer_decode(headers, body),
                                                  world.recipient, world.recipient_key)
            content, signature, _ = direct_message.smime.unwrap_signed(opened)
            signed = direct_message.smime.verify_detached(content, signature)
            inner = direct_message.smime.split_headers(signed.content)[1]
            dispositions.append(direct_message.read_notice(inner).kind)
        assert dispositions == ['processed', 'dispatched']
        # A message from an untrusted sender is recorded as not filed and never answered.
        rogue = Party(RECIPIENT, world.rogue, world.rogue_key)
        imap.add(direct_message_from(rogue, world.sender, document, message_id='<rogue.1@example.net>'))
        worker.step()
        messages = {item['counterpart'] + item['state'] for item in client.get(
            '/digital/messages?direction=in', headers=B).json()['messages']}
        assert RECIPIENT + 'not_filed' in messages and len(hisp.messages) == 2
    finally:
        hisp.close()
        imap.close()


def _no_crl(url):
    raise LookupError('no list')


def test_messages_are_listed_for_sent_and_received(client):
    assert client.get('/digital/messages', headers=B).json() == {'messages': []}
    assert client.get('/digital/faxes/' + 'a' * 32, headers=B).json()['messages'] == []
    assert client.get('/digital/messages').status_code in (401, 403)


def test_pem_cert_helper_is_the_chain():
    assert pem_cert(pki().sender, pki().intermediate).count('BEGIN CERTIFICATE') == 2
