"""faxbot numbers reply: the number your faxes show, so replies reach you on your cheapest number.

The console's Numbers → Sender identity, Reply number. Reads need settings:read; changes need
settings:write and are checked like the console's: the number must be yours, receive into
Faxbot and reach the mailbox (for a mailbox's own number, that mailbox).
"""
import typer

from .. import resolve, state
from ..client import segment

reply = typer.Typer(help='The number printed on the faxes you send, so replies reach you on your cheapest number.',
                    no_args_is_help=True)

MAILBOX = typer.Option(None, '--mailbox', help='A mailbox, for faxes sent from it; leave out for every fax.')


@reply.command('show')
def reply_show():
    """Show the number your faxes show, why, what caller ID each provider shows, and which number is cheapest."""
    data = state.api().get('/numbers/reply')

    def human(out):
        out.line(data['sentence'])
        for problem in data.get('problems') or []:
            out.line(problem)
        if data.get('header_problem'):
            out.line(data['header_problem'])
        if data.get('suggestion'):
            out.line(data['suggestion']['sentence'])
        for item in data.get('avoid') or []:
            out.line(item['sentence'])
        for item in data.get('caller_id') or []:
            out.line(item['sentence'])
        if data.get('mailboxes'):
            out.table(['Mailbox', 'Reply number', 'Problem'],
                      [[item['mailbox'], item['number'], item['problem'] or '-'] for item in data['mailboxes']],
                      title='Mailboxes with their own reply number')
    state.out().result(data, human)


@reply.command('numbers')
def reply_numbers():
    """List your numbers with the mailbox each reaches and what receiving on it costs."""
    data = state.api().get('/numbers/reply')
    rows = [[item['number'], item['provider'], item['mailbox'] or 'No mailbox',
             'Yes' if item['receives'] else 'No', item['price']] for item in data['candidates']]
    state.out().result(data['candidates'], lambda out: out.table(
        ['Number', 'Provided by', 'Mailbox', 'Receives here', 'Receiving cost'], rows, empty='No numbers yet.'))


@reply.command('set')
def reply_set(number: str = typer.Argument(..., help='Your fax number that replies should reach.'),
              mailbox: str = MAILBOX):
    """Print this number on every fax (or on faxes from one mailbox) and send it as the station ID."""
    api = state.api()
    if mailbox:
        found = resolve.mailbox(api, mailbox)
        result = api.put('/numbers/reply/mailboxes/' + segment(found['id']), json={'number': number})
        state.out().result(result, lambda out: out.line(
            f"Faxes from {found['label']} now show {result['number']}."))
        return
    result = api.put('/numbers/reply', json={'number': number})
    state.out().result(result, lambda out: out.line(f"Faxes now show {result['number']}."))


@reply.command('clear')
def reply_clear(mailbox: str = MAILBOX):
    """Let Faxbot choose the number again (or give a mailbox's faxes the organization's number)."""
    api = state.api()
    if mailbox:
        found = resolve.mailbox(api, mailbox)
        result = api.delete('/numbers/reply/mailboxes/' + segment(found['id']))
        state.out().result(result, lambda out: out.line(
            f"Faxes from {found['label']} show the organization's reply number again."))
        return
    result = api.put('/numbers/reply', json={'number': ''})
    state.out().result(result, lambda out: out.line(
        'Faxbot now chooses the number: your cheapest number to receive on that reaches a mailbox.'))


# The notice line printed beside the reply number at the top of every page (header_notice.py).
from .header_notice import notice as _notice  # noqa: E402

reply.add_typer(_notice, name='notice')
