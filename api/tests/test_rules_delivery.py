"""Sending rules in delivery: the envelope decided at acceptance, chosen within at each attempt (SQLite, PostgreSQL).

Every account, number and fax here is synthetic. The provider doubles never
contact a provider; they record which account each submission used.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.tests.test_plan_budget import plans  # noqa: F401 (fixture)
from api.tests.test_routing_multiprovider import multi  # noqa: F401 (fixture)
from api.tests.test_partner_relay_terms import sender  # noqa: F401 (fixture)
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_store import DeliveryConflict, OutboundStore
from api.app.outbound_worker import OutboundWorker, PreparationFailure, SubmissionReceipt
from api.app.routing import envelope as envelopes, rules_acceptance
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.fallback import FallbackPolicy, FallbackScheduler
from api.app.routing.holds import HoldConflict, HoldForbidden, HoldStore
from api.app.routing.plan import RoutePlanner
from api.app.routing.route_view import apply_to_waiting, fax_route
from api.app.routing.store import RouteStore
from api.app.routing.transport import RoutedTransport
from api.app.rules.store import RuleStore


TO = '+12025550123'
BASE = {
    'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false',
    'PHAXIO_API_KEY': 'synthetic-key', 'PHAXIO_API_SECRET': 'synthetic-secret',
    'SIGNALWIRE_SPACE_URL': 'example.signalwire.com', 'SIGNALWIRE_PROJECT_ID': 'project-1',
    'SIGNALWIRE_API_TOKEN': 'synthetic-token', 'SIGNALWIRE_WEBHOOK_SIGNING_KEY': 'synthetic-signing-key',
    'SIGNALWIRE_FAX_FROM_E164': '+13035550111', 'PUBLIC_API_URL': 'https://faxbot.example.org',
}
# SignalWire is set up but not in FAX_OUTBOUND_ROUTES: only a rule can send by it.
UNLISTED = {**BASE, 'FAX_OUTBOUND_ROUTES': ''}
# Today's multi-route installation: SignalWire is an extra route the automatic choice may use.
LISTED = {**BASE, 'FAX_OUTBOUND_ROUTES': 'signalwire'}
ANNE = SimpleNamespace(principal_id='person-anne', credential=None)
BEN = SimpleNamespace(principal_id='person-ben', credential=None)


def card(provider, **rates):
    return RateCard(None, provider, 'outbound', provider.title(), 'USD', parse_amount(rates.get('minute', '0')),
                    parse_amount(rates.get('page', '0')), 0, 60, 0, None, datetime(2026, 10, 3))


def rule(rule_id, then, when=None, **extra):
    return {'id': rule_id, 'name': f'Rule {rule_id}', 'on': True, 'when': when or {}, 'then': then, **extra}


def installation(database, tmp_path, environment, outbound=None):
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({**environment, 'FAX_DATA_DIR': str(tmp_path)})
    outbound = outbound or ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key',
                                                                         'api_secret': 'synthetic-secret'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': outbound})
    routes = RouteStore(database)
    routes.replace_cards([card('phaxio', page='0.07'), card('signalwire', minute='0.0095')])
    return SimpleNamespace(configuration=configuration, delivery=OutboundStore(configuration), routes=routes,
                           snapshot=snapshot, rules=RuleStore(database), engine=database, tmp=tmp_path)


@pytest.fixture
def ruled(database, tmp_path):
    return installation(database, tmp_path, UNLISTED)


@pytest.fixture
def listed(database, tmp_path):
    return installation(database, tmp_path, LISTED)


def publish(env, document):
    current = env.rules.draft('organization', '')
    draft = env.rules.save_draft('organization', '', document, expected_version=current['version'] if current else 0)
    active = env.rules.active('organization', '')
    env.rules.publish('organization', '', expected_active_revision=active['number'] if active else None,
                      expected_draft_version=draft['version'])


def accept(env, *, to=TO, pages=3, actor=ANNE, document_sha256=None, **facts):
    """Accept one fax the way POST /fax does: facts and a preview first, then the decision in the acceptance
    transaction with the fax and its binding."""
    plan = rules_acceptance.prepare(env.engine, env.snapshot.active, actor=actor, destination=to, pages=pages,
                                    document_sha256=document_sha256, **facts)
    identity, now = uuid4().hex, datetime.utcnow()
    job = {'id': identity, 'to_number': to, 'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
           'pages': pages, 'created_at': now, 'updated_at': now}
    with env.configuration._locked() as connection:
        env.configuration._accept_outbound_on(connection, env.snapshot.active, job)
        rules_acceptance.recorder(plan, identity, actor)(connection, now)
    return identity


class Inner:
    """Captured-transport double that records which account each submission used."""

    def __init__(self, store, receipts=()):
        self.store, self.receipts = store, list(receipts)
        self.used, self.fail_for = [], set()

    @asynccontextmanager
    async def prepare(self, claim):
        _, profile, _ = self.store.load_dispatch(claim)
        if profile.configuration.provider_id in self.fail_for:
            raise PreparationFailure('provider_unavailable')
        self.current = profile.configuration.provider_id
        yield self

    async def submit(self):
        self.used.append(self.current)
        return self.receipts.pop(0) if self.receipts else SubmissionReceipt('FX' + uuid4().hex[:8], 'in_progress')


def holds(env, job, state='open'):
    return HoldStore(env.delivery).holds(state=state, job_id=job)


def choice(env, attempt):
    with env.engine.connect() as connection:
        return envelopes.choice_on(connection, attempt)


# Acceptance and parity ------------------------------------------------------------------------------------------

def plan_view(env, job, pinned):
    revision, profile = env.configuration.outbound_context(job)
    plan = RoutePlanner(env.routes).plan(to_number=TO, bound='phaxio', values=revision.values, pages=3,
                                         alternates=True, pinned=pinned)
    return [(item.route.key, item.reason, item.estimated_cost_micros) for item in plan.choices]


def test_with_no_rules_every_fax_gets_the_automatic_decision_and_the_plan_is_unchanged(listed):
    """The parity matrix: no rules, an empty published document and a fax accepted before rules plan alike."""
    plain = accept(listed)
    pinned = envelopes.load(listed.engine, plain)
    assert pinned.decision.outcome == 'route' and pinned.envelope.mode == 'automatic'
    assert pinned.envelope.accounts == ('phaxio', 'signalwire') and not pinned.strict
    before_rules = plan_view(listed, plain, None)
    assert plan_view(listed, plain, pinned) == before_rules
    publish(listed, {'format': 1, 'limits': [], 'routes': []})
    empty = accept(listed)
    assert plan_view(listed, empty, envelopes.load(listed.engine, empty)) == before_rules
    assert before_rules[0][0] == 'signalwire'  # the cheaper route, exactly as before rules


@pytest.mark.asyncio
async def test_a_rule_sends_by_an_account_outside_the_routes_list_and_its_results_authenticate(ruled):
    publish(ruled, {'format': 1, 'routes': [rule('r-sw', {'use': 'signalwire'})]})
    job = accept(ruled)
    pinned = envelopes.load(ruled.engine, job)
    assert (pinned.envelope.mode, pinned.envelope.accounts, pinned.strict) == ('one', ('signalwire',), True)
    inner = Inner(ruled.delivery)
    assert await OutboundWorker(ruled.delivery, RoutedTransport(inner, direct=None)).step() is True
    assert inner.used == ['signalwire']
    attempt = ruled.delivery.get(job)['attempt_id']
    recorded = choice(ruled, attempt)
    assert (recorded['account_key'], recorded['mode'], recorded['rule_id'], recorded['place']) == (
        'signalwire', 'one', 'r-sw', 0)
    # Results for the rule-chosen account authenticate against it, though it is not in FAX_OUTBOUND_ROUTES.
    _, profile = ruled.delivery.attempt_context(job, attempt)
    assert profile.configuration.provider_id == 'signalwire'
    assert ruled.delivery.observe(job, attempt_id=attempt, profile_id=profile.id, provider_sid=None,
                                  status='success', event_key='synthetic-success') is True
    view = fax_route(ruled.engine, ruled.configuration, job, rules=ruled.rules)
    assert view['attempts'][0]['account'] == 'signalwire'
    assert view['sentence'].startswith('Goes by SignalWire because the rule ‘Rule r-sw’ matched')
    assert 'Organization rules version 1' in view['sentence']
    assert view['attempts'][0]['sentence'].endswith('Delivered.')


def _claim(env):
    return env.delivery.claim('worker-test')


def test_assign_route_refuses_an_account_outside_the_envelope(ruled):
    publish(ruled, {'format': 1, 'routes': [rule('r-phaxio', {'use': 'phaxio'})]})
    accept(ruled)
    claim = _claim(ruled)
    revision, _ = ruled.configuration.outbound_context(claim.job_id)
    from api.app.accounts import route_configuration
    with pytest.raises(DeliveryConflict, match="sending rules"):
        ruled.delivery.assign_route(claim, route_configuration(revision, 'signalwire'), account_key='signalwire')


@pytest.mark.asyncio
async def test_the_default_account_is_never_a_drop_back_outside_the_envelope(ruled):
    """The chosen account fails local preparation: the fax waits in Sent instead of going by the default."""
    publish(ruled, {'format': 1, 'routes': [rule('r-sw', {'use': 'signalwire'})]})
    job = accept(ruled)
    inner = Inner(ruled.delivery)
    inner.fail_for = {'signalwire'}
    assert await OutboundWorker(ruled.delivery, RoutedTransport(inner, direct=None)).step() is False
    assert inner.used == []
    assert ruled.delivery.get(job)['state'] == 'ready'
    held = holds(ruled, job)
    assert [item['kind'] for item in held] == ['no_route'] and 'nothing was sent' in held[0]['reason']
    assert _claim(ruled) is None


@pytest.mark.asyncio
async def test_with_no_usable_account_the_fax_waits_and_check_again_tries_it_again(ruled, monkeypatch):
    from api.app.routing import transport
    publish(ruled, {'format': 1, 'routes': [rule('r-sw', {'use': 'signalwire'})]})
    job = accept(ruled)
    monkeypatch.setattr(transport, 'route_ready', lambda configuration, ami=None: False)
    inner = Inner(ruled.delivery)
    worker = OutboundWorker(ruled.delivery, RoutedTransport(inner, direct=None))
    assert await worker.step() is False
    assert inner.used == [] and ruled.delivery.get(job)['state'] == 'ready'
    hold = holds(ruled, job)[0]
    assert hold['reason'] == ('No account your rules allow can send this fax now: SignalWire is not ready. It waits '
                              'for you in Sent; nothing was sent.')
    store = HoldStore(ruled.delivery)
    view = store.view(dict(hold, to_number=TO), ANNE, approver=True)
    assert [item['account'] for item in view['options']] == ['signalwire']
    worker.paused.clear()
    assert await worker.step() is False  # still held: never claimed
    store.check_again(hold['id'], version=hold['version'], actor=ANNE)
    monkeypatch.setattr(transport, 'route_ready', lambda configuration, ami=None: True)
    assert await worker.step() is True and inner.used == ['signalwire']


def test_a_fax_no_rule_allows_is_held_at_acceptance_with_its_sentence(ruled):
    publish(ruled, {'format': 1, 'limits': [rule('l-none', {'never': ['phaxio', 'signalwire']})]})
    job = accept(ruled)
    assert envelopes.load(ruled.engine, job).decision.outcome == 'blocked'
    hold = holds(ruled, job)[0]
    assert hold['kind'] == 'no_route'
    assert hold['reason'] == ('No account is allowed for this fax: the rule ‘Rule l-none’ removes every one it could '
                              'use. It waits for you in Sent.')
    assert _claim(ruled) is None
    assert ruled.delivery.get(job)['state'] == 'ready'


# Approvals, refusals and time windows -----------------------------------------------------------------------------

APPROVAL = {'format': 1, 'limits': [rule('l-big', {'hold_for_approval': {'separate_approver': True}},
                                         {'document': {'pages_over': 2}})]}


def test_an_approval_hold_waits_and_approving_releases_it_before_the_claim(ruled):
    publish(ruled, APPROVAL)
    job = accept(ruled, actor=ANNE)
    hold = holds(ruled, job)[0]
    assert hold['kind'] == 'approval' and hold['separate_approver'] == 1
    assert _claim(ruled) is None
    store = HoldStore(ruled.delivery)
    # The rule wants someone other than the sender.
    with pytest.raises(HoldForbidden):
        store.approve(hold['id'], version=hold['version'], actor=ANNE, actor_name='Anne Example')
    decided = store.approve(hold['id'], version=hold['version'], actor=BEN, actor_name='Ben Example')
    assert decided['state'] == 'released' and decided['sentence'] == 'Approved. The fax is no longer held.'
    with pytest.raises(HoldConflict):
        store.approve(hold['id'], version=hold['version'], actor=BEN)
    claim = _claim(ruled)
    assert claim is not None and claim.job_id == job


def test_the_approval_and_the_claim_cannot_race_into_two_sends(ruled):
    """Approve and claim both run under the configuration lock: one claim, never two."""
    publish(ruled, APPROVAL)
    job = accept(ruled)
    hold = holds(ruled, job)[0]
    HoldStore(ruled.delivery).approve(hold['id'], version=hold['version'], actor=BEN)
    first, second = _claim(ruled), _claim(ruled)
    assert first is not None and first.job_id == job and second is None


def _watch_claims(monkeypatch):
    """Record, inside each claim's own locked transaction, the holds still open on the fax it claims."""
    from api.app.routing import holds as hold_module
    seen, guard = [], __import__('threading').Lock()
    original = OutboundStore._claim_row_on

    def watched(self, connection, row, owner, now, lease_seconds):
        t = envelopes.tables(connection)
        still_open = [hold['kind'] for hold in hold_module.open_on(connection, t, row['id'])]
        claim = original(self, connection, row, owner, now, lease_seconds)
        if claim is not None:
            with guard:
                seen.append((row['id'], still_open))
        return claim
    monkeypatch.setattr(OutboundStore, '_claim_row_on', watched)
    return seen


def test_approving_while_two_workers_claim_sends_each_fax_once_and_only_after_its_approval(ruled, monkeypatch):
    """Real threads on real connections: one approver and two claiming workers start together. Every fax is claimed
    exactly once, and never while its approval is still open (checked inside the claim's own transaction)."""
    import threading
    publish(ruled, APPROVAL)
    # One number each: a claimed fax keeps its number's one line busy (capacity.py).
    jobs = [accept(ruled, to=f'+120255501{n:02d}') for n in range(6)]
    pending = {hold['job_id']: hold for hold in HoldStore(ruled.delivery).holds()}
    assert set(pending) == set(jobs)
    seen = _watch_claims(monkeypatch)
    start, approved, claims, failures = threading.Barrier(3), threading.Event(), [], []
    guard = threading.Lock()

    def approve():
        try:
            start.wait(timeout=30)
            store = HoldStore(ruled.delivery)
            for job in jobs:
                hold = pending[job]
                store.approve(hold['id'], version=hold['version'], actor=BEN, actor_name='Ben Example')
        except Exception as error:  # surfaced below
            failures.append(error)
        finally:
            approved.set()

    def work(name):
        try:
            start.wait(timeout=30)
            deadline = datetime.utcnow() + timedelta(seconds=60)
            while datetime.utcnow() < deadline:
                done = approved.is_set()  # read before claiming: a claim after the last approval ends the loop
                claim = ruled.delivery.claim(name)
                if claim is not None:
                    with guard:
                        claims.extend(member.job_id for member in claim.everyone)
                elif done:
                    return
        except Exception as error:
            failures.append(error)

    threads = [threading.Thread(target=approve)] + [threading.Thread(target=work, args=(f'worker-{n}',))
                                                     for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not failures and not any(thread.is_alive() for thread in threads)
    assert sorted(claims) == sorted(jobs)  # each fax once: never twice, never missed
    assert sorted(job for job, _ in seen) == sorted(jobs)
    assert all(still_open == [] for _, still_open in seen)  # never claimed while its approval was open
    assert _claim(ruled) is None
    with ruled.engine.connect() as connection:
        attempts = dict(connection.execute(sa.select(ruled.delivery.attempts.c.job_id, sa.func.count())
                                           .group_by(ruled.delivery.attempts.c.job_id)).all())
    assert attempts == {job: 1 for job in jobs}


def test_a_fax_held_after_its_preview_never_goes_with_others_before_its_approval(ruled, monkeypatch):
    """POST /fax decides a preview before the lock and sending together follows it, but the acceptance transaction
    decides again. A rule published in between holds the fax while it already waits to go with others: the
    together claim must not take it before its approval, and the other faxes to that number still go."""
    from api.app.batching import store as batching
    batching.BatchingSettings(ruled.engine).save(TO, enabled=True, recipient_agreed=True, actor='principal:p1',
                                                 actor_name='Owner')
    seen = _watch_claims(monkeypatch)
    earlier = datetime.utcnow() - timedelta(hours=1)

    def accept_together(plan):
        identity, now = uuid4().hex, datetime.utcnow()
        job = {'id': identity, 'to_number': TO, 'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
               'pages': 1, 'created_at': now, 'updated_at': now}
        with ruled.configuration._locked() as connection:
            ruled.configuration._accept_outbound_on(connection, ruled.snapshot.active, job)
            rules_acceptance.recorder(plan, identity, ANNE)(connection, now)
            batching.hold_on(connection, batching.tables(ruled.engine, connection), identity,
                             batching.HoldPlan(TO, 'key:front', 'Front Desk', 1, False, 60), earlier)
        return identity
    preview = lambda: rules_acceptance.prepare(ruled.engine, ruled.snapshot.active, actor=ANNE,  # noqa: E731
                                               destination=TO, pages=3)
    free = accept_together(preview())
    stale = preview()  # decided before the rule below: no hold
    publish(ruled, APPROVAL)
    held = accept_together(stale)
    assert [hold['kind'] for hold in holds(ruled, held)] == ['approval']
    claim = _claim(ruled)
    assert claim is not None and [member.job_id for member in claim.everyone] == [free]
    assert _claim(ruled) is None
    assert batching.member(ruled.engine, held)['state'] == 'separate'  # it goes on its own once released
    ruled.delivery.fail_preparation(claim, category='preparation_failed')  # frees the number's one line
    assert _claim(ruled) is None  # still waiting for its approval
    hold = holds(ruled, held)[0]
    HoldStore(ruled.delivery).approve(hold['id'], version=hold['version'], actor=BEN)
    later = _claim(ruled)
    assert later is not None and [member.job_id for member in later.everyone] == [held]
    assert all(still_open == [] for _, still_open in seen)


def test_an_approval_binds_to_the_document_it_was_held_with(ruled):
    publish(ruled, APPROVAL)
    pdf = ruled.tmp / 'probe.pdf'
    pdf.write_bytes(b'%PDF-1.4 synthetic original')
    from api.app.routing.holds import document_sha256
    job = accept(ruled, document_sha256=document_sha256(pdf))
    (ruled.tmp / f'{job}.pdf').write_bytes(b'%PDF-1.4 synthetic changed')
    hold = holds(ruled, job)[0]
    with pytest.raises(HoldConflict, match='changed after it was held'):
        HoldStore(ruled.delivery).approve(hold['id'], version=hold['version'], actor=BEN)
    (ruled.tmp / f'{job}.pdf').write_bytes(b'%PDF-1.4 synthetic original')
    assert HoldStore(ruled.delivery).approve(hold['id'], version=hold['version'], actor=BEN)['state'] == 'released'


def test_refusing_fails_the_fax_before_anything_is_sent(ruled):
    publish(ruled, APPROVAL)
    job = accept(ruled)
    hold = holds(ruled, job)[0]
    result = HoldStore(ruled.delivery).refuse(hold['id'], version=hold['version'], actor=BEN,
                                              actor_name='Ben Example', reason='wrong recipient')
    assert result['state'] == 'refused'
    assert ruled.delivery.get(job)['state'] == 'failed'
    with ruled.engine.connect() as connection:
        row = connection.execute(sa.select(ruled.configuration.jobs.c.status, ruled.configuration.jobs.c.error)
                                 .where(ruled.configuration.jobs.c.id == job)).one()
        attempts = connection.execute(sa.select(sa.func.count()).select_from(ruled.delivery.attempts)
                                      .where(ruled.delivery.attempts.c.job_id == job)).scalar()
    assert tuple(row) == ('failed', 'Refused by Ben Example: wrong recipient') and attempts == 0


def test_a_time_window_opens_by_itself(ruled):
    later = (datetime.utcnow() + timedelta(hours=2)).replace(second=0, microsecond=0)
    window = {'from': later.strftime('%H:%M'), 'until': (later + timedelta(hours=1)).strftime('%H:%M')}
    publish(ruled, {'format': 1, 'limits': [rule('l-night', {'hold_until': window})]})
    job = accept(ruled)
    hold = holds(ruled, job)[0]
    assert hold['kind'] == 'window' and hold['release_at'] == later
    assert ruled.delivery.claim('worker-test', now=later - timedelta(minutes=1)) is None
    claim = ruled.delivery.claim('worker-test', now=later + timedelta(minutes=1))
    assert claim is not None and claim.job_id == job
    assert HoldStore(ruled.delivery).release_due(now=later + timedelta(minutes=1)) == 1


# Fallback ---------------------------------------------------------------------------------------------------------

def _fail(env, job, *, before_data):
    attempt = env.delivery.get(job)['attempt_id']
    _, profile = env.delivery.attempt_context(job, attempt)
    env.delivery.observe(job, attempt_id=attempt, profile_id=profile.id, provider_sid=None, status='failed',
                         event_key='synthetic-failure-' + uuid4().hex, error=None, before_data=before_data)
    return env.delivery.get(job)['state']


@pytest.mark.asyncio
async def test_a_rule_chosen_route_falls_back_only_after_a_call_that_ended_before_fax_data(listed):
    policy = FallbackPolicy(FallbackScheduler(listed.delivery, listed.routes))
    OutboundStore.fallback_policy = policy
    try:
        publish(listed, {'format': 1, 'routes': [rule('r-order', {'try_in_order': ['signalwire', 'phaxio']})]})
        inner = Inner(listed.delivery)
        worker = OutboundWorker(listed.delivery, RoutedTransport(inner, direct=None))
        unknown = accept(listed)
        await worker.step()
        assert _fail(listed, unknown, before_data=None) == 'failed'  # the provider did not say: no fallback
        after = accept(listed)
        await worker.step()
        assert _fail(listed, after, before_data=False) == 'failed'  # pages may have gone: no fallback
        busy = accept(listed)
        await worker.step()
        assert _fail(listed, busy, before_data=True) == 'ready'  # busy, before any fax data: the next account
        await worker.step()
        assert inner.used == ['signalwire', 'signalwire', 'signalwire', 'phaxio']
        with listed.engine.connect() as connection:
            flags = dict(connection.execute(sa.select(listed.delivery.attempts.c.job_id,
                                                      listed.delivery.attempts.c.ended_before_data)
                                            .where(listed.delivery.attempts.c.phase == 'failed')).all())
        assert flags == {unknown: None, after: 0, busy: 1}
    finally:
        OutboundStore.fallback_policy = None


@pytest.mark.asyncio
async def test_today_s_fallback_for_faxes_no_rule_decides_is_unchanged(listed):
    OutboundStore.fallback_policy = FallbackPolicy(FallbackScheduler(listed.delivery, listed.routes))
    try:
        inner = Inner(listed.delivery)
        worker = OutboundWorker(listed.delivery, RoutedTransport(inner, direct=None))
        job = accept(listed)
        await worker.step()
        assert _fail(listed, job, before_data=None) == 'ready'
        await worker.step()
        assert inner.used == ['signalwire', 'phaxio']
    finally:
        OutboundStore.fallback_policy = None


# The last check, the busy trunk and applying rules again -------------------------------------------------------------

def test_the_submission_marker_refuses_an_unrecorded_route_when_rules_exclude_the_default(ruled):
    publish(ruled, {'format': 1, 'routes': [rule('r-sw', {'use': 'signalwire'})]})
    job = accept(ruled)
    claim = _claim(ruled)
    assert ruled.delivery.begin_submission(claim) is False
    assert ruled.delivery.get(job)['state'] == 'ready'
    assert holds(ruled, job)[0]['reason'].startswith('Faxbot could not confirm that this fax was going by a route')


@pytest.mark.asyncio
async def test_when_the_trunk_is_busy_a_rule_may_use_the_next_allowed_account(ruled):
    from api.app.outbound_worker import CapacityWait
    from api.app.routing.plan import RoutePlan
    from api.app.routing.policy import RouteCandidate, RouteChoice
    earlier = []
    for setting, expect in (('next', 'signalwire'), ('wait', None)):
        publish(ruled, {'format': 1, 'routes': [rule('r-busy', {'cheapest_reliable': ['phaxio', 'signalwire'],
                                                                 'when_busy': setting})]})
        job = accept(ruled)
        claim = ruled.delivery.claim('worker-test', exclude=tuple(earlier))
        assert claim.job_id == job
        earlier.append(job)
        pinned = envelopes.load(ruled.engine, claim.job_id)
        revision, _ = ruled.configuration.outbound_context(claim.job_id)
        plan = RoutePlan(TO, (RouteChoice(RouteCandidate('sip', 'provider', 'sip', None), 'cheapest', None),
                              RouteChoice(RouteCandidate('signalwire', 'provider', 'signalwire', None), 'alternative',
                                          None)), None, pinned=pinned)
        transport = RoutedTransport(Inner(ruled.delivery), direct=None)
        transport._trunk_has_room = lambda claim, revision: False
        if expect is None:
            with pytest.raises(CapacityWait):
                transport._assign(claim, plan, revision)
        else:
            chosen, _ = transport._assign(claim, plan, revision)
            assert chosen.route.key == expect and transport._skipped == (('sip', 'busy'),)
        ruled.delivery.defer(claim)


@pytest.mark.asyncio
async def test_applying_the_current_rules_never_touches_a_submitted_or_uncertain_fax(ruled):
    waiting = accept(ruled)
    uncertain = accept(ruled)
    claim = _claim(ruled)
    assert claim.job_id == waiting
    ruled.delivery.defer(claim)
    claim = ruled.delivery.claim('worker-test', exclude=(waiting,))
    assert claim.job_id == uncertain
    assert ruled.delivery.begin_submission(claim) is True
    ruled.delivery.record_uncertain(claim)
    publish(ruled, {'format': 1, 'routes': [rule('r-sw', {'use': 'signalwire'})]})
    result = apply_to_waiting(ruled.delivery, ruled.rules, actor=BEN, actor_name='Ben Example')
    assert (result['checked'], result['changed']) == (1, 1)
    assert [item['job_id'] for item in result['faxes']] == [waiting]
    assert envelopes.load(ruled.engine, waiting).envelope.accounts == ('signalwire',)
    assert envelopes.load(ruled.engine, waiting).sequence == 2
    assert envelopes.load(ruled.engine, uncertain).sequence == 1
    assert ruled.delivery.get(uncertain)['state'] == 'reconciliation_required'
    again = apply_to_waiting(ruled.delivery, ruled.rules, actor=BEN)
    assert (again['checked'], again['changed']) == (1, 0)


def test_the_claim_still_asks_capacity_for_the_next_fax_with_the_held_ones_left_out(ruled, monkeypatch):
    """AQ's claim order (T13): next_ready(connection, values, now, waiting=, exclude=) inside the lock."""
    publish(ruled, APPROVAL)
    held = accept(ruled)
    publish(ruled, {'format': 1})
    free = accept(ruled)
    real = ruled.delivery.capacity()
    calls = []

    class Spy:
        def __getattr__(self, name):
            return getattr(real, name)

        def next_ready(self, connection, values, now, *, waiting=None, exclude=()):
            calls.append((waiting is not None, tuple(exclude)))
            return real.next_ready(connection, values, now, waiting=waiting, exclude=exclude)
    monkeypatch.setattr(ruled.delivery, 'capacity', lambda connection=None: Spy())
    claim = _claim(ruled)
    assert claim.job_id == free and calls == [(True, ())]
    assert _claim(ruled) is None and held != free


# Ranking with the shared predictor and plan budgets ----------------------------------------------------------------

def test_ao_s_synthetic_month_through_the_real_policy(plans):  # noqa: F811
    """Toll-free numbers by Telnyx, the rest by HumbleFax until its budget, then Telnyx, then Sinch, and HumbleFax
    again from the next billing day: what RoutePlanner gives with the shared prices (routing.pricing)."""
    from api.app.routing import pricing
    from api.tests.test_plan_budget import LAB, TOLL_FREE, record, values
    multi, routes = plans
    settings = values(plan_budgets='humblefax:pages=30,faxes=none,day=1')
    planner = RoutePlanner(routes)

    def order(number, now):
        prices = pricing.prices_for(routes, settings, number, 3, bound='humblefax', now=now)
        plan = planner.plan(to_number=number, bound='humblefax', values=settings, pages=3, alternates=True,
                            prices=prices)
        return [choice.route.key for choice in plan.choices]
    taken, used = [], 0
    for day in range(1, 31):
        now = datetime(2026, 10, day, 15)
        for number in [LAB] + ([TOLL_FREE] if day % 5 == 0 else []):
            found = order(number, now)
            if number == TOLL_FREE:
                assert found[0] == 'sip', (day, found)
            elif used + 3 <= 30:
                assert found == ['humblefax', 'sip', 'sinch'], (day, found)
                used += 3
            else:
                assert found == ['sip', 'sinch', 'humblefax'], (day, found)
            taken.append(found[0])
            record(multi, routes, found[0], number, now)
    assert taken.count('humblefax') == 10
    assert order(LAB, datetime(2026, 11, 1, 9)) == ['humblefax', 'sip', 'sinch']


def _with_minutes(facts, minutes):
    from dataclasses import replace
    from api.app.routing.predict import PlanUse
    return replace(facts, plan=PlanUse(minutes=minutes))


def test_a_flat_plan_is_in_your_plan_and_a_minute_allowance_prices_its_overage(plans):  # noqa: F811
    from api.app.routing import pricing
    from api.app.routing.costs import RateTerms
    from api.app.routing.destinations import classify
    from api.app.routing.predict import Link, RouteFacts, Shape, predict_from
    from api.tests.test_plan_budget import LAB, TELNYX_OUT, values
    _, routes = plans
    quote = pricing.price(routes, values(), 'humblefax', LAB, 2, now=datetime(2026, 10, 20, 15))
    assert (quote.micros, quote.in_plan, quote.text(), quote.money()) == (0, True, 'In your plan', None)
    facts = RouteFacts('sip', 'Telnyx', classify(LAB), RateTerms(TELNYX_OUT, included_minutes=100), Link())
    inside = predict_from(_with_minutes(facts, 50), Shape(1, None, 'standard', 'normal'))
    assert inside.cost.micros == 0 and inside.marginal
    over = predict_from(_with_minutes(facts, 100), Shape(3, None, "standard", "normal"))
    assert over.cost.micros == TELNYX_OUT.per_minute_micros
    assert over.basis.startswith('1 minute past what your Telnyx plan includes')
    unknown = predict_from(_with_minutes(facts, None), Shape(1, None, 'standard', 'normal'))
    assert unknown.cost is None and 'no count of the minutes' in unknown.basis


def test_one_toll_free_classifier_keeps_north_american_terms_north_american():
    from api.app.routing import dialing, tollfree
    from api.app.routing.destinations import classify
    for number in ('+18005550100', '+18335550100', '+18885550100'):
        assert dialing.is_toll_free(number) and tollfree.is_toll_free(number)
        assert classify(number, 'US').kind == 'toll_free'
    assert not dialing.is_toll_free('+13035550100') and not tollfree.is_toll_free('+13035550100')
    # A UK 0800 number is toll-free from the UK, never priced at a US carrier's free toll-free rate.
    assert tollfree.is_toll_free('+448001234567', 'GB') and not dialing.is_toll_free('+448001234567', 'GB')
    # From the US it is an international call, as a US 800 number is from the UK.
    assert not tollfree.is_toll_free('+448001234567', 'US') and not dialing.is_toll_free('+18005550100', 'GB')


# Costs from the pages actually sent, and accounts with their own costs ----------------------------------------------

def _attempt(env, job, route, provider, *, phase='success', seconds=60):
    attempt, when = uuid4().hex, datetime.utcnow()
    with env.engine.begin() as connection:
        connection.execute(env.routes.attempts.insert().values(
            id=attempt, job_id=job, sequence=1, phase=phase, created_at=when,
            submitted_at=when - timedelta(seconds=seconds), completed_at=when))
        connection.execute(env.routes.costs.insert().values(
            id=attempt, job_id=job, destination=TO, route=route, route_reason='cheapest', provider_id=provider,
            outcome='pending', billing_checks=0, created_at=when, updated_at=when))
    return attempt


def _captured(env):
    targets = {target.attempt_id: target for target in env.routes.pending_captures()}
    for target in targets.values():
        env.routes.capture(target)
    with env.engine.connect() as connection:
        rows = connection.execute(sa.select(env.routes.costs)).mappings().all()
    return targets, {row['id']: dict(row) for row in rows}


def test_costs_come_from_the_pages_each_attempt_actually_sent(ruled):
    dense_job, codec_job, other_job = accept(ruled), accept(ruled), accept(ruled)
    dense = _attempt(ruled, dense_job, 'phaxio', 'phaxio')
    codec = _attempt(ruled, codec_job, 'phaxio', 'phaxio')
    original = _attempt(ruled, other_job, 'phaxio', 'phaxio')
    pages = sa.Table('fax_page_changes', sa.MetaData(), autoload_with=ruled.engine)
    sends = sa.Table('codec_sends', sa.MetaData(), autoload_with=ruled.engine)
    now = datetime.utcnow()
    codec_row = dict(phone_number=TO, layout='codec', resolution='standard', fec='rs', pages_original=3,
                     encrypted=0, format_version=1, created_at=now)
    with ruled.engine.begin() as connection:
        connection.execute(pages.insert().values(id=uuid4().hex, job_id=dense_job, attempt_id=dense, number=TO,
                                                 route='phaxio', original_pages=3, sent_pages=1, layout='dense',
                                                 pages_saved=2, created_at=now))
        connection.execute(sends.insert().values(id=codec_job, provider_id='phaxio', pages_encoded=2,
                                                 document_sha256='a' * 64, **codec_row))
        # Encoded pages made for another route: this attempt sent the original pages.
        connection.execute(sends.insert().values(id=other_job, provider_id='signalwire', pages_encoded=1,
                                                 document_sha256='b' * 64, **codec_row))
    targets, costs = _captured(ruled)
    assert [(targets[item].pages, targets[item].pages_source) for item in (dense, codec, original)] == [
        (1, 'sent'), (2, 'codec'), (3, 'original')]
    assert [(costs[item]['billed_pages'], costs[item]['estimated_cost_micros']) for item in (dense, codec, original)] \
        == [(1, 70_000), (2, 140_000), (3, 210_000)]
    # Spending and the dense pages' savings line agree: the cost is what was sent, the saving what was not.
    from api.app.pages.views import savings
    saved = savings(ruled.routes, ruled.engine, since=now - timedelta(days=1), days=1)
    assert saved['saved'] == {'USD': 140_000}
    assert costs[dense]['estimated_cost_micros'] + saved['saved']['USD'] == 210_000


def test_two_accounts_at_one_provider_each_send_with_their_own_costs(ruled):
    """sinch and sinch-uk both sending: each attempt records its account, priced by its own card first."""
    ruled.routes.replace_cards([card('phaxio', page='0.07'), card('signalwire', minute='0.0095'),
                                card('sinch', page='0.045'), card('sinch-uk', page='0.06')])
    first = _attempt(ruled, accept(ruled), 'sinch', 'sinch')
    second = _attempt(ruled, accept(ruled), 'sinch-uk', 'sinch')
    other = _attempt(ruled, accept(ruled), 'sinch-eu', 'sinch')
    _, costs = _captured(ruled)
    assert (costs[first]['route'], costs[first]['estimated_cost_micros']) == ('sinch', 135_000)
    assert (costs[second]['route'], costs[second]['provider_id'], costs[second]['estimated_cost_micros']) == (
        'sinch-uk', 'sinch', 180_000)
    # An account with no card of its own is priced by its provider's card, never at nothing.
    assert costs[other]['estimated_cost_micros'] == 135_000
    totals = {row['provider_id']: row['attempts'] for row in ruled.routes.cost_totals(datetime(2026, 1, 1))}
    assert totals == {'sinch': 3}


def test_require_encryption_is_met_by_ssl_fax_a_number_used_before(ruled):
    """Owner's answer Q2: direct delivery, or SSL Fax on the trunk for a number that has completed an SSL Fax call."""
    from api.app.rules import model
    from api.app.rules.evaluate import decide
    from api.app.rules.explain import FactsReader
    publish(ruled, {'format': 1, 'limits': [rule('l-enc', {'require_encryption': True})]})
    trunk = model.Account('sip', 'sip', 'Telnyx', default=True, automatic=True, sslfax=True)
    cloud = model.Account('phaxio', 'phaxio', 'Phaxio', automatic=True)
    reader = FactsReader(ruled.engine, ruled.snapshot.active.values, ruled.routes)

    def decided():
        facts = reader.read(to_number=TO, accounts=(trunk, cloud), pages=1)
        return facts, decide(ruled.rules.compiled_active(), facts, (trunk, cloud))
    facts, decision = decided()
    assert not facts.sslfax_seen and decision.outcome == 'blocked' and decision.reason == 'needs_encryption'
    observations = sa.Table('sslfax_observations', sa.MetaData(), autoload_with=ruled.engine)
    with ruled.engine.begin() as connection:
        connection.execute(observations.insert().values(id=uuid4().hex, number=TO, direction='outbound', accepts=1,
                                                        source='engine-synthetic-1', observed_at=datetime.utcnow()))
    facts, decision = decided()
    assert facts.sslfax_seen and decision.outcome == 'route'
    assert decision.envelope.accounts == ('sip',) and decision.envelope.sslfax
    assert [(item.account, item.why) for item in decision.excluded] == [('phaxio', 'not_encrypted')]


# Acceptance checks, direct delivery, sending together, caps, alternates and layout ---------------------------------

def _prepared(env, document=None, **facts):
    if document is not None:
        publish(env, document)
    return rules_acceptance.prepare(env.engine, env.snapshot.active, actor=ANNE, destination=TO, pages=3, **facts)


def test_with_the_trunk_down_only_a_fax_that_could_go_nowhere_else_is_refused(ruled):
    from api.app.main import _engine_down_refuses
    trunk_only = replace_bound(_prepared(ruled, {'format': 1, 'routes': [rule('r-p', {'use': 'phaxio'})]}), 'phaxio')
    assert _engine_down_refuses(trunk_only) is True
    other = replace_bound(_prepared(ruled, {'format': 1, 'routes': [rule('r-sw', {'use': 'signalwire'})]}), 'phaxio')
    assert _engine_down_refuses(other) is False
    held = replace_bound(_prepared(ruled, {'format': 1, 'limits': [rule('l-a', {'hold_for_approval': {}})],
                                           'routes': [rule('r-p', {'use': 'phaxio'})]}), 'phaxio')
    assert held.decision.outcome == 'held' and _engine_down_refuses(held) is False


def replace_bound(prepared, key):
    from dataclasses import replace
    return replace(prepared, bound_key=key)


def test_require_direct_never_reaches_a_call(ruled):
    publish(ruled, {'format': 1, 'limits': [rule('l-direct', {'require_direct': True})]})
    job = accept(ruled)
    pinned = envelopes.load(ruled.engine, job)
    assert (pinned.decision.outcome, pinned.decision.reason, pinned.envelope.accounts) == (
        'blocked', 'needs_partner', ())
    assert holds(ruled, job)[0]['reason'].startswith('The rule ‘Rule l-direct’ requires direct delivery')
    revision, _ = ruled.configuration.outbound_context(job)
    plan = RoutePlanner(ruled.routes).plan(to_number=TO, bound='phaxio', values=revision.values, pages=3,
                                           alternates=True, pinned=pinned)
    assert plan.choices == ()  # no call, and no drop-back to the default account


def test_sending_together_waits_only_when_the_trunk_goes_first():
    from api.app.rules import model
    from api.app.routing.rules_acceptance import first_route_is_bound

    def decision(mode, accounts, outcome='route', reason=None):
        return model.Decision(outcome=outcome, envelope=model.Envelope(mode=mode, accounts=accounts),
                              route=model.AUTOMATIC, facts_digest='0' * 64, reason=reason)
    assert first_route_is_bound(decision('automatic', ('phaxio', 'sip')), 'sip')
    assert first_route_is_bound(decision('ordered', ('sip', 'phaxio')), 'sip')
    assert not first_route_is_bound(decision('ordered', ('phaxio', 'sip')), 'sip')
    assert not first_route_is_bound(decision('one', ('phaxio',)), 'sip')
    assert not first_route_is_bound(decision('ordered', (), 'blocked', 'no_allowed_account'), 'sip')


def test_a_cap_at_dispatch_skips_an_account_whose_price_is_unknown_or_over(ruled):
    from api.app.routing.pricing import Price
    publish(ruled, {'format': 1, 'limits': [rule('l-cap', {'cap_cost': {'currency': 'USD', 'amount': '0.50'}})],
                    'routes': [rule('r-both', {'cheapest_reliable': ['phaxio', 'signalwire']})]})
    job = accept(ruled)
    pinned = envelopes.load(ruled.engine, job)
    revision, _ = ruled.configuration.outbound_context(job)
    planner = RoutePlanner(ruled.routes)
    prices = {'phaxio': Price('phaxio', 900_000, 'USD')}  # today's price is over; SignalWire's is unknown
    plan = planner.plan(to_number=TO, bound='phaxio', values=revision.values, pages=3, alternates=True,
                        pinned=pinned, prices=prices)
    assert plan.choices == () and plan.skipped == (('phaxio', 'over_cap'), ('signalwire', 'unknown_cost'))
    prices['signalwire'] = Price('signalwire', 9_500, 'USD')
    plan = planner.plan(to_number=TO, bound='phaxio', values=revision.values, pages=3, alternates=True,
                        pinned=pinned, prices=prices)
    assert [choice.route.key for choice in plan.choices] == ['signalwire']


TOLL_FREE_ALTERNATE = '+18005550100'


def _approve_alternate(env, action='approved'):
    from api.app.routing.tollfree import TollFreeApprovals
    TollFreeApprovals(env.engine).record(TO, action=action, alternate_number=TOLL_FREE_ALTERNATE,
                                         approved_by='Jane Smith', approved_on=datetime(2026, 10, 7),
                                         evidence='Same intake, confirmed by phone on 7 October.')


def _alternate_of(env, job):
    deliveries = env.delivery.deliveries
    with env.engine.connect() as connection:
        return connection.scalar(sa.select(deliveries.c.alternate_number).where(deliveries.c.id == job))


def test_the_alternate_number_follows_use_never_and_only_and_a_withdrawal_changes_new_faxes_only(ruled):
    _approve_alternate(ruled)
    used = accept(ruled)
    assert envelopes.load(ruled.engine, used).envelope.dial.number == TOLL_FREE_ALTERNATE
    assert _alternate_of(ruled, used) == TOLL_FREE_ALTERNATE
    publish(ruled, {'format': 1, 'limits': [rule('l-never', {'alternate_number': 'never'})]})
    never = accept(ruled)
    assert envelopes.load(ruled.engine, never).envelope.dial is None and _alternate_of(ruled, never) is None
    publish(ruled, {'format': 1, 'routes': [rule('r-only', {'automatic': True, 'alternate_number': 'only'})]})
    _approve_alternate(ruled, 'withdrawn')
    only = accept(ruled)
    assert envelopes.load(ruled.engine, only).decision.reason == 'needs_alternate'
    # The queued fax keeps the number it was accepted with.
    assert envelopes.load(ruled.engine, used).envelope.dial.number == TOLL_FREE_ALTERNATE
    assert _alternate_of(ruled, used) == TOLL_FREE_ALTERNATE


def test_the_envelope_s_layout_reaches_the_page_hook(ruled):
    from api.app.outbound_transport import _layout_rule
    plain = accept(ruled)
    publish(ruled, {'format': 1, 'routes': [rule('r-long', {'automatic': True, 'page_layout': 'as_receiver_allows'})]})
    long_pages = accept(ruled)
    publish(ruled, {'format': 1, 'routes': [rule('r-one', {'automatic': True, 'page_layout': 'one_per_sheet'})]})
    one = accept(ruled)
    assert [_layout_rule(ruled.engine, job) for job in (plain, long_pages, one)] == [None, 'allow', 'never']


def test_a_later_publish_leaves_a_queued_fax_alone_while_turning_an_account_off_applies_at_once(ruled):
    publish(ruled, {'format': 1, 'routes': [rule('r-sw', {'use': 'signalwire'})]})
    job = accept(ruled)
    publish(ruled, {'format': 1, 'routes': [rule('r-phaxio', {'use': 'phaxio'})]})
    pinned = envelopes.load(ruled.engine, job)
    assert pinned.envelope.accounts == ('signalwire',)  # decided under version 1, kept
    revision, _ = ruled.configuration.outbound_context(job)
    planner = RoutePlanner(ruled.routes)
    plan = planner.plan(to_number=TO, bound='phaxio', values=revision.values, pages=3, alternates=True,
                        pinned=pinned, current=revision.values)
    assert [choice.route.key for choice in plan.choices] == ['signalwire']
    from api.app.config_profiles import ConfigurationDocument
    turned_off = revision.values.with_provider_accounts(ConfigurationDocument({'signalwire': {'enabled': False}}))
    plan = planner.plan(to_number=TO, bound='phaxio', values=revision.values, pages=3, alternates=True,
                        pinned=pinned, current=turned_off)
    assert plan.choices == () and plan.skipped == (('signalwire', 'turned_off'),)


# Partner relays in the real planner (AS's relay_candidates) ----------------------------------------------------------

def test_the_planner_ranks_partner_relays_with_its_own_routes_by_cost(sender):
    """No-call routes first, then every priced route by cost (relays and own alike), unknown prices last; a rule's
    "never relay" and a direct-only rule keep relays out."""
    from api.app.rules import model
    from api.tests.test_partner_relay_terms import DEST, NOW
    cheap = sender.partner('Sydney office', '+61255501234', 20_000)
    unpriced = sender.partner('Hobart office', '+61355501234', None)
    routes = RouteStore(sender.engine)
    routes.replace_cards([card('phaxio', page='0.10')])
    values = SimpleNamespace(fax_default_country='US', sip_trunk_preset='', outbound_route_providers=('sinch',),
                             direct_delivery_enabled=False, route_min_success_percent=80, sip_trunk_did_list=(),
                             local_delivery_enabled=False)
    planner = RoutePlanner(routes)
    plan = planner.plan(to_number=DEST, bound='phaxio', values=values, pages=2, alternates=True, now=NOW)
    keys = [choice.route.key for choice in plan.choices]
    assert keys[:2] == ['relay:' + cheap, 'phaxio'] and set(keys[2:]) == {'relay:' + unpriced, 'sinch'}
    assert plan.choices[0].route.kind == 'relay' and plan.choices[0].estimated_cost_micros == 40_000

    def pinned(**envelope):
        decision = model.Decision(outcome='route', envelope=model.Envelope(mode='automatic',
                                                                          accounts=('phaxio', 'sinch'), **envelope),
                                  route=model.AUTOMATIC, facts_digest='0' * 64,
                                  excluded=(model.Excluded('relay', 'never'),) if not envelope else ())
        return envelopes.Pinned('decision-1', 1, decision, model.Facts(DEST, '2026-10-07T03:00:00'))
    never = planner.plan(to_number=DEST, bound='phaxio', values=values, pages=2, alternates=True, now=NOW,
                         pinned=pinned())
    assert [choice.route.key for choice in never.choices] == ['phaxio', 'sinch']
    direct_only = planner.plan(to_number=DEST, bound='phaxio', values=values, pages=2, alternates=True, now=NOW,
                               pinned=pinned(require_direct=True, direct=True))
    assert all(choice.route.kind != 'relay' for choice in direct_only.choices)


# The trunk's engine down ----------------------------------------------------------------------------------------------

TRUNK = {**BASE, 'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_ROUTES': 'signalwire', 'SIP_TRUNK_PRESET': 'telnyx',
         'SIP_TRUNK_HOST': 'sip.telnyx.com', 'SIP_TRUNK_CALLER_ID': '+13035550100'}


@pytest.mark.asyncio
async def test_with_the_trunk_engine_down_the_fax_goes_by_the_next_allowed_account(database, tmp_path):
    """The trunk is the default account and the cheapest; with its engine down a fax the rules let go another way is
    accepted and sent by SignalWire, and one the rules keep on the trunk waits in Sent instead of failing."""
    from api.app.main import _engine_down_refuses
    env = installation(database, tmp_path, TRUNK, outbound=ProviderConfiguration('sip'))
    env.routes.replace_cards([card('sip', minute='0.001'), card('signalwire', minute='0.0095')])
    plan = rules_acceptance.prepare(env.engine, env.snapshot.active, actor=ANNE, destination=TO, pages=3)
    assert plan.bound_key == 'sip' and not _engine_down_refuses(plan)
    job = accept(env)
    inner = Inner(env.delivery)  # no AMI connection: the trunk's engine is down
    assert await OutboundWorker(env.delivery, RoutedTransport(inner, direct=None)).step() is True
    assert inner.used == ['signalwire']
    recorded = choice(env, env.delivery.get(job)['attempt_id'])
    assert recorded['account_key'] == 'signalwire'
    assert envelopes.skipped_of(recorded)[0] == [('sip', 'not_ready')]
    publish(env, {'format': 1, 'routes': [rule('r-trunk', {'use': 'sip'})]})
    kept = rules_acceptance.prepare(env.engine, env.snapshot.active, actor=ANNE, destination=TO, pages=3)
    assert _engine_down_refuses(kept)  # POST /fax answers 503 with the engine's own sentence
    # Another number: the first fax's call still holds TO's one line (capacity.py).
    held = accept(env, to='+12025550199')
    assert await OutboundWorker(env.delivery, RoutedTransport(inner, direct=None)).step() is False
    assert inner.used == ['signalwire'] and env.delivery.get(held)['state'] == 'ready'
    assert holds(env, held)[0]['reason'] == ('No account your rules allow can send this fax now: Telnyx is not '
                                             'ready. It waits for you in Sent; nothing was sent.')


# Two accounts at one provider through the real worker ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rule_sends_by_a_second_sinch_account_with_its_own_credentials_card_and_costs(database, tmp_path):
    """sinch and sinch-uk both send: a rule picks sinch-uk; its attempt binds sinch-uk's own credentials, records
    route sinch-uk (provider sinch), is priced by sinch-uk's card, and its result authenticates."""
    from api.tests.test_provider_accounts import UK, environment, manager, with_accounts
    control = manager(database, tmp_path)
    first = control.initialize({**environment(tmp_path), 'FAX_DATA_DIR': str(tmp_path), 'FAX_DISABLED': 'false',
                                'PUBLIC_API_URL': 'https://faxbot.example.org'})
    snapshot = with_accounts(control, first, {'sinch-uk': {**UK, 'receives': False, 'numbers': []}})
    env = SimpleNamespace(configuration=control.store, delivery=OutboundStore(control.store),
                          routes=RouteStore(database), snapshot=snapshot, rules=RuleStore(database), engine=database,
                          tmp=tmp_path)
    env.routes.replace_cards([card('sinch', page='0.045'), card('sinch-uk', page='0.06')])
    publish(env, {'format': 1, 'routes': [rule('r-uk', {'use': 'sinch-uk'})]})
    job = accept(env)
    inner = Inner(env.delivery, [SubmissionReceipt('FXUK1', 'in_progress')])
    assert await OutboundWorker(env.delivery, RoutedTransport(inner, direct=None)).step() is True
    attempt = env.delivery.get(job)['attempt_id']
    _, profile = env.delivery.attempt_context(job, attempt)
    assert profile.configuration.provider_id == 'sinch'
    assert profile.configuration.credentials['api_key'] == 'synthetic-uk-key'
    assert profile.id != snapshot.active.profile_id('outbound')
    assert choice(env, attempt)['account_key'] == 'sinch-uk'
    decided = env.routes.decision(attempt)
    assert (decided['route'], decided['provider_id']) == ('sinch-uk', 'sinch')
    assert env.delivery.observe(job, attempt_id=attempt, profile_id=profile.id, provider_sid='FXUK1',
                                status='success', event_key='synthetic-uk-success') is True
    (target,) = [item for item in env.routes.pending_captures() if item.attempt_id == attempt]
    assert (target.route, target.provider_id) == ('sinch-uk', 'sinch')
    assert env.routes.capture(target)['estimated_cost_micros'] == 180_000  # 3 pages at sinch-uk's $0.06
