"""Fax servers that send through Faxbot (``api/app/fax_servers.py``), such as OpenText RightFax with a Brooktrout SR140.
Its home is ``faxbot delivery providers trunk fax-servers``, beside ``trunk copier-extensions``."""
from __future__ import annotations

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

servers = typer.Typer(help='Fax servers, such as RightFax, that send their faxes through Faxbot: Faxbot takes each '
                           'whole fax and sends it on by your routes and rules.',
                      no_args_is_help=True)


def _list(api):
    return api.get('/admin/sip/fax-servers')


def _one(api, reference):
    wanted = reference.strip().casefold()
    matches = [item for item in _list(api).get('servers') or []
               if wanted in (item['id'], item['address'], item['name'].casefold(), item['username'])]
    if len(matches) != 1:
        raise CliError(f"No single fax server matches '{reference}'. Run faxbot delivery providers trunk fax-servers "
                       'list.')
    return matches[0]


def _countries(value):
    return [code.strip().upper() for code in (value or '').replace(' ', ',').split(',') if code.strip()]


@servers.command('list')
def servers_list():
    """List your fax servers, where each may send, and its limits."""
    found = _list(state.api())
    items = found.get('servers') or []

    def show(out):
        out.table(['Fax server', 'Address', 'User name', 'Countries', 'Per hour', 'Per day', 'Told on failure'], [
            [item['name'], item['address'], item['username'], ', '.join(item['countries']), str(item['per_hour']),
             str(item['per_day']), ', '.join(value for value in (item.get('contact_email'),
                                                                   item.get('contact_webhook')) if value) or '-']
            for item in items], empty='No fax servers send through Faxbot.')
        if found.get('where_text'):
            out.line(found['where_text'])
    state.out().result(found, show)


@servers.command('add')
def servers_add(name: str = typer.Argument(..., help='The fax server, such as RightFax.'),
                address: str = typer.Argument(..., help="Its one address on your network, such as 192.168.1.40."),
                countries: str = typer.Option(None, '--countries', metavar='US,CA',
                                              help="Countries it may send to (two-letter codes). Faxbot's dialing "
                                                   'rules still apply. Default: your own country.'),
                per_hour: int = typer.Option(None, '--per-hour', help='Most faxes it may send in an hour (default 60).'),
                per_day: int = typer.Option(None, '--per-day', help='Most faxes it may send in a day (default 500).'),
                dial_prefix: str = typer.Option(None, '--dial-prefix', metavar='DIGITS',
                                                help='An outside-line prefix it puts before every number, such as 9; '
                                                     'Faxbot removes it.'),
                email: str = typer.Option(None, '--email', help='Who is told when one of its faxes is not delivered.'),
                webhook: str = typer.Option(None, '--webhook', metavar='URL',
                                            help='Your system that is told when one of its faxes is not delivered.'),
                mailbox: str = typer.Option(None, '--mailbox', help='The mailbox its faxes are sent from, by ID.')):
    """Let a fax server send through Faxbot. Its password is printed once: enter the user name and password on the
    fax server, then press Apply on the trunk page (faxbot delivery providers trunk apply) so the fax engine loads
    it."""
    created = state.api().post('/admin/sip/fax-servers', json={
        'name': name, 'address': address, 'countries': _countries(countries), 'per_hour': per_hour,
        'per_day': per_day, 'dial_prefix': dial_prefix, 'contact_email': email, 'contact_webhook': webhook,
        'mailbox_id': mailbox})

    def show(out):
        out.line(created['sentence'])
        out.line(f"User name: {created['username']}")
        out.line(f"Password: {created['password']}")
        out.line(f"Realm: {created['realm']}")
        out.line(created['password_text'])
        out.line(created['apply_text'])
        if created.get('where_text'):
            out.line(created['where_text'])
    state.out().result(created, show)


@servers.command('change')
def servers_change(server: str = typer.Argument(..., help='The fax server, by its name, address or ID.'),
                   countries: str = typer.Option(None, '--countries', metavar='US,CA',
                                                 help='Countries it may send to, replacing the list.'),
                   per_hour: int = typer.Option(None, '--per-hour', help='Most faxes it may send in an hour.'),
                   per_day: int = typer.Option(None, '--per-day', help='Most faxes it may send in a day.'),
                   dial_prefix: str = typer.Option(None, '--dial-prefix', metavar='DIGITS',
                                                   help='Its outside-line prefix, such as 9.'),
                   email: str = typer.Option(None, '--email', help='Who is told when one of its faxes is not '
                                                                   'delivered.'),
                   webhook: str = typer.Option(None, '--webhook', metavar='URL',
                                               help='Your system that is told when one of its faxes is not '
                                                    'delivered.')):
    """Change where a fax server may send, its limits, its prefix or who is told about failures."""
    api = state.api()
    found = _one(api, server)
    body = {key: value for key, value in {
        'countries': _countries(countries) if countries is not None else None, 'per_hour': per_hour,
        'per_day': per_day, 'dial_prefix': dial_prefix, 'contact_email': email, 'contact_webhook': webhook}.items()
        if value is not None}
    if not body:
        raise CliError('Say what to change, such as --per-day 200.')
    changed = api.put('/admin/sip/fax-servers/' + segment(found['id']), json=body)
    state.out().result(changed, lambda out: out.line(changed['sentence']))


@servers.command('remove')
def servers_remove(server: str = typer.Argument(..., help='The fax server, by its name, address or ID.')):
    """Stop a fax server sending through Faxbot and revoke its sending key."""
    api = state.api()
    found = _one(api, server)
    result = api.post(f"/admin/sip/fax-servers/{segment(found['id'])}/remove")
    state.out().result(result, lambda out: out.line(result['detail']))


@servers.command('faxes')
def servers_faxes(server: str = typer.Argument(None, help='Only this fax server, by its name, address or ID.'),
                  limit: int = typer.Option(50, '--limit', min=1, max=500, help='How many, newest first.')):
    """List the faxes your fax servers handed to Faxbot, and what became of each."""
    api = state.api()
    params = {'limit': limit}
    if server:
        params['server'] = _one(api, server)['id']
    found = api.get('/admin/sip/fax-servers/calls', params=params)
    items = found.get('calls') or []

    def show(out):
        out.table(['Received', 'Fax server', 'To', 'State', 'What happened', 'Notice'], [
            [local_time(item['received_at']), item.get('server') or '-', item.get('to') or item.get('dialled') or '-',
             item['state_text'], item['sentence'], item.get('notice') or '-'] for item in items],
            empty='No faxes from fax servers yet.')
    state.out().result(found, show)
