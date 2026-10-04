"""Telnyx call charges: parsing, the Telnyx client, matching and the billing acceptance table.

Synthetic Telnyx detail records only, shaped like the four real calls of
October 4, 2026 03:00-03:20 UTC (two sent, two received); the HTTP layer is
mocked and nothing here reaches the network.
"""
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.tests.test_outbound_store import installation, accept  # noqa: F401  (fixtures)
from api.tests.test_schema import database  # noqa: F401
from api.app.routing.carriers import (CallView, CarrierChargeStore, CarrierReconciler, fits, match_records,
                                      same_number)
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.spending import Spending
from api.app.routing.store import CaptureTarget, RouteStore
from api.app.routing.telnyx import (CarrierRateLimited, CarrierUnavailable, TelnyxDetailRecords, parse_amount as
                                    parse_cost, parse_record)


OURS, FAR = '+13035550100', '+12025550123'
CALLER_C, CALLER_D = '+17205550111', '+17205550112'
NOW = datetime.utcnow().replace(microsecond=0)
BASE = NOW - timedelta(hours=2)


def at(minutes, seconds=0):
    return BASE + timedelta(minutes=minutes, seconds=seconds)


def iso(moment):
    return moment.isoformat() + 'Z'


def telnyx(record_id, direction, start, answer, finish, cost, *, cli, cld, call_id=None, billed=60, call_sec=None):
    return {'record_type': 'sip-trunking', 'id': record_id, 'direction': direction, 'sip_call_id': call_id or str(uuid4()),
            'cli': cli, 'cld': cld, 'started_at': iso(start), 'answered_at': iso(answer) if answer else None,
            'finished_at': iso(finish), 'call_sec': call_sec if call_sec is not None else int((finish - answer).total_seconds()),
            'billed_sec': billed, 'rate': '0.005', 'cost': cost, 'currency': 'USD'}


# Tonight's shapes: 63 s billed 120 s for 0.01, 45 s billed 60 s for 0.005, two inbound at 0.0032.
def sent_a(cost='0.01', **extra):
    return telnyx('rec-a', 'outbound', at(0, 25), at(0, 26), at(1, 29), cost, cli=OURS, cld=FAR, billed=120, **extra)


def sent_b(cost='0.005', **extra):
    return telnyx('rec-b', 'outbound', at(3, 25), at(3, 26), at(4, 11), cost, cli=OURS, cld=FAR, **extra)


def received_c(cost='0.0032', **extra):
    return telnyx('rec-c', 'inbound', at(14, 4), at(14, 4), at(14, 29), cost, cli=CALLER_C, cld=OURS, **extra)


def received_d(cost='0.0032', **extra):
    return telnyx('rec-d', 'inbound', at(18, 54), at(18, 54), at(19, 25), cost, cli=CALLER_D, cld=OURS, **extra)


class FakeTelnyx:
    """Stands in for TelnyxDetailRecords: serves the current payloads by start time and counts reads."""
    carrier, label = 'telnyx', 'Telnyx'

    def __init__(self, payloads=(), *, key='KEYsynthetic'):
        self.payloads, self.key, self.reads, self.failure = list(payloads), key, 0, None

    def ready(self):
        return bool(self.key)

    def fetch(self, start, end):
        self.reads += 1
        if self.failure is not None:
            raise self.failure
        records = [parse_record(payload) for payload in self.payloads]
        return [record for record in records if record and start <= record.started_at < end], True


# Parsing and the client --------------------------------------------------------------

def test_parse_keeps_unknown_zero_and_credits_distinct():
    priced = parse_record(sent_b())
    assert (priced.amount_micros, priced.raw_amount, priced.currency, priced.billed_seconds) == (5000, '0.005', 'USD', 60)
    assert priced.answered_at == at(3, 26) and priced.direction == 'outbound'
    for missing in (None, ''):
        unknown = parse_record({**sent_b(), 'cost': missing})
        assert unknown is not None and unknown.amount_micros is None and unknown.raw_amount is None
    assert parse_record(sent_b(cost='0')).amount_micros == 0
    assert parse_record(sent_b(cost='-0.01')).amount_micros == -10_000  # the carrier's sign is kept
    assert parse_cost('0.0000001', 'USD')[0] == 1  # rounded up in the carrier's favour
    assert parse_cost('free', 'USD') is None and parse_cost('0.01', 'dollars') is None
    assert parse_record({**sent_b(), 'record_type': 'messaging'}) is None
    assert parse_record({**sent_b(), 'direction': 'sideways'}) is None
    assert parse_record({**sent_b(), 'started_at': 'yesterday'}) is None


def test_client_pages_by_start_time_and_never_needs_more_than_the_key():
    seen = []
    pages = {1: [sent_a(), sent_b()], 2: [received_c()]}

    def handler(request):
        seen.append(request)
        number = int(request.url.params['page[number]'])
        return httpx.Response(200, json={'data': pages[number], 'meta': {'total_pages': 2, 'page_number': number}})
    source = TelnyxDetailRecords(lambda: 'KEYsynthetic',
                                 client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    records, complete = source.fetch(at(0), at(30))
    assert complete and [record.id for record in records] == ['rec-a', 'rec-b', 'rec-c']
    first = seen[0]
    assert first.url.host == 'api.telnyx.com' and first.url.path == '/v2/detail_records'
    assert first.url.params['filter[record_type]'] == 'sip-trunking'
    assert first.url.params['filter[started_at][gte]'] == iso(at(0)) and first.url.params['page[size]'] == '50'
    assert first.headers['authorization'] == 'Bearer KEYsynthetic'
    limited = TelnyxDetailRecords(lambda: 'KEYsynthetic', max_pages=1,
                                  client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    assert limited.fetch(at(0), at(30))[1] is False  # more pages than allowed: not complete


@pytest.mark.parametrize('status, error', [(429, CarrierRateLimited), (401, CarrierUnavailable),
                                           (500, CarrierUnavailable)])
def test_client_failures_are_plain_and_carry_no_response(status, error):
    source = TelnyxDetailRecords(lambda: 'KEYsynthetic', client_factory=lambda: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text='KEYsynthetic secret body'))))
    with pytest.raises(error) as raised:
        source.fetch(at(0), at(30))
    assert 'KEYsynthetic' not in str(raised.value)
    with pytest.raises(CarrierUnavailable):
        TelnyxDetailRecords(lambda: '').fetch(at(0), at(30))
    assert TelnyxDetailRecords(lambda: '').ready() is False


# Matching (pure) ------------------------------------------------------------------------

def view(identity, direction, answer, end, *, local=OURS, remote=FAR, call_id=None, start=None):
    return CallView(identity, direction, call_id, local, remote, start or answer, answer, end)


def test_numbers_match_with_or_without_country_code():
    assert same_number('+13035550100', '13035550100') and same_number('+13035550100', '3035550100')
    assert not same_number('+13035550100', '+13035550101') and not same_number('5550100', '+13035550100')
    assert not same_number(None, '+13035550100')


def test_tonights_calls_match_by_numbers_and_time_without_call_ids():
    records = [parse_record(payload) for payload in (sent_a(), sent_b(), received_c(), received_d())]
    # Faxbot's clock is a few seconds off; its outbound start is the submission time.
    calls = [view('a', 'outbound', at(0, 29), at(1, 31), start=at(0, 20)),
             view('b', 'outbound', at(3, 28), at(4, 14), start=at(3, 20)),
             view('c', 'inbound', at(14, 6), at(14, 31), remote='7205550111'),
             view('d', 'inbound', at(18, 56), at(19, 27), remote=CALLER_D)]
    matches, ambiguous = match_records(calls, calls, records)
    assert ambiguous == set()
    assert {call: [(record.id, method) for record, method in found] for call, found in matches.items()} == {
        'a': [('rec-a', 'time_window')], 'b': [('rec-b', 'time_window')],
        'c': [('rec-c', 'time_window')], 'd': [('rec-d', 'time_window')]}


def test_a_received_call_without_its_dialled_number_needs_the_caller_number():
    record = parse_record(received_c())
    no_did = view('c', 'inbound', at(14, 6), at(14, 31), local=None, remote='7205550111')
    assert match_records([no_did], [no_did], [record])[0] == {'c': [(record, 'time_window')]}
    anonymous = view('c', 'inbound', at(14, 6), at(14, 31), local=None, remote=None)
    assert match_records([anonymous], [anonymous], [record]) == ({}, set())  # time alone never decides
    other_caller = view('c', 'inbound', at(14, 6), at(14, 31), local=None, remote=CALLER_D)
    assert match_records([other_caller], [other_caller], [record]) == ({}, set())


def test_a_captured_call_id_matches_exactly_even_with_a_skewed_clock():
    record = parse_record(sent_b(call_id='3f0c5a8e-1111-4000-8000-000000000001'))
    skewed = view('b', 'outbound', at(9), at(10), call_id='3f0c5a8e-1111-4000-8000-000000000001')
    assert not fits(skewed, record)
    matches, ambiguous = match_records([skewed], [skewed], [record])
    assert matches == {'b': [(record, 'call_id')]} and ambiguous == set()


def test_ambiguity_is_refused_in_both_directions():
    record = parse_record(sent_b())
    twin_one = view('x', 'outbound', at(3, 24), at(4, 10))
    twin_two = view('y', 'outbound', at(3, 40), at(4, 20))
    matches, ambiguous = match_records([twin_one], [twin_one, twin_two], [record])
    assert matches == {} and ambiguous == {'x'}  # the record fits two calls
    second = parse_record({**sent_b(), 'id': 'rec-b2', 'answered_at': iso(at(3, 40)), 'finished_at': iso(at(4, 20))})
    alone = view('z', 'outbound', at(3, 30), at(4, 15))
    matches, ambiguous = match_records([alone], [alone], [record, second])
    assert matches == {} and ambiguous == {'z'}  # the call fits two records
    other_way = view('in', 'inbound', at(3, 26), at(4, 11), remote=FAR)
    assert match_records([other_way], [other_way], [record]) == ({}, set())  # wrong direction: no match


def test_records_already_held_stay_with_their_call_and_holders_are_not_rematched():
    record = parse_record(sent_b(cost='0.004'))
    near = view('later', 'outbound', at(3, 26), at(4, 11))
    holder = view('held', 'outbound', at(3, 26), at(4, 11))
    matches, ambiguous = match_records([holder, near], [holder, near], [record], attached={'rec-b': 'held'},
                                       holding={'held'})
    assert matches == {'held': [(record, 'known')]} and ambiguous == set()


# The billing acceptance table against real stores --------------------------------------

def sip_card(direction='outbound', minute='0.005'):
    return RateCard(None, 'sip', direction, f'Telnyx {direction}', 'USD', parse_amount(minute), 0, 0, 60, 60,
                    'https://telnyx.com/pricing/elastic-sip', datetime(2026, 10, 3))


@pytest.fixture
def ledger(installation):
    configuration, delivery, snapshot = installation
    routes = RouteStore(configuration.engine, sip_preset=lambda: 'telnyx')
    routes.replace_cards([sip_card(), sip_card('inbound', '0.0032')])
    carriers = CarrierChargeStore(configuration.engine)
    return installation, routes, carriers


def outbound_attempt(ledger, job, *, phase, answer, end, start=None, call_id=None, sequence=1):
    installation, routes, carriers = ledger
    attempt = uuid4().hex
    start = start or answer - timedelta(seconds=5)
    with routes.engine.begin() as connection:
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=sequence, phase=phase,
                                                           created_at=start, submitted_at=start, completed_at=end))
        connection.execute(carriers.calls.insert().values(
            id=uuid4().hex, direction='outbound', call_id=attempt, job_id=job, attempt_id=attempt,
            trunk_preset='telnyx', did=OURS, caller=OURS, called=FAR, started_at=start, answered_at=answer,
            ended_at=end, disposition='answered', connected_seconds=int((end - answer).total_seconds()), t38='yes',
            pages=3 if phase == 'success' else 0, fax_status='SUCCESS' if phase == 'success' else 'FAILED',
            fax_preference=0, sip_call_id=call_id, created_at=start, updated_at=end))
    routes.capture(CaptureTarget(attempt, job, FAR, 'sip', None, phase, 3, start, end, False),
                   observed_seconds=int((end - answer).total_seconds()))
    return attempt


def inbound_call(ledger, *, answer, end, caller, inbound_id=None):
    _, routes, carriers = ledger
    identity = uuid4().hex
    with routes.engine.begin() as connection:
        connection.execute(carriers.calls.insert().values(
            id=identity, direction='inbound', call_id=f'1759.{identity[:6]}', job_id=inbound_id, attempt_id=None,
            trunk_preset='telnyx', did=OURS, caller=caller, called=OURS, started_at=answer, answered_at=answer,
            ended_at=end, disposition='answered', connected_seconds=int((end - answer).total_seconds()), t38='yes',
            pages=1, fax_status='SUCCESS', fax_preference=0, created_at=answer, updated_at=end))
    return identity


def delivery_state(installation, job):
    configuration, delivery, _ = installation
    return delivery.get(job), delivery.history(job)


def checks(carriers, call_id=None):
    with carriers.engine.connect() as connection:
        rows = connection.execute(sa.select(carriers.checks)).mappings().all()
    states = {row['id']: row['state'] for row in rows}
    return states if call_id is None else states.get(call_id)


def call_of(carriers, attempt):
    with carriers.engine.connect() as connection:
        return connection.execute(sa.select(carriers.calls.c.id).where(carriers.calls.c.attempt_id == attempt)).scalar_one()


def test_acceptance_table_for_a_sent_fax(ledger):
    installation, routes, carriers = ledger
    job = accept(installation)
    attempt = outbound_attempt(ledger, job, phase='success', answer=at(3, 28), end=at(4, 14))
    call = call_of(carriers, attempt)
    before = delivery_state(installation, job)
    spending = Spending(routes, carriers)
    source = FakeTelnyx([sent_b(cost=None)])
    reconciler = CarrierReconciler(carriers, routes, source)

    # Delivery succeeds with a null price: billing stays unknown, never the estimate.
    reconciler.step(now=NOW)
    assert checks(carriers, call) == 'waiting' and carriers.history(call) == []
    assert routes.decision(attempt)['reported_cost_micros'] is None
    assert spending.job(job)['summary'] == 'Cost not reported yet.'
    totals = spending.outbound(BASE - timedelta(days=1), now=NOW)[0]
    assert (totals['awaiting'], totals['reported'], totals['unreported_estimate_micros']) == (1, 0, {'USD': 5000})

    # The charge arrives later; nothing is asked again before the retry time.
    source.payloads = [sent_b()]
    reconciler.step(now=NOW + timedelta(minutes=1))
    assert source.reads == 1
    reconciler.step(now=NOW + timedelta(minutes=6))
    assert checks(carriers, call) == 'matched'
    row = routes.decision(attempt)
    assert (row['reported_cost_micros'], row['reported_currency'], row['settled_cost_micros']) == (5000, 'USD', None)
    assert spending.job(job)['summary'] == 'Telnyx charged $0.005 for this call.'
    totals = spending.outbound(BASE - timedelta(days=1), now=NOW)[0]
    assert (totals['awaiting'], totals['reported'], totals['billed_seconds']) == (0, 1, 60)
    assert totals['carriers'] == {'telnyx'}

    # The same report again has one effect.
    for minutes in (30, 60):
        CarrierReconciler(carriers, routes, source).run_now(now=NOW + timedelta(minutes=minutes))
    assert len(carriers.history(call)) == 1
    with routes.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(routes.charges)).scalar_one() == 1

    # A corrected amount supersedes the earlier one; both stay in history and are never summed.
    source.payloads = [sent_b(cost='0.004')]
    CarrierReconciler(carriers, routes, source).run_now(now=NOW + timedelta(hours=2))
    history = carriers.history(call)
    assert [(item['version'], item['amount_micros'], item['applied']) for item in history] == [(1, 5000, 1), (2, 4000, 1)]
    assert history[1]['supersedes_id'] == history[0]['id'] and history[1]['match_method'] == 'time_window'
    assert routes.decision(attempt)['reported_cost_micros'] == 4000

    # An older report arriving after the correction is kept, but never replaces the newer amount.
    older = parse_record(sent_b(cost='0.005'))
    assert carriers.record(call, provider_id='telnyx', record=older, method='time_window',
                           effective_at=NOW + timedelta(hours=1), final=False, now=NOW + timedelta(hours=3)) == 'older'
    assert carriers.record(call, provider_id='telnyx', record=older, method='time_window',
                           effective_at=NOW + timedelta(hours=1), final=False, now=NOW + timedelta(hours=3)) == 'duplicate'
    assert [charge['amount_micros'] for charge in carriers.in_effect([call])[call]] == [4000]
    assert carriers.history(call)[-1]['applied'] == 0
    CarrierReconciler(carriers, routes, FakeTelnyx([sent_b(cost='0.004')])).run_now(now=NOW + timedelta(hours=4))
    assert routes.decision(attempt)['reported_cost_micros'] == 4000

    # A day after the call ended the amount in effect is settled and no longer asked about.
    settle = FakeTelnyx([sent_b(cost='0.004')])
    CarrierReconciler(carriers, routes, settle).step(now=at(4, 14) + timedelta(hours=25))
    assert checks(carriers, call) == 'settled'
    row = routes.decision(attempt)
    assert (row['reported_cost_micros'], row['settled_cost_micros']) == (4000, 4000) and row['settled_at'] is not None
    CarrierReconciler(carriers, routes, settle).step(now=at(4, 14) + timedelta(hours=26))
    assert settle.reads == 1
    # Billing never touched the delivery.
    assert delivery_state(installation, job) == before


def test_a_charged_failed_attempt_counts_in_the_fax_total(ledger):
    installation, routes, carriers = ledger
    job = accept(installation)
    failed = outbound_attempt(ledger, job, phase='failed', answer=at(0, 29), end=at(1, 31))
    sent = outbound_attempt(ledger, job, phase='success', answer=at(3, 28), end=at(4, 14), sequence=2)
    before = delivery_state(installation, job)
    CarrierReconciler(carriers, routes, FakeTelnyx([sent_a(), sent_b()])).run_now(now=NOW)
    assert routes.decision(failed)['reported_cost_micros'] == 10_000
    assert routes.decision(sent)['reported_cost_micros'] == 5000
    cost = Spending(routes, carriers).job(job)
    assert cost['summary'] == 'Telnyx charged $0.015 for 2 calls.' and cost['reported_cost'] == {'USD': 15_000}
    assert delivery_state(installation, job) == before


def test_partial_reports_say_what_is_still_missing(ledger):
    installation, routes, carriers = ledger
    job = accept(installation)
    outbound_attempt(ledger, job, phase='failed', answer=at(0, 29), end=at(1, 31))
    outbound_attempt(ledger, job, phase='success', answer=at(3, 28), end=at(4, 14), sequence=2)
    CarrierReconciler(carriers, routes, FakeTelnyx([sent_a(), sent_b(cost=None)])).run_now(now=NOW)
    assert Spending(routes, carriers).job(job)['summary'] == (
        'Telnyx charged $0.01 so far; the cost of 1 more call is not reported yet.')


def test_an_ambiguous_record_stays_unmatched_and_is_counted(ledger):
    installation, routes, carriers = ledger
    first, second = accept(installation), accept(installation)
    one = outbound_attempt(ledger, first, phase='success', answer=at(3, 24), end=at(4, 10))
    two = outbound_attempt(ledger, second, phase='success', answer=at(3, 40), end=at(4, 20))
    result = CarrierReconciler(carriers, routes, FakeTelnyx([sent_b()])).run_now(now=NOW)
    assert (result.ambiguous, result.recorded) == (2, 0)
    for attempt in (one, two):
        assert checks(carriers, call_of(carriers, attempt)) == 'ambiguous'
        assert routes.decision(attempt)['reported_cost_micros'] is None
    with routes.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(carriers.charges)).scalar_one() == 0
    spending = Spending(routes, carriers)
    totals = spending.outbound(BASE - timedelta(days=1), now=NOW)[0]
    assert (totals['unmatched'], totals['awaiting']) == (2, 0)
    assert spending.job(first)['summary'] == 'Faxbot could not match this call to one Telnyx record, so its cost is unknown.'


def test_a_received_fax_gets_its_call_charge(ledger):
    installation, routes, carriers = ledger
    inbound_c = inbound_call(ledger, answer=at(14, 6), end=at(14, 31), caller='7205550111', inbound_id='inbound-c')
    inbound_call(ledger, answer=at(18, 56), end=at(19, 27), caller=CALLER_D, inbound_id='inbound-d')
    spending = Spending(routes, carriers)
    assert spending.inbound('inbound-c')['summary'] == 'Cost not reported yet.'
    result = CarrierReconciler(carriers, routes, FakeTelnyx([received_c(), received_d()])).run_now(now=NOW)
    assert (result.checked, result.matched, result.recorded) == (2, 2, 2)
    assert spending.inbound('inbound-c') == {'state': 'reported', 'reported_cost': {'USD': 3200},
                                             'summary': 'Telnyx charged $0.0032 for this call.'}
    received = spending.received(BASE - timedelta(days=1), now=NOW)
    assert len(received) == 1
    entry = received[0]
    assert (entry['calls'], entry['faxes'], entry['reported'], entry['billed_seconds']) == (2, 2, 2, 120)
    assert entry['reported_cost_micros'] == {'USD': 6400} and entry['carrier'] == 'telnyx'
    assert carriers.history(inbound_c)[0]['match_method'] == 'time_window'
    assert spending.inbound('not-a-trunk-fax') == {'state': 'none', 'summary': None, 'reported_cost': {}}


def test_carrier_failures_back_off_and_a_missing_key_does_nothing(ledger):
    installation, routes, carriers = ledger
    job = accept(installation)
    outbound_attempt(ledger, job, phase='success', answer=at(3, 28), end=at(4, 14))
    unset = FakeTelnyx([sent_b()], key='')
    CarrierReconciler(carriers, routes, unset).step(now=NOW)
    assert unset.reads == 0 and checks(carriers) == {}
    failing = FakeTelnyx([sent_b()])
    failing.failure = CarrierRateLimited('slow down')
    reconciler = CarrierReconciler(carriers, routes, failing)
    reconciler.step(now=NOW)
    reconciler.step(now=NOW + timedelta(minutes=2))
    assert failing.reads == 1  # paused for at least five minutes after a rate limit
    failing.failure = None
    reconciler.step(now=NOW + timedelta(minutes=6))
    assert failing.reads == 2 and list(checks(carriers).values()) == ['matched']


def test_calls_never_priced_within_a_week_stop_being_asked_about(ledger):
    installation, routes, carriers = ledger
    job = accept(installation)
    attempt = outbound_attempt(ledger, job, phase='success', answer=at(3, 28), end=at(4, 14))
    source = FakeTelnyx([])
    CarrierReconciler(carriers, routes, source).step(now=NOW)
    assert checks(carriers, call_of(carriers, attempt)) == 'waiting'
    CarrierReconciler(carriers, routes, source).step(now=NOW + timedelta(days=8))
    assert checks(carriers, call_of(carriers, attempt)) == 'unreported'
    totals = Spending(routes, carriers).outbound(BASE - timedelta(days=1), now=NOW + timedelta(days=8))[0]
    assert (totals['awaiting'], totals['unreported']) == (0, 1)


def test_flat_plan_cards_are_included_not_unknown(ledger):
    installation, routes, carriers = ledger
    plan = RateCard(None, 'humblefax', 'outbound', 'HumbleFax unlimited plan', 'USD', 0, 0, 0, 60, 0,
                    'https://humblefax.com/', datetime(2026, 10, 3), parse_amount('10.00', whole_digits=4))
    routes.replace_cards([plan])
    card = routes.card_for('humblefax')
    assert card.flat_plan and card.monthly_fee_micros == 10_000_000
    job = accept(installation)
    attempt = uuid4().hex
    with routes.engine.begin() as connection:
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='success',
                                                           created_at=at(1), submitted_at=at(1), completed_at=at(2)))
    routes.capture(CaptureTarget(attempt, job, FAR, 'humblefax', 'hf-1', 'success', 3, at(1), at(2), False))
    spending = Spending(routes, carriers)
    assert spending.job(job)['summary'] == 'Included in your HumbleFax plan ($10 a month).'
    totals = spending.outbound(NOW - timedelta(days=30), now=NOW)[0]
    assert totals['plan_card'].provider_id == 'humblefax' and totals['cost_micros'] == {'USD': 0}
    # The plan fee counts once for 30 days, pro-rated by day for other periods.
    assert (totals['plan_fee_micros'], totals['plan_days'], totals['total_micros']) == (10_000_000, 30, {'USD': 10_000_000})
    week = spending.outbound(NOW - timedelta(days=7), now=NOW)[0]
    assert week['plan_fee_micros'] == 2_333_334


def test_unrecorded_records_are_kept_attached_to_a_received_fax_and_counted_once(ledger):
    """R1: Telnyx billed a received call Faxbot has no call record of; its recovered fax gets the charge."""
    installation, routes, carriers = ledger
    received = inbound_call(ledger, answer=at(18, 56), end=at(19, 27), caller=CALLER_D, inbound_id=None)
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=routes.engine)
    imports = sa.Table('inbound_imports', sa.MetaData(), autoload_with=routes.engine)
    with routes.engine.begin() as connection:
        for identity, pages in (('fax-r1', 1), ('fax-other', 2)):
            connection.execute(faxes.insert().values(
                id=identity, from_number=CALLER_C, to_number=OURS, status='received', backend='sip', pages=pages,
                created_at=NOW, received_at=NOW, updated_at=NOW))
        # R1 was brought in later; the source's receipt time is when the call ended.
        connection.execute(imports.insert().values(
            id='import-1', source='sip', account='trunk', operation_id='1791083644.1', revision='1', state='received',
            attempts=1, imported_at=NOW, source_received_at=at(14, 26), acquired_at=NOW, artifact_digest='a' * 64,
            artifact_size=10, inbound_fax_id='fax-r1', created_at=NOW, updated_at=NOW))
    source = FakeTelnyx([received_c(), received_d(), sent_a(), telnyx(
        'rec-zero', 'inbound', at(20), at(20), at(21), '0', cli=CALLER_C, cld=OURS), telnyx(
        'rec-other-number', 'inbound', at(22), at(22), at(23), '0.0032', cli=CALLER_C, cld='+19995550000')])
    reconciler = CarrierReconciler(carriers, routes, source, numbers=lambda: (OURS,))
    result = reconciler.run_now(now=NOW)
    # rec-d matched its call; rec-c fits no call and is kept; rec-a (sent, Faxbot recorded nothing at all) too;
    # zero charges and other numbers are left out.
    assert result.unrecorded == 2
    kept = {row['record_id']: row for row in carriers.unrecorded_in_effect()}
    assert set(kept) == {'rec-c', 'rec-a'}
    assert kept['rec-c']['inbound_fax_id'] == 'fax-r1' and kept['rec-a']['inbound_fax_id'] is None
    spending = Spending(routes, carriers)
    assert spending.inbound('fax-r1')['summary'] == 'Telnyx charged $0.0032 for this call.'
    assert spending.inbound('fax-other')['summary'] == 'Cost not reported yet.'
    entry = spending.received(BASE - timedelta(days=1), now=NOW)[0]
    assert (entry['calls'], entry['reported'], entry['unrecorded'], entry['unrecorded_attached']) == (1, 1, 1, 1)
    assert entry['reported_cost_micros'] == {'USD': 6400} and entry['unrecorded_micros'] == {'USD': 3200}
    sent = spending.outbound(BASE - timedelta(days=1), now=NOW)[0]
    assert (sent['provider_id'], sent['unrecorded'], sent['reported_cost_micros']) == ('sip', 1, {'USD': 10_000})
    # Repeated sweeps have one effect; a later call record that holds the record removes it from the list.
    CarrierReconciler(carriers, routes, source, numbers=lambda: (OURS,)).run_now(now=NOW + timedelta(minutes=5))
    with routes.engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(carriers.records)).scalar_one() == 2
    job = accept(installation)
    outbound_attempt(ledger, job, phase='success', answer=at(0, 29), end=at(1, 31))
    CarrierReconciler(carriers, routes, source, numbers=lambda: (OURS,)).run_now(now=NOW + timedelta(minutes=10))
    assert {row['record_id'] for row in carriers.unrecorded_in_effect()} == {'rec-c'}
    assert spending.outbound(BASE - timedelta(days=1), now=NOW)[0]['reported_cost_micros'] == {'USD': 10_000}


def test_an_unrecorded_record_fitting_two_received_faxes_is_kept_but_attached_to_neither(ledger):
    installation, routes, carriers = ledger
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=routes.engine)
    with routes.engine.begin() as connection:
        for identity in ('fax-1', 'fax-2'):
            connection.execute(faxes.insert().values(
                id=identity, from_number=CALLER_C, to_number=OURS, status='received', backend='sip', pages=1,
                created_at=at(14, 30), received_at=at(14, 30), updated_at=at(14, 30)))
    CarrierReconciler(carriers, routes, FakeTelnyx([received_c()]), numbers=lambda: (OURS,)).run_now(now=NOW)
    [row] = carriers.unrecorded_in_effect()
    assert row['inbound_fax_id'] is None
    assert Spending(routes, carriers).inbound('fax-1')['summary'] == 'Cost not reported yet.'


def test_shipped_cards_are_added_for_providers_in_use_once_and_never_after_removal(ledger):
    from api.app.config_values import ConfigurationValues
    from api.app.routing.seed import cards_in_use, load_cards
    installation, routes, carriers = ledger
    values = ConfigurationValues.from_environment({
        'FAX_OUTBOUND_BACKEND': 'humblefax', 'FAX_INBOUND_BACKEND': 'sip', 'FAX_OUTBOUND_ROUTES': 'sip, phaxio',
        'SIP_TRUNK_PRESET': 'telnyx'})
    chosen = {(card.provider_id, card.direction) for card in cards_in_use(values, load_cards())}
    assert chosen == {('humblefax', 'outbound'), ('phaxio', 'outbound'), ('sip-telnyx', 'outbound'),
                      ('sip-telnyx', 'inbound')}
    added = routes.add_missing_cards(cards_in_use(values, load_cards()))
    # The ledger already had 'sip' cards for both directions, not 'sip-telnyx' ones.
    assert {(card.provider_id, card.direction) for card in added} == chosen
    plan = routes.card_for('humblefax')
    assert plan.flat_plan and plan.source_url == 'https://humblefax.com/faq'
    assert routes.add_missing_cards(cards_in_use(values, load_cards())) == []
    routes.replace_cards([card for card in routes.current_cards() if card.provider_id != 'humblefax'])
    assert routes.add_missing_cards(cards_in_use(values, load_cards())) == []  # a removal is remembered
    assert routes.card_for('humblefax') is None
