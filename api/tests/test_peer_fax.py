"""Peer fax (M1a): the exact fax image goes to an enrolled Faxbot partner, and every direct arrival is a received fax.

Two installations exchange documents over the real HTTP stack, on SQLite and on
PostgreSQL: installation B is the running application (TestClient) and
installation A is a durable delivery store whose direct route posts to B. Every
document, number and partner is synthetic; no provider is contacted.
"""
import asyncio
from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from api.app.config_profiles import ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.direct import faximage, http as direct_http
from api.app.direct.crypto import capabilities, seal, signed, timestamp
from api.app.direct.service import DirectReconciler, DirectRoute, DirectService
from api.app.outbound_store import OutboundStore
from api.app.outbound_worker import OutboundWorker
from api.app.routing.savings import savings as count_savings
from api.app.routing.store import RouteStore
from api.app.routing.transport import RoutedTransport
from api.app.schema import create_database_engine, upgrade_schema
from api.tests.test_direct_delivery import A_NUMBER, B_NUMBER, Conventional, ToB, pdf_bytes
from api.tests.test_routing_http import ADMIN, BOOTSTRAP


IMAGE_LABEL = 'Delivered directly as a fax image by Valley Hospital; no telephone call.'
ORIGINAL_LABEL = 'Delivered directly by Valley Hospital as the original document; no telephone call.'


class ToA:
    """B's transport to installation A, answered in-process.

    ``drop`` loses B's statements about fax images; ``older`` answers them as a Faxbot without fax images does (404).
    """

    def __init__(self, service):
        self.service = service
        self.drop = self.older = False

    async def request(self, method, url, **kwargs):
        body = kwargs['json']
        if method == 'POST' and url == 'https://a.example/direct/verifications':
            return await asyncio.to_thread(self.service.confirm, body['statement'], body['signature'])
        assert method == 'POST' and url == 'https://a.example/direct/capabilities'
        if self.drop:
            raise httpx.ConnectTimeout('partner offline')
        if self.older:
            return 404, {'detail': 'Not Found'}
        return await asyncio.to_thread(self.service.note, body['statement'], body['signature'])


def _namespaces(request, count):
    """Database URLs for ``count`` installations: SQLite files, or PostgreSQL schemas dropped afterwards."""
    if request.param == 'sqlite':
        return None
    url = os.environ.get('FAXBOT_SCHEMA_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set FAXBOT_SCHEMA_TEST_POSTGRES_URL to run the PostgreSQL peer fax tests')
    admin = create_database_engine(url)
    names = ['faxbot_peer_fax_' + uuid4().hex for _ in range(count)]
    with admin.begin() as connection:
        for name in names:
            connection.exec_driver_sql(f'CREATE SCHEMA {name}')
    request.addfinalizer(lambda: _drop(admin, names))
    return [sa.engine.make_url(url).update_query_dict({'options': '-csearch_path=' + name}) for name in names]


def _drop(admin, names):
    with admin.begin() as connection:
        for name in names:
            connection.exec_driver_sql(f'DROP SCHEMA {name} CASCADE')
    admin.dispose()


@pytest.fixture(params=['sqlite', 'postgresql'])
def peer_pair(request, isolated_installation, monkeypatch, tmp_path):
    scoped = _namespaces(request, 2)
    b_url = scoped[1].render_as_string(hide_password=False) if scoped else None
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0', 'DIRECT_DELIVERY_ENABLED': 'true',
                        'INBOUND_ENABLED': 'true',
                        'DIRECT_ORGANIZATION': 'County Clinic', 'DIRECT_FAX_NUMBER': B_NUMBER,
                        'DIRECT_ALLOW_PRIVATE_PEERS': 'true', 'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'b-direct.key'),
                        'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', **({'DATABASE_URL': b_url} if b_url else {})}.items():
        monkeypatch.setenv(name, value)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        if scoped:
            engine = create_database_engine(scoped[0].render_as_string(hide_password=False))
        else:
            engine = create_database_engine('sqlite:///' + str(tmp_path / 'a.db'))
        upgrade_schema(engine)
        data = tmp_path / 'a-data'
        data.mkdir()
        configuration = ConfigurationStore(engine, tmp_path / 'a-installation.key')
        values = ConfigurationValues.from_environment({
            'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(data),
            'PUBLIC_API_URL': 'https://a.example', 'DIRECT_DELIVERY_ENABLED': 'true', 'FAX_HEADER': 'Valley Hospital',
            'DIRECT_ORGANIZATION': 'Valley Hospital', 'DIRECT_FAX_NUMBER': A_NUMBER, 'DIRECT_ALLOW_PRIVATE_PEERS': 'true'})
        phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'k', 'api_secret': 's'})
        snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
        to_b = ToB(client)
        a = DirectService(engine, values=lambda: values, environment={'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'a.key')},
                          http=to_b)
        to_a = ToA(a)
        main.app.state.direct_http = to_a
        b_card = client.get('/direct/card', headers=ADMIN).json()['card']
        enrolled = client.post('/direct/peers', headers=ADMIN, json={'card': a.own_card()})
        assert enrolled.status_code == 201, enrolled.text
        a_on_b = enrolled.json()
        b_on_a = a.enroll(b_card)
        a.store.start_challenge(b_on_a['id'], code='48291374', job_id=None)
        confirmed = client.post(f"/direct/peers/{a_on_b['id']}/confirm", headers=ADMIN, json={'code': '4829 1374'})
        assert confirmed.json()['confirmed'] is True
        try:
            yield {'a': a, 'to_a': to_a, 'to_b': to_b, 'delivery': OutboundStore(configuration),
                   'configuration': configuration, 'snapshot': snapshot, 'data': data, 'routes': RouteStore(engine),
                   'a_on_b': a_on_b, 'b_on_a': b_on_a, 'b_client': client, 'values': values,
                   'b_engine': main.app.state.configuration_runtime.manager.store.engine}
        finally:
            main.app.state.direct_http = None
            engine.dispose()


def engine_image(pages, width=1734):
    """A synthetic engine image as ``pdf_to_tiff`` makes one: Group 4 at 204 by 196 dots per inch."""
    from PIL import Image, ImageDraw
    frames = []
    for number in range(pages):
        frame = Image.new('1', (width, 400), 1)
        ImageDraw.Draw(frame).text((120, 120 + 20 * number), f'Synthetic referral, page {number + 1}', fill=0)
        frames.append(frame)
    output = io.BytesIO()
    frames[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=frames[1:], dpi=(204, 196))
    return output.getvalue()


def accept(pair, *, pages=1, image=True):
    job = uuid4().hex
    (pair['data'] / (job + '.pdf')).write_bytes(pdf_bytes('Synthetic referral'))
    if image:
        (pair['data'] / (job + '.tiff')).write_bytes(engine_image(pages))
    now = datetime.utcnow()
    pair['configuration'].accept_outbound(pair['snapshot'].active, {
        'id': job, 'to_number': B_NUMBER, 'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued',
        'pages': pages, 'created_at': now, 'updated_at': now})
    return job


def transport(pair):
    conventional = Conventional(pair['delivery'])
    return RoutedTransport(conventional, direct=DirectRoute(pair['a'])), conventional


def opt_in(pair, accept=True):
    response = pair['b_client'].post(f"/direct/peers/{pair['a_on_b']['id']}/fax-images", headers=ADMIN,
                                     json={'accept': accept})
    assert response.status_code == 200, response.text
    return response.json()


def mailbox(client, label, number):
    version = lambda: client.get('/auth/me', headers=ADMIN).json()['policy_version']  # noqa: E731
    created = client.post('/access/mailboxes', headers=ADMIN, json={'label': label, 'enabled': True,
                                                                     'expected_policy_version': version()})
    assert created.status_code in (200, 201), created.text
    rule = client.post('/access/inbound-rules', headers=ADMIN, json={
        'to_number': number, 'mailbox_id': created.json()['mailbox']['id'], 'expected_policy_version': version()})
    assert rule.status_code in (200, 201), rule.text


def rows(engine, sql, **params):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(sql), params).mappings()]


def received(pair):
    return [item for item in pair['b_client'].get('/inbound', headers=ADMIN).json() if item['backend'] == 'direct']


def sha(data):
    return hashlib.sha256(data).hexdigest()


async def send(pair, job):
    routed, conventional = transport(pair)
    assert await OutboundWorker(pair['delivery'], routed).step() is True
    return pair['delivery'].get(job), conventional


@pytest.mark.asyncio
async def test_a_fax_image_arrives_byte_for_byte_as_a_received_fax_with_work_email_and_its_mailbox(peer_pair):
    pair, client = peer_pair, peer_pair['b_client']
    mailbox(client, 'Referrals', B_NUMBER)
    chosen = opt_in(pair)
    assert chosen['receive_fax_images'] is True and chosen['partner_told'] is True
    assert chosen['detail'] == 'Valley Hospital now sends you faxes as the exact fax image.'
    assert pair['a'].store.get_peer(pair['b_on_a']['id'])['partner_receives_fax_images'] == 1

    job = accept(pair, pages=2)
    row, conventional = await send(pair, job)
    assert row['state'] == 'success' and conventional.submissions == 0
    assert pair['routes'].decision(row['attempt_id'])['route'] == 'direct'
    sent = pair['a'].store.find('outbound', row['attempt_id'])
    assert (sent['state'], sent['kind']) == ('accepted', 'fax_image')
    manifest = json.loads(sent['manifest'])
    assert manifest['document']['pages'] == 2 and manifest['fax']['resolution'] == 'fine'
    assert manifest['fax']['width'] == 1728 and 'Valley Hospital' in manifest['fax']['header_line']
    # The engine image is untouched; the fax image is a separate, derived file.
    assert (pair['data'] / (job + '.tiff')).read_bytes() == engine_image(2)

    # B kept exactly the bytes A built: its ledger, its stored image and the received fax's image.
    (arrival,) = rows(pair['b_engine'], "SELECT * FROM direct_deliveries WHERE direction = 'inbound'")
    assert (arrival['kind'], arrival['digest']) == ('fax_image', sent['digest'])
    assert sha(Path(arrival['document_path']).read_bytes()) == sent['digest']
    (fax,) = received(pair)
    assert fax['status_text'] == IMAGE_LABEL and fax['mailbox'] == 'Referrals' and fax['pages'] == 2
    (stored,) = rows(pair['b_engine'], 'SELECT tiff_path, pdf_path FROM inbound_faxes WHERE id = :id', id=fax['id'])
    assert sha(Path(stored['tiff_path']).read_bytes()) == sent['digest']
    assert Path(stored['pdf_path']).read_bytes().startswith(b'%PDF')

    # Work gives it an owner and a deadline like any fax; its history says how it arrived.
    from app.work.store import WorkStore
    WorkStore(pair['b_engine']).feed()
    (item,) = [item for item in client.get('/work', headers=ADMIN).json()['items'] if item['inbound_fax_id'] == fax['id']]
    assert item['mailbox'] == 'Referrals'
    history = client.get(f"/work/{item['id']}/history", headers=ADMIN).json()['events']
    assert history[0]['text'] == IMAGE_LABEL + ' It arrived in Referrals.'

    # Exactly one email item, named as a direct delivery; the inbound feed adds none.
    from app.intake.store import IntakeStore
    assert IntakeStore(pair['b_engine'], None).feed_inbound() == 0
    (email,) = client.get('/intake/items', headers=ADMIN).json()['items']
    assert (email['source'], email['inbound_fax_id']) == ('direct', fax['id'])

    # Neither side ever calls it "faxed".
    a_row = {**sent, 'organization': 'County Clinic'}
    labels = [direct_http.delivery_text(a_row), fax['status_text'], history[0]['text'],
              *[item['status'] for item in client.get('/direct/deliveries', headers=ADMIN).json()['deliveries']]]
    assert direct_http.delivery_text(a_row) == 'Delivered directly as a fax image to County Clinic; no telephone call.'

    # Savings count the telephone call the fax image avoided once, as a fax image and not as an original.
    found = count_savings(pair['routes'], pair['routes'].engine)
    assert found['direct_fax_images']['calls_avoided'] == 1 and found['direct_delivery']['faxes'] == 0
    assert found['direct_fax_images']['sentence'].startswith('1 telephone call avoided by direct fax images')
    assert all('faxed' not in label.lower() for label in labels), labels


@pytest.mark.asyncio
async def test_partners_learn_fax_images_are_on_by_default_without_anyone_doing_anything(peer_pair):
    pair, client = peer_pair, peer_pair['b_client']
    (peer,) = client.get('/direct/peers', headers=ADMIN).json()['peers']
    assert peer['receive_fax_images'] is True
    assert peer['fax_images_text'] == 'Their faxes to you arrive as the exact fax image and are filed like any received fax.'
    # Until B says so, signed, A sends originals; B's tell step (after an upgrade or an enrollment) says so.
    assert pair['a'].store.get_peer(pair['b_on_a']['id'])['partner_receives_fax_images'] is None
    service = direct_http.service_for(main.app)
    await service.tell_partners()
    assert pair['a'].store.get_peer(pair['b_on_a']['id'])['partner_receives_fax_images'] == 1
    assert service.store.untold() == []
    job = accept(pair)
    row, conventional = await send(pair, job)
    assert pair['a'].store.find('outbound', row['attempt_id'])['kind'] == 'fax_image'
    assert [fax['status_text'] for fax in received(pair)] == [IMAGE_LABEL]


@pytest.mark.asyncio
async def test_a_partner_that_turned_fax_images_off_gets_the_original_directly_and_it_still_gets_an_owner(peer_pair):
    pair, client = peer_pair, peer_pair['b_client']
    stopped = opt_in(pair, accept=False)
    assert stopped['receive_fax_images'] is False and stopped['partner_told'] is True
    assert stopped['detail'] == 'Valley Hospital now sends you the original documents.'
    job = accept(pair)
    row, conventional = await send(pair, job)
    assert row['state'] == 'success' and conventional.submissions == 0
    sent = pair['a'].store.find('outbound', row['attempt_id'])
    assert sent['kind'] is None
    found = count_savings(pair['routes'], pair['routes'].engine)
    assert found['direct_delivery']['calls_avoided'] == 1 and found['direct_fax_images']['faxes'] == 0
    original = (pair['data'] / (job + '.pdf')).read_bytes()
    assert sent['digest'] == sha(original)
    (fax,) = received(pair)
    assert fax['status_text'] == ORIGINAL_LABEL
    (stored,) = rows(pair['b_engine'], 'SELECT tiff_path, pdf_path, sha256 FROM inbound_faxes WHERE id = :id',
                     id=fax['id'])
    assert stored['tiff_path'] is None and stored['sha256'] == sha(original)
    assert Path(stored['pdf_path']).read_bytes() == original
    # The app's own Work worker (faxbot-work-queue: 2 s after start, then every 5 s) may create the item before
    # this test does, so the test asserts the outcome, not which feeder made it. Forcing the worker first shows
    # the feeders agree: the worker's step makes the one item, and a later feed adds none.
    from app.work.store import WorkStore
    from app.work.worker import WorkWorker
    WorkWorker(WorkStore(pair['b_engine']), control=lambda: main.app.state.access_runtime.control,
               values=lambda: main.app.state.configuration_runtime.manager.store.read().active.values).step()
    assert WorkStore(pair['b_engine']).feed() == 0
    assert [item['inbound_fax_id'] for item in client.get('/work', headers=ADMIN).json()['items']] == [fax['id']]


@pytest.mark.asyncio
async def test_a_refused_fax_image_goes_by_fax_in_the_same_attempt_only_because_nothing_was_accepted(peer_pair):
    pair = peer_pair
    opt_in(pair)
    # B stops accepting fax images while A is unreachable, so A still believes it may send one.
    pair['to_a'].drop = True
    stopped = opt_in(pair, accept=False)
    assert stopped['partner_told'] is False and stopped['detail'].startswith('Saved. Faxbot could not reach Valley')
    assert pair['a'].store.get_peer(pair['b_on_a']['id'])['partner_receives_fax_images'] == 1
    job = accept(pair)
    row, conventional = await send(pair, job)
    assert row['state'] == 'in_progress' and conventional.submissions == 1
    assert pair['routes'].decision(row['attempt_id'])['route'] == 'phaxio'
    assert pair['a'].store.find('outbound', row['attempt_id'])['state'] == 'refused'
    assert pair['to_b'].posts == 1
    # B's signed refusal told A, so the next fax goes as the original.
    assert pair['a'].store.get_peer(pair['b_on_a']['id'])['partner_receives_fax_images'] is None
    assert received(pair) == [] and rows(pair['b_engine'], 'SELECT id FROM direct_deliveries') == []


@pytest.mark.asyncio
async def test_a_fax_with_encoded_pages_reaches_a_partner_as_the_fax_image_of_its_original(peer_pair, monkeypatch):
    """Encoded pages (experimental, codec/send.py) may replace a fax's engine image; a partner never gets them."""
    import shutil
    from api.app import conversion
    from api.app.codec.store import record_send
    from api.app.direct import service as direct_service
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript renders the document')
    pair = peer_pair
    opt_in(pair)
    signed_at = timestamp()
    monkeypatch.setattr(direct_service, 'timestamp', lambda: signed_at)  # one header time for both faxes
    digests = {}
    for encoded in (False, True):
        job = accept(pair, image=False)
        conversion.pdf_to_tiff(str(pair['data'] / (job + '.pdf')), str(pair['data'] / (job + '.tiff')))
        if encoded:
            # Acceptance wrote the encoded pages over the engine image and recorded the send.
            (pair['data'] / (job + '.tiff')).write_bytes(engine_image(1))
            engine = pair['a'].store.engine
            with engine.begin() as connection:
                record_send(connection, engine, job, {
                    'phone_number': B_NUMBER, 'provider_id': 'sip', 'layout': 'grid', 'resolution': 'fine',
                    'fec': 'medium', 'pages_original': 1, 'pages_encoded': 1, 'document_sha256': '0' * 64,
                    'encrypted': 0, 'format_version': 1}, datetime.utcnow())
        row, conventional = await send(pair, job)
        assert row['state'] == 'success' and conventional.submissions == 0
        sent = pair['a'].store.find('outbound', row['attempt_id'])
        assert sent['kind'] == 'fax_image'
        digests[encoded] = sent['digest']
    # The partner gets exactly the fax image a fax without encoded pages has.
    assert digests[True] == digests[False]


@pytest.mark.asyncio
async def test_a_lost_answer_for_a_fax_image_is_asked_about_never_sent_again(peer_pair):
    pair = peer_pair
    opt_in(pair)
    job = accept(pair)
    routed, conventional = transport(pair)
    pair['to_b'].mode = 'lose_answer'
    await OutboundWorker(pair['delivery'], routed).step()
    assert pair['delivery'].get(job)['state'] == 'reconciliation_required'
    pair['to_b'].mode = 'normal'
    assert await DirectReconciler(pair['a'], pair['delivery']).reconcile(pair['a'].store.awaiting_partner()[0]) == 'accepted'
    assert pair['delivery'].get(job)['state'] == 'success'
    assert pair['to_b'].posts == 1 and conventional.submissions == 0
    assert [fax['status_text'] for fax in received(pair)] == [IMAGE_LABEL]


@pytest.mark.asyncio
async def test_an_attempt_prepared_again_before_sending_builds_the_same_fax_image(peer_pair, monkeypatch):
    from types import SimpleNamespace
    from api.app.direct import service as direct_service
    pair = peer_pair
    opt_in(pair)
    job = accept(pair)
    claim = pair['delivery'].claim('synthetic-worker')
    plan = SimpleNamespace(peer=pair['b_on_a'])
    route = DirectRoute(pair['a'])
    monkeypatch.setattr(direct_service, 'timestamp', lambda: '2026-10-07T12:00:00Z')
    async with route.prepare(claim, plan, {'pages': 1}) as first:
        pass  # Faxbot stopped here, before anything was sent.
    # Prepared again five minutes later: a fresh header would print another time and change the bytes.
    monkeypatch.setattr(direct_service, 'timestamp', lambda: '2026-10-07T12:05:00Z')
    async with route.prepare(claim, plan, {'pages': 1}) as again:
        pass
    assert first.digest == again.digest == pair['a'].store.find('outbound', claim.attempt_id)['digest']
    assert job == claim.job_id and pair['to_b'].posts == 0


@pytest.mark.asyncio
async def test_an_arrival_accepted_just_before_a_crash_is_filed_once_by_the_filing_step(peer_pair, monkeypatch):
    pair = peer_pair
    opt_in(pair)
    from app.direct.filing import DirectFiling
    original = DirectFiling.file

    def crash(self, row):
        raise RuntimeError('Faxbot stopped before filing')
    monkeypatch.setattr(DirectFiling, 'file', crash)
    job = accept(pair)
    row, _ = await send(pair, job)
    assert row['state'] == 'success' and received(pair) == []
    service = direct_http.service_for(main.app)
    # Checked while filing still fails, so the app's own filing step (every 60 s) cannot file it first.
    assert [item['message_id'] for item in service.store.unfiled()] == [row['attempt_id']]
    monkeypatch.setattr(DirectFiling, 'file', original)
    service.filing.step()
    service.filing.step()
    assert [fax['status_text'] for fax in received(pair)] == [IMAGE_LABEL]
    assert service.store.unfiled() == []
    assert len(pair['b_client'].get('/intake/items', headers=ADMIN).json()['items']) == 1


def test_a_fax_image_that_does_not_match_its_signed_facts_is_refused_before_anything_is_kept(peer_pair):
    pair, client = peer_pair, peer_pair['b_client']
    opt_in(pair)
    a, b_peer = pair['a'], pair['b_on_a']
    data, facts = faximage.stamp(engine_image(1), header='Valley Hospital', station=A_NUMBER,
                                 moment=datetime.utcnow())

    def post(document, fax, pages=1):
        manifest, signature, ciphertext = seal(
            a.identity(), message_id=uuid4().hex, organization='Valley Hospital', fax_number=A_NUMBER,
            recipient_number=B_NUMBER, recipient_signing_key=b_peer['signing_key'],
            recipient_exchange_key=b_peer['exchange_key'], document=document, pages=pages, fax=fax)
        return client.post('/direct/deliveries', files={'manifest': (None, manifest), 'signature': (None, signature),
                                                        'document': ('d', ciphertext, 'application/octet-stream')})
    wider = post(data, {**facts, 'width': 2048})
    assert wider.status_code == 400 and '"reason":"not_fax_image"' in wider.json()['statement']
    pages = post(data, facts, pages=2)
    assert pages.status_code == 400 and '"reason":"not_fax_image"' in pages.json()['statement']
    not_tiff = post(pdf_bytes('A PDF called a fax image'), facts)
    assert not_tiff.status_code == 400 and '"reason":"not_fax_image"' in not_tiff.json()['statement']
    assert rows(pair['b_engine'], 'SELECT id FROM direct_deliveries') == [] and received(pair) == []
    assert post(data, facts).status_code == 200


def test_statements_about_fax_images_are_checked_and_an_older_one_never_wins(peer_pair):
    pair = peer_pair
    a, b_on_a = pair['a'], pair['b_on_a']
    service = direct_http.service_for(main.app)  # B signs with its own key
    b_key = service.identity()

    def statement(fax_images, said_at, recipient=None):
        return signed(b_key, {'type': 'capabilities', 'recipient': recipient or a.identity().signing_key,
                              'capabilities': capabilities(fax_images=fax_images, peer_calls=False, said_at=said_at)})
    now = datetime.utcnow().replace(microsecond=0)
    newer, older = statement(True, timestamp(now)), statement(False, timestamp(now - timedelta(minutes=5)))
    assert a.note(newer['statement'], newer['signature']) == (200, {'recorded': True})
    assert a.note(older['statement'], older['signature']) == (200, {'recorded': True})
    assert a.store.get_peer(b_on_a['id'])['partner_receives_fax_images'] == 1
    elsewhere = statement(False, timestamp(now + timedelta(minutes=1)), recipient=b_key.signing_key)
    stale = statement(False, timestamp(now - timedelta(days=2)))
    for refused in (elsewhere, stale, {**newer, 'signature': older['signature']}):
        assert a.note(refused['statement'], refused['signature'])[0] == 400
    assert a.store.get_peer(b_on_a['id'])['partner_receives_fax_images'] == 1


def test_fax_image_settings_need_settings_permission(peer_pair):
    from api.tests.test_routing_http import scoped_key
    client = peer_pair['b_client']
    sender = scoped_key(client, ['fax:send'])
    path = f"/direct/peers/{peer_pair['a_on_b']['id']}/fax-images"
    assert client.post(path, headers=sender, json={'accept': False}).status_code == 403
    assert client.get('/direct/peers', headers=ADMIN).json()['peers'][0]['receive_fax_images'] is True
    assert client.post('/direct/capabilities', json={'statement': '{}', 'signature': 'x'}).json()['recorded'] is False
    off = opt_in(peer_pair, accept=False)
    assert off['receive_fax_images'] is False and off['fax_images_text'] is None
    # A partner on a Faxbot without fax images answers 404; it is told honestly that originals keep arriving.
    peer_pair['to_a'].older = True
    older = opt_in(peer_pair, accept=False)
    assert older['partner_told'] is False and older['detail'] == (
        "Saved. Valley Hospital's Faxbot cannot send fax images yet, so their documents keep arriving as originals.")


# -- the fax image itself, and the route choice a rule can ask for ------------------------------------

def test_the_header_line_carries_what_68_318_d_requires_on_every_page_above_the_page():
    from PIL import Image
    moment = datetime(2026, 10, 7, 20, 5)  # 14:05 in Denver
    data, facts = faximage.stamp(engine_image(3), header='Valley Hospital', station=A_NUMBER, moment=moment,
                                 zone_name='America/Denver')
    assert facts == {'resolution': 'fine', 'x_dpi': 204, 'y_dpi': 196, 'width': 1728, 'compression': 'MMR',
                     'header_line': ' 7-Oct-2026   14:05   Valley Hospital   +15550100001   p.1'}
    assert faximage.check(data, facts, 3) == 3
    with Image.open(io.BytesIO(data)) as image:
        for page in range(3):
            image.seek(page)
            # The band is added above the page (nothing on the page is covered), and the width is the call's.
            assert image.size == (1728, faximage.BAND_ROWS_FINE + 400)
            band = image.crop((0, 0, 1728, faximage.BAND_ROWS_FINE)).convert('L')
            assert band.getextrema()[0] == 0  # the header line is printed
    assert faximage.header_line(moment, header='Valley Hospital', station=A_NUMBER, page=3,
                                zone_name='America/Denver').endswith('p.3')
    # The same image and time always give the same bytes, so both ends can prove them with one hash.
    again, _ = faximage.stamp(engine_image(3), header='Valley Hospital', station=A_NUMBER, moment=moment,
                              zone_name='America/Denver')
    assert sha(again) == sha(data)


def test_a_huge_fax_image_is_refused_from_its_page_sizes_before_any_page_is_decoded():
    from PIL import Image
    pages = [Image.new('1', (1728, 20000), 1) for _ in range(3)]  # blank: tiny in Group 4, 103 million pixels
    output = io.BytesIO()
    pages[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=pages[1:], dpi=(204, 196))
    facts = {'resolution': 'fine', 'x_dpi': 204, 'y_dpi': 196, 'width': 1728, 'compression': 'MMR',
             'header_line': 'Synthetic header'}
    assert len(output.getvalue()) < 100_000
    with pytest.raises(faximage.FaxImageInvalid):
        faximage.check(output.getvalue(), facts, 3)
    assert faximage.check(engine_image(2, width=1728), facts, 2) == 2


def test_a_fax_image_is_built_from_the_engine_image_or_the_same_conversion(tmp_path):
    from types import SimpleNamespace
    values = SimpleNamespace(fax_data_dir=str(tmp_path), fax_header='Valley Hospital', fax_station_id='',
                             direct_organization='Valley Hospital', direct_fax_number=A_NUMBER,
                             fax_default_country='US', time_zone='')
    job = uuid4().hex
    (tmp_path / (job + '.tiff')).write_bytes(engine_image(2, width=1687))  # A4 at 204 dots per inch
    image = faximage.build(values, job, moment=datetime(2026, 10, 7, 12, 0))
    assert image.pages == 2 and image.facts['width'] == 1728 and A_NUMBER in image.facts['header_line']
    with pytest.raises(faximage.FaxImageUnavailable):
        faximage.build(values, uuid4().hex)
    other = uuid4().hex
    (tmp_path / (other + '.pdf')).write_bytes(pdf_bytes('Rasterized here'))
    import shutil
    if shutil.which('gs') is None:
        with pytest.raises(faximage.FaxImageUnavailable):
            faximage.build(values, other)
    else:
        assert faximage.build(values, other).pages == 1
    assert not (tmp_path / (other + '.tiff')).exists()  # the derived image never replaces the fax's own files


def test_a_routing_rule_can_ask_for_peer_first_or_never_peer_and_a_peer_route_costs_nothing():
    now = datetime(2026, 10, 7, 12, 0)
    peer = {'state': 'verified', 'expires_at': now + timedelta(days=1), 'partner_receives_fax_images': 1}
    route = faximage.peer_route(peer, now=now)
    assert (route.kind, route.predicted_cost_micros, route.reason, route.sentence) == (
        'fax_image', 0, 'no_telephone_call', 'No telephone call.')
    assert faximage.peer_route({**peer, 'partner_receives_fax_images': None}, now=now).kind == 'original'
    assert faximage.peer_route(peer, preference=faximage.NEVER_PEER, now=now) is None
    assert faximage.peer_route({**peer, 'state': 'pending'}, now=now) is None
    assert faximage.peer_route({**peer, 'expires_at': now}, now=now) is None
    with pytest.raises(ValueError):
        faximage.peer_route(peer, preference='sometimes')


@pytest.mark.asyncio
async def test_never_peer_keeps_the_fax_off_direct_delivery_and_it_goes_by_fax(peer_pair):
    pair = peer_pair
    job = accept(pair)
    conventional = Conventional(pair['delivery'])
    routed = RoutedTransport(conventional, direct=DirectRoute(pair['a'], preference=faximage.NEVER_PEER))
    assert await OutboundWorker(pair['delivery'], routed).step() is True
    assert conventional.submissions == 1 and pair['to_b'].posts == 0
    assert pair['a'].store.find('outbound', pair['delivery'].get(job)['attempt_id']) is None


# -- M1b: a peer fax call only inside an encrypted tunnel ----------------------------------------------

def _calling_peer(**changes):
    return {'organization': 'Valley Hospital', 'state': 'verified', 'expires_at': None,
            'partner_receives_fax_images': None, 'partner_peer_calls': 1, 'peer_call_address': '10.20.0.2:5060',
            **changes}


def test_a_peer_fax_call_is_refused_without_an_encrypted_tunnel():
    from api.app.direct import peer_call
    no_tunnel = lambda address: None  # noqa: E731
    wireguard = lambda address: peer_call.Tunnel('wg0', 'wireguard')  # noqa: E731
    refused = peer_call.decide(_calling_peer(), tunnel_lookup=no_tunnel)
    assert (refused.applies, refused.reason) == (False, 'no_tunnel')
    assert refused.sentence == 'No encrypted tunnel reaches Valley Hospital, so Faxbot will not place a fax call to it.'
    with pytest.raises(peer_call.PeerCallRefused):
        peer_call.require_tunnel(_calling_peer(), tunnel_lookup=no_tunnel)
    # A public address is refused even when a tunnel would carry it: the call address must be inside the tunnel.
    public = peer_call.decide(_calling_peer(peer_call_address='93.184.215.14'), tunnel_lookup=wireguard)
    assert public.reason == 'public_address'
    for changes, reason in (({'partner_receives_fax_images': 1}, 'fax_image_first'),
                            ({'partner_peer_calls': None}, 'partner_cannot'),
                            ({'peer_call_address': 'partner.example'}, 'no_address'),
                            ({'state': 'pending'}, 'not_verified')):
        decision = peer_call.decide(_calling_peer(**changes), tunnel_lookup=wireguard)
        assert (decision.applies, decision.reason) == (False, reason)
    allowed = peer_call.require_tunnel(_calling_peer(peer_call_address='[fd00::2]:5070'), tunnel_lookup=wireguard)
    assert (allowed.address, allowed.port, allowed.tunnel.interface) == ('fd00::2', 5070, 'wg0')


def test_the_tunnel_check_reads_the_route_and_the_interface_type(tmp_path):
    from types import SimpleNamespace
    from api.app.direct import peer_call
    (tmp_path / 'wg0').mkdir()
    (tmp_path / 'wg0' / 'uevent').write_text('DEVTYPE=wireguard\nINTERFACE=wg0\nIFINDEX=7\n')
    (tmp_path / 'eth0').mkdir()
    (tmp_path / 'eth0' / 'uevent').write_text('INTERFACE=eth0\nIFINDEX=2\n')

    def routes(device):
        return lambda arguments, **options: SimpleNamespace(
            returncode=0, stdout=f'{arguments[-1]} dev {device} src 10.20.0.1 uid 0 \\    cache ')

    def lookup(device):
        return lambda address: peer_call.route_interface(address, run=routes(device))

    def kind(name):
        return peer_call.interface_kind(name, root=tmp_path)
    assert peer_call.tunnel_for('10.20.0.2', lookup=lookup('wg0'), kind_of=kind) == peer_call.Tunnel('wg0', 'wireguard')
    assert peer_call.tunnel_for('10.20.0.2', lookup=lookup('eth0'), kind_of=kind) is None
    assert peer_call.interface_kind('../etc', root=tmp_path) is None
    failed = lambda arguments, **options: SimpleNamespace(returncode=2, stdout='')  # noqa: E731
    assert peer_call.route_interface('10.20.0.2', run=failed) is None
