"""faxbot numbers forwarded-trust: the certificate authorities you trust for forwarded calls."""
from pathlib import Path
import sys

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

forwarded_trust = typer.Typer(help='Certificate authorities you trust to verify that a carrier forwarded a call '
                                   '(STIR/SHAKEN STI-CAs). A forwarding is verified only when it chains to one.',
                              no_args_is_help=True)


def _show(out, result):
    out.line(result['sentence'])
    if result['anchors']:
        out.table(['Certificate authority', 'Fingerprint', 'Valid until', 'From', 'Added'],
                  [[item['name'], item['short'], local_time(item['valid_until'], empty='-'), item['source'],
                    item['added_on']] for item in result['anchors']])
    else:
        out.line(result['note'])


@forwarded_trust.command('list')
def trust_list():
    """List the certificate authorities you trust for forwarded calls."""
    result = state.api().get('/admin/forwarded-trust')
    state.out().result(result, lambda out: _show(out, result))


@forwarded_trust.command('add')
def trust_add(file: str = typer.Argument(None, metavar='[FILE]',
                                         help="PEM certificates of the certificate authorities, or '-' for standard "
                                              'input.'),
              url: str = typer.Option(None, '--url', metavar='ADDRESS',
                                      help='Read the list from this https:// address instead, once, now (a list you '
                                           'can reach, such as one your carrier gives you).')):
    """Trust certificate authorities for forwarded calls: from a file of PEM certificates, or from a list at an
    address. Only certificate authorities are kept, each once."""
    if bool(file) == bool(url):
        raise CliError('Give a file of PEM certificates, or --url ADDRESS, but not both.')
    if file:
        try:
            pem = sys.stdin.read() if file == '-' else Path(file).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            raise CliError(f'Faxbot could not read {file}.') from None
        result = state.api().post('/admin/forwarded-trust', json={'pem': pem})
    else:
        result = state.api().post('/admin/forwarded-trust', json={'url': url})
    state.out().result(result, lambda out: _show(out, result))


@forwarded_trust.command('remove')
def trust_remove(fingerprint: str = typer.Argument(..., help='The start of its fingerprint, as the list shows it '
                                                              '(at least 8 characters).')):
    """Stop trusting one certificate authority for forwarded calls."""
    result = state.api().delete('/admin/forwarded-trust/' + segment(fingerprint))
    state.out().result(result, lambda out: _show(out, result))
