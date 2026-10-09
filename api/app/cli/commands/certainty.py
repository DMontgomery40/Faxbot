"""Sent faxes whose outcome is uncertain: who owns each, the checks ranked by cost, and settling them."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError, EXIT_NOT_FOUND
from ..output import local_time
from .fax import _report_saved, save_document
from .work import _person, short_time


RESULT_WORDS = {'delivered': 'Delivered (signed)', 'not_delivered': 'Not delivered (signed)',
                'partial': 'Partly delivered (signed)', 'probably_not_delivered': 'Probably not delivered',
                'probably_partial': 'Probably only partly delivered', 'consistent': 'Fits a delivered fax',
                'asking': 'Asking', 'unavailable': 'Not available', 'unknown': 'Cannot tell yet',
                'not_done': 'Not done yet', 'sent': 'Sent'}
OUTCOME_WORDS = {'delivered': 'delivered', 'not_delivered': 'not delivered', 'unknown': "can't tell"}
FAX_ID_HELP = "Fax ID of the sent fax, from 'faxbot sent uncertain --ids' or 'faxbot sent list --ids'."


def state_sentence(item):
    """The server's sentence, with any time in it shown in local time."""
    if item.get('state_key') == 'assigned' and item.get('due_at'):
        owner = (item.get('owner') or {}).get('name') or 'someone'
        return f"Assigned to {owner}; settle by {short_time(item['due_at'])}."
    return item.get('state_text') or '-'


def _item(api, fax_id, *, open_only=False):
    """The newest uncertain item of a sent fax (or the item with this ID)."""
    found = api.get('/certainty/faxes/' + segment(fax_id))
    items = found.get('items') or []
    if open_only:
        items = [item for item in items if item.get('state') == 'open'] or items
    if items:
        return items[0]
    about = found.get('about')
    if about:
        kind = 'a new fax sent again' if about.get('kind') == 'resend' else 'the receipt query'
        raise CliError(f"This fax is {kind} for fax {about['fax_id']}; check that one instead.", EXIT_NOT_FOUND)
    try:
        return api.get('/certainty/items/' + segment(fax_id))
    except CliError as failure:
        if failure.status != 404:
            raise
    raise CliError('Faxbot is sure what happened to this fax: there is nothing to settle.', EXIT_NOT_FOUND)


def uncertain_list(mine: bool = typer.Option(False, '--mine', help='Only faxes you own.'),
                   unassigned: bool = typer.Option(False, '--unassigned', help='Only faxes nobody owns yet.'),
                   overdue: bool = typer.Option(False, '--overdue', help='Only faxes past the time to settle them.'),
                   settled: bool = typer.Option(False, '--settled', help='Faxes already settled, instead of open ones.'),
                   limit: int = typer.Option(100, '--limit', min=1, max=200, help='How many faxes to show.'),
                   ids: bool = typer.Option(False, '--ids', help='Also show each fax ID, for probe, settle and assign.')):
    """List sent faxes whose outcome Faxbot could not confirm, with their owner and the time to settle each."""
    chosen = [name for name, flag in (('mine', mine), ('unassigned', unassigned), ('overdue', overdue)) if flag]
    if len(chosen) > 1:
        raise CliError('Choose one of --mine, --unassigned or --overdue.')
    api = state.api()
    result = api.get('/certainty/items', params={'view': chosen[0] if chosen else 'all', 'limit': limit,
                                                 'state': 'settled' if settled else 'open'})
    counts = api.get('/certainty/counts')
    items = result.get('items', [])

    def human(out):
        if not settled:
            number = counts.get('open') or 0
            out.line(f"{number} sent {'fax needs' if number == 1 else 'faxes need'} settling; "
                     f"{counts.get('overdue') or 0} overdue.")
        out.table((['Fax ID'] if ids else []) + ['To', 'Sent', 'Pages', 'Why', 'Owner', 'State'],
                  [([item['fax_id']] if ids else []) + [item.get('to_number'), local_time(item.get('sent_at')),
                                                         item.get('pages') or '-', item.get('why'),
                                                         (item.get('owner') or {}).get('name') or '-',
                                                         state_sentence(item)] for item in items],
                  empty='No uncertain sent faxes.')
    state.out().result({**result, 'counts': counts}, human)


def uncertain_probe(fax_id: str = typer.Argument(..., help=FAX_ID_HELP),
                    history: bool = typer.Option(False, '--history', help='Also show everything that happened.'),
                    query_pdf: str = typer.Option(None, '--query-pdf', metavar='FILE',
                                                  help="Save the one-page receipt query to check it before sending. "
                                                       "Use '-' for standard output."),
                    send_query: bool = typer.Option(False, '--send-query',
                                                    help='Fax the one-page receipt query to the recipient now.'),
                    force: bool = typer.Option(False, '--force', help='With --query-pdf: replace the file.')):
    """Show what Faxbot found out about a sent fax it is unsure of: the checks, cheapest first, and what each means."""
    api = state.api()
    item = _item(api, fax_id)
    if query_pdf:
        response = api.get(f"/certainty/items/{segment(item['id'])}/receipt-query", raw=True,
                           headers={'Accept': 'application/pdf'})
        _report_saved(save_document(response, query_pdf, f"receipt-query-{item['reference']}.pdf", force),
                      len(response.content))
        return
    if send_query:
        item = api.post(f"/certainty/items/{segment(item['id'])}/receipt-query", json={'version': item['version']})
    events = api.get(f"/certainty/items/{segment(item['id'])}/history").get('events', []) if history else []

    def human(out):
        if send_query:
            out.line('Receipt query sent. When the recipient faxes it back, settle this fax.')
        out.fields([('To', item.get('number') or item.get('to_number')), ('Sent', local_time(item.get('sent_at'))),
                    ('Pages', item.get('pages')), ('Reference', item.get('reference')), ('Why', item.get('why')),
                    ('Owner', (item.get('owner') or {}).get('name')), ('State', state_sentence(item)),
                    ('Settle by', local_time(item.get('due_at')))])
        out.table(['Check', 'Cost', 'Found', 'What it means'],
                  [[check['title'], check['cost'], RESULT_WORDS.get(check['result'], check['result']),
                    ' '.join(part for part in (check.get('text'), check.get('meaning')) if part)]
                   for check in item.get('checks') or []], empty='No checks.')
        phone = next((check for check in item.get('checks') or [] if check['kind'] == 'phone_call'), None)
        if item.get('state') == 'open' and phone and phone.get('script'):
            out.line('Phone script:')
            for line in phone['script']:
                out.line('  ' + line)
        npi = next((check for check in item.get('checks') or [] if check['kind'] == 'npi_lookup'), None)
        if npi and npi.get('source_url'):
            out.line(f"NPI registry: {npi['source_url']}")
        if (item.get('moved_on') or {}).get('text'):
            out.line(item['moved_on']['text'])
        if item.get('suggestion'):
            out.line(f"The checks point to {OUTCOME_WORDS[item['suggestion']]}; you decide with: "
                     f"faxbot sent settle {item['fax_id']}")
        if events:
            out.table(['When', 'What happened'], [[local_time(event['at']), event['text']] for event in events])
    state.out().result({**item, 'history': events} if history else item, human)


def uncertain_settle(fax_id: str = typer.Argument(..., help=FAX_ID_HELP),
                     delivered: bool = typer.Option(False, '--delivered', help='It arrived.'),
                     not_delivered: bool = typer.Option(False, '--not-delivered', help='It did not arrive.'),
                     cant_tell: bool = typer.Option(False, '--cant-tell', help='You cannot find out.'),
                     reason: str = typer.Option(..., '--reason', help='How you know, for example who you spoke to '
                                                                      '(up to 400 characters).'),
                     send_again: bool = typer.Option(False, '--send-again',
                                                     help='With --not-delivered: send the same document again now, '
                                                          'as a new fax linked to this one.')):
    """Settle what happened to a sent fax Faxbot was unsure of. Faxbot records who decided and why."""
    chosen = [name for name, flag in (('delivered', delivered), ('not_delivered', not_delivered),
                                      ('unknown', cant_tell)) if flag]
    if len(chosen) != 1:
        raise CliError('Choose one of --delivered, --not-delivered or --cant-tell.')
    if send_again and chosen[0] != 'not_delivered':
        raise CliError('--send-again goes with --not-delivered.')
    api = state.api()
    item = _item(api, fax_id, open_only=True)
    result = api.post(f"/certainty/items/{segment(item['id'])}/settle", json={
        'outcome': chosen[0], 'reason': reason, 'version': item['version'], 'send_again': send_again})

    def human(out):
        out.line(state_sentence(result))
        if result.get('resend_fax_id'):
            out.line(f"New fax: {result['resend_fax_id']}. Check on it with: faxbot status {result['resend_fax_id']}")
    state.out().result(result, human)


def uncertain_assign(fax_id: str = typer.Argument(..., help=FAX_ID_HELP),
                     user: str = typer.Argument(..., help='The new owner: their login or name.')):
    """Give a sent fax Faxbot is unsure of to the person who will settle it. They must already be able to see it."""
    api = state.api()
    item = _item(api, fax_id, open_only=True)
    people = api.get(f"/certainty/items/{segment(item['id'])}/assignees").get('people', [])
    person = _person(people, user, where='this fax')
    result = api.post(f"/certainty/items/{segment(item['id'])}/assign", json={'principal_id': person['id'],
                                                                            'version': item['version']})
    state.out().result(result, lambda out: out.line(state_sentence(result)))


def uncertain_settings(hours: int = typer.Option(None, '--hours', min=0, max=720,
                                                 help='Hours to settle an uncertain fax, from when Faxbot finds it '
                                                      '(0 for no deadline).'),
                       fallback: str = typer.Option(None, '--fallback',
                                                    help='The person who settles uncertain faxes when the sender '
                                                         'cannot: their login or name.'),
                       no_fallback: bool = typer.Option(False, '--no-fallback', help='Remove the fallback person.')):
    """Show or change how soon uncertain sent faxes should be settled, and who settles them when the sender cannot."""
    api = state.api()
    current = api.get('/certainty/settings')
    if fallback and no_fallback:
        raise CliError('Choose --fallback or --no-fallback.')
    if hours is not None or fallback or no_fallback:
        person = current.get('fallback') or {}
        chosen = person.get('id')
        if fallback:
            chosen = _person(current.get('people') or [], fallback, where='every sent fax')['id']
        elif no_fallback:
            chosen = None
        current = api.put('/certainty/settings', json={
            'fallback_principal_id': chosen, 'settle_hours': current['settle_hours'] if hours is None else hours,
            'version': current['version']})

    def human(out):
        hours_value = current.get('settle_hours') or 0
        out.line(f'Settle each uncertain sent fax within {hours_value} hours of Faxbot finding it.'
                 if hours_value else 'Uncertain sent faxes have no time to settle them by.')
        person = (current.get('fallback') or {}).get('name')
        out.line(f'When the sender cannot see the fax, {person} settles it.' if person else
                 "When the sender cannot see the fax, the backup person of the mailbox it was sent from settles it; "
                 'with neither, it waits for you to assign it.')
    state.out().result(current, human)
