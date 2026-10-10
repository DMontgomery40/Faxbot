"""Public test lines: ``faxbot admin diagnostics test-lines`` (test_lines.py, research N10).

Hangs off the diagnostics group (``settings.diagnostics``); ``settings.py`` imports this module so the commands
exist before ``nouns.py`` copies the group. Only ``send`` places a call, and only to the one line you name.
Faxbot never sends a test fax by itself.
"""
from datetime import datetime

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from .settings import diagnostics


test_lines = typer.Typer(help='Send a test fax to a public test line whose operator invites test faxes, and see '
                              'what came back.', no_args_is_help=True)
diagnostics.add_typer(test_lines, name='test-lines')

FAX_WORDS = {'on_its_way': 'On its way', 'held': 'Waiting in Sent', 'sent': 'Went through', 'failed': 'Failed',
             'uncertain': 'Not confirmed', 'cancelled': 'Cancelled'}


def _day(text):
    try:
        day = datetime.strptime(text, '%Y-%m-%d')
    except (TypeError, ValueError):
        return text
    return f'{day.day} {day:%B %Y}'


def _send_lines(out, item):
    out.line(f"{item['operator']} ({item['number']}), sent {item['sent_at_text']}"
             f"{' by ' + item['actor_name'] if item.get('actor_name') else ''}: "
             f"{FAX_WORDS.get(item['fax_state'], item['fax_state'])}. {item['fax_sentence']}")
    reply = item.get('reply') or {}
    if reply.get('sentence'):
        out.line(f"  {reply['sentence']}")
    for found in reply.get('candidates') or []:
        out.line(f"  Received {found['received_at_text']} from {found['from_number'] or 'an unknown number'}: "
                 f"faxbot admin diagnostics test-lines reply {item['id'][:8]} {found['inbound_id']}")
    if item.get('public_sentence'):
        out.line(f"  {item['public_sentence']} {item.get('public_page') or ''}".rstrip())
    if item.get('receipt') == 'faxbeep':
        out.line(f"  To find it on Faxbeep: faxbot admin diagnostics test-lines receipt {item['id'][:8]}")


def _find_send(api, prefix):
    sends = api.get('/diagnostics/test-lines').get('sends') or []
    prefix = (prefix or '').strip().lower()
    found = [item for item in sends if prefix and item['id'].startswith(prefix)]
    if len(found) != 1:
        raise CliError(f'No single recent test fax starts with "{prefix}". Run faxbot admin diagnostics test-lines '
                       'list to see them.')
    return found[0]


@test_lines.command('list')
def test_lines_list():
    """The public test lines, whether Faxbot may dial each, whether a reply reaches Faxbot, and recent tests."""
    result = state.api().get('/diagnostics/test-lines')

    def human(out):
        if result.get('reply'):
            out.line(result['reply']['sentence'])
        out.table(['Line', 'Number', 'What it does', 'Faxbot may dial it'],
                  [[line['id'], line['number'], f"{line['operator']}: {line['shows']}",
                    'Yes' if line['guard']['allowed'] else f"No: {line['guard']['sentence']}"]
                   for line in result.get('lines') or []])
        for line in result.get('lines') or []:
            out.line(f"{line['id']}: invitation at {line['source_url']}, read {_day(line['read_on'])}."
                     + (f" {line['note']}" if line.get('note') else ''))
        sends = result.get('sends') or []
        if sends:
            out.line('')
            out.line('Recent test faxes:')
        for item in sends:
            _send_lines(out, item)
    state.out().result(result, human)


@test_lines.command('send')
def test_lines_send(line: str = typer.Argument(..., metavar='LINE',
                                               help='The line, such as faxbeep-us (see faxbot admin diagnostics '
                                                    'test-lines list).'),
                    allow_country: bool = typer.Option(False, '--allow-country',
                                                       help='If Faxbot may not dial this country yet, allow it from '
                                                            'now on without asking.'),
                    yes: bool = typer.Option(False, '--yes', help='Do not ask before sending to a public page.')):
    """Send one test fax to a public test line, now. Faxbot never sends one by itself."""
    api = state.api()
    lines = {item['id']: item for item in api.get('/diagnostics/test-lines').get('lines') or []}
    chosen = lines.get(line.strip())
    if chosen is None:
        raise CliError(f'There is no test line called {line}. Run faxbot admin diagnostics test-lines list.')
    if chosen['kind'] == 'public' and not yes:
        typer.confirm(f"{chosen['operator']} shows every fax it receives on a public web page, including the line "
                      'at the top of each page with your organization\'s name and reply number. Faxbot sends only '
                      'its own test page. Send it?', abort=True)
    result = api.post(f'/diagnostics/test-lines/{segment(chosen["id"])}/send')
    needs = result.get('needs_allow')
    if not result.get('sent') and needs:
        if needs.get('fenced') or not needs.get('class'):
            raise CliError(result['sentence'])
        if not allow_country:
            typer.confirm(f"{result['sentence']} Allow Faxbot to dial {needs['class_text']} from now on?", abort=True)
        allowed = api.put('/routing/dialing/' + segment(needs['class']), json={'state': 'allowed'})
        state.out().line(allowed.get('sentence') or '')
        result = api.post(f'/diagnostics/test-lines/{segment(chosen["id"])}/send')
    if not result.get('sent'):
        raise CliError(result.get('sentence') or 'The test fax was not sent.')
    state.out().result(result, lambda out: out.line(result['sentence']))


@test_lines.command('replies')
def test_lines_replies():
    """Received faxes that are replies to test faxes, as Received labels them."""
    result = state.api().get('/diagnostics/test-lines/replies')
    state.out().result(result, lambda out: out.table(
        ['Received fax', 'What it is'], [[item['inbound_id'], item['sentence']] for item in result.get('replies') or []],
        empty='No replies to test faxes yet.'))


@test_lines.command('show')
def test_lines_show(test: str = typer.Argument(..., metavar='TEST',
                                               help='The start of a recent test fax ID (see test-lines list).')):
    """One test fax's result and what came back."""
    api = state.api()
    found = _find_send(api, test)
    result = api.get(f"/diagnostics/test-lines/sends/{segment(found['id'])}")
    state.out().result(result, lambda out: _send_lines(out, result))


@test_lines.command('receipt')
def test_lines_receipt(test: str = typer.Argument(..., metavar='TEST',
                                                  help='The start of a recent Faxbeep test fax ID.')):
    """Look up a Faxbeep test fax on Faxbeep's public page now."""
    api = state.api()
    found = _find_send(api, test)
    result = api.get(f"/diagnostics/test-lines/sends/{segment(found['id'])}/receipt")

    def human(out):
        out.line(result['sentence'])
        if result.get('url') or result.get('list_url'):
            out.line(result.get('url') or result.get('list_url'))
    state.out().result(result, human)


@test_lines.command('reply')
def test_lines_reply(test: str = typer.Argument(..., metavar='TEST', help='The start of a recent test fax ID.'),
                     received: str = typer.Argument(..., metavar='RECEIVED',
                                                    help='The received fax that is the reply, as test-lines show '
                                                         'offers it.')):
    """Mark a received fax as a test fax's reply."""
    api = state.api()
    found = _find_send(api, test)
    result = api.post(f"/diagnostics/test-lines/sends/{segment(found['id'])}/reply",
                      json={'inbound_id': received.strip()})
    state.out().result(result, lambda out: _send_lines(out, result))
