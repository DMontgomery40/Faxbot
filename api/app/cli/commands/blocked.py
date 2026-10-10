"""faxbot delivery blocked and faxbot received block: junk senders turned away before Faxbot answers.

The console's Numbers → Blocked senders and "Mark as junk" on a received fax. Reading needs
settings:read; blocking and unblocking need settings:write.
"""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

blocked = typer.Typer(help='Junk senders whose calls are turned away before Faxbot answers, and the calls turned away.',
                      no_args_is_help=True)

REASON = typer.Option(..., '--reason', help='Why this sender is junk, in a few words.')
DAYS = typer.Option(90, '--days', min=1, max=365, help='How many days to block the sender (90 unless you say).')


def _until(entry):
    if entry['active']:
        return local_time(entry['expires_at'])
    if entry['removed_at']:
        return f"Unblocked {local_time(entry['removed_at'])}"
    return f"Ended {local_time(entry['expires_at'])}"


@blocked.command('list')
def blocked_list(all_entries: bool = typer.Option(False, '--all', help='Also list senders no longer blocked.')):
    """List blocked senders and the calls turned away."""
    data = state.api().get('/screening')
    entries = [entry for entry in data['entries'] if all_entries or entry['active']]

    def human(out):
        out.line(data['sentence'])
        out.table(['Number', 'Why', 'Blocked by', 'Until', 'Calls turned away'],
                  [[entry['number'], entry['reason'], entry['added_by'] or 'An integration key', _until(entry),
                    entry['rejected_calls']] for entry in entries], empty='No sender is blocked.')
        out.table(['When', 'From', 'To your number'],
                  [[local_time(call['rejected_at']), call['number'], call['called'] or '-']
                   for call in data['rejections'][:20]], title='Calls turned away', empty='No calls turned away yet.')
    state.out().result({**data, 'entries': entries}, human)


@blocked.command('add')
def blocked_add(number: str = typer.Argument(..., help="The sender's fax number."), reason: str = REASON,
                days: int = DAYS):
    """Block a sender: their calls are turned away before Faxbot answers."""
    result = state.api().post('/screening/senders', json={'number': number, 'reason': reason, 'days': days})
    entry = result['entry']
    state.out().result(result, lambda out: out.line(
        f"Calls from {entry['number']} are turned away until {local_time(entry['expires_at'])}."))


@blocked.command('remove')
def blocked_remove(number: str = typer.Argument(..., help='The blocked number (or its entry ID, from --json).')):
    """Unblock a sender. The entry stays in the history, marked as removed by you."""
    api = state.api()
    data = api.get('/screening')
    digits = ''.join(char for char in number if char.isdigit())
    matches = [entry for entry in data['entries'] if entry['active'] and (
        entry['id'] == number or (digits and ''.join(c for c in entry['number'] if c.isdigit()).endswith(digits)))]
    if not matches:
        raise CliError(f'{number} is not blocked. See \'faxbot delivery blocked list\'.')
    results = [api.delete('/screening/senders/' + segment(entry['id'])) for entry in matches]
    state.out().result(results, lambda out: out.line(f"{matches[0]['number']} is no longer blocked."))


def received_block(inbound_id: str = typer.Argument(..., help='The received fax, by its ID.'), reason: str = REASON,
                   days: int = DAYS):
    """Mark a received fax's sender as junk: their calls are turned away before Faxbot answers."""
    result = state.api().post('/screening/senders', json={'inbound_id': inbound_id, 'reason': reason, 'days': days})
    entry = result['entry']
    state.out().result(result, lambda out: out.line(
        f"Calls from {entry['number']} are turned away until {local_time(entry['expires_at'])}."))
