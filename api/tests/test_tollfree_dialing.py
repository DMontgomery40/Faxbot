"""Dialing a recipient's approved toll-free number (migration 0027): the number each attempt dialed is evidence.

Synthetic numbers only: recipients +1 202 555 01xx, approved toll-free numbers +1 800 555 01xx. Approvals come
from a fake source installed in ``routing.alternates``; no provider, carrier or registry is contacted.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from api.tests.test_schema import database
from api.app import schema, schema_dialed
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_store import DeliveryConflict, OutboundStore
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import OutboundWorker
from api.app.routing import alternates, dialing
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.fallback import FallbackScheduler
from api.app.routing.plan import RoutePlanner, explain
from api.app.routing.provenance import dialed_view
from api.app.routing.store import CaptureTarget, RouteStore
from api.app.routing.transport import RoutedTransport


RECIPIENT, OTHER, TOLL_FREE = '+12025550123', '+12025550188', '+18005550100'
APPROVED_ON = datetime(2026, 10, 3)


class Approvals:
    """A fake ``routing.tollfree``: append-only rows per recipient number, the newest row in force."""

    def __init__(self):
        self.rows, self.reads = {}, []

    def approve(self, number, alternate, name='Example Clinic'):
        identity = uuid4().hex
        self.rows.setdefault(number, []).append({'id': identity, 'number': number, 'alternate': alternate,
                                                 'recipient_name': name, 'approved_at': APPROVED_ON,
                                                 'withdrawn_at': None, 'action': 'approved'})
        return identity

    def withdraw(self, number, when=datetime(2026, 10, 9)):
        for row in self.rows.get(number, []):
            if row['action'] == 'approved' and row['withdrawn_at'] is None:
                row['withdrawn_at'] = when
        self.rows.setdefault(number, []).append({'id': uuid4().hex, 'number': number, 'action': 'withdrawn'})

    def current_approval(self, number, *, engine=None, connection=None):
        self.reads.append(connection is not None)
        rows = self.rows.get(number) or []
        return dict(rows[-1]) if rows and rows[-1]['action'] == 'approved' else None

    def approval(self, approval_id, *, engine=None, connection=None):
        return next((dict(row) for rows in self.rows.values() for row in rows
                     if row['id'] == approval_id and row['action'] == 'approved'), None)


@pytest.fixture
def approvals():
    source = Approvals()
    alternates.install(source)
    yield source
    alternates.reset()


def card(provider, **rates):
    return RateCard(None, provider, 'outbound', provider.title(), 'USD', parse_amount(rates.get('minute', '0')),
                    parse_amount(rates.get('page', '0')), 0, 60, 0, None, datetime(2026, 10, 3))


@pytest.fixture
def toll(database, tmp_path, approvals):
    """Phaxio sends; SignalWire is a second route. Faxbot's own transport places every send with fake services."""
    schema.upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'signalwire', 'FAX_DISABLED': 'false',
        'PHAXIO_API_KEY': 'synthetic-key', 'PHAXIO_API_SECRET': 'synthetic-secret',
        'SIGNALWIRE_SPACE_URL': 'example.signalwire.com', 'SIGNALWIRE_PROJECT_ID': 'project-1',
        'SIGNALWIRE_API_TOKEN': 'synthetic-token', 'SIGNALWIRE_WEBHOOK_SIGNING_KEY': 'synthetic-signing-key',
        'PUBLIC_API_URL': 'https://faxbot.example.org', 'FAX_DATA_DIR': str(tmp_path)})
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key', 'api_secret': 'synthetic-secret'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    delivery, routes = OutboundStore(configuration), RouteStore(database)
    routes.replace_cards([card('phaxio', page='0.07'), card('signalwire', minute='0.0095')])
    return SimpleNamespace(configuration=configuration, delivery=delivery, routes=routes, snapshot=snapshot,
                           approvals=approvals, data=tmp_path, engine=database)


def accept(toll, to=RECIPIENT, pages=3):
    identity, now = uuid4().hex, datetime.utcnow()
    toll.configuration.accept_outbound(toll.snapshot.active, {'id': identity, 'to_number': to,
        'file_name': 'synthetic.txt', 'tiff_path': '', 'status': 'queued', 'pages': pages,
        'created_at': now, 'updated_at': now})
    (toll.data / (identity + '.pdf')).write_bytes(b'%PDF-synthetic-toll-free')
    return identity


class Runtime:
    @contextmanager
    def frame(self, revision):
        yield


class Services:
    """Fake Phaxio and SignalWire services: record the number each send dialed and answer as told."""

    def __init__(self, toll, monkeypatch):
        self.toll, self.sent, self.answers = toll, [], []
        monkeypatch.setattr('api.app.outbound_transport.service_from_profile', self.factory)

    def factory(self, profile):
        services = self

        class Service:
            def is_configured(self):
                return True

            async def send_fax(self, to, url, job_id, *, attempt_id):
                # The number is recorded before the durable submission marker, and never changes after it.
                assert services.dialed(attempt_id) == to
                assert services.toll.delivery.get(job_id)['state'] == 'submitting'
                services.sent.append((profile.configuration.provider_id, to, job_id))
                answer = services.answers.pop(0) if services.answers else 'queued'
                if isinstance(answer, Exception):
                    raise answer
                return {'provider_sid': uuid4().hex[:12], 'status': answer}
        return Service()

    def dialed(self, attempt_id):
        attempts = self.toll.delivery.attempts
        with self.toll.engine.connect() as connection:
            return connection.scalar(sa.select(attempts.c.dialed_number).where(attempts.c.id == attempt_id))


def worker(toll):
    return OutboundWorker(toll.delivery, RoutedTransport(CapturedTransport(toll.delivery, Runtime()), direct=None))


def attempt_rows(toll, job):
    attempts = toll.delivery.attempts
    with toll.engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(attempts).where(attempts.c.job_id == job)
                                                        .order_by(attempts.c.sequence)).mappings()]


def finish(toll, job, status='success'):
    attempt = toll.delivery.get(job)['attempt_id']
    profile = next(row['profile_id'] for row in attempt_rows(toll, job) if row['id'] == attempt)
    toll.delivery.observe(job, attempt_id=attempt, profile_id=profile, provider_sid=None, status=status,
                          event_key='synthetic-' + attempt)


def to_number(toll, job):
    jobs = toll.configuration.jobs
    with toll.engine.connect() as connection:
        return connection.scalar(sa.select(jobs.c.to_number).where(jobs.c.id == job))


# Numbers, terms and decisions --------------------------------------------------------------------------------

@pytest.mark.parametrize('number', ['+18005550100', '+18335550100', '+18445550100', '+18555550100',
                                    '+18665550100', '+18775550100', '+18885550100'])
def test_every_toll_free_code_in_service_is_toll_free(number):
    assert dialing.number_class(number) == 'toll_free' and dialing.display_number(number) == number[1:2] + '-' + \
        number[2:5] + '-' + number[5:8] + '-' + number[8:]


@pytest.mark.parametrize('number', ['+18225550100', '+18805550100', '+18815550100', '+18825550100', '+18835550100',
                                    '+18845550100', '+18855550100', '+18865550100', '+18875550100', '+18995550100',
                                    '+18000550100', '+18001550100', '+13035550100', '+442071838750', '', None,
                                    '+1800555010'])
def test_reserved_codes_local_numbers_and_other_countries_are_standard(number):
    # Pinned for the swap to one shared classifier (routing.destinations.classify): 822 and 880 to 887 are not in
    # service, and an exchange never starts with 0 or 1.
    assert dialing.number_class(number) == 'standard'


def test_the_shipped_terms_carry_each_routes_published_toll_free_price_with_its_source():
    terms = dialing.load_terms()
    telnyx = terms['sip-telnyx']
    assert (telnyx.reaches, telnyx.pricing, telnyx.card.per_minute_micros, telnyx.caller_id) == (
        'yes', 'own', 0, 'account_number')
    assert telnyx.source_url == 'https://telnyx.com/pricing/elastic-sip' and telnyx.advertised_on == '2026-10-07'
    assert terms['sip-anveo'].card.per_minute_micros == 1820 and terms['sip-anveo'].card.billing_increment_seconds == 1
    assert terms['sip-flowroute'].caller_id == 'local_number' and terms['sip-flowroute'].pricing == 'not_published'
    assert terms['phaxio'].pricing == 'same_as_card' and terms['signalwire'].pricing == 'not_published'
    assert all(entry.source_url and entry.advertised_on == '2026-10-07' for entry in terms.values())


def test_the_class_card_prices_a_toll_free_call_by_its_own_price_the_card_or_not_at_all():
    phaxio, signalwire, trunk = card('phaxio', page='0.07'), card('signalwire', minute='0.0095'), card('sip-telnyx', minute='0.005')
    assert dialing.class_card(phaxio, 'phaxio', TOLL_FREE) is phaxio
    assert dialing.class_card(phaxio, 'phaxio', RECIPIENT) is phaxio
    # Unknown stays unknown: an unpublished toll-free price is never the local price, and never zero.
    assert dialing.class_card(signalwire, 'signalwire', TOLL_FREE) is None
    free = dialing.class_card(trunk, 'sip', TOLL_FREE, sip_preset='telnyx')
    assert free.per_minute_micros == 0 and free.currency == 'USD'
    assert dialing.class_card(None, 'documo', TOLL_FREE) is None


def trunk_values(preset='telnyx', caller='+13035550100', dids='+13035550100'):
    return SimpleNamespace(sip_trunk_preset=preset, sip_trunk_caller_id=caller, sip_trunk_did_list=tuple(
        part for part in dids.split(',') if part))


def test_a_trunk_calls_toll_free_numbers_only_with_a_caller_id_its_carrier_accepts():
    assert dialing.reaches('sip', TOLL_FREE, trunk_values()) is True
    assert dialing.reaches('sip', RECIPIENT, trunk_values(caller='')) is True  # standard numbers need nothing
    assert dialing.caller_id_problem(trunk_values(caller=''), 'sip', dialing.terms_for('sip', 'telnyx')) == 'no_caller_id'
    assert dialing.reaches('sip', TOLL_FREE, trunk_values(caller='')) is False
    # Telnyx: a number on the account; only the trunk's own numbers can be checked here.
    assert dialing.reaches('sip', TOLL_FREE, trunk_values(caller='+13035550199')) is False
    assert dialing.reaches('sip', TOLL_FREE, trunk_values(caller='+13035550199', dids='')) is True
    # Flowroute: a local number, never a toll-free one.
    assert dialing.reaches('sip', TOLL_FREE, trunk_values('flowroute', caller='+18885550100', dids='')) is False
    assert dialing.reaches('sip', TOLL_FREE, trunk_values('flowroute', caller='+13035550100', dids='')) is True
    # A cloud fax service sends its own number.
    assert dialing.reaches('phaxio', TOLL_FREE, trunk_values(caller='')) is True


def test_a_route_that_publishes_it_cannot_call_toll_free_numbers_never_does(monkeypatch):
    refusing = dialing.TollFreeTerms('phaxio', 'Phaxio', 'no', 'not_published', None, None, None, None, None, None)
    monkeypatch.setattr(dialing, '_shipped', lambda: {'phaxio': refusing})
    assert dialing.reaches('phaxio', TOLL_FREE, trunk_values()) is False
    assert dialing.reaches('phaxio', RECIPIENT, trunk_values()) is True


def test_the_acceptance_decision_and_each_attempts_number():
    approval = alternates.Approval(TOLL_FREE, 'approval-1', RECIPIENT, 'Example Clinic', APPROVED_ON)
    job = {'to_number': RECIPIENT}
    assert alternates.dialed_number_for(job, facts=alternates.DialFacts(None)) == (RECIPIENT, None)
    assert alternates.dialed_number_for(job, facts=alternates.DialFacts(approval)) == (TOLL_FREE, 'approval-1')
    # No route of the fax may call it: the fax calls the number entered.
    assert alternates.dialed_number_for(job, facts=alternates.DialFacts(approval, lambda number: False)) == (
        RECIPIENT, None)
    assert alternates.attempt_number(RECIPIENT, alternate=TOLL_FREE, approval_id='a') == (TOLL_FREE, 'a')
    assert alternates.attempt_number(RECIPIENT, alternate=TOLL_FREE, approval_id='a', refused=True) == (RECIPIENT, None)
    assert alternates.attempt_number(RECIPIENT, alternate=TOLL_FREE, approval_id='a', route_reaches=False) == (
        RECIPIENT, None)


def test_an_unreadable_or_unusable_approval_means_the_number_entered(approvals):
    approvals.current_approval = lambda number, **where: (_ for _ in ()).throw(RuntimeError('synthetic'))
    assert alternates.current(RECIPIENT) is None
    approvals.current_approval = lambda number, **where: {'id': 'x', 'alternate': 'not a number'}
    assert alternates.current(RECIPIENT) is None
    approvals.current_approval = lambda number, **where: {'id': 'x', 'alternate': RECIPIENT}
    assert alternates.current(RECIPIENT) is None
    # A source that gives only the number still works; provenance then names "the recipient".
    approvals.current_approval = lambda number, **where: TOLL_FREE
    assert alternates.current(RECIPIENT) == alternates.Approval(TOLL_FREE, number=RECIPIENT)
    assert alternates.provenance(TOLL_FREE, RECIPIENT, alternates.Approval(TOLL_FREE)) == (
        'Dialed 1-800-555-0100, the toll-free number the recipient approved.')


# The planner -------------------------------------------------------------------------------------------------

def test_route_choice_calls_the_approved_number_where_it_can_and_prices_it_for_its_class(toll):
    values = toll.snapshot.active.values
    planner = RoutePlanner(toll.routes)
    dial = {'alternate': TOLL_FREE, 'refused': False}
    plan = planner.plan(to_number=RECIPIENT, bound='phaxio', values=values, pages=3, alternates=True, dial=dial)
    # SignalWire publishes no toll-free fax price, so Phaxio's known price wins and it calls the toll-free number.
    first = plan.first
    assert first.route.key == 'phaxio' and plan.number_for('phaxio') == TOLL_FREE
    assert first.estimated_cost_micros == 210_000
    assert next(choice for choice in plan.choices if choice.route.key == 'signalwire').estimated_cost_micros is None
    assert explain(first, plan.destination, plan.number_for('phaxio')) == (
        'The cheapest route with a known price that works reliably for this number, calling the toll-free '
        'number the recipient approved.')
    refused = planner.plan(to_number=RECIPIENT, bound='phaxio', values=values, pages=3, alternates=True,
                           dial={'alternate': TOLL_FREE, 'refused': True})
    assert refused.dialed == {} and refused.first.route.key == 'signalwire'


def test_a_route_that_cannot_call_toll_free_numbers_calls_the_number_entered(toll, monkeypatch):
    refusing = dialing.TollFreeTerms('phaxio', 'Phaxio', 'no', 'not_published', None, None, None, None, None, None)
    monkeypatch.setattr(dialing, '_shipped', lambda: {'phaxio': refusing})
    plan = RoutePlanner(toll.routes).plan(to_number=RECIPIENT, bound='phaxio', values=toll.snapshot.active.values,
                                          pages=3, alternates=True, dial={'alternate': TOLL_FREE, 'refused': False})
    assert plan.number_for('phaxio') == RECIPIENT and plan.number_for('signalwire') == TOLL_FREE


def test_the_telnyx_trunk_wins_an_approved_toll_free_number_because_those_calls_are_free(database, tmp_path):
    schema.upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.replace_cards([card('phaxio', page='0.07'), card('sip-telnyx', minute='0.005')])
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'sip', 'SIP_TRUNK_PRESET': 'telnyx',
        'SIP_TRUNK_CALLER_ID': '+13035550100', 'SIP_TRUNK_DIDS': '+13035550100', 'FAX_DISABLED': 'false'})
    plan = RoutePlanner(routes).plan(to_number=RECIPIENT, bound='phaxio', values=values, pages=3, alternates=True,
                                     dial={'alternate': TOLL_FREE, 'refused': False})
    assert (plan.first.route.key, plan.number_for('sip'), plan.first.estimated_cost_micros) == ('sip', TOLL_FREE, 0)
    # Without a caller ID the trunk calls the number entered, priced as an ordinary call.
    quiet = values.with_patch({'sip_trunk_caller_id': ''})
    plan = RoutePlanner(routes).plan(to_number=RECIPIENT, bound='phaxio', values=quiet, pages=3, alternates=True,
                                     dial={'alternate': TOLL_FREE, 'refused': False})
    assert plan.number_for('sip') == RECIPIENT and plan.number_for('phaxio') == TOLL_FREE


# Sending -----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_approved_fax_dials_the_toll_free_number_and_keeps_its_recipient(toll, monkeypatch):
    approval = toll.approvals.approve(RECIPIENT, TOLL_FREE)
    services = Services(toll, monkeypatch)
    job = accept(toll)
    # The approval is read once, through the acceptance transaction.
    assert toll.approvals.reads == [True]
    assert toll.delivery.dial_state(job) == {'alternate': TOLL_FREE, 'approval': approval, 'refused': False}
    assert await worker(toll).step() is True
    assert services.sent == [('phaxio', TOLL_FREE, job)]
    assert to_number(toll, job) == RECIPIENT
    [attempt] = attempt_rows(toll, job)
    assert (attempt['dialed_number'], attempt['dialed_approval'], attempt['phase']) == (TOLL_FREE, approval, 'in_progress')
    view = dialed_view(toll.engine, job)
    assert view['sentence'] == 'Dialed 1-800-555-0100, the toll-free number Example Clinic approved on October 3, 2026.'
    assert (view['display'], view['toll_free'], view['approved_on']) == ('1-800-555-0100', True, 'October 3, 2026')


@pytest.mark.asyncio
async def test_a_fax_without_an_approval_dials_its_own_number_and_shows_no_provenance(toll, monkeypatch):
    toll.approvals.approve(OTHER, TOLL_FREE)
    services = Services(toll, monkeypatch)
    job = accept(toll)
    await worker(toll).step()
    assert services.sent[0][1] == RECIPIENT and toll.delivery.dial_state(job)['alternate'] is None
    assert attempt_rows(toll, job)[0]['dialed_number'] == RECIPIENT and dialed_view(toll.engine, job) is None


@pytest.mark.asyncio
async def test_withdrawing_an_approval_changes_new_faxes_only(toll, monkeypatch):
    toll.approvals.approve(RECIPIENT, TOLL_FREE)
    services = Services(toll, monkeypatch)
    before = accept(toll)
    toll.approvals.withdraw(RECIPIENT)
    after = accept(toll)
    run = worker(toll)
    await run.step()
    # The number takes one call at a time: the first fax finishes before the second is dialed.
    finish(toll, before)
    await run.step()
    assert sorted(services.sent, key=lambda sent: sent[2] != before) == [('phaxio', TOLL_FREE, before),
                                                                         ('signalwire', RECIPIENT, after)]
    # The earlier fax says what it dialed, and that the approval has since been withdrawn.
    assert dialed_view(toll.engine, before)['sentence'] == (
        'Dialed 1-800-555-0100, the toll-free number Example Clinic approved on October 3, 2026; the approval was '
        'withdrawn on October 9, 2026, so new faxes call the number entered.')


@pytest.mark.asyncio
async def test_a_definite_failure_calling_the_toll_free_number_falls_back_to_the_number_entered(toll, monkeypatch):
    toll.approvals.approve(RECIPIENT, TOLL_FREE)
    services = Services(toll, monkeypatch)
    services.answers = ['failed', 'queued']
    job = accept(toll)
    run = worker(toll)
    await run.step()
    assert toll.delivery.get(job)['state'] == 'failed'
    assert FallbackScheduler(toll.delivery, toll.routes).step() is True
    await run.step()
    # The number entered is a different call, so the same route may place it.
    assert [sent[1] for sent in services.sent] == [TOLL_FREE, RECIPIENT]
    first, second = attempt_rows(toll, job)
    assert (first['dialed_number'], first['phase'], second['dialed_number']) == (TOLL_FREE, 'failed', RECIPIENT)
    assert second['dialed_approval'] is None and toll.delivery.dial_state(job)['refused'] is True
    assert dialed_view(toll.engine, job)['sentence'] == (
        'Dialed the number you entered, because the call to the approved number 1-800-555-0100 failed.')


@pytest.mark.asyncio
async def test_the_installed_policy_moves_a_refused_toll_free_fax_to_the_number_entered(toll, monkeypatch):
    from api.app.routing.fallback import FallbackPolicy
    monkeypatch.setattr(OutboundStore, 'fallback_policy', FallbackPolicy(FallbackScheduler(toll.delivery, toll.routes)))
    toll.approvals.approve(RECIPIENT, TOLL_FREE)
    services = Services(toll, monkeypatch)
    services.answers = ['failed', 'queued']
    job = accept(toll)
    run = worker(toll)
    await run.step()
    assert toll.delivery.get(job)['state'] == 'ready'
    await run.step()
    assert [sent[1] for sent in services.sent] == [TOLL_FREE, RECIPIENT]


@pytest.mark.asyncio
async def test_an_uncertain_toll_free_call_is_never_sent_again_to_either_number(toll, monkeypatch):
    toll.approvals.approve(RECIPIENT, TOLL_FREE)
    services = Services(toll, monkeypatch)
    services.answers = [ConnectionError('synthetic lost answer')]
    job = accept(toll)
    run = worker(toll)
    await run.step()
    assert toll.delivery.get(job)['state'] == 'reconciliation_required'
    assert FallbackScheduler(toll.delivery, toll.routes).step() is False
    assert await run.step() is False
    assert [sent[1] for sent in services.sent] == [TOLL_FREE]
    [attempt] = attempt_rows(toll, job)
    assert (attempt['dialed_number'], attempt['phase']) == (TOLL_FREE, 'uncertain')
    assert toll.delivery.dial_state(job)['refused'] is False


@pytest.mark.asyncio
async def test_a_restart_before_the_call_keeps_the_choice_made_at_acceptance(toll, monkeypatch):
    approval = toll.approvals.approve(RECIPIENT, TOLL_FREE)
    services = Services(toll, monkeypatch)
    job = accept(toll)
    claim = toll.delivery.claim('worker-that-stops')
    assert claim.job_id == job
    # The worker stops while preparing; its lease runs out and the approval is withdrawn meanwhile.
    toll.delivery.recover_expired(now=claim.expires_at + timedelta(seconds=1))
    toll.approvals.withdraw(RECIPIENT)
    await worker(toll).step()
    assert services.sent == [('phaxio', TOLL_FREE, job)]
    abandoned, placed = attempt_rows(toll, job)
    assert abandoned['phase'] == 'abandoned' and abandoned['dialed_number'] is None
    assert (placed['dialed_number'], placed['dialed_approval']) == (TOLL_FREE, approval)


def test_a_restart_after_the_submission_marker_leaves_the_number_and_sends_nothing(toll):
    toll.approvals.approve(RECIPIENT, TOLL_FREE)
    job = accept(toll)
    claim = toll.delivery.claim('worker-that-stops')
    assert toll.delivery.record_dialed(claim, TOLL_FREE, 'approval') is True
    assert toll.delivery.begin_submission(claim) is True
    # Once submitted, the dialed number can never change.
    with pytest.raises(DeliveryConflict):
        toll.delivery.record_dialed(claim, RECIPIENT)
    toll.delivery.recover_expired(now=claim.expires_at + timedelta(seconds=1))
    assert toll.delivery.get(job)['state'] == 'reconciliation_required'
    assert FallbackScheduler(toll.delivery, toll.routes).step() is False
    assert toll.delivery.claim('next-worker') is None
    [attempt] = attempt_rows(toll, job)
    assert (attempt['dialed_number'], attempt['phase']) == (TOLL_FREE, 'uncertain')


def test_a_number_is_recorded_only_while_its_worker_holds_the_lease(toll):
    job = accept(toll)
    claim = toll.delivery.claim('worker-one')
    toll.delivery.recover_expired(now=claim.expires_at + timedelta(seconds=1))
    with pytest.raises(DeliveryConflict):
        toll.delivery.record_dialed(claim, TOLL_FREE)
    with pytest.raises(ValueError):
        toll.delivery.record_dialed(claim, '')
    assert attempt_rows(toll, job)[0]['dialed_number'] is None


# Costs -------------------------------------------------------------------------------------------------------

def test_an_attempt_is_priced_by_the_class_of_the_number_it_dialed(toll):
    routes = RouteStore(toll.engine, sip_preset=lambda: 'telnyx')
    routes.replace_cards([card('sip-telnyx', minute='0.005'), card('signalwire', minute='0.0095')])
    now = datetime(2026, 10, 7, 12)

    numbers = iter(['+12025550131', '+12025550132', '+12025550133'])

    def target(provider, dialed):
        # A real fax and attempt for the ledger row; each to its own number, so each may be claimed at once.
        job = accept(toll, to=next(numbers))
        claim = toll.delivery.claim('worker-' + job[:8])
        return CaptureTarget(claim.attempt_id, job, RECIPIENT, provider, None, 'success', 2,
                             now - timedelta(seconds=90), now, False, dialed)
    assert routes.capture(target('sip', None))['estimated_cost_micros'] == 10_000  # two whole minutes at $0.005
    assert routes.capture(target('sip', TOLL_FREE))['estimated_cost_micros'] == 0  # Telnyx: toll-free calls are free
    unknown = routes.capture(target('signalwire', TOLL_FREE))
    assert (unknown['estimated_cost_micros'], unknown['currency'], unknown['rate_card_id']) == (None, None, None)


@pytest.mark.asyncio
async def test_savings_report_what_the_approved_toll_free_numbers_saved_on_their_own(toll, monkeypatch):
    from api.app.routing.savings import savings
    toll.approvals.approve(RECIPIENT, TOLL_FREE)
    services = Services(toll, monkeypatch)
    job = accept(toll)
    await worker(toll).step()
    finish(toll, job)
    from api.app.routing.capture import CostRecorder
    CostRecorder(toll.routes).step()
    part = savings(toll.routes, toll.engine)['toll_free']
    # Phaxio charges its page price for a toll-free number too: same cost, never a made-up saving.
    assert (part['faxes'], part['priced'], part['saved']) == (1, 1, {'USD': 0})
    assert part['sentence'] == ('1 fax called the toll-free number its recipient approved, which cost the same as '
                                'calling the number entered.')
    assert services.sent[0][1] == TOLL_FREE


def test_the_savings_sentence_names_the_saving_and_unknown_prices():
    assert alternates.savings_sentence({'faxes': 0, 'saved': {}, 'unpriced': 0, 'in_plan': 0}, 30) == (
        'No faxes called an approved toll-free number in the last 30 days.')
    assert alternates.savings_sentence({'faxes': 3, 'saved': {'USD': 30_000}, 'unpriced': 1, 'in_plan': 0}, 30) == (
        '3 faxes called the toll-free numbers their recipients approved: about $0.03 saved by using the approved '
        "toll-free numbers, whose owners pay for the calls. 1 of them has no price, because the route's toll-free "
        'price is not published.')


# The charge match --------------------------------------------------------------------------------------------

def test_a_carrier_charge_matches_the_number_the_attempt_dialed(toll):
    from api.app.routing.carriers import CarrierChargeStore, match_records
    from api.app.routing.telnyx import CarrierRecord
    toll.approvals.approve(RECIPIENT, TOLL_FREE)
    job = accept(toll)
    claim = toll.delivery.claim('worker-one')
    toll.delivery.record_dialed(claim, TOLL_FREE)
    start = datetime(2026, 10, 7, 12)
    carriers = CarrierChargeStore(toll.engine)
    with toll.engine.begin() as connection:
        # A call record that never learned the number it called (its submission event was lost).
        connection.execute(carriers.calls.insert().values(
            id='call-1', direction='outbound', call_id=claim.attempt_id, job_id=job, attempt_id=claim.attempt_id,
            trunk_preset='telnyx', did='+13035550100', caller='+13035550100', called=None, started_at=start,
            answered_at=start + timedelta(seconds=5), ended_at=start + timedelta(seconds=95), disposition='answered',
            t38='yes', fax_preference=1, created_at=start, updated_at=start))
    [row] = carriers.calls_between(start - timedelta(minutes=5), start + timedelta(minutes=5), 'telnyx')
    view = carriers.view(row)
    assert view.remote == TOLL_FREE

    def record(cld):
        return CarrierRecord(uuid4().hex, 'outbound', None, '+13035550100', cld, start, start + timedelta(seconds=6),
                             start + timedelta(seconds=94), 89, 120, 0, '0', 'USD')
    matched, ambiguous = match_records([view], [view], [record(TOLL_FREE)])
    assert list(matched) == ['call-1'] and not ambiguous
    matched, _ = match_records([view], [view], [record(RECIPIENT)])
    assert matched == {}


# Rate cards, NPPES and the migration ---------------------------------------------------------------------------

def test_prices_and_plans_list_each_sending_routes_toll_free_terms():
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_ROUTES': 'phaxio,signalwire', 'SIP_TRUNK_PRESET': 'telnyx',
        'SIP_TRUNK_CALLER_ID': '', 'FAX_DISABLED': 'false'})
    items = {item['provider_id']: item for item in dialing.terms_view(values)}
    assert set(items) == {'sip', 'phaxio', 'signalwire'}
    trunk = items['sip']
    assert (trunk['route'], trunk['price_text'], trunk['reaches']) == ('sip-telnyx', 'Free.', 'no')
    assert trunk['reach_text'].startswith('Set a caller ID under Providers → Carrier trunk')
    assert trunk['caller_id_text'] == 'A number on your account, or one the carrier verified.'
    assert items['phaxio']['price_text'] == 'The same as its sending price.'
    assert items['signalwire']['price_text'] == 'Not published.'
    anveo = dialing.price_text(dialing.load_terms()['sip-anveo'])
    assert anveo == '$0.00182 a minute, at least 1 seconds.'


NPPES = {'result_count': 1, 'results': [{
    'number': 1234567893, 'basic': {'organization_name': 'EXAMPLE CLINIC'},
    'addresses': [
        {'address_purpose': 'MAILING', 'address_1': '1 Example Way', 'city': 'DENVER', 'state': 'CO',
         'telephone_number': '303-555-0100', 'fax_number': '866-555-0142'},
        {'address_purpose': 'LOCATION', 'address_1': '2 Example Way', 'city': 'DENVER', 'state': 'CO',
         'telephone_number': '303-555-0101', 'fax_number': '303-555-0102'},
        {'address_purpose': 'LOCATION', 'address_1': '3 Example Way', 'city': 'DENVER', 'state': 'CO'}]}]}


def test_nppes_toll_free_fax_numbers_are_suggestions_with_their_evidence():
    from api.app.routing.nppes import suggested_tollfree
    asked = []
    found = suggested_tollfree('1234567893', fetch=lambda params: asked.append(params) or NPPES,
                               now=datetime(2026, 10, 7))
    assert asked == [{'number': '1234567893'}]
    assert found == [{'source': 'NPPES', 'read_at': datetime(2026, 10, 7), 'npi': '1234567893', 'name': 'EXAMPLE CLINIC', 'address_purpose': 'mailing address',
                      'address': '1 Example Way, DENVER, CO', 'fax_number': '+18665550142',
                      'evidence': 'NPPES record NPI 1234567893, mailing address, read October 7, 2026',
                      'source_url': 'https://npiregistry.cms.hhs.gov/api/?version=2.1&number=1234567893'}]
    with pytest.raises(ValueError):
        suggested_tollfree('12345')
    with pytest.raises(ValueError):
        suggested_tollfree(name='Example Clinic')
    by_name = []
    suggested_tollfree(name='Example Clinic', state='co', fetch=lambda params: by_name.append(params) or {})
    assert by_name == [{'organization_name': 'Example Clinic', 'limit': 20, 'state': 'CO'}]


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def test_0027_is_head_and_adds_empty_columns_that_downgrade(toll):
    assert schema.HEAD == schema_dialed.REVISION == '0027_dialed_number' and schema_dialed.TABLES == frozenset()
    job = accept(toll)
    assert toll.delivery.dial_state(job)['alternate'] is None
    with toll.engine.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    toll.engine.dispose()
    _downgrade(toll.engine, '0023_negotiation')
    assert not {'dialed_number', 'dialed_approval'} & _columns(toll.engine, 'outbound_attempts')
    assert not {'alternate_number', 'alternate_approval'} & _columns(toll.engine, 'outbound_deliveries')
    with toll.engine.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0023_negotiation'
        assert connection.scalar(sa.text('SELECT count(*) FROM outbound_deliveries')) == 1
    schema.upgrade_schema(toll.engine)
    assert {'dialed_number', 'dialed_approval'} <= _columns(toll.engine, 'outbound_attempts')
    with toll.engine.connect() as connection:
        assert connection.scalar(sa.text('SELECT count(*) FROM outbound_attempts WHERE dialed_number IS NOT NULL')) == 0
