"""Combinations, group 5: long preparation x queue contention x lease renewal x fallback (brief 104).

The invariant:

- no fax starves: every fax in the queue goes, within a bounded number of worker rounds, whatever another fax is
  doing (preparing for longer than a lease, waiting for its number's line);
- no fax is submitted twice: one call per authorized attempt, never a second for the same attempt, and never a
  call from a worker whose lease ran out;
- each attempt stays bound to its account: the account the route choice recorded is the trunk the call went over;
- a definite failure before any fax data moves the fax to its next account, while an uncertain one is never sent
  again by itself.

The real outbound workers (two at once where contention is the point), the real route choice over two configured
SIP accounts (tests/test_joint_optimizer.py's installation), real page preparation and lease renewal; preparation is
made longer than the lease by holding it open, as a large fax's measuring and encoding does. Database lock faults
are builder WAL's (brief 103). SQLite and PostgreSQL. Synthetic numbers and documents; nothing is dialed.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa

from api.app.outbound_worker import OutboundWorker
from api.app.routing import selections
from api.tests.combinations_support import Case, database, record  # noqa: F401 - fixture
from api.tests.test_joint_optimizer import NUMBER, joint, transport, write_document  # noqa: F401 - fixture
from api.tests.test_rules_delivery import ANNE, accept, holds, publish, rule

GROUP = '5 long preparation x contention x lease renewal x fallback'
INVARIANT = ('no fax starves; never submitted twice; attempt bound to its account; definite failure before data '
             'moves on, uncertain never resent')
CHEAPEST = {'cheapest_reliable': ['sip', 'sip-pages']}
LEASE = 2  # seconds: a short lease, as tests/test_joint_optimizer.py takes it
LONGER_THAN_A_LEASE = 2.6


def slow(env, seconds=LONGER_THAN_A_LEASE, sweeps=None):
    """The real routed transport, with preparation held open for ``seconds`` (longer than one lease), during which
    another worker's sweep for expired leases runs (``sweeps`` collects what it recovered)."""
    routed = transport(env)
    prepare = routed.prepare

    def held_open(claim):
        @asynccontextmanager
        async def preparing():
            await asyncio.sleep(seconds)
            if sweeps is not None:
                sweeps.append(await asyncio.to_thread(env.delivery.recover_expired))
            async with prepare(claim) as operation:
                yield operation
        return preparing()
    routed.prepare = held_open
    return routed


def faxes(env, count):
    jobs = []
    for _ in range(count):
        job = accept(env, to=NUMBER, pages=2)
        write_document(env, job)
        jobs.append(job)
    return jobs


def attempts(env, job):
    table = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=env.engine)
    with env.engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table).where(table.c.job_id == job)
                                                        .order_by(table.c.sequence)).mappings()]


def went_on(env, job):
    """The attempts that went on to submission: an attempt given back before any call is abandoned, unsent."""
    return [attempt for attempt in attempts(env, job) if attempt['phase'] != 'abandoned']


def authorized(env, job):
    return [event for event in env.delivery.history(job) if event['kind'] == 'submission_authorized']


def assert_once_each_and_bound(env, jobs):
    """One call per authorized attempt, the call over the trunk its attempt's route was recorded for."""
    calls = env.ami.calls
    for job in jobs:
        made = [call for call in calls if call['job_id'] == job]
        assert len(made) == len(authorized(env, job)) == len({call['attempt_id'] for call in made})
        for call in made:
            route = env.routes.decision(call['attempt_id'])['route']
            assert (call['trunk'] or 'sip') == route
            found = selections.for_attempt(env.engine, call['attempt_id'])
            if found is not None:
                assert found['account_key'] == route
    assert len({call['attempt_id'] for call in calls}) == len(calls)


def end_calls(env):
    """Each call in progress ends with all its pages sent, so its number's line is free again."""
    for call in env.ami.calls:
        row = env.delivery.get(call['job_id'])
        if row['state'] == 'in_progress' and row['attempt_id'] == call['attempt_id']:
            profile = next(item['profile_id'] for item in attempts(env, call['job_id'])
                           if item['id'] == call['attempt_id'])
            env.delivery.observe(call['job_id'], attempt_id=call['attempt_id'], profile_id=profile,
                                 provider_sid=call['job_id'], status='success', event_key=f'done-{call["attempt_id"]}')


@pytest.mark.asyncio
async def test_two_workers_and_long_preparations_send_every_fax_once_on_its_own_account(joint):
    """Three faxes, two workers at once, every preparation longer than a lease, and the number takes calls at once.
    Each worker renews its lease while it prepares, so the other's sweep recovers nothing; every fax goes once."""
    joint.routes.update_destination(NUMBER, max_calls=0)
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', CHEAPEST)]})
    jobs = faxes(joint, 3)
    sweeps = []
    workers = [OutboundWorker(joint.delivery, slow(joint, sweeps=sweeps), lease_seconds=LEASE) for _ in range(2)]
    for _ in range(8):
        await asyncio.gather(*(worker.step() for worker in workers))
        end_calls(joint)  # calls end, so the trunks' lines are free for the next fax
        for worker in workers:
            worker.paused.clear()  # a fax given back for lack of room may be claimed again at once here
        if all(joint.delivery.get(job)['state'] == 'success' for job in jobs):
            break
    assert [joint.delivery.get(job)['state'] for job in jobs] == ['success'] * 3  # none starved
    assert set(sweeps) == {0}  # no lease ran out while its worker was preparing
    assert_once_each_and_bound(joint, jobs)
    # A preparation the database was too busy for gives the fax back (#77, #83): that attempt is abandoned with
    # nothing sent, and the fax is claimed again. Each fax still has exactly one attempt that went on.
    assert all(len(went_on(joint, job)) == 1 for job in jobs), {
        job: [(attempt['sequence'], attempt['phase']) for attempt in attempts(joint, job)] for job in jobs}
    record(Case(GROUP, 'two workers, three faxes, long preparations', ('long preparation', 'two workers',
                                                                     'lease renewal'), ('each sent once',),
                INVARIANT))


@pytest.mark.asyncio
async def test_faxes_waiting_for_one_line_each_go_in_turn_and_none_starves(joint):
    """The number takes one call at a time and every preparation is long: while a call is on the line the next fax
    waits for room (it is given back, never failed), and as each call ends the next goes."""
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', CHEAPEST)]})
    jobs = faxes(joint, 3)
    worker = OutboundWorker(joint.delivery, slow(joint), lease_seconds=LEASE)
    rounds = 0
    while any(joint.delivery.get(job)['state'] == 'ready' for job in jobs) and rounds < 12:
        rounds += 1
        worker.paused.clear()  # a given-back fax may be claimed again at once in this test
        await worker.step()
        end_calls(joint)
    assert [joint.delivery.get(job)['state'] for job in jobs] == ['success'] * 3
    assert [call['job_id'] for call in joint.ami.calls] == jobs  # in the order they were accepted
    assert_once_each_and_bound(joint, jobs)
    assert all('preparation_failed' not in [event['kind'] for event in joint.delivery.history(job)] for job in jobs)
    record(Case(GROUP, 'one line, three faxes, long preparations', ('long preparation', 'one line',
                                                                   'lease renewal'), ('each sent in turn',),
                INVARIANT))


@pytest.mark.asyncio
async def test_a_definite_failure_after_a_long_preparation_moves_on_to_the_other_account_once(joint):
    from api.app.routing.holds import HoldStore
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', CHEAPEST)]})
    [job] = faxes(joint, 1)
    worker = OutboundWorker(joint.delivery, slow(joint), lease_seconds=LEASE)
    assert await worker.step() is True
    [first] = attempts(joint, job)
    # The call ended before any fax data (busy): a definite failure another account may take.
    joint.delivery.observe(job, attempt_id=first['id'], profile_id=first['profile_id'], provider_sid=job,
                           status='failed', event_key='synthetic-busy', before_data=True)
    [hold] = holds(joint, job)
    assert hold['kind'] == 'no_route'
    HoldStore(joint.delivery).check_again(hold['id'], version=hold['version'], actor=ANNE)
    assert await worker.step() is True
    assert await worker.step() is False  # nothing more to send
    first_call, second_call = joint.ami.calls
    assert (first_call['trunk'] or 'sip') != (second_call['trunk'] or 'sip')  # the other account
    assert_once_each_and_bound(joint, [job])
    record(Case(GROUP, 'long preparation, failure before data, fallback', ('long preparation', 'fallback'),
                ('first account failed before data', 'second account sent once'), INVARIANT))


@pytest.mark.asyncio
async def test_an_uncertain_send_after_a_long_preparation_is_never_sent_again_by_any_worker(joint):
    from api.app.routing.fallback import FallbackScheduler
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', CHEAPEST)]})
    [job] = faxes(joint, 1)
    lost = []

    async def answer_lost(*args, **kwargs):
        lost.append(kwargs.get('attempt_id'))
        raise TimeoutError('synthetic lost acknowledgement')
    joint.ami.originate_sendfax = answer_lost
    workers = [OutboundWorker(joint.delivery, slow(joint), lease_seconds=LEASE) for _ in range(2)]
    await asyncio.gather(*(worker.step() for worker in workers))
    assert len(lost) == 1 and joint.delivery.get(job)['state'] == 'reconciliation_required'
    # Neither worker, the fallback scheduler, nor a sweep of expired leases sends it again.
    assert FallbackScheduler(joint.delivery, joint.routes).step() is False
    assert joint.delivery.recover_expired() == 0
    assert not any(await asyncio.gather(*(worker.step() for worker in workers)))
    assert len(lost) == 1 and len(went_on(joint, job)) == 1
    record(Case(GROUP, 'long preparation, lost acknowledgement, two workers', ('long preparation', 'uncertain'),
                ('uncertain', 'not resent'), INVARIANT))


@pytest.mark.asyncio
async def test_a_fax_waiting_for_its_numbers_line_never_holds_up_a_fax_to_another_number(joint):
    from api.app.pages import capability
    other = '+12025550199'
    capability.records_for(joint.engine).record_observation(
        other, source='f' * 32, engine='hylafax', values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1})
    publish(joint, {'format': 1, 'routes': [rule('r-cheap', CHEAPEST)]})
    busy, waiting = faxes(joint, 2)  # the number takes one call at a time
    later = accept(joint, to=other, pages=2)
    write_document(joint, later)
    worker = OutboundWorker(joint.delivery, slow(joint), lease_seconds=LEASE)
    assert await worker.step() is True  # the first call to NUMBER is on the line
    for _ in range(3):
        await worker.step()
    assert joint.delivery.get(later)['state'] == 'in_progress'  # went while the second waited for the line
    assert joint.delivery.get(waiting)['state'] == 'ready'
    end_calls(joint)
    worker.paused.clear()
    assert await worker.step() is True
    assert joint.delivery.get(waiting)['state'] == 'in_progress'
    assert_once_each_and_bound(joint, [busy, waiting, later])
    record(Case(GROUP, 'line contention across numbers', ('one line', 'two numbers'), ('no starvation',),
                INVARIANT))
