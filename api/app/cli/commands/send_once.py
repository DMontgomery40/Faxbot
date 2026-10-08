"""Send once: a partner's intake files one copy of your fax for each of its numbers, or your intake files theirs."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from .delivery import _peer


send_once = typer.Typer(help="Send once: a partner's intake files one copy of a fax for each of its numbers, so "
                             'the same document to several of them goes over the internet once.',
                        no_args_is_help=True)

ROLE = {'receiver': 'Your intake files theirs', 'sender': 'Their intake files yours'}


def _agreement(api, partner, *, role, states):
    peer = _peer(api, partner)
    items = [item for item in api.get('/direct/send-once')['agreements']
             if item['peer_id'] == peer['id'] and item['role'] == role and item['state'] in states]
    if not items:
        raise CliError(f"There is no matching send-once agreement with {peer['organization']}. "
                       "See 'faxbot recipients partners send-once list'.")
    return items[0]


def _show(out, item):
    out.line(item['summary'])
    out.line(item['status'])
    for place in item.get('placements') or []:
        where = place['mailbox'] or place['held']
        out.line(f"  {place['fax_number']}: {where}" if where else f"  {place['fax_number']}")


@send_once.command('list')
def send_once_list():
    """List send-once agreements both ways, and the bytes partners did not need sent again in the last 30 days."""
    result = state.api().get('/direct/send-once')
    items = result['agreements']

    def human(out):
        out.table(['Partner', 'Direction', 'State', 'Numbers', 'Status'],
                  [[item['partner'], ROLE[item['role']], item['state'], ', '.join(item['numbers']), item['status']]
                   for item in items],
                  empty="No send-once agreements. A partner offers one from its Faxbot, or you offer yours with "
                        "'faxbot recipients partners send-once offer'.")
        for item in items:
            if item.get('placements'):
                out.line()
                _show(out, item)
        if (result.get('bytes') or {}).get('sentence'):
            out.line()
            out.line(result['bytes']['sentence'])
    state.out().result(result, human)


@send_once.command('offer')
def send_once_offer(partner: str = typer.Argument(..., help='Partner organization, fax number or ID.'),
                    numbers: list[str] = typer.Argument(..., help='Your fax numbers that your intake files for, '
                                                                  'such as +15551234567 +15551234568.'),
                    intake: str = typer.Option('Central intake', '--intake',
                                               help='The name of your intake, as the partner sees it.')):
    """Let a partner send one copy to your intake, which files it for each of these numbers by your receiving rules."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/send-once", json={'numbers': numbers, 'intake': intake})

    def human(out):
        _show(out, result)
        out.line(result['detail'])
    state.out().result(result, human)


@send_once.command('accept')
def send_once_accept(partner: str = typer.Argument(..., help='Partner organization, fax number or ID.')):
    """Accept a partner's offer: your faxes to its numbers then go once to its intake, with no telephone call."""
    api = state.api()
    item = _agreement(api, partner, role='sender', states=('offered', 'accepting'))
    result = api.post(f"/direct/send-once/{segment(item['id'])}/accept")

    def human(out):
        _show(out, result)
        out.line(result['detail'])
    state.out().result(result, human)


@send_once.command('end')
def send_once_end(partner: str = typer.Argument(..., help='Partner organization, fax number or ID.'),
                  direction: str = typer.Option(None, '--direction', metavar='yours|theirs',
                                                help="Which agreement to end when there are both: 'yours' (their "
                                                     "intake files your faxes) or 'theirs' (yours files theirs).")):
    """End a send-once agreement with a partner at once; faxes to those numbers go by your usual routes again."""
    if direction not in (None, 'yours', 'theirs'):
        raise typer.BadParameter('Use yours or theirs.', param_hint='--direction')
    api = state.api()
    roles = {'yours': ('sender',), 'theirs': ('receiver',), None: ('sender', 'receiver')}[direction]
    peer = _peer(api, partner)
    items = [item for item in api.get('/direct/send-once')['agreements']
             if item['peer_id'] == peer['id'] and item['role'] in roles and item['state'] != 'withdrawn']
    if not items:
        raise CliError(f"There is no send-once agreement with {peer['organization']} to end.")
    if len(items) > 1:
        raise CliError("There is one agreement each way; add --direction yours or --direction theirs.")
    result = api.post(f"/direct/send-once/{segment(items[0]['id'])}/withdraw")
    state.out().result(result, lambda out: out.line(result['detail']))
