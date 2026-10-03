"""Route decisions and cost capture against real durable delivery stores."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from api.tests.test_outbound_store import installation, accept
from api.tests.test_schema import database
from api.app.outbound_worker import OutboundWorker, SubmissionReceipt, PreparationFailure
from api.app.routing.capture import CostRecorder
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.store import RouteStore, RoutingConflict, RoutingInputError
from api.app.routing.transport import DirectRefused, RoutedTransport


CAPTURED = datetime(2026, 10, 3)


def phaxio_card(**changes):
    values = dict(id=None, provider_id='phaxio', direction='outbound', label='Phaxio', currency='USD',
                  per_minute_micros=0, per_page_micros=parse_amount('0.07'), per_call_micros=0,
                  billing_increment_seconds=60, minimum_seconds=0, source_url='https://www.phaxio.com/pricing',
                  captured_on=CAPTURED)
    values.update(changes)
    return RateCard(**values)


class Inner:
    """Stands in for the captured provider transport; counts real submissions."""
    def __init__(self, store, receipt=None, fail_prepare=False):
        self.store = store
        self.receipt = receipt or SubmissionReceipt('remote-one', 'success')
        self.submissions = 0
        self.prepared = 0
        self.fail_prepare = fail_prepare

    @asynccontextmanager
    async def prepare(self, claim):
        self.prepared += 1
        if self.fail_prepare:
            raise PreparationFailure('provider_unavailable')
        yield self

    async def submit(self):
        self.submissions += 1
        return self.receipt


@pytest.fixture
def routes(installation):
    configuration, delivery, _ = installation
    return RouteStore(configuration.engine)


def test_rate_cards_are_versioned_and_unchanged_cards_keep_their_identity(routes):
    first = routes.replace_cards([phaxio_card()])
    again = routes.replace_cards([phaxio_card()])
    assert [card.id for card in again] == [card.id for card in first]
    changed = routes.replace_cards([phaxio_card(per_page_micros=parse_amount('0.05'))])
    assert changed[0].id != first[0].id and changed[0].per_page_micros == 50_000
    with routes.engine.connect() as connection:
        rows = connection.execute(sa.select(routes.cards.c.id, routes.cards.c.superseded_at)).all()
    assert len(rows) == 2 and sum(1 for _, superseded in rows if superseded is None) == 1
    assert routes.replace_cards([]) == []
    with pytest.raises(RoutingInputError):
        routes.replace_cards([phaxio_card(), phaxio_card(label='Again')])


def test_destination_updates_are_versioned(routes):
    row = routes.update_destination('+15550100001', display_name='County clinic', preferred_route='sip')
    assert row['version'] == 1 and row['preferred_route'] == 'sip'
    row = routes.update_destination('+15550100001', expected_version=1, accepts_references=True)
    assert row['version'] == 2 and row['accepts_references'] == 1 and row['display_name'] == 'County clinic'
    with pytest.raises(RoutingConflict):
        routes.update_destination('+15550100001', expected_version=1, notes='stale')
    with pytest.raises(RoutingInputError):
        routes.update_destination('+15550100001', preferred_route='Not A Route!')


@pytest.mark.asyncio
async def test_worker_records_route_and_reason_then_cost_is_captured(installation, routes):
    _, delivery, _ = installation
    routes.replace_cards([phaxio_card()])
    job = accept(installation)
    inner = Inner(delivery)
    assert await OutboundWorker(delivery, RoutedTransport(inner)).step() is True
    assert inner.submissions == 1
    assert delivery.get(job)['state'] == 'success'
    attempt = delivery.get(job)['attempt_id']
    decision = routes.decision(attempt)
    assert (decision['route'], decision['route_reason'], decision['provider_id'], decision['outcome']) == (
        'phaxio', 'cheapest', 'phaxio', 'pending')
    assert decision['destination'] == '+12025550123'
    assert CostRecorder(routes).step() is False
    priced = routes.decision(attempt)
    # Three accepted pages at 0.07 each.
    assert (priced['outcome'], priced['computed_cost_micros'], priced['currency'], priced['billed_pages']) == (
        'success', 210_000, 'USD', 3)
    assert priced['cost_basis'] == 'estimated' and priced['rate_card_id'] is not None
    assert routes.pending_captures() == []
    totals = routes.cost_totals(datetime.utcnow() - timedelta(days=1))
    assert totals[0]['provider_id'] == 'phaxio' and totals[0]['cost_micros'] == {'USD': 210_000}


@pytest.mark.asyncio
async def test_uncertain_attempt_is_priced_as_if_sent_and_never_resubmitted(installation, routes):
    _, delivery, _ = installation
    routes.replace_cards([phaxio_card()])
    job = accept(installation)

    class Lost(Inner):
        async def submit(self):
            self.submissions += 1
            raise TimeoutError('response lost')
    inner = Lost(delivery)
    await OutboundWorker(delivery, RoutedTransport(inner)).step()
    assert delivery.get(job)['state'] == 'reconciliation_required'
    assert await OutboundWorker(delivery, RoutedTransport(inner)).step() is False
    assert inner.submissions == 1
    CostRecorder(routes).step()
    priced = routes.decision(delivery.get(job)['attempt_id'])
    assert priced['outcome'] == 'uncertain' and priced['cost_basis'] == 'estimated'
    assert priced['computed_cost_micros'] == 210_000
    # A late provider receipt settles the attempt; the recorder prices it again.
    claim = replace_claim(delivery, job)
    delivery.record_receipt(claim, provider_sid='remote-late', status='failed')
    assert len(routes.pending_captures()) == 1
    CostRecorder(routes).step()
    priced = routes.decision(delivery.get(job)['attempt_id'])
    assert priced['outcome'] == 'failed' and priced['computed_cost_micros'] == 0


def replace_claim(delivery, job):
    from api.app.outbound_store import DispatchClaim
    row = delivery.get(job)
    with delivery.configuration.engine.connect() as connection:
        profile = connection.execute(sa.select(delivery.attempts.c.profile_id).where(
            delivery.attempts.c.id == row['attempt_id'])).scalar_one()
    return DispatchClaim(job, row['attempt_id'], profile, row['claim_owner'], row['claim_token'], datetime.utcnow())


@pytest.mark.asyncio
async def test_failed_definite_attempt_counts_against_route_reliability(installation, routes):
    _, delivery, _ = installation
    for _ in range(3):
        accept(installation)
    inner = Inner(delivery, SubmissionReceipt('remote', 'failed'))
    worker = OutboundWorker(delivery, RoutedTransport(inner))
    while await worker.step():
        pass
    CostRecorder(routes).step()
    stats = routes.route_stats('+12025550123')
    assert stats['phaxio'].attempts == 3 and stats['phaxio'].successes == 0
    assert inner.submissions == 3


@pytest.mark.asyncio
async def test_route_choice_failure_falls_back_to_the_outbound_provider(installation, monkeypatch):
    _, delivery, _ = installation
    job = accept(installation)
    inner = Inner(delivery)
    transport = RoutedTransport(inner)

    def broken(claim):
        raise RuntimeError('synthetic route storage failure')
    monkeypatch.setattr(transport, '_plan', broken)
    assert await OutboundWorker(delivery, transport).step() is True
    assert inner.submissions == 1 and delivery.get(job)['state'] == 'success'


@pytest.mark.asyncio
async def test_preparation_failure_semantics_are_unchanged(installation, routes):
    _, delivery, _ = installation
    job = accept(installation)
    inner = Inner(delivery, fail_prepare=True)
    await OutboundWorker(delivery, RoutedTransport(inner)).step()
    assert delivery.get(job)['state'] == 'failed' and inner.submissions == 0


class Direct:
    """A direct route double: refuses before acceptance or returns a receipt."""
    def __init__(self, outcome):
        self.outcome = outcome
        self.submissions = 0

    def ready(self):
        return True

    @asynccontextmanager
    async def prepare(self, claim, plan, job):
        yield self

    async def submit(self):
        self.submissions += 1
        if self.outcome == 'refused':
            raise DirectRefused('partner offline')
        if self.outcome == 'lost':
            raise TimeoutError('receipt lost')
        return SubmissionReceipt(None, 'success')


def verified_peer(routes, number='+12025550123'):
    now = datetime.utcnow()
    with routes.engine.begin() as connection:
        connection.execute(routes.peers.insert().values(
            id='peer-1', organization='County Clinic', phone_number=number, endpoint_url='https://clinic.invalid',
            signing_key='s' * 43, exchange_key='x' * 43, state='verified', challenge_failures=0,
            verified_at=now, version=1, created_at=now, updated_at=now))


def direct_installation(installation):
    configuration, delivery, snapshot = installation
    values = snapshot.active.values.with_patch({'direct_delivery_enabled': True})
    return configuration.apply(snapshot, values, actor='test', restart_required=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome, state, submissions, route', [
    ('accepted', 'success', (1, 0), 'direct'),
    ('refused', 'success', (1, 1), 'phaxio'),
    ('lost', 'reconciliation_required', (1, 0), 'direct'),
])
async def test_direct_route_goes_first_and_only_a_definite_refusal_falls_back(
        installation, routes, outcome, state, submissions, route):
    configuration, delivery, _ = installation
    snapshot = direct_installation(installation)
    verified_peer(routes)
    job = accept((configuration, delivery, snapshot))
    inner, direct = Inner(delivery), Direct(outcome)
    await OutboundWorker(delivery, RoutedTransport(inner, direct=direct)).step()
    assert delivery.get(job)['state'] == state
    assert (direct.submissions, inner.submissions) == submissions
    assert routes.decision(delivery.get(job)['attempt_id'])['route'] == route
    assert await OutboundWorker(delivery, RoutedTransport(inner, direct=direct)).step() is False


@pytest.mark.asyncio
async def test_signalwire_reported_charge_is_stored_beside_the_computed_cost(database, tmp_path):
    import httpx
    from api.app.schema import upgrade_schema
    from api.app.config_store import ConfigurationStore
    from api.app.config_values import ConfigurationValues
    from api.app.config_profiles import ProviderConfiguration
    from api.app.outbound_store import OutboundStore
    from api.app.routing.charges import SignalWireCharges, parse_signalwire_charge
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    profile = ProviderConfiguration('signalwire', credentials={'api_token': 'synthetic-token'},
                                    settings={'space_url': 'example.signalwire.com', 'project_id': 'project-1'})
    snapshot = configuration.initialize(ConfigurationValues.from_environment({'FAX_BACKEND': 'signalwire'}),
                                        actor='test', providers={'outbound': profile})
    delivery, routes = OutboundStore(configuration), RouteStore(database)
    routes.replace_cards([phaxio_card(provider_id='signalwire', label='SignalWire', per_page_micros=0,
                                      per_minute_micros=9500)])
    job = accept((configuration, delivery, snapshot))
    await OutboundWorker(delivery, RoutedTransport(Inner(delivery, SubmissionReceipt('FX123', 'success')))).step()
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers['authorization'].startswith('Basic ')))
        return httpx.Response(200, json={'sid': 'FX123', 'status': 'delivered', 'price': '-0.0285',
                                         'price_unit': 'usd', 'duration': 150, 'num_pages': '3'})
    reporter = SignalWireCharges(configuration,
                                 client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    CostRecorder(routes, reporter=reporter).step()
    priced = routes.decision(delivery.get(job)['attempt_id'])
    assert seen == [('https://example.signalwire.com/api/laml/2010-04-01/Accounts/project-1/Faxes/FX123.json', True)]
    # 150 reported seconds bill as 3 whole minutes at 0.0095.
    assert (priced['reported_cost_micros'], priced['computed_cost_micros'], priced['billed_seconds']) == (
        28_500, 28_500, 180)
    assert priced['cost_basis'] == 'measured'
    assert parse_signalwire_charge({'price': None, 'price_unit': 'USD'}) is None
    assert parse_signalwire_charge({'price': 'free', 'price_unit': 'USD'}) is None
