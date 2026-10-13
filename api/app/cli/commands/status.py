"""faxbot status: the live outbound board, as Overview and Sent show it (GET /outbound/now).

What is sending now, what waits and why, what needs you, how today went, how full your lines are, whether
Faxbot switched new calls on your phone line to audio, and how each big send is going, in that order. With a
fax ID it shows where that one sent fax is now, as it always did.
"""
import time

import typer

from .. import state
from ..errors import CliError
from ..output import local_time
from . import fax

# How often --watch shows the board again: the console's own poll.
WATCH_SECONDS = 5
SECTION_NAMES = {'sending': 'sending now', 'waiting': 'waiting', 'needs_you': 'needs you', 'today': 'today',
                 'lines': 'lines', 'engine': 'fax engine', 'groups': 'big sends'}
# The command for each thing a person may do about a fax that needs them.
ACTIONS = {
    'approve': 'faxbot faxes sent approve {id}',
    'send_by_account': 'faxbot faxes sent approve {id} --account KEY',
    'check_again': 'faxbot faxes sent check-again {id}',
    'refuse': 'faxbot faxes sent refuse {id} --reason "..."',
    'settle': 'faxbot faxes sent settle {id} --delivered | --not-delivered | --cant-tell --reason "..."',
}


def _plural(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _fax(item):
    """'*******0123 (Riverside Clinic), 2 pages'."""
    who = f"{item.get('to') or 'Unknown number'}"
    if item.get('recipient'):
        who += f" ({item['recipient']})"
    if item.get('pages'):
        who += f", {_plural(item['pages'], 'page')}"
    return who


def board_lines(board):
    """The board as lines of plain text, in the contract's order."""
    found = [f"As of {local_time(board.get('as_of'))}"]
    sending = board.get('sending') or {}
    found.append(f"Sending now: {sending.get('count', 0)}")
    for item in sending.get('items') or []:
        how = f"by {item.get('route') or 'an unknown route'}"
        if item.get('engine'):
            how += f" with the {item['engine']}"
        if item.get('page') and item.get('pages'):
            how += f", page {item['page']} of {item['pages']}"
        if item.get('started_at'):
            how += f", started {local_time(item['started_at'])}"
        found.append(f'  {_fax(item)}: {how}')

    waiting = board.get('waiting') or {}
    found.append(f"Waiting: {waiting.get('count', 0)}")
    for reason in waiting.get('reasons') or []:
        if reason.get('count'):
            found.append(f"  {reason['count']}  {reason['sentence']}")
    if waiting.get('more'):
        found.append(f"  {waiting['more']}  Further back in the queue; Faxbot works out why once they move up.")
    if waiting.get('items'):
        found.append('  First in the queue:')
        for item in waiting['items']:
            found.append(f"    {_fax(item)}: {item.get('sentence') or ''} Waiting since "
                         f"{local_time(item.get('since'))}.")

    needs = board.get('needs_you') or {}
    found.append(f"Needs you: {needs.get('count', 0)}")
    for item in needs.get('items') or []:
        when = f" Since {local_time(item.get('since'))}."
        if item.get('until'):
            when += f" Goes out at {local_time(item['until'])}."
        found.append(f"  {_fax(item)}: {item.get('sentence') or ''}{when}")
        commands = [ACTIONS[action].format(id=item['id']) for action in item.get('actions') or []
                    if action in ACTIONS] or [f"faxbot faxes sent show {item['id']}"]
        found.extend(f'    {command}' for command in commands)

    today = board.get('today') or {}
    found.append(f"Today, since {local_time(today.get('since'))}: {today.get('accepted', 0)} accepted, "
                 f"{today.get('delivered', 0)} delivered, {today.get('failed', 0)} failed")
    for failure in today.get('failures') or []:
        found.append(f"  {failure['label']}: {failure['count']}")

    lines = board.get('lines') or []
    if lines:
        found.append('Lines')
        found.extend(f"  {line['label']}: {line['sentence']}" for line in lines)
    engine = board.get('engine')
    if engine:
        found.append('Fax engine')
        found.append(f"  {engine['sentence']}")
        if engine.get('fix'):
            found.append(f"  {engine['fix']}")
    groups = board.get('groups') or []
    if groups:
        found.append('Big sends today')
        for group in groups:
            found.append(f"  {group['who']}: {_plural(group['total'], 'fax', 'faxes')}: {group['delivered']} "
                         f"delivered, {group['sending']} sending, {group['waiting']} waiting, {group['needs_you']} "
                         f"{'needs' if group['needs_you'] == 1 else 'need'} you, {group['failed']} failed")
    missing = [SECTION_NAMES.get(name, name) for name in board.get('not_checked') or []]
    if missing:
        found.append(f"Not checked just now: {', '.join(missing)}. Faxbot tries again at the next look.")
    return found


def status(fax_id: str = typer.Argument(None, metavar='[FAX_ID]',
                                        help='A sent fax: show where that fax is now. Leave it out to see every fax '
                                             'that is sending or waiting.'),
           watch: bool = typer.Option(False, '--watch', help='Show the board again every 5 seconds until you press '
                                                             'Ctrl-C.')):
    """Show what is sending now, what waits and why, what needs you, and how today went; or where one sent fax is."""
    if fax_id:
        if watch:
            raise CliError('--watch shows every fax. Leave out the fax ID, or leave out --watch.')
        return fax.status(fax_id)
    api, out = state.api(), state.out()

    def show():
        board = api.get('/outbound/now')
        out.result(board, lambda printer: [printer.line(line) for line in board_lines(board)])
    if not watch:
        show()
        return
    try:
        while True:
            if not out.json_mode and not out.quiet:
                out.console.clear()
            show()
            time.sleep(WATCH_SECONDS)
    except KeyboardInterrupt:
        out.line()
