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
from ..output import cost_amount, local_time, money, text

routing = typer.Typer(help='Delivery routes, destinations, fax costs and rate cards.', no_args_is_help=True)
intake = typer.Typer(help='The intake queue: received documents being delivered to email and other places.',
                     no_args_is_help=True)
connectors = typer.Typer(help='The email inboxes and other places received faxes are delivered to.', no_args_is_help=True)
direct = typer.Typer(help='Direct delivery: send faxes to verified Faxbot partners over the internet.',
                     no_args_is_help=True)
peers = typer.Typer(help='Direct delivery partners.', no_args_is_help=True)
cases = typer.Typer(help='Case packets: when you fax documents for a case, leave out the ones the recipient already has.',
                    no_args_is_help=True)


batching = typer.Typer(help='Send short faxes to the same number together in one call, where the recipient agreed '
                            'and it saves money.', no_args_is_help=True)


routing.add_typer(batching, name='batching')
intake.add_typer(connectors, name='connectors')
direct.add_typer(peers, name='peers')


def _read_document(path):
    """Text from a file, or standard input for '-'."""
    if path == '-':
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError:
        raise CliError(f'Cannot read {path}.') from None


# -- routing ---------------------------------------------------------------------------

def preferred_text(item):
    """The route a number is sent by first, in the console's words."""
    route = item.get('preferred_route')
    if not route:
        return 'Cheapest reliable'
    if route == 'direct':
        return 'Direct delivery'
    return next((known['label'] for known in item.get('routes') or [] if known.get('route') == route), route)


def references_text(value):
    """Whether a number takes a case packet's one-page list, in the console's words."""
    return 'Takes a one-page list instead' if value else 'Full documents'


@routing.command('destinations')
def routing_destinations():
    """List the numbers you fax, with how faxes went and what they cost over the last 30 days."""
    result = state.api().get('/routing/destinations')
    state.out().result(result, lambda out: out.table(
        ['Fax number', 'Name', 'Preferred way to send', 'Case packets', 'Routes used', 'Estimated cost'],
        [[item['number'], item.get('display_name'), preferred_text(item),
          references_text(item.get('accepts_references')), len(item.get('routes', [])),
          money(item.get('estimated_cost_30_days'))]
         for item in result.get('destinations', [])], empty='No destinations yet.'))


def _route_rows(routes):
    return [[item['label'], item['attempts'], item['successes'], item['failures'],
             '-' if item['success_percent'] is None else f"{item['success_percent']}%",
             money(item['estimated_cost_30_days']), local_time(item.get('last_attempt_at'), empty='never')]
            for item in routes]


@routing.command('destination')
def routing_destination(number: str = typer.Argument(..., help='Fax number.'),
                        pages: int = typer.Option(1, '--pages', min=1, max=1000,
                                                  help='Estimate the cost of a fax this many pages long.')):
    """Show one number you fax: its settings, how faxes to it went, and how Faxbot would send now."""
    view = state.api().get('/routing/destinations/' + segment(number), params={'pages': pages})

    def human(out):
        partner = view.get('direct_partner') or {}
        out.fields([('Fax number', view['number']), ('Name', view.get('display_name')), ('Notes', view.get('notes')),
                    ('Preferred way to send', preferred_text(view)),
                    ('Case packets', references_text(view.get('accepts_references'))),
                    ('Direct partner', partner.get('organization')),
                    ('Available routes', [item['label'] for item in view.get('available_routes', [])])])
        out.table(['Route', 'Attempts', 'Delivered', 'Failed', 'Success', 'Estimated cost', 'Last used'],
                  _route_rows(view.get('routes', [])), empty='No faxes sent to this number in the last 30 days.')
        out.table(['Faxbot would choose', 'Why', 'Rate', f"Estimated cost, {pages} {'page' if pages == 1 else 'pages'}"],
                  [[item['label'], item['explanation'], _route_rate(item),
                    'In your plan' if item.get('included_in_plan')
                    else f"{money([item['estimated_cost']])} estimate" if item.get('estimated_cost') else 'Unknown']
                   for item in view.get('recommended_routes', [])],
                  empty='No route recommendation: outbound delivery is not set up.')
    state.out().result(view, human)


@routing.command('update-destination')
def routing_update_destination(number: str = typer.Argument(..., help='Fax number.'),
                               name: str = typer.Option(None, '--name', help='A name for this destination.'),
                               notes: str = typer.Option(None, '--notes', help='Notes for your team.'),
                               preferred_route: str = typer.Option(None, '--preferred-route',
                                   help="Route to use first, as listed by 'faxbot recipients show'. Use "
                                        "'automatic' for the cheapest reliable route."),
                               references: bool = typer.Option(None, '--accepts-references/--no-references',
                                   help='Whether this recipient accepts case packets that reference documents '
                                        'they already received instead of resending them.')):
    """Change a number's name, notes, preferred route, or whether it accepts case packets."""
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


def _monthly(card):
    return f"{money([{'currency': card['currency'], 'amount': card['monthly_fee']}])} a month"


def _route_rate(item):
    """A route's price as Prices & plans words it: the plan with its fee, the rate, or no price yet."""
    fee = item.get('monthly_fee')
    if item.get('included_in_plan') and fee:
        return f'{money([fee])} a month, faxes included'
    return item.get('rate') or 'No price set'


def _route_name(item):
    return f"{item['label']} ({item['carrier']})" if item.get('carrier') else item['label']


def _not_billed(item, unreported):
    """Estimate for faxes the carrier has not billed yet, or the plan that includes them."""
    plan = item.get('plan')
    if plan:
        fee = plan.get('monthly_fee_text') or money([plan['monthly_fee']])
        return f"Included in your {plan['label']} plan ({fee} a month)"
    if item.get('priced') is False and not item.get('reported_cost'):
        return 'No published price; add your rate'
    return money(item.get('estimated_cost_not_reported')) if item.get(unreported) else '-'


def _unrecorded_lines(out, items):
    for item in items:
        calls = item.get('unrecorded_calls') or 0
        matched = item.get('unrecorded_matched_to_faxes') or 0
        who = item.get('carrier') or 'Your carrier'
        if calls - matched:
            unmatched = calls - matched
            out.line(f"{who} billed {unmatched} {'call' if unmatched == 1 else 'calls'} Faxbot has no record of: "
                     f"{money(item.get('unrecorded_unmatched_cost') or item.get('unrecorded_cost'))}. "
                     "It is included in Charged.")
        if matched:
            out.line(f"{matched} {'call' if matched == 1 else 'calls'} reached Faxbot without a call record; "
                     f"{'its fax is' if matched == 1 else 'their faxes are'} in Received. Included in Charged.")


@routing.command('costs')
def routing_costs(since: str = typer.Option(None, '--since', help='Start date, for example 2026-09-01. Default: '
                                                                  'the last 30 days.')):
    """Show what faxing cost per route: carrier charges, estimates for faxes not billed yet, and what is waiting."""
    result = state.api().get('/routing/costs', params={'since': since})

    def human(out):
        out.line(f"Since {local_time(result['since'])}")
        out.table(['Route', 'Faxes', 'Delivered', 'Billed minutes', 'Charged', 'Estimated, not billed yet',
                   'Waiting for the bill', 'Could not be matched'],
                  [[_route_name(item), item['attempts'], item['successes'], item['billed_minutes'],
                    money(item['reported_cost']), _not_billed(item, 'attempts_without_reported_cost'),
                    item.get('awaiting_carrier_bill', 0), item.get('unmatched_charges', 0)]
                   for item in result.get('providers', [])],
                  empty='No faxes sent in this period.')
        received = result.get('received') or []
        if received:
            out.table(['Received on', 'Calls', 'Faxes', 'Billed minutes', 'Charged', 'Estimated, not billed yet',
                       'Waiting for the bill', 'Could not be matched'],
                      [[_route_name(item), item['calls'], item['faxes'], item['billed_minutes'],
                        money(item['reported_cost']), _not_billed(item, 'calls_without_reported_cost'),
                        item['awaiting_carrier_bill'], item['unmatched_charges']] for item in received])
        _unrecorded_lines(out, [*result.get('providers', []), *received])
        if result.get('total_cost'):
            out.line(f"Total: {money(result['total_cost'])} (charges, estimates for faxes not billed yet, and plan fees "
                     "counted once per 30 days, pro-rated by day).")
        carrier = result.get('carrier_charges') or {}
        if carrier.get('supported') and not carrier.get('readable'):
            out.line(f"{carrier['carrier']} call charges appear once a {carrier['carrier']} API key is set: "
                     "add TELNYX_API_KEY to .env, then run docker compose up -d.")
    state.out().result(result, human)


@routing.command('reconcile')
def routing_reconcile():
    """Ask your SIP trunk carrier now what each recent call cost. This never changes a fax's delivery result."""
    result = state.api().post('/routing/reconcile', json={})
    state.out().result(result, lambda out: out.line(result['summary']))


@routing.command('fax-cost')
def routing_fax_cost(fax_id: str = typer.Argument(..., help="Fax ID from 'faxbot sent list --ids' or, with --received, "
                                                           "from 'faxbot received list --ids'."),
                     received: bool = typer.Option(False, '--received', help='The fax is a received fax.')):
    """Show what one fax cost: the carrier's charge, or why it is not known yet."""
    path = ('/routing/inbound/' if received else '/routing/faxes/') + segment(fax_id) + '/cost'
    result = state.api().get(path)
    state.out().result(result, lambda out: out.line(result.get('summary') or 'No call was placed for this fax.'))


def _received_cost_rows(costs, items):
    from .fax import arrived
    return [[item.get('fr') or 'Unknown', arrived(item), cost_amount(costs.get(item['id']))] for item in items]


@routing.command('received')
def routing_received_costs(fax_id: str = typer.Argument(None, help="Received fax ID, from 'faxbot received list --ids'."),
                           every: bool = typer.Option(False, '--all',
                                                      help='Every received fax you can see, newest 100 first.')):
    """Show what the call that brought in a received fax cost, or the cost of every received fax."""
    from .fax import received_id
    if bool(fax_id) == every:
        raise CliError('Give a received fax ID, or --all.')
    api = state.api()
    if fax_id:
        result = received_id(api, fax_id, lambda found: api.get(f'/routing/inbound/{segment(found)}/cost'))
        state.out().result(result, lambda out: out.line(result.get('summary') or 'No call record for this fax.'))
        return
    items = api.get('/inbound')[:100]
    costs = api.get('/routing/inbound-costs', params={'ids': ','.join(item['id'] for item in items)})['costs'] \
        if items else {}
    state.out().result({'costs': costs}, lambda out: out.table(
        ['From', 'Received', 'Cost'], _received_cost_rows(costs, items), empty='No received faxes.'))


SAVING_PARTS = (('sending_together', 'Sending together'), ('direct_delivery', 'Direct delivery'),
                ('case_packets', 'Case packets'))


def routing_savings(days: int = typer.Option(30, '--days', min=1, max=366, help='How many days back to count.')):
    """Show how much money Faxbot saved by batching faxes to the same number, delivering directly to partners, and leaving out documents a recipient already has. All figures are estimates."""
    result = state.api().get('/routing/savings', params={'days': days})

    def human(out):
        total = result.get('total_saved') or []
        out.line(f"About {money(total)} saved in the last {result['days']} days." if total
                 else f"No money saved in the last {result['days']} days, as far as Faxbot can tell.")
        out.table(['Saving', 'Estimate', 'What happened'],
                  [[title, money((result.get(key) or {}).get('saved')), (result.get(key) or {}).get('sentence') or '-']
                   for key, title in SAVING_PARTS])
        counted = (result.get('case_packets') or {}).get('counted_from_sentence')
        if counted:
            out.line(counted)
        if result.get('sentence'):
            out.line(result['sentence'])
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
        ['Provider', 'Name', 'Per minute', 'Per page', 'Per call', 'Monthly', 'Billing step', 'Currency', 'Captured'],
        [[card['provider_id'], card['label'], card['per_minute'], card['per_page'], card['per_call'],
          (f"{_monthly(card)}, faxes included" if card.get('included_in_plan')
           else _monthly(card) if card.get('monthly_fee') else '-'),
          f"{card['billing_increment_seconds']} s", card['currency'], card['captured_on']]
         for card in result.get('cards', [])], empty='No rate cards.'))


# -- routing batching --------------------------------------------------------------------

def _batching_human(view):
    def human(out):
        agreement = view.get('agreement') or {}
        out.line(view['state_sentence'])
        out.line(view['route_sentence'])
        out.fields([('Fax number', view['number']), ('Sending together', 'on' if view['enabled'] else 'off'),
                    ('Longest wait', f"{view['max_wait_minutes']} minutes"),
                    ('Most pages in one call', view['max_pages']),
                    ('Different senders may share a call', view['mixed_senders']),
                    ('Recipient agreement recorded by', agreement.get('by')),
                    ('Recorded', local_time(agreement.get('at')) if agreement else None)])
        out.line(view['savings']['sentence'])
    return human


@batching.command('show')
def batching_show(number: str = typer.Argument(..., help='Fax number.')):
    """Show whether faxes to a number go together in one call, why that saves money or not, and what it saved."""
    view = state.api().get('/batching/numbers/' + segment(number))
    state.out().result(view, _batching_human(view))


@batching.command('set')
def batching_set(number: str = typer.Argument(..., help='Fax number.'),
                 recipient_agreed: bool = typer.Option(False, '--recipient-agreed',
                     help='Record that this recipient has agreed to receive several documents in one call. '
                          'Needed to turn sending together on.'),
                 wait: int = typer.Option(None, '--wait', min=1, max=60, metavar='MINUTES',
                                          help='Longest time a fax waits for others (default 10).'),
                 max_pages: int = typer.Option(None, '--max-pages', min=2, max=200,
                                               help='Most pages one call carries, separator pages included (default 30).'),
                 mixed_senders: bool = typer.Option(None, '--mixed-senders/--same-sender-only',
                     help='Whether faxes from different users or API keys may share one call (default: no).')):
    """Turn sending together on for a number, or change how long faxes wait."""
    api = state.api()
    current = api.get('/batching/numbers/' + segment(number))
    body = {'enabled': True, 'recipient_agreed': recipient_agreed, 'version': current.get('version', 0)}
    if wait is not None:
        body['max_wait_minutes'] = wait
    if max_pages is not None:
        body['max_pages'] = max_pages
    if mixed_senders is not None:
        body['mixed_senders'] = mixed_senders
    view = api.put('/batching/numbers/' + segment(number), json=body)
    state.out().result(view, _batching_human(view))


@batching.command('check')
def batching_check(number: str = typer.Argument(..., help='Fax number.')):
    """Show whether a fax to this number would wait to go with others, or go straight away."""
    result = state.api().get('/batching/check', params={'to': number})
    state.out().result(result, lambda out: out.line(
        result.get('sentence') or 'Faxes to this number go straight away; they do not wait for others.'))


@batching.command('off')
def batching_off(number: str = typer.Argument(..., help='Fax number.')):
    """Turn off batching for a number; faxes waiting for it are sent straight away."""
    view = state.api().delete('/batching/numbers/' + segment(number))
    state.out().result(view, lambda out: out.line(view['state_sentence']))


def _read_on(value):
    from datetime import date
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        return '-'
    return f'{day.day} {day:%B %Y}'


def _plan_row(plan):
    fee = (f"{money([{'currency': plan['currency'], 'amount': plan['monthly_fee']}])} a month"
           if plan.get('monthly_fee') else 'By quote')
    extra = money([{'currency': plan['currency'], 'amount': plan['overage_per_page']}]) \
        if plan.get('overage_per_page') else '-'
    return [plan.get('label'), plan.get('country') or '-', fee, plan.get('includes') or '-', extra,
            plan.get('source_url') or '-', _read_on(plan.get('advertised_on'))]


@routing.command('plans')
def routing_plans(provider: str = typer.Argument(None, help='The fax service, for example efax.'),
                  in_use: bool = typer.Option(False, '--in-use',
                                              help='Plans for every sending provider that has no rate card yet.')):
    """Show the price plans a fax service advertises, with where Faxbot found them and when."""
    if bool(provider) == in_use:
        raise CliError('Name a provider, such as efax, or add --in-use.')
    if in_use:
        result = state.api().get('/routing/published-plans/in-use')
        found = result.get('items') or []
    else:
        result = state.api().get('/routing/published-plans', params={'provider_id': provider})
        found = [result]

    def human(out):
        if not found:
            out.line('Every fax service you send with has a price set.')
        for item in found:
            out.line(item.get('sentence') or '')
            out.table(['Plan', 'Country', 'Price', 'Includes', 'Extra page', 'Source', 'Read on'],
                      [_plan_row(plan) for plan in item.get('plans') or []], empty='No published plans.')
        if any(item.get('card') for item in found):
            out.line('To use the first plan as your estimate, add it as a rate card in Costs, Prices and plans, '
                     'or with faxbot costs rate-cards --replace.')
    state.out().result(result, human)


# -- intake ------------------------------------------------------------------------------

@intake.command('items')
def intake_items(state_filter: str = typer.Option(None, '--state', help='received, sending, delivered or failed.'),
                 limit: int = typer.Option(100, '--limit', min=1, max=500, help='How many to show.'),
                 ids: bool = typer.Option(False, '--ids', help="Also show each delivery's ID, to use with faxbot received deliveries retry.")):
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
def intake_retry(item_id: str = typer.Argument(..., help="Item ID from 'faxbot received deliveries list --ids'.")):
    """Try delivering a received document again."""
    item = state.api().post(f'/intake/items/{segment(item_id)}/retry')
    state.out().result(item, lambda out: out.line(f"Delivery will be tried again. {item['status']}"))


def _connector(api, name):
    items = api.get('/intake/connectors')['connectors']
    matches = [item for item in items if item['id'] == name or item['name'].casefold() == name.casefold()]
    if len(matches) != 1:
        raise CliError(f"No single connector matches '{name}'. See 'faxbot numbers email connectors list'.")
    return matches[0]


@connectors.command('list')
def connectors_list():
    """List the email inboxes and other places received faxes are delivered to."""
    items = state.api().get('/intake/connectors')['connectors']
    state.out().result(items, lambda out: out.table(['Name', 'Enabled', 'Fax number', 'Mail server', 'Sends to',
                                                     'Password saved'],
        [[item['name'], item['enabled'], item.get('match_number') or 'all', f"{item['host']}:{item['port']}",
          item['recipients'], item['has_password']] for item in items], empty='No connectors.'))


@connectors.command('add')
def connectors_add(name: str = typer.Argument(..., help='A name for this delivery, for example "Front desk email".'),
                   host: str = typer.Option(..., '--host', help="Your mail server's address."),
                   recipients: list[str] = typer.Option(..., '--to', help='Email address to deliver to. Repeat for more.'),
                   from_address: str = typer.Option(..., '--from', help='Sender email address.'),
                   port: int = typer.Option(587, '--port', help='Mail server port.'),
                   security: str = typer.Option('starttls', '--security', help='How the connection to the mail server is protected: starttls, tls or none.'),
                   username: str = typer.Option('', '--username', help='Mail server sign-in name.'),
                   ask_password: bool = typer.Option(False, '--ask-password',
                                                     help='Prompt for the mail server password without echoing it '
                                                          '(or set FAXBOT_SMTP_PASSWORD).'),
                   subject: str = typer.Option('Fax from {from_number}', '--subject', help='Email subject.'),
                   match_number: str = typer.Option(None, '--fax-number',
                                                    help='Only documents sent to this fax number. Default: all.'),
                   disabled: bool = typer.Option(False, '--disabled', help='Create the group disabled, so its roles do not apply yet.')):
    """Add an email inbox that received faxes are delivered to."""
    password = os.environ.get('FAXBOT_SMTP_PASSWORD') or None
    if ask_password:
        password = typer.prompt('Mail server password', hide_input=True)
    body = {'name': name, 'enabled': not disabled, 'match_number': match_number, 'host': host, 'port': port,
            'security': security, 'username': username, 'password': password, 'from_address': from_address,
            'recipients': recipients, 'subject_template': subject}
    connector = state.api().post('/intake/connectors', json=body)
    state.out().result(connector, lambda out: out.line(f"Connector {connector['name']} added. Check it with: "
                                                       f"faxbot numbers email connectors test \"{connector['name']}\""))


@connectors.command('update')
def connectors_update(name: str = typer.Argument(..., help='A name for this delivery, for example "Front desk email".'),
                      new_name: str = typer.Option(None, '--name', help='New name.'),
                      host: str = typer.Option(None, '--host', help='Mail server address.'),
                      port: int = typer.Option(None, '--port', help='Mail server port.'),
                      security: str = typer.Option(None, '--security', help='How the connection to the mail server is protected: starttls, tls or none.'),
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
    """Change an email delivery. Settings you leave out stay as they are."""
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
def connectors_test(name: str = typer.Argument(..., help='A name for this delivery, for example "Front desk email".')):
    """Send a test email to check a delivery address."""
    api = state.api()
    connector = _connector(api, name)
    result = api.post(f"/intake/connectors/{segment(connector['id'])}/test")
    state.out().result(result, lambda out: out.line(result.get('detail') or ''))
    if not result.get('ok'):
        raise typer.Exit(1)


@connectors.command('remove')
def connectors_remove(name: str = typer.Argument(..., help='A name for this delivery, for example "Front desk email".')):
    """Stop delivering to an email inbox and remove it."""
    api = state.api()
    connector = _connector(api, name)
    result = api.delete('/intake/connectors/' + segment(connector['id']))
    state.out().result(result, lambda out: out.line(f"Connector {connector['name']} removed."))


# -- direct delivery -------------------------------------------------------------------------

@direct.command('card')
def direct_card(output: str = typer.Option(None, '--output', '-o', help='Save the card to this file.')):
    """Show this installation's partner card. Partners need it to add you for direct delivery."""
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
        raise CliError(f"No single partner matches '{reference}'. See 'faxbot recipients partners list'.")
    return matches[0]


@peers.command('list')
def peers_list():
    """List your partners."""
    items = state.api().get('/direct/peers')['peers']
    state.out().result(items, lambda out: out.table(['Partner', 'Fax number', 'State', 'Status', 'Verified'],
        [[item['organization'], item['fax_number'], item['state'], item['status'],
          local_time(item.get('verified_at'), empty='-')] for item in items], empty='No partners.'))


@peers.command('add')
def peers_add(card_file: str = typer.Argument(..., help="The partner's card file, or '-' for standard input.")):
    """Add a partner from their card. Then send them a check fax to confirm their number."""
    raw = _read_document(card_file)
    try:
        card = json.loads(raw)
    except ValueError:
        card = raw.strip()
    peer = state.api().post('/direct/peers', json={'card': card})
    state.out().result(peer, lambda out: out.line(f"{peer['organization']} enrolled. {peer['status']}"))


@peers.command('challenge')
def peers_challenge(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """Fax the partner a one-page code to enter in their Faxbot, which proves the number is theirs."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/challenge")
    state.out().result(result, lambda out: out.line(f"Challenge fax sent to {result['fax_number']} (fax ID "
                                                    f"{result['fax_id']}). {result['status']}"))


@peers.command('confirm')
def peers_confirm(partner: str = typer.Argument(..., help='Partner organization, fax number or id.'),
                  code: str = typer.Argument(..., help="The verification code printed on the partner's challenge fax.")):
    """Enter the code from a partner's check fax to prove this installation to them."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/confirm", json={'code': code})
    state.out().result(result, lambda out: out.line(result.get('detail') or 'Confirmed.'))


@peers.command('revoke')
def peers_revoke(partner: str = typer.Argument(..., help='Partner organization, fax number or id.')):
    """Remove a partner. Faxes to their number go by phone again."""
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/revoke")
    state.out().result(result, lambda out: out.line(f"{result['organization']} removed."))


@direct.command('deliveries')
def direct_deliveries():
    """List recent faxes sent to and received from partners over the internet."""
    items = state.api().get('/direct/deliveries')['deliveries']
    state.out().result(items, lambda out: out.table(['When', 'Direction', 'Partner', 'Fax number', 'Status'],
        [[local_time(item['created_at']), item['direction'], item.get('partner'), item.get('fax_number'),
          item['status']] for item in items], empty='No direct deliveries yet.'))


# -- case packets ---------------------------------------------------------------------------------

@cases.command('list')
def cases_list(limit: int = typer.Option(50, '--limit', min=1, max=200, help='How many cases to show.')):
    """List the newest cases you sent packets for: who received them, documents sent and received, and when."""
    result = state.api().get('/cases', params={'limit': limit})
    items = result.get('cases') or []
    state.out().result(result, lambda out: out.table(
        ['Case', 'Recipient', 'Documents', 'Pages', 'Last sent'],
        [[item['case_id'], item['to'], f"{item['documents']} sent, {item['accepted']} received", item['pages'],
          local_time(item.get('last_sent_at'))] for item in items],
        empty='No case packets sent yet.'))


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
    """Send a case packet, leaving out documents the recipient already has."""
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
