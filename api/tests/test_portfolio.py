"""Explicit synthetic setup portfolios checked against an independent complete search."""
from copy import deepcopy
from decimal import Decimal
from itertools import combinations
import random

import pytest
from pydantic import ValidationError
from app.routing.portfolio import PortfolioInput, plan


def scenario():
    return {'currency': 'USD', 'horizon': 'Next twelve months', 'perspective': 'Our installation',
            'budget': '12', 'nodes': [
                {'id': n, 'label': n.upper(), 'cost': '6' if n == 'r' else '2',
                 'installed': False, 'group_id': None} for n in ('a', 'b', 'c', 'r')],
            'groups': [], 'relationships': [
                {'a': n, 'b': 'r', 'expected': '6', 'cautious': '2'} for n in ('a', 'b', 'c')]}


def oracle(raw, selected):
    """Recompute complete costs and benefits, independently of production incremental helpers."""
    installed = {n['id'] for n in raw['nodes'] if n['installed']}
    node_groups = {n['id']: n['group_id'] for n in raw['nodes']}
    sunk_groups = {node_groups[n] for n in installed} - {None}
    used_groups = {node_groups[n] for n in selected} - {None}
    cost = sum((Decimal(n['cost']) for n in raw['nodes'] if n['id'] in selected - installed), Decimal(0))
    cost += sum((Decimal(g['cost']) for g in raw['groups'] if g['id'] in used_groups - sunk_groups), Decimal(0))
    nets = []
    for name in ('expected', 'cautious'):
        benefit = sum((Decimal(e[name]) for e in raw['relationships']
                       if {e['a'], e['b']} <= selected and not {e['a'], e['b']} <= installed), Decimal(0))
        nets.append(benefit - cost)
    return cost, tuple(nets)


def test_complementary_bundle_beats_every_singleton_and_cautious_plan_abstains():
    raw = scenario(); result = plan(raw)
    assert result['state'] == 'planned'
    expected, cautious = result['plans']['expected'], result['plans']['cautious']
    assert expected['new_ids'] == ['a', 'b', 'c', 'r']
    assert expected['incremental_cost'] == '12.00' and expected['expected_benefit'] == '18.00'
    assert expected['expected_net'] == expected['objective'] == '6.00'
    assert expected['cautious_net'] == '-6.00'
    assert cautious['state'] == 'abstain' and cautious['new_ids'] == [] and cautious['objective'] == '0.00'
    assert all(oracle(raw, {n['id']})[1][0] < 0 for n in raw['nodes'])
    assert result['saved'] is False and result['realized'] is False and result['estimate'] is True
    raw['budget'] = '7'
    assert plan(raw)['state'] == 'abstain'


def test_installed_baseline_shared_setup_and_unused_group_unknowns_cancel():
    raw = scenario()
    raw['nodes'][0].update(installed=True, group_id='adapter', cost=None)
    raw['nodes'][1]['group_id'] = 'adapter'
    raw['nodes'][-1].update(installed=True, cost=None)
    raw['groups'] = [{'id': 'adapter', 'label': 'Shared adapter', 'cost': None},
                     {'id': 'unused', 'label': 'Unused group', 'cost': None}]
    raw['relationships'][0].update(expected=None, cautious=None)
    result = plan(raw)
    assert result['missing'] == []
    expected = result['plans']['expected']
    assert expected['installed_ids'] == ['a', 'r'] and expected['new_ids'] == ['b', 'c']
    assert expected['incremental_cost'] == '4.00' and expected['expected_benefit'] == '12.00'
    assert expected['expected_net'] == '8.00'
    raw['nodes'][0]['installed'] = False
    assert {m['field'] for m in plan(raw)['missing']} == {
        'nodes[0].cost', 'groups[0].cost', 'relationships[0].expected', 'relationships[0].cautious'}


def test_shared_group_is_charged_once():
    raw = scenario()
    for node in raw['nodes']:
        node['group_id'] = 'shared'
    raw['groups'] = [{'id': 'shared', 'label': 'Receiver adapter', 'cost': '3'}]
    raw['budget'] = '15'
    got = plan(raw)['plans']['expected']
    assert got['new_ids'] == ['a', 'b', 'c', 'r'] and got['incremental_cost'] == '15.00'
    assert got['expected_net'] == '3.00'


def test_cautious_minimum_is_taken_after_aggregating_scenarios():
    raw = scenario(); raw['nodes'] = raw['nodes'][:2] + raw['nodes'][-1:]
    raw['relationships'] = [{'a': 'a', 'b': 'r', 'expected': '20', 'cautious': '0'},
                            {'a': 'b', 'b': 'r', 'expected': '0', 'cautious': '20'}]
    got = plan(raw)['plans']['cautious']
    assert got['new_ids'] == ['a', 'b', 'r']
    assert got['expected_net'] == got['cautious_net'] == got['objective'] == '10.00'


def test_unknown_is_incomplete_but_explicit_zero_is_a_price():
    raw = scenario(); raw['budget'] = None; raw['relationships'][0]['expected'] = None
    result = plan(raw)
    assert result['state'] == 'incomplete' and result['plans'] is None
    assert [m['field'] for m in result['missing']] == ['budget', 'relationships[0].expected']
    raw['budget'] = '0'; raw['relationships'][0]['expected'] = '0'
    assert plan(raw)['state'] == 'abstain'


def test_exact_arithmetic_and_deterministic_ties_ignore_input_order():
    raw = {'currency': 'EUR', 'horizon': 'One year', 'perspective': 'Our costs', 'budget': '0.300001',
           'nodes': [{'id': n, 'label': n, 'cost': c, 'installed': i, 'group_id': None}
                     for n, c, i in [('r', None, True), ('a', '0.300001', False), ('b', '0.300001', False)]],
           'groups': [], 'relationships': [{'a': n, 'b': 'r', 'expected': '0.300002', 'cautious': '0.300002'}
                                            for n in ('a', 'b')]}
    result = plan(raw)
    assert result['plans']['expected']['new_ids'] == ['a']
    assert result['plans']['cautious']['objective'] == '0.000001'
    raw['nodes'].reverse(); raw['relationships'].reverse()
    assert plan(raw) == result


@pytest.mark.parametrize('bad', [True, 1, 0.1, '-1', 'NaN', 'Infinity', '1e3', '0.0000001',
                                '1000000000000.000001', '9' * 2000, ' 1', '1 '])
def test_money_is_strict_bounded_decimal_text(bad):
    raw = scenario(); raw['budget'] = bad
    with pytest.raises(ValidationError):
        PortfolioInput.model_validate(raw)


@pytest.mark.parametrize('change', [
    lambda x: x.update(currency='usd'), lambda x: x.update(horizon=''),
    lambda x: x.update(perspective='x' * 201), lambda x: x.update(unexpected=True),
    lambda x: x['nodes'][0].update(id='Bad ID'), lambda x: x['nodes'][0].update(label='x' * 101),
    lambda x: x['nodes'][0].update(installed='false'), lambda x: x['nodes'][0].update(currency='EUR'),
    lambda x: x['nodes'][0].update(group_id='missing'),
    lambda x: x['nodes'].append(deepcopy(x['nodes'][0])),
    lambda x: x['relationships'][0].update(a='missing'), lambda x: x['relationships'][0].update(b='a'),
    lambda x: x['relationships'].append({'a': 'r', 'b': 'a', 'expected': '1', 'cautious': '1'}),
    lambda x: x['groups'].extend([{'id': 'g', 'label': 'g', 'cost': '1'}] * 2),
    lambda x: x['nodes'].extend([{'id': f'n{i}', 'label': 'N', 'cost': '1', 'installed': False, 'group_id': None}
                                  for i in range(7)]),
])
def test_invalid_or_ambiguous_models_are_refused(change):
    raw = scenario(); change(raw)
    with pytest.raises(ValidationError):
        PortfolioInput.model_validate(raw)


def test_random_graphs_match_independent_complete_objectives_and_returned_plans():
    rng = random.Random(2026100945)
    for _ in range(200):
        ids = [f'n{i}' for i in range(rng.randint(1, 10))]
        raw = {'currency': 'USD', 'horizon': 'Synthetic year', 'perspective': 'Synthetic joint cost',
               'budget': str(rng.randint(0, 60)),
               'groups': [{'id': 'g0', 'label': 'Shared setup', 'cost': str(rng.randint(0, 10))}],
               'nodes': [{'id': n, 'label': n, 'cost': str(rng.randint(0, 10)),
                          'installed': rng.random() < .2, 'group_id': 'g0' if rng.random() < .5 else None}
                         for n in ids],
               'relationships': [{'a': a, 'b': b, 'expected': str(rng.randint(-10, 25)),
                                  'cautious': str(rng.randint(-10, 25))}
                                 for a, b in combinations(ids, 2) if rng.random() < .3]}
        installed = {n['id'] for n in raw['nodes'] if n['installed']}
        options = [n for n in ids if n not in installed]
        candidates = []
        for mask in range(1 << len(options)):
            selected = installed | {n for i, n in enumerate(options) if mask & (1 << i)}
            cost, nets = oracle(raw, selected)
            if cost <= Decimal(raw['budget']):
                candidates.append((cost, nets, selected))
        result = plan(raw)
        for name in ('expected', 'cautious'):
            ranked = sorted(candidates, key=lambda c: (-(c[1][0] if name == 'expected' else min(c[1])),
                                                       c[0], len(c[2] - installed), tuple(sorted(c[2] - installed))))
            cost, nets, selected = ranked[0]
            got = result['plans'][name]
            objective = nets[0] if name == 'expected' else min(nets)
            assert Decimal(got['objective']) == objective
            assert Decimal(got['incremental_cost']) == cost
            assert set(got['selected_ids']) == selected
            assert oracle(raw, set(got['selected_ids'])) == (cost, nets)
            assert Decimal(got['expected_net']) == nets[0] and Decimal(got['cautious_net']) == nets[1]


def test_ties_do_not_add_free_irrelevant_items():
    raw = scenario()
    raw['nodes'].append({'id': 'aa', 'label': 'Unused free item', 'cost': '0', 'installed': False, 'group_id': None})
    assert plan(raw)['plans']['expected']['new_ids'] == ['a', 'b', 'c', 'r']


def test_negative_relationship_benefits_mean_increased_running_costs():
    raw = scenario()
    for edge in raw['relationships']:
        edge['cautious'] = '-10.000001'
    result = plan(raw)
    assert result['plans']['expected']['cautious_benefit'] == '-30.000003'
    assert result['plans']['expected']['cautious_net'] == '-42.000003'
    assert result['plans']['cautious']['state'] == 'abstain'
    raw['relationships'][0]['expected'] = '-1000000000000.000001'
    with pytest.raises(ValidationError):
        PortfolioInput.model_validate(raw)


def test_maximum_amount_and_signed_zero_are_exact():
    raw = scenario(); raw['budget'] = '1000000000000.000000'
    raw['nodes'][0]['cost'] = '1000000000000.000000'
    raw['relationships'][0]['expected'] = '-1000000000000.000000'
    raw['relationships'][0]['cautious'] = '-0.000000'
    parsed = PortfolioInput.model_validate(raw)
    assert parsed.budget == '1000000000000.00'
    assert parsed.relationships[0].expected == '-1000000000000.00'
    assert parsed.relationships[0].cautious == '0.00'
    assert 'a' not in plan(parsed)['plans']['expected']['new_ids']
