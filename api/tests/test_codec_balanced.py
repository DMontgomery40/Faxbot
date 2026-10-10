"""The balanced capacity profile on routes billed by the greater of pages and time (brief 99, item 3).

Capacity pages (payload format 2) have a per-minute optimum (profile 'time') and a per-page optimum ('pages'). On a
route that bills the greater of the pages sent and each started period on the line, the cheapest point is where the
two counts cross, so the encoded-page chooser also tries the published 'balanced' profile there, and the real
predictor's bill decides. Synthetic random documents; the 180-second unit is synthetic, chosen only to put the
crossing between the two pure profiles: no published fax tariff found (searched 10 October 2026) bills the greater
of pages and a period longer than a minute.
"""
from datetime import datetime
import random

import pytest

from app import codec
from app.codec import capacity, decision
from app.routing.costs import RateCard, RateTerms, parse_amount
from app.routing.destinations import classify
from app.routing.predict import Link, Prediction, RouteFacts, Shape, predict_from

NUMBER = '+12025550123'


def _document(size, seed=7):
    return codec.Document(random.Random(seed).randbytes(size), 'application/pdf', 'synthetic.pdf')


def _card(page='0.10', minute='0'):
    return RateCard(None, 'synthetic', 'outbound', 'Synthetic', 'USD', parse_amount(minute), parse_amount(page), 0, 60,
                    0, None, datetime(2026, 10, 10))


def _real(terms, rate=14_400):
    """The shared predictor's own arithmetic (``predict_from``) on these terms, at a learned speed."""
    facts = RouteFacts('synthetic', 'Synthetic', classify(NUMBER), terms,
                       link=Link(rate=rate, rate_calls=5, rate_scope='number'))
    seen = []

    def predict(route_key, destination, shape, *, now=None):
        found = predict_from(facts, shape)
        seen.append((shape, found))
        return found
    return (predict, Shape), seen


def _choose(tools, document, memo=None, pages_original=40):
    return decision.choose(document, route_key='synthetic', destination=NUMBER, pages_original=pages_original,
                           page_bits_original=[400_000] * pages_original, exact_raster=True, ecm_and_fine_seen=True,
                           provider_renders=False, tools=tools, capacity=True, memo=memo)


def _profiles_tried(monkeypatch):
    from app.codec import pages
    tried = []
    real = pages.encode

    def encode(*args, **kwargs):
        if kwargs.get('layout') == 'capacity':
            tried.append(capacity.PROFILES[kwargs['profile']][0])
        return real(*args, **kwargs)
    monkeypatch.setattr(pages, 'encode', encode)
    return tried


@pytest.mark.parametrize('terms, balanced', [
    (RateTerms(_card(page='0.10')), False),                        # by the page
    (RateTerms(_card(page='0', minute='0.05')), False),             # by the minute
    (RateTerms(_card(page='0.10'), page_time_seconds=60), True),    # the greater of pages and minutes (Fax.Plus)
])
def test_balanced_is_tried_only_where_the_bill_counts_the_greater_of_pages_and_time(monkeypatch, terms, balanced):
    tried = _profiles_tried(monkeypatch)
    tools, _ = _real(terms)
    _choose(tools, _document(20_000), pages_original=3)
    assert tried == (['time', 'balanced', 'pages'] if balanced else ['time', 'pages'])
    assert decision.greater_of(Prediction(1, 60.0, None, 'synthetic', False, expected_billed_pages=1.5)) is True
    assert decision.greater_of(Prediction(1, 60.0, None, 'synthetic', False)) is False


@pytest.fixture(scope='module')
def memo():
    """One 600 kB document's encoded candidates and measured codings, shared by the cases below."""
    return {}


def test_on_a_three_minute_greater_of_card_balanced_beats_both_pure_profiles(memo):
    document = _document(600_000)
    tools, seen = _real(RateTerms(_card(page='0.10'), page_time_seconds=180))
    chosen = _choose(tools, document, memo)
    assert chosen.use and chosen.layout == 'capacity'
    assert chosen.profile == capacity.PROFILE_NAMES['balanced']
    # The same predictor numbers for each profile's own pages: balanced's bill is the lowest of the three.
    bills = {}
    for name in ('time', 'balanced', 'pages'):
        pages = codec.encode_document(document, layout='capacity', profile=capacity.PROFILE_NAMES[name], fec='medium')
        tools_one, _ = _real(RateTerms(_card(page='0.10'), page_time_seconds=180))
        shape = decision._unchosen_shape(Shape, pages.pages, 'fine', 'codec', memo)
        bills[name] = tools_one[0]('synthetic', NUMBER, shape)
    assert bills['balanced'].cost.micros < bills['time'].cost.micros
    assert bills['balanced'].cost.micros < bills['pages'].cost.micros
    assert chosen.encoded.cost == bills['balanced'].cost
    # Expected billed pages: time sends four pages (four billed), pages sends two whose time bills more, and
    # balanced's three pages land between them.
    assert (bills['time'].expected_billed_pages, bills['balanced'].expected_billed_pages,
            bills['pages'].expected_billed_pages) == pytest.approx((4.0, 3.75, 4.25))


def test_on_a_one_minute_greater_of_card_the_shortest_call_still_wins(memo):
    # Fax.Plus's published rule (a page for each started minute on the line). At 14,400 bit/s every capacity page
    # takes longer than a minute, so the minute count decides and the time profile, the fewest seconds, is cheapest.
    tools, seen = _real(RateTerms(_card(page='0.10'), page_time_seconds=60))
    chosen = _choose(tools, _document(600_000), memo)
    assert chosen.use and chosen.layout == 'capacity' and chosen.profile == capacity.PROFILE_NAMES['time']
    assert any(shape.layout == 'codec' for shape, _ in seen)
