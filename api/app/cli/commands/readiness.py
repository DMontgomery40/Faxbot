"""Receiving readiness, receive owners and the UPS: ``faxbot system diagnostics receiving|power`` (brief 92, RF).

Hangs off the diagnostics group; ``route_families.py`` imports this module, so both register before ``nouns.py``
copies the group. Nothing here sends a fax.
"""
import typer

from .. import state
from ..errors import CliError
from .settings import diagnostics


receiving = typer.Typer(help='Whether each of your numbers can receive faxes now, and which receiver owns each '
                             'number.', no_args_is_help=True)
diagnostics.add_typer(receiving, name='receiving')
power = typer.Typer(help='The UPS Faxbot reads, so it holds long calls while the office runs on battery.',
                    no_args_is_help=True)
diagnostics.add_typer(power, name='power')

STATUS_WORDS = {'receiving': 'Receiving', 'attention': 'Needs attention', 'not_receiving': 'Not receiving',
                'off': 'Not in use', 'ok': 'Working'}


@receiving.command('check')
def receiving_check():
    """Check each number you receive on, from receiving evidence only. Sends nothing."""
    result = state.api().get('/receiving/readiness')

    def human(out):
        numbers = result.get('numbers') or []
        if not numbers:
            out.line('Faxbot receives on no number yet.' if result.get('receiving_on')
                     else 'Receiving faxes is off on this installation.')
        for item in numbers:
            out.line(f"{item['number']}: {STATUS_WORDS.get(item['status'], item['status'])}. {item['sentence']}")
            for check in item['checks']:
                out.line(f"  {check['title']}: {check['sentence']}")
    state.out().result(result, human)


@receiving.command('owner')
def receiving_owner(number: str = typer.Argument(..., metavar='NUMBER', help='One of your fax numbers.'),
                    account: str = typer.Option(None, '--account', metavar='KEY',
                                                help='The receiving account that takes its faxes, such as sip.'),
                    elsewhere: str = typer.Option(None, '--elsewhere', metavar='NAME',
                                                  help='A receiver outside this Faxbot, such as "the fax machine at '
                                                       'reception".'),
                    release: bool = typer.Option(False, '--release', help='Name no receiver for the number.'),
                    move: bool = typer.Option(False, '--move',
                                              help='Move the number from the receiver that has it now.')):
    """Name the one receiver of a number's faxes, so two places never take them without you knowing."""
    if sum(bool(item) for item in (account, elsewhere, release)) != 1:
        raise CliError('Give one of --account, --elsewhere or --release.')
    body = {'number': number.strip(), 'move': move}
    if account:
        body['owner'] = account.strip()
    elif elsewhere:
        body.update(owner='elsewhere', label=elsewhere.strip())
    else:
        body['owner'] = None
    result = state.api().post('/receiving/owners', json=body)
    state.out().result(result, lambda out: out.line(result['sentence']))


def _power_lines(out, result):
    out.line(f"{STATUS_WORDS.get(result['status'], result['status'])}. {result['sentence']}")
    if result.get('configured'):
        name = f" ({result['ups_name']})" if result.get('ups_name') else ''
        out.line(f"UPS server: {result['host']} port {result['port']}{name}. Reserve: {result['reserve_minutes']} "
                 'minutes.')


@power.command('show')
def power_show():
    """What the UPS says now and how Faxbot uses it."""
    result = state.api().get('/power')
    state.out().result(result, lambda out: _power_lines(out, result))


@power.command('set')
def power_set(address: str = typer.Option(..., '--address', metavar='HOST',
                                          help="The NUT server's address, such as 192.168.1.5."),
              port: int = typer.Option(3493, '--port', help="The NUT server's port; NUT uses 3493."),
              ups: str = typer.Option(None, '--ups', metavar='NAME',
                                      help='The UPS name on that server; the first it lists when left out.'),
              reserve_minutes: int = typer.Option(2, '--reserve-minutes',
                                                  help='Minutes of battery a call must leave to spare.')):
    """Read this UPS through NUT; Faxbot then holds a call that the battery could not see through."""
    result = state.api().put('/power', json={'host': address.strip(), 'port': port, 'ups_name': ups,
                                             'reserve_minutes': reserve_minutes})
    state.out().result(result, lambda out: _power_lines(out, result))


@power.command('off')
def power_off():
    """Stop reading the UPS; calls start without checking the battery."""
    result = state.api().put('/power', json={'host': ''})
    state.out().result(result, lambda out: _power_lines(out, result))
