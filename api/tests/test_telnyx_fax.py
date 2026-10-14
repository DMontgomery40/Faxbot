"""Telnyx Fax API route: the adapter against a fake Telnyx, captured delivery, outcomes, pricing and route choice.

Every request goes to an in-process fake (httpx.MockTransport); nothing reaches Telnyx.
"""
import base64
from datetime import datetime, timedelta
import json
import time

import httpx
import pytest
import sqlalchemy as sa

from api.app import telnyx_fax_service as telnyx
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_polling import OutboundPoller
from api.app.outbound_transport import CapturedTransport, telnyx_callback_url
from api.app.outbound_worker import OutboundWorker, SubmissionReceipt
from api.app.telnyx_fax_service import TelnyxFaxError, TelnyxFaxRefused, TelnyxFaxService
from api.tests.test_outbound_store import installation, accept  # noqa: F401 (fixtures)
from api.tests.test_outbound_transport import Runtime
from api.tests.test_schema import database  # noqa: F401 (fixture)


SID = '3b1f0b8e-5c55-4d3a-9b7e-2a4f6c1d9e01'
APP = '1293384261075731499'
KEY = 'KEYsynthetic-telnyx-key'


def service(handler, **kwargs):
    return TelnyxFaxService(api_key=KEY, connection_id=APP, from_number='+13035550100',
                            transport=httpx.MockTransport(handler), **kwargs)


def fax(status, **fields):
    return {'data': {'record_type': 'fax', 'id': SID, 'direction': 'outbound', 'status': status,
                     'connection_id': APP, 'from': '+13035550100', 'to': '+12025550123', **fields}}


class FakeTelnyx:
    """Telnyx's media, fax and Fax Application endpoints, recording each request."""

    def __init__(self, *, media=None, create=None, status=None):
        self.requests = []
        self.media = media or (lambda request: httpx.Response(
            201, json={'data': {'media_name': 'faxbot-' + 'a' * 32, 'content_type': 'application/pdf'}}))
        self.create = create or (lambda request: httpx.Response(202, json=fax('queued')))
        self.status = status or (lambda request: httpx.Response(200, json=fax('delivered')))

    def __call__(self, request):
        self.requests.append(request)
        assert request.headers['Authorization'] == 'Bearer ' + KEY
        assert request.url.host == 'api.telnyx.com'
        if request.method == 'POST' and request.url.path == '/v2/media':
            return self.media(request)
        if request.method == 'POST' and request.url.path == '/v2/faxes':
            return self.create(request)
        if request.method == 'GET' and request.url.path == f'/v2/faxes/{SID}':
            return self.status(request)
        if request.method == 'POST' and request.url.path == '/v2/fax_applications':
            return httpx.Response(201, json={'data': {'id': APP, 'record_type': 'fax_application'}})
        return httpx.Response(404, json={'errors': [{'code': '10005'}]})


def pdf(tmp_path, content=b'%PDF-1.4 synthetic telnyx document'):
    path = tmp_path / 'fax.pdf'
    path.write_bytes(content)
    return str(path)


# -- the adapter ------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_uploads_the_document_then_names_it_in_one_fax(tmp_path):
    fake = FakeTelnyx()
    receipt = await service(fake, status_callback_url='https://faxbot.example.org/telnyx-fax-callback?job_id=j') \
        .send_fax_file('+12025550123', pdf(tmp_path), reference='a' * 32)
    assert receipt == {'provider_sid': SID, 'status': 'in_progress'}
    upload, create = fake.requests
    assert b'%PDF-1.4 synthetic telnyx document' in upload.content and b'faxbot-' + b'a' * 32 in upload.content
    body = json.loads(create.content)
    assert body == {'connection_id': APP, 'from': '+13035550100', 'to': '+12025550123',
                    'media_name': 'faxbot-' + 'a' * 32, 'client_state': base64.b64encode(b'a' * 32).decode(),
                    'webhook_url': 'https://faxbot.example.org/telnyx-fax-callback?job_id=j'}
    assert 'media_url' not in body  # no public link to the document is needed


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [
    lambda request: httpx.Response(500),
    lambda request: httpx.Response(201, json={'data': {}}),
    lambda request: (_ for _ in ()).throw(httpx.ReadTimeout('lost', request=request)),
])
async def test_any_upload_failure_is_definite_because_no_fax_exists_yet(tmp_path, answer):
    fake = FakeTelnyx(media=answer)
    with pytest.raises(TelnyxFaxRefused) as refused:
        await service(fake).send_fax_file('+12025550123', pdf(tmp_path), reference='b' * 32)
    assert refused.value.sentence in telnyx.FAILURE_SENTENCES
    assert [request.url.path for request in fake.requests] == ['/v2/media']


@pytest.mark.asyncio
async def test_documented_refusals_are_definite_and_everything_else_stays_uncertain(tmp_path):
    for code in (401, 403, 422, 429):
        fake = FakeTelnyx(create=lambda request, code=code: httpx.Response(code, json={'errors': [{'code': '1'}]}))
        with pytest.raises(TelnyxFaxRefused) as refused:
            await service(fake).send_fax_file('+12025550123', pdf(tmp_path), reference='c' * 32)
        assert refused.value.sentence == telnyx.REFUSED[code]

    def lost(request):
        raise httpx.ReadTimeout('lost after the request left', request=request)

    for answer in (lambda request: httpx.Response(500), lambda request: httpx.Response(202, json={'data': {}}), lost):
        with pytest.raises(RuntimeError) as uncertain:
            await service(FakeTelnyx(create=answer)).send_fax_file('+12025550123', pdf(tmp_path), reference='c' * 32)
        assert not isinstance(uncertain.value, TelnyxFaxRefused)

    def unreachable(request):
        raise httpx.ConnectError('no route', request=request)

    with pytest.raises(TelnyxFaxRefused) as refused:
        await service(FakeTelnyx(create=unreachable)).send_fax_file('+12025550123', pdf(tmp_path), reference='c' * 32)
    assert refused.value.sentence == telnyx.UNREACHABLE


@pytest.mark.asyncio
@pytest.mark.parametrize('reason, sentence, category, before', [
    ('user_busy', telnyx.BUSY, None, True),
    ('receiver_no_answer', telnyx.NO_ANSWER, None, True),
    ('fax_initial_communication_timeout', telnyx.NO_FAX, None, True),
    ('no_outbound_profile', telnyx.NO_OUTBOUND_PROFILE, None, True),
    ('file_format_invalid', telnyx.NOT_CONVERTED, None, True),
    ('fax_signaling_error', telnyx.NOT_CONFIRMED, 'pages_unconfirmed', None),
    ('receiver_call_dropped', telnyx.NOT_CONFIRMED, 'pages_unconfirmed', None),
    ('a_reason_telnyx_adds_later', telnyx.NOT_CONFIRMED, 'pages_unconfirmed', None),
    (None, telnyx.NOT_CONFIRMED, 'pages_unconfirmed', None),
])
async def test_failure_reasons_map_to_a_definite_failure_or_an_uncertain_one(reason, sentence, category, before):
    fake = FakeTelnyx(status=lambda request: httpx.Response(200, json=fax('failed', failure_reason=reason)))
    result = await service(fake).get_fax_status(SID.upper())
    assert result['status'] == 'failed' and result['failure'] == sentence
    assert result.get('failure_category') == category and result['before_fax_data'] is before
    assert sentence in telnyx.FAILURE_SENTENCES


@pytest.mark.asyncio
async def test_a_page_count_on_a_failed_fax_means_partly_sent():
    fake = FakeTelnyx(status=lambda request: httpx.Response(200, json=fax('failed', failure_reason='user_busy',
                                                                           page_count=2)))
    result = await service(fake).get_fax_status(SID)
    assert (result['failure'], result['failure_category'], result['pages']) == (telnyx.PARTLY_SENT, 'partly_sent', 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('status, expected', [('queued', 'in_progress'), ('media.processed', 'in_progress'),
                                              ('originated', 'in_progress'), ('sending', 'in_progress'),
                                              ('delivered', 'success')])
async def test_documented_statuses(status, expected):
    fake = FakeTelnyx(status=lambda request: httpx.Response(200, json=fax(status)))
    assert (await service(fake).get_fax_status(SID))['status'] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('answer', [
    lambda request: httpx.Response(200, json=fax('received')),  # a received fax's status is not a sent fax's
    lambda request: httpx.Response(200, json=fax('some_new_status')),
    lambda request: httpx.Response(200, json={'data': {**fax('delivered')['data'], 'id': SID.replace('3b', '4c')}}),
    lambda request: httpx.Response(200, json={'data': {**fax('delivered')['data'], 'direction': 'inbound'}}),
    lambda request: httpx.Response(404),
])
async def test_an_unrecognized_status_answer_changes_nothing(answer):
    with pytest.raises(RuntimeError):
        await service(FakeTelnyx(status=answer)).get_fax_status(SID)


@pytest.mark.asyncio
async def test_creating_a_fax_application_names_the_results_address():
    fake = FakeTelnyx()
    identity = await service(fake).create_fax_application('Faxbot', 'https://faxbot.example.org/telnyx-fax-callback')
    assert identity == APP
    assert json.loads(fake.requests[0].content) == {
        'application_name': 'Faxbot', 'webhook_event_url': 'https://faxbot.example.org/telnyx-fax-callback',
        'active': True}

    def refused(request):
        return httpx.Response(401, json={'errors': [{'code': '10009', 'detail': 'echo KEYsynthetic-telnyx-key'}]})

    with pytest.raises(TelnyxFaxError) as error:
        await service(refused).create_fax_application('Faxbot', 'https://faxbot.example.org/telnyx-fax-callback')
    assert 'API key' in error.value.sentence and KEY not in error.value.sentence


# -- notification signatures -----------------------------------------------------------------------------------

def keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    raw = base64.b64encode(public.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    der = base64.b64encode(public.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()
    return private, raw, der


def sign(private, body, stamp):
    return base64.b64encode(private.sign(str(stamp).encode() + b'|' + body)).decode()


def test_signature_checks_the_raw_body_the_timestamp_and_the_key():
    private, raw, der = keypair()
    body, now = b'{"data":{"event_type":"fax.delivered"}}', int(time.time())
    signature = sign(private, body, now)
    for key in (raw, der):
        assert telnyx.verify_signature(key, timestamp=str(now), signature=signature, body=body)
    assert not telnyx.verify_signature(raw, timestamp=str(now), signature=signature, body=body + b' ')
    assert not telnyx.verify_signature(raw, timestamp=str(now + 1), signature=signature, body=body)
    stale = now - 301
    assert not telnyx.verify_signature(raw, timestamp=str(stale), signature=sign(private, body, stale), body=body)
    assert not telnyx.verify_signature(keypair()[1], timestamp=str(now), signature=signature, body=body)
    assert not telnyx.verify_signature('', timestamp=str(now), signature=signature, body=body)
    assert not telnyx.verify_signature('not base64!', timestamp=str(now), signature=signature, body=body)


# -- captured delivery through the real worker, transport, poller and store --------------------------------------

def captured(public_key=''):
    return ProviderConfiguration('telnyx_fax', credentials={'api_key': KEY},
                                 settings={'connection_id': APP, 'from_number': '+13035550100',
                                           'public_key': public_key})


def accepted(installation, tmp_path, configuration_for_fax):
    configuration, store, snapshot = installation
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch(
        {'fax_data_dir': str(tmp_path), 'public_api_url': 'https://faxbot.example.org'}),
        actor='test', restart_required=False, providers={'outbound': configuration_for_fax})
    job = accept((configuration, store, snapshot))
    (tmp_path / (job + '.pdf')).write_bytes(b'%PDF-internal-synthetic-document')
    return configuration, store, job


def mock_clients(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        kwargs['transport'] = httpx.MockTransport(handler)
        return original(**kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', client)


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous', [False, True])
async def test_a_telnyx_fax_is_sent_once_and_confirmed_by_polling(installation, tmp_path, monkeypatch, ambiguous):
    configuration, store, job = accepted(installation, tmp_path, captured())

    def create(request):
        assert store.get(job)['state'] == 'submitting'
        body = json.loads(request.content)
        assert 'webhook_url' not in body  # no public key: Faxbot polls
        assert base64.b64decode(body['client_state']).decode() == store.get(job)['attempt_id']
        if ambiguous:
            raise httpx.ReadTimeout('private-response-failure', request=request)
        return httpx.Response(202, json=fax('queued'))
    fake = FakeTelnyx(create=create)
    mock_clients(monkeypatch, fake)
    worker = OutboundWorker(store, CapturedTransport(store, Runtime()))
    await worker.step()
    assert [request.url.path for request in fake.requests] == ['/v2/media', '/v2/faxes']
    if ambiguous:
        # The create may have reached Telnyx: never sent again; a person attaches the fax ID Telnyx shows.
        assert store.get(job)['state'] == 'reconciliation_required'
        view = store.operator_view(job)
        assert view['can_bind_provider_identity']
        store.bind_provider_identity(job, expected_version=view['version'], provider_sid=SID.upper(), actor='admin')
    else:
        assert store.get(job)['state'] == 'in_progress'
    # The background poller finds the fax by itself: polling is how a Telnyx fax finishes without notices.
    with configuration.engine.begin() as connection:
        connection.execute(store.deliveries.update().where(store.deliveries.c.id == job).values(
            next_poll_at=datetime.utcnow() - timedelta(seconds=1)))
    assert await OutboundPoller(store).step() is True
    assert store.get(job)['state'] == 'success'
    await worker.step()
    assert [request.method for request in fake.requests] == ['POST', 'POST', 'GET']
    # Before sending, route choice prices it from the card; a sent fax's own cost waits for its call length,
    # which Telnyx's fax record does not give, so it is unknown, never $0.
    from api.app.routing.capture import CostRecorder
    from api.app.routing.store import RouteStore
    from api.tests.test_sinch_route import fax_cost
    routes = RouteStore(configuration.engine)
    routes.replace_cards([rate('telnyx_fax', page='0.007', minute='0.005', minimum=60)])
    CostRecorder(routes).step()
    state, summary = fax_cost(configuration, routes, job)
    assert state != 'priced' and '$0.00' not in summary, (state, summary)


@pytest.mark.asyncio
async def test_a_refused_send_is_a_definite_failure_with_a_plain_reason(installation, tmp_path, monkeypatch):
    configuration, store, job = accepted(installation, tmp_path, captured())
    mock_clients(monkeypatch, FakeTelnyx(create=lambda request: httpx.Response(422, json={'errors': []})))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    row = store.get(job)
    assert row['state'] == 'failed'
    with configuration.engine.connect() as connection:
        error = connection.execute(sa.select(configuration.jobs.c.error).where(configuration.jobs.c.id == job)).scalar()
    assert error == telnyx.REFUSED[422]


@pytest.mark.asyncio
@pytest.mark.parametrize('reason, state', [('user_busy', 'failed'), ('fax_signaling_error', 'reconciliation_required')])
async def test_a_failed_fax_is_failed_or_waits_for_a_person(installation, tmp_path, monkeypatch, reason, state):
    configuration, store, job = accepted(installation, tmp_path, captured())
    mock_clients(monkeypatch, FakeTelnyx(
        status=lambda request: httpx.Response(200, json=fax('failed', failure_reason=reason))))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == state


@pytest.mark.asyncio
async def test_a_signed_notice_prompts_a_status_read_and_nothing_else_does(installation, tmp_path, monkeypatch):
    from api.app.outbound_callbacks import CallbackRejected, verified_telnyx_notice
    private, public, _ = keypair()
    configuration, store, job = accepted(installation, tmp_path, captured(public))
    bodies = []

    def create(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(202, json=fax('queued'))
    mock_clients(monkeypatch, FakeTelnyx(create=create))
    await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
    attempt = store.get(job)['attempt_id']
    # With the account's public key, Telnyx is told to send this attempt's results to Faxbot.
    assert bodies[0]['webhook_url'] == (f'https://faxbot.example.org/telnyx-fax-callback?job_id={job}'
                                        f'&attempt_id={attempt}')
    body = json.dumps({'data': {'event_type': 'fax.delivered', 'payload': {'fax_id': SID, 'status': 'delivered',
                                                                          'page_count': 3}}}).encode()
    now = int(time.time())

    def check(**changes):
        given = {'timestamp': str(now), 'signature': sign(private, body, now), 'body': body, **changes}
        return verified_telnyx_notice(store, changes.pop('job', job), changes.pop('attempt', attempt),
                                      **{name: given[name] for name in ('timestamp', 'signature', 'body')})

    assert check() is True
    for forged in ({'body': body.replace(b'delivered', b'failed')}, {'signature': sign(keypair()[0], body, now)},
                   {'timestamp': str(now - 600), 'signature': sign(private, body, now - 600)}):
        try:
            check(**forged)
        except CallbackRejected:
            continue
        pytest.fail(f'accepted a notice with a forged {sorted(forged)}')
    with pytest.raises(CallbackRejected):
        verified_telnyx_notice(store, job, 'f' * 32, timestamp=str(now), signature=sign(private, body, now),
                               body=body)
    other = json.dumps({'data': {'payload': {'fax_id': SID.replace('3b', '4c')}}}).encode()
    with pytest.raises(CallbackRejected):
        verified_telnyx_notice(store, job, attempt, timestamp=str(now), signature=sign(private, other, now),
                               body=other)
    # The status read, not the notice, decides the result.
    assert store.get(job)['state'] == 'in_progress'
    await OutboundPoller(store).refresh(job)
    assert store.get(job)['state'] == 'success'


def test_the_results_address_is_given_only_with_a_public_key():
    from types import SimpleNamespace
    values = SimpleNamespace(public_api_url='https://faxbot.example.org/')
    claim = SimpleNamespace(job_id='a' * 32, attempt_id='b' * 32)
    assert telnyx_callback_url(values, captured(), claim) is None
    url = telnyx_callback_url(values, captured('c2lnbmluZy1rZXk='), claim)
    assert url == f"https://faxbot.example.org/telnyx-fax-callback?job_id={'a' * 32}&attempt_id={'b' * 32}"
    assert telnyx_callback_url(SimpleNamespace(public_api_url='not a url'), captured('c2lnbmluZy1rZXk='), claim) is None


# -- pricing and route choice ------------------------------------------------------------------------------------

def rate(provider, *, page='0', minute='0', minimum=0):
    from datetime import datetime
    from api.app.routing.costs import RateCard, parse_amount
    return RateCard(None, provider, 'outbound', provider, 'USD', parse_amount(minute), parse_amount(page), 0, 60,
                    minimum, None, datetime(2026, 10, 13))


TELNYX_SETTINGS = {'TELNYX_API_KEY': KEY, 'TELNYX_FAX_CONNECTION_ID': APP, 'TELNYX_FAX_FROM_NUMBER': '+13035550100'}


class Recorder:
    """A captured transport that records which account each submission used."""

    def __init__(self, store):
        self.store, self.used = store, []

    def prepare(self, claim):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        @asynccontextmanager
        async def prepared():
            _, profile, _ = self.store.load_dispatch(claim)
            provider = profile.configuration.provider_id

            async def submit():
                self.used.append(provider)
                return SubmissionReceipt('48151623', 'in_progress')
            yield SimpleNamespace(submit=submit)
        return prepared()


@pytest.mark.asyncio
@pytest.mark.parametrize('other, expected', [
    # Per page against per page: $0.007 a page plus the call's minutes beats Sinch's $0.045 a page.
    ('sinch', 'telnyx_fax'),
    # Per page against per minute: a route billed only by the minute at $0.0095 costs less than the same minutes
    # at $0.005 plus $0.007 for each page, so the per-minute route wins.
    ('signalwire', 'signalwire'),
])
async def test_route_choice_prices_the_per_page_route_against_the_others(database, tmp_path, other, expected):
    from api.app.routing.costs import estimate_cost
    from api.app.routing.transport import RoutedTransport
    from api.tests.test_sinch_route import SINCH_SETTINGS, accept as accept_fax, installed
    telnyx_card = rate('telnyx_fax', page='0.007', minute='0.005', minimum=60)
    other_card = rate('sinch', page='0.045') if other == 'sinch' else rate('signalwire', minute='0.0095', minimum=60)
    environment = {'FAX_BACKEND': other, 'FAX_OUTBOUND_ROUTES': 'telnyx_fax',
                   'PUBLIC_API_URL': 'https://faxbot.example.org', **TELNYX_SETTINGS, **SINCH_SETTINGS,
                   'SIGNALWIRE_SPACE_URL': 'example.signalwire.com', 'SIGNALWIRE_PROJECT_ID': 'project-1',
                   'SIGNALWIRE_API_TOKEN': 'synthetic-token', 'SIGNALWIRE_FAX_FROM_E164': '+13035550101'}
    profile = ProviderConfiguration(other, credentials={'api_key': 'synthetic', 'api_secret': 'synthetic',
                                                        'api_token': 'synthetic'},
                                    settings={'project_id': 'project-1', 'space_url': 'example.signalwire.com'})
    configuration, delivery, routes, snapshot = installed(database, tmp_path, environment, profile,
                                                          [telnyx_card, other_card])
    job = accept_fax(configuration, snapshot)
    recorder = Recorder(delivery)
    assert await OutboundWorker(delivery, RoutedTransport(recorder, direct=None)).step() is True
    assert recorder.used == [expected]
    decision = routes.decision(delivery.get(job)['attempt_id'])
    assert (decision['route'], decision['provider_id']) == (expected, expected)
    # The same ordering as the shared estimate of one delivered three-page fax on each card.
    cheaper = min((telnyx_card, other_card), key=lambda card: estimate_cost(card, 3))
    assert cheaper.provider_id == expected


def test_the_published_card_prices_pages_and_minutes_with_its_source():
    from api.app.routing.costs import attempt_cost
    from api.app.routing.seed import load_cards
    card = next(card for card in load_cards() if card.provider_id == 'telnyx_fax' and card.direction == 'outbound')
    assert (card.per_page_micros, card.per_minute_micros, card.billing_increment_seconds, card.minimum_seconds) == \
        (7_000, 5_000, 60, 60)
    assert card.source_url == 'https://telnyx.com/pricing/fax' and card.captured_on.date().isoformat() == '2026-10-13'
    # Three delivered pages over a 95-second call: two billed minutes and three pages.
    assert attempt_cost(card, seconds=95, pages=3, delivered=True) == 2 * 5_000 + 3 * 7_000
    # An unknown call length is an unknown cost, never zero minutes.
    assert attempt_cost(card, seconds=None, pages=3, delivered=True) is None


@pytest.mark.parametrize('key, source', [('sip', "Telnyx's published price, read 3 October 2026"),
                                         ('telnyx_fax', "Telnyx Fax API's published price, read 13 October 2026")])
def test_a_telnyx_account_with_no_saved_card_is_priced_by_telnyxs_published_price(database, key, source):  # noqa: F811
    """Live, 13 October 2026: a Telnyx trunk and a Telnyx Fax API account with no rate card saved in Faxbot read "none
    of your routes had a price", so cost routing was blind, though Telnyx publishes both prices (telnyx.com/pricing/
    elastic-sip: $0.005 a minute in the US, 60/60; telnyx.com/pricing/fax: $0.007 a page plus that trunk usage; both
    read again on 13 October 2026). The published card prices them and the sentence names it; a price you enter
    wins."""
    from types import SimpleNamespace
    from api.app.routing.costs import RateCard, parse_amount
    from api.app.routing.pricing import price
    from api.app.routing.store import RouteStore
    from api.app.schema import upgrade_schema
    upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    humblefax = RateCard(None, 'humblefax', 'outbound', 'HumbleFax', 'USD', 0, 0, 0, 60, 0, None,
                         datetime(2026, 10, 9), parse_amount('10', whole_digits=4))
    routes.replace_cards([humblefax])  # cards saved in Faxbot, none for Telnyx
    values = SimpleNamespace(fax_default_country='US', sip_trunk_preset='telnyx', sip_t38_enabled=True,
                             plan_budgets='', time_zone='', provider_accounts={})
    now = datetime(2026, 10, 13, 18)
    found = price(routes, values, key, '+12025550123', 2, now=now)
    # About 31 seconds for two typical pages: one billed minute, and the Fax API's two pages.
    assert found.micros == (5_000 if key == 'sip' else 5_000 + 2 * 7_000) and found.currency == 'USD'
    assert source in found.sentence
    # The price you enter for the account wins, and no longer says it is Telnyx's.
    identity = 'sip-telnyx' if key == 'sip' else 'telnyx_fax'
    yours = RateCard(None, identity, 'outbound', 'Yours', 'USD', parse_amount('0.004'),
                     parse_amount('0.006' if key == 'telnyx_fax' else '0'), 0, 60, 60, None, datetime(2026, 10, 13))
    routes.replace_cards([humblefax, yours])
    entered = price(routes, values, key, '+12025550123', 2, now=now)
    assert entered.micros == (4_000 if key == 'sip' else 4_000 + 2 * 6_000)
    assert 'published price' not in entered.sentence
