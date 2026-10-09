"""Bounded, read-only setup portfolios from explicitly declared same-horizon assumptions.

At most ten setup items means complete enumeration costs at most 1,024 subsets.
Installed items and relationships are the baseline. The expected plan maximizes
incremental expected net benefit; the cautious plan maximizes the lower aggregate
net benefit across both scenarios. No account, enrollment or route is changed.
"""
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from .costs import MICROS, format_amount, parse_amount

MAX_AMOUNT = 10**12 * MICROS


def _amount(value):
    amount = parse_amount(value, whole_digits=13)
    if amount > MAX_AMOUNT:
        raise ValueError('An amount must not exceed 1,000,000,000,000 currency units.')
    return format_amount(amount)


def _benefit(value):
    amount = parse_amount(value.removeprefix('-'), whole_digits=13)
    if amount > MAX_AMOUNT:
        raise ValueError('A benefit magnitude must not exceed 1,000,000,000,000 currency units.')
    return format_amount(-amount if value.startswith('-') else amount)


def _text(value):
    if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Enter a name without control characters.')
    return value.strip()


Identity = Annotated[str, Field(min_length=1, max_length=32, pattern=r'^[a-z][a-z0-9_-]{0,31}$')]
Label = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(_text)]
Description = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_text)]
Amount = Annotated[str, Field(min_length=1, max_length=20, pattern=r'^[0-9]{1,13}(?:\.[0-9]{1,6})?$'),
                   AfterValidator(_amount)]
Benefit = Annotated[str, Field(min_length=1, max_length=21, pattern=r'^-?[0-9]{1,13}(?:\.[0-9]{1,6})?$'),
                    AfterValidator(_benefit)]


class _Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Node(_Input):
    id: Identity
    label: Label
    cost: Amount | None
    installed: bool
    group_id: Identity | None


class Group(_Input):
    id: Identity
    label: Label
    cost: Amount | None


class Relationship(_Input):
    a: Identity
    b: Identity
    expected: Benefit | None
    cautious: Benefit | None


class PortfolioInput(_Input):
    currency: Annotated[str, Field(min_length=3, max_length=3, pattern=r'^[A-Z]{3}$')]
    horizon: Description
    perspective: Description
    budget: Amount | None
    nodes: list[Node] = Field(min_length=1, max_length=10)
    groups: list[Group] = Field(max_length=10)
    relationships: list[Relationship] = Field(max_length=45)

    @model_validator(mode='after')
    def distinct_references(self):
        nodes = {node.id for node in self.nodes}
        groups = {group.id for group in self.groups}
        if len(nodes) != len(self.nodes) or len(groups) != len(self.groups):
            raise ValueError('Each setup item and each shared group needs its own stable ID.')
        if any(node.group_id is not None and node.group_id not in groups for node in self.nodes):
            raise ValueError('Every named shared group must be included in the scenario.')
        pairs = set()
        for relationship in self.relationships:
            pair = frozenset((relationship.a, relationship.b))
            if len(pair) != 2 or not pair <= nodes:
                raise ValueError('A relationship needs two different setup items in this scenario.')
            if pair in pairs:
                raise ValueError('Include each pair of setup items only once to avoid counting its benefit twice.')
            pairs.add(pair)
        return self


def _micros(value):
    amount = parse_amount(value.removeprefix('-'), whole_digits=13)
    return -amount if value.startswith('-') else amount


def _missing(request, installed, sunk_groups):
    missing = []

    def need(value, field, reason):
        if value is None:
            missing.append({'field': field, 'reason': reason})

    need(request.budget, 'budget', 'Enter the spending limit for this scenario; zero is a limit too.')
    used_groups = {node.group_id for node in request.nodes if node.group_id is not None}
    for i, node in enumerate(request.nodes):
        if node.id not in installed:
            need(node.cost, f'nodes[{i}].cost', f'Enter the setup cost for {node.label}.')
    for i, group in enumerate(request.groups):
        if group.id in used_groups - sunk_groups:
            need(group.cost, f'groups[{i}].cost', f'Enter the shared setup cost for {group.label}.')
    labels = {node.id: node.label for node in request.nodes}
    for i, relationship in enumerate(request.relationships):
        if {relationship.a, relationship.b} <= installed:
            continue  # Unknown baseline benefits cancel from every incremental alternative.
        for scenario in ('expected', 'cautious'):
            need(getattr(relationship, scenario), f'relationships[{i}].{scenario}',
                 f'Enter the {scenario} benefit for {labels[relationship.a]} and {labels[relationship.b]}.')
    return missing


def _view(selected, installed, cost, benefits, objective):
    new = sorted(selected - installed)
    return {'state': 'planned' if new else 'abstain', 'installed_ids': sorted(installed),
            'new_ids': new, 'selected_ids': sorted(selected), 'incremental_cost': format_amount(cost),
            'expected_benefit': format_amount(benefits[0]), 'cautious_benefit': format_amount(benefits[1]),
            'expected_net': format_amount(benefits[0] - cost),
            'cautious_net': format_amount(benefits[1] - cost), 'objective': format_amount(objective)}


def plan(raw: PortfolioInput | dict) -> dict:
    """Exact expected/cautious bundles, or the relevant unknown inputs preventing calculation."""
    request = raw if isinstance(raw, PortfolioInput) else PortfolioInput.model_validate(raw)
    installed = {node.id for node in request.nodes if node.installed}
    sunk_groups = {node.group_id for node in request.nodes if node.installed and node.group_id is not None}
    missing = _missing(request, installed, sunk_groups)
    result = {'state': 'incomplete', 'currency': request.currency, 'horizon': request.horizon,
              'perspective': request.perspective, 'budget': request.budget, 'missing': missing, 'plans': None,
              'estimate': True, 'realized': False, 'saved': False,
              'note': 'Inputs are not saved. This compares your assumptions; it does not enroll partners or change routes.',
              'assumptions': [
                  'Every amount uses the stated currency, time period and whose costs and benefits are counted.',
                  'Setup costs for installed items and their shared groups are already paid. Only additional costs count.',
                  'A relationship adds benefit only when both items are present. Already-active relationships add no new benefit.',
                  'Relationship benefits are net running-cost reductions over the period, excluding setup. Negative amounts mean higher running costs.',
                  'A shared setup cost is paid once. Count a payment between participants only once in your chosen perspective.',
                  'The cautious plan maximizes the lower total net benefit across the two scenarios.',
                  'These are modeled amounts from your inputs, not measured savings.']}
    if missing:
        return result
    budget = _micros(request.budget)
    choices = sorted((node for node in request.nodes if not node.installed), key=lambda node: node.id)
    costs = {node.id: _micros(node.cost) for node in choices}
    groups = {group.id: group for group in request.groups}
    edges = [(frozenset((edge.a, edge.b)), (_micros(edge.expected), _micros(edge.cautious)))
             for edge in request.relationships if not {edge.a, edge.b} <= installed]
    best = {}
    for mask in range(1 << len(choices)):
        added = [node for i, node in enumerate(choices) if mask & (1 << i)]
        new_ids = tuple(node.id for node in added)
        selected = installed | set(new_ids)
        new_groups = {node.group_id for node in added if node.group_id is not None} - sunk_groups
        cost = sum(costs[node.id] for node in added) + sum(_micros(groups[g].cost) for g in new_groups)
        if cost > budget:
            continue
        benefits = tuple(sum(values[i] for pair, values in edges if pair <= selected) for i in (0, 1))
        for name, objective in (('expected', benefits[0] - cost),
                                ('cautious', min(benefits) - cost)):
            # Baseline always has objective and cost zero, so nonpositive investments never displace it.
            rank = (-objective, cost, len(new_ids), new_ids)
            if name not in best or rank < best[name][0]:
                best[name] = (rank, _view(selected, installed, cost, benefits, objective))
    result['plans'] = {name: best[name][1] for name in ('expected', 'cautious')}
    result['state'] = 'planned' if any(p['new_ids'] for p in result['plans'].values()) else 'abstain'
    return result
