"""Callers of the shared predictor (routing/predict.py) run against the real predictor on synthetic facts.

The codec's own check once passed a list where the predictor's Shape takes a
tuple and compared Money with plain numbers. Every test then gave it fake
tools, so the failure only showed up in the product, hidden as "unavailable".
Each caller here gets one call through ``predict.facts_source`` (or
``predict_from`` on synthetic facts), with no stand-in. All prices are synthetic.
"""
from datetime import datetime

from app.direct import relay
from app.pages import decision as pages_decision
from app.routing import plan_budget, predict, schedule
from app.routing.costs import Money, RateCard, RateTerms, parse_amount
from app.routing.destinations import LOCAL, DestinationClass

NUMBER = '+12025550123'
DAY = datetime(2026, 10, 7)


def card(provider, *, page='0', minute='0', increment=60):
    return RateCard(None, provider, 'outbound', provider, 'USD', parse_amount(minute), parse_amount(page),
                    parse_amount('0'), increment, 0, None, DAY, None)


def facts(route, rates):
    return predict.RouteFacts(route, route.title(), DestinationClass(LOCAL, 'US', '+1', NUMBER), RateTerms(rates))


def source(**cards):
    table = {route: facts(route, rates) for route, rates in cards.items()}
    return predict.facts_source(lambda route, destination, now=None: table[route])


STAND_IN_BASES = {'per_page', 'per_minute', 'plan', 'unpriced'}


def test_the_layout_choosers_prices_come_from_the_shared_predictor_for_every_shape():
    """pages.decision (the attempt-time layout chooser): no quiet fall back to the stand-in."""
    shapes = [pages_decision.Shape(5, (200_000,) * 5, 'fine', 'normal'),
              pages_decision.Shape(2, (601_200, 400_800), 'fine', 'dense'),
              pages_decision.Shape(1, (180_000,), 'fine', 'codec')]
    with source(sinch=card('sinch', page='0.045')):
        prices = pages_decision.price_all('sinch', NUMBER, shapes)
    assert [price.cost for price in prices] == [Money(225_000, 'USD'), Money(90_000, 'USD'), Money(45_000, 'USD')]
    assert all(price.basis not in STAND_IN_BASES for price in prices)
    ranked = sorted(zip(prices, shapes), key=lambda pair: pages_decision.rank(*pair))
    assert ranked[0][1].layout == 'codec'


def test_a_try_is_priced_as_money_text_for_the_scheduler():
    """routing.schedule.attempt_price (failed-try billing sentences)."""
    with source(sinch=card('sinch', page='0.05')):
        assert schedule.attempt_price('sinch', NUMBER, 2) == '$0.10'


def test_relays_and_plan_budgets_order_real_predictions_by_their_money():
    """direct.relay.rank and routing.plan_budget.marginal take the predictor's Money, never plain numbers."""
    shape = predict.Shape(3, None, 'fine', 'normal')
    cheap = predict.predict_from(facts('relay', card('relay', page='0.02')), shape)
    dear = predict.predict_from(facts('relay', card('relay', page='0.05')), shape)
    candidates = [relay.RelayCandidate(f'relay:{name}', name, 'agreement', name, prediction, '')
                  for name, prediction in (('dear', dear), ('cheap', cheap), ('unknown', None))]
    assert [item.peer_id for item in sorted(candidates, key=relay.rank)] == ['cheap', 'dear', 'unknown']
    found = plan_budget.marginal(None, 3, cheap)
    assert found.cost == Money(60_000, 'USD') and plan_budget.order_key(found)[2] == 60_000
