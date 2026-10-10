"""The station check and the answer cap (routing/stations.py) on the command line.

faxbot delivery identity station-check: what a mailbox's faxes do when a number answers as another fax machine.
Before any page, Faxbot's built-in engine compares the station a number answers as with the stations it
expects there. warn lets the fax go on and Sent says so; refuse hangs up before any page. A recipient's own
choice (faxbot recipients set --station-check) comes first.

faxbot delivery providers trunk answer-cap: whether Faxbot hangs up when no fax machine answers within 50 seconds, per
trunk, and why it does or does not use it there.
"""
import typer

from .. import resolve, state
from ..client import segment
from ..errors import CliError, EXIT_NOT_FOUND
from ..settings_write import write_settings


def station_check(mode: str = typer.Argument(None, metavar='[WARN|REFUSE]',
                                             help='warn: the fax goes on and Sent says so. refuse: Faxbot hangs up '
                                                  'before any page. Leave it out to see the current choice.'),
                  mailbox: str = typer.Option(None, '--mailbox',
                                              help='The mailbox whose faxes this is for. Leave it out to see every '
                                                   "mailbox's choice.")):
    """See or choose what a mailbox's faxes do when a number answers as another fax machine."""
    api = state.api()
    if mode is None:
        found = api.get('/routing/stations/mailboxes').get('mailboxes') or []
        if mailbox:
            wanted = resolve.mailbox(api, mailbox)
            found = [item for item in found if item['mailbox_id'] == wanted['id']]
        result = {'mailboxes': found}

        def lines(out):
            if not found:
                out.line('You have no mailboxes yet.')
            for item in found:
                out.line(item['sentence'])
            if found:
                out.line("A recipient's own choice comes first: faxbot recipients set NUMBER --station-check.")
        state.out().result(result, lines)
        return
    if mode.lower() not in ('warn', 'refuse'):
        raise CliError('Use warn or refuse.')
    if not mailbox:
        raise CliError('Name the mailbox with --mailbox.')
    found = resolve.mailbox(api, mailbox)
    result = api.put('/routing/stations/mailboxes/' + segment(found['id']), json={'mode': mode.lower()})
    state.out().result(result, lambda out: out.line(result.get('sentence') or ''))


def answer_cap(choice: str = typer.Argument(None, metavar='[on|off]',
                                            help='on: hang up when no fax machine answers within 50 seconds, where '
                                                 'the carrier bills by the minute. off: wait the usual 60 seconds. '
                                                 'Leave it out to see the setting.'),
               account: str = typer.Option(None, '--account', metavar='KEY',
                                           help="Which trunk, by its key from 'faxbot delivery providers accounts list'; the "
                                                'first trunk when left out.')):
    """See or change whether Faxbot hangs up when no fax machine answers within 50 seconds."""
    api = state.api()
    key = (account or '').strip() or None
    if choice is not None:
        if choice.lower() not in ('on', 'off'):
            raise CliError('Use on or off.')
        on = choice.lower() == 'on'
        if key and key != 'sip':
            from .accounts import find, load
            current = load(api)
            found = find(current, key)
            if found['provider'] != 'sip':
                raise CliError(f"{found['label']} is not a phone line (trunk). Choose a trunk's key.")
            key = found['key']
            api.patch('/admin/providers/accounts/' + segment(key),
                      json={'settings': {**(found.get('settings') or {}), 'answer_cap': on},
                            'expected_generation': current['generation']})
        else:
            write_settings(api, {'sip_fax_answer_cap': on})
    trunks = api.get('/routing/stations/answer-cap').get('trunks') or []
    if not trunks:
        raise CliError('No phone line (trunk) is set up yet. Set one up with faxbot delivery providers trunk use.',
                       EXIT_NOT_FOUND)
    chosen = [item for item in trunks if item['account'] == (key or 'sip')] or (trunks[:1] if not key else [])
    if not chosen:
        raise CliError(f"No trunk is called '{key}'. See 'faxbot delivery providers accounts list'.", EXIT_NOT_FOUND)
    trunk = chosen[0]
    state.out().result(trunk, lambda out: out.line(f"{trunk['label']}: {trunk['sentence']}"))
