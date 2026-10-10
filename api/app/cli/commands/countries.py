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
