"""Callers of the shared predictor (routing/predict.py) run against the real predictor on synthetic facts.

The codec's own check once passed a list where the predictor's Shape takes a
tuple and compared Money with plain numbers. Every test then gave it fake
tools, so the failure only showed up in the product, hidden as "unavailable".
Each caller here gets one call through ``predict.facts_source`` (or
``predict_from`` on synthetic facts), with no stand-in. All prices are synthetic.
"""
from datetime import datetime
import logging
from types import SimpleNamespace

import pytest

from api.tests.test_schema import database  # noqa: F401 - fixture
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


def test_a_relays_signed_price_is_priced_by_the_real_predictor():
    """direct.relay.predicted: a partner's signed price body becomes the predictor's facts."""
    from api.tests.test_partner_relay_terms import DEST, NOW, _price
    prediction = relay.predicted(_price(20_000), DEST, 3, 'Valley Hospital', now=NOW)
    assert prediction.cost == Money(60_000, 'USD') and prediction.billed_pages == 3
    assert relay.predicted(_price(None), DEST, 3, 'Valley Hospital', now=NOW) is None  # no price: unknown


def _own(database, tmp_path):
    from api.tests.test_rules_delivery import TO, UNLISTED, installation
    env = installation(database, tmp_path, UNLISTED)  # Phaxio is the only route the automatic choice may use
    return env, env.snapshot.active.values, TO


def test_this_installations_own_route_is_priced_from_its_card(database, tmp_path):
    """direct.relay.own_prediction: the planner's first provider route, priced from the stored card."""
    env, values, to = _own(database, tmp_path)
    prediction, label = relay.own_prediction(env.engine, values, 'phaxio', to, 2)
    assert prediction.cost == Money(140_000, 'USD') and label == 'Phaxio'


def test_only_the_predictors_refusal_or_unreadable_records_make_a_price_unknown(database, tmp_path, monkeypatch,
                                                                                caplog):
    """The quiet ``except Exception`` blocks are narrowed: a refusal is logged with its cause; a bug is raised."""
    env, values, to = _own(database, tmp_path)

    def refuse(*args, **kwargs):
        raise ValueError('A fax has from 1 to 10,000 pages.')

    def bug(*args, **kwargs):
        raise KeyError('a programming error')
    monkeypatch.setattr(predict, 'predict_from', refuse)
    monkeypatch.setattr(predict, 'predict', refuse)
    with caplog.at_level(logging.WARNING):
        assert relay.own_prediction(env.engine, values, 'phaxio', to, 2) == (None, None)
        assert schedule.attempt_price('phaxio', to, 2) is None
    ours = [record for record in caplog.records if record.getMessage() in (
        "This installation's own route could not be priced.", 'The price of a try could not be predicted.')]
    assert len(ours) == 2 and all(record.exc_info[0] is ValueError for record in ours)
    monkeypatch.setattr(predict, 'predict_from', bug)
    monkeypatch.setattr(predict, 'predict', bug)
    with pytest.raises(KeyError):
        relay.own_prediction(env.engine, values, 'phaxio', to, 2)
    with pytest.raises(KeyError):
        schedule.attempt_price('phaxio', to, 2)


def test_relays_are_left_out_only_when_their_records_or_prices_cannot_be_read(monkeypatch, caplog):
    """routing.plan's relay offer: unreadable records leave the fax its own routes, logged; a bug is raised."""
    from app.routing.database import DeliveryStoreError
    from app.routing.plan import RoutePlanner
    planner = RoutePlanner(SimpleNamespace(engine=None))

    def unreadable(*args, **kwargs):
        raise DeliveryStoreError('Relay records are unavailable.')

    def bug(*args, **kwargs):
        raise KeyError('a programming error')
    monkeypatch.setattr(relay, 'relay_candidates', unreadable)
    with caplog.at_level(logging.WARNING):
        assert planner._relays(NUMBER, 2, None, None, DAY) == ([], {})
    assert [record.getMessage() for record in caplog.records] == ['Relays could not be offered for this fax.']
    monkeypatch.setattr(relay, 'relay_candidates', bug)
    with pytest.raises(KeyError):
        planner._relays(NUMBER, 2, None, None, DAY)
