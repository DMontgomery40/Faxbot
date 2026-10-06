"""Receiving-side savings: the shared-channel replay, split-window advice, quiet numbers and connections.

Synthetic traffic only. Twenty numbers on one Telnyx trunk: eight busy local
numbers on a fixed schedule with Monday afternoon bursts, ten quiet local
numbers, and a heavily used toll-free and Canadian number that must stay
billed by the minute. Expected money is worked out here from the calls
themselves, never read back from the code under test.
"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from api.app.routing.carriers import CarrierChargeStore
from api.app.routing.costs import RateCard, format_amount, parse_amount
from api.app.routing.receiving import (Call, ReceivingHistory, about, carrier_prices, channel_fee, choose_pool,
                                       number_kind, prorate, receiving_report, replay)
from api.app.routing.seed import load_cards
from api.app.routing.store import RouteStore
from api.app.routing.telnyx import CarrierRecord
from api.app.schema import upgrade_schema
from api.tests.test_routing_http import ADMIN, scoped_key, telnyx_client  # noqa: F401  (a Telnyx trunk over HTTPS)
from api.tests.test_schema import database  # noqa: F401  (fixture: SQLite and PostgreSQL)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 30)
DAYS = 30
CHOOSE_START, CHECK_START = NOW - timedelta(days=2 * DAYS), NOW - timedelta(days=DAYS)
BUSY = [f'+1303555{index:04d}' for index in range(100, 108)]
QUIET = [f'+1720555{index:04d}' for index in range(200, 210)]
TOLL_FREE, CANADA = '+18005550199', '+15145550123'
CALLER = '+12025550123'
LOCAL_MINUTE, TOLL_FREE_MINUTE, CHANNEL = 3_200, 15_000, 12_000_000
TIERS = ((10, 12_000_000), (40, 11_000_000), (200, 9_000_000), (None, 8_000_000))


def at(start, seconds):
    return start + timedelta(seconds=seconds)


def call(identity, number, start, seconds, metered=None):
    return Call(identity, number, start, at(start, seconds), metered)


# The replay -------------------------------------------------------------------------------

def test_a_channel_freed_at_a_calls_end_is_free_for_a_call_arriving_at_that_same_instant():
    first = call('a', BUSY[0], NOW, 60)
    exactly = call('b', BUSY[1], NOW + timedelta(seconds=60), 60)
    result = replay([exactly, first], channels=1)
    assert result.turned_away == [] and result.peak == 1 and result.calls == 2
    early = call('c', BUSY[1], NOW + timedelta(seconds=59), 60)
    result = replay([first, early], channels=1)
    assert [item.id for item in result.turned_away] == ['c'] and result.peak == 1
    assert result.busy == [{'start': NOW, 'end': NOW + timedelta(seconds=60), 'turned_away': 1,
                            'numbers': [BUSY[1]]}]
    # Unlimited channels measure the peak: back to back is one channel, a one-second overlap two.
    assert replay([first, exactly]).peak == 1 and replay([first, early]).peak == 2


def test_calls_arriving_together_take_the_last_channel_in_a_fixed_order():
    """Earlier end first, then id: the same history always turns away the same call."""
    long, short, other = call('z', BUSY[0], NOW, 300), call('y', BUSY[1], NOW, 120), call('x', BUSY[2], NOW, 120)
    for order in ([long, short, other], [other, long, short], [short, other, long]):
        result = replay(order, channels=2)
        assert [item.id for item in result.turned_away] == ['z']
        assert result.busy == [{'start': NOW, 'end': at(NOW, 120), 'turned_away': 1, 'numbers': [BUSY[0]]}]


def test_busy_stretches_cover_only_the_time_every_channel_was_in_use():
    calls = [call('a', BUSY[0], NOW, 600), call('b', BUSY[1], at(NOW, 60), 600),
             call('c', BUSY[2], at(NOW, 120), 60), call('d', BUSY[3], at(NOW, 700), 60),
             call('e', BUSY[4], at(NOW, 3600), 60), call('f', BUSY[5], at(NOW, 3600), 60)]
    result = replay(calls, channels=2)
    assert [item.id for item in result.turned_away] == ['c']
    assert result.busy == [
        {'start': at(NOW, 60), 'end': at(NOW, 600), 'turned_away': 1, 'numbers': [BUSY[2]]},
        {'start': at(NOW, 3600), 'end': at(NOW, 3660), 'turned_away': 0, 'numbers': []}]


def test_channel_prices_follow_the_published_tiers_and_prorate_by_day():
    assert channel_fee(TIERS, 0) == 0 and channel_fee(TIERS, 1) == 12_000_000
    assert channel_fee(TIERS, 10) == 120_000_000 and channel_fee(TIERS, 11) == 131_000_000
    assert channel_fee(TIERS, 50) == 560_000_000 and channel_fee(TIERS, 251) == 2_368_000_000
    assert prorate(12_000_000, 30 * 86_400) == 12_000_000 and prorate(12_000_000, 15 * 86_400) == 6_000_000
    assert prorate(1_000_000, 7 * 86_400) == 233_334  # rounded up, never down


def test_numbers_join_busiest_first_and_no_pool_wins_a_tie():
    window = 30 * 86_400
    # One number spending exactly a channel's price: no pool (a tie keeps billing by the minute).
    even = {BUSY[0]: [call('a', BUSY[0], NOW, 60, CHANNEL)]}
    assert choose_pool(even, [BUSY[0]], TIERS, window) == ((), 0, CHANNEL)
    # Two busy numbers that never overlap share one channel; the cheap third would need a second.
    calls = {BUSY[0]: [call('a', BUSY[0], NOW, 600, 10_000_000)],
             BUSY[1]: [call('b', BUSY[1], at(NOW, 600), 600, 9_000_000)],
             BUSY[2]: [call('c', BUSY[2], at(NOW, 300), 60, 1_000_000)]}
    assert choose_pool(calls, list(calls), TIERS, window) == ((BUSY[0], BUSY[1]), 1, 13_000_000)


def test_toll_free_international_and_unknown_numbers_are_told_apart():
    assert number_kind(BUSY[0], 'US') == 'local' and number_kind(TOLL_FREE, 'US') == 'toll_free'
    assert number_kind(CANADA, 'US') == 'international' and number_kind('+442079460000', 'US') == 'international'
    assert number_kind('+19995550100', 'US') == 'other'


def test_estimates_in_sentences_are_to_the_cent_once_they_reach_one():
    assert about(115_212_800, 'USD') == '$115.21' and about(12_000_000, 'USD') == '$12.00'
    assert about(3_200, 'USD') == '$0.0032' and about(5_000, 'EUR') == '0.005 EUR'
    assert about(9_995_000, 'USD') == '$10.00'


# The shipped prices -----------------------------------------------------------------------

def test_receiving_prices_are_recorded_with_their_source_and_read_date_and_never_become_rate_cards():
    document = json.loads((ROOT / 'config' / 'rate_cards.json').read_text())
    entries = document['receiving_prices']
    assert {(entry['kind'], entry.get('number_type')) for entry in entries} == {
        ('inbound_channel', None), ('inbound_per_minute', 'local'), ('inbound_per_minute', 'toll_free'),
        ('number_rental', 'local'), ('number_rental', 'toll_free'), ('trunk', None)}
    for entry in entries:
        assert entry['carrier'] == 'telnyx' and entry['country'] == 'US' and entry['currency'] == 'USD'
        assert entry['source_url'].startswith('https://') and entry['source_url'] in entry['sources']
        assert date.fromisoformat(entry['advertised_on']) == date(2026, 10, 5) and entry['notes'].endswith('.')
    labels = {entry['label'] for entry in entries}
    assert not labels & {card.label for card in load_cards()}
    prices = carrier_prices('telnyx')
    assert prices.tiers == TIERS and prices.currency == 'USD' and prices.country == 'US'
    assert prices.per_minute['local'].per_minute_micros == LOCAL_MINUTE
    assert prices.per_minute['toll_free'].per_minute_micros == TOLL_FREE_MINUTE
    assert prices.per_minute['local'].billing_increment_seconds == 60
    assert prices.rental == {'local': 1_000_000, 'toll_free': 1_000_000} and prices.trunk_fee == 0
    assert {item['read_on'] for item in prices.sources} == {'2026-10-05'}
    assert prices.sources[0]['text'] == ('$12.00 a month each for the first 10, $11.00 for the next 40, $9.00 for the '
                                         'next 200 and $8.00 after 250')
    # Another carrier's number rental comes from its shipped trunk card; it publishes no channel price.
    signalwire = carrier_prices('signalwire')
    assert signalwire.rental == {'local': 500_000} and not signalwire.channels_priced


# Synthetic history in the database ------------------------------------------------------------

def busy_calls(window_start, *, extra_burst=None):
    """Eight busy numbers: fifteen 10-minute calls a day, five minutes apart, plus Monday 13:00 bursts.

    Number i calls at 08:00 + 40 j + 5 i minutes, so at most two overlap (a call
    ending at 08:10 frees its channel for the one arriving at 08:10). Each Monday
    at 13:00 three 4-minute calls reach numbers 0-2 while numbers 3 and 4 are
    on a call: five channels at once.
    """
    calls = []
    for day in range(DAYS):
        midnight = window_start + timedelta(days=day)
        for index, number in enumerate(BUSY):
            for slot in range(15):
                calls.append((number, midnight + timedelta(hours=8, minutes=40 * slot + 5 * index), 600))
        if midnight.weekday() == 0:
            calls += [(BUSY[index], midnight + timedelta(hours=13), 240) for index in range(3)]
    if extra_burst is not None:
        calls += [(BUSY[index], extra_burst, 240) for index in range(4)]
    return calls


def heavy_calls(window_start):
    """Twenty 10-minute calls a night to the toll-free and the Canadian number: busy, but never eligible."""
    return [(number, window_start + timedelta(days=day, minutes=20 * slot), 600)
            for day in range(DAYS) for slot in range(20) for number in (TOLL_FREE, CANADA)]


def quiet_calls():
    """Received one-minute calls: QUIET 4-6 once, QUIET 7 once (and one sent), QUIET 8 three times in the
    later window; QUIET 9 five times in the earlier window only, each during a Monday burst."""
    received = [(QUIET[index], CHECK_START + timedelta(days=index, hours=2), 60) for index in (4, 5, 6, 7)]
    received += [(QUIET[8], CHECK_START + timedelta(days=day, hours=3), 60) for day in (1, 2, 3)]
    mondays = [CHOOSE_START + timedelta(days=day) for day in range(DAYS)
               if (CHOOSE_START + timedelta(days=day)).weekday() == 0]
    received += [(QUIET[9], monday + timedelta(hours=13, minutes=1), 60) for monday in mondays]
    return received


def peak(calls):
    """Most calls in progress at once, by brute force over half-open [start, end) intervals."""
    return max(sum(1 for _, other, seconds in calls if other <= start < other + timedelta(seconds=seconds))
               for _, start, _ in calls)


@pytest.fixture
def history(database):
    upgrade_schema(database)
    routes = RouteStore(database, sip_preset=lambda: 'telnyx')
    routes.replace_cards([RateCard(None, 'sip', 'inbound', 'Telnyx inbound', 'USD', parse_amount('0.0032'), 0, 0, 60,
                                   60, 'https://telnyx.com/pricing/elastic-sip', datetime(2026, 10, 5))])
    return database, routes, CarrierChargeStore(database)


def insert_calls(store, received, *, direction='inbound', preset='telnyx', no_plus=False):
    rows = []
    for number, start, seconds in received:
        identity = uuid4().hex
        ours = number[1:] if no_plus else number
        rows.append(dict(id=identity, direction=direction, call_id=f'call.{identity}', job_id=None, attempt_id=None,
                         trunk_preset=preset, did=ours, caller=CALLER if direction == 'inbound' else ours,
                         called=ours if direction == 'inbound' else CALLER, started_at=start, answered_at=start,
                         ended_at=start + timedelta(seconds=seconds), disposition='answered',
                         connected_seconds=seconds, t38='yes', pages=1, fax_status='SUCCESS', fax_preference=0,
                         created_at=start, updated_at=start))
    with store.engine.begin() as connection:
        connection.execute(store.calls.insert(), rows)
    return [row['id'] for row in rows]


def values(**changes):
    base = dict(sip_trunk_preset='telnyx', fax_default_country='US', sip_trunk_did_list=tuple(BUSY + QUIET),
                sip_trunk_caller_id=BUSY[0], effective_outbound='sip', outbound_route_providers=(),
                effective_inbound='sip', inbound_enabled=True)
    return SimpleNamespace(**{**base, **changes})


def minutes(seconds):
    return -(-seconds // 60)


def metered(calls):
    """What calls cost billed by the minute: whole minutes at the local or toll-free published rate."""
    return sum(minutes(seconds) * (TOLL_FREE_MINUTE if number == TOLL_FREE else LOCAL_MINUTE)
               for number, _, seconds in calls)


def money(micros):
    return [{'currency': 'USD', 'amount': format_amount(micros)}]


def seed(store, *, extra_burst=None):
    earlier = busy_calls(CHOOSE_START) + heavy_calls(CHOOSE_START)
    later = busy_calls(CHECK_START, extra_burst=extra_burst) + heavy_calls(CHECK_START)
    insert_calls(store, earlier + later + quiet_calls())
    insert_calls(store, [(QUIET[7], CHECK_START + timedelta(days=9, hours=2), 60)], direction='outbound')
    return earlier, later


def test_busy_local_numbers_share_channels_chosen_on_one_month_and_checked_on_the_next(history):
    engine, routes, store = history
    earlier, later = seed(store)
    busy_earlier = [item for item in earlier if item[0] in BUSY]
    assert peak(busy_earlier) == peak([item for item in later if item[0] in BUSY]) == 5
    report = receiving_report(engine, routes, values(), now=NOW, days=DAYS)
    pool = report['pool']
    assert report['estimate'] is True and report['carrier'] == 'Telnyx' and report['history']['enough'] is True
    assert pool['state'] == 'share' and sorted(pool['pool_numbers']) == BUSY and pool['channels'] == 5
    assert (pool['turned_away'], pool['peak'], pool['needed'], pool['busy_windows']) == (0, 5, 5, [])
    assert pool['choose']['turned_away'] == 0 and pool['choose']['peak'] == 5
    # The later month's money, worked out from its calls: 18 local numbers and the toll-free one at $1 a month;
    # the Canadian number has no published rental here.
    quiet_later = [item for item in quiet_calls() if item[1] >= CHECK_START]
    today = metered(later) + metered(quiet_later)
    still = today - metered([item for item in later if item[0] in BUSY])
    rental = 19 * 1_000_000
    check = pool['check']
    assert check['billed_by_the_minute'] == money(today) and check['channels'] == money(5 * CHANNEL)
    assert check['still_billed_by_the_minute'] == money(still) and check['number_rental'] == money(rental)
    assert check['total_today'] == money(today + rental)
    assert check['total_with_pool'] == money(5 * CHANNEL + still + rental)
    assert pool['numbers_without_rental_price'] == 1
    assert report['sentence'] == pool['sentence'] == (
        f'Put 8 of your Telnyx numbers on 5 shared lines: in the last 30 days that would have cost about '
        f'{about(5 * CHANNEL + still + rental, "USD")} instead of {about(today + rental, "USD")}, and no caller '
        'would have heard busy (estimate).')
    rows = {row['number']: row for row in pool['numbers']}
    assert len(rows) == 20 and rows[BUSY[0]]['in_pool'] and rows[BUSY[0]]['eligible']
    assert rows[QUIET[9]]['in_pool'] is False  # its Monday calls would need a sixth channel
    assert rows[TOLL_FREE] == {
        'number': TOLL_FREE, 'kind': 'toll_free', 'eligible': False, 'in_pool': False,
        'reason': 'Toll-free numbers stay billed by the minute.', 'calls_before': 600, 'calls': 600,
        'billed_by_the_minute': money(600 * 10 * TOLL_FREE_MINUTE)}
    assert rows[CANADA]['kind'] == 'international' and rows[CANADA]['in_pool'] is False
    assert rows[CANADA]['reason'] == 'Numbers outside the US stay billed by the minute, because the shared-line price is for the US.'
    assert pool['break_even'] == ('One shared line at $12.00 a month costs as much as 3,750 received minutes at $0.0032 a '
                                  'minute.')
    assert 'A caller who would have heard a busy signal is counted once, not as calling back.' in pool['assumptions']
    assert ('Numbers outside the US, Canadian numbers included, stay billed by the minute, because the shared-line '
            'price is for the US.') in pool['assumptions']
    assert pool['assumptions'][0] == ('Telnyx calls a shared line an inbound channel: it takes one call at a time, '
                                      'with no charge per minute.')
    assert 'Faxbot only recommends; it never changes your Telnyx account.' in pool['assumptions']


def test_a_pool_chosen_on_the_earlier_month_is_not_advised_when_the_later_month_would_have_turned_callers_away(history):
    engine, routes, store = history
    tuesday = next(CHECK_START + timedelta(days=day, hours=13) for day in range(DAYS)
                   if (CHECK_START + timedelta(days=day)).weekday() == 1)
    seed(store, extra_burst=tuesday)
    pool = receiving_report(engine, routes, values(), now=NOW, days=DAYS)['pool']
    # Four burst calls arrive at 13:00 with number 3 on a call (number 2's call ends at that instant and frees
    # its channel); they take four channels and number 4's own 13:00 call, ending later, finds none free.
    assert pool['state'] == 'turned_away' and pool['channels'] == 5
    assert (pool['turned_away'], pool['needed']) == (1, 6)
    assert pool['busy_windows'] == [{'start': tuesday, 'end': tuesday + timedelta(minutes=4), 'turned_away': 1,
                                     'numbers': [BUSY[4]]}]
    assert pool['sentence'] == ("Don't switch yet: with 5 shared lines for 8 of your Telnyx numbers, 1 caller would "
                                'have heard a busy signal in the last 30 days (estimate).')


def test_light_traffic_keeps_every_number_billed_by_the_minute(history):
    engine, routes, store = history
    light = [(BUSY[day % 8], CHOOSE_START + timedelta(days=day, hours=9), 120) for day in range(2 * DAYS)]
    insert_calls(store, light)
    pool = receiving_report(engine, routes, values(), now=NOW, days=DAYS)['pool']
    later = [item for item in light if item[1] >= CHECK_START]
    assert pool['state'] == 'keep_metered' and pool['pool_numbers'] == [] and pool['channels'] == 0
    assert pool['sentence'] == (f'Keep your Telnyx numbers billed by the minute: in the last 30 days their received '
                                f'calls cost about {about(metered(later), "USD")}, and one shared line costs '
                                '$12.00 a month (estimate).')
    assert pool['note'] is None


def test_quiet_numbers_list_their_monthly_rental_and_total(history):
    engine, routes, store = history
    seed(store)
    quiet = receiving_report(engine, routes, values(), now=NOW, days=DAYS)['quiet_numbers']
    expected = [QUIET[index] for index in (0, 1, 2, 3, 4, 5, 6, 7, 9)]
    assert [row['number'] for row in quiet['numbers']] == expected
    rows = {row['number']: row for row in quiet['numbers']}
    assert (rows[QUIET[7]]['received'], rows[QUIET[7]]['sent']) == (1, 1)
    assert (rows[QUIET[9]]['received'], rows[QUIET[0]]['received']) == (0, 0)
    assert all(row['monthly_rental'] == money(1_000_000) for row in quiet['numbers'])
    assert quiet['state'] == 'quiet' and quiet['monthly_total'] == money(9_000_000)
    assert quiet['sentence'] == ('9 of your Telnyx numbers had 2 calls or fewer in the last 30 days; together they '
                                 'cost about $9.00 a month to keep (estimate). Check that no one still faxes a number '
                                 'before you give it up.')


def test_too_little_history_is_one_sentence_for_the_pool_and_for_quiet_numbers(history):
    engine, routes, store = history
    insert_calls(store, [(BUSY[0], NOW - timedelta(days=40), 60)])
    report = receiving_report(engine, routes, values(), now=NOW, days=DAYS)
    assert report['history'] == {'enough': False, 'first_call_at': NOW - timedelta(days=40), 'days': 40}
    assert report['pool'] == {'state': 'too_little_history', 'numbers': [], 'sentence': (
        'Faxbot needs 60 days of call history to advise on shared lines; it has 40 days so far.')}
    assert report['quiet_numbers']['state'] == 'quiet'  # 40 days cover the later month
    empty = receiving_report(engine, routes, values(), now=NOW - timedelta(days=45), days=DAYS)
    assert empty['pool']['sentence'] == ('Faxbot needs 60 days of call history to advise on shared lines; it has '
                                         'none yet.')
    assert empty['quiet_numbers']['sentence'] == ('Faxbot needs 30 days of call history to tell which numbers are '
                                                  'quiet; it has none yet.')


def test_reported_charges_count_once_and_numbers_match_however_they_were_stored(history):
    engine, routes, store = history
    start = CHECK_START + timedelta(days=3, hours=10)
    reported, estimated = insert_calls(store, [(QUIET[0], start, 90), (QUIET[0], start + timedelta(hours=1), 90)],
                                       no_plus=True)
    insert_calls(store, [(QUIET[1], start, 60)], preset='signalwire')

    def priced(record_id, amount, when):
        return CarrierRecord(record_id, 'inbound', None, CALLER, QUIET[0], start, start, start + timedelta(seconds=90),
                             90, 120, amount, format_amount(amount), 'USD')
    store.record(reported, provider_id='telnyx', record=priced('rec-1', 6_400, start), method='call_id',
                 effective_at=start + timedelta(hours=2), final=False)
    store.record(reported, provider_id='telnyx', record=priced('rec-1', 9_600, start), method='call_id',
                 effective_at=start + timedelta(hours=3), final=True)
    kept = CarrierRecord('rec-2', 'inbound', None, CALLER, QUIET[0][1:], start + timedelta(hours=2),
                         start + timedelta(hours=2), start + timedelta(hours=2, seconds=50), 50, 60, 3_200, '0.0032',
                         'USD')
    store.record_unrecorded(kept, provider_id='telnyx', inbound_fax_id=None, effective_at=start)
    store.record_unrecorded(kept, provider_id='telnyx', inbound_fax_id=None, effective_at=start)  # reported twice
    found = ReceivingHistory(engine, routes).read('telnyx', CHOOSE_START, NOW, country='US',
                                                  prices=carrier_prices('telnyx'))
    costs = sorted(item.metered_micros for item, _ in found.inbound)
    # The corrected charge in effect (not both reports), the estimate (2 minutes) and the kept record once.
    assert costs == [3_200, 6_400, 9_600] and {item.number for item, _ in found.inbound} == {QUIET[0]}
    assert found.other_carrier == 1 and found.no_end == 0 and found.unpriced == 0
    assert QUIET[0] in found.numbers_seen and QUIET[0][1:] not in found.numbers_seen
    assert estimated in {item.id for item, _ in found.inbound}


def test_connections_are_arithmetic_on_monthly_fees(history):
    engine, routes, store = history
    report = receiving_report(engine, routes, values(), now=NOW, days=DAYS)
    assert report['connections'] == {
        'sentence': 'Telnyx is your only fax service, so there is no second monthly fee to save.',
        'items': [{'name': 'Telnyx', 'kind': 'trunk', 'monthly_fee': money(0)}]}
    routes.replace_cards([*routes.current_cards(),
                          RateCard(None, 'humblefax', 'outbound', 'HumbleFax plan', 'USD', 0, 0, 0, 60, 0, None,
                                   datetime(2026, 10, 3), 10_000_000)])
    insert_calls(store, [(BUSY[0], NOW - timedelta(days=3), 60)], preset='signalwire')
    both = receiving_report(engine, routes, values(outbound_route_providers=('humblefax', 'phaxio')), now=NOW,
                            days=DAYS)['connections']
    assert [(item['name'], item['monthly_fee']) for item in both['items']] == [
        ('Telnyx', money(0)), ('HumbleFax', money(10_000_000)), ('Phaxio', []), ('SignalWire', [])]
    assert both['sentence'] == ('Your 4 fax services cost $10.00 a month in fixed fees (estimate). Keeping only '
                                'Telnyx would cost $0.00 a month, $10.00 less, if it can carry all your numbers and '
                                'calls; keep a second service if you need a backup. To compare Phaxio and SignalWire, '
                                'enter their prices in Costs → Prices & plans.')
    unpriced = receiving_report(engine, routes, values(outbound_route_providers=('phaxio',)), now=NOW, days=DAYS)
    assert unpriced['connections']['sentence'] == (
        'Telnyx has no monthly fee. To compare Phaxio and SignalWire, enter their prices in Costs → Prices & plans.')


def test_no_trunk_or_a_carrier_without_a_channel_price_says_so_in_one_sentence(history):
    engine, routes, store = history
    none = receiving_report(engine, routes, values(sip_trunk_preset=''), now=NOW, days=DAYS)
    assert none['pool']['sentence'] == ('Faxbot has no phone line from a carrier set up, so there are no received '
                                        'calls to compare.')
    other = receiving_report(engine, routes, values(sip_trunk_preset='signalwire'), now=NOW, days=DAYS)
    assert other['pool'] == {'state': 'no_channel_price', 'numbers': [], 'sentence': (
        'Shared lines are a Telnyx option, so Faxbot suggests them only for your Telnyx numbers.')}


# Over HTTPS -------------------------------------------------------------------------------------

def test_the_receiving_route_needs_settings_read_and_answers_in_sentences(telnyx_client):  # noqa: F811
    route = '/routing/recommendations/receiving'
    response = telnyx_client.get(route, headers=ADMIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['estimate'] is True and body['days'] == 30 and body['carrier'] == 'Telnyx'
    assert body['pool']['state'] == 'too_little_history' and body['quiet_numbers']['state'] == 'too_little_history'
    assert body['sentence'] == 'Faxbot needs 60 days of call history to advise on shared lines; it has none yet.'
    assert body['connections']['sentence'] == 'Telnyx is your only fax service, so there is no second monthly fee to save.'
    assert {item['read_on'] for item in body['prices']} == {'2026-10-05'}
    assert {item['source_url'] for item in body['prices']} == {'https://telnyx.com/pricing/elastic-sip',
                                                               'https://telnyx.com/pricing/numbers'}
    week = telnyx_client.get(route, headers=ADMIN, params={'days': 7}).json()
    assert week['days'] == 7 and week['windows']['check']['days'] == 7
    assert week['sentence'].startswith('Faxbot needs 14 days of call history')
    assert telnyx_client.get(route, headers=ADMIN, params={'days': 3}).status_code == 422
    assert telnyx_client.get(route, headers=scoped_key(telnyx_client, ['fax:send'])).status_code == 403
