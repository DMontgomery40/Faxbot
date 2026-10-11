"""Combinations, fault injection: an outbound fax through each stage with a fault at that stage (brief 104).

Every case runs the real outbound worker, the real route choice over two configured SIP trunk accounts
(``sip`` by the minute, ``sip-pages`` by the page: tests/test_joint_optimizer.py's installation), real page
preparation and the real captured transport. Faults are injected only at the provider seam: the stand-in AMI
(down, refusing, or losing its answer), the engine's own events handed to Faxbot's real result handlers
(``main._handle_fax_result`` and ``main._handle_originate_response``, pointed at this installation's store), and a
worker that dies in the middle of preparation. Database lock faults are builder WAL's (brief 103).

What applied is read back from the fax's own records (``outbound_deliveries``, ``outbound_attempts``,
``outbound_events``, ``route_selections``, ``delivery_attempt_costs``, holds) and from the calls the stand-in AMI
recorded. The invariant, on every case:

- no fax is submitted twice (one AMI call per attempt that was authorized, and never a second for an uncertain
  attempt);
- a definite failure before any fax data is told apart from an uncertain or partial delivery: only the first may
  move to the next account;
- an uncertain fax is never sent again by itself;
- each attempt stays bound to the account it was submitted on: a result is applied only from that account.

SQLite and PostgreSQL. Synthetic numbers and documents; nothing is dialed.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from api.app.outbound_store import DeliveryConflict
from api.app.outbound_worker import OutboundWorker
from api.tests.combinations_support import Case, database, record  # noqa: F401 - fixture
from api.tests.test_joint_optimizer import NUMBER, joint, transport, write_document  # noqa: F401 - fixture
from api.app.routing.fallback import FallbackScheduler
from api.app.routing.holds import HoldStore
from api.tests.test_rules_delivery import ANNE, accept, holds, publish, rule

GROUP = 'faults'
INVARIANT = ('never submitted twice; definite failure before data told apart from uncertain or partial; an '
             'uncertain fax never resent; each attempt bound to its account')
CHEAPEST = {'cheapest_reliable': ['sip', 'sip-pages']}


# Reading the fax's own records ---------------------------------------------------------------------------------

def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def attempts(env, job):
    table = _table(env.engine, 'outbound_attempts')
    with env.engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).where(table.c.job_id == job)
                                                        .order_by(table.c.sequence)).mappings()]


def events(env, job):
    return [event['kind'] for event in env.delivery.history(job)]


def account_of(env, attempt_id):
    decision = env.routes.decision(attempt_id)
    return decision['route'] if decision else None


def profile_of(env, attempt_id):
    table = _table(env.engine, 'outbound_attempts')
    with env.engine.connect() as connection:
        return connection.scalar(sa.select(table.c.profile_id).where(table.c.id == attempt_id))


def calls_for(env, job):
    return [call for call in env.ami.calls if call['job_id'] == job]


def fax(env, *, rules=CHEAPEST):
    """One accepted fax under a cheapest-account rule, with its files as acceptance leaves them."""
    publish(env, {'format': 1, 'routes': [rule('r-cheap', rules)]})
    job = accept(env, to=NUMBER, pages=2)
    write_document(env, job)
    return job


async def step(env):
    return await OutboundWorker(env.delivery, transport(env)).step()


def assert_invariant(env, job):
    """The group's invariant on the fax's records as they stand."""
    found = attempts(env, job)
    calls = calls_for(env, job)
    submitted = [attempt for attempt in found if attempt['submitted_at'] is not None]
    # One call per submitted attempt, each on its own attempt, never two for one attempt.
    assert sorted(call['attempt_id'] for call in calls) == sorted(attempt['id'] for attempt in submitted)
    # Each call went over the account its attempt was bound to.
    for call in calls:
        account = account_of(env, call['attempt_id'])
        assert (call['trunk'] or 'sip') == account
    # An attempt that is uncertain, or that broke after pages went, is never followed by another attempt.
    for attempt in submitted:
        later = [other for other in found if other['sequence'] > attempt['sequence']]
        if attempt['phase'] == 'uncertain' or attempt['error_category'] == 'partly_sent':
            assert not later, f"attempt {attempt['sequence']} was followed though its outcome was not definite"
        if later and attempt['phase'] == 'failed':
            # Only a failure before any fax data moves to the next account.
            assert attempt['ended_before_data'] == 1
    return found, calls


def epoch(moment):
    return str(int(moment.timestamp()))


def fax_result(job, attempt, *, status='SUCCESS', pages='2', station=None, answered=True):
    """Asterisk's FaxResult user event for one built-in engine call, as the dialplan sends it."""
    now = datetime(2026, 10, 10, 9, 0)
    event = {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': job, 'AttemptID': attempt, 'Status': status,
             'Error': '' if status == 'SUCCESS' else 'T30_ERR_RX_NOCARRIER', 'Pages': pages, 'Mode': 'T38',
             'Ended': epoch(now + timedelta(seconds=58)), 'Rate': '14400', 'Resolution': '8031x7700'}
    if answered:
        event['Answered'] = epoch(now)
    if station is not None:
        event['Station64'] = base64.b64encode(station.encode()).decode()
    return event


@pytest.fixture
def results(joint, monkeypatch):  # noqa: F811 - the fixture
    """Faxbot's real handlers for the engine's events, pointed at this installation's delivery store."""
    from api.app import main
    monkeypatch.setattr(main, '_deliveries', lambda: joint.delivery)
    return main


# Before submission ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_trunk_that_is_down_while_preparing_sends_nothing_and_the_fax_goes_once_when_it_is_back(joint):
    job = fax(joint)
    joint.ami._connected.clear()
    assert await step(joint) is False
    found, calls = assert_invariant(joint, job)
    [first] = found
    assert calls == [] and first['submitted_at'] is None and first['phase'] == 'abandoned'
    assert 'submission_authorized' not in events(joint, job) and 'route_held' in events(joint, job)
    [hold] = holds(joint, job)
    assert hold['kind'] == 'no_route' and hold['reason'].endswith('nothing was sent.')
    # The trunk is back: checking again lets the next claim send the fax, once.
    joint.ami._connected.set()
    HoldStore(joint.delivery).check_again(hold['id'], version=hold['version'], actor=ANNE)
    assert await step(joint) is True
    assert await step(joint) is False
    found, calls = assert_invariant(joint, job)
    assert len(calls) == 1 and joint.delivery.get(job)['state'] == 'in_progress'
    record(Case(GROUP, 'trunk down while preparing', ('fault:before submit', 'sip', 'sip-pages'),
                ('held, nothing sent', 'sent once after check again'), INVARIANT))


@pytest.mark.asyncio
async def test_a_worker_that_dies_in_the_middle_of_preparation_gives_the_attempt_up_and_the_fax_goes_once(
        joint, monkeypatch):
    """The worker process dies after the route choice bound an account and while the pages are being prepared
    (nothing handed to the trunk yet). Once its lease runs out another worker gives that attempt up and claims the
    fax again: one call, on the second attempt."""
    from api.app.pages import sending
    job = fax(joint)
    real = sending.prepare

    def dies(*args, **kwargs):
        raise SystemExit('synthetic worker death in the middle of preparation')
    monkeypatch.setattr(sending, 'prepare', dies)
    with pytest.raises(SystemExit):
        await step(joint)
    monkeypatch.setattr(sending, 'prepare', real)
    row = joint.delivery.get(job)
    assert row['state'] == 'preparing' and calls_for(joint, job) == []
    # A restarted worker before the lease runs out leaves it alone.
    assert await step(joint) is False and calls_for(joint, job) == []
    assert joint.delivery.recover_expired(now=row['claim_expires_at'] + timedelta(seconds=1)) == 1
    [given_up] = attempts(joint, job)
    assert (given_up['phase'], given_up['error_category'], given_up['submitted_at']) == ('abandoned', 'worker_lost',
                                                                                         None)
    assert await step(joint) is True
    found, calls = assert_invariant(joint, job)
    assert len(found) == 2 and len(calls) == 1 and calls[0]['attempt_id'] == found[1]['id']
    assert 'preparation_expired' in events(joint, job)
    record(Case(GROUP, 'worker dies in the middle of preparation', ('fault:restart while preparing',),
                ('attempt 1 given up', 'attempt 2 sent once'), INVARIANT))


# While handing the call over ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lost_answer_while_handing_the_call_over_is_uncertain_on_its_own_account_and_never_resent(
        joint, results):
    job = fax(joint)
    handed = []
    original = joint.ami.originate_sendfax

    async def lost(job_id, dest, tiff_path, **kwargs):
        # The call is placed (the stand-in records it), and Asterisk's answer never comes back.
        handed.append(job_id)
        await original(job_id, dest, tiff_path, **kwargs)
        raise TimeoutError('synthetic lost acknowledgement')
    joint.ami.originate_sendfax = lost
    assert await step(joint) is True
    [attempt] = attempts(joint, job)
    assert handed == [job] and joint.delivery.get(job)['state'] == 'reconciliation_required'
    assert attempt['phase'] == 'uncertain' and attempt['submitted_at'] is not None
    bound = account_of(joint, attempt['id'])
    assert bound == 'sip-pages'
    # Never sent again by itself: not by the worker, the fallback scheduler or a requeue.
    joint.ami.originate_sendfax = original
    assert await step(joint) is False
    assert FallbackScheduler(joint.delivery, joint.routes).step() is False
    assert joint.delivery.requeue_after_failure(job, attempt_id=attempt['id'], category='provider_failed') is False
    # A result in the name of another account (the fax's first trunk) is refused and changes nothing.
    _, accepted = joint.configuration.outbound_context(job)
    assert accepted.id != attempt['profile_id']
    with pytest.raises(DeliveryConflict):
        joint.delivery.observe(job, attempt_id=attempt['id'], profile_id=accepted.id, provider_sid=job,
                               status='success', event_key='synthetic-other-account')
    assert joint.delivery.get(job)['state'] == 'reconciliation_required'
    assert 'provider_observation_refused' in events(joint, job)
    # The engine's own late result for that attempt replaces the uncertainty with the real outcome.
    results._handle_fax_result(fax_result(job, attempt['id']))
    assert joint.delivery.get(job)['state'] == 'success'
    found, calls = assert_invariant(joint, job)
    assert len(found) == 1 and len(calls) == 1
    record(Case(GROUP, 'lost answer while handing over', ('fault:during submit', 'sip-pages'),
                ('uncertain on sip-pages', 'other account refused', 'late result applied'), INVARIANT))


@pytest.mark.asyncio
async def test_a_call_that_never_connected_is_a_definite_failure_before_data_and_the_next_account_sends_once(
        joint, results):
    job = fax(joint)
    assert await step(joint) is True
    [first] = attempts(joint, job)
    assert account_of(joint, first['id']) == 'sip-pages'
    # Asterisk answers the originate with a failure: the call was never placed (busy).
    results._handle_originate_response({'Event': 'OriginateResponse', 'Response': 'Failure', 'Reason': '5',
                                        'ActionID': f"faxbot:{job}:{first['id']}"})
    [first] = attempts(joint, job)
    assert (first['phase'], first['ended_before_data'], first['error_category']) == ('failed', 1, None)
    # The rules chose its route, so the fax waits in Sent naming the account tried; nothing failed for good.
    [hold] = holds(joint, job)
    assert hold['kind'] == 'no_route' and 'Page trunk' in hold['reason']
    HoldStore(joint.delivery).check_again(hold['id'], version=hold['version'], actor=ANNE)
    assert await step(joint) is True and await step(joint) is False
    found, calls = assert_invariant(joint, job)
    assert [account_of(joint, attempt['id']) for attempt in found] == ['sip-pages', 'sip']
    assert len(calls) == 2 and calls[1]['attempt_id'] == found[1]['id']
    record(Case(GROUP, 'call never connected', ('fault:during submit (definite)', 'sip-pages', 'sip'),
                ('failed before data on sip-pages', 'sent once on sip'), INVARIANT))


# After the call: the engine's result ---------------------------------------------------------------------------

RESULTS = {
    # (status, pages, station, answered) -> (attempt phase, ended before data, error category, fax state)
    'no answer': (('FAILED', '0', None, False), ('failed', 1, None, 'ready')),
    'a fax machine answered and no page went': (('FAILED', '0', 'FAX 555 0123', True), ('failed', 0, None, 'failed')),
    'broke after a page': (('FAILED', '1', 'FAX 555 0123', True), ('failed', 0, 'partly_sent', 'failed')),
    'went through': (('SUCCESS', '2', 'FAX 555 0123', True), ('success', None, None, 'success')),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', sorted(RESULTS))
async def test_each_engine_result_is_classified_and_only_a_failure_before_data_moves_on(joint, results, outcome):
    (status, pages, station, answered), expected = RESULTS[outcome]
    job = fax(joint)
    assert await step(joint) is True
    [first] = attempts(joint, job)
    results._handle_fax_result(fax_result(job, first['id'], status=status, pages=pages, station=station,
                                          answered=answered))
    [first] = attempts(joint, job)
    state = joint.delivery.get(job)['state']
    assert (first['phase'], first['ended_before_data'], first['error_category'], state) == expected
    # Nothing moves it on by itself except a failure before data, and that only to an account not tried yet.
    assert FallbackScheduler(joint.delivery, joint.routes).step() is False
    if expected[1] == 1:
        [hold] = holds(joint, job)
        HoldStore(joint.delivery).check_again(hold['id'], version=hold['version'], actor=ANNE)
    assert await step(joint) is (expected[1] == 1)
    assert await step(joint) is False
    found, calls = assert_invariant(joint, job)
    assert len(calls) == (2 if expected[1] == 1 else 1)
    record(Case(GROUP, f'engine result: {outcome}', ('fault:after submit', outcome),
                (f'phase {expected[0]}', f'before data {expected[1]}', f'category {expected[2]}',
                 f'calls {len(calls)}'), INVARIANT))


@pytest.mark.asyncio
async def test_a_send_whose_result_has_not_come_is_never_sent_again(joint):
    job = fax(joint)
    assert await step(joint) is True
    [attempt] = attempts(joint, job)
    for _ in range(2):
        assert await step(joint) is False
    assert FallbackScheduler(joint.delivery, joint.routes).step() is False
    assert joint.delivery.requeue_after_failure(job, attempt_id=attempt['id'], category='provider_failed') is False
    found, calls = assert_invariant(joint, job)
    assert len(calls) == 1 and joint.delivery.get(job)['state'] == 'in_progress'
    record(Case(GROUP, 'engine result not come yet', ('fault:lost result',), ('in progress', 'one call'),
                INVARIANT))


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason='defect C40: a built-in engine send whose result never arrives stays in '
                                       'progress for ever; only SSL Fax engine sends are settled as uncertain')
async def test_a_built_in_engine_send_whose_result_never_comes_waits_for_a_person(joint):
    """Asterisk restarts (or its FaxResult event is lost) after the call was placed by the built-in engine. Faxbot's
    sweep for sends the engine took and never reported on (``hylafax_http._sweep``, after ``NO_RESULT_AFTER``)
    settles only the SSL Fax engine's sends (``hylafax_records.unfinished_sends`` reads engine 'hylafax'), so this
    fax stays in progress with no sentence for a person; the SSL Fax engine's would wait as uncertain."""
    from api.app import hylafax_http
    job = fax(joint)
    assert await step(joint) is True
    later = datetime.utcnow() + timedelta(minutes=1)  # the sweep's cut-off once NO_RESULT_AFTER has passed
    marked = hylafax_http._settle_unreported(joint.engine, joint.delivery, later, 'no-result')
    assert marked == 1 and joint.delivery.get(job)['state'] == 'reconciliation_required'


# Duplicate and contradictory results ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['went through', 'no answer'])
async def test_the_same_engine_result_twice_changes_the_fax_once(joint, results, outcome):
    (status, pages, station, answered), expected = RESULTS[outcome]
    job = fax(joint)
    assert await step(joint) is True
    [first] = attempts(joint, job)
    event = fax_result(job, first['id'], status=status, pages=pages, station=station, answered=answered)
    results._handle_fax_result(event)
    once = events(joint, job)
    results._handle_fax_result(dict(event))  # the same event again (an AMI replay)
    kinds = events(joint, job)
    assert kinds == once and attempts(joint, job)[0]['phase'] == expected[0]
    assert kinds.count('route_held') == (1 if expected[1] == 1 else 0)
    if expected[1] == 1:
        [hold] = holds(joint, job)
        HoldStore(joint.delivery).check_again(hold['id'], version=hold['version'], actor=ANNE)
        assert await step(joint) is True
        # The first attempt's result once more, after the fax moved on: it no longer moves the fax.
        results._handle_fax_result(dict(event))
    assert await step(joint) is False
    found, calls = assert_invariant(joint, job)
    assert len(calls) == (2 if expected[1] == 1 else 1)
    # The first attempt's result was applied once (the receipt and the result); a replay adds nothing.
    first_events = [event for event in joint.delivery.history(job) if event.get('attempt_id') == first['id']
                    and event['kind'] == 'provider_observed']
    assert len(first_events) == 2
    record(Case(GROUP, f'duplicate engine result: {outcome}', ('fault:duplicate result', outcome),
                ('observed once', f'calls {len(calls)}'), INVARIANT))


@pytest.mark.asyncio
async def test_a_contradicting_late_result_is_kept_as_evidence_and_never_sends_again(joint, results):
    job = fax(joint)
    assert await step(joint) is True
    [first] = attempts(joint, job)
    results._handle_fax_result(fax_result(job, first['id']))
    results._handle_fax_result(fax_result(job, first['id'], status='FAILED', pages='0', answered=False))
    assert joint.delivery.get(job)['state'] == 'success'
    assert 'terminal_conflict' in events(joint, job)
    assert FallbackScheduler(joint.delivery, joint.routes).step() is False and await step(joint) is False
    found, calls = assert_invariant(joint, job)
    assert len(calls) == 1
    record(Case(GROUP, 'contradicting late result', ('fault:duplicate result', 'success then failed'),
                ('success kept', 'conflict recorded', 'calls 1'), INVARIANT))
