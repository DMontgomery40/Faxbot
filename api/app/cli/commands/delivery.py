"""Delivery routes and costs, the intake queue, direct delivery partners and case packets."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time, money, text

routing = typer.Typer(help='Delivery routes, destinations, fax costs and rate cards.', no_args_is_help=True)
intake = typer.Typer(help='The intake queue: received documents being delivered to email and other places.',
                     no_args_is_help=True)
connectors = typer.Typer(help='Where intake delivers documents, such as an email inbox.', no_args_is_help=True)
direct = typer.Typer(help='Direct delivery: send faxes to verified Faxbot partners over the internet.',
                     no_args_is_help=True)
peers = typer.Typer(help='Direct delivery partners.', no_args_is_help=True)
cases = typer.Typer(help='Case packets: send only the documents a recipient has not already accepted.',
                    no_args_is_help=True)


def register(app):
    app.add_typer(routing, name='routing')
    intake.add_typer(connectors, name='connectors')
    app.add_typer(intake, name='intake')
    direct.add_typer(peers, name='peers')
    app.add_typer(direct, name='direct')
    app.add_typer(cases, name='cases')


def _read_document(path):
    """Text from a file, or standard input for '-'."""
    if path == '-':
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError:
        raise CliError(f'Cannot read {path}.') from None


# -- routing ---------------------------------------------------------------------------

@routing.command('destinations')
def routing_destinations():
    """List destinations Faxbot knows about, with routes used and costs over the last 30 days."""
    result = state.api().get('/routing/destinations')
    state.out().result(result, lambda out: out.table(
        ['Fax number', 'Name', 'Preferred route', 'Accepts references', 'Routes used', 'Estimated cost'],
        [[item['number'], item.get('display_name'), item.get('preferred_route') or 'automatic',
          item.get('accepts_references'), len(item.get('routes', [])), money(item.get('estimated_cost_30_days'))]
         for item in result.get('destinations', [])], empty='No destinations yet.'))


def _route_rows(routes):
    return [[item['label'], item['attempts'], item['successes'], item['failures'],
             '-' if item['success_percent'] is None else f"{item['success_percent']}%",
             money(item['estimated_cost_30_days']), local_time(item.get('last_attempt_at'), empty='never')]
            for item in routes]


@routing.command('destination')
def routing_destination(number: str = typer.Argument(..., help='Fax number.')):
    """Show one destination: its settings, the routes used and the route Faxbot would choose now."""
    view = state.api().get('/routing/destinations/' + segment(number))

    def human(out):
        partner = view.get('direct_partner') or {}
        out.fields([('Fax number', view['number']), ('Name', view.get('display_name')), ('Notes', view.get('notes')),
                    ('Preferred route', view.get('preferred_route') or 'automatic'),
                    ('Accepts references', view.get('accepts_references')),
                    ('Direct partner', partner.get('organization')),
                    ('Available routes', [item['label'] for item in view.get('available_routes', [])])])
        out.table(['Route', 'Attempts', 'Delivered', 'Failed', 'Success', 'Estimated cost', 'Last used'],
                  _route_rows(view.get('routes', [])), empty='No faxes sent to this number in the last 30 days.')
        out.table(['Faxbot would choose', 'Why', 'Estimated cost, one page'],
                  [[item['label'], item['explanation'], money([item['estimated_cost_one_page']])
                    if item.get('estimated_cost_one_page') else 'unknown'] for item in view.get('recommended_routes', [])],
                  empty='No route recommendation: outbound delivery is not set up.')
    state.out().result(view, human)


@routing.command('update-destination')
def routing_update_destination(number: str = typer.Argument(..., help='Fax number.'),
                               name: str = typer.Option(None, '--name', help='A name for this destination.'),
                               notes: str = typer.Option(None, '--notes', help='Notes for your team.'),
                               preferred_route: str = typer.Option(None, '--preferred-route',
                                   help="Route to use first, as listed by 'faxbot routing destination'. Use "
                                        "'automatic' to let Faxbot choose."),
                               references: bool = typer.Option(None, '--accepts-references/--no-references',
                                   help='Whether this recipient accepts case packets that refer to documents '
                                        'they already accepted.')):
    """Change a destination's name, notes, preferred route or case-packet setting."""
    api = state.api()
    body = {}
    if name is not None:
        body['display_name'] = name
    if notes is not None:
        body['notes'] = notes
    if preferred_route is not None:
        body['preferred_route'] = None if preferred_route == 'automatic' else preferred_route
    if references is not None:
        body['accepts_references'] = references
    if not body:
        raise CliError('Nothing to change. Add at least one option; see --help.')
    current = api.get('/routing/destinations/' + segment(number))
    view = api.patch('/routing/destinations/' + segment(number), json={**body, 'version': current.get('version', 0)})
    state.out().result(view, lambda out: out.line(f"Destination {view['number']} updated."))


@routing.command('costs')
def routing_costs(since: str = typer.Option(None, '--since', help='Start date, for example 2026-09-01. Default: '
                                                                  'the last 30 days.')):
    """Show fax attempts and costs per provider: Faxbot's estimate, the provider's report and settled charges."""
    result = state.api().get('/routing/costs', params={'since': since})

    def human(out):
        out.line(f"Since {local_time(result['since'])}")
        out.table(['Provider', 'Attempts', 'Delivered', 'Failed', 'Uncertain', 'Estimated', 'Provider reported',
                   'Settled', 'No reported cost'],
                  [[item['label'], item['attempts'], item['successes'], item['failures'], item['uncertain'],
                    money(item['estimated_cost']), money(item['reported_cost']), money(item['settled_cost']),
                    item['attempts_without_reported_cost']] for item in result.get('providers', [])],
                  empty='No fax attempts in this period.')
    state.out().result(result, human)


@routing.command('rate-cards')
def routing_rate_cards(replace: str = typer.Option(None, '--replace', metavar='FILE',
                                                   help="Replace all rate cards with the cards in this JSON file "
                                                        "({\"cards\": [...]}, or '-' for standard input).")):
    """Show the prices Faxbot uses to estimate costs, or replace them from a file."""
    api = state.api()
    if replace:
        try:
            document = json.loads(_read_document(replace))
        except ValueError:
            raise CliError('The rate card file is not valid JSON.') from None
        if isinstance(document, list):
            document = {'cards': document}
        result = api.put('/routing/rate-cards', json=document)
    else:
        result = api.get('/routing/rate-cards')
    state.out().result(result, lambda out: out.table(
        ['Provider', 'Name', 'Per minute', 'Per page', 'Per call', 'Billing step', 'Currency', 'Captured'],
        [[card['provider_id'], card['label'], card['per_minute'], card['per_page'], card['per_call'],
          f"{card['billing_increment_seconds']} s", card['currency'], card['captured_on']]
         for card in result.get('cards', [])], empty='No rate cards.'))


# -- intake ------------------------------------------------------------------------------

@intake.command('items')
def intake_items(state_filter: str = typer.Option(None, '--state', help='received, sending, delivered or failed.'),
                 limit: int = typer.Option(100, '--limit', min=1, max=500, help='How many to show.'),
                 ids: bool = typer.Option(False, '--ids', help='Also show item IDs, for intake retry.')):
    """List received documents and their delivery, newest first."""
    result = state.api().get('/intake/items', params={'state': state_filter, 'limit': limit})

    def human(out):
        out.table((['Item ID'] if ids else []) + ['Received', 'From', 'To', 'Pages', 'Delivery', 'Status',
                                                  'Needs action'],
                  [([item['id']] if ids else []) + [local_time(item['received_at']), item.get('from_number'),
                                                    item.get('to_number'), item.get('pages'), item.get('connector'),
                                                    item['status'], item['needs_action']]
                   for item in result.get('items', [])], empty='The intake queue is empty.')
        counts = result.get('counts', {})
        out.line(', '.join(f'{count} {name}' for name, count in counts.items()))
    state.out().result(result, human)


@intake.command('retry')
def intake_retry(item_id: str = typer.Argument(..., help="Item ID from 'faxbot intake items'.")):
    """Try delivering a received document again."""
    item = state.api().post(f'/intake/items/{segment(item_id)}/retry')
    state.out().result(item, lambda out: out.line(f"Delivery will be tried again. {item['status']}"))


def _connector(api, name):
    items = api.get('/intake/connectors')['connectors']
    matches = [item for item in items if item['id'] == name or item['name'].casefold() == name.casefold()]
    if len(matches) != 1:
        raise CliError(f"No single connector matches '{name}'. See 'faxbot intake connectors list'.")
    return matches[0]


@connectors.command('list')
def connectors_list():
    """List intake connectors."""
    items = state.api().get('/intake/connectors')['connectors']
    state.out().result(items, lambda out: out.table(['Name', 'Enabled', 'Fax number', 'Mail server', 'Sends to',
                                                     'Password saved'],
        [[item['name'], item['enabled'], item.get('match_number') or 'all', f"{item['host']}:{item['port']}",
          item['recipients'], item['has_password']] for item in items], empty='No connectors.'))


@connectors.command('add')
def connectors_add(name: str = typer.Argument(..., help='Connector name.'),
                   host: str = typer.Option(..., '--host', help='Mail server (SMTP) address.'),
                   recipients: list[str] = typer.Option(..., '--to', help='Email address to deliver to. Repeat for more.'),
                   from_address: str = typer.Option(..., '--from', help='Sender email address.'),
                   port: int = typer.Option(587, '--port', help='Mail server port.'),
                   security: str = typer.Option('starttls', '--security', help='starttls, tls or none.'),
                   username: str = typer.Option('', '--username', help='Mail server sign-in name.'),
                   ask_password: bool = typer.Option(False, '--ask-password',
                                                     help='Ask for the mail server password without showing it. '
                                                          'FAXBOT_SMTP_PASSWORD also works.'),
                   subject: str = typer.Option('Fax from {from_number}', '--subject', help='Email subject.'),
                   match_number: str = typer.Option(None, '--fax-number',
                                                    help='Only documents sent to this fax number. Default: all.'),
                   disabled: bool = typer.Option(False, '--disabled', help='Create it switched off.')):
    """Add an email connector for received documents."""
    password = os.environ.get('FAXBOT_SMTP_PASSWORD') or None
    if ask_password:
        password = typer.prompt('Mail server password', hide_input=True)
    body = {'name': name, 'enabled': not disabled, 'match_number': match_number, 'host': host, 'port': port,
            'security': security, 'username': username, 'password': password, 'from_address': from_address,
            'recipients': recipients, 'subject_template': subject}
    connector = state.api().post('/intake/connectors', json=body)
    state.out().result(connector, lambda out: out.line(f"Connector {connector['name']} added. Check it with: "
                                                       f"faxbot intake connectors test \"{connector['name']}\""))


@connectors.command('update')
def connectors_update(name: str = typer.Argument(..., help='Connector name.'),
                      new_name: str = typer.Option(None, '--name', help='New name.'),
                      host: str = typer.Option(None, '--host', help='Mail server address.'),
                      port: int = typer.Option(None, '--port', help='Mail server port.'),
                      security: str = typer.Option(None, '--security', help='starttls, tls or none.'),
                      username: str = typer.Option(None, '--username', help='Mail server sign-in name.'),
                      ask_password: bool = typer.Option(False, '--ask-password', help='Ask for a new mail server '
                                                                                      'password without showing it.'),
                      recipients: list[str] = typer.Option(None, '--to', help='Replace the recipients. Repeat for more.'),
                      from_address: str = typer.Option(None, '--from', help='Sender email address.'),
                      subject: str = typer.Option(None, '--subject', help='Email subject.'),
                      match_number: str = typer.Option(None, '--fax-number', help="Only this fax number; 'all' for "
                                                                                 'every number.'),
                      enable: bool = typer.Option(False, '--enable', help='Switch on.'),
                      disable: bool = typer.Option(False, '--disable', help='Switch off.')):
    """Change an email connector. Settings you leave out stay as they are."""
    if enable and disable:
        raise CliError('Choose --enable or --disable, not both.')
    api = state.api()
    current = _connector(api, name)
    body = {'name': new_name or current['name'], 'host': host or current['host'],
            'port': port if port is not None else current['port'], 'security': security or current['security'],
            'username': username if username is not None else current['username'],
            'from_address': from_address or current['from_address'], 'recipients': recipients or current['recipients'],
            'subject_template': subject or current['subject_template'],
            'match_number': None if match_number == 'all' else (match_number or current.get('match_number')),
            'enabled': True if enable else False if disable else current['enabled'],
            'password': typer.prompt('Mail server password', hide_input=True) if ask_password else None,
            'version': current['version']}
    connector = api.put('/intake/connectors/' + segment(current['id']), json=body)
    state.out().result(connector, lambda out: out.line(f"Connector {connector['name']} updated."))


@connectors.command('test')
def connectors_test(name: str = typer.Argument(..., help='Connector name.')):
    """Send a test email through a connector."""
    api = state.api()
    connector = _connector(api, name)
    result = api.post(f"/intake/connectors/{segment(connector['id'])}/test")
    state.out().result(result, lambda out: out.line(result.get('detail') or ''))
    if not result.get('ok'):
        raise typer.Exit(1)


@connectors.command('remove')
def connectors_remove(name: str = typer.Argument(..., help='Connector name.')):
    """Remove a connector."""
    api = state.api()
    connector = _connector(api, name)
    result = api.delete('/intake/connectors/' + segment(connector['id']))
    state.out().result(result, lambda out: out.line(f"Connector {connector['name']} removed."))


# -- direct delivery -------------------------------------------------------------------------

@direct.command('card')
def direct_card(output: str = typer.Option(None, '--output', '-o', help='Save the card to this file.')):
    """Show this installation's partner card. Send it to partners so they can enroll you."""
    card = state.api().get('/direct/card')['card']
    if output:
        Path(output).write_text(json.dumps(card, indent=2) + '\n', encoding='utf-8')
        state.out().result({'card': card, 'saved_to': output}, lambda out: out.line(f'Partner card saved to {output}.'))
        return
    state.out().result({'card': card}, lambda out: out.json(card))


def _peer(api, reference):
    items = api.get('/direct/peers')['peers']
    digits = ''.join(ch for ch in reference if ch.isdigit())
    matches = [item for item in items if item['id'] == reference
               or item['organization'].casefold() == reference.casefold()
               or (digits and ''.join(ch for ch in item['fax_number'] if ch.isdigit()) == digits)]
    if len(matches) != 1:
        raise CliError(f"No single partner matches '{reference}'. See 'faxbot direct peers list'.")
    return matches[0]


@peers.command('list')
def peers_list():
    """List direct delivery partners."""
    items = state.api().get('/direct/peers')['peers']
    state.out().result(items, lambda out: out.table(['Partner', 'Fax number', 'State', 'Status', 'Verified'],
        [[item['organization'], item['fax_number'], item['state'], item['status'],
          local_time(item.get('verified_at'), empty='-')] for item in items], empty='No partners.'))


@peers.command('add')
def peers_add(card_file: str = typer.Argument(..., help="The partner's card file, or '-' for standard input.")):
    """Enroll a partner from their card. Then send them a challenge fax to verify their number."""
    raw = _read_document(card_file)
    try:
        card = json.loads(raw)
    except ValueError:
        card = raw.strip()
    peer = state.api().post('/direct/peers', json={'card': card})
    state.out().result(peer, lambda out: out.line(f"{peer['organization']} enrolled. {peer['status']}"))


@peers.command('challenge')
def peers_challenge(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """Fax the partner a one-page code they confirm from their Faxbot."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/challenge")
    state.out().result(result, lambda out: out.line(f"Challenge fax sent to {result['fax_number']} (fax ID "
                                                    f"{result['fax_id']}). {result['status']}"))


@peers.command('confirm')
def peers_confirm(partner: str = typer.Argument(..., help='Partner organization, fax number or id.'),
                  code: str = typer.Argument(..., help="The code printed on the partner's challenge fax.")):
    """Enter the code from a partner's challenge fax to prove this installation to them."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/confirm", json={'code': code})
    state.out().result(result, lambda out: out.line(result.get('detail') or 'Confirmed.'))


@peers.command('revoke')
def peers_revoke(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """Remove a partner. Faxes to their number go by fax again."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/revoke")
    state.out().result(result, lambda out: out.line(f"{result['organization']} removed."))


@direct.command('deliveries')
def direct_deliveries():
    """List recent direct deliveries in both directions."""
    items = state.api().get('/direct/deliveries')['deliveries']
    state.out().result(items, lambda out: out.table(['When', 'Direction', 'Partner', 'Fax number', 'Status'],
        [[local_time(item['created_at']), item['direction'], item.get('partner'), item.get('fax_number'),
          item['status']] for item in items], empty='No direct deliveries yet.'))


# -- case packets ---------------------------------------------------------------------------------

@cases.command('documents')
def cases_documents(case_id: str = typer.Argument(..., help='Your case reference.'),
                    to: str = typer.Option(..., '--to', help='Recipient fax number.'),
                    ids: bool = typer.Option(False, '--ids', help='Also show the fax ID each document was sent in.')):
    """List the documents of a case already sent to a recipient, and which they accepted."""
    result = state.api().get(f'/cases/{segment(case_id)}/documents', params={'to': to})

    def human(out):
        out.line(f"Recipient accepts references: {text(result.get('accepts_references'))}")
        out.table(['Document', 'Pages', 'Accepted', 'Reference'] + (['Fax ID'] if ids else []),
                  [[item['title'], item['pages'], local_time(item.get('accepted_at'), empty='no'), item['reference']]
                   + ([item.get('fax_id')] if ids else []) for item in result.get('documents', [])],
                  empty='Nothing has been sent for this case to this recipient.')
    state.out().result(result, human)


@cases.command('send')
def cases_send(case_id: str = typer.Argument(..., help='Your case reference.'),
               to: str = typer.Argument(..., help='Recipient fax number.'),
               files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                                  help='PDF documents for the packet, in order.'),
               title: list[str] = typer.Option(None, '--title', help='Title for each document, in the same order. '
                                                                     'Default: the file name.'),
               preview: bool = typer.Option(False, '--preview', help='Show what would be sent without sending.')):
    """Send a case packet, leaving out documents the recipient already accepted."""
    with ExitStack() as stack:
        uploads = [('documents', (path.name, stack.enter_context(path.open('rb')), 'application/pdf'))
                   for path in files]
        data = {'to': to, 'preview': 'true' if preview else 'false', 'titles': list(title or [])}
        result = state.api().post(f'/cases/{segment(case_id)}/faxes', data=data, files=uploads)

    def human(out):
        out.table(['Document', 'Pages', 'In this packet'],
                  [[item['title'], item['pages'], 'included' if item['status'] == 'included' else 'referred to']
                   for item in result.get('documents', [])])
        out.line(f"{result['pages']} pages to send, {result['pages_saved']} pages saved.")
        if result.get('fax_id'):
            out.line(f"Sent as fax {result['fax_id']}. Check on it with: faxbot status {result['fax_id']}")
        else:
            out.line('Preview only; nothing was sent.')
    state.out().result(result, human)
