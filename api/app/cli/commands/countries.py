"""Country commands: rate decks priced by the caller ID a call shows, and the caller IDs you confirmed.

The deck import is part of ``faxbot costs rate-rows ROUTE --caller-id-deck FILE``; the caller-ID commands hang off
``faxbot providers trunk``. Nothing here changes a caller ID or contacts a carrier.
"""
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from .trunk import local_date, trunk


FOR_TYPE = {'local': 'for local caller IDs', 'eea': 'for EEA caller IDs', 'non_surcharged': 'for listed caller IDs',
            'surcharged': 'for any other caller ID'}


def _deck_lines(out, deck):
    kinds = ', '.join(f"{count:,} {FOR_TYPE.get(kind, kind)}" for kind, count in
                      sorted((deck.get('by_type') or {}).items()))
    out.line(f"{deck['route']}: {deck['rows']:,} prices by caller ID ({kinds}), published or read "
             f"{local_date(deck.get('published_on')) or 'on an unknown date'}.")
    if deck.get('source_url'):
        out.line(f"Source: {deck['source_url']}")


def import_caller_id_deck(route, path: Path, *, deck_format=None, source=None, published=None):
    """``faxbot costs rate-rows ROUTE --caller-id-deck FILE``: import a carrier's deck priced by caller ID."""
    if deck_format is not None and deck_format not in ('twilio', 'faxbot'):
        raise CliError('Choose the deck layout twilio or faxbot.')
    data = {key: value for key, value in (('deck_format', deck_format), ('source_url', source),
                                          ('published_on', published)) if value}
    with path.open('rb') as handle:
        result = state.api().post(f'/routing/rate-cards/{segment(route.strip())}/caller-id-prices', data=data,
                                  files={'file': (path.name, handle, 'text/csv')})

    def human(out):
        _deck_lines(out, result['deck'])
        out.line(result.get('terms') or '')
        if result.get('skipped_count'):
            out.line(f"{result['skipped_count']:,} lines were not read:")
            for reason in result.get('skipped') or []:
                out.line(f'  {reason}')
    state.out().result(result, human)


@trunk.command('caller-ids')
def caller_ids(quote: str = typer.Option(None, '--quote', metavar='NUMBER',
                                         help='Show how a fax to this number is priced by caller ID on each sending '
                                              'account.')):
    """The caller ID each sending account's calls show, what you confirmed about it, and the rate decks priced by
    caller ID."""
    api = state.api()
    if quote:
        result = api.get('/routing/caller-id-prices/quote', params={'to': quote})

        def human(out):
            if not result.get('quotes'):
                out.line(f"No rate deck priced by caller ID covers {result['number']}.")
            for item in result.get('quotes') or []:
                out.line(f"{item['label']}: {item['sentence']}")
        state.out().result(result, human)
        return
    result = api.get('/routing/caller-id-prices')

    def human(out):
        out.table(['Account', 'Caller ID', 'Confirmed', 'Bought on this account', 'Evidence'],
                  [[item['label'], item.get('caller_id') or '-',
                    _confirmed(item.get('eligibility')) if item.get('priced_by_caller_id') else 'Not needed',
                    'Yes' if (item.get('eligibility') or {}).get('bought_here') else '-',
                    (item.get('eligibility') or {}).get('evidence') or '-']
                   for item in result.get('callers') or []], empty='No sending accounts.')
        for item in result.get('callers') or []:
            out.line(f"{item['label']}: {item['sentence']}")
        out.line()
        if not result.get('decks'):
            out.line("No rate deck priced by caller ID. Import one with 'faxbot costs rate-rows ROUTE --caller-id-deck "
                     "FILE'.")
        for deck in result.get('decks') or []:
            _deck_lines(out, deck)
        out.line(result['layouts']['telnyx'])
    state.out().result(result, human)


def _confirmed(record):
    if not record:
        return 'Not yet'
    return 'Yes' if record.get('state') == 'confirmed' else 'Withdrawn'


@trunk.command('confirm-caller-id')
def confirm_caller_id(account: str = typer.Argument(..., metavar='ACCOUNT',
                                                    help="The sending account, by its key from 'faxbot providers "
                                                         "accounts list'."),
                      number: str = typer.Argument(..., metavar='CALLER_ID',
                                                   help='The caller ID with its country code, such as +442079460000.'),
                      evidence: str = typer.Option(..., '--evidence', metavar='TEXT',
                                                   help='How you know you may send from it on this account, such as '
                                                        'the number order or invoice.'),
                      evidence_url: str = typer.Option(None, '--evidence-url', metavar='URL',
                                                       help='A link to that evidence.'),
                      bought_here: bool = typer.Option(False, '--bought-here',
                                                       help='The number was bought on this account, so the carrier '
                                                            'prices calls from it to its own country as local.')):
    """Confirm that you hold a caller ID and may send faxes from it on one account, so calls from it get the rate
    its carrier gives that caller ID."""
    result = state.api().post('/routing/caller-ids/confirm', json={
        'account': account.strip(), 'caller_id': number.strip(), 'evidence': evidence, 'evidence_url': evidence_url,
        'bought_here': bought_here})
    state.out().result(result, lambda out: out.line(
        f"Confirmed {result['eligibility']['caller_id']} on {result['eligibility']['account']}"
        + (', bought on this account.' if result['eligibility']['bought_here'] else '.')))


@trunk.command('registered-senders')
def registered_senders():
    """Recipients that recognise your faxes by the number they come from, and the trunk registered with each."""
    result = state.api().get('/routing/sender-pins')

    def human(out):
        out.table(['Recipient', 'Trunk', 'Caller ID', 'Station ID', 'Now'],
                  [[item['recipient'], item['account_label'], item['caller_id'], item['station_id'] or '-',
                    'Ready' if item['ready'] else 'Faxes wait in Sent']
                   for item in result.get('pins') or []], empty='No registered senders.')
        for item in result.get('pins') or []:
            out.line(item['sentence'])
    state.out().result(result, human)


@trunk.command('register-sender')
def register_sender(recipient: str = typer.Argument(..., metavar='RECIPIENT',
                                                    help="The recipient's fax number, such as +902122220000."),
                    caller_id: str = typer.Option(..., '--caller-id', metavar='NUMBER',
                                                  help='The caller ID registered with the recipient.'),
                    station_id: str = typer.Option(None, '--station-id', metavar='TEXT',
                                                   help='The station ID registered with it, if it differs from the '
                                                        'caller ID.'),
                    account: str = typer.Option('sip', '--account', metavar='KEY',
                                                help="The trunk registered with it, by its key from 'faxbot "
                                                     "providers accounts list'; the first trunk when left out."),
                    note: str = typer.Option('', '--note', metavar='TEXT', help='Where it is registered, for the '
                                                                                'history.')):
    """Send faxes to RECIPIENT only from the trunk, caller ID and station ID registered with it. When that trunk
    cannot send them, they wait in Sent; they never go from another number."""
    body = {'account': account.strip(), 'caller_id': caller_id.strip(), 'station_id': station_id, 'note': note}
    result = state.api().put(f'/routing/sender-pins/{segment(recipient.strip())}', json=body)
    state.out().result(result, lambda out: out.line(next(
        (item['sentence'] for item in result.get('pins') or [] if item['recipient'] == recipient.strip()), 'Saved.')))


@trunk.command('unregister-sender')
def unregister_sender(recipient: str = typer.Argument(..., metavar='RECIPIENT', help="The recipient's fax number."),
                      note: str = typer.Option('', '--note', metavar='TEXT', help='Why, for the history.')):
    """Stop pinning RECIPIENT to one trunk; faxes to it go by your sending rules again. The history is kept."""
    result = state.api().delete(f'/routing/sender-pins/{segment(recipient.strip())}', params={'note': note})
    state.out().result(result, lambda out: out.line(f'Faxes to {recipient.strip()} go by your sending rules again.'))


@trunk.command('sender-evidence')
def sender_evidence(fax_id: str = typer.Argument(..., metavar='FAX_ID', help='The sent fax.'),
                    original: str = typer.Option(None, '--original', metavar='requested|sent|cancelled',
                                                 help="Record that the recipient asked for the original, that you "
                                                      'sent it, or that the request was withdrawn.'),
                    note: str = typer.Option('', '--note', metavar='TEXT', help='What happened, for the history.')):
    """The sender's evidence for a fax to a registered-sender recipient: the identity registered then, the kept
    pages, each call with the station that answered, and any request for the original."""
    api = state.api()
    if original:
        result = api.post(f'/routing/faxes/{segment(fax_id.strip())}/original', json={'state': original,
                                                                                       'note': note})
    else:
        result = api.get(f'/routing/faxes/{segment(fax_id.strip())}/sender-evidence')

    def human(out):
        pin = result['pin']
        out.line(f"Sent to {result['recipient']}, registered with {pin['caller_id']} on {pin['account']}.")
        out.line('Kept: ' + (', '.join(result.get('kept') or []) or 'no files') + '.')
        out.table(['Caller ID shown', 'Answered by', 'Pages', 'Result'],
                  [[call['caller_id'] or '-', call['answering_station'] or '-', call['pages'] or 0,
                    call['fax_status'] or call['disposition']] for call in result.get('calls') or []],
                  empty='No call records.')
        if result.get('original'):
            out.line({'requested': 'The recipient asked for the original.', 'sent': 'The original was sent.',
                      'cancelled': 'The request for the original was withdrawn.'}[result['original']])
    state.out().result(result, human)


@trunk.command('own-access')
def own_access(addresses: str = typer.Argument(None, metavar='ADDRESSES',
                                               help="Your Telekom line's internet addresses or ranges, "
                                                    "comma-separated; '' clears them.")):
    """For Telekom CompanyFlex: the internet address of your Telekom line. On any other access Faxbot encrypts the
    calls and sends audio fax by itself, as CompanyFlex requires."""
    api = state.api()
    if addresses is not None:
        from ..settings_write import write_settings
        write_settings(api, {'sip_trunk_own_access': addresses.strip()})
    trunk_view = ((api.get('/admin/settings').get('sip') or {}).get('trunk') or {})
    result = {'own_access': trunk_view.get('own_access') or '', 'access': trunk_view.get('access'),
              'media_encryption': trunk_view.get('media_encryption'),
              'sentence': trunk_view.get('encryption_sentence')}

    def human(out):
        out.line(f"Your own line: {result['own_access'] or 'not listed'}.")
        if result['sentence']:
            out.line(result['sentence'])
    state.out().result(result, human)


@trunk.command('withdraw-caller-id')
def withdraw_caller_id(account: str = typer.Argument(..., metavar='ACCOUNT', help='The sending account.'),
                       number: str = typer.Argument(..., metavar='CALLER_ID', help='The confirmed caller ID.'),
                       note: str = typer.Option('', '--note', metavar='TEXT', help='Why, for the history.')):
    """Withdraw a caller-ID confirmation; calls from it are priced as unconfirmed again. The history is kept."""
    result = state.api().post('/routing/caller-ids/withdraw', json={
        'account': account.strip(), 'caller_id': number.strip(), 'note': note})
    state.out().result(result, lambda out: out.line(
        f"Withdrawn: calls from {result['eligibility']['caller_id']} on {result['eligibility']['account']} are priced "
        'as not confirmed.'))
