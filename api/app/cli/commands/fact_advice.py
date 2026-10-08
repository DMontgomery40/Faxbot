"""Advice on missing facts, quiet lines and moving a number, on the command line.

``faxbot costs advice`` (what one missing fact cost you), ``faxbot numbers advice`` (keep, move the termination,
investigate or can likely go, with the evidence and the questions only you can answer) and ``faxbot numbers move``
(a number's move as a checked plan). Everything here advises or records what you did; Faxbot never ports, cancels,
enrolls or changes a provider account.
"""
import typer

from .. import state
from ..client import segment
from ..output import money


# -- what a missing fact costs you ----------------------------------------------------------------------------------

def show_facts(out, result):
    out.line(result.get('sentence') or '')
    for item in result.get('recipients') or []:
        out.line('')
        title = f"{item['name']} ({item['display']})" if item.get('name') else item['display']
        out.line(title + (f" - up to {money(item['largest'])} less (estimate)" if item.get('largest') else ''))
        if item.get('sentence'):
            out.line(item['sentence'])
        for row in item.get('facts') or []:
            out.line(f"  {row['title']}: {row['sentence']}")
            out.line(f"    {row['confirm']}")
            out.line(f"    {row['step']}")
    out.line('')
    out.line(result.get('realized') or '')
    out.line(result.get('note') or '')


def costs_advice(days: int = typer.Option(90, '--days', min=7, max=183,
                                          help='How many days of sent faxes to price again.')):
    """Show what one missing fact cost you per recipient: a partner, a recipient's approval, a price or a plan's
    allowance, priced against the best route you may use, less what establishing it costs. Advice only."""
    result = state.api().get('/routing/recommendations/facts', params={'days': days})
    state.out().result(result, lambda out: show_facts(out, result))
