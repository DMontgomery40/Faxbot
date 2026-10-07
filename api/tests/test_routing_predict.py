"""The shared pre-dial predictor: time on the line, billing models, number classes and the dry run.

Synthetic data only. The two recorded figures the defaults come from (the
SSL Fax spike of 2026-10-03 and the live engine call of 2026-10-05) are
checked here as recorded calls, and synthetic recorded calls check what
Faxbot learns from ``fax_engine_calls``.
"""
from datetime import datetime, timedelta
import json
import random

from PIL import Image, ImageDraw
import pytest

from api.app import schema
from api.tests.test_fax_negotiation import add_call
from api.tests.test_routing_http import ADMIN, client  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture
from app.routing import predict as predictor, predict_facts
from app.routing.costs import Money, RateCard, RateTerms, greater_of_pages, terms_cost
from app.routing.destinations import (INTERNATIONAL, LOCAL, PREMIUM, TOLL_FREE, UNKNOWN, DestinationClass,
                                      classify)
from app.routing.predict import Link, PlanUse, RouteFacts, Shape, line_seconds, predict_from


NOW = datetime(2026, 10, 7, 9, 0, 0)
DAY = datetime(2026, 10, 7)
NUMBER = '+12025550123'


def card(provider='sip-telnyx', *, minute='0', page='0', call='0', increment=60, minimum=0, monthly=None):
    from app.routing.costs import parse_amount
    return RateCard(None, provider, 'outbound', provider, 'USD', parse_amount(minute), parse_amount(page),
                    parse_amount(call), increment, minimum, None, DAY,
                    None if monthly is None else parse_amount(monthly, whole_digits=4))


def facts(terms, *, route='sip', label='Telnyx', number=NUMBER, link=Link(), plan=None, kind=LOCAL, region='US'):
    return RouteFacts(route, label, DestinationClass(kind, region, '+1', number), terms, link, plan)


def text_page():
    page = Image.new('1', (1728, 2287), 1)
    draw = ImageDraw.Draw(page)
    for line in range(40):
        draw.text((100, 100 + line * 52), 'Synthetic referral letter line %02d for the predictor test.' % line, fill=0)
    return page


def photo_page(seed=7):
    """A halftone-like page: scattered dots that T.6 compresses badly."""
    rng = random.Random(seed)
    page = Image.new('1', (1728, 2287), 1)
    pixels = page.load()
    for y in range(0, 2287):
        for x in range(0, 1728, 2):
            if rng.random() < 0.35:
                pixels[x, y] = 0
    return page


def g4_tiff(path, pages):
    first, *rest = pages
    first.save(path, save_all=True, append_images=rest, compression='group4', dpi=(204, 196))
    return path


# Shape and number classes ----------------------------------------------------------------------

def test_a_shape_names_every_page_size_and_known_values():
    assert Shape(2, (100, 200), 'fine', 'normal').pages == 2
    for bad in [dict(pages=0, page_bits=None), dict(pages=2, page_bits=(100,)), dict(pages=1, page_bits=(-1,)),
                dict(pages=1, page_bits=None, resolution='ultra'), dict(pages=1, page_bits=None, layout='tall')]:
        values = {'resolution': 'fine', 'layout': 'normal', **bad}
        with pytest.raises(ValueError):
            Shape(**values)


@pytest.mark.parametrize('number,home,kind,region', [
    ('+12025550123', 'US', LOCAL, 'US'),
    ('+18005550100', 'US', TOLL_FREE, 'US'),
    ('+18335550100', 'US', TOLL_FREE, 'US'),
    ('+18885550100', 'CA', TOLL_FREE, 'US'),     # North American toll-free is toll-free from Canada too
    ('+18225550100', 'US', UNKNOWN, None),       # 822 is reserved, not in service
    ('+18805550100', 'US', UNKNOWN, None),
    ('+19005550100', 'US', PREMIUM, 'US'),
    ('+14165550100', 'US', INTERNATIONAL, 'CA'),
    ('+442079460000', 'US', INTERNATIONAL, 'GB'),
    ('+448005550100', 'GB', TOLL_FREE, 'GB'),
    ('+448005550100', 'US', INTERNATIONAL, 'GB'),
    ('(202) 555-0123', 'US', LOCAL, 'US'),
    ('not a number', 'US', UNKNOWN, None),
])
def test_one_classifier_names_each_numbers_class(number, home, kind, region):
    found = classify(number, home)
    assert (found.kind, found.region) == (kind, region)


# Time on the line ------------------------------------------------------------------------------

def test_tiff_strip_bytes_give_each_pages_bits_and_photo_pages_stand_out(tmp_path):
    path = g4_tiff(tmp_path / 'fax.tiff', [text_page(), photo_page()])
    bits = predictor.tiff_page_bits(path)
    assert len(bits) == 2 and all(value > 0 and value % 8 == 0 for value in bits)
    assert bits[1] > 10 * bits[0]
    assert predictor.halftone_pages(Shape(2, bits, 'fine', 'normal')) == 1
    assert predictor.tiff_page_bits(tmp_path / 'missing.tiff') is None


def test_predicted_seconds_match_the_recorded_spike_call():
    """2026-10-03 spike: 6 pages at 14,400 bit/s with JBIG took 50 s of transfer, 10,270 bytes a page."""
    jbig_bits = 10_270 * 8
    prepared = round(jbig_bits / predictor.WIRE_FACTOR['JBIG'])
    link = Link(rate=14400, rate_calls=1, rate_scope='number', coding='JBIG')
    seconds, how = line_seconds(Shape(6, (prepared,) * 6, 'standard', 'normal'), link)
    transfer = seconds - predictor.SETUP_SECONDS
    assert abs(transfer - 50) <= 2.5
    assert 'from the size of each page at the speed 1 earlier fax to this number reached' in how


def test_learned_figures_come_only_from_successful_reported_calls(database):
    schema.upgrade_schema(database)
    reported = {'negotiation_by': 'hylafax', 'number': NUMBER, 'compression': 'MR', 'ecm': 'off',
                'rate_first': 9600, 'rate_lowest': 9600}
    for minute in range(3):
        # Recorded: 2 pages, 30 s of pages in a 41 s call (11 s outside the pages).
        add_call(database, job=f'j{minute}', pages=2, seconds=41, transfer_seconds=30,
                 when=NOW - timedelta(minutes=minute), **reported)
    # A failed call whose row holds what Faxbot asked for, not what the call reached: never learned from.
    add_call(database, job='bad', status='FAILED', pages=0, seconds=30, number=NUMBER, compression='JBIG',
             when=NOW, signal_rate='14400 bit/s', data_format='JBIG')
    rows = predict_facts.recorded_calls(database, since=NOW - timedelta(days=90), number=NUMBER)
    assert len(rows) == 3 and all(row['negotiation_by'] and row['fax_status'] == 'SUCCESS' for row in rows)
    unreported = {'number': NUMBER, 'negotiation_by': None, 'fax_status': 'SUCCESS', 'pages': 2,
                  'rate_lowest': 14400, 'compression': 'JBIG', 'connected_seconds': 20, 'transfer_seconds': 9}
    assert predict_facts.learn(rows + [unreported], NUMBER) == predict_facts.learn(rows, NUMBER)
    link = predict_facts.learn(rows, NUMBER)
    assert (link.rate, link.rate_scope, link.rate_calls, link.coding) == (9600, 'number', 3, 'MR')
    assert (link.seconds_per_page, link.page_calls, link.setup_seconds, link.setup_calls) == (15.0, 3, 11.0, 3)
    assert link.jbig is False
    # Without page sizes: 11 s + 15 s a page, the same as the recorded calls.
    seconds, how = line_seconds(Shape(2, None, 'fine', 'normal'), link)
    assert seconds == pytest.approx(41.0)
    assert 'from the time a page took on 3 earlier faxes to this number' in how
    # Another number on the same route learns the route's usual speed.
    other_rows = predict_facts.recorded_calls(database, since=NOW - timedelta(days=90), number='+12025550999')
    other = predict_facts.learn(other_rows, '+12025550999')
    assert (other.rate, other.rate_scope, other.seconds_per_page) == (9600, 'route', None)
    # Reads are bounded: the newest calls to the number, and the newest to any other number.
    assert len(predict_facts.recorded_calls(database, since=NOW - timedelta(days=90), number=NUMBER, limit=2)) == 2


def test_a_speed_limit_set_for_a_number_caps_what_earlier_calls_reached(database):
    from app import hylafax_records
    schema.upgrade_schema(database)
    for index in range(3):
        add_call(database, job=f'f{index}', pages=1, seconds=30, transfer_seconds=19, negotiation_by='hylafax',
                 number=NUMBER, compression='MMR', ecm='on', rate_first=14400, rate_lowest=14400,
                 when=datetime.utcnow() - timedelta(hours=index + 1))

    class Values:
        fax_default_country = 'US'
        sip_trunk_preset = 'telnyx'

    fast = predict_facts.facts_for('sip', NUMBER, values=Values(), engine=database)
    assert (fast.link.rate, fast.link.rate_scope) == (14400, 'number')
    hylafax_records.records_for(database).set_recipient_settings(NUMBER, max_rate=4800)
    capped = predict_facts.facts_for('sip', NUMBER, values=Values(), engine=database)
    assert capped.link.rate == 4800 and capped.link.typical_rate <= 4800
    shape = Shape(1, (180_000,), 'fine', 'normal')
    assert predict_from(capped, shape).seconds > predict_from(fast, shape).seconds


def test_predictions_check_out_against_recorded_synthetic_calls(database, tmp_path):
    """Recorded calls at a known speed: predicting each one's pages from its own image gives its own length."""
    schema.upgrade_schema(database)
    image = g4_tiff(tmp_path / 'letter.tiff', [text_page(), text_page()])
    bits = predictor.tiff_page_bits(image)
    rate, setup = 9600, 11.0
    recorded = setup + sum(bits) * predictor.WIRE_FACTOR['MR'] / rate + predictor.PAGE_SECONDS * 2
    for index in range(3):
        add_call(database, job=f'r{index}', pages=2, seconds=round(recorded), transfer_seconds=round(recorded - setup),
                 negotiation_by='hylafax', number=NUMBER, compression='MR', ecm='off', rate_first=rate,
                 rate_lowest=rate, when=NOW - timedelta(hours=index))
    learned = predict_facts.learn(predict_facts.recorded_calls(database, since=NOW - timedelta(days=90)), NUMBER)
    seconds, _ = line_seconds(Shape(2, bits, 'fine', 'normal'), learned)
    assert abs(seconds - round(recorded)) <= 1.5
    no_bits, _ = line_seconds(Shape(2, None, 'fine', 'normal'), learned)
    assert abs(no_bits - round(recorded)) <= 1.5


def test_a_coded_page_without_its_size_has_unknown_time_and_per_minute_cost_stays_unknown():
    shape = Shape(2, None, 'fine', 'codec')
    per_minute = predict_from(facts(RateTerms(card(minute='0.005', minimum=60))), shape)
    assert per_minute.seconds is None and per_minute.cost is None
    assert "a coded page's size depends on its data" in per_minute.basis
    per_page = predict_from(facts(RateTerms(card('sinch', page='0.045')), route='sinch', label='Sinch'), shape)
    assert per_page.cost == Money(90_000, 'USD') and per_page.billed_pages == 2


# Billing models ----------------------------------------------------------------------------------

def _seconds_for(target, pages=1):
    """Page bits that put a call at ``target`` seconds with the defaults (14,400 bit/s, MR)."""
    data = target - predictor.SETUP_SECONDS - predictor.PAGE_SECONDS * pages
    total = data * 14400 / predictor.WIRE_FACTOR['MR']
    return tuple([int(total // pages)] * pages)


def test_per_minute_billing_crosses_a_whole_minute_and_reports_the_room_left():
    terms = RateTerms(card(minute='0.005', increment=60, minimum=60))
    under = predict_from(facts(terms), Shape(1, _seconds_for(59), 'fine', 'normal'))
    over = predict_from(facts(terms), Shape(1, _seconds_for(61), 'fine', 'normal'))
    assert under.cost == Money(5000, 'USD') and over.cost == Money(10_000, 'USD')
    assert under.billed_pages == 0 and over.billed_pages == 0
    assert predictor.boundary(terms.card, under.seconds) == (60, pytest.approx(60 - under.seconds))
    assert predictor.boundary(terms.card, over.seconds)[0] == 120
    assert under.basis.startswith('Billed as 1 minute at $0.005 a minute; about 59 seconds on the line')
    # Six-second steps after a 30-second minimum (AnveoDirect to Canada).
    six = RateTerms(card('sip-anveo', minute='0.06', increment=6, minimum=30))
    assert predictor.boundary(six.card, 20.0)[0] == 30 and predictor.boundary(six.card, 31.2)[0] == 36


def test_per_page_and_per_call_prices_add_up():
    terms = RateTerms(card('example', page='0.05', call='0.01'))
    found = predict_from(facts(terms, route='example', label='Example'), Shape(3, None, 'fine', 'normal'))
    assert found.cost == Money(160_000, 'USD') and found.billed_pages == 3
    assert found.basis.startswith('Billed as 3 pages at $0.05 a page plus $0.01 for the call;')


def test_per_page_and_per_minute_cross_over_on_an_international_fixture(tmp_path):
    """A per-minute carrier abroad against a flat page price: one page goes cheaper by the page, two by the minute."""
    path = tmp_path / 'rate_cards.json'
    path.write_text(json.dumps({
        'cards': [{'provider_id': 'sip-example', 'label': 'Example trunk', 'direction': 'outbound',
                   'currency': 'USD', 'per_minute': '0.005', 'billing_increment_seconds': 60,
                   'minimum_seconds': 60, 'advertised_on': '2026-10-07'}],
        'providers': [{'provider_id': 'pages', 'label': 'Pages', 'direction': 'outbound', 'currency': 'USD',
                       'per_page': '0.07', 'advertised_on': '2026-10-07'}],
        'international': [
            {'route': 'sip-example', 'prefixes': ['+63'], 'pricing': 'own', 'currency': 'USD',
             'per_minute': '0.1645', 'billing_increment_seconds': 6, 'minimum_seconds': 60,
             'advertised_on': '2026-10-07'},
            {'route': 'pages', 'pricing': 'own', 'currency': 'USD', 'per_page': '0.10',
             'advertised_on': '2026-10-07'}]}))
    data = predict_facts.shipped(str(path))

    class Values:
        fax_default_country = 'US'
        sip_trunk_preset = 'example'

    manila = '+63288123456'

    def cost(route, pages):
        found = predict_facts.facts_for(route, manila, values=Values(), engine=None, data=data)
        assert found.destination.kind == INTERNATIONAL and found.destination.region == 'PH'
        return predict_from(found, Shape(pages, None, 'fine', 'normal')).cost.micros

    assert cost('pages', 1) < cost('sip', 1)       # $0.10 against a whole minute at $0.1645
    assert cost('sip', 2) < cost('pages', 2)       # under a minute for two pages: $0.1645 against $0.20
    assert cost('sip', 10) < cost('pages', 10)
    # A country with no entry for the route stays unknown.
    tokyo = predict_facts.facts_for('sip', '+81312345678', values=Values(), engine=None, data=data)
    assert predict_from(tokyo, Shape(1, None, 'fine', 'normal')).cost is None


def test_the_greater_of_pages_or_started_minutes_on_a_fax_plus_fixture():
    """Fax.Plus: the greater of physical pages or full/partial 60-second intervals; 1 page in 66 s bills 2."""
    assert greater_of_pages(1, 66, 60) == 2 and greater_of_pages(3, 66, 60) == 3
    assert greater_of_pages(1, 60, 60) == 1 and greater_of_pages(1, None, 60) is None
    fax_plus = RateTerms(card('faxplus', page='0.10'), page_time_seconds=60, published=True)
    assert terms_cost(fax_plus, seconds=66, pages=1) == (2, 200_000)
    slow = predict_from(facts(fax_plus, route='faxplus', label='Fax.Plus'), Shape(1, _seconds_for(66), 'fine', 'normal'))
    assert (slow.billed_pages, slow.cost) == (2, Money(200_000, 'USD'))
    assert 'the greater of the pages sent and each started minute on the line' in slow.basis
    unknown = predict_from(facts(fax_plus, route='faxplus', label='Fax.Plus'), Shape(1, None, 'fine', 'codec'))
    assert (unknown.billed_pages, unknown.cost) == (None, None)


def test_a_flat_plan_costs_nothing_extra_and_says_how_much_room_is_left():
    humblefax = RateTerms(card('humblefax', monthly='10.00'), published=True)
    plain = predict_from(facts(humblefax, route='humblefax', label='HumbleFax'), Shape(3, None, 'fine', 'normal'))
    assert (plain.cost, plain.marginal, plain.billed_pages) == (Money(0, 'USD'), True, 0)
    assert plain.basis.startswith('Included in your HumbleFax plan ($10 a month), so this fax adds nothing to the bill')
    budget = predict_from(facts(humblefax, route='humblefax', label='HumbleFax',
                                plan=PlanUse(pages=1999, faxes=400, page_budget=2000)), Shape(3, None, 'fine', 'normal'))
    assert budget.cost == Money(0, 'USD')
    assert '1999 of the 2000 pages you allow on it a month used so far, and this fax would go past that' in budget.basis


def test_a_page_allowance_plan_charges_only_the_pages_past_it():
    efax = RateTerms(card('efax', monthly='18.99'), included_pages=200, overage_page_micros=100_000)
    shape = Shape(3, None, 'fine', 'normal')
    inside = predict_from(facts(efax, route='efax', label='eFax', plan=PlanUse(pages=150)), shape)
    past = predict_from(facts(efax, route='efax', label='eFax', plan=PlanUse(pages=199)), shape)
    unknown = predict_from(facts(efax, route='efax', label='eFax', plan=PlanUse()), shape)
    assert (inside.cost, inside.marginal) == (Money(0, 'USD'), True)
    assert (past.cost, past.billed_pages, past.marginal) == (Money(200_000, 'USD'), 2, True)
    assert unknown.cost is None and unknown.marginal is True
    assert 'Faxbot has no count of the pages sent on it this month' in unknown.basis


def test_a_route_that_publishes_a_page_limit_refuses_a_longer_fax():
    capped = RateTerms(card('capped', monthly='10.00'), max_pages_per_fax=100)
    found = predict_from(facts(capped, route='capped', label='Capped'), Shape(101, None, 'fine', 'normal'))
    assert found.cost is None and 'takes at most 100 pages in one fax' in found.basis


def test_toll_free_numbers_use_each_routes_toll_free_price(tmp_path):
    """Builder AE's ``toll_free`` list: Telnyx free, Phaxio its card, Sinch not published."""
    path = tmp_path / 'rate_cards.json'
    path.write_text(json.dumps({
        'cards': [{'provider_id': 'sip-telnyx', 'label': 'Telnyx', 'direction': 'outbound', 'currency': 'USD',
                   'per_minute': '0.005', 'billing_increment_seconds': 60, 'minimum_seconds': 60,
                   'advertised_on': '2026-10-03'}],
        'providers': [{'provider_id': 'phaxio', 'label': 'Phaxio', 'direction': 'outbound', 'currency': 'USD',
                       'per_page': '0.07', 'advertised_on': '2026-10-03'},
                      {'provider_id': 'sinch', 'label': 'Sinch', 'direction': 'outbound', 'currency': 'USD',
                       'per_page': '0.045', 'advertised_on': '2026-10-03'}],
        'toll_free': [
            {'route': 'sip-telnyx', 'reaches': 'yes', 'pricing': 'own', 'currency': 'USD', 'per_minute': '0',
             'per_page': '0', 'per_call': '0', 'billing_increment_seconds': 60, 'minimum_seconds': 60,
             'advertised_on': '2026-10-07'},
            {'route': 'phaxio', 'reaches': 'yes', 'pricing': 'same_as_card'},
            {'route': 'sinch', 'reaches': 'not_published', 'pricing': 'not_published'},
            {'route': 'humblefax', 'reaches': 'no', 'pricing': 'not_published'}]}))
    data = predict_facts.shipped(str(path))

    class Values:
        fax_default_country = 'US'
        sip_trunk_preset = 'telnyx'

    shape = Shape(2, None, 'fine', 'normal')

    def found(route, number='+18005550100'):
        return predict_from(predict_facts.facts_for(route, number, values=Values(), engine=None, data=data), shape)

    free = found('sip')
    assert free.cost == Money(0, 'USD') and free.marginal is False
    assert free.basis.startswith('Telnyx charges nothing for calls to toll-free numbers;')
    assert found('sip', NUMBER).cost == Money(5000, 'USD')
    assert found('phaxio').cost == Money(140_000, 'USD')
    sinch = found('sinch')
    assert sinch.cost is None and sinch.basis.startswith('Sinch publishes no price for faxes to toll-free numbers')
    refused = found('humblefax')
    assert refused.cost is None
    assert refused.basis == 'HumbleFax does not call toll-free numbers, so this fax cannot go this way.'


def test_the_shipped_prices_abroad_parse_and_match_their_sources():
    """The real config/rate_cards.json: read 2026-10-07 from AnveoDirect's rate deck and Phaxio's price page."""
    predict_facts.shipped.cache_clear()
    data = predict_facts.shipped()

    class Anveo:
        fax_default_country = 'US'
        sip_trunk_preset = 'anveo'

    class Telnyx:
        fax_default_country = 'US'
        sip_trunk_preset = 'telnyx'

    def facts_of(route, number, values=Anveo):
        return predict_facts.facts_for(route, number, values=values(), engine=None, data=data)

    london = facts_of('sip', '+442079460000')
    assert london.terms.card.per_minute_micros == 2410 and london.terms.card.billing_increment_seconds == 1
    assert london.terms.published and london.label == 'AnveoDirect'
    assert facts_of('sip', '+18675550100').terms.card.per_minute_micros == 54_600  # +1867 beats Canada
    assert facts_of('sip', '+14165550100').terms.card.per_minute_micros == 2050
    assert facts_of('phaxio', '+14165550100').terms.card.per_page_micros == 70_000   # same as its US card
    assert facts_of('phaxio', '+63288123456').terms.card.per_page_micros == 100_000
    assert facts_of('sip', '+442079460000', Telnyx).terms is None
    assert facts_of('sinch', '+442079460000').terms is None


def test_unknown_stays_unknown():
    shape = Shape(1, None, 'fine', 'normal')
    no_card = predict_from(RouteFacts('sip', 'Telnyx', DestinationClass(LOCAL, 'US', '+1', NUMBER), None,
                                      missing='Telnyx has no rate card'), shape)
    assert no_card.cost is None and no_card.basis.startswith('Telnyx has no rate card, so the cost is unknown')
    premium = predict_facts.terms_for('sip-telnyx', classify('+19005550100'), card(minute='0.005'),
                                      {'plans': [], 'classes': {}})
    assert premium == (None, None)
    unpublished = card(minute='0')  # a published card with nothing per minute is a price, not an unknown
    assert predict_from(facts(RateTerms(unpublished)), shape).cost == Money(0, 'USD')


def test_routes_without_a_call_cost_nothing():
    for route in ('local', 'direct'):
        found = predict_from(RouteFacts(route, route, DestinationClass(LOCAL, 'US', '+1', NUMBER), None),
                             Shape(4, None, 'fine', 'normal'))
        assert (found.cost, found.seconds, found.billed_pages) == (Money(0, 'USD'), 0.0, 0)


def test_predict_works_without_a_database_and_takes_injected_facts():
    shape = Shape(2, None, 'fine', 'normal')
    shipped = predictor.predict('phaxio', NUMBER, shape)
    assert shipped.cost == Money(140_000, 'USD') and shipped.billed_pages == 2
    injected = facts(RateTerms(card(minute='0.01')))
    with predictor.facts_source(lambda route, destination, now=None: injected):
        assert predictor.predict('anything', NUMBER, shape).cost == Money(10_000, 'USD')
    with pytest.raises(TypeError):
        predictor.predict('phaxio', NUMBER, {'pages': 2})


def test_the_photo_coding_is_compared_only_for_a_number_that_took_it(tmp_path):
    bits = predictor.tiff_page_bits(g4_tiff(tmp_path / 'photo.tiff', [photo_page(), text_page()]))
    shape = Shape(2, bits, 'fine', 'normal')
    terms = RateTerms(card(minute='0.005', minimum=60))
    took = facts(terms, link=Link(coding='MMR', jbig=True))
    choice = predictor.jbig_choice(took, shape)
    assert choice.faster == 'jbig' and choice.jbig.seconds < choice.standard.seconds
    assert "only Faxbot's fast fax service sends; for the 1 photo-like page here it should save" in choice.sentence
    assert predictor.jbig_choice(facts(terms, link=Link(coding='MMR')), shape) is None
    text_only = Shape(1, (bits[1],), 'fine', 'normal')
    assert predictor.jbig_choice(took, text_only) is None
    assert predictor.jbig_choice(facts(terms, route='phaxio', link=Link(jbig=True)), shape) is None


# The dry run -----------------------------------------------------------------------------------

def test_the_dry_run_prices_every_allowed_route(client):  # noqa: F811 - fixture
    response = client.get('/routing/predict', headers=ADMIN, params={'to': '(202) 555-0123', 'pages': 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['to'] == NUMBER and body['number_class'] == 'local' and body['number_class_text'] == 'a local number'
    routes = {route['route']: route for route in body['routes']}
    assert list(routes) == ['phaxio', 'sip', 'signalwire']
    assert routes['phaxio']['cost'] == {'amount': '0.14', 'currency': 'USD'}
    assert routes['phaxio']['headline'] == 'About $0.14 for this 2-page fax.'
    assert routes['phaxio']['billed_pages'] == 2 and routes['phaxio']['marginal'] is False
    assert routes['signalwire']['billed_seconds'] in (60, 120) and routes['signalwire']['seconds'] > 0
    assert all(route['basis'].endswith('.') for route in body['routes'])
    assert body['sentence'].startswith(('SignalWire would cost least', 'Phaxio would cost least',
                                        'Telnyx would cost least', 'Carrier trunk would cost least'))
    bad = client.get('/routing/predict', headers=ADMIN, params={'to': NUMBER, 'layout': 'tall'})
    assert bad.status_code == 400 and bad.json()['detail'] == 'Choose a normal or dense layout.'
    assert client.get('/routing/predict', params={'to': NUMBER}).status_code in (401, 403)
