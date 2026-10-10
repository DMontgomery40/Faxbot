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


move = typer.Typer(help='A checked plan for moving a number between your accounts. Faxbot places no port order.',
                   no_args_is_help=True)


def _show_move(result):
    def human(out):
        out.line(result.get('sentence') or '')
        for step in result.get('steps') or []:
            out.line(f"{step['label']}: {step['state_label']}")
            for evidence in step.get('evidence') or []:
                out.line('  ' + evidence)
        if result.get('accounts'):
            out.table(['Receiving account', 'Name'], [[row['key'], row['label']] for row in result['accounts']])
        if result.get('origins'):
            out.table(['Test route', 'Name'], [[row['key'], row['label']] for row in result['origins']])
        out.line(result.get('note') or '')
    state.out().result(result, human)


def numbers_advice(days: int = typer.Option(90, min=30, max=183)):
    """Read line advice with its history and unanswered dependency questions."""
    result = state.api().get('/routing/recommendations/lines', params={'days': days})
    def human(out):
        out.line(result.get('sentence') or '')
        for row in result.get('numbers') or []:
            out.line(row['sentence'])
            for reason in row.get('reasons') or []:
                out.line('  ' + reason)
            for answer in row.get('dependencies') or []:
                out.line(f"  {answer['question']}: {answer['label']} {answer['answer']}")
        out.line(result.get('note') or '')
    state.out().result(result, human)


def numbers_dependencies(number: str, question: str, answer: str, note: str = typer.Option('')):
    """Record a dependency: broadband, other_lines, emergency or printed; answer yes, no or unknown."""
    result = state.api().post('/routing/numbers/' + segment(number) + '/dependencies',
                              json={'question': question, 'answer': answer, 'note': note})
    state.out().result(result, lambda out: out.line('Dependency answer saved. Run faxbot numbers advice to see the updated advice.'))


@move.command('show')
def move_show(number: str):
    """Read the plan, account choices, evidence and steps still to do."""
    _show_move(state.api().get('/routing/numbers/' + segment(number) + '/move'))


@move.command('start')
def move_start(number: str, to_account: str = typer.Option(...)):
    """Start a checked move plan to another receiving account; this places no port order."""
    _show_move(state.api().post('/routing/numbers/' + segment(number) + '/move', json={'to_account': to_account}))


@move.command('record')
def move_record(number: str, step: str, status: str, note: str = typer.Option('')):
    """Record a step as done or not_done. Use step move with finished or abandoned to end the plan."""
    _show_move(state.api().post('/routing/numbers/' + segment(number) + '/move/steps/' + segment(step),
                               json={'state': status, 'note': note}))


@move.command('test')
def move_test(number: str, origin: str = typer.Option(...)):
    """Start watching for a receipt test from this route; send the test fax separately."""
    _show_move(state.api().post('/routing/numbers/' + segment(number) + '/move/tests', json={'origin': origin}))


@move.command('forget')
def move_forget(number: str):
    """Forget learned call properties from the old carrier and record this step in the plan."""
    _show_move(state.api().post('/routing/numbers/' + segment(number) + '/move/forget'))


# The line inventory and carrier lists (line_inventory.py) hang off the move group; importing them here registers them
# before nouns.py builds the command tree.
from . import line_inventory as _line_inventory  # noqa: E402,F401
