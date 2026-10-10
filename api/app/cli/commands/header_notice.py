"""faxbot delivery identity notice: the notice line printed at the top of every page you send.

The console's Delivery setup → Sending identity, Header notice. A sender may then mark a fax's first page as a
cover sheet whose notice goes in that line instead (`faxbot send --cover-in-header`), so the page is not
sent. Reads need settings:read; changes need settings:write.
"""
import typer

from .. import resolve, state
from ..client import segment

notice = typer.Typer(help='A notice line, such as a confidentiality notice, printed at the top of every page you '
                          'send, so a cover sheet carrying it can stay unsent.', no_args_is_help=True)

MAILBOX = typer.Option(None, '--mailbox', help='A mailbox, for faxes sent from it; leave out for every fax.')


@notice.command('show')
def notice_show():
    """Show the notice your faxes carry, and each mailbox's own."""
    data = state.api().get('/header-notice')

    def human(out):
        organization = data.get('organization')
        out.line(f"Every page carries: {organization['notice']}" if organization
                 else 'Your faxes carry no header notice.')
        if data.get('mailboxes'):
            out.table(['Mailbox', 'Notice'], [[item['mailbox'], item['notice']] for item in data['mailboxes']],
                      title='Mailboxes with their own notice')
    state.out().result(data, human)


def _save(mailbox, text):
    api = state.api()
    if mailbox:
        found = resolve.mailbox(api, mailbox)
        result = api.put('/header-notice/mailboxes/' + segment(found['id']), json={'notice': text})
    else:
        result = api.put('/header-notice', json={'notice': text})
    state.out().result(result, lambda out: out.line(result.get('sentence') or ''))


@notice.command('set')
def notice_set(text: str = typer.Argument(..., metavar='NOTICE',
                                          help='One line of up to 120 characters, such as "Confidential: for the '
                                               'addressee only. If you received this in error, call us."'),
               mailbox: str = MAILBOX):
    """Print this notice at the top of every page (or of every page sent from one mailbox)."""
    _save(mailbox, text)


@notice.command('clear')
def notice_clear(mailbox: str = MAILBOX):
    """Stop printing the notice (a mailbox's faxes then carry the organization's notice, if any)."""
    _save(mailbox, '')
