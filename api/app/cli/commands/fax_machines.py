"""faxbot recipients fax-machine and faxbot recipients iaf: what a number's fax machine said, what Faxbot learned
and changes for it, and Internet Aware Fax.

The console's Recipients → Details, "Their fax machine". Reading needs settings:read; forgetting what failed
with a number, and approving or removing a fax server for Internet Aware Fax, need settings:write.
"""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

iaf = typer.Typer(help='Internet Aware Fax to fax servers that receive over the internet, such as another '
                       'Faxbot or a Brooktrout SR140.', no_args_is_help=True)

KINDS = {'faxbot': 'peer', 'server': 'endpoint'}


def fax_machine(number: str = typer.Argument(..., help='A fax number you send to or receive from.'),
                forget: bool = typer.Option(False, '--forget', help='Forget that fax over IP or audio fax failed with '
                                                                   'this number, so its next calls use the usual '
                                                                   'settings.')):
    """What a number's fax machine said on recent calls, what Faxbot learned from them, and what it changes."""
    api = state.api()
    if forget:
        result = api.post('/fax-machines/numbers/' + segment(number) + '/forget')
        state.out().result(result, lambda out: out.line(result['sentence']))
        return
    data = api.get('/fax-machines/numbers/' + segment(number))
    directions = {'out': 'Sent', 'in': 'Received'}

    def human(out):
        out.line(data['sentence'])
        for call in data['calls'][:5]:
            what = ', '.join(part for part in (directions.get(call['direction']), call.get('mode_label'),
                                               call.get('engine_label')) if part)
            out.line(f"{local_time(call['when'])}: {what}." + (f" {call['outcome']}" if call.get('outcome') else ''))
            for text in call['sentences'] + call.get('changes', []):
                out.line('  ' + text)
        learned = data['learned']
        if learned['sentences']:
            out.line('What Faxbot changes for this number:')
            for text in learned['sentences']:
                out.line('  ' + text)
        for text in learned.get('notes', []) + ([learned['since']] if learned.get('since') else []):
            out.line(text)
        if data.get('can_forget'):
            out.line(f"To forget what failed with this number: faxbot recipients fax-machine {data['number']} --forget")
        if data['iaf']:
            out.line('Faxes to and from this number go as Internet Aware Fax.')
    state.out().result(data, human)


@iaf.command('list')
def iaf_list():
    """List the fax servers you approved for Internet Aware Fax, and partner offices marked for it."""
    data = state.api().get('/fax-machines/iaf')
    kinds = {'peer': 'Another Faxbot', 'endpoint': 'Fax server'}

    def human(out):
        out.table(['Number', 'Name', 'Kind', 'Approved by', 'Since'],
                  [[item['number'], item['label'], kinds[item['kind']], item['added_by'] or 'An integration key',
                    local_time(item['added_at'])] for item in data['servers']],
                  empty='No fax server is approved for Internet Aware Fax.')
        if data['partners']:
            out.line('Partner offices marked for Internet Aware Fax: ' + ', '.join(data['partners']) + '.')
    state.out().result(data, human)


@iaf.command('add')
def iaf_add(number: str = typer.Argument(..., help="The fax server's number."),
            kind: str = typer.Option(..., '--kind', help='faxbot (another Faxbot) or server (a fax server that takes '
                                                          'Internet Aware Fax, such as an SR140).'),
            name: str = typer.Option(..., '--name', help='A name you will recognise, such as "Head office SR140".')):
    """Send faxes to and from a fax server as Internet Aware Fax. Never for a fax machine on a phone line."""
    if kind not in KINDS:
        raise CliError("Choose --kind faxbot or --kind server.")
    result = state.api().post('/fax-machines/iaf', json={'number': number, 'kind': KINDS[kind], 'label': name})
    state.out().result(result, lambda out: out.line(
        f"Faxes to and from {result['server']['number']} now go as Internet Aware Fax."))


@iaf.command('remove')
def iaf_remove(number: str = typer.Argument(..., help='The approved number (or its ID, from --json).')):
    """Stop Internet Aware Fax for a fax server: its faxes go at fax line speed again."""
    api = state.api()
    servers = api.get('/fax-machines/iaf')['servers']
    digits = ''.join(char for char in number if char.isdigit())
    matches = [item for item in servers if item['id'] == number
               or (digits and ''.join(c for c in item['number'] if c.isdigit()).endswith(digits))]
    if not matches:
        raise CliError(f"{number} is not approved for Internet Aware Fax. See 'faxbot recipients iaf list'.")
    results = [api.delete('/fax-machines/iaf/' + segment(item['id'])) for item in matches]
    state.out().result(results, lambda out: out.line(
        f"Faxes to {matches[0]['number']} go at fax line speed again."))
