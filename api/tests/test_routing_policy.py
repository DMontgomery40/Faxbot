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
    # With one provider there is nothing to compare: it is the configured provider, not the cheapest.
    assert keys(RoutePolicy().order([trunk, DIRECT])) == [('direct', 'direct_peer'), ('sip', 'configured')]


def test_explicit_provider_preference_overrides_cost_and_direct_route():
    trunk = provider('sip', card('sip', minute='0.001'), bound=True)
    api = provider('phaxio', card('phaxio', page='0.07'))
    stats = {'phaxio': RouteStats(attempts=10, successes=1)}
    assert keys(RoutePolicy().order([trunk, api, DIRECT], stats=stats, preferred='phaxio')) == [
        ('phaxio', 'preferred'), ('direct', 'direct_peer'), ('sip', 'alternative')]


def test_preferring_direct_keeps_the_default_order():
    trunk = provider('sip', card('sip', minute='0.001'), bound=True)
    assert keys(RoutePolicy().order([trunk, DIRECT], preferred='direct')) == [
        ('direct', 'direct_peer'), ('sip', 'configured')]


def test_no_providers_means_no_routes():
    assert RoutePolicy().order([]) == []


def test_unknown_cost_sorts_after_known_cost_and_keeps_outbound_provider_first_on_ties():
    unknown_bound = provider('documo', None, bound=True)
    unknown = provider('sinch', None)
    priced = provider('signalwire', card('signalwire', minute='0.0095'))
    # Other routes' prices are unknown, so the priced one is only the cheapest with a known price.
    assert keys(RoutePolicy().order([unknown, unknown_bound, priced])) == [
        ('signalwire', 'known_cheapest'), ('documo', 'alternative'), ('sinch', 'alternative')]


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


UK = '+441782684953'


@pytest.mark.parametrize('country, raw, expected', [
    # A US installation: every way of writing one number gives one E.164 identity.
    ('US', '303 555 0123', '+13035550123'), ('US', '(303) 555-0123', '+13035550123'),
    ('US', '1-303-555-0123', '+13035550123'), ('US', '13035550123', '+13035550123'),
    ('US', '+1 303 555 0123', '+13035550123'), ('US', '011 44 1782 684953', UK), ('US', '+44 1782 684953', UK),
    # A UK installation reads national numbers with the trunk 0 the way people dial them.
    ('GB', '01782 684953', UK), ('GB', '01782684953', UK), ('GB', '(01782) 684 953', UK),
    ('GB', '+44 1782 684953', UK), ('GB', '0044 1782 684953', UK), ('GB', UK, UK),
    ('GB', '+1 303 555 0123', '+13035550123'), ('GB', '00 1 303 555 0123', '+13035550123'),
    ('AU', '02 9876 5432', '+61298765432'), ('AU', '0011 44 1782 684953', UK),
])
def test_numbers_resolve_for_the_installation_country(country, raw, expected):
    assert normalize_number(raw, country=country) == expected


@pytest.mark.parametrize('country, raw', [
    ('US', '555 0100'), ('GB', '684953'),          # no area code: only dialable locally
    ('US', '442079460000'),                         # a UK number without its +: refused, not guessed
    ('US', '+3035550123'),                          # +30 is Greece; this is not a complete number there
    ('US', '0123456789'), ('US', '+1 555 0100'),    # no North American area code starts with 0; +1 needs ten digits
    ('GB', '01782 684953 1234'), ('US', '303-555-0123 ext 4'), ('US', '1-800-FLOWERS'),
])
def test_incomplete_or_ambiguous_numbers_are_refused_with_one_sentence(country, raw):
    with pytest.raises(InvalidNumber) as error:
        normalize_number(raw, country=country)
    message = str(error.value)
    assert message.endswith('.') and raw not in message


def test_only_the_local_number_is_reported_as_missing_its_area_code():
    from api.app.routing.numbers import AmbiguousNumber
    with pytest.raises(AmbiguousNumber, match='area code'):
        normalize_number('555 0100', country='US')


def test_canonical_numbers_are_kept_and_anything_else_is_never_reinterpreted():
    from api.app.routing.numbers import accepted_destination, canonical_number, is_canonical
    assert canonical_number(UK) == UK and is_canonical('+13035550123')
    for value in ('01782684953', '+44 1782 684953', '441782684953', '3035550123', '+3035550123', 3035550123):
        assert not is_canonical(value)
    # Accepted jobs keep their number under any later country; older jobs that
    # stored the entered text resolve once with the country captured for them.
    assert accepted_destination(UK, country='US') == UK
    assert accepted_destination('3035550123', country='US') == '+13035550123'
    assert accepted_destination('01782 684953', country='GB') == UK
    with pytest.raises(InvalidNumber):
        accepted_destination('01782 684953', country='US')


def test_installation_country_must_be_a_known_region():
    with pytest.raises(ValueError, match='country'):
        normalize_number('303 555 0123', country='XX')
