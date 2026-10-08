"""Delivery routes and costs, the intake queue, direct delivery partners and case packets."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

import typer

from .. import state
from ..client import segment
from ..errors import CliError, EXIT_FAILURE
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


def calls_at_once_text(value):
    """Calls at once to a number, in the console's words."""
    if value is None:
        return 'One at a time'
    return 'No limit' if value == 0 else f'{value} at once'


def references_text(value):
    """Whether a number takes a case packet's one-page list, in the console's words."""
    return 'Takes a one-page list instead' if value else 'Full documents'


@routing.command('destinations')
def routing_destinations():
    """List the numbers you fax, with how faxes went and what they cost over the last 30 days."""
    result = state.api().get('/routing/destinations')
    state.out().result(result, lambda out: out.table(
        ['Fax number', 'Name', 'Preferred way to send', 'Case packets', 'Routes used', 'Estimated cost',
         'Per delivered fax'],
        [[item['number'], item.get('display_name'), preferred_text(item),
          references_text(item.get('accepts_references')), len(item.get('routes', [])),
          money(item.get('estimated_cost_30_days')), per_delivered_text(item.get('delivered_costs'))]
         for item in result.get('destinations', [])], empty='No destinations yet.'))


def per_delivered_text(routes):
    """Each route's cost per delivered fax in one cell, the cheapest first: "Telnyx: $0.0089; HumbleFax: Included in your plan"."""
    return '; '.join(f"{item['label']}: {item['cost_text']}" for item in routes or []) or '-'


def _route_rows(routes):
    return [[item['label'], item['attempts'], item['successes'], item['failures'],
             '-' if item['success_percent'] is None else f"{item['success_percent']}%",
             money(item['estimated_cost_30_days']), local_time(item.get('last_attempt_at'), empty='never')]
            for item in routes]


def call_time(seconds):
    """Seconds as people say them: "45 seconds", "1 minute 5 seconds"."""
    if seconds is None:
        return '-'
    minutes, rest = divmod(int(seconds), 60)
    parts = ([f"{minutes} {'minute' if minutes == 1 else 'minutes'}"] if minutes else []) + (
        [f"{rest} {'second' if rest == 1 else 'seconds'}"] if rest or not minutes else [])
    return ' '.join(parts)


def delivered_rows(routes):
    """Each route's delivered faxes and what one cost, counting every attempt on it (failed ones too)."""
    return [[item['label'], f"{item['delivered']} of {item['attempts']}", item['cost_text'],
             item.get('basis_text') or '-', text(item.get('average_pages')),
             call_time(item.get('average_connected_seconds'))] for item in routes]


@routing.command('destination')
def routing_destination(number: str = typer.Argument(..., help='Fax number.'),
                        pages: int = typer.Option(1, '--pages', min=1, max=1000,
                                                  help='Estimate the cost of a fax this many pages long.')):
    """Show one number you fax: its settings, how faxes to it went, and how Faxbot would send now."""
    api = state.api()
    view = api.get('/routing/destinations/' + segment(number), params={'pages': pages})
    from .sslfax import limits_fields
    from .pages import page_fields
    try:
        limits = api.get('/routing/destinations/' + segment(number) + '/fax-limits')
    except CliError:
        limits = None
    try:
        page_view = api.get('/routing/destinations/' + segment(number) + '/pages')
    except CliError:
        page_view = None

    def human(out):
        partner = view.get('direct_partner') or {}
        out.fields([('Fax number', view['number']), ('Name', view.get('display_name')), ('Notes', view.get('notes')),
                    ('Preferred way to send', preferred_text(view)),
                    ('Case packets', references_text(view.get('accepts_references'))),
                    ('Calls at once to this number', calls_at_once_text(view.get('max_calls'))),
                    ('Direct partner', partner.get('organization')),
                    ('Available routes', [item['label'] for item in view.get('available_routes', [])]),
                    *(limits_fields(limits) if limits else []), *(page_fields(page_view) if page_view else [])])
        out.table(['Route', 'Attempts', 'Delivered', 'Failed', 'Success', 'Estimated cost', 'Last used'],
                  _route_rows(view.get('routes', [])), empty='No faxes sent to this number in the last 30 days.')
        if view.get('delivered_costs'):
            out.table(['Route', 'Delivered', 'Cost per delivered fax', 'Where the cost comes from', 'Average pages',
                       'Average call time'],
                      delivered_rows(view['delivered_costs']), title='Cost per delivered fax, last 30 days')
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
                               calls_at_once: str = typer.Option(None, '--calls-at-once', metavar='N|default',
                                   help="Calls at once to this number: a number from 1 to 20, 0 for no limit, or "
                                        "'default' for one at a time."),
                               references: bool = typer.Option(None, '--accepts-references/--no-references',
                                   help='Whether this recipient accepts case packets that reference documents '
                                        'they already received instead of resending them.'),
                               index_page: bool = typer.Option(False, '--index-page',
                                   help="Faxes sent together to this number start with one index page listing "
                                        "each document's pages, instead of a separator page before each document. "
                                        "Records that the recipient agreed to it. Sending together must be on "
                                        "('faxbot recipients together set')."),
                               page_headers: bool = typer.Option(False, '--page-headers',
                                   help='Faxes sent together to this number have a line at the top of every page '
                                        'naming its document and page, with no separator or index page. Records '
                                        'that the recipient agreed to it. Needs your header text and sending number '
                                        '(faxbot system settings set fax_header=... fax_station_id=...).'),
                               separator_pages: bool = typer.Option(False, '--separator-pages',
                                   help='Go back to a separator page before each document sent together to this '
                                        'number.'),
                               pages_per_sheet: str = typer.Option(None, '--pages-per-sheet', metavar='MACHINE|NEVER',
                                   help='Several pages on one long page: machine (as the receiving machine allows) '
                                        'or never.'),
                               blank_space: str = typer.Option(None, '--blank-space', metavar='ON|OFF|DEFAULT',
                                   help='Leave out the blank bottom of pages when this machine has no error '
                                        'correction: on, off, or default for the setting all faxes use.'),
                               shading: str = typer.Option(None, '--shading', metavar='ON|OFF|DEFAULT',
                                   help='Lighten shaded areas and remove specks on documents sent to this '
                                        'recipient: on (always), off (never), or default for the setting all '
                                        'faxes use.')):
    """Change a number's name, notes, preferred route, calls at once, case packets, pages per sheet, blank space, shading, or how faxes sent together to it mark each document."""
    api = state.api()
    chosen = [value for flag, value in ((index_page, 'index_page'), (page_headers, 'page_headers'),
                                        (separator_pages, 'separators')) if flag]
    if len(chosen) > 1:
        raise CliError('Choose one of --index-page, --page-headers or --separator-pages.')
    boundaries = chosen[0] if chosen else None
    from .pages import recipient_page_body
    page_body = recipient_page_body(pages_per_sheet, blank_space, shading)
    body = {}
    if name is not None:
        body['display_name'] = name
    if notes is not None:
        body['notes'] = notes
    if preferred_route is not None:
        body['preferred_route'] = None if preferred_route == 'automatic' else preferred_route
    if references is not None:
        body['accepts_references'] = references
    if calls_at_once is not None:
        if calls_at_once == 'default':
            body['max_calls'] = None
        elif calls_at_once.isdigit() and int(calls_at_once) <= 20:
            body['max_calls'] = int(calls_at_once)
        else:
            raise CliError("Use a number from 0 to 20 for --calls-at-once, or 'default'.")
    if not body and not page_body and boundaries is None:
        raise CliError('Nothing to change. Add at least one option; see --help.')
    # How documents are marked is part of sending together (/batching); set it first, so a refusal changes nothing.
    together = _set_boundaries(api, number, boundaries) if boundaries is not None else None
    view = None
    if page_body:
        view = api.put('/routing/destinations/' + segment(number) + '/pages', json=page_body)
    if body:
        current = api.get('/routing/destinations/' + segment(number))
        view = api.patch('/routing/destinations/' + segment(number),
                         json={**body, 'version': current.get('version', 0)})
    result = together if view is None else view if together is None else {**view, 'sending_together': together}

    def human(out):
        if view is not None:
            out.line(f"Destination {view['number']} updated.")
        if together is not None:
            out.line(together.get('boundaries_sentence')
                     or 'Sending together is off for this number, so each fax goes in its own call.')
    state.out().result(result, human)


def _set_boundaries(api, number, boundaries):
    """Choose how a number's shared calls mark each document, recording the recipient's agreement to it."""
    current = api.get('/batching/numbers/' + segment(number))
    if boundaries == 'separators' and current.get('boundaries', 'separators') == 'separators':
        return current  # already separators: nothing to record
    return api.put('/batching/numbers/' + segment(number), json={
        'enabled': current['enabled'], 'boundaries': boundaries, 'boundaries_agreed': boundaries != 'separators',
        'version': current.get('version', 0)})


def _monthly(card):
    return f"{money([{'currency': card['currency'], 'amount': card['monthly_fee']}])} a month"


def _card_price(card, field):
    """One of a rate card's prices as money ("$0.005"), or '-' when the card charges nothing that way."""
    amount = card.get(field)
    if amount in (None, '') or not any(digit not in '0.' for digit in str(amount)):
        return '-'
    return money([{'currency': card['currency'], 'amount': amount}])


def _billing_step(seconds):
    if seconds and seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} {'minute' if minutes == 1 else 'minutes'}"
    return f"{seconds} {'second' if seconds == 1 else 'seconds'}"


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


def _not_priced_line(result):
    """How many faxes (sent, or received from a cloud provider) and received calls have no charge and no estimate,
    so no total counts them."""
    sent = sum(item.get('attempts_not_priced') or 0 for item in result.get('providers') or []) + sum(
        item.get('faxes_not_priced') or 0 for item in result.get('received_faxes') or [])
    calls = sum(item.get('calls_not_priced') or 0 for item in result.get('received') or [])
    parts = ([f"{sent} {'fax' if sent == 1 else 'faxes'}"] if sent else []) + (
        [f"{calls} {'call' if calls == 1 else 'calls'}"] if calls else [])
    if not parts:
        return None
    one = sent + calls == 1
    return (f"{' and '.join(parts)} {'is' if one else 'are'} not priced yet, so {'it is' if one else 'they are'} "
            "not in the total.")


def never_priced_sentence(who, count, unit):
    """A carrier that priced part of a call by the give-up time and never the rest; the console says the same."""
    many = count != 1
    noun = unit if not many else ('faxes' if unit == 'fax' else f'{unit}s')
    return (f"{who or 'Your carrier'} never priced {count} {noun} in full; only "
            f"{'their priced parts are' if many else 'its priced part is'} in the total.")


def _never_priced_lines(out, result):
    for item, unit in [*((item, 'fax') for item in result.get('providers') or []),
                       *((item, 'call') for item in result.get('received') or [])]:
        count = item.get('attempts_never_priced' if unit == 'fax' else 'calls_never_priced') or 0
        if count:
            out.line(never_priced_sentence(item.get('carrier'), count, unit))


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
                   'Not priced yet', 'Waiting for the bill', 'Could not be matched'],
                  [[_route_name(item), item['attempts'], item['successes'], item['billed_minutes'],
                    money(item['reported_cost']), _not_billed(item, 'attempts_without_reported_cost'),
                    item.get('attempts_not_priced', 0), item.get('awaiting_carrier_bill', 0),
                    item.get('unmatched_charges', 0)]
                   for item in result.get('providers', [])],
                  empty='No faxes sent in this period.')
        received = result.get('received') or []
        if received:
            out.table(['Received on', 'Calls', 'Faxes', 'Billed minutes', 'Charged', 'Estimated, not billed yet',
                       'Not priced yet', 'Waiting for the bill', 'Could not be matched'],
                      [[_route_name(item), item['calls'], item['faxes'], item['billed_minutes'],
                        money(item['reported_cost']), _not_billed(item, 'calls_without_reported_cost'),
                        item.get('calls_not_priced', 0), item['awaiting_carrier_bill'], item['unmatched_charges']]
                       for item in received])
        for item in result.get('received_faxes') or []:
            out.line(item['summary'])  # "Received faxes: Sinch $0.42 for 6 faxes."
        _unrecorded_lines(out, [*result.get('providers', []), *received])
        if result.get('total_cost'):
            out.line(f"Total: {money(result['total_cost'])} (charges, estimates for faxes not billed yet, and plan fees "
                     "counted once per 30 days, pro-rated by day).")
        not_priced = _not_priced_line(result)
        if not_priced:
            out.line(not_priced)
        _never_priced_lines(out, result)
        carrier = result.get('carrier_charges') or {}
        if carrier.get('supported') and not carrier.get('readable'):
            # Another carrier's sentence names what it needs; Telnyx's text names the command that saves its key.
            out.line(carrier['sentence'] if carrier.get('sentence') and carrier['carrier'] != 'Telnyx' else (
                f"{carrier['carrier']} call charges appear once a {carrier['carrier']} API key is saved: add it "
                f"in the console under Providers → {carrier['carrier']}, or run faxbot system settings set "
                "--secret telnyx_api_key."))
    state.out().result(result, human)


@routing.command('reconcile')
def routing_reconcile():
    """Ask your SIP trunk carrier now what each recent call cost. This never changes a fax's delivery result."""
    result = state.api().post('/routing/reconcile', json={})
    state.out().result(result, lambda out: out.line(result['summary']))


@routing.command('fax-cost')
def routing_fax_cost(fax_id: str = typer.Argument(None, help="Fax ID from 'faxbot sent list --ids' or, with --received, "
                                                            "from 'faxbot received list --ids'."),
                     received: bool = typer.Option(False, '--received', help='The fax is a received fax.'),
                     to: str = typer.Option(None, '--to', metavar='NUMBER',
                                            help='Instead of a sent fax: what a fax to this number would cost by each '
                                                 'account your rules allow.'),
                     pages: int = typer.Option(1, '--pages', min=1, max=1000, help='With --to: pages in the fax.'),
                     from_site: str = typer.Option(None, '--from-site', metavar='SITE',
                                                   help="With --to: price calls from this site's accounts first.")):
    """Show what one fax cost (the carrier's charge, or why it is not known yet), or with --to what one would cost."""
    if to:
        from .accounts import quote_command
        return quote_command(to=to, pages=pages, from_site=from_site)
    if not fax_id:
        raise CliError("Give a fax ID, or --to NUMBER for what a fax would cost.")
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


SAVING_PARTS = (('sending_together', 'Sending together'), ('separator_pages', 'Separator pages left out'),
                ('direct_delivery', 'Direct delivery'), ('direct_fax_images', 'Direct fax images'),
                ('case_packets', 'Case packets'), ('sslfax', 'Faster pages'), ('own_numbers', 'Faxes to your own numbers'),
                ('toll_free', 'Approved toll-free numbers'), ('packing', 'Pages saved by packing'),
                ('encoding', 'Pages saved by encoding (experimental)'))


def routing_savings(days: int = typer.Option(30, '--days', min=1, max=366, help='How many days back to count.')):
    """Show how much money Faxbot saved by batching faxes to the same number, delivering directly to partners, leaving out documents a recipient already has, and sending pages faster. All figures are estimates."""
    result = state.api().get('/routing/savings', params={'days': days})

    def human(out):
        total = result.get('total_saved') or []
        # The server's headline says honestly when something cost more than it saved.
        out.line(result.get('total_sentence') or (
            f"About {money(total)} saved in the last {result['days']} days." if total
            else f"No money saved in the last {result['days']} days, as far as Faxbot can tell."))
        out.table(['Saving', 'Estimate', 'What happened'],
                  [[title, money((result.get(key) or {}).get('saved')), (result.get(key) or {}).get('sentence') or '-']
                   for key, title in SAVING_PARTS])
        counted = (result.get('case_packets') or {}).get('counted_from_sentence')
        if counted:
            out.line(counted)
        if result.get('sentence'):
            out.line(result['sentence'])
        # Bytes partners did not need sent again: kept out of the money table, because they are not money.
        if (result.get('direct_bytes') or {}).get('sentence'):
            out.line('Bytes saved by reuse and patches: ' + result['direct_bytes']['sentence'])
    state.out().result(result, human)


# -- costs recommendations ----------------------------------------------------------------

def _read_sending(api):
    return api.get('/routing/recommendations/sending')


def _show_sending(out, result):
    items = result.get('items') or []
    if not items:
        out.line(result.get('empty_sentence') or 'Nothing to suggest yet.')
        return
    out.table(['Fax number', 'Name', 'Sent now by', 'Per delivered fax', 'Cheaper route', 'Per delivered fax',
               'Saves per fax'],
              [[item['number'], item.get('display_name'), item.get('current_label') or (item['current'] or {}).get('label'),
                (item['current'] or {}).get('cost_text') or '-',
                item['suggested']['label'], item['suggested']['cost_text'],
                money([item['saving_per_fax']]) if item.get('saving_per_fax') else '-']
               for item in items])
    for item in items:
        out.line(item['sentence'])
        out.line(f"To send by {item['suggested']['label']}: faxbot recipients set {item['number']} "
                 f"--preferred-route {item['suggested']['route']}")
    # A rule for a whole country, where one account was cheaper for several of its numbers.
    for country in result.get('country_rules') or []:
        out.line(country['sentence'])
        rule = country['rule_suggestion']
        out.line(f"To add it to your draft rules: faxbot providers rules add '{rule['name']}' "
                 f"--when to-country={country['country']} --use {country['route']}")


def _line_advice(row):
    if not row.get('eligible'):
        return row.get('reason') or 'Billed by the minute'
    return 'Shared lines' if row.get('in_pool') else 'Billed by the minute'


def print_receiving(out, result):
    """The receiving recommendations in words and tables; ``faxbot costs recommendations`` reuses this."""
    days = result.get('days', 30)
    pool = result.get('pool') or {}
    out.line(pool.get('sentence') or result.get('sentence') or '')
    for extra in (pool.get('action'), pool.get('note')):
        if extra:
            out.line(extra)
    if pool.get('numbers'):
        # A number with a call that has no price shows "Not priced yet", never $0.
        out.table(['Number', f'Calls, last {days} days', 'Billed by the minute (estimate)', 'Advice'],
                  [[row['number'], row['calls'],
                    'Not priced yet' if row.get('unpriced_calls') else money(row.get('billed_by_the_minute'), empty='$0.00'),
                    _line_advice(row)] for row in pool['numbers']],
                  title=f'Should your numbers share incoming lines? (estimate, last {days} days)')
    check, choose = pool.get('check'), pool.get('choose')
    if check and choose and pool.get('pool_numbers'):
        rows = [('Billed by the minute today', 'billed_by_the_minute'), ('Shared lines', 'channels'),
                ('Still billed by the minute', 'still_billed_by_the_minute'), ('Fax number rental', 'number_rental'),
                ('Total today', 'total_today'), ('Total with shared lines', 'total_with_pool')]
        out.table(['Estimate', f'The {days} days before', f'The last {days} days'],
                  [[label, money(choose.get(key), empty='$0.00'), money(check.get(key), empty='$0.00')]
                   for label, key in rows])
        out.line(f"Most calls at once in the last {days} days: {pool.get('needed', 0)}.")
    for stretch in pool.get('busy_windows') or []:
        count = stretch['turned_away']
        out.line(f"All shared lines busy from {local_time(stretch['start'])} to {local_time(stretch['end'])}: "
                 f"{count} {'caller' if count == 1 else 'callers'} would have heard a busy signal.")
    if pool.get('break_even'):
        out.line(pool['break_even'])
    for line in pool.get('assumptions') or []:
        out.line(line)
    quiet = result.get('quiet_numbers') or {}
    out.line('')
    out.line(quiet.get('sentence') or '')
    if quiet.get('numbers'):
        out.table(['Number', 'Received', 'Sent', 'Rental a month (estimate)'],
                  [[row['number'], row['received'], row['sent'], money(row.get('monthly_rental'))]
                   for row in quiet['numbers']], title=f'Numbers with few calls in the last {days} days')
    connections = result.get('connections') or {}
    out.line('')
    out.line(connections.get('sentence') or '')
    if len(connections.get('items') or []) > 1:
        out.table(['Fax service', 'Monthly fee'],
                  [[item['name'], money(item.get('monthly_fee'), empty='No price yet')]
                   for item in connections['items']])
    if result.get('prices'):
        out.table(['Price', 'Amount', 'Read on', 'Source'],
                  [[item.get('label'), item.get('text'), _read_on(item.get('read_on')), item.get('source_url')]
                   for item in result['prices']], title='Published prices used')


def _read_receiving(api):
    return api.get('/routing/recommendations/receiving')


def _read_plans(api):
    return api.get('/routing/recommendations/plans')


def show_plans(out, result):
    """Whether each monthly plan is worth its fee; every figure an estimate, next to its period."""
    plans = result.get('plans') or []
    if not plans:
        out.line(result.get('empty_sentence') or '')
        return
    days = result.get('days', 30)
    for index, plan in enumerate(plans):
        if index:
            out.line('')
        out.line(plan['sentence'])
        if plan.get('action'):
            out.line(plan['action'])
        for caveat in plan.get('caveats') or []:
            out.line(caveat)
        latest, before = (plan.get('windows') or [{}, {}])[:2]

        def cell(window, key, empty='-'):
            if key == 'number_rental' and window.get('number_rental_unpublished'):
                return 'Not published'  # the carrier publishes no price for keeping the number: unknown, not $0
            value = window.get(key)
            return money(value, empty=empty) if isinstance(value, list) else (empty if value is None else value)
        rows = [('Days Faxbot has records for', 'days'), ('Faxes sent', 'sent'), ('Faxes received', 'received'),
                ('Of these, test faxes to your own numbers', 'own_numbers'), ('Plan fee for this period', 'fee'),
                ('Plan fee per fax', 'fee_per_fax'), ('The same faxes another way', 'other_way'),
                ('Rent for the fax number at your carrier', 'number_rental')]
        out.table(['Estimate', f'Last {days} days', f'The {days} days before'],
                  [[label, cell(latest, key), cell(before, key)] for label, key in rows],
                  title=f"{plan['name']}, {money(plan.get('monthly_fee'))} a month")


def _read_fax_marker(api):
    return api.get('/routing/recommendations/fax-marker')


def show_fax_marker(out, result):
    """Calls marked as fax against calls not marked, from history; the setting never changes."""
    out.line(result.get('sentence') or '')
    if result.get('state') == 'compared':
        rows = [('Calls', 'calls'), ('Delivered', 'delivered_percent'), ('Used fax over IP (T.38)', 't38_percent'),
                ('Seconds a page', 'seconds_per_page'), ('Cost per delivered fax', 'cost_text')]

        def cell(side, key):
            value = (result.get(side) or {}).get(key)
            if value is None:
                return '-'
            return f'{value}%' if key.endswith('percent') else value
        out.table(['', 'Marked as fax', 'Not marked'], [[label, cell('marked', key), cell('not_marked', key)]
                                                         for label, key in rows],
                  title=f"Fax marker, last {result.get('days', 90)} days")
    for key in ('caveat', 'left_out_sentence', 'setting_sentence'):
        if result.get(key):
            out.line(result[key])


def _read_billing_steps(api):
    return api.get('/routing/recommendations/billing-steps')


def show_billing_steps(out, result):
    """Numbers whose calls end just past a billed minute, and what a shorter call would have saved."""
    out.line(result.get('sentence') or '')
    numbers = result.get('numbers') or []
    if numbers:
        out.table(['Fax number', 'Name', 'Calls', 'Just past a step', 'Seconds past', 'Would have saved (estimate)'],
                  [[item['number'], item.get('display_name'), item['calls'], item['calls_near'],
                    f"{item['seconds_past']['least']}–{item['seconds_past']['most']} s"
                    if item['seconds_past']['least'] != item['seconds_past']['most']
                    else f"{item['seconds_past']['least']} s", money([item['saving']])] for item in numbers])
    step = result.get('step') or {}
    if step.get('seconds'):
        from .trunk import local_date
        out.line(f"{result.get('carrier') or 'Your carrier'} bills in {step['seconds']}-second steps of "
                 f"{step['price_text']}, from your rate card dated {local_date(step.get('read_on'))}.")


def _read_partners(api):
    return api.get('/routing/recommendations/partners')


def show_partners(out, result):
    """Numbers whose faxes cost the most again and again, and how to enroll one as a direct partner."""
    out.line(result.get('sentence') or '')
    items = result.get('items') or []
    if items:
        out.table(['Fax number', 'Name', 'Faxes', 'Average pages', 'A month (estimate)'],
                  [[item['number'], item.get('display_name'), item['faxes'], item.get('average_pages') or '-',
                    money([item['monthly_cost']]) if item.get('monthly_cost') else item['cost_text']]
                   for item in items])
        for item in items:
            out.line(item['sentence'])
        out.line('To enroll a recipient as a direct partner, run faxbot recipients partners add.')


def _read_service_numbers(api):
    return api.get('/routing/recommendations/receiving')


def show_service_numbers(out, result):
    """Quiet numbers at every fax service: the carrier line's numbers and the HumbleFax and eFax numbers."""
    days = result.get('days', 30)
    quiet = result.get('quiet_numbers') or {}
    out.line(quiet.get('sentence') or '')
    if quiet.get('numbers'):
        out.table(['Number', 'Received', 'Sent', 'Rental a month (estimate)'],
                  [[row['number'], row['received'], row['sent'], money(row.get('monthly_rental'))]
                   for row in quiet['numbers']], title=f'Carrier numbers with few calls in the last {days} days')
        for row in quiet['numbers']:
            if row.get('question'):
                out.line(row['question'])
    services = result.get('provider_numbers') or {}
    out.line('')
    out.line(services.get('sentence') or '')
    if services.get('numbers'):
        out.table(['Fax service', 'Number', 'Received', 'Sent', 'Plan a month'],
                  [[row['name'], row['number'], row['received'], row['sent'], money(row.get('plan_fee'), empty='No price yet')]
                   for row in services['numbers']], title=f'Fax service numbers, last {days} days')
        for row in services['numbers']:
            out.line(row['sentence'])
            if row.get('question'):
                out.line(row['question'])


def _read_toll_free(api):
    return api.get('/routing/recommendations/toll-free')


def show_toll_free(out, result):
    """Recipients with a toll-free fax number on file, approved or not; the recipient pays for those calls."""
    out.line(result.get('sentence') or '')
    items = result.get('items') or []
    if items:
        out.table(['Fax number', 'Name', 'Toll-free number', 'Approved'],
                  [[item['number'], item.get('display_name'), item['alternate_display'],
                    'Yes' if item['approved'] else 'Not yet'] for item in items])
        for item in items:
            out.line(item['sentence'])
        out.line('To record an approval, run faxbot recipients toll-free approve.')


def _read_carriers(api):
    return api.get('/routing/recommendations/carriers')


def _left_out(row):
    """What a carrier's figure leaves out because it publishes no price for it."""
    parts = []
    if row.get('not_priced'):
        parts.append(f"{row['not_priced']} {'fax' if row['not_priced'] == 1 else 'faxes'}")
    if row.get('numbers_not_priced'):
        parts.append(f"{row['numbers_not_priced']} {'number' if row['numbers_not_priced'] == 1 else 'numbers'}")
    return ', '.join(parts) or '-'


def show_carriers(out, result):
    """What your last 30 days of faxing would have cost at each carrier's published prices. Advice only."""
    out.line(result.get('sentence') or '')
    rows = result.get('carriers') or []
    if rows:
        def name(row):
            notes = [note for note, on in (('yours', row.get('yours')), ('cheapest', row.get('cheapest'))) if on]
            return row['name'] + (f" ({', '.join(notes)})" if notes else '')
        out.table(['Carrier', 'Estimate', 'Left out (no published price)', 'Less than now', 'Prices read on'],
                  [[name(row), money(row.get('total')), _left_out(row), money(row.get('difference')),
                    _day(row.get('advertised_on'))] for row in rows],
                  title=f"Your last {result.get('days', 30)} days at each carrier's published prices")
        for row in rows:
            out.line(f"{row['name']}: {row['sentence']}")
    if result.get('unpublished_sentence'):
        out.line(result['unpublished_sentence'])
    out.line(result.get('switching_sentence') or '')


def _read_friendly(api):
    return api.get('/routing/recommendations/fax-friendly')


def show_friendly(out, result):
    """Lightening shaded areas and removing specks: with the setting at never, what it would have saved."""
    choice = result.get('choice')
    if result.get('sentence'):
        out.line(result['sentence'])
    elif choice in ('where_it_saves', 'always'):
        out.line('Nothing to suggest: shaded areas are lightened '
                 + ('where it saves time.' if choice == 'where_it_saves' else 'on every document.'))
    else:
        out.line('Faxbot has no recent faxes to check yet.')
    if result.get('action'):
        out.line(result['action'] + ' Or run: faxbot system settings set fax_friendly_documents=where_it_saves')


# Each section of `faxbot costs recommendations`: (key in --json output, heading, read(api), show(out, data)).
RECOMMENDATION_SECTIONS = [
    ('sending', 'Sending', _read_sending, _show_sending),
    ('receiving', 'Receiving', _read_receiving, print_receiving),
    ('plans', 'Plans', _read_plans, show_plans),
    ('fax_marker', 'Fax marker', _read_fax_marker, show_fax_marker),
    ('billing_steps', 'Billing steps', _read_billing_steps, show_billing_steps),
    ('partners', 'Partner candidates', _read_partners, show_partners),
    ('toll_free', 'Toll-free numbers', _read_toll_free, show_toll_free),
    ('carriers', 'Other carriers', _read_carriers, show_carriers),
    ('pages', 'Shaded areas and specks', _read_friendly, show_friendly),
    ('trunks', 'Your trunks', lambda api: _read_trunks(api), lambda out, result: show_trunks(out, result)),
    ('numbers', 'Where each number should live', lambda api: _number_advice().read_numbers(api),
     lambda out, result: _number_advice().show_numbers(out, result)),
    ('sites', 'Which site calls cost less from', lambda api: _number_advice().read_sites(api),
     lambda out, result: _number_advice().show_sites(out, result)),
]


def _number_advice():
    from . import number_advice
    return number_advice


def _read_trunks(api):
    return api.get('/routing/recommendations/trunks')


def show_trunks(out, result):
    """Each trunk's fee, lines, faxes and cost per delivered fax, then whether one's traffic fits on another."""
    trunks = result.get('trunks') or []
    if trunks:
        out.table(['Trunk', 'A month', 'Lines', 'Most at once', 'Faxes sent', 'Faxes received', 'Each sent fax'],
                  [[row['label'], row.get('monthly') or 'Not known', row['lines'], row['peak_lines'], row['sent'],
                    row['received'], row.get('cost_per_delivered') or '-'] for row in trunks],
                  empty='')
    for item in result.get('items') or []:
        out.line(item['sentence'])
    if result.get('sentence'):
        out.line(result['sentence'])
    if result.get('items'):
        out.line('This is advice only: Faxbot never cancels a trunk. To keep faxes off a trunk, add a sending limit '
                 "with 'faxbot providers rules add'.")

recommendations = typer.Typer(help='Ways to pay less, from what your faxes and calls actually cost. Run it alone for '
                                   'every section.', invoke_without_command=True)


@recommendations.callback()
def routing_recommendations(context: typer.Context):
    """Show ways to pay less: cheaper routes, shared incoming lines, whether each plan is worth its fee, the fax marker, calls that end just past a billed minute, partner candidates, toll-free numbers, what other carriers would have cost, how much time lightening shaded areas would save, whether one trunk's faxes fit on another, where each number should live, and which site's trunk costs less. Every figure is an estimate."""
    if context.invoked_subcommand is not None:
        return
    api = state.api()
    result = {key: read(api) for key, _, read, _ in RECOMMENDATION_SECTIONS}

    def human(out):
        for index, (key, heading, _, show) in enumerate(RECOMMENDATION_SECTIONS):
            if index:
                out.line()
            out.line(heading)
            show(out, result[key])
    state.out().result(result, human)


def _section(read, show):
    def command():
        result = read(state.api())
        state.out().result(result, lambda out: show(out, result))
    return command


for _name, _read, _show, _help in (
        ('sending', _read_sending, _show_sending,
         'Show numbers where another route cost less per delivered fax in the last 30 days.'),
        ('receiving', _read_receiving, print_receiving,
         'Show which numbers could share incoming lines, numbers with few calls, and your fax services\' monthly fees.'),
        ('plans', _read_plans, show_plans, 'Show whether each monthly plan is worth its fee at your traffic.'),
        ('fax-marker', _read_fax_marker, show_fax_marker,
         'Compare calls marked as fax with calls not marked: delivery, fax over IP (T.38), time and cost. Changes no '
         'setting.'),
        ('billing-steps', _read_billing_steps, show_billing_steps,
         'Show numbers whose calls end just past a billed minute, where one page less or a faster mode would have '
         'cost less.'),
        ('partners', _read_partners, show_partners,
         'Show the numbers whose faxes cost the most again and again: candidates to enroll as direct partners.'),
        ('service-numbers', _read_service_numbers, show_service_numbers,
         'Show quiet numbers at your carrier and at HumbleFax and eFax, with what each costs to keep.'),
        ('toll-free', _read_toll_free, show_toll_free,
         'Show recipients with a toll-free fax number on file and whether their approval is recorded.'),
        ('carriers', _read_carriers, show_carriers,
         "Show what your last 30 days of faxing would have cost at each carrier's published prices. Advice only: "
         'switching carriers means moving your numbers, and Faxbot never switches anything.'),
        ('shading', _read_friendly, show_friendly,
         'Show how much time lightening shaded areas and removing specks saved, or would save, on your recent '
         'faxes.'),
        ('trunks', _read_trunks, show_trunks,
         "Compare your trunks' monthly fees, busiest times and cost per fax, and show when one trunk's faxes fit on "
         'another and what that would save. Advice only.'),
        ('numbers', lambda api: _number_advice().read_numbers(api),
         lambda out, result: _number_advice().show_numbers(out, result),
         'Show where each of your fax numbers costs least to receive on, and the steps to move one. Advice only: '
         'Faxbot never moves a number.'),
        ('sites', lambda api: _number_advice().read_sites(api),
         lambda out, result: _number_advice().show_sites(out, result),
         "Show whether your carriers price US calls by state, and when another site's trunk would send faxes to a "
         'state for less. Advice only.')):
    recommendations.command(_name, help=_help)(_section(_read, _show))


# -- recipients toll-free -------------------------------------------------------------------------------

toll_free = typer.Typer(help="A recipient's toll-free fax number, used only after you record who at the recipient "
                             'agreed. The recipient pays for those calls.', no_args_is_help=True)


def _toll_free_human(view):
    def human(out):
        current = view.get('current') or {}
        out.line(view.get('sentence') or 'No toll-free number is on file for this recipient.')
        if view.get('history'):
            from .trunk import local_date
            out.table(['Recorded', 'Toll-free number', 'What', 'Who agreed', 'Day agreed', 'Evidence', 'Recorded by'],
                      [[local_time(row['recorded_at']), row['alternate_display'],
                        {'noted': 'On file', 'approved': 'Approved', 'withdrawn': 'Withdrawn'}[row['action']],
                        row.get('approved_by') or '-', local_date(row.get('approved_on')) if row.get('approved_on') else '-',
                        row.get('evidence') or '-', row.get('recorded_by_name') or '-'] for row in view['history']],
                      title='History')
        if current.get('action') == 'noted':
            out.line(f"To record the approval, run faxbot recipients toll-free approve {view['number']} "
                     f"{current['alternate_number']} --by NAME --on DATE --evidence TEXT.")
    return human


def _toll_free_post(number, body):
    view = state.api().post('/routing/destinations/' + segment(number) + '/toll-free', json=body)
    state.out().result(view, _toll_free_human(view))


@toll_free.command('show')
def toll_free_show(number: str = typer.Argument(..., help="The recipient's own fax number.")):
    """Show a recipient's toll-free fax number and every approval recorded for it."""
    view = state.api().get('/routing/destinations/' + segment(number) + '/toll-free')
    state.out().result(view, _toll_free_human(view))


@toll_free.command('note')
def toll_free_note(number: str = typer.Argument(..., help="The recipient's own fax number."),
                   toll_free_number: str = typer.Argument(..., metavar='TOLL_FREE',
                                                          help='The toll-free fax number the recipient publishes.')):
    """Put a recipient's toll-free fax number on file without approving it; Faxbot does not use it yet."""
    _toll_free_post(number, {'action': 'noted', 'alternate_number': toll_free_number})


@toll_free.command('approve')
def toll_free_approve(number: str = typer.Argument(..., help="The recipient's own fax number."),
                      toll_free_number: str = typer.Argument(..., metavar='TOLL_FREE',
                                                             help='The toll-free fax number the recipient approved.'),
                      by: str = typer.Option(..., '--by', help='Who at the recipient agreed, for example "Dana, intake lead".'),
                      on: str = typer.Option(..., '--on', metavar='DATE', help='The day they agreed, as 2026-10-03.'),
                      evidence: str = typer.Option(..., '--evidence',
                                                   help='Where the agreement is recorded, such as an email and its date.')):
    """Record that the recipient agreed to faxes on its toll-free number: who, when and the evidence. The recipient pays for those calls."""
    _toll_free_post(number, {'action': 'approved', 'alternate_number': toll_free_number, 'approved_by': by,
                             'approved_on': on, 'evidence': evidence})


@toll_free.command('withdraw')
def toll_free_withdraw(number: str = typer.Argument(..., help="The recipient's own fax number."),
                       evidence: str = typer.Option(None, '--evidence', help='Why, or where the withdrawal is recorded.')):
    """Withdraw a recipient's toll-free number; Faxbot sends to its own number again. The history is kept."""
    _toll_free_post(number, {'action': 'withdrawn', 'evidence': evidence})


@toll_free.command('lookup')
def toll_free_lookup(number: str = typer.Argument(..., help="The recipient's own fax number."),
                     npi: str = typer.Option(None, '--npi', help="The provider's ten-digit NPI."),
                     name: str = typer.Option(None, '--name', help='Or the organization name, with --city or --state.'),
                     city: str = typer.Option(None, '--city', help='City, with --name.'),
                     us_state: str = typer.Option(None, '--state', help='Two-letter state, with --name.')):
    """Look up toll-free fax numbers the NPI registry (NPPES) lists for a provider. It suggests only; nothing is approved."""
    params = {key: value for key, value in (('npi', npi), ('name', name), ('city', city), ('state', us_state)) if value}
    result = state.api().get('/routing/destinations/' + segment(number) + '/toll-free/suggestions', params=params)

    def human(out):
        out.line(result.get('sentence') or '')
        out.table(['Toll-free fax number', 'Where', 'Address', 'Evidence'],
                  [[item['fax_display'], item.get('address_purpose') or '-', item.get('address') or '-',
                    item.get('evidence') or '-'] for item in result.get('items') or []], empty='')
        if result.get('items'):
            out.line(f"To record an approval, run faxbot recipients toll-free approve {result['number']} "
                     f"{result['items'][0]['fax_number']} --by NAME --on DATE --evidence TEXT.")

def _line_time(seconds):
    """'about 52 s' for the time column; '-' when unknown."""
    if seconds is None:
        return '-'
    whole = int(round(seconds))
    return f'about {whole // 60} min {whole % 60} s' if whole >= 60 else f'about {whole} s'


def _predicted_cost(route):
    if route.get('cost') is None:
        return 'Unknown'
    amount = money([route['cost']])
    if not route.get('marginal'):
        return amount
    return 'Nothing extra' if float(route['cost']['amount']) == 0 else f'{amount} extra'


def routing_predict(to: str = typer.Option(..., '--to', help='Fax number to price, for example +12025550123.'),
                    pages: int = typer.Option(1, '--pages', min=1, max=1000, help='Pages in the fax.'),
                    layout: str = typer.Option('normal', '--layout',
                                               help='normal, or dense for pages packed with more text.'),
                    resolution: str = typer.Option('fine', '--resolution',
                                                   help='standard, fine, superfine, 300 or 400.')):
    """Show what a fax to a number would take and cost on each of your sending routes, before sending it. All figures are estimates; nothing is sent."""
    result = state.api().get('/routing/predict', params={'to': to, 'pages': pages, 'layout': layout,
                                                         'resolution': resolution})

    def human(out):
        out.line(f"{result['to']} is {result['number_class_text']}; {result['pages']} "
                 f"page{'' if result['pages'] == 1 else 's'}.")
        routes = result.get('routes') or []
        if routes:
            out.table(['Route', 'Cost', 'Time on the line'],
                      [[route['label'], _predicted_cost(route), _line_time(route.get('seconds'))] for route in routes])
            for route in routes:
                out.line(f"{route['label']}: {route['basis']}")
        out.line(result['sentence'])
        out.line(result['note'])
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
    from .trunk import local_date

    def human(out):
        # Money as money and the provider's name, as Costs → Prices & plans shows them.
        out.table(
            ['Provider', 'Name', 'For', 'Per minute', 'Per page', 'Per call', 'Monthly', 'Billed in steps of',
             'Advertised on'],
            [[card.get('provider_name') or card['provider_id'], card['label'],
              'Receiving' if card.get('direction') == 'inbound' else 'Sending',
              _card_price(card, 'per_minute'), _card_price(card, 'per_page'), _card_price(card, 'per_call'),
              (f"{_monthly(card)}, faxes included" if card.get('included_in_plan')
               else _monthly(card) if card.get('monthly_fee') else '-'),
              _billing_step(card['billing_increment_seconds']), local_date(card['captured_on'])]
             for card in result.get('cards', [])], empty='No rate cards.')
        # Each sending card's prices by where calls start (origin-rated rows), with source and date.
        from .accounts import origin_rows
        for card in result.get('cards', []):
            if card.get('rows'):
                out.line()
                origin_rows(out, card)
        # What each sending route publishes about calling a recipient's approved toll-free number.
        if result.get('toll_free'):
            out.line()
            out.table(['Provider', 'Calls to toll-free numbers', 'Price', 'Caller ID it needs', 'Advertised on'],
                      [[item.get('provider_name') or item['provider_id'], item['reach_text'], item['price_text'],
                        item.get('caller_id_text') or '-', local_date(item['advertised_on'])]
                       for item in result['toll_free']], title='Calls to toll-free numbers')
    state.out().result(result, human)


@routing.command('rate-rows')
def routing_rate_rows(route: str = typer.Argument(..., metavar='ROUTE',
                                                  help="The sending card's route, as 'faxbot costs rate-cards' lists "
                                                       'it, such as sip-gamma or sinch-uk.'),
                      replace: str = typer.Option(..., '--replace', metavar='FILE',
                                                  help='Your prices by where calls start for that card, from this JSON '
                                                       'file ({"rows": [...]}, or \'-\' for standard input).')):
    """Replace the prices by where calls start that you entered for one sending card. Earlier rows are kept as history."""
    try:
        document = json.loads(_read_document(replace))
    except ValueError:
        raise CliError('The file is not valid JSON.') from None
    if isinstance(document, list):
        document = {'rows': document}
    from urllib.parse import quote
    result = state.api().put(f'/routing/rate-cards/{quote(route.strip(), safe="")}/rows', json=document)
    from .accounts import origin_rows
    state.out().result(result, lambda out: origin_rows(out, {'label': route.strip(), 'rows': result.get('rows') or []})
                       if result.get('rows') else out.line('No prices by where calls start for this card.'))


# -- routing batching --------------------------------------------------------------------

def _batching_human(view):
    def human(out):
        agreement = view.get('agreement') or {}
        marks = view.get('boundaries_agreement') or {}
        labels = {choice['value']: choice['label'] for choice in view.get('boundaries_choices') or []}
        out.line(view['state_sentence'])
        out.line(view['route_sentence'])
        if view.get('boundaries_sentence'):
            out.line(view['boundaries_sentence'])
        out.fields([('Fax number', view['number']), ('Sending together', 'on' if view['enabled'] else 'off'),
                    ('Longest wait', f"{view['max_wait_minutes']} minutes"),
                    ('Most pages in one call', view['max_pages']),
                    ('Different senders may share a call', view['mixed_senders']),
                    ('Recipient agreement recorded by', agreement.get('by')),
                    ('Recorded', local_time(agreement.get('at')) if agreement else None),
                    ('Each document is marked by', labels.get(view.get('boundaries'))),
                    ('Agreement to that recorded by', marks.get('by')),
                    ('Agreement to that recorded', local_time(marks.get('at')) if marks else None)])
        out.line(view['savings']['sentence'])
        if (view['savings'].get('separator_pages') or {}).get('calls'):
            out.line(view['savings']['separator_pages']['sentence'])
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


# -- costs plans: budgets, the contract view and published plans ---------------------------------

from typer.core import TyperGroup  # noqa: E402


class _PlansGroup(TyperGroup):
    """`faxbot costs plans efax` and `faxbot costs plans --in-use` still list published plans.

    A first word that is not one of the group's commands goes to `published`, so
    the older command keeps working beside `show` and `budget`.
    """
    def parse_args(self, ctx, args):
        if not args or (args[0] not in self.commands and args[0] not in ('--help', '-h')):
            args = ['published', *args]
        return super().parse_args(ctx, args)


plans = typer.Typer(cls=_PlansGroup, help="Your plans: each plan's budget or allowance this month and what is "
                                          'committed (show), setting a budget (budget), and the plans a fax service '
                                          'publishes (published, or name the service: faxbot costs plans efax).')
plans.command('published')(routing_plans)


def _day(value):
    """'15 Oct' for a date the server sends as YYYY-MM-DD."""
    from datetime import date
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return text(value)
    return f'{day.day} {day:%b}'


def _ordinal(day):
    """'The 1st', 'The 22nd'"""
    suffix = 'th' if 11 <= day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    return f'The {day}{suffix}'


def _count(value, empty='-'):
    return empty if value is None else f'{value:,}'


def show_contract(out, result, *, burn_down=False):
    """Each plan this billing period: its budget or allowance, what is left, what is committed; all estimates."""
    plans_ = result.get('plans') or []
    if not plans_:
        out.line(result.get('empty_sentence') or '')
        return
    for index, plan in enumerate(plans_):
        if index:
            out.line('')
        budget, used, left, period = plan['budget'], plan['used'], plan['left'], plan['period']
        fee = money(plan.get('monthly_fee'), empty='')
        out.line(plan['sentence'])
        rows = [['Pages sent and received', _count(used['pages'])], ['Faxes sent and received', _count(used['faxes'])]]
        if budget.get('pages') is not None or budget.get('faxes') is not None:
            rows.append(['Normal-use budget a month', ' and '.join(
                part for part in (f"{budget['pages']:,} pages" if budget.get('pages') is not None else '',
                                  f"{budget['faxes']:,} faxes" if budget.get('faxes') is not None else '') if part)])
            rows.append(['Left of the budget', ' and '.join(
                part for part in (f"{max(0, left['pages']):,} pages" if left.get('pages') is not None else '',
                                  f"{max(0, left['faxes']):,} faxes" if left.get('faxes') is not None else '')
                if part)])
        if budget.get('included_pages'):
            rows.append(['Pages the plan includes', _count(budget['included_pages'])])
            rows.append(['Included pages left', _count(max(0, left['allowance']))])
            rows.append(['Price of each extra page', money(budget.get('page_overage'), empty='Not known')])
        if budget.get('included_minutes'):
            rows.append(['Minutes the plan includes', _count(budget['included_minutes'])])
            rows.append(['Minutes used', _count(used.get('minutes'))])
        if budget.get('commitment'):
            rows.append(['Monthly commitment', money(budget['commitment'])])
            rows.append(['Spent so far', money(used.get('spend'), empty='Not known')])
        overage = plan.get('overage') or {}
        if overage.get('pages') or overage.get('minutes'):
            rows.append(['Past the allowance so far', 'Not known' if overage.get('cost_unknown')
                         else money(overage.get('cost'))])
        rows.append(['Committed this period', money(plan.get('committed'), empty='Nothing')])
        rows.append(['Bill so far', money(plan.get('bill_so_far'), empty='Not known')])
        rows.append(['Billing day', _ordinal(budget['day'])])
        rows.append(['Counts start again', _day(period.get('next_day'))])
        out.table(['Estimate', 'This period'], rows,
                  title=f"{plan['name']}" + (f', {fee} a month' if fee else ''))
        for sentence in (budget.get('sentence'), plan.get('pace_sentence'), plan.get('bill_sentence'),
                         plan.get('count_sentence'), plan.get('untimed_sentence')):
            if sentence:
                out.line(sentence)
        for row in plan.get('own_accounts') or []:
            out.line(row['sentence'])
        if burn_down and plan.get('burn_down'):
            out.table(['Day', 'Pages so far', 'Faxes so far'],
                      [[_day(row['date']), _count(row['pages']), _count(row['faxes'])] for row in plan['burn_down']],
                      title=f"{plan['name']}, day by day since {_day(period.get('first_day'))}")
    out.line('')
    out.line('To change a budget, run faxbot costs plans budget <plan>, for example faxbot costs plans budget '
             'humblefax --pages 500.')


@plans.command('show')
def plans_show(burn_down: bool = typer.Option(False, '--by-day', help='Also show the pages and faxes carried each day '
                                                                       'of this billing period.')):
    """Show each plan's normal-use budget or allowance this billing period, what is committed, and faxes between your own accounts. Every figure is an estimate."""
    result = state.api().get('/routing/plans')
    state.out().result(result, lambda out: show_contract(out, result, burn_down=burn_down))


def _limit(value, name):
    """A whole number for a budget, or None for 'none' (no limit)."""
    if value is None:
        return None
    word = value.strip().lower()
    if word in ('none', 'no-limit'):
        return 'none'
    if not word.isdigit() or not 0 < int(word) <= 1_000_000:
        raise CliError(f'Give {name} as a whole number from 1 to 1,000,000, or none.')
    return int(word)


@plans.command('budget')
def plans_budget(plan: str = typer.Argument(..., help='The plan, for example humblefax or efax; the carrier trunk '
                                                      'is sip.'),
                 pages: str = typer.Option(None, '--pages', metavar='COUNT', help='Normal-use pages a month, or none for no limit.'),
                 faxes: str = typer.Option(None, '--faxes', metavar='COUNT', help='Normal-use faxes a month, or none for no limit.'),
                 billing_day: int = typer.Option(None, '--billing-day', min=1, max=31,
                                                 help="The day of the month the plan's counts start again."),
                 included_pages: str = typer.Option(None, '--included-pages', metavar='COUNT',
                                                    help='Pages the plan includes each month, or none.'),
                 page_overage: str = typer.Option(None, '--page-overage', metavar='PRICE',
                                                  help='The price of each page past them, such as 0.10.'),
                 included_minutes: str = typer.Option(None, '--included-minutes', metavar='COUNT',
                                                      help='Minutes the plan includes each month, or none.'),
                 commitment: str = typer.Option(None, '--commitment', metavar='AMOUNT',
                                                help='A monthly amount you have committed to spend, such as 50.'),
                 default: bool = typer.Option(False, '--default',
                                              help="Go back to Faxbot's starting budget for this plan.")):
    """Set a plan's monthly normal-use budget, allowance or commitment, and the day its counts start again. Faxbot never changes the plan itself."""
    from ...routing.plan_budget import InvalidBudget, parse_budgets, with_entry
    from ..settings_write import write_settings
    api = state.api()
    view = api.get('/routing/plans')
    current = view.get('plan_budgets') or ''
    changes = {'pages': _limit(pages, '--pages'), 'faxes': _limit(faxes, '--faxes'), 'day': billing_day,
               'included_pages': _limit(included_pages, '--included-pages'), 'page_overage': page_overage,
               'included_minutes': _limit(included_minutes, '--included-minutes'), 'commitment': commitment}
    given = {key: value for key, value in changes.items() if value is not None}
    if default and given:
        raise CliError('Use --default on its own: it goes back to the starting budget.')
    if not default and not given:
        raise CliError('Give at least one of --pages, --faxes, --billing-day, --included-pages, --page-overage, '
                       '--included-minutes or --commitment, or --default.')
    try:
        key = plan.strip().lower()
        known = dict(parse_budgets(current).get('sip' if key.startswith('sip') else key) or {})
        if not default:
            # Only the values given change; the rest keep what you set before, else Faxbot's starting values.
            entry_text = ','.join(f'{name}={value}' for name, value in given.items())
            parsed = parse_budgets(f'{key}:{entry_text}')
            known.update(next(iter(parsed.values())))
        updated = with_entry(current, key, None if default else known)
    except InvalidBudget as error:
        raise CliError(str(error)) from None
    write_settings(api, {'plan_budgets': updated})
    result = api.get('/routing/plans')
    state.out().result(result, lambda out: show_contract(out, result))


# -- intake ------------------------------------------------------------------------------

def _emailed_to(item):
    """Who a delivered email went to, as stored when the email server accepted it."""
    if item.get('state') != 'delivered':
        return '-'
    if item.get('delivered_to'):
        return ', '.join(item['delivered_to'])
    return 'Not recorded' if item.get('recipients_recorded') is False else '-'


@intake.command('items')
def intake_items(state_filter: str = typer.Option(None, '--state', help='received, sending, delivered or failed.'),
                 limit: int = typer.Option(100, '--limit', min=1, max=500, help='How many to show.'),
                 ids: bool = typer.Option(False, '--ids', help="Also show each delivery's ID, to use with faxbot received deliveries retry.")):
    """List received documents and their delivery, newest first."""
    result = state.api().get('/intake/items', params={'state': state_filter, 'limit': limit})

    def human(out):
        out.table((['Item ID'] if ids else []) + ['Received', 'From', 'To', 'Pages', 'Delivery', 'Emailed to',
                                                  'Status', 'Needs action'],
                  [([item['id']] if ids else []) + [local_time(item['received_at']), item.get('from_number'),
                                                    item.get('to_number'), item.get('pages'), item.get('connector'),
                                                    _emailed_to(item), item['status'], item['needs_action']]
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
        raise typer.Exit(EXIT_FAILURE)  # the result above already says what failed, in --json too


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
    state.out().result(items, lambda out: out.table(['Partner', 'Fax number', 'State', 'Status', 'Fax images', 'Verified'],
        [[item['organization'], item['fax_number'], item['state'], item['status'], item.get('fax_images_text') or '-',
          local_time(item.get('verified_at'), empty='-')] for item in items], empty='No partners.'))


@peers.command('fax-images')
def peers_fax_images(partner: str = typer.Argument(..., help='Partner organization, fax number or id.'),
                     choice: str = typer.Argument(..., metavar='on|off',
                                                  help="on (the default) accepts the partner's faxes as the exact fax "
                                                       'image, filed like any received fax; off accepts only original '
                                                       'documents.')):
    """Accept faxes from a partner as the exact fax image (on, the default) or only as original documents (off)."""
    if choice not in ('on', 'off'):
        raise typer.BadParameter('Use on or off.', param_hint='on|off')
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/fax-images", json={'accept': choice == 'on'})
    state.out().result(result, lambda out: out.line(result['detail']))


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
    state.out().result(items, lambda out: out.table(['When', 'Sent or received', 'Partner', 'Fax number', 'Status'],
        [[local_time(item['created_at']), 'Received' if item['direction'] == 'inbound' else 'Sent', item.get('partner'),
          item.get('fax_number'), item['status']] for item in items], empty='No direct deliveries yet.'))


# -- case packets ---------------------------------------------------------------------------------

@cases.command('list')
def cases_list(limit: int = typer.Option(50, '--limit', min=1, max=200, help='How many cases to show.')):
    """List the newest cases you sent packets for: who received them, what was delivered and acknowledged, and when."""
    result = state.api().get('/cases', params={'limit': limit})
    items = result.get('cases') or []
    state.out().result(result, lambda out: out.table(
        ['Case', 'Recipient', 'Documents', 'Pages', 'Last sent'],
        [[item['case_id'], item['to'],
          f"{item['documents']} sent, {item.get('sent', 0)} delivered, {item['accepted']} acknowledged", item['pages'],
          local_time(item.get('last_sent_at'))] for item in items],
        empty='No case packets sent yet.'))


@cases.command('documents')
def cases_documents(case_id: str = typer.Argument(..., help='Your case reference.'),
                    to: str = typer.Option(..., '--to', help='Recipient fax number.'),
                    ids: bool = typer.Option(False, '--ids', help='Also show the fax ID each document was last sent in.')):
    """List the documents of a case sent to a recipient: delivered, acknowledged, too old, or not found by them."""
    from .cases import detail, state_text
    result = state.api().get(f'/cases/{segment(case_id)}/documents', params={'to': to})

    def human(out):
        out.line('This recipient accepts a one-page list instead of documents it acknowledged.'
                 if result.get('accepts_references') else 'This recipient wants every document in full.')
        if result.get('reuse_days') == 0:
            out.line('Its acknowledgements are trusted with no time limit.')
        else:
            out.line(f"Its acknowledgements are trusted for {result.get('reuse_days')} days.")
        out.table(['Document', 'Version and source', 'Purpose', 'Pages', 'State', 'Reference']
                  + (['Fax ID'] if ids else []),
                  [[item['title'], detail(item), item.get('purpose') or '-', item['pages'], state_text(item),
                    item['reference']] + ([item.get('fax_id')] if ids else [])
                   for item in result.get('documents', [])],
                  empty='Nothing has been sent for this case to this recipient.')
    state.out().result(result, human)


@cases.command('send')
def cases_send(case_id: str = typer.Argument(..., help='Your case reference.'),
               to: str = typer.Argument(..., help='Recipient fax number.'),
               files: list[Path] = typer.Argument(..., exists=True, dir_okay=False, readable=True,
                                                  help='PDF documents for the packet, in order.'),
               title: list[str] = typer.Option(None, '--title', help='Title for each document, in the same order. '
                                                                     'Default: the file name.'),
               purpose: str = typer.Option('', '--purpose', help='What the packet is for. The same document sent '
                                           'for another purpose is sent in full.'),
               source: list[str] = typer.Option(None, '--source', help='Where each document came from, in order.'),
               version: list[str] = typer.Option(None, '--version', help='Version of each document, in order.'),
               kind: list[str] = typer.Option(None, '--type', help='Document type of each document, in order.'),
               date: list[str] = typer.Option(None, '--date', help='The date on each document, in order, as '
                                                                   'year-month-day.'),
               preview: bool = typer.Option(False, '--preview', help='Show what would be sent without sending.')):
    """Send a case packet, listing documents the recipient acknowledged instead of sending them again."""
    from .cases import WHY_TEXT
    with ExitStack() as stack:
        uploads = [('documents', (path.name, stack.enter_context(path.open('rb')), 'application/pdf'))
                   for path in files]
        data = {'to': to, 'preview': 'true' if preview else 'false', 'titles': list(title or []), 'purpose': purpose,
                'sources': list(source or []), 'versions': list(version or []), 'types': list(kind or []),
                'dates': list(date or [])}
        result = state.api().post(f'/cases/{segment(case_id)}/faxes', data=data, files=uploads)

    def human(out):
        out.table(['Document', 'Pages', 'In this packet', 'Why'],
                  [[item['title'], item['pages'], 'included' if item['status'] == 'included' else 'listed only',
                    WHY_TEXT.get(item.get('why'), '-')] for item in result.get('documents', [])])
        out.line(f"{result['pages']} pages to send, {result['pages_saved']} pages saved.")
        if result.get('fax_id'):
            out.line(f"Sent as fax {result['fax_id']}. Check on it with: faxbot status {result['fax_id']}")
        else:
            out.line('Preview only; nothing was sent.')
    state.out().result(result, human)
