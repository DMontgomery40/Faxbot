"""Two installations exchange an original document over the real HTTPS stack.

Installation B is the running application (TestClient). Installation A is a
real durable delivery store whose direct route posts to B through the client.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hashlib
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from api.tests.test_schema import database
from api.tests.test_routing_http import ADMIN, BOOTSTRAP
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_profiles import ProviderConfiguration
from api.app.direct.crypto import Identity, card, seal
from api.app.direct.service import DirectReconciler, DirectRoute, DirectService
from api.app.outbound_store import OutboundStore
from api.app.outbound_worker import OutboundWorker, SubmissionReceipt
from api.app.routing.store import RouteStore
from api.app.routing.transport import RoutedTransport


A_NUMBER, B_NUMBER = '+15550100001', '+15550100002'


def pdf_bytes(text):
    from io import BytesIO
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(72, 720, text)
    pdf.showPage()
    pdf.save()
    return output.getvalue()


class ToB:
    """A's transport to installation B; can lose B's answer after B has it, or drop the request."""
    def __init__(self, client):
        self.client = client
        self.mode = 'normal'
        self.posts = 0

    async def request(self, method, url, **kwargs):
        assert url.startswith('https://testserver/')
        path = url[len('https://testserver'):]
        if self.mode == 'drop':
            self.dropped = (method, path, kwargs)
            raise httpx.ReadTimeout('request lost before reaching the partner')
        response = await asyncio.to_thread(self.client.request, method, path, **kwargs)
        if method == 'POST':
            self.posts += 1
        if self.mode == 'lose_answer' and method == 'POST':
            raise httpx.ReadTimeout('answer lost after the partner received it')
        return response.status_code, response.json()


class ToA:
    """B's transport to installation A, answering in-process."""
    def __init__(self, service):
        self.service = service

    async def request(self, method, url, **kwargs):
        assert method == 'POST' and url == 'https://a.example/direct/verifications'
        body = kwargs['json']
        return await asyncio.to_thread(self.service.confirm, body['statement'], body['signature'])


class Conventional:
    """A's ordinary fax provider transport (synthetic)."""
    def __init__(self, store):
        self.store = store
        self.submissions = 0

    @asynccontextmanager
    async def prepare(self, claim):
        self.store.load_dispatch(claim)
        yield self

    async def submit(self):
        self.submissions += 1
        return SubmissionReceipt('PX-' + uuid4().hex[:8], 'in_progress')


@pytest.fixture(params=['sqlite', 'postgresql'])
def direct_databases(request):
    """The two installations' databases: SQLite files, or PostgreSQL schemas dropped afterwards."""
    from api.tests.test_peer_fax import _namespaces
    return _namespaces(request, 2)


@pytest.fixture
def b_client(isolated_installation, monkeypatch, tmp_path, direct_databases):
    b_url = direct_databases[1].render_as_string(hide_password=False) if direct_databases else None
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0', 'DIRECT_DELIVERY_ENABLED': 'true',
                        'DIRECT_ORGANIZATION': 'County Clinic', 'DIRECT_FAX_NUMBER': B_NUMBER,
                        'DIRECT_ALLOW_PRIVATE_PEERS': 'true',
                        'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'b-direct.key'),
                        **({'DATABASE_URL': b_url} if b_url else {})}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client
    main.app.state.direct_http = None


@pytest.fixture
def pair(b_client, tmp_path, direct_databases):
    from api.app.schema import create_database_engine
    engine = create_database_engine(direct_databases[0].render_as_string(hide_password=False) if direct_databases
                                    else 'sqlite:///' + str(tmp_path / 'a.db'))
    upgrade_schema(engine)
    data = tmp_path / 'a-data'
    data.mkdir()
    configuration = ConfigurationStore(engine, tmp_path / 'a-installation.key')
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(data),
        'PUBLIC_API_URL': 'https://a.example', 'DIRECT_DELIVERY_ENABLED': 'true',
        'DIRECT_ORGANIZATION': 'Valley Hospital', 'DIRECT_FAX_NUMBER': A_NUMBER,
        'DIRECT_ALLOW_PRIVATE_PEERS': 'true'})
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'k', 'api_secret': 's'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    to_b = ToB(b_client)
    a = DirectService(engine, values=lambda: values, environment={'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'a.key')},
                      http=to_b)
    main.app.state.direct_http = ToA(a)
    # Enroll each other with self-signed cards.
    b_card = b_client.get('/direct/card', headers=ADMIN).json()['card']
    enrolled = b_client.post('/direct/peers', headers=ADMIN, json={'card': a.own_card()})
    assert enrolled.status_code == 201, enrolled.text
    a_on_b = enrolled.json()
    b_on_a = a.enroll(b_card)
    # A faxes B a code; B's operator confirms it, proving B receives at that number with its key.
    a.store.start_challenge(b_on_a['id'], code='48291374', job_id=None)
    confirmed = b_client.post(f"/direct/peers/{a_on_b['id']}/confirm", headers=ADMIN, json={'code': '4829 1374'})
    assert confirmed.json() == {'confirmed': True, 'detail': 'The partner confirmed the code.'}
    assert a.store.get_peer(b_on_a['id'])['state'] == 'verified'
    delivery = OutboundStore(configuration)
    return {'a': a, 'to_b': to_b, 'delivery': delivery, 'configuration': configuration, 'snapshot': snapshot,
            'data': data, 'routes': RouteStore(engine), 'a_on_b': a_on_b, 'b_on_a': b_on_a, 'b_client': b_client}


def accept(pair, document):
    job = uuid4().hex
    (pair['data'] / (job + '.pdf')).write_bytes(document)
    now = datetime.utcnow()
    pair['configuration'].accept_outbound(pair['snapshot'].active, {'id': job, 'to_number': B_NUMBER,
        'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued', 'pages': 1,
        'created_at': now, 'updated_at': now})
    return job


def transport(pair):
    conventional = Conventional(pair['delivery'])
    return RoutedTransport(conventional, direct=DirectRoute(pair['a'])), conventional


def b_items(client):
    return client.get('/intake/items', headers=ADMIN).json()['items']


def stored_document(client):
    engine = main.app.state.configuration_runtime.manager.store.engine
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT document_path FROM direct_deliveries WHERE direction='inbound' AND document_path IS NOT NULL")).all()
    return [open(row[0], 'rb').read() for row in rows]


@pytest.mark.asyncio
async def test_document_is_delivered_directly_and_byte_for_byte(pair):
    original = pdf_bytes('Referral for direct delivery')
    job = accept(pair, original)
    routed, conventional = transport(pair)
    assert await OutboundWorker(pair['delivery'], routed).step() is True
    assert pair['delivery'].get(job)['state'] == 'success' and conventional.submissions == 0
    attempt = pair['delivery'].get(job)['attempt_id']
    assert pair['routes'].decision(attempt)['route'] == 'direct'
    sent = pair['a'].store.find('outbound', attempt)
    assert sent['state'] == 'accepted' and sent['digest'] == hashlib.sha256(original).hexdigest()
    (item,) = b_items(pair['b_client'])
    assert item['source'] == 'direct' and item['from_number'] == A_NUMBER and item['to_number'] == B_NUMBER
    assert stored_document(pair['b_client']) == [original]


@pytest.mark.asyncio
async def test_replay_is_idempotent_and_a_reused_id_is_refused(pair):
    a, client = pair['a'], pair['b_client']
    identity = a.identity()
    b_peer = pair['b_on_a']
    manifest, signature, ciphertext = seal(identity, message_id='c' * 32, organization='Valley Hospital',
        fax_number=A_NUMBER, recipient_number=B_NUMBER, recipient_signing_key=b_peer['signing_key'],
        recipient_exchange_key=b_peer['exchange_key'], document=pdf_bytes('First'), pages=1)

    def post(m, s, c):
        return client.post('/direct/deliveries', files={'manifest': (None, m), 'signature': (None, s),
                                                        'document': ('d', c, 'application/octet-stream')})
    first = post(manifest, signature, ciphertext)
    again = post(manifest, signature, ciphertext)
    assert first.status_code == again.status_code == 200 and first.json() == again.json()
    assert len(b_items(client)) == 1
    other, other_signature, other_ciphertext = seal(identity, message_id='c' * 32, organization='Valley Hospital',
        fax_number=A_NUMBER, recipient_number=B_NUMBER, recipient_signing_key=b_peer['signing_key'],
        recipient_exchange_key=b_peer['exchange_key'], document=pdf_bytes('Different'), pages=1)
    reused = post(other, other_signature, other_ciphertext)
    assert reused.status_code == 409 and '"reason":"replay"' in reused.json()['statement']
    assert len(b_items(client)) == 1


@pytest.mark.asyncio
async def test_tampered_wrong_recipient_and_unknown_sender_are_refused(pair):
    a, client = pair['a'], pair['b_client']
    identity, b_peer = a.identity(), pair['b_on_a']

    def post(m, s, c):
        return client.post('/direct/deliveries', files={'manifest': (None, m), 'signature': (None, s),
                                                        'document': ('d', c, 'application/octet-stream')})

    def make(sender=identity, number=B_NUMBER, message=None):
        return seal(sender, message_id=message or uuid4().hex, organization='Valley Hospital', fax_number=A_NUMBER,
                    recipient_number=number, recipient_signing_key=b_peer['signing_key'],
                    recipient_exchange_key=b_peer['exchange_key'], document=pdf_bytes('Tamper test'), pages=1)
    manifest, signature, ciphertext = make()
    flipped = bytearray(ciphertext)
    flipped[5] ^= 1
    tampered = post(manifest, signature, bytes(flipped))
    assert tampered.status_code == 400 and '"reason":"tampered"' in tampered.json()['statement']
    wrong = post(*make(number='+15550100009'))
    assert wrong.status_code == 403 and '"reason":"wrong_recipient"' in wrong.json()['statement']
    stranger = post(*make(sender=Identity.generate()))
    assert stranger.status_code == 403 and '"reason":"unknown_sender"' in stranger.json()['statement']
    forged_manifest, _, forged_ciphertext = make()
    forged = post(forged_manifest, Identity.generate().sign(forged_manifest), forged_ciphertext)
    assert forged.status_code == 403
    assert b_items(client) == [] and stored_document(client) == []


@pytest.mark.asyncio
async def test_lost_receipt_is_reconciled_by_asking_never_by_resending(pair):
    original = pdf_bytes('Answer will be lost')
    job = accept(pair, original)
    routed, conventional = transport(pair)
    pair['to_b'].mode = 'lose_answer'
    await OutboundWorker(pair['delivery'], routed).step()
    assert pair['delivery'].get(job)['state'] == 'reconciliation_required'
    assert pair['to_b'].posts == 1 and len(b_items(pair['b_client'])) == 1
    assert await OutboundWorker(pair['delivery'], routed).step() is False
    pair['to_b'].mode = 'normal'
    reconciler = DirectReconciler(pair['a'], pair['delivery'])
    assert await reconciler.reconcile(pair['a'].store.awaiting_partner()[0]) == 'accepted'
    assert pair['delivery'].get(job)['state'] == 'success'
    assert pair['to_b'].posts == 1 and conventional.submissions == 0 and len(b_items(pair['b_client'])) == 1


@pytest.mark.asyncio
async def test_signed_not_received_falls_back_to_fax_under_the_same_job(pair):
    job = accept(pair, pdf_bytes('Request will be lost'))
    routed, conventional = transport(pair)
    pair['to_b'].mode = 'drop'
    await OutboundWorker(pair['delivery'], routed).step()
    assert pair['delivery'].get(job)['state'] == 'reconciliation_required'
    pair['to_b'].mode = 'normal'
    assert await DirectReconciler(pair['a'], pair['delivery']).reconcile(
        pair['a'].store.awaiting_partner()[0]) == 'not_received'
    assert pair['delivery'].get(job)['state'] == 'ready'
    await OutboundWorker(pair['delivery'], routed).step()
    row = pair['delivery'].get(job)
    assert row['state'] == 'in_progress' and conventional.submissions == 1
    assert pair['routes'].decision(row['attempt_id'])['route'] == 'phaxio'
    assert b_items(pair['b_client']) == []
    # The upload that was presumed lost arrives late; the partner's answer fenced it.
    method, path, kwargs = pair['to_b'].dropped
    late = pair['b_client'].request(method, path, **kwargs)
    assert late.status_code == 409 and '"reason":"withdrawn"' in late.json()['statement']
    assert b_items(pair['b_client']) == [] and stored_document(pair['b_client']) == []


@pytest.mark.asyncio
async def test_partner_refusal_falls_back_to_fax_in_the_same_attempt(pair):
    revoked = pair['b_client'].post(f"/direct/peers/{pair['a_on_b']['id']}/revoke", headers=ADMIN)
    assert revoked.json()['state'] == 'revoked'
    job = accept(pair, pdf_bytes('Partner no longer accepts'))
    routed, conventional = transport(pair)
    await OutboundWorker(pair['delivery'], routed).step()
    row = pair['delivery'].get(job)
    assert row['state'] == 'in_progress' and conventional.submissions == 1
    assert pair['routes'].decision(row['attempt_id'])['route'] == 'phaxio'
    assert pair['a'].store.find('outbound', row['attempt_id'])['state'] == 'refused'


def test_partner_routes_are_signature_authenticated_and_hidden_when_disabled(pair, monkeypatch):
    client = pair['b_client']
    assert client.get('/direct/deliveries/' + 'd' * 32).status_code == 403
    bad = client.post('/direct/verifications', json={'statement': '{}', 'signature': 'x'})
    assert bad.status_code == 400 and bad.json()['verified'] is False
    peers = client.get('/direct/peers', headers=ADMIN).json()['peers']
    assert [peer['organization'] for peer in peers] == ['Valley Hospital']
    assert peers[0]['status'].endswith('.')


def test_delivery_endpoint_rejects_malformed_bodies_before_any_work(pair):
    client = pair['b_client']
    assert client.post('/direct/deliveries', data={'manifest': '{}'}).status_code == 400
    too_many = client.post('/direct/deliveries', data={'manifest': '{}', 'signature': 'x', 'extra': 'y'},
                           files={'document': ('d', b'x', 'application/octet-stream')})
    assert too_many.status_code == 400
    assert b_items(client) == []


def test_challenge_fax_carries_a_code_and_is_an_ordinary_fax(pair):
    from io import BytesIO
    from pypdf import PdfReader
    client, peer = pair['b_client'], pair['a_on_b']
    response = client.post(f"/direct/peers/{peer['id']}/challenge", headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['code_sent'] is True and body['status'].endswith('.')
    job = client.get(f"/fax/{body['fax_id']}", headers=ADMIN)
    assert job.status_code == 200 and job.json()['pages'] == 1
    document = client.get(f"/admin/fax-jobs/{body['fax_id']}/pdf", headers=ADMIN)
    text = PdfReader(BytesIO(document.content)).pages[0].extract_text()
    assert 'County Clinic would like to send documents directly to this fax number.' in text
    assert any(part.isdigit() and len(part) == 4 for part in text.split())


def test_direct_administration_requires_settings_permissions(pair):
    from api.tests.test_routing_http import scoped_key
    client = pair['b_client']
    sender = scoped_key(client, ['fax:send'])
    for method, path, body in [('GET', '/direct/card', None), ('GET', '/direct/peers', None),
                               ('POST', '/direct/peers', {'card': {}}), ('GET', '/direct/deliveries', None),
                               ('POST', '/direct/peers/x/challenge', None),
                               ('POST', '/direct/peers/x/confirm', {'code': '12345678'}),
                               ('POST', '/direct/peers/x/revoke', None)]:
        assert client.request(method, path, json=body, headers=sender).status_code == 403, (method, path)


def test_uk_installation_card_and_partner_lookup_use_e164(b_client, tmp_path):
    # The UK installation entered its fax number nationally; its card carries E.164.
    from api.app.schema import create_database_engine
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'uk.db'))
    upgrade_schema(engine)
    values = ConfigurationValues.from_environment({
        'FAX_DEFAULT_COUNTRY': 'GB', 'PUBLIC_API_URL': 'https://uk.example', 'DIRECT_DELIVERY_ENABLED': 'true',
        'DIRECT_ORGANIZATION': 'Stoke Clinic'}).with_patch({'direct_fax_number': '01782 684953'})
    assert values.direct_fax_number == '+441782684953'
    uk = DirectService(engine, values=lambda: values, environment={'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'uk.key')})
    uk_card = uk.own_card()
    assert uk_card['fax_number'] == '+441782684953'
    # The partner installation enrolls that card and finds it by the canonical number.
    enrolled = b_client.post('/direct/peers', headers=ADMIN, json={'card': uk_card})
    assert enrolled.status_code == 201, enrolled.text
    view = b_client.get('/routing/destinations/+44 1782 684953', headers=ADMIN)
    assert view.status_code == 200, view.text
    assert view.json()['number'] == '+441782684953'


# -- partner addresses (SSRF guard) ------------------------------------------------------------------

from api.app.direct import addresses as partner_addresses  # noqa: E402
from api.app.direct import service as direct_service  # noqa: E402

PRIVATE_SENTENCE = ("The partner's address points to a private or local network, which direct delivery refuses "
                    'unless DIRECT_ALLOW_PRIVATE_PEERS is turned on.')


@pytest.mark.parametrize('address, allowed', [
    ('127.0.0.1', False), ('::1', False), ('10.1.2.3', False), ('172.16.0.1', False), ('192.168.1.1', False),
    ('169.254.169.254', False), ('fe80::1', False), ('fc00::1', False), ('fd12:3456::1', False),
    ('224.0.0.1', False), ('ff02::1', False), ('0.0.0.0', False), ('::', False), ('::ffff:127.0.0.1', False),
    ('::ffff:10.0.0.1', False), ('100.64.0.1', False),
    ('1.1.1.1', True), ('8.8.8.8', True), ('2606:4700:4700::1111', True), ('::ffff:8.8.8.8', True),
])
def test_only_public_internet_addresses_are_partner_addresses(address, allowed):
    assert partner_addresses.public(address) is allowed


def _guarded_service(tmp_path, *, allow_private, answers):
    from api.app.schema import create_database_engine
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'guard.db'))
    upgrade_schema(engine)
    settings = {'value': ConfigurationValues.from_environment({
        'PUBLIC_API_URL': 'https://own.example', 'DIRECT_DELIVERY_ENABLED': 'true',
        'DIRECT_ORGANIZATION': 'County Clinic', 'DIRECT_FAX_NUMBER': A_NUMBER,
        'DIRECT_ALLOW_PRIVATE_PEERS': 'true' if allow_private else 'false'})}
    lookups = []

    def resolver(host, port):
        """Synthetic DNS: IP literals resolve to themselves, unknown names are not found."""
        import ipaddress
        import socket
        lookups.append((host, port))
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        if host not in answers:
            raise socket.gaierror('synthetic name not found')
        return answers[host]
    service = DirectService(engine, values=lambda: settings['value'], resolver=resolver,
                            environment={'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'guard.key')})
    return service, settings, lookups


def _partner_card(endpoint):
    return card(Identity.generate(), organization='Valley Hospital', fax_number=B_NUMBER, endpoint=endpoint)


def test_enrollment_refuses_partners_on_private_or_unknown_addresses(tmp_path):
    service, _, lookups = _guarded_service(tmp_path, allow_private=False, answers={
        'metadata.example': ['169.254.169.254'], 'mixed.example': ['93.184.215.14', '10.0.0.7'],
        'valley.example': ['93.184.215.14', '2606:2800:21f:cb07:6820:80da:af6b:8b2c']})
    for endpoint in ('https://metadata.example', 'https://mixed.example', 'https://127.0.0.1', 'https://[::1]:8443'):
        with pytest.raises(direct_service.DirectConflict) as refused:
            service.enroll(_partner_card(endpoint))
        assert str(refused.value) == PRIVATE_SENTENCE
    with pytest.raises(direct_service.DirectConflict) as unknown:
        service.enroll(_partner_card('https://nowhere.example'))
    assert str(unknown.value) == "Faxbot could not find the partner's address; check the card and try again."
    assert service.store.list_peers() == []
    enrolled = service.enroll(_partner_card('https://valley.example'))
    assert enrolled['endpoint_url'] == 'https://valley.example' and ('valley.example', 443) in lookups


def test_private_partners_are_allowed_only_when_the_setting_is_on(tmp_path):
    service, _, lookups = _guarded_service(tmp_path, allow_private=True, answers={})
    enrolled = service.enroll(_partner_card('https://10.0.0.7'))
    assert enrolled['endpoint_url'] == 'https://10.0.0.7' and lookups == []


class _Capture:
    """Stands in for httpx.AsyncClient and records what would have been sent."""
    requests = []

    def __init__(self, **options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, **kwargs):
        type(self).requests.append((method, url, kwargs))
        return httpx.Response(200, json={'verified': True})


def test_requests_go_to_the_checked_address_with_the_partner_host_name(tmp_path, monkeypatch):
    _Capture.requests = []
    monkeypatch.setattr(direct_service.httpx, 'AsyncClient', _Capture)
    service, settings, _ = _guarded_service(tmp_path, allow_private=False, answers={
        'valley.example': ['93.184.215.14'], 'v6.example': ['2606:2800:21f:cb07:6820:80da:af6b:8b2c']})
    status, body = asyncio.run(service.http.request('POST', 'https://valley.example/direct/verifications',
                                                    json={'statement': 's'}))
    assert (status, body) == (200, {'verified': True})
    method, url, options = _Capture.requests[-1]
    assert (method, url) == ('POST', 'https://93.184.215.14/direct/verifications')
    assert options['headers']['Host'] == 'valley.example'
    assert options['extensions'] == {'sni_hostname': 'valley.example'} and options['json'] == {'statement': 's'}
    asyncio.run(service.http.request('GET', 'https://v6.example:8443/direct/deliveries/m1', headers={'X-A': '1'}))
    _, url, options = _Capture.requests[-1]
    assert url == 'https://[2606:2800:21f:cb07:6820:80da:af6b:8b2c]:8443/direct/deliveries/m1'
    assert options['headers'] == {'X-A': '1', 'Host': 'v6.example:8443'}


def test_a_partner_whose_address_turns_private_is_refused_before_anything_is_sent(tmp_path, monkeypatch):
    _Capture.requests = []
    monkeypatch.setattr(direct_service.httpx, 'AsyncClient', _Capture)
    answers = {'valley.example': ['93.184.215.14']}
    service, _, _ = _guarded_service(tmp_path, allow_private=False, answers=answers)
    peer = service.enroll(_partner_card('https://valley.example'))
    answers['valley.example'] = ['10.0.0.7']  # the partner's DNS now points inside our network
    with pytest.raises(direct_service.DirectConflict) as refused:
        asyncio.run(service.send_confirmation(peer['id'], '1234 5678'))
    assert str(refused.value) == PRIVATE_SENTENCE
    with pytest.raises(direct_service.PartnerAddressRefused):
        asyncio.run(service.http.request('GET', 'https://valley.example/direct/deliveries/m1'))
    assert _Capture.requests == []


def test_allowing_private_partners_keeps_the_original_address(tmp_path, monkeypatch):
    _Capture.requests = []
    monkeypatch.setattr(direct_service.httpx, 'AsyncClient', _Capture)
    service, _, lookups = _guarded_service(tmp_path, allow_private=True, answers={})
    asyncio.run(service.http.request('POST', 'https://partner.lan/direct/verifications', json={}))
    assert _Capture.requests[-1][1] == 'https://partner.lan/direct/verifications' and lookups == []
