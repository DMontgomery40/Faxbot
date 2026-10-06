"""Sending faxes to one number together: holding, release, the shared call and each fax's own outcome."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_profiles import ProviderConfiguration
from api.app.outbound_store import DeliveryConflict, OutboundStore
from api.app.outbound_transport import CapturedTransport
from api.app.outbound_worker import BatchSplit, OutboundWorker
from api.app.request_identity import IdempotentReplay, RequestIdentity
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.store import RouteStore
from api.app.routing.transport import RoutedTransport
from api.app.batching import acceptance, image, policy, results
from api.app.batching import store as batching
from api.app.batching.outcomes import map_call
from api.app.batching.transport import BatchingTransport


NUMBER = '+15555550123'
T0 = datetime(2026, 10, 3, 22, 30)


def card(provider='sip', *, minute='0.005', page='0', call='0', increment=60, minimum=60, monthly=None):
    return RateCard(None, provider, 'outbound', provider.title(), 'USD', parse_amount(minute), parse_amount(page),
                    parse_amount(call), increment, minimum, None, datetime(2026, 10, 3),
                    None if monthly is None else parse_amount(monthly, whole_digits=4))


class Ami:
    def __init__(self, connected=True):
        self._connected = asyncio.Event()
        if connected:
            self._connected.set()
        self.calls = []

    async def originate_sendfax(self, job_id, dest, tiff_path, *, attempt_id=None, call=None):
        self.calls.append((job_id, dest, tiff_path, attempt_id))


@pytest.fixture
def sip(database, tmp_path):
    upgrade_schema(database)
    data = tmp_path / 'faxdata'
    data.mkdir()
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'FAX_DISABLED': 'false',
                                                   'FAX_DATA_DIR': str(data)})
    snapshot = configuration.initialize(values, actor='test', providers={
        'outbound': ProviderConfiguration('sip', traits={'requires_tiff': True})})
    routes = RouteStore(database, sip_preset=lambda: '')
    routes.replace_cards([card()])
    settings = batching.BatchingSettings(database)
    settings.save(NUMBER, enabled=True, recipient_agreed=True, actor='principal:p1', actor_name='Owner')
    return configuration, OutboundStore(configuration), snapshot, routes, data


def write_fax(data, job_id, pages):
    from reportlab.pdfgen import canvas
    from api.app.conversion import pdf_to_tiff
    pdf = canvas.Canvas(str(data / (job_id + '.pdf')))
    for number in range(pages):
        pdf.drawString(72, 720, f'Synthetic fax {job_id[:6]} page {number + 1}')
        pdf.showPage()
    pdf.save()
    pdf_to_tiff(str(data / (job_id + '.pdf')), str(data / (job_id + '.tiff')))


def accept(sip, *, pages=1, sender='key:front', urgent=False, at=T0, wait=600, held=True, files=False,
           request_identity=None, number=NUMBER):
    """Acceptance and its hold in one installation transaction, as POST /fax does."""
    configuration, _, snapshot, _, data = sip
    job_id = uuid4().hex
    job = {'id': job_id, 'to_number': number, 'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
           'pages': pages, 'created_at': at, 'updated_at': at}
    with configuration._locked() as connection:
        configuration._accept_outbound_on(connection, snapshot.active, job, request_identity=request_identity)
        if held:
            plan = batching.HoldPlan(number, sender, 'Front Desk', pages, urgent, wait)
            batching.hold_on(connection, batching.tables(configuration.engine, connection), job_id, plan, at)
    if files:
        write_fax(data, job_id, pages)
    return job_id


def state(sip, job_id):
    return sip[1].get(job_id)['state']


def row(sip, job_id):
    return batching.member(sip[0].engine, job_id)


# Holding and release ------------------------------------------------------------------

def test_a_waiting_fax_is_not_claimed_until_its_wait_ends_then_goes_with_the_others(sip):
    _, delivery, *_ = sip
    first = accept(sip, at=T0)
    second = accept(sip, pages=2, at=T0 + timedelta(minutes=3))
    assert delivery.claim('worker', now=T0 + timedelta(minutes=9)) is None
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=10))
    assert [member.job_id for member in claim.members] == [first, second]
    assert claim.job_id == first and claim.members[0] == claim.everyone[0]
    rows = [row(sip, first), row(sip, second)]
    assert [(r['state'], r['document_number'], r['documents'], r['first_page'], r['last_page']) for r in rows] == [
        ('together', 1, 2, 1, 2), ('together', 2, 2, 3, 5)]
    assert {r['batch_id'] for r in rows} == {claim.attempt_id}
    assert rows[0]['reference'] == 'Faxbot ' + first[:8]
    assert 'sent_together' in [event['kind'] for event in delivery.history(second)]
    assert delivery.claim('worker', now=T0 + timedelta(minutes=10)) is None


def test_the_call_goes_at_once_when_the_next_fax_would_exceed_the_page_cap(sip):
    configuration, delivery, *_ = sip
    batching.BatchingSettings(configuration.engine).save(NUMBER, enabled=True, actor='principal:p1', max_pages=5)
    first, second = accept(sip, at=T0), accept(sip, at=T0 + timedelta(seconds=1))
    third = accept(sip, pages=2, at=T0 + timedelta(seconds=2))
    claim = delivery.claim('worker', now=T0 + timedelta(seconds=3))
    assert [member.job_id for member in claim.members] == [first, second]
    assert row(sip, third)['state'] == 'waiting'
    # The third fax now waits alone until its own wait ends, then goes on its own.
    assert delivery.claim('worker', now=T0 + timedelta(minutes=5)) is None
    alone = delivery.claim('worker', now=T0 + timedelta(minutes=10, seconds=2))
    assert alone.job_id == third and alone.members == () and row(sip, third)['state'] == 'separate'


def test_send_now_takes_the_waiting_faxes_with_it(sip):
    _, delivery, *_ = sip
    first = accept(sip, at=T0)
    urgent = accept(sip, at=T0 + timedelta(minutes=1), urgent=True)
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=1))
    assert [member.job_id for member in claim.members] == [first, urgent]


def test_send_now_on_a_waiting_fax_releases_its_group_and_refuses_one_already_sent(sip):
    _, delivery, *_ = sip
    first, second = accept(sip, at=T0), accept(sip, at=T0)
    assert delivery.claim('worker', now=T0 + timedelta(minutes=1)) is None
    assert delivery.urge(second, now=T0 + timedelta(minutes=1)) is True
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=1))
    assert {member.job_id for member in claim.members} == {first, second}
    assert delivery.urge(second) is False


def test_faxes_from_different_senders_never_share_a_call_unless_the_number_allows_it(sip):
    configuration, delivery, *_ = sip
    # Two separate calls to one number in a row: this number takes calls at once here (capacity.py
    # otherwise lets the second wait for the first).
    from api.app.routing.store import RouteStore
    RouteStore(configuration.engine).update_destination(NUMBER, max_calls=0)
    mine, theirs = accept(sip, sender='key:front'), accept(sip, sender='principal:other', at=T0 + timedelta(seconds=1))
    later = T0 + timedelta(minutes=11)
    first = delivery.claim('worker', now=later)
    second = delivery.claim('worker', now=later)
    assert {first.job_id, second.job_id} == {mine, theirs}
    assert first.members == () and second.members == ()
    # Those two calls end, so both trunk lines are free for the next call.
    for claim in (first, second):
        assert delivery.begin_submission(claim, now=later)
        delivery.record_receipt(claim, provider_sid='SID' + claim.attempt_id[:8], status='success', now=later)
    batching.BatchingSettings(configuration.engine).save(NUMBER, enabled=True, actor='principal:p1',
                                                          mixed_senders=True)
    a, b = accept(sip, sender='key:front'), accept(sip, sender='principal:other', at=T0 + timedelta(seconds=1))
    together = delivery.claim('worker', now=later)
    assert [member.job_id for member in together.members] == [a, b]


def test_turning_a_number_off_releases_its_waiting_faxes_one_at_a_time(sip):
    configuration, delivery, *_ = sip
    # Two separate calls to one number in a row: this number takes calls at once here (capacity.py
    # otherwise lets the second wait for the first).
    from api.app.routing.store import RouteStore
    RouteStore(configuration.engine).update_destination(NUMBER, max_calls=0)
    first, second = accept(sip, at=T0), accept(sip, at=T0 + timedelta(seconds=1))
    batching.BatchingSettings(configuration.engine).save(NUMBER, enabled=False, actor='principal:p1')
    claims = [delivery.claim('worker', now=T0 + timedelta(seconds=2)) for _ in range(2)]
    assert [claim.job_id for claim in claims] == [first, second]
    assert all(claim.members == () for claim in claims)


def test_an_ordinary_fax_is_claimed_while_others_wait(sip):
    _, delivery, *_ = sip
    accept(sip, at=T0)
    plain = accept(sip, held=False, at=T0 + timedelta(seconds=1))
    assert delivery.claim('worker', now=T0 + timedelta(seconds=2)).job_id == plain


def test_turning_on_needs_the_recipients_agreement_and_every_change_is_kept(sip):
    configuration, *_ = sip
    settings = batching.BatchingSettings(configuration.engine)
    with pytest.raises(batching.BatchingInputError):
        settings.save('+15555550999', enabled=True, actor='principal:p1')
    setting, action = settings.save('+15555550999', enabled=True, recipient_agreed=True, actor='principal:p1',
                                    actor_name='Owner', max_wait_seconds=300)
    assert action == 'on' and setting['max_wait_seconds'] == 300 and setting['version'] == 1
    assert settings.save('+15555550999', enabled=True, actor='principal:p1', max_wait_seconds=300)[1] is None
    assert settings.save('+15555550999', enabled=True, actor='principal:p1', max_pages=12)[1] == 'changed'
    with pytest.raises(batching.BatchingConflict):
        settings.save('+15555550999', enabled=False, actor='principal:p1', expected_version=1)
    assert settings.save('+15555550999', enabled=False, actor='principal:p1')[1] == 'off'
    assert [change['action'] for change in settings.history('+15555550999')] == ['off', 'changed', 'on']
    # The change to 12 pages did not record the recipient's agreement again, so its row does not claim it.
    assert [change['recipient_agreed'] for change in settings.history('+15555550999')] == [0, 0, 1]
    for bad in ({'max_wait_seconds': 30}, {'max_pages': 1}, {'mixed_senders': 'yes'}):
        with pytest.raises(batching.BatchingInputError):
            settings.save('+15555550999', enabled=True, recipient_agreed=True, actor='principal:p1', **bad)


# Which routes hold a fax -------------------------------------------------------------

@pytest.mark.parametrize('rates, saves, words', [
    (dict(), True, 'with a 1-minute minimum'),
    (dict(minute='0', call='0.01', minimum=0, increment=1), True, 'for each call'),
    (dict(minute='0', page='0.07', minimum=0), False, 'charges by the page'),
    (dict(minute='0', minimum=0, monthly='19.99'), False, 'flat plan'),
    (dict(minute='0.006', increment=1, minimum=0), False, 'by the second'),
])
def test_only_a_trunk_billed_per_call_or_with_a_minimum_saves(rates, saves, words):
    found = card(**rates)
    assert policy.card_saves(found) is saves
    assert words in policy.card_sentence(found, 'your SIP trunk')


def test_a_non_saving_route_never_holds_a_fax(sip):
    configuration, delivery, snapshot, routes, _ = sip
    actor = type('Actor', (), {'replay_scope': 'key:front', 'principal_id': 'p1'})()
    active = configuration.read().active
    profile = configuration.read_profile(active.profile_id('outbound'))
    plan = acceptance.hold_plan(configuration.engine, active, profile, destination=NUMBER, pages=2, actor=actor)
    assert plan is not None and plan.wait_seconds == 600 and plan.pages == 2
    assert acceptance.hold_plan(configuration.engine, active, profile, destination=NUMBER, pages=30,
                                actor=actor) is None  # 30 pages and a separator exceed the cap
    routes.replace_cards([card(minute='0', page='0.07', minimum=0)])
    assert acceptance.hold_plan(configuration.engine, active, profile, destination=NUMBER, pages=2, actor=actor) is None
    routes.replace_cards([card(minute='0', minimum=0, monthly='19.99')])
    assert acceptance.hold_plan(configuration.engine, active, profile, destination=NUMBER, pages=2, actor=actor) is None
    routes.replace_cards([card()])
    humblefax = ProviderConfiguration('humblefax', credentials={'access_key': 'a', 'secret_key': 'b'})
    assert acceptance.hold_plan(configuration.engine, active, type(profile)('x', 'x', humblefax),
                                destination=NUMBER, pages=2, actor=actor) is None
    assert acceptance.hold_plan(configuration.engine, active, profile, destination='+15555550777', pages=2,
                                actor=actor) is None  # this number does not send together
    verdict = policy.verdict(type('Choice', (), {'route': type('Route', (), {
        'kind': 'provider', 'provider_id': 'humblefax', 'key': 'humblefax'})(), 'reason': 'preferred'})(),
        card('humblefax', minute='0', minimum=0, monthly='19.99'))
    assert verdict.sentence == "On HumbleFax's flat plan, sending together saves nothing, so faxes go straight away."


def test_an_idempotent_replay_returns_the_first_fax_and_holds_nothing_new(sip):
    configuration, delivery, *_ = sip
    intent = RequestIdentity.from_key('same-fax', principal_scope='key:front', fingerprint='a' * 64)
    first = accept(sip, request_identity=intent)
    with pytest.raises(IdempotentReplay) as replay:
        accept(sip, request_identity=intent)
    assert replay.value.job_id == first
    t = batching.tables(configuration.engine)
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t['outbound_batch_members'])) == 1


# The shared call -------------------------------------------------------------------------

def transport(sip, ami):
    configuration, delivery, _, routes, _ = sip
    runtime = type('Runtime', (), {'frame': staticmethod(_frame)})()
    return BatchingTransport(RoutedTransport(CapturedTransport(delivery, runtime, ami=ami), direct=None,
                                             route_store=routes))


class _frame:
    def __init__(self, revision):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_one_call_carries_both_faxes_and_each_keeps_its_own_record(sip):
    configuration, delivery, _, routes, data = sip
    first = accept(sip, pages=2, files=True, at=datetime.utcnow() - timedelta(minutes=11))
    second = accept(sip, pages=1, files=True, at=datetime.utcnow() - timedelta(minutes=10))
    ami = Ami()
    assert await OutboundWorker(delivery, transport(sip, ami)).step() is True
    [(job_id, dest, tiff, attempt)] = ami.calls
    assert job_id == first and dest == NUMBER and Path(tiff).name == f'batch-{attempt}.tiff'
    assert image.page_count(tiff) == 1 + 2 + 1 + 1
    assert [state(sip, job) for job in (first, second)] == ['in_progress', 'in_progress']
    assert {routes.decision(delivery.get(job)['attempt_id'])['route'] for job in (first, second)} == {'sip'}
    event = {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': first, 'AttemptID': attempt,
             'Status': 'SUCCESS', 'Pages': '5'}
    assert results.apply_fax_result(delivery, event) is True
    assert [state(sip, job) for job in (first, second)] == ['success', 'success']
    assert not Path(tiff).exists()
    # The same report again changes nothing; an ordinary call is not a shared one.
    assert results.apply_fax_result(delivery, event) is True
    assert [state(sip, job) for job in (first, second)] == ['success', 'success']
    assert results.apply_fax_result(delivery, {**event, 'AttemptID': uuid4().hex}) is False


@pytest.mark.asyncio
async def test_a_call_that_cannot_go_over_the_trunk_is_split_and_nothing_fails(sip):
    _, delivery, *_ = sip
    first = accept(sip, files=True, at=datetime.utcnow() - timedelta(minutes=11))
    second = accept(sip, files=True, at=datetime.utcnow() - timedelta(minutes=10))
    ami = Ami(connected=False)
    await OutboundWorker(delivery, transport(sip, ami)).step()
    assert ami.calls == []
    assert [state(sip, job) for job in (first, second)] == ['ready', 'ready']
    assert [row(sip, job)['state'] for job in (first, second)] == ['separate', 'separate']
    assert 'batch_split' in [event['kind'] for event in delivery.history(first)]
    claim = delivery.claim('worker')
    assert claim.job_id == first and claim.members == ()


@pytest.mark.asyncio
async def test_a_fax_whose_image_is_missing_goes_on_its_own_and_the_rest_go_together(sip):
    _, delivery, _, _, data = sip
    earlier = datetime.utcnow() - timedelta(minutes=11)
    first, broken, third = (accept(sip, files=True, at=earlier + timedelta(seconds=n)) for n in range(3))
    (data / (broken + '.tiff')).unlink()
    ami = Ami()
    worker = OutboundWorker(delivery, transport(sip, ami))
    await worker.step()
    assert ami.calls == [] and row(sip, broken)['state'] == 'separate'
    assert [row(sip, job)['state'] for job in (first, third)] == ['waiting', 'waiting']
    await worker.step()
    [(job_id, _, tiff, _)] = ami.calls
    assert job_id == first and image.page_count(tiff) == 4
    assert [state(sip, job) for job in (first, third)] == ['in_progress', 'in_progress']


def test_an_unplaced_call_whose_worker_stopped_waits_again_and_forms_a_new_call(sip):
    _, delivery, *_ = sip
    first, second = accept(sip, at=T0), accept(sip, at=T0 + timedelta(seconds=1))
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=10), lease_seconds=5)
    delivery.recover_expired(now=T0 + timedelta(minutes=10, seconds=6))
    assert [row(sip, job)['state'] for job in (first, second)] == ['waiting', 'waiting']
    again = delivery.claim('worker', now=T0 + timedelta(minutes=10, seconds=6))
    assert [member.job_id for member in again.members] == [first, second]
    assert again.attempt_id != claim.attempt_id


def test_the_durable_marker_covers_every_fax_in_the_call_or_none(sip):
    _, delivery, *_ = sip
    first, second = accept(sip), accept(sip)
    now = T0 + timedelta(minutes=10)
    claim = delivery.claim('worker', now=now, lease_seconds=30)
    stale = type(claim.members[1])(**{**claim.members[1].__dict__, 'token': 'stolen'})
    broken = type(claim)(**{**claim.__dict__, 'members': (claim.members[0], stale)})
    assert delivery.begin_submission(broken, now=now) is False
    assert [state(sip, job) for job in (first, second)] == ['preparing', 'preparing']
    assert delivery.begin_submission(claim, now=now) is True
    assert [state(sip, job) for job in (first, second)] == ['submitting', 'submitting']
    assert delivery.record_uncertain(claim, now=now) is True
    assert [state(sip, job) for job in (first, second)] == ['reconciliation_required'] * 2


# Outcomes ---------------------------------------------------------------------------------

def members(*pages):
    rows, first = [], 1
    for number, count in enumerate(pages, start=1):
        rows.append({'id': f'job-{number}', 'attempt_id': f'attempt-{number}', 'first_page': first,
                     'last_page': first + count})
        first += count + 1
    return rows


def outcome(found):
    return [(item.status, item.category) for item in found]


def test_full_success_delivers_every_fax():
    assert outcome(map_call(members(2, 1, 3), succeeded=True, confirmed_pages=9)) == [('success', None)] * 3


def test_failure_after_document_one_delivers_only_document_one():
    found = map_call(members(2, 1, 3), succeeded=False, confirmed_pages=3, failure_sentence='Busy.')
    assert outcome(found) == [('success', None), ('failed', None), ('failed', None)]
    assert found[1].sentence == 'The call failed before this fax was sent.'


def test_failure_on_a_separator_page_fails_that_fax_without_holding_it_for_a_person():
    # Pages: 1 sep, 2-3 doc one, 4 sep, 5 doc two, 6 sep ... the call ended with page 5 confirmed.
    found = map_call(members(2, 1, 3), succeeded=False, confirmed_pages=5)
    assert outcome(found) == [('success', None), ('success', None), ('failed', None)]


def test_a_partly_confirmed_fax_fails_and_waits_for_a_person():
    found = map_call(members(2, 3), succeeded=False, confirmed_pages=5)
    assert outcome(found) == [('success', None), ('failed', 'partly_sent')]
    assert found[1].sentence == 'The call failed after 1 of its 3 pages; check before sending again.'
    began = map_call(members(2, 3), succeeded=False, confirmed_pages=4)
    assert outcome(began)[1] == ('failed', 'partly_sent')
    assert began[1].sentence == 'The call failed as this fax began; check before sending again.'
    assert all(len(item.sentence or '') <= 80 for item in found + began)


def test_no_confirmed_page_count_leaves_every_fax_uncertain():
    assert outcome(map_call(members(1, 1), succeeded=False, confirmed_pages=None)) == [
        ('unconfirmed', 'pages_unconfirmed')] * 2


def test_nothing_confirmed_fails_every_fax_with_the_calls_own_sentence():
    found = map_call(members(1, 1), succeeded=False, confirmed_pages=0, failure_sentence='The number was busy.')
    assert [item.sentence for item in found] == ['The number was busy.'] * 2


def _submitted(sip, *pages):
    _, delivery, *_ = sip
    jobs = [accept(sip, pages=count, at=T0 + timedelta(seconds=n)) for n, count in enumerate(pages)]
    now = T0 + timedelta(minutes=10)
    claim = delivery.claim('worker', now=now)
    assert delivery.begin_submission(claim, now=now)
    delivery.record_receipt(claim, provider_sid=claim.job_id, status='in_progress', now=now)
    return jobs, claim


def test_results_apply_per_fax_and_a_partly_sent_fax_is_never_routed_again(sip):
    configuration, delivery, *_ = sip
    calls = []
    OutboundStore.fallback_policy = lambda job, attempt: calls.append(job) or False
    try:
        jobs, claim = _submitted(sip, 1, 2, 1)
        event = {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': 'FAILED', 'Pages': '3'}
        assert results.apply_fax_result(delivery, event) is True
    finally:
        OutboundStore.fallback_policy = None
    assert [state(sip, job) for job in jobs] == ['success', 'failed', 'failed']
    with configuration.engine.connect() as connection:
        attempts = {job: connection.execute(sa.select(delivery.attempts).where(
            delivery.attempts.c.id == delivery.get(job)['attempt_id'])).mappings().one() for job in jobs}
        errors = {job: connection.scalar(sa.select(configuration.jobs.c.error).where(configuration.jobs.c.id == job))
                  for job in jobs}
    assert attempts[jobs[1]]['error_category'] == 'partly_sent'
    assert attempts[jobs[2]]['error_category'] is None
    assert calls == [jobs[2]]  # only the fax that was never sent may use another route
    assert errors[jobs[1]] == 'The call failed as this fax began; check before sending again.'


def test_a_result_without_pages_leaves_every_fax_waiting_for_a_person(sip):
    _, delivery, *_ = sip
    jobs, claim = _submitted(sip, 1, 1)
    event = {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': 'FAILED'}
    assert results.apply_fax_result(delivery, event) is True
    assert [state(sip, job) for job in jobs] == ['reconciliation_required'] * 2
    assert results.apply_fax_result(delivery, event) is True
    history = [event['kind'] for event in delivery.history(jobs[1])]
    assert history.count('submission_uncertain') == 1


def test_a_call_that_never_connected_fails_every_fax(sip):
    _, delivery, *_ = sip
    jobs, claim = _submitted(sip, 1, 1)
    event = {'Event': 'OriginateResponse', 'Response': 'Failure', 'Reason': '5',
             'ActionID': f'faxbot:{claim.job_id}:{claim.attempt_id}'}
    assert results.apply_originate_failure(delivery, event, failure_sentence='The number was busy.') is True
    assert [state(sip, job) for job in jobs] == ['failed', 'failed']


def test_a_result_naming_another_fax_as_the_first_is_refused(sip):
    _, delivery, *_ = sip
    jobs, claim = _submitted(sip, 1, 1)
    with pytest.raises(DeliveryConflict):
        results.apply_fax_result(delivery, {'JobID': jobs[1], 'AttemptID': claim.attempt_id, 'Status': 'SUCCESS'})
    assert [state(sip, job) for job in jobs] == ['in_progress', 'in_progress']


# Money ---------------------------------------------------------------------------------------

def test_the_call_is_costed_once_on_the_fax_that_placed_it_and_shares_split_by_pages(sip):
    from api.app.batching import money
    from api.app.routing.capture import CostRecorder
    configuration, delivery, _, routes, _ = sip
    jobs, claim = _submitted(sip, 1, 2, 1)
    for job in jobs:
        attempt = delivery.get(job)['attempt_id']
        routes.record_decision(attempt_id=attempt, job_id=job, destination=NUMBER, route='sip',
                               reason='configured', provider_id='sip')
    results.apply_fax_result(delivery, {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': 'SUCCESS',
                                        'Pages': '7'})
    riders = [delivery.get(job)['attempt_id'] for job in jobs[1:]]
    assert routes.rides_in_another_call(claim.attempt_id) is False
    assert all(routes.rides_in_another_call(attempt) for attempt in riders)
    CostRecorder(routes, observed_seconds=lambda target: 100).step()
    assert routes.decision(claim.attempt_id)['estimated_cost_micros'] == 10_000  # 100 s: two whole minutes
    assert all(routes.decision(attempt)['outcome'] == 'pending' for attempt in riders)
    assert routes.route_stats(NUMBER)['sip'].attempts == 1
    member = batching.member(configuration.engine, jobs[1])
    found = money.share(routes, configuration.engine, member)
    assert found['amount_micros'] == 10_000 * 3 // 7 + 1 and found['basis'] == 'estimated'
    assert found['sentence'] == "Its share of the call's charge, split by pages: $0.004286 of $0.01 (estimate)."
    saved = money.savings(routes, configuration.engine, NUMBER)
    # Separately: three calls of one minute or more (1 + 2 + 1 pages) = $0.005 + $0.01 + $0.005.
    assert saved['calls_saved'] == 2 and saved['saved'] == {'USD': 10_000}
    assert money.savings_sentence(saved) == 'Last 30 days: 3 faxes in 1 call, 2 calls saved, about $0.01 saved (estimate).'


def test_shares_of_a_call_sum_exactly_to_its_charge_by_largest_remainder_in_call_order(sip):
    """Three one-page faxes split $0.01 (10,000 micros): 3,333.33 each would drift, so the first in the call gets 3,334."""
    from api.app.batching import money
    from api.app.routing.capture import CostRecorder
    from api.app.routing.carriers import CarrierChargeStore
    from api.app.routing.spending import Spending
    configuration, delivery, _, routes, _ = sip
    jobs, claim = _submitted(sip, 1, 1, 1)
    for job in jobs:
        routes.record_decision(attempt_id=delivery.get(job)['attempt_id'], job_id=job, destination=NUMBER,
                               route='sip', reason='configured', provider_id='sip')
    results.apply_fax_result(delivery, {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': 'SUCCESS',
                                        'Pages': '6'})
    CostRecorder(routes, observed_seconds=lambda target: 100).step()
    assert routes.decision(claim.attempt_id)['estimated_cost_micros'] == 10_000
    shares = [money.share(routes, configuration.engine, batching.member(configuration.engine, job))['amount_micros']
              for job in jobs]
    assert shares == [3334, 3333, 3333] and sum(shares) == 10_000
    spending = Spending(routes, CarrierChargeStore(configuration.engine))
    assert [spending.job(job)['estimated_cost'] for job in jobs] == [{'USD': 3334}, {'USD': 3333}, {'USD': 3333}]
    # The rule itself: round each part down, then one unit each to the largest remainders, earlier first on a tie.
    from api.app.routing.costs import split_by_weight
    assert split_by_weight(5, [2, 2, 2]) == [2, 2, 1]
    assert split_by_weight(10_000, [2, 3, 2]) == [2857, 4286, 2857]
    assert split_by_weight(7, [1, 3]) == [2, 5]
    assert split_by_weight(0, [4, 1]) == [0, 0]
    for total, weights in ((1, [1, 1, 1]), (999_999, [2, 7, 3, 5]), (10, [3])):
        assert sum(split_by_weight(total, weights)) == total


def test_a_shared_call_that_cost_more_than_separate_calls_shows_the_loss_never_a_zero_saving(sip):
    from api.app.batching import money
    from api.app.routing.capture import CostRecorder
    from api.app.routing.savings import savings, sending_together
    configuration, delivery, _, routes, _ = sip
    jobs, claim = _submitted(sip, 1, 2, 1)
    for job in jobs:
        routes.record_decision(attempt_id=delivery.get(job)['attempt_id'], job_id=job, destination=NUMBER,
                               route='sip', reason='configured', provider_id='sip')
    results.apply_fax_result(delivery, {'JobID': claim.job_id, 'AttemptID': claim.attempt_id, 'Status': 'SUCCESS',
                                        'Pages': '7'})
    CostRecorder(routes, observed_seconds=lambda target: 100).step()
    # The carrier billed the shared call at $0.05 (a slow call); three separate calls would have cost about $0.02.
    assert routes.ingest_charge(claim.attempt_id, provider_id='sip', charge_id='rec-slow', amount_micros=50_000,
                                currency='USD', billed_seconds=600) == 'new'
    saved = money.savings(routes, configuration.engine, NUMBER)
    assert saved['saved'] == {'USD': -30_000}
    assert money.savings_sentence(saved) == ('Last 30 days: 3 faxes in 1 call, 2 calls saved, but sending together '
                                             'cost about $0.03 more (estimate).')
    together = sending_together(routes, configuration.engine, now=datetime.utcnow(), days=30)
    assert together['saved'] == {'USD': -30_000}
    assert together['sentence'] == ('3 faxes to the same number went in 1 call instead of 3, saving 2 calls, but '
                                    'that call cost about $0.03 more than 3 separate calls.')
    # Costs → Savings sums signed amounts, and its headline says the money went the other way.
    found = savings(routes, configuration.engine)
    assert found['total'] == {'USD': -30_000}
    assert found['total_sentence'] == 'About $0.03 more spent than saved in the last 30 days.'


# The image ------------------------------------------------------------------------------------

def test_the_call_image_copies_each_faxs_pages_unchanged_after_its_separator(tmp_path):
    from PIL import Image, ImageChops
    jobs = [('1' * 32, 2), ('2' * 32, 1)]
    for job, pages in jobs:
        write_fax(tmp_path, job, pages)
    lines = [(job, pages, image.separator_line(n, 2, 'Faxbot ' + job[:8], pages, 'Front Desk'))
             for n, (job, pages) in enumerate(jobs, start=1)]
    assert lines[0][2] == 'Document 1 of 2 · Faxbot 11111111 · 2 pages · from Front Desk'
    out = image.build_call_image(tmp_path, 'a' * 32, lines)
    assert image.page_count(out) == 5
    # Asterisk sends the call image as its own user (the data folder's group): 0640, never world-readable.
    assert oct(out.stat().st_mode & 0o777) == '0o640'
    with Image.open(out) as combined, Image.open(tmp_path / ('1' * 32 + '.tiff')) as original:
        for combined_page, original_page in ((1, 0), (2, 1)):
            combined.seek(combined_page)
            original.seek(original_page)
            assert ImageChops.difference(combined.convert('1'), original.convert('1')).getbbox() is None
    (tmp_path / ('2' * 32 + '.tiff')).unlink()
    with pytest.raises(image.MemberUnusable) as missing:
        image.build_call_image(tmp_path, 'b' * 32, lines)
    assert missing.value.job_id == '2' * 32
