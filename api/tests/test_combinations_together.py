"""Combinations, group 4: sending together x mixed mailboxes x urgency and deadlines x separators or index page x
tariff (brief 104).

The invariant:

- each document keeps its own identity and stays intact: in the one call image, every fax's pages are its own fax
  image's pages pixel for pixel, at the page range its own record names, and each fax keeps its own attempt and
  outcome;
- the recipient's agreement is required: to send together at all, and to an index page instead of separators;
- urgent and deadline faxes never wait past their limit: an urgent fax or one whose send-by time is near goes at the
  next claim and takes the waiting faxes with it, a fax that may wait waits no longer than the number's maximum, and a
  deadline fax's own pages are on the line before its send-by time;
- the shared call's economics come from the real tariff: one charge, from the trunk's rate card, split by each fax's
  own pages, summing exactly to it.

The real claim, worker, batching transport, call image and result mapping; a stand-in AMI that dials nothing. The
rate card is synthetic. SQLite and PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.app import conversion
from api.app.batching import acceptance, money, results
from api.app.batching import store as batching
from api.app.outbound_worker import OutboundWorker
from api.app.routing import schedule
from api.tests.combinations_support import Case, record
from api.tests.test_batching import Ami, NUMBER, T0, accept, card, row, sip, state, transport  # noqa: F401
from api.tests.test_batching_index_page import index_on
from api.tests.test_schema import database  # noqa: F401 - fixture

GROUP = '4 sending together x mailboxes x urgency/deadline x separators/index x tariff'
INVARIANT = ('each document keeps its identity and stays intact; agreement required; urgent and deadline faxes '
             'never wait past their limit; economics from the real tariff')
FRONT, BILLING = 'key:front', 'key:billing'


def pixels(frames):
    return [(frame.size, frame.tobytes()) for frame in frames]


async def send_together(sip, jobs):
    """One worker step: the shared call. Returns (call image frames, the call's attempt, the job that placed it)."""
    delivery = sip[1]
    ami = Ami()
    assert await OutboundWorker(delivery, transport(sip, ami)).step() is True
    [(placed_by, dest, tiff, attempt)] = ami.calls
    assert dest == NUMBER
    return list(conversion.read_fax_frames(tiff)), attempt, placed_by, tiff


def assert_each_document_intact(sip, jobs, frames, layout):
    data = sip[4]
    for job in jobs:
        member = row(sip, job)
        assert member['state'] == 'together' and member['layout'] == layout
        own = list(conversion.read_fax_frames(str(data / f'{job}.tiff')))
        # With separators, a fax's range starts at its own separator page; its pages follow it.
        first = member['first_page'] + (1 if layout == 'separators' else 0)
        # Its own pages, at the range its own record names, pixel for pixel.
        assert pixels(frames[first - 1:member['last_page']]) == pixels(own)
        assert member['last_page'] - first + 1 == len(own) == member['pages']
    # Each fax has its own attempt.
    attempts = {sip[1].get(job)['attempt_id'] for job in jobs}
    assert len(attempts) == len(jobs) and None not in attempts


@pytest.mark.asyncio
@pytest.mark.parametrize('layout', ['separators', 'index_page'])
@pytest.mark.parametrize('mixed', [False, True], ids=['one mailbox', 'two mailboxes'])
async def test_each_fax_keeps_its_own_pages_record_and_outcome_in_one_call(sip, layout, mixed):
    configuration, delivery, *_ = sip
    if layout == 'index_page':
        index_on(sip, mixed_senders=mixed)
    elif mixed:
        batching.BatchingSettings(configuration.engine).save(NUMBER, enabled=True, actor='principal:p1',
                                                              mixed_senders=True)
    earlier = datetime.utcnow() - timedelta(minutes=11)
    jobs = [accept(sip, pages=pages, files=True, at=earlier + timedelta(seconds=n),
                   sender=BILLING if (mixed and n == 1) else FRONT) for n, pages in enumerate((2, 1, 3))]
    frames, attempt, placed_by, tiff = await send_together(sip, jobs)
    assert placed_by == jobs[0]
    marks = 1 if layout == 'index_page' else len(jobs)
    assert len(frames) == marks + 2 + 1 + 3
    assert_each_document_intact(sip, jobs, frames, layout)
    # Each fax's own outcome, from the call's one result.
    results.apply_fax_result(delivery, {'JobID': placed_by, 'AttemptID': attempt, 'Status': 'SUCCESS',
                                        'Pages': str(len(frames))})
    assert [state(sip, job) for job in jobs] == ['success'] * 3
    record(Case(GROUP, f'{layout}, {"two mailboxes" if mixed else "one mailbox"}', ('together', layout) +
                (('mixed mailboxes',) if mixed else ()), ('together', layout, f'{len(jobs)} faxes'), INVARIANT))


def test_faxes_from_two_mailboxes_go_in_separate_calls_unless_the_number_allows_mixing(sip):
    configuration, delivery, *_ = sip
    from api.app.routing.store import RouteStore
    RouteStore(configuration.engine).update_destination(NUMBER, max_calls=0)  # two calls at once are allowed
    front, billing = accept(sip, sender=FRONT, at=T0), accept(sip, sender=BILLING, at=T0 + timedelta(seconds=1))
    later = T0 + timedelta(minutes=11)
    claims = [delivery.claim('worker', now=later), delivery.claim('worker', now=later)]
    assert {claim.job_id for claim in claims} == {front, billing}
    assert all(claim.members == () for claim in claims)
    record(Case(GROUP, 'two mailboxes without mixing allowed', ('together', 'mixed mailboxes'), ('separate calls',),
                INVARIANT, 'impossible', alternate='each fax in its own call'))


def test_the_recipients_agreement_is_required_to_send_together_and_to_an_index_page(sip):
    settings = batching.BatchingSettings(sip[0].engine)
    other = '+15555550999'
    with pytest.raises(batching.BatchingInputError) as refused:
        settings.save(other, enabled=True, actor='principal:p1')
    assert str(refused.value) == 'Record that the recipient agreed before turning this on.'
    with pytest.raises(batching.BatchingInputError) as index_refused:
        settings.save(NUMBER, enabled=True, boundaries='index_page', actor='principal:p1')
    from api.app.batching import policy
    assert str(index_refused.value) == policy.REFUSALS['index_page']
    # Without the agreement to an index page, a call keeps a separator page before each fax.
    jobs = [accept(sip, pages=1, at=T0 + timedelta(seconds=n)) for n in range(2)]
    claim = sip[1].claim('worker', now=T0 + timedelta(minutes=11))
    assert [member.job_id for member in claim.members] == jobs
    assert [row(sip, job)['layout'] for job in jobs] == ['separators', 'separators']
    assert row(sip, jobs[1])['last_page'] == 4  # separator, page, separator, page
    record(Case(GROUP, 'index page without the agreement', ('together', 'index_page'), ('together', 'separators'),
                INVARIANT, 'impossible', alternate=f'refused: "{policy.REFUSALS["index_page"]}"; '
                                                   'separators before each document'))
    record(Case(GROUP, 'sending together without the agreement', ('together',), ('own call',), INVARIANT,
                'impossible', alternate='refused: "Record that the recipient agreed before turning this on."'))


def test_an_urgent_fax_does_not_wait_and_takes_the_waiting_faxes_with_it(sip):
    delivery = sip[1]
    waiting = accept(sip, pages=2, at=T0, sender=FRONT)
    assert delivery.claim('worker', now=T0 + timedelta(minutes=1)) is None  # it may wait ten minutes
    urgent = accept(sip, pages=1, at=T0 + timedelta(minutes=1), urgent=True, sender=FRONT)
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=1))
    assert [member.job_id for member in claim.members] == [waiting, urgent]
    record(Case(GROUP, 'urgent fax with one waiting', ('together', 'urgent'), ('together', 'went at once'),
                INVARIANT))


def test_a_fax_that_may_wait_waits_no_longer_than_the_numbers_maximum(sip):
    delivery = sip[1]
    first = accept(sip, pages=1, at=T0, wait=600)
    accept(sip, pages=1, at=T0 + timedelta(seconds=30), wait=600)
    assert delivery.claim('worker', now=T0 + timedelta(seconds=599)) is None
    claim = delivery.claim('worker', now=T0 + timedelta(seconds=600))
    assert claim is not None and claim.job_id == first and len(claim.members) == 2
    record(Case(GROUP, 'waiting faxes, maximum wait', ('together', 'wait'), ('together', 'went at its maximum'),
                INVARIANT))


def accept_with_send_by(sip, *, pages, send_by, now, sender=FRONT):
    """Acceptance as POST /fax does it for a fax with a send-by time: the hold plan (``batching.acceptance``) with
    "send now" when the send-by time leaves no room to wait (``schedule.too_soon_to_wait``)."""
    configuration, _, snapshot, _, data = sip
    profile = SimpleNamespace(configuration=SimpleNamespace(manifest=None, provider_id='sip'))
    plan = acceptance.hold_plan(configuration.engine, snapshot.active, profile, destination=NUMBER, pages=pages,
                                actor=SimpleNamespace(replay_scope=sender, principal_id=None),
                                send_now=schedule.too_soon_to_wait(send_by, pages, now))
    job_id = uuid4().hex
    job = {'id': job_id, 'to_number': NUMBER, 'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
           'pages': pages, 'created_at': now, 'updated_at': now, 'send_by': send_by}
    with configuration._locked() as connection:
        configuration._accept_outbound_on(connection, snapshot.active, job)
        if plan is not None:
            batching.hold_on(connection, batching.tables(configuration.engine, connection), job_id, plan, now)
    return job_id, plan


def test_a_fax_whose_send_by_time_is_near_goes_at_once_and_one_far_off_may_wait(sip):
    delivery = sip[1]
    waiting = accept(sip, pages=2, at=T0)
    near, plan = accept_with_send_by(sip, pages=1, send_by=T0 + timedelta(minutes=40), now=T0 + timedelta(minutes=1))
    assert plan.urgent is True
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=1))
    assert [member.job_id for member in claim.members] == [waiting, near]
    far, plan = accept_with_send_by(sip, pages=1, send_by=T0 + timedelta(hours=5), now=T0 + timedelta(minutes=2))
    assert plan.urgent is False and plan.wait_seconds <= 3600
    record(Case(GROUP, 'deadline near and far', ('together', 'send-by'), ('near: went at once', 'far: may wait'),
                INVARIANT))


@pytest.mark.xfail(strict=True, reason="defect C4: a deadline fax that takes waiting faxes with it goes last in the "
                                       "call, after their pages, and can finish past its send-by time")
def test_a_deadline_faxs_own_pages_are_on_the_line_before_its_send_by_time(sip):
    """A number that sends up to 200 pages in one call; 190 pages wait to go together. A one-page fax due in 35
    minutes cannot wait, so it goes at once and takes them with it. Its page should be sent before its send-by time:
    first in the call, or on its own. The call puts it last, after every waiting page, about 40 minutes of line time
    by the predictor's own arithmetic (``schedule.call_time``)."""
    configuration, delivery, *_ = sip
    batching.BatchingSettings(configuration.engine).save(NUMBER, enabled=True, actor='principal:p1', max_pages=200)
    for n in range(10):
        accept(sip, pages=18, at=T0 + timedelta(seconds=n))
    now = T0 + timedelta(minutes=1)
    near, plan = accept_with_send_by(sip, pages=1, send_by=now + timedelta(minutes=35), now=now)
    assert plan is not None and plan.urgent is True
    claim = delivery.claim('worker', now=now)
    assert near in [member.job_id for member in claim.members]
    member = row(sip, near)
    on_the_line = now + schedule.call_time(member['last_page'])
    assert on_the_line <= now + timedelta(minutes=35)


def _priced_call(sip, *pages):
    from api.tests.test_batching_index_page import _priced_call as priced
    return priced(sip, *pages)


@pytest.mark.parametrize('layout', ['separators', 'index_page'])
def test_one_charge_from_the_trunks_card_split_by_each_faxs_own_pages(sip, layout):
    from api.app.routing.costs import attempt_cost
    configuration, _, _, routes, _ = sip
    if layout == 'index_page':
        index_on(sip)
    jobs, claim = _priced_call(sip, 1, 2, 1)
    # One charge for the call: the card's own arithmetic over the call's 100 seconds (CostRecorder above).
    charge = routes.decision(claim.attempt_id)['estimated_cost_micros']
    assert charge == attempt_cost(card(), seconds=100, pages=row(sip, jobs[-1])['last_page'], delivered=True)
    shares = [money.share(routes, configuration.engine, batching.member(configuration.engine, job))['amount_micros']
              for job in jobs]
    assert sum(shares) == charge
    weights = [1, 2, 1] if layout == 'index_page' else [2, 3, 2]  # with separators, each fax's separator is its own
    # Largest remainder in call order: each share is its own pages' part, rounded so the parts sum to the charge.
    assert all(abs(share * sum(weights) - charge * weight) < sum(weights) for share, weight in zip(shares, weights))
    found = money.savings(routes, configuration.engine, NUMBER)
    assert found['calls_saved'] == 2
    record(Case(GROUP, f'shared call economics, {layout}', ('together', layout, 'per-minute card'),
                (f'charge {charge}', f'shares {shares}'), INVARIANT))


def test_a_registered_senders_recipient_never_holds_a_fax_to_send_together(sip):
    """A pinned recipient (routing/sender_pins.py) gets each fax in its own call."""
    from api.app.routing import sender_pins
    configuration, _, snapshot, _, _ = sip
    pins = sender_pins.PINS
    with configuration.engine.begin() as connection:
        connection.execute(pins.insert().values(id=uuid4().hex, recipient=NUMBER, account='sip',
                                                caller_id='+13035550100', station_id=None, state='active',
                                                note=None, recorded_by=None, recorded_by_name=None,
                                                created_at=T0))
    assert sender_pins.pinned(configuration.engine, NUMBER)
    profile = SimpleNamespace(configuration=SimpleNamespace(manifest=None, provider_id='sip'))
    plan = acceptance.hold_plan(configuration.engine, snapshot.active, profile, destination=NUMBER, pages=1,
                                actor=SimpleNamespace(replay_scope=FRONT, principal_id=None))
    assert plan is None
    record(Case(GROUP, 'registered sender', ('together',), ('own call',), INVARIANT, 'impossible',
                alternate='each fax in its own call to a pinned recipient'))


def test_a_route_where_one_call_costs_no_less_never_holds_a_fax(sip):
    configuration, _, snapshot, routes, _ = sip
    routes.replace_cards([card(page='0.03', minute='0', minimum=0, increment=1)])
    profile = SimpleNamespace(configuration=SimpleNamespace(manifest=None, provider_id='sip'))
    plan = acceptance.hold_plan(configuration.engine, snapshot.active, profile, destination=NUMBER, pages=1,
                                actor=SimpleNamespace(replay_scope=FRONT, principal_id=None))
    assert plan is None
    record(Case(GROUP, 'per-page trunk', ('together', 'per-page tariff'), ('own call',), INVARIANT, 'impossible',
                alternate='each fax goes straight away in its own call'))
