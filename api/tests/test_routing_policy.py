"""Route ordering, exact cost arithmetic and number normalization; no I/O."""
from datetime import datetime

import pytest

from api.app.routing.costs import (InvalidRateCard, RateCard, attempt_cost, billed_seconds, estimate_cost,
                                   format_amount, parse_amount)
from api.app.routing.numbers import InvalidNumber, normalize_number
from api.app.routing.policy import RouteCandidate, RoutePolicy, RouteStats


CAPTURED = datetime(2026, 10, 3)


def card(provider, *, minute='0', page='0', call='0', increment=60, minimum=0):
    return RateCard(None, provider, 'outbound', provider.title(), 'USD', parse_amount(minute), parse_amount(page),
                    parse_amount(call), increment, minimum, None, CAPTURED)


def provider(key, rate=None, bound=False):
    return RouteCandidate(key, 'provider', key, rate, bound=bound)


DIRECT = RouteCandidate('direct', 'direct', 'direct', None, peer_id='peer-1')


def keys(choices):
    return [(choice.route.key, choice.reason) for choice in choices]


def test_cheapest_reliable_route_wins_even_when_it_is_not_the_outbound_provider():
    api = provider('phaxio', card('phaxio', page='0.007', minute='0.005'), bound=True)
    trunk = provider('sip', card('sip', minute='0.005'))
    choices = RoutePolicy().order([api, trunk], pages=20)
    assert keys(choices) == [('sip', 'cheapest'), ('phaxio', 'alternative')]
    assert choices[0].estimated_cost_micros < choices[1].estimated_cost_micros


def test_unreliable_route_is_demoted_but_kept_as_a_last_resort():
    api = provider('phaxio', card('phaxio', page='0.007'), bound=True)
    trunk = provider('sip', card('sip', minute='0.005'))
    stats = {'sip': RouteStats(attempts=5, successes=2), 'phaxio': RouteStats(attempts=5, successes=5)}
    assert keys(RoutePolicy().order([api, trunk], stats=stats, pages=3)) == [
        ('phaxio', 'cheapest'), ('sip', 'unreliable')]


def test_too_few_outcomes_are_not_evidence_of_unreliability():
    trunk = provider('sip', card('sip', minute='0.005'))
    api = provider('phaxio', card('phaxio', page='0.007'), bound=True)
    stats = {'sip': RouteStats(attempts=2, successes=0)}
    assert keys(RoutePolicy(min_attempts=3).order([api, trunk], stats=stats))[0] == ('sip', 'cheapest')


def test_verified_direct_route_is_preferred_over_every_provider():
    trunk = provider('sip', card('sip', minute='0.001'), bound=True)
    assert keys(RoutePolicy().order([trunk, DIRECT])) == [('direct', 'direct_peer'), ('sip', 'cheapest')]


def test_explicit_provider_preference_overrides_cost_and_direct_route():
    trunk = provider('sip', card('sip', minute='0.001'), bound=True)
    api = provider('phaxio', card('phaxio', page='0.07'))
    stats = {'phaxio': RouteStats(attempts=10, successes=1)}
    assert keys(RoutePolicy().order([trunk, api, DIRECT], stats=stats, preferred='phaxio')) == [
        ('phaxio', 'preferred'), ('direct', 'direct_peer'), ('sip', 'alternative')]


def test_preferring_direct_keeps_the_default_order():
    trunk = provider('sip', card('sip', minute='0.001'), bound=True)
    assert keys(RoutePolicy().order([trunk, DIRECT], preferred='direct')) == [
        ('direct', 'direct_peer'), ('sip', 'cheapest')]


def test_no_providers_means_no_routes():
    assert RoutePolicy().order([]) == []


def test_unknown_cost_sorts_after_known_cost_and_keeps_outbound_provider_first_on_ties():
    unknown_bound = provider('documo', None, bound=True)
    unknown = provider('sinch', None)
    priced = provider('signalwire', card('signalwire', minute='0.0095'))
    assert keys(RoutePolicy().order([unknown, unknown_bound, priced])) == [
        ('signalwire', 'cheapest'), ('documo', 'alternative'), ('sinch', 'alternative')]


def test_duplicate_routes_are_rejected():
    with pytest.raises(ValueError):
        RoutePolicy().order([provider('sip'), provider('sip')])


def test_whole_minute_rounding_matches_the_research_example():
    # 20 pages at 30 s/page plus 30 s setup = 10.5 minutes, billed as 11 whole minutes.
    trunk = card('sip', minute='0.005')
    assert billed_seconds(trunk, 630) == 660
    assert attempt_cost(trunk, seconds=630, pages=20, delivered=True) == parse_amount('0.055')
    assert estimate_cost(trunk, 20) == parse_amount('0.055')
    api = card('telnyx-fax', page='0.007', minute='0.005')
    # 500 such documents: $97.50 through a per-page API, $27.50 over a SIP trunk.
    assert 500 * estimate_cost(api, 20) == parse_amount('97.5')
    assert 500 * estimate_cost(trunk, 20) == parse_amount('27.5')


def test_rounding_increments_minimums_and_failed_pages():
    six_second = card('x', minute='0.0095', increment=6, minimum=30)
    assert billed_seconds(six_second, 0) == 0
    assert billed_seconds(six_second, 1) == 30
    assert billed_seconds(six_second, 31) == 36
    assert billed_seconds(six_second, 30.2) == 36
    per_page = card('y', page='0.10', call='0.01')
    assert attempt_cost(per_page, seconds=200, pages=4, delivered=True) == parse_amount('0.41')
    assert attempt_cost(per_page, seconds=200, pages=4, delivered=False) == parse_amount('0.01')
    # Fractions of a micro round up, like an invoice.
    assert attempt_cost(card('z', minute='0.000001'), seconds=1, pages=0, delivered=True) == 1


def test_amount_parsing_and_formatting_are_exact():
    assert parse_amount('0.0095') == 9500
    assert parse_amount(2) == 2_000_000
    assert format_amount(9500) == '0.0095'
    assert format_amount(0) == '0.00'
    assert format_amount(1_500_000) == '1.50'
    assert format_amount(55_000) == '0.055'
    for bad in ('-1', '0.0000001', 'abc', True, '1e3', '1000'):
        with pytest.raises(InvalidRateCard):
            parse_amount(bad)


def test_rate_card_validation_uses_plain_sentences():
    with pytest.raises(InvalidRateCard, match='three-letter currency'):
        RateCard(None, 'sip', 'outbound', 'Trunk', 'usd', 0, 0, 0, 60, 0, None, CAPTURED)
    with pytest.raises(InvalidRateCard, match='Billing increments'):
        RateCard(None, 'sip', 'outbound', 'Trunk', 'USD', 0, 0, 0, 0, 0, None, CAPTURED)


@pytest.mark.parametrize('raw, expected', [
    ('+1 (555) 010-0001', '+15550100001'), ('5550100001', '+15550100001'),
    ('1-555-010-0001', '+15550100001'), ('+44 20 7946 0000', '+442079460000'),
])
def test_numbers_normalize_to_e164(raw, expected):
    assert normalize_number(raw) == expected


@pytest.mark.parametrize('raw', ['', '555-0100', '+0123456789', 'call me', '+1+5550100001', '0' * 70])
def test_unusable_numbers_are_rejected(raw):
    with pytest.raises(InvalidNumber):
        normalize_number(raw)
