"""Send only the pages of a sent fax that its broken call did not confirm, as a new fax linked to it."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError


FAX_ID_HELP = "Fax ID of the sent fax whose call broke part way, from 'faxbot faxes sent list --ids'."


def continue_fax(fax_id: str = typer.Argument(..., help=FAX_ID_HELP),
                 send: bool = typer.Option(False, '--send',
                                           help='Send the remaining pages now, as a new fax linked to this one.'),
                 reason: str = typer.Option(None, '--reason',
                                            help='How you know the rest did not arrive (up to 400 characters). '
                                                 'Needed when the fax is waiting to be settled.')):
    """Show which pages of a broken fax are left to send and what they cost; send only those with --send."""
    api = state.api()
    view = api.get('/continuations/faxes/' + segment(fax_id))
    offer = view.get('offer') or {}
    result = view
    if send:
        if not offer.get('available'):
            raise CliError(offer.get('reason') or 'This fax has no remaining pages to send.')
        body = {'first_page': offer['first_page']}
        if reason:
            body['reason'] = reason
        if offer.get('open_item_id'):
            if not reason:
                raise CliError('This fax is waiting to be settled. Add --reason with how you know the rest did not '
                               'arrive, for example who you spoke to.')
            item = api.get('/certainty/items/' + segment(offer['open_item_id']))
            body['version'] = item['version']
        result = api.post('/continuations/faxes/' + segment(fax_id), json=body)

    def human(out):
        continues = result.get('continues')
        if continues:
            out.line(f"This fax carries {continues['pages_text']} of fax {continues['fax_id']}, whose call broke.")
        sent = result.get('continued_by')
        if sent:
            out.line(f"{sent['pages_text'][0].upper()}{sent['pages_text'][1:]} went as a new fax: {sent['fax_id']}. "
                     f"Check on it with: faxbot status {sent['fax_id']}")
            return
        found = result.get('offer')
        if not found:
            if not continues:
                out.line('This fax did not break part way through a call, so there are no remaining pages to send.')
            return
        if not found.get('available'):
            out.line(found.get('reason') or 'Its remaining pages cannot be sent from here.')
            return
        out.fields([('Send', found.get('pages_text')), ('Why these pages', found.get('basis')),
                    ('Cost', found.get('cost_text'))])
        out.line(found.get('warning') or '')
        if not found.get('may_send'):
            out.line('Only the person who sent this fax, or someone who may confirm receipt of it, can send them.')
        elif found.get('open_item_id'):
            out.line(f'Send them with: faxbot faxes sent continue {fax_id} --send --reason "<how you know>"')
        else:
            out.line(f'Send them with: faxbot faxes sent continue {fax_id} --send')
    state.out().result(result, human)
