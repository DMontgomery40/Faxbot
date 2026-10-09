"""Read-only setup portfolios from explicit scenario files; no automatic enrollment."""
import json
from pathlib import Path
import sys

import typer

from .. import state
from ..errors import CliError, EXIT_REJECTED


MAX_INPUT_BYTES = 65_536


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('A scenario repeats a field.')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('A scenario contains an invalid number.')


def _read(file):
    try:
        if file == '-':
            raw = sys.stdin.read(MAX_INPUT_BYTES + 1).encode('utf-8')
        else:
            with Path(file).open('rb') as stream:
                raw = stream.read(MAX_INPUT_BYTES + 1)
    except (OSError, UnicodeError):
        raise CliError(f'Cannot read {file}.') from None
    if len(raw) > MAX_INPUT_BYTES:
        raise CliError('The scenario must be at most 64 KiB.', EXIT_REJECTED)
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, UnicodeError):
        raise CliError('Use a UTF-8 JSON scenario without duplicate fields or invalid numbers.', EXIT_REJECTED) from None
    if not isinstance(result, dict):
        raise CliError('The scenario must be a JSON object.', EXIT_REJECTED)
    return result


def _show(out, result, labels):
    out.line(f"{result['horizon']} — {result['perspective']} ({result['currency']})")
    if result['state'] == 'incomplete':
        out.line('Enter the missing assumptions before comparing plans:')
        for missing in result['missing']:
            out.line('  ' + missing['reason'])
    else:
        for key, title in (('expected', 'Expected plan'), ('cautious', 'Cautious plan')):
            chosen = result['plans'][key]
            out.line(title + ':')
            if chosen['state'] == 'abstain':
                out.line('  Keep what is installed; no additional spending is worthwhile in this scenario.')
            else:
                out.line('  Add: ' + ', '.join(labels.get(identity, identity) for identity in chosen['new_ids']))
            for name, label in (('incremental_cost', 'Additional setup cost'),
                                ('expected_net', 'Expected net benefit'), ('cautious_net', 'Cautious net benefit')):
                out.line(f"  {label}: {chosen[name]} {result['currency']}")
    out.line(result['note'])
    for assumption in result['assumptions']:
        out.line(assumption)


def portfolio(file: str = typer.Option(..., '--file', help='UTF-8 JSON scenario file, or - for standard input; at most 64 KiB.')):
    """Compare setup bundles under an explicit budget and expected/cautious assumptions. Costs, currency, period
    and whose amounts count come from the file. Inputs are not saved and no partner is enrolled or route changed.

    The JSON object needs currency (for example USD), horizon (the period), perspective (whose amounts count),
    budget, nodes, groups and relationships. A node has id, label, cost, installed (true/false) and group_id
    (or null). A shared group has id, label and cost. A relationship has a and b (the two node IDs), expected
    and cautious benefits. Use empty lists for no groups or relationships, and stable lowercase IDs.

    Write every amount as quoted decimal text with up to six decimal places, or null for unknown. Costs and
    budget are nonnegative; benefits are running-cost reductions over the period, excluding setup, and can
    be negative for higher running costs. Use one currency throughout, at most ten nodes and ten groups,
    and one relationship per pair. Amount magnitudes cannot exceed 1,000,000,000,000."""
    body = _read(file)
    result = state.api().post('/routing/portfolio/plan', json=body)
    labels = {node['id']: node['label'] for node in body['nodes']}
    state.out().result(result, lambda out: _show(out, result, labels))
