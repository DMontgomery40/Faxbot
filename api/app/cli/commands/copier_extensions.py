"""Copiers as Faxbot's own extensions (``api/app/copier_extensions.py``). Its home is
``faxbot delivery providers trunk copier-extensions``, beside ``trunk copiers``."""
from __future__ import annotations

import typer

from .. import state
from ..client import segment
from ..errors import CliError

extensions = typer.Typer(help='Copiers that register to Faxbot as their own extension: their faxes go out through '
                              'your routes and rules, and faxes for their numbers go on to them.',
                         no_args_is_help=True)


def _list(api):
    return api.get('/admin/sip/copiers').get('extensions') or []


@extensions.command('list')
def extensions_list():
    """List your copier extensions and where their faxes go."""
    items = _list(state.api())
    state.out().result(items, lambda out: out.table(['Copier', 'Extension', 'Mailbox', 'Numbers', 'Faxes to it'], [
        [item['name'], item['extension'], item.get('mailbox') or '-', ', '.join(item['numbers']) or '-',
         'go on to it' if item['receive'] == 'copier' else 'stay in Received'] for item in items],
        empty='No copier extensions.'))


@extensions.command('add')
def extensions_add(name: str = typer.Argument(..., help='The copier, such as "Front desk Ricoh".'),
                   extension: str = typer.Argument(..., help='Its extension, 3 to 6 digits, such as 601.'),
                   mailbox: str = typer.Option(None, '--mailbox', help='The mailbox its faxes are sent from, by ID.'),
                   number: list[str] = typer.Option(None, '--number', help='A fax number whose faxes go on to it; '
                                                    'repeat it.'),
                   receive: str = typer.Option('copier', '--receive', metavar='copier|intake',
                                               help='copier: faxes to its numbers go on to it; intake: they stay in '
                                                    'Received.')):
    """Add a copier extension. Its password is printed once: enter it on the copier, then press Apply on the trunk
    page (faxbot delivery providers trunk apply) so the fax engine loads it."""
    if receive not in ('copier', 'intake'):
        raise typer.BadParameter('Use copier or intake.', param_hint='--receive')
    created = state.api().post('/admin/sip/copiers/extensions', json={
        'name': name, 'extension': extension, 'mailbox_id': mailbox, 'numbers': list(number or ()),
        'receive': receive})

    def show(out):
        out.line(created['sentence'])
        out.line(f"User name: {created['username']}")
        out.line(f"Password: {created['password']}")
        out.line(f"Realm: {created['realm']}")
        out.line(created['password_text'])
        out.line(created['apply_text'])
    state.out().result(created, show)


@extensions.command('remove')
def extensions_remove(copier: str = typer.Argument(..., help='The copier, by its extension, name or ID.')):
    """Take a copier extension away and revoke its sending key."""
    api = state.api()
    wanted = copier.strip().casefold()
    matches = [item for item in _list(api) if wanted in (item['id'], item['extension'], item['name'].casefold())]
    if len(matches) != 1:
        raise CliError(f"No single copier extension matches '{copier}'. Run faxbot delivery providers trunk "
                       'copier-extensions list.')
    result = api.post(f"/admin/sip/copiers/extensions/{segment(matches[0]['id'])}/remove")
    state.out().result(result, lambda out: out.line(result['detail']))


@extensions.command('received')
def extensions_received(fax_id: str = typer.Argument(..., help="A received fax's ID.")):
    """Say whether a received fax went on to its copier, or why it stays in Received."""
    result = state.api().get('/admin/sip/copiers/received/' + segment(fax_id))
    state.out().result(result, lambda out: out.line(result.get('sentence') or 'This fax was not for a copier.'))
