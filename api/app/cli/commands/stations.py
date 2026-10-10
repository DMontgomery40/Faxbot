"""faxbot numbers reply station-check: what a mailbox's faxes do when a number answers as another fax machine.

Before any page, Faxbot's built-in engine compares the station a number answers as with the stations it
expects there (routing/stations.py). warn lets the fax go on and Sent says so; refuse hangs up before any page.
A recipient's own choice (faxbot recipients set --station-check) comes first.
"""
import typer

from .. import resolve, state
from ..client import segment
from ..errors import CliError


def station_check(mode: str = typer.Argument(..., metavar='WARN|REFUSE',
                                             help='warn: the fax goes on and Sent says so. refuse: Faxbot hangs up '
                                                  'before any page.'),
                  mailbox: str = typer.Option(..., '--mailbox', help='The mailbox whose faxes this is for.')):
    """Choose what a mailbox's faxes do when a number answers as another fax machine."""
    if mode.lower() not in ('warn', 'refuse'):
        raise CliError('Use warn or refuse.')
    api = state.api()
    found = resolve.mailbox(api, mailbox)
    result = api.put('/routing/stations/mailboxes/' + segment(found['id']), json={'mode': mode.lower()})
    state.out().result(result, lambda out: out.line(result.get('sentence') or ''))
