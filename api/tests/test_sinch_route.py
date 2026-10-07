"""Sinch as a third live route: its published price, the charges Sinch and Phaxio report, route
choice with three routes, and which Sinch failures may take another route.

Sinch's and Phaxio's APIs are httpx mock transports; every account, fax ID and number is synthetic.
The database tests run on SQLite and, with PGDB, on PostgreSQL (the ``database`` fixture).
"""
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import json
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.app import sinch_service
from api.app.config_profiles import ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.outbound_store import OutboundStore
from api.app.outbound_transport import PreparedSubmission
from api.app.outbound_worker import OutboundWorker, SubmissionReceipt
from api.app.routing.billing import BillingReconciler
from api.app.routing.capture import CostRecorder
from api.app.routing.charges import PhaxioCharges, SinchCharges, parse_phaxio_charge, parse_sinch_charge
from api.app.routing.costs import RateCard, estimate_cost, parse_amount
from api.app.routing.store import RouteStore
from api.app.routing.transport import RoutedTransport
from api.app.schema import upgrade_schema


TO = '+12025550123'
SINCH_FAX = '01JSYNTHETICSINCHFAX0001'
SINCH_AUTH = 'Basic ' + base64.b64encode(b'synthetic-sinch-key:synthetic-sinch-secret').decode()
SINCH_SETTINGS = {'SINCH_PROJECT_ID': 'project-1', 'SINCH_API_KEY': 'synthetic-sinch-key',
                  'SINCH_API_SECRET': 'synthetic-sinch-secret'}


def card(provider, *, page='0', minute='0', monthly=None, direction='outbound'):
    return RateCard(None, provider, direction, provider.title(), 'USD', parse_amount(minute), parse_amount(page), 0,
                    60, 0, None, datetime(2026, 10, 7),
                    None if monthly is None else parse_amount(monthly, whole_digits=4))


def installed(database, tmp_path, environment, profile, cards):
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({'FAX_DISABLED': 'false', **environment})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': profile})
    delivery, routes = OutboundStore(configuration), RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.replace_cards(cards)
    return configuration, delivery, routes, snapshot


def accept(configuration, snapshot, to=TO, pages=3):
    identity, now = uuid4().hex, datetime.utcnow()
    configuration.accept_outbound(snapshot.active, {'id': identity, 'to_number': to, 'file_name': 'synthetic.pdf',
        'tiff_path': '', 'status': 'queued', 'pages': pages, 'created_at': now, 'updated_at': now})
    return identity


class Answering:
    """A transport whose single submission answers with ``receipt``."""

    def __init__(self, store, receipt):
        self.store, self.receipt = store, receipt

    @asynccontextmanager
    async def prepare(self, claim):
        yield self

    async def submit(self):
        return self.receipt


def mock_client(handler):
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def fax_cost(configuration, routes, job):
    """(state, sentence) for one sent fax's cost, as Sent and 'faxbot costs fax' show it."""
    from api.app.routing.carriers import CarrierChargeStore
    from api.app.routing.spending import Spending
    cost = Spending(routes, CarrierChargeStore(configuration.engine)).job(job)
    return cost['state'], cost['summary']


# -- the published price ----------------------------------------------------------------------------

def test_the_shipped_sinch_card_prices_pages_from_its_published_page_and_leaves_the_number_unknown(tmp_path):
    from api.app.routing.seed import default_path, load_cards
    cards = {(item.provider_id, item.direction): item for item in load_cards()}
    sending, receiving = cards[('sinch', 'outbound')], cards[('sinch', 'inbound')]
    assert (sending.per_page_micros, sending.per_minute_micros, sending.currency) == (45_000, 0, 'USD')
    assert (sending.source_url, sending.captured_on) == ('https://sinch.com/voice/fax-api/', datetime(2026, 10, 7))
    assert receiving.per_page_micros == 45_000 and receiving.captured_on == datetime(2026, 10, 7)
    # Three pages at $0.045 each.
    assert estimate_cost(sending, 3) == 135_000
    document = json.loads(default_path().read_text(encoding='utf-8'))
    shipped = {(entry['provider_id'], entry['direction']): entry for entry in document['providers']}
    # Sinch publishes no fax number price: unknown, never $0. Phaxio publishes $2 a month.
    assert shipped[('sinch', 'outbound')]['number_rental_monthly'] is None
    assert shipped[('sinch', 'inbound')]['number_rental_monthly'] is None
    assert shipped[('phaxio', 'outbound')]['number_rental_monthly'] == '2.00'
    assert shipped[('phaxio', 'outbound')]['advertised_on'] == '2026-10-07'
    # A card whose price is not published is left out, never loaded as free.
    unpublished = dict(shipped[('sinch', 'outbound')], per_page=None)
    path = tmp_path / 'rate_cards.json'
    path.write_text(json.dumps({'providers': [unpublished]}), encoding='utf-8')
    assert load_cards(path) == []


# -- charges Sinch and Phaxio report --------------------------------------------------------------

def test_a_price_counts_only_once_the_provider_says_the_fax_is_finished():
    assert parse_sinch_charge({'status': 'COMPLETED', 'price': {'amount': '0.1350', 'currencyCode': 'usd'}}) == (
        135_000, 'USD', None)
    assert parse_sinch_charge({'status': 'FAILURE', 'price': {'amount': '0.0000', 'currencyCode': 'USD'}}) == (
        0, 'USD', None)
    for unknown in ({'status': 'IN_PROGRESS', 'price': {'amount': '0.0000', 'currencyCode': 'USD'}},
                    {'status': 'QUEUED'}, {'status': 'COMPLETED'}, {'status': 'COMPLETED', 'price': None},
                    {'status': 'COMPLETED', 'price': {'amount': 'n/a', 'currencyCode': 'USD'}},
                    {'status': 'COMPLETED', 'price': {'amount': 0.135, 'currencyCode': 'USD'}},
                    {'status': 'COMPLETED', 'price': {'amount': '0.1350', 'currencyCode': 'dollars'}}, None):
        assert parse_sinch_charge(unknown) is None
    assert parse_phaxio_charge({'status': 'success', 'cost': 21}) == (210_000, 'USD', None)
    assert parse_phaxio_charge({'status': 'failure', 'cost': 0}) == (0, 'USD', None)
    for unknown in ({'status': 'queued', 'cost': 21}, {'status': 'inprogress', 'cost': 21},
                    {'status': 'success'}, {'status': 'success', 'cost': None}, {'status': 'success', 'cost': '21'},
                    {'status': 'success', 'cost': True}, {'status': 'success', 'cost': -5}):
        assert parse_phaxio_charge(unknown) is None


def sinch_installation(database, tmp_path, *, base_url=''):
    profile = ProviderConfiguration('sinch', credentials={'api_key': 'synthetic-sinch-key',
                                                          'api_secret': 'synthetic-sinch-secret'},
                                    settings={'project_id': 'project-1', 'base_url': base_url})
    return installed(database, tmp_path, {'FAX_BACKEND': 'sinch', **SINCH_SETTINGS}, profile,
                     [card('sinch', page='0.045')])


@pytest.mark.asyncio
async def test_sinch_reports_its_charge_after_delivery_and_a_correction_settles_it(database, tmp_path):
    configuration, delivery, routes, snapshot = sinch_installation(database, tmp_path)
    job = accept(configuration, snapshot)
    await OutboundWorker(delivery, RoutedTransport(Answering(delivery, SubmissionReceipt(SINCH_FAX, 'success')),
                                                   direct=None)).step()
    CostRecorder(routes).step()
    attempt = delivery.get(job)['attempt_id']
    estimate = routes.decision(attempt)
    # Faxbot's own estimate from the card: three pages at $0.045, kept apart from what Sinch reports.
    assert (estimate['estimated_cost_micros'], estimate['cost_basis'], estimate['reported_cost_micros']) == (
        135_000, 'estimated', None)
    delivered, history = delivery.get(job), delivery.history(job)
    answers = [{'status': 'IN_PROGRESS', 'price': {'amount': '0.0000', 'currencyCode': 'USD'}},
               {'status': 'COMPLETED'},
               {'status': 'COMPLETED', 'price': {'amount': '0.1350', 'currencyCode': 'USD'}},
               {'status': 'COMPLETED', 'price': {'amount': '0.1350', 'currencyCode': 'USD'}},
               {'status': 'COMPLETED', 'price': {'amount': '0.0900', 'currencyCode': 'USD'}}]
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get('authorization')))
        return httpx.Response(200, json={'id': SINCH_FAX, 'direction': 'OUTBOUND', **answers[len(seen) - 1]})
    billing = BillingReconciler(routes, {'sinch': SinchCharges(delivery, client_factory=mock_client(handler))},
                                retry=timedelta(minutes=10))
    start = datetime.utcnow()
    # A placeholder while the fax is going, then a finished fax not priced yet: unknown stays unknown.
    billing.step(now=start)
    billing.step(now=start + timedelta(minutes=11))
    row = routes.decision(attempt)
    assert (row['reported_cost_micros'], row['settled_cost_micros'], row['billing_checks']) == (None, None, 2)
    assert fax_cost(configuration, routes, job) == ('waiting', 'Cost not reported yet.')
    billing.step(now=start + timedelta(minutes=22))
    row = routes.decision(attempt)
    assert (row['reported_cost_micros'], row['reported_currency'], row['settled_cost_micros']) == (135_000, 'USD', None)
    billing.step(now=start + timedelta(minutes=33))  # the same price again changes nothing
    with routes.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(routes.charges)).scalar_one() == 1
    # A day after the fax ended, a corrected price replaces it and settles.
    billing.step(now=start + timedelta(hours=25))
    row = routes.decision(attempt)
    assert (row['reported_cost_micros'], row['settled_cost_micros']) == (90_000, 90_000)
    assert row['estimated_cost_micros'] == 135_000  # the estimate is never overwritten
    with routes.engine.connect() as connection:
        version = connection.execute(sa.select(routes.charges.c.version)).scalar_one()
    assert version == 2
    # Asked with the account that sent the fax, only at Sinch's documented host.
    assert seen == [(f'https://fax.api.sinch.com/v3/projects/project-1/faxes/{SINCH_FAX}', SINCH_AUTH)] * 5
    billing.step(now=start + timedelta(hours=26))
    assert len(seen) == 5  # settled charges are not read again
    assert delivery.get(job) == delivered and delivery.history(job) == history
    totals = routes.cost_totals(start - timedelta(days=1))[0]
    assert (totals['provider_id'], totals['settled_cost_micros'], totals['unreported']) == ('sinch', {'USD': 90_000}, 0)
    assert fax_cost(configuration, routes, job) == ('reported', 'Sinch charged $0.09 for this fax.')


@pytest.mark.asyncio
async def test_sinch_charges_are_asked_only_of_a_sinch_host(database, tmp_path):
    configuration, delivery, routes, snapshot = sinch_installation(database, tmp_path,
                                                                   base_url='https://fax.example.org/v3')
    job = accept(configuration, snapshot)
    await OutboundWorker(delivery, RoutedTransport(Answering(delivery, SubmissionReceipt(SINCH_FAX, 'success')),
                                                   direct=None)).step()
    asked = []
    source = SinchCharges(delivery, client_factory=mock_client(lambda request: asked.append(request) or
                                                               httpx.Response(500)))
    row = {'id': delivery.get(job)['attempt_id'], 'job_id': job, 'provider_sid': SINCH_FAX}
    assert source(row) == [] and asked == []
    # Another provider's attempt and an unusable fax ID are never asked about.
    phaxio = PhaxioCharges(delivery, client_factory=mock_client(lambda request: asked.append(request)))
    assert phaxio(row) == [] and phaxio({**row, 'provider_sid': '48151623'}) == []
    assert source({**row, 'provider_sid': '../../projects'}) == [] and asked == []


def phaxio_installation(database, tmp_path):
    profile = ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-phaxio-key',
                                                           'api_secret': 'synthetic-phaxio-secret'})
    return installed(database, tmp_path, {'FAX_BACKEND': 'phaxio'}, profile, [card('phaxio', page='0.07')])


@pytest.mark.asyncio
async def test_phaxio_reports_its_cost_and_a_credited_failure_is_a_correction(database, tmp_path):
    configuration, delivery, routes, snapshot = phaxio_installation(database, tmp_path)
    job = accept(configuration, snapshot)
    await OutboundWorker(delivery, RoutedTransport(Answering(delivery, SubmissionReceipt('48151623', 'success')),
                                                   direct=None)).step()
    CostRecorder(routes).step()
    attempt = delivery.get(job)['attempt_id']
    answers = [{'status': 'inprogress', 'cost': 21}, {'status': 'success', 'cost': 21},
               {'status': 'success', 'cost': 0}]
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={'success': True, 'message': 'Metadata for fax',
                                         'data': {'id': 48151623, 'direction': 'sent', **answers[len(seen) - 1]}})
    billing = BillingReconciler(routes, {'phaxio': PhaxioCharges(delivery, client_factory=mock_client(handler))},
                                retry=timedelta(minutes=10))
    start = datetime.utcnow()
    billing.step(now=start)
    assert routes.decision(attempt)['reported_cost_micros'] is None  # still going: unknown
    billing.step(now=start + timedelta(minutes=11))
    assert routes.decision(attempt)['reported_cost_micros'] == 210_000  # 21 US cents
    billing.step(now=start + timedelta(hours=25))
    row = routes.decision(attempt)
    # Phaxio credited the fax back: a real $0, recorded as a correction and settled.
    assert (row['reported_cost_micros'], row['settled_cost_micros'], row['estimated_cost_micros']) == (0, 0, 210_000)
    assert seen == ['https://api.phaxio.com/v2.1/faxes/48151623'] * 3
    assert fax_cost(configuration, routes, job) == ('reported', 'Phaxio charged $0.00 for this fax.')


def test_a_failed_charge_lookup_is_asked_again_later_and_records_nothing(database, tmp_path):
    configuration, delivery, routes, snapshot = sinch_installation(database, tmp_path)
    job = accept(configuration, snapshot)
    import asyncio
    asyncio.run(OutboundWorker(delivery, RoutedTransport(
        Answering(delivery, SubmissionReceipt(SINCH_FAX, 'success')), direct=None)).step())
    CostRecorder(routes).step()
    attempt = delivery.get(job)['attempt_id']
    for answer in (httpx.Response(503), httpx.Response(200, content=b'not json'),
                   httpx.Response(200, json={'id': 'another-fax', 'status': 'COMPLETED',
                                             'price': {'amount': '9.0000', 'currencyCode': 'USD'}})):
        billing = BillingReconciler(routes, {'sinch': SinchCharges(delivery, client_factory=mock_client(
            lambda request, answer=answer: answer))}, retry=timedelta(seconds=0))
        billing.step(now=datetime.utcnow())
        assert routes.decision(attempt)['reported_cost_micros'] is None


# -- route choice with three routes -----------------------------------------------------------------

def finished(configuration, snapshot, routes, route, outcomes, reported):
    """Finished attempts on ``route`` to TO, with what the provider reported for each."""
    now = datetime.utcnow()
    jobs = [accept(configuration, snapshot, pages=1) for _ in outcomes]
    with routes.engine.begin() as connection:
        for job, outcome in zip(jobs, outcomes):
            attempt = uuid4().hex
            connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase=outcome,
                                                               created_at=now, submitted_at=now, completed_at=now))
            connection.execute(routes.costs.insert().values(
                id=attempt, job_id=job, destination=TO, route=route, route_reason='preferred', provider_id=route,
                outcome=outcome, reported_cost_micros=parse_amount(reported), reported_currency='USD',
                billing_checks=0, created_at=now, updated_at=now))


def test_sinch_takes_its_place_among_the_trunk_and_humblefax_by_cost_per_delivered_fax(database, tmp_path):
    from api.app.routing.plan import RoutePlanner, explain
    environment = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'FAX_OUTBOUND_ROUTES': 'humblefax, sinch',
                   **SINCH_SETTINGS}
    configuration, _, routes, snapshot = installed(
        database, tmp_path, environment, ProviderConfiguration('sip'),
        [card('sip-telnyx', minute='0.005'), card('humblefax', monthly='10.00'), card('sinch', page='0.045')])
    values = snapshot.active.values
    planner = RoutePlanner(routes)

    def order(**options):
        plan = planner.plan(to_number=TO, bound='sip', values=values, pages=1, alternates=True, **options)
        return plan, [(choice.route.key, choice.reason) for choice in plan.choices]
    # By the rate cards alone: the flat plan is included, then a one-minute trunk call ($0.005) before Sinch ($0.045).
    _, choices = order()
    assert choices == [('humblefax', 'included'), ('sip', 'alternative'), ('sinch', 'alternative')]
    # Long calls made the trunk cost more here; what each route really cost per delivered fax decides.
    finished(configuration, snapshot, routes, 'sip', ['success'] * 4 + ['failed'], '0.12')
    finished(configuration, snapshot, routes, 'sinch', ['success'] * 3, '0.045')
    _, choices = order()
    assert choices == [('humblefax', 'included'), ('sinch', 'alternative'), ('sip', 'alternative')]
    # When HumbleFax has failed this fax for certain, Sinch is next, and says why in one sentence.
    plan, choices = order(exclude={'humblefax'})
    assert choices == [('sinch', 'cheapest_delivered'), ('sip', 'alternative')]
    assert explain(plan.first) == ('Sinch cost $0.045 per delivered fax to this number over the last 30 days, '
                                   'the cheapest of 2 routes.')


# -- what may take another route ------------------------------------------------------------------

def sinch_api(monkeypatch, handler):
    """Point the Sinch adapter's HTTP client at ``handler``; returns the requests it saw."""
    seen = []

    def recording(request):
        seen.append(request)
        return handler(request)
    shim = ModuleType('httpx')
    shim.__dict__.update(httpx.__dict__)
    shim.AsyncClient = lambda **options: httpx.AsyncClient(transport=httpx.MockTransport(recording), **options)
    monkeypatch.setattr(sinch_service, 'httpx', shim)
    return seen


class SinchFirst:
    """The captured transport, with the real Sinch adapter for Sinch and a recorder for the fax's own provider."""

    def __init__(self, store, pdf):
        self.store, self.pdf, self.used = store, pdf, []

    @asynccontextmanager
    async def prepare(self, claim):
        from api.app.provider_execution import service_from_profile
        _, profile, job = self.store.load_dispatch(claim)
        provider = profile.configuration.provider_id
        if provider == 'sinch':
            yield SimpleNamespace(submit=self._recorded(provider, PreparedSubmission(
                claim, profile, {**job, 'to_number': TO}, str(self.pdf), None, service_from_profile(profile)).submit))
        else:
            async def phaxio():
                return SubmissionReceipt('48151623', 'in_progress')
            yield SimpleNamespace(submit=self._recorded(provider, phaxio))

    def _recorded(self, provider, submit):
        async def run():
            self.used.append(provider)
            return await submit()
        return run


@pytest.fixture
def phaxio_then_sinch(database, tmp_path, monkeypatch):
    from api.app.routing.fallback import FallbackPolicy, FallbackScheduler
    environment = {'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'sinch', 'PHAXIO_API_KEY': 'synthetic-phaxio-key',
                   'PHAXIO_API_SECRET': 'synthetic-phaxio-secret', 'PUBLIC_API_URL': 'https://faxbot.example.org',
                   **SINCH_SETTINGS}
    profile = ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-phaxio-key',
                                                           'api_secret': 'synthetic-phaxio-secret'})
    configuration, delivery, routes, snapshot = installed(
        database, tmp_path, environment, profile, [card('phaxio', page='0.07'), card('sinch', page='0.045')])
    monkeypatch.setattr(OutboundStore, 'fallback_policy', FallbackPolicy(FallbackScheduler(delivery, routes)))
    pdf = tmp_path / 'synthetic.pdf'
    pdf.write_bytes(b'%PDF-1.4 synthetic')
    return configuration, delivery, routes, snapshot, SinchFirst(delivery, pdf)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', sorted(sinch_service.REFUSED))
async def test_a_send_sinch_refuses_goes_by_the_next_route_once(phaxio_then_sinch, monkeypatch, status):
    configuration, delivery, routes, snapshot, transport = phaxio_then_sinch
    seen = sinch_api(monkeypatch, lambda request: httpx.Response(status, json={'code': str(status)}))
    job = accept(configuration, snapshot)
    worker = OutboundWorker(delivery, RoutedTransport(transport, direct=None))
    await worker.step()
    # Sinch answered that it did not take the fax: nothing was sent, so the fax goes back for its next route.
    row = delivery.get(job)
    assert row['state'] == 'ready' and row['attempt_id'] is None
    fallback = next(event for event in delivery.operator_view(job)['events'] if event['kind'] == 'route_fallback')
    assert fallback['details'] == {'category': 'provider_failed', 'reason': sinch_service.REFUSED[status]}
    await worker.step()
    assert transport.used == ['sinch', 'phaxio'] and len(seen) == 1
    assert seen[0].url.path == '/v3/projects/project-1/faxes'
    assert delivery.get(job)['state'] == 'in_progress'


@pytest.mark.asyncio
async def test_sinch_out_of_reach_is_a_refusal_but_a_lost_answer_is_never_resent(phaxio_then_sinch, monkeypatch):
    from api.app.routing.fallback import FallbackScheduler
    configuration, delivery, routes, snapshot, transport = phaxio_then_sinch

    def unreachable(request):
        raise httpx.ConnectError('synthetic: connection refused', request=request)
    sinch_api(monkeypatch, unreachable)
    job = accept(configuration, snapshot)
    worker = OutboundWorker(delivery, RoutedTransport(transport, direct=None))
    await worker.step()
    assert delivery.get(job)['state'] == 'ready'
    await worker.step()
    assert transport.used == ['sinch', 'phaxio']

    for index, lost in enumerate((lambda request: httpx.Response(500), lambda request: httpx.Response(503),
                                  lambda request: (_ for _ in ()).throw(httpx.ReadTimeout('synthetic', request=request)),
                                  lambda request: httpx.Response(200, content=b'not json'))):
        transport.used.clear()
        seen = sinch_api(monkeypatch, lost)
        # Another number each time: one fax at a time goes to a number.
        job = accept(configuration, snapshot, to=f'+1202555017{index}')
        await worker.step()
        # Sinch may have taken it: the fax waits for a person and no other route sends it.
        row = delivery.get(job)
        assert row['state'] == 'reconciliation_required'
        assert FallbackScheduler(delivery, routes).step() is False
        assert await worker.step() is False
        assert transport.used == ['sinch'] and len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('fax, state, sentence, category', [
    ({'errorType': 'CALL_ERROR', 'errorCode': 17}, 'ready', 'The number was busy.', None),
    ({'errorType': 'DOCUMENT_CONVERSION_ERROR', 'errorCode': 133}, 'ready',
     'Sinch could not turn the document into fax pages.', None),
    ({'errorType': 'FAX_ERROR', 'errorCode': 8, 'pagesSentSuccessfully': 2}, 'failed',
     sinch_service.PARTLY_SENT, 'partly_sent'),
    ({'errorType': 'FAX_ERROR', 'errorCode': 8, 'pagesSentSuccessfully': 0}, 'reconciliation_required',
     None, 'pages_unconfirmed'),
    ({'errorType': 'CALL_ERROR', 'errorCode': 11}, 'reconciliation_required', None, 'pages_unconfirmed'),
    ({'errorType': 'GENERAL_ERROR'}, 'reconciliation_required', None, 'pages_unconfirmed'),
])
async def test_a_sinch_failure_takes_another_route_only_when_no_page_can_have_arrived(
        phaxio_then_sinch, monkeypatch, fax, state, sentence, category):
    from api.app.outbound_polling import OutboundPoller
    configuration, delivery, routes, snapshot, transport = phaxio_then_sinch
    answers = [httpx.Response(200, json={'id': SINCH_FAX, 'direction': 'OUTBOUND', 'status': 'IN_PROGRESS'}),
               httpx.Response(200, json={'id': SINCH_FAX, 'direction': 'OUTBOUND', 'status': 'FAILURE', **fax})]
    sinch_api(monkeypatch, lambda request: answers.pop(0))
    job = accept(configuration, snapshot)
    worker = OutboundWorker(delivery, RoutedTransport(transport, direct=None))
    await worker.step()
    assert delivery.get(job)['state'] == 'in_progress'
    assert await OutboundPoller(delivery).refresh(job) is True
    row = delivery.get(job)
    assert row['state'] == state
    if state == 'ready':
        fallback = next(event for event in delivery.operator_view(job)['events'] if event['kind'] == 'route_fallback')
        assert fallback['details'] == {'category': 'provider_failed', 'reason': sentence}
        await worker.step()
        assert transport.used == ['sinch', 'phaxio']
    else:
        # Part of the fax may have reached the machine: never sent again. With pages confirmed it failed and
        # says why; with none confirmed it is uncertain and waits for a person, as the fax engine's calls do.
        from api.app.routing.fallback import FallbackScheduler
        with configuration.engine.connect() as connection:
            job_row = connection.execute(sa.select(configuration.jobs.c.status, configuration.jobs.c.error).where(
                configuration.jobs.c.id == job)).one()
            category_now = connection.execute(sa.select(delivery.attempts.c.error_category).where(
                delivery.attempts.c.job_id == job)).scalar_one()
        assert category_now == category
        if sentence is not None:
            assert tuple(job_row) == ('failed', sentence)
        assert FallbackScheduler(delivery, routes).step() is False
        assert await worker.step() is False and transport.used == ['sinch']


@pytest.mark.asyncio
async def test_a_fax_sent_by_the_sinch_route_has_its_charge_read_with_that_routes_account(phaxio_then_sinch,
                                                                                          monkeypatch):
    """On a live install Sinch is an extra route beside the fax's own provider, never its bound account."""
    from api.app.outbound_polling import OutboundPoller
    configuration, delivery, routes, snapshot, transport = phaxio_then_sinch
    sinch_api(monkeypatch, lambda request: httpx.Response(200, json={
        'id': SINCH_FAX, 'direction': 'OUTBOUND', 'status': 'IN_PROGRESS' if request.method == 'POST' else 'COMPLETED'}))
    job = accept(configuration, snapshot)
    await OutboundWorker(delivery, RoutedTransport(transport, direct=None)).step()
    assert await OutboundPoller(delivery).refresh(job) is True
    assert transport.used == ['sinch'] and delivery.get(job)['state'] == 'success'
    CostRecorder(routes).step()
    attempt = delivery.get(job)['attempt_id']
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get('authorization')))
        return httpx.Response(200, json={'id': SINCH_FAX, 'direction': 'OUTBOUND', 'status': 'COMPLETED',
                                         'price': {'amount': '0.1350', 'currencyCode': 'USD'}})
    BillingReconciler(routes, {'sinch': SinchCharges(delivery, client_factory=mock_client(handler)),
                               'phaxio': PhaxioCharges(delivery, client_factory=mock_client(handler))}).step()
    row = routes.decision(attempt)
    assert (row['route'], row['provider_id'], row['reported_cost_micros']) == ('sinch', 'sinch', 135_000)
    assert seen == [(f'https://fax.api.sinch.com/v3/projects/project-1/faxes/{SINCH_FAX}', SINCH_AUTH)]
