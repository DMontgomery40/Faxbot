"""Route decisions and cost capture against real durable delivery stores."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

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


def test_seed_waits_for_a_rate_card_save_in_progress(routes, tmp_path):
    """An operator save that is mid-write when the seed runs is never replaced by the shipped set.

    The seed's emptiness check and insert share one serialized write transaction,
    so it only runs after the save commits, sees the table is not empty, and stops.
    """
    import json
    import threading
    import time
    from api.app.routing.database import write_transaction
    from api.app.routing.seed import load_cards
    path = tmp_path / 'rate_cards.json'
    path.write_text(json.dumps({'carriers': {
        'sip': {'label': 'Telnyx Elastic SIP', 'per_minute': '0.005', 'rounding': 'whole_minute',
                'source_url': 'https://telnyx.com/pricing/elastic-sip', 'advertised_on': '2026-10-03'}}}))
    shipped = load_cards(path)
    outcome = {}
    seed = threading.Thread(target=lambda: outcome.setdefault('seeded', routes.seed_cards(shipped)))
    with write_transaction(routes.engine) as connection:
        seed.start()
        time.sleep(0.5)  # The seed has started and must now be waiting for this save.
        routes._write_cards(connection, [phaxio_card()])
    seed.join(timeout=20)
    assert not seed.is_alive()
    assert outcome['seeded'] is False
    assert [card.provider_id for card in routes.current_cards()] == ['phaxio']


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
        'phaxio', 'configured', 'phaxio', 'pending')
    assert decision['destination'] == '+12025550123'
    assert CostRecorder(routes).step() is False
    priced = routes.decision(attempt)
    # Three accepted pages at 0.07 each.
    assert (priced['outcome'], priced['estimated_cost_micros'], priced['currency'], priced['billed_pages']) == (
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
    assert priced['estimated_cost_micros'] == 210_000
    # A late provider receipt settles the attempt; the recorder prices it again.
    claim = replace_claim(delivery, job)
    delivery.record_receipt(claim, provider_sid='remote-late', status='failed')
    assert len(routes.pending_captures()) == 1
    CostRecorder(routes).step()
    priced = routes.decision(delivery.get(job)['attempt_id'])
    assert priced['outcome'] == 'failed' and priced['estimated_cost_micros'] == 0


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


def signalwire_installation(database, tmp_path):
    from api.app.schema import upgrade_schema
    from api.app.config_store import ConfigurationStore
    from api.app.config_values import ConfigurationValues
    from api.app.config_profiles import ProviderConfiguration
    from api.app.outbound_store import OutboundStore
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    profile = ProviderConfiguration('signalwire', credentials={'api_token': 'synthetic-token'},
                                    settings={'space_url': 'example.signalwire.com', 'project_id': 'project-1'})
    snapshot = configuration.initialize(ConfigurationValues.from_environment({'FAX_BACKEND': 'signalwire'}),
                                        actor='test', providers={'outbound': profile})
    delivery, routes = OutboundStore(configuration), RouteStore(database)
    routes.replace_cards([phaxio_card(provider_id='signalwire', label='SignalWire', per_page_micros=0,
                                      per_minute_micros=9500)])
    return configuration, delivery, routes, snapshot


@pytest.mark.asyncio
async def test_late_provider_charges_are_reconciled_without_reopening_delivery(database, tmp_path):
    import httpx
    from api.app.routing.billing import BillingReconciler
    from api.app.routing.charges import SignalWireCharges, parse_signalwire_charge
    configuration, delivery, routes, snapshot = signalwire_installation(database, tmp_path)
    job = accept((configuration, delivery, snapshot))
    await OutboundWorker(delivery, RoutedTransport(Inner(delivery, SubmissionReceipt('FX123', 'success')))).step()
    CostRecorder(routes).step()
    attempt = delivery.get(job)['attempt_id']
    delivered = delivery.get(job)
    history = delivery.history(job)
    prices = [None, '-0.0285', '-0.0285', '-0.019']
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={'sid': 'FX123', 'status': 'delivered', 'price': prices[len(seen) - 1],
                                         'price_unit': 'usd', 'duration': 150, 'num_pages': '3'})
    source = SignalWireCharges(delivery, client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    billing = BillingReconciler(routes, {'signalwire': source}, retry=timedelta(minutes=10))
    start = datetime.utcnow()
    # Delivered with no price yet: unknown stays unknown, never the estimate.
    billing.step(now=start)
    row = routes.decision(attempt)
    assert row['reported_cost_micros'] is None and row['settled_cost_micros'] is None
    assert row['estimated_cost_micros'] == 0 and row['billing_checks'] == 1  # instant test call
    billing.step(now=start + timedelta(minutes=1))
    assert len(seen) == 1  # Not asked again before the retry interval.
    billing.step(now=start + timedelta(minutes=11))
    row = routes.decision(attempt)
    assert (row['reported_cost_micros'], row['reported_currency'], row['settled_cost_micros']) == (28_500, 'USD', None)
    # The same charge again is a duplicate.
    assert routes.ingest_charge(attempt, provider_id='signalwire', charge_id='FX123', amount_micros=28_500,
                                currency='USD', billed_seconds=150) == 'duplicate'
    billing.step(now=start + timedelta(minutes=22))
    with routes.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(routes.charges)).scalar_one() == 1
    # A day after the call ended, a corrected price settles the charge.
    billing.step(now=start + timedelta(hours=25))
    row = routes.decision(attempt)
    assert (row['reported_cost_micros'], row['settled_cost_micros']) == (19_000, 19_000)
    assert row['settled_at'] is not None
    assert seen == ['https://example.signalwire.com/api/laml/2010-04-01/Accounts/project-1/Faxes/FX123.json'] * 4
    billing.step(now=start + timedelta(hours=26))
    assert len(seen) == 4  # Settled charges are not read again.
    assert delivery.get(job) == delivered and delivery.history(job) == history
    totals = routes.cost_totals(start - timedelta(days=1))[0]
    assert totals['settled_cost_micros'] == {'USD': 19_000} and totals['unreported'] == 0
    assert parse_signalwire_charge({'price': None, 'price_unit': 'USD'}) is None
    assert parse_signalwire_charge({'price': 'free', 'price_unit': 'USD'}) is None


def test_whole_minute_rounding_is_per_call_never_on_averages(installation, routes):
    from api.app.routing.store import CaptureTarget
    routes.replace_cards([phaxio_card(per_page_micros=0, per_minute_micros=parse_amount('0.01'))])
    start = datetime(2026, 10, 3, 12)
    billed = []
    for index, seconds in enumerate((59, 61)):
        job = accept(installation)
        attempt = uuid4().hex
        with routes.engine.begin() as connection:
            connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='success',
                created_at=start, submitted_at=start, completed_at=start + timedelta(seconds=seconds)))
        target = CaptureTarget(attempt, job, '+12025550123', 'phaxio', None, 'success', 1, start,
                               start + timedelta(seconds=seconds), False)
        billed.append(routes.capture(target)['billed_seconds'])
    # 59 s bills one minute and 61 s bills two: three minutes, not two minutes of average.
    assert billed == [60, 120]
    totals = routes.cost_totals(start - timedelta(days=1))[0]
    assert totals['billed_seconds'] == 180 and totals['cost_micros'] == {'USD': 30_000}


def test_an_attempt_whose_call_length_is_unknown_has_no_estimate_not_the_call_fee_alone(installation, routes):
    """Unknown cost is not zero cost: without a finish time a per-minute card cannot price the call."""
    from api.app.routing.store import CaptureTarget
    routes.replace_cards([phaxio_card(per_page_micros=0, per_minute_micros=parse_amount('0.01'),
                                      per_call_micros=parse_amount('0.02'))])
    start = datetime(2026, 10, 3, 12)
    job, attempt = accept(installation), uuid4().hex
    with routes.engine.begin() as connection:
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='failed',
                                                           created_at=start, submitted_at=start, completed_at=None))
    values = routes.capture(CaptureTarget(attempt, job, '+12025550123', 'phaxio', None, 'failed', 1, start, None, False))
    assert values['estimated_cost_micros'] is None and values['billed_seconds'] is None
    assert values['cost_basis'] is None and values['currency'] is None
    assert routes.decision(attempt)['estimated_cost_micros'] is None


def test_shipped_rate_cards_seed_only_an_empty_table(routes, tmp_path):
    import json
    from api.app.routing.seed import load_cards
    path = tmp_path / 'rate_cards.json'
    path.write_text(json.dumps({'carriers': {
        'sip': {'label': 'Telnyx Elastic SIP', 'per_minute': '0.005', 'rounding': 'whole_minute',
                'source_url': 'https://telnyx.com/pricing/elastic-sip', 'advertised_on': '2026-10-03'},
        'flowroute': {'label': 'Flowroute SIP', 'per_minute': '0.00833', 'rounding': '6_second',
                      'advertised_on': '2026-10-03'},
        'broken': {'per_minute': 'cheap'},
    }}))
    cards = load_cards(path)
    assert [(card.provider_id, card.billing_increment_seconds) for card in cards] == [('sip', 60), ('flowroute', 6)]
    assert load_cards(tmp_path / 'missing.json') == []
    assert routes.seed_cards(cards) is True
    assert {card.provider_id for card in routes.current_cards()} == {'sip', 'flowroute'}
    routes.replace_cards([phaxio_card()])
    assert routes.seed_cards(cards) is False  # Operator edits stay authoritative.
    assert [card.provider_id for card in routes.current_cards()] == ['phaxio']


@pytest.mark.asyncio
async def test_measured_call_seconds_are_preferred_over_faxbot_timing(installation, routes):
    _, delivery, _ = installation
    routes.replace_cards([phaxio_card(per_page_micros=0, per_minute_micros=parse_amount('0.01'))])
    job = accept(installation)
    await OutboundWorker(delivery, RoutedTransport(Inner(delivery))).step()
    seen = []

    def observed(target):
        seen.append(target.attempt_id)
        return 125
    CostRecorder(routes, observed_seconds=observed).step()
    priced = routes.decision(delivery.get(job)['attempt_id'])
    assert seen == [delivery.get(job)['attempt_id']]
    assert (priced['billed_seconds'], priced['estimated_cost_micros'], priced['cost_basis']) == (180, 30_000, 'measured')


def test_native_sip_faxes_are_priced_by_the_trunk_carriers_card(installation):
    """With a Telnyx trunk, a 'sip' attempt uses the sip-telnyx card and its 60/60 rounding."""
    from api.app.routing.store import CaptureTarget
    configuration, _, _ = installation
    preset = {'value': 'telnyx'}
    routes = RouteStore(configuration.engine, sip_preset=lambda: preset['value'])
    generic = phaxio_card(provider_id='sip', label='Any SIP trunk', per_page_micros=0,
                          per_minute_micros=parse_amount('0.02'), billing_increment_seconds=6)
    telnyx = phaxio_card(provider_id='sip-telnyx', label='Telnyx SIP trunk', per_page_micros=0,
                         per_minute_micros=parse_amount('0.005'), billing_increment_seconds=60, minimum_seconds=60)
    routes.replace_cards([generic, telnyx])
    assert routes.card_for('sip').provider_id == 'sip-telnyx'
    assert routes.card_for('phaxio') is None
    preset['value'] = 'flowroute'  # no Flowroute card: the plain sip card still prices it
    assert routes.card_for('sip').provider_id == 'sip'
    preset['value'] = ''
    assert routes.card_for('sip').provider_id == 'sip'
    preset['value'] = 'telnyx'
    start = datetime(2026, 10, 3, 12)
    job = accept(installation)
    attempt = uuid4().hex
    with routes.engine.begin() as connection:
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='success',
            created_at=start, submitted_at=start, completed_at=start + timedelta(seconds=90)))
    target = CaptureTarget(attempt, job, '+12025550123', 'sip', None, 'success', 2, start,
                           start + timedelta(seconds=90), False)
    # 65 measured seconds on the trunk bill as two whole minutes at $0.005.
    priced = routes.capture(target, observed_seconds=65)
    assert (priced['billed_seconds'], priced['estimated_cost_micros']) == (120, 10_000)


def test_the_configured_sip_preset_comes_from_active_settings():
    from api.app.config import use_configuration
    from api.app.config_values import ConfigurationValues
    from api.app.routing.store import _configured_sip_preset
    with use_configuration(ConfigurationValues.from_environment({'SIP_TRUNK_PRESET': 'telnyx'})):
        assert _configured_sip_preset() == 'telnyx'
    with use_configuration(ConfigurationValues.from_environment({})):
        assert _configured_sip_preset() == ''
