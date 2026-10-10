"""Sending time priced in money (brief 84, JO M3): carrier prices by time of day, and a later hour waited for only
when the call costs less then. Synthetic prices and numbers; the Andrews & Arnold bands are the shipped data
(read 10 October 2026) and apply only to a card named sip-aaisp in GBP. SQLite and PostgreSQL where a database is
used."""
from datetime import datetime, timedelta
import itertools
import random
from types import SimpleNamespace

import pytest

from api.app.routing import schedule
from api.app.routing.costs import InvalidRateCard, RateCard, RateTerms, TimeBand
from api.app.routing.destinations import LOCAL, DestinationClass
from api.app.routing.predict import Link, RouteFacts, Shape, predict_from
from api.tests.test_schema import database  # noqa: F401 (fixture)
from api.tests.test_batching import sip  # noqa: F401 (fixture)

UK = '+441132000099'
# Monday 12 October 2026, 10:00 in London (BST, UTC+1): peak.
MONDAY_PEAK = datetime(2026, 10, 12, 9, 0)
WEEKDAYS = (0, 1, 2, 3, 4)
BANDS = (TimeBand(WEEKDAYS, 8 * 60, 18 * 60, 12_500, 'peak'), TimeBand(WEEKDAYS, 0, 8 * 60, 10_417, 'off-peak'),
         TimeBand(WEEKDAYS, 18 * 60, 1440, 10_417, 'off-peak'), TimeBand((5, 6), 0, 1440, 8_333, 'weekend'))


def card(per_minute=12_500, currency='GBP', identity='sip-aaisp'):
    return RateCard(None, identity, 'outbound', 'Synthetic banded trunk', currency, per_minute, 0, 0, 60, 60, None,
                    datetime(2026, 10, 10))


def facts(at, bands=BANDS):
    terms = RateTerms(card(), time_bands=bands, time_zone='Europe/London', bands_version='synthetic')
    return RouteFacts('sip', 'Andrews & Arnold', DestinationClass(LOCAL, 'GB', '+44', UK), terms, Link(), at=at)


def test_a_carriers_time_of_day_price_is_read_at_the_moment_the_call_starts():
    shape = Shape(2, None, 'standard', 'normal')
    peak = predict_from(facts(MONDAY_PEAK), shape)
    evening = predict_from(facts(MONDAY_PEAK + timedelta(hours=9)), shape)  # 19:00 BST
    weekend = predict_from(facts(datetime(2026, 10, 17, 9, 0)), shape)    # Saturday
    assert (peak.cost.micros, evening.cost.micros, weekend.cost.micros) == (12_500, 10_417, 8_333)
    assert "Andrews & Arnold's peak rate" in peak.basis and "off-peak rate" in evening.basis
    # No moment, or no band covering it: the card's own per-minute price.
    assert predict_from(facts(None), shape).cost.micros == 12_500
    with pytest.raises(InvalidRateCard):
        RateTerms(card(), time_bands=BANDS, time_zone='Not/AZone')
    with pytest.raises(InvalidRateCard):
        TimeBand((7,), 0, 60, 1)


def test_the_shipped_bands_apply_only_to_their_route_country_and_currency():
    from api.app.routing.predict_facts import banded, shipped
    data = shipped()
    where = DestinationClass(LOCAL, 'GB', '+44', UK)
    gbp = banded(RateTerms(card()), 'sip-aaisp', where, data)
    assert gbp.time_zone == 'Europe/London' and [band.label for band in gbp.time_bands] == [
        'peak', 'off-peak', 'off-peak', 'weekend'] and gbp.bands_version == 'sip-aaisp read 2026-10-10'
    assert banded(RateTerms(card(currency='USD')), 'sip-aaisp', where, data).time_bands == ()
    assert banded(RateTerms(card(identity='sip-telnyx')), 'sip-telnyx', where, data).time_bands == ()
    us = DestinationClass(LOCAL, 'US', '+1', '+12025550123')
    assert banded(RateTerms(card()), 'sip-aaisp', us, data).time_bands == ()


def hours():
    return schedule.Settings()


def fax(**changes):
    return schedule.Fax(UK, pages=2, route='sip', preset='aaisp', **changes)


class Priced:
    """A price for a call starting at each moment, from the synthetic banded facts; counts what it priced."""

    def __init__(self, bands=BANDS):
        self.bands, self.asked = bands, []
        self.by_hour = True

    def __call__(self, moment):
        self.asked.append(moment)
        found = predict_from(facts(moment, self.bands), Shape(2, None, 'standard', 'normal'))
        return (found.cost.micros, found.cost.currency)


def test_an_ordinary_fax_waits_for_the_off_peak_hour_that_costs_less():
    priced = Priced()
    decision = schedule.decide(fax(), hours(), None, MONDAY_PEAK, price_at=priced)
    # 18:00 BST is 17:00 UTC: the first off-peak hour, within the 12 hours an ordinary fax may wait.
    assert (decision.hold_until, decision.why) == (datetime(2026, 10, 12, 17, 0), 'cheaper')
    assert (decision.better.now_micros, decision.better.then_micros, decision.better.currency) == (
        12_500, 10_417, 'GBP')
    sentence = schedule.reason(decision, hours(), now=MONDAY_PEAK)
    assert sentence.startswith('Waiting until ') and 'its call costs about 0.010417 GBP from ' in sentence
    assert sentence.endswith('instead of 0.0125 GBP now.')


@pytest.mark.parametrize('change', ['flat', 'urgent', 'deadline', 'closed'])
def test_waiting_that_saves_nothing_or_is_not_allowed_is_rejected(change):
    bands = BANDS
    if change == 'flat':
        bands = (TimeBand(tuple(range(7)), 0, 1440, 12_500, 'any time'),)
    priced = Priced(bands)
    kwargs = {}
    if change == 'urgent':
        kwargs['urgent'] = True
    if change == 'deadline':
        kwargs['send_by'] = MONDAY_PEAK + timedelta(hours=3)
    settings = hours()
    if change == 'closed':
        # The recipient takes faxes only until 17:00 their time: the off-peak hours are closed.
        settings = schedule.Settings(hours=schedule.Hours(days=frozenset(WEEKDAYS), start=8 * 60, end=17 * 60,
                                                          zone_name='Europe/London'),
                                     zone_name='Europe/London', zone_set=True)
    decision = schedule.decide(fax(**kwargs), settings, None, MONDAY_PEAK, price_at=priced)
    assert decision.why != 'cheaper' and decision.better is None


def test_a_faster_hour_that_bills_the_same_saves_nothing_and_the_fax_goes_now(monkeypatch):
    """The older rule waited for an hour 25% faster; now it must also cost less. A 2-page call billed in whole
    minutes takes one minute at either hour, so it goes now; with a price per second it saves and still waits."""
    later = MONDAY_PEAK + timedelta(hours=2)
    better = schedule.BetterHour(later, 'faster', None, None, 40)
    monkeypatch.setattr(schedule, 'better_hour', lambda *args: better)
    same = lambda moment: (5000, 'USD')  # noqa: E731
    same.by_hour = False
    assert schedule.decide(fax(), hours(), None, MONDAY_PEAK, price_at=same).hold_until is None
    cheaper = lambda moment: (5000, 'USD') if moment == MONDAY_PEAK else (4100, 'USD')  # noqa: E731
    cheaper.by_hour = False
    decision = schedule.decide(fax(), hours(), None, MONDAY_PEAK, price_at=cheaper)
    assert (decision.hold_until, decision.why) == (later, 'faster')


def test_the_slot_search_matches_a_brute_force_over_every_permitted_hour():
    """Exact over the at most 12 hourly slots an ordinary fax may wait: random prices, recipient hours and busy
    hours, against every permitted hour priced directly."""
    rng = random.Random(84)
    for case in range(60):
        prices = {MONDAY_PEAK: rng.choice([4000, 5000, 6000])}
        busy_hours = set()
        for step in range(1, 14):
            moment = MONDAY_PEAK + timedelta(hours=step)
            prices[moment] = rng.choice([None, 3000, 4000, 5000, 6000, 7000])
            if rng.random() < 0.2:
                busy_hours.add(moment)
        busy = SimpleNamespace(busy_at=lambda moment: object() if moment in busy_hours else None)
        priced = lambda moment: None if prices.get(moment) is None else (prices[moment], 'USD')  # noqa: E731
        priced.by_hour = True
        found = schedule.cheaper_hour(fax(), hours(), busy, MONDAY_PEAK, None, priced)
        allowed = [moment for moment in sorted(prices) if MONDAY_PEAK < moment <= MONDAY_PEAK + schedule.HOUR_WAIT
                   and moment not in busy_hours and prices[moment] is not None
                   and prices[moment] < prices[MONDAY_PEAK]]
        if not allowed:
            assert found is None, case
            continue
        best = min(allowed, key=lambda moment: (prices[moment], moment))
        assert (found.at, found.then_micros) == (best, prices[best]), case


def test_pages_chosen_inside_a_plan_are_chosen_again_once_the_plan_is_used_up(monkeypatch, tmp_path):
    """M4: a selection priced inside a page allowance is published only while it still costs that: once other faxes
    used the plan's last pages (its price for these pages changed), it is chosen again before anything is sent."""
    from api.app.pages import sending
    from api.app.routing.predict import PlanUse
    plan = RateCard(None, 'efax', 'outbound', 'Synthetic plan', 'USD', 0, 0, 0, 60, 0, None, datetime(2026, 10, 10),
                    10_000_000)
    terms = RateTerms(plan, included_pages=200, overage_page_micros=100_000)

    def plan_facts(used):
        return RouteFacts('efax', 'eFax', DestinationClass(LOCAL, 'US', '+1', '+12025550123'), terms, Link(),
                          PlanUse(pages=used))
    shape = Shape(2, None, 'standard', 'normal')
    inside = predict_from(plan_facts(150), shape)
    assert inside.cost.micros == 0
    account = sending.Account('efax', 'efax', 'pdf_upload', plan, plan_facts(150))
    option = sending.Option('efax', 'as_is', 'normal', None, 2, (), inside.seconds, 0, 'USD', None, 0, None)
    evaluated = sending.Evaluated(account, None, 2, (option,), option, shape, (None, None), True, {})
    pdf = tmp_path / 'fax.pdf'
    pdf.write_bytes(b'%PDF-synthetic')
    evaluated.sources = (sending.RasterCache(tmp_path, '', '').digest(pdf), None)
    configuration = SimpleNamespace(provider_id='efax', manifest=None, traits={}, settings={})
    monkeypatch.setattr(sending, 'how_sent', lambda configuration: 'pdf_upload')
    for used, why in ((150, None), (199, "the plan's room changed")):
        monkeypatch.setattr(sending, 'account_for', lambda *args, used=used, **kwargs: sending.Account(
            'efax', 'efax', 'pdf_upload', plan, plan_facts(used)))
        assert sending.still_current(None, None, evaluated, configuration, {'to_number': '+12025550123'}, pdf,
                                     None) == why


def test_the_cheapest_partition_of_a_shared_call_differs_from_arrival_order(sip):
    """Cap 5 pages with separator pages; faxes of 1, 4 and 1 pages arrive in that order, the trunk bills whole
    minutes with a one-minute minimum. Arrival order sends three calls (the 4-page fax alone fills a call):
    $0.005 + about $0.00875 + $0.005. Together, the two 1-page faxes take four pages and one call, and the 4-page
    fax goes on its own: about $0.0175 in all. The claim sends the cheaper partition's call first."""
    from api.app.batching import store as batching
    from api.tests.test_batching import T0, accept as batch_accept
    configuration, delivery, *_ = sip
    batching.BatchingSettings(configuration.engine).save('+12025550123', enabled=True, actor='principal:p1',
                                                         max_pages=5)
    first = batch_accept(sip, pages=1, at=T0)
    middle = batch_accept(sip, pages=4, at=T0 + timedelta(seconds=1))
    last = batch_accept(sip, pages=1, at=T0 + timedelta(seconds=2))
    claim = delivery.claim('worker', now=T0 + timedelta(minutes=10))
    assert [member.job_id for member in claim.members] == [first, last]
    assert batching.member(configuration.engine, middle)['state'] == 'waiting'


def test_the_partition_search_matches_a_brute_force_over_every_partition():
    from api.app.batching import policy
    from api.app.batching.store import _arrival_partition, _need, best_partition

    def partitions(items):
        if not items:
            yield []
            return
        head, rest = items[0], items[1:]
        for smaller in partitions(rest):
            for index in range(len(smaller)):
                yield smaller[:index] + [[head] + smaller[index]] + smaller[index + 1:]
            yield [[head]] + smaller
    rng = random.Random(1084)
    for case in range(80):
        rows = [{'id': f'f{index}', 'pages': rng.randint(1, 4)} for index in range(rng.randint(2, 6))]
        cap = rng.randint(4, 9)
        layout = rng.choice(policy.LAYOUTS)
        step = rng.choice([1, 6, 60])

        def cost(pages, step=step):
            seconds = 11 + 12.3 * pages
            return 5000 * max(1, -(-int(seconds) // step) * step) // 60
        feasible = [calls for calls in partitions(rows)
                    if all(len(call) == 1 or _need(call, layout) <= cap for call in calls)
                    and all(layout != policy.LAYOUT_INDEX_PAGE or len(call) <= policy.INDEX_PAGE_DOCUMENTS
                            for call in calls)]
        best = min(sum(cost(_need(call, layout)) for call in calls) for calls in feasible)
        arrival = sum(cost(_need(call, layout)) for call in _arrival_partition(rows, cap, layout))
        found = best_partition(rows, cap, layout, cost)
        if best < arrival:
            assert found is not None and found[0] is rows[0], case
            # The call returned belongs to a partition that costs the brute-force minimum.
            rest = [row for row in rows if row not in found]
            remainder = min((sum(cost(_need(call, layout)) for call in calls) for calls in partitions(rest)
                             if all(len(call) == 1 or _need(call, layout) <= cap for call in calls)), default=0)
            assert cost(_need(found, layout)) + remainder == best, case
        else:
            assert found is None, case


def test_confirmed_long_pages_map_back_to_whole_call_pages():
    from api.app.batching.outcomes import confirmed_originals
    sheets = [[[1, True], [2, True], [3, True]], [[4, True], [5, False]], [[5, False], [6, True]]]
    assert confirmed_originals(sheets, 0) == (0, 3)
    assert confirmed_originals(sheets, 1) == (3, 5)
    assert confirmed_originals(sheets, 2) == (4, 6)  # page 5 continues on the third long page
    assert confirmed_originals(sheets, 3) == (6, None)


@pytest.mark.asyncio
async def test_a_shared_call_goes_on_long_pages_and_its_result_maps_back_to_each_fax(sip):
    """A receiving machine that takes pages of any length: the shared call image (separator, 2 pages, separator,
    1 page) goes as 2 long pages; every call page is recovered pixel for pixel, the long pages are listed, and a
    call that confirmed only the first long page leaves the first fax delivered and the second unconfirmed (part
    of it was on the long page being sent), never failed with nothing sent."""
    from pathlib import Path
    from api.app import conversion
    from api.app.batching import results
    from api.app.outbound_worker import OutboundWorker
    from api.app.pages import capability, unpack
    from api.tests.test_batching import Ami, NUMBER as BATCH_NUMBER, accept as batch_accept, transport, write_fax
    configuration, delivery, _, routes, data = sip
    capability.records_for(configuration.engine).record_observation(
        BATCH_NUMBER, source='d' * 32, engine='hylafax', values={'max_length': 'unlimited', 'ecm': 1, 'fine': 1},
        now=datetime.utcnow())
    first = batch_accept(sip, pages=2, files=True, at=datetime.utcnow() - timedelta(minutes=11))
    second = batch_accept(sip, pages=1, files=True, at=datetime.utcnow() - timedelta(minutes=10))
    ami = Ami()
    assert await OutboundWorker(delivery, transport(sip, ami)).step() is True
    [(job_id, _, tiff, attempt)] = ami.calls
    assert job_id == first and Path(tiff).name == f'packed-{first}-{attempt}.tiff'
    sent = conversion.read_fax_frames(tiff)
    assert len(sent) == 2
    call_image = conversion.read_fax_frames(str(data / f'batch-{attempt}.tiff'))
    recovered = unpack.split_frames(sent)
    assert [frame.tobytes() for frame in recovered] == [frame.tobytes() for frame in call_image]
    sheets = __import__('json').loads((data / f'packed-{first}-{attempt}.sheets.json').read_text())
    assert [[page for page, _ in sheet] for sheet in sheets] == [[1, 2, 3], [4, 5]]
    assert results.apply_fax_result(delivery, {'JobID': first, 'AttemptID': attempt, 'Status': 'FAILED',
                                               'Pages': '1'}) is True
    assert delivery.get(first)['state'] == 'success'
    assert delivery.get(second)['state'] == 'reconciliation_required'
    assert write_fax  # the documents were written by acceptance above


def test_the_scheduler_prices_each_hour_on_the_faxs_own_account(database):  # noqa: F811
    """Through the scheduler's own price: a saved GBP sip-aaisp card picks up the shipped bands for a UK number from
    a UK installation, and peak and evening differ."""
    from api.app.schema import upgrade_schema
    from api.app.routing.store import RouteStore
    upgrade_schema(database)
    RouteStore(database, sip_preset=lambda: 'aaisp').replace_cards([card()])
    values = SimpleNamespace(fax_default_country='GB', sip_trunk_preset='aaisp', sip_t38_enabled=True,
                             plan_budgets='', time_zone='Europe/London', provider_accounts={})
    priced = schedule.PriceAt(database, values, fax(), by_hour=schedule.priced_by_hour('sip', 'aaisp'))
    assert priced.by_hour is True and schedule.priced_by_hour('sip', 'telnyx') is False
    assert priced(MONDAY_PEAK) == (12_500, 'GBP')
    assert priced(MONDAY_PEAK + timedelta(hours=9)) == (10_417, 'GBP')
    assert list(itertools.islice(priced._found, 3))
