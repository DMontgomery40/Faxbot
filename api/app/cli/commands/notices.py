"""Partners: notice faxes, documents sent in pieces, and fax calls completed directly after they broke."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time
from .delivery import _peer


DIRECTION = {'outbound': 'Sent', 'inbound': 'Received'}


def notice_fax(partner: str = typer.Argument(..., help='Partner organization, fax number or ID.'),
               choice: str = typer.Argument(..., metavar='on|off',
                                            help="on sends each document to the partner directly with a one-page "
                                                 "notice by fax, for a fax intake that needs a fax event; off (the "
                                                 "default) sends documents directly with no fax.")):
    """Send each document to a partner directly with a one-page notice by fax (on), or with no fax (off)."""
    if choice not in ('on', 'off'):
        raise typer.BadParameter('Use on or off.', param_hint='on|off')
    api = state.api()
    peer = _peer(api, partner)
    result = api.post(f"/direct/peers/{segment(peer['id'])}/notice-fax", json={'on': choice == 'on'})
    state.out().result(result, lambda out: out.line(result['detail']))


def notices_list(ids: bool = typer.Option(False, '--ids', help='Also show the ID to pair a notice with.')):
    """List notice faxes sent and received, and whether each was paired with its document."""
    items = state.api().get('/direct/notices')['notices']
    headers = ['When', 'Sent or received', 'Partner', 'Code', 'Status'] + (['ID'] if ids else [])
    state.out().result(items, lambda out: out.table(headers, [
        [local_time(item['created_at']), DIRECTION[item['direction']], item.get('partner') or '-', item['code'],
         item['status']] + ([item['id']] if ids else []) for item in items], empty='No notice faxes yet.'))


def notice_faxes(notice: str = typer.Argument(..., help="The notice's ID, from 'faxbot recipients partners notices --ids'.")):
    """List the received faxes of one or two pages that may be a held document's notice page, newest first."""
    items = state.api().get(f'/direct/notices/{segment(notice)}/faxes')['faxes']
    state.out().result(items, lambda out: out.table(['Received', 'From', 'Pages', 'Fax ID'], [
        [local_time(item['received_at']), item.get('from_number') or '-', item.get('pages') or '-', item['id']]
        for item in items], empty='No received fax could be its notice page yet.'))


def notice_pair(notice: str = typer.Argument(..., help="The notice's ID, from 'faxbot recipients partners notices --ids'."),
                code: str = typer.Option(None, '--code', help='The 20-digit code printed on the notice page.'),
                fax: str = typer.Option(None, '--fax', help="The received fax that is the notice, from "
                                                            "'faxbot received list --ids'."),
                without_notice: bool = typer.Option(False, '--without-notice',
                                                    help='File the document in Received without its notice fax.')):
    """Pair a document a partner delivered directly with its notice fax, or file it without one."""
    if not (code or fax or without_notice):
        raise CliError('Give the code from the notice page (--code), the received fax (--fax), or --without-notice.')
    if without_notice and (code or fax):
        raise CliError('Use --without-notice on its own.')
    body = {key: value for key, value in (('code', code), ('fax_id', fax)) if value}
    result = state.api().post(f'/direct/notices/{segment(notice)}/pair', json=body)
    state.out().result(result, lambda out: out.line(result['detail']))


def transfers_list():
    """List documents sent to and received from partners in pieces, and how many pieces each side holds."""
    items = state.api().get('/direct/transfers')['transfers']
    state.out().result(items, lambda out: out.table(['When', 'Sent or received', 'Partner', 'Status'], [
        [local_time(item['created_at']), DIRECTION[item['direction']], item.get('partner') or '-', item['status']]
        for item in items], empty='No documents sent in pieces yet.'))


def repairs_list():
    """List fax calls with partners that broke part way, and how each was completed directly."""
    items = state.api().get('/direct/repairs')['repairs']
    state.out().result(items, lambda out: out.table(['When', 'Sent or received', 'Partner', 'Status'], [
        [local_time(item['created_at']), DIRECTION[item['direction']], item.get('partner') or '-', item['status']]
        for item in items], empty='No broken calls with partners.'))


def received_line(api, inbound_id):
    """For a received fax that is a paired notice: which document it announced, or None."""
    try:
        return api.get('/direct/notices', params={'fax': inbound_id}).get('notice_text')
    except CliError:
        return None
