"""Line windows and preflight with partners that take part (``api/app/direct/admission.py``, ``preflight.py``).

Its home is ``faxbot recipients partners windows`` (registered in ``cli/nouns.py``)."""
from __future__ import annotations

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

windows = typer.Typer(help='Line windows and preflight with partners that take part: ask before paying to send by '
                           'phone, and queue for a partner\'s limited lines instead of retrying into a busy one.',
                      no_args_is_help=True)


def _view(api):
    return api.get('/direct/admission')


def _partner(view, reference):
    wanted = reference.strip().casefold()
    matches = [item for item in view['partners'] if item['peer_id'] == reference
               or item['organization'].casefold() == wanted]
    if len(matches) != 1:
        raise CliError(f"No single partner matches '{reference}'. See 'faxbot recipients partners list'.")
    return matches[0]


def _show_partner(out, item):
    out.line(f"{item['organization']}: {'takes part' if item['take_part'] else 'does not take part'}.")
    if item['refused_classes']:
        out.line(f"You refuse {', '.join(item['refused_classes'])} faxes from them: {item['refusal_reason']}")
    out.line(item['advice'])
    for grant in item['grants']:
        side = 'You granted' if grant['direction'] == 'issued' else 'You received'
        if grant['outcome'] == 'granted':
            out.line(f"{side} a {grant['duration_class']} window from {local_time(grant['earliest'])} to "
                     f"{local_time(grant['latest'])}.")
        else:
            out.line(f"{side} no window ({grant['outcome'].replace('_', ' ')}), {local_time(grant['created_at'])}.")
    for answer in item['preflights']:
        said = {1: 'accept', 0: 'refuse'}.get(answer['accepts'], 'did not say whether they take')
        who = 'They' if answer['direction'] == 'asked' else 'You'
        out.line(f"{who} {said} {answer['document_class']} faxes"
                 f"{' at ' + answer['mailbox'] if answer['mailbox'] else ''}"
                 f"{': ' + answer['reason'] if answer['reason'] and answer['accepts'] == 0 else ''} "
                 f"({local_time(answer['created_at'])}).")


@windows.command('show')
def windows_show(partner: str = typer.Argument(None, metavar='[PARTNER]', help='Show one partner only.')):
    """Show your receiving settings and, for each partner, whether you take part, its windows and its answers."""
    view = _view(state.api())

    def show(out):
        if partner:
            _show_partner(out, _partner(view, partner))
            return
        for line in (view['lines_sentence'], view['policy_sentence'], view['legacy']['sentence'],
                     view['not_a_receipt']):
            out.line(line)
        for item in view['partners']:
            _show_partner(out, item)
    state.out().result(view, show)


@windows.command('settings')
def windows_settings(lines: str = typer.Option(None, '--lines', metavar='N|trunk',
                                               help='Lines you offer partners; trunk uses the trunk\'s lines less '
                                                    'the reserve.'),
                     reserve: int = typer.Option(None, '--reserve', min=0, max=500,
                                                 help='Lines kept for callers that don\'t take part.'),
                     window: int = typer.Option(None, '--window', min=1, max=240, metavar='MINUTES',
                                                help='How long a window stays open.'),
                     urgent: str = typer.Option(None, '--urgent', metavar='go|wait',
                                                help='go: urgent faxes start without a window; wait: they wait too.'),
                     deadline: str = typer.Option(None, '--deadline', metavar='go|wait',
                                                  help='go: a fax that would miss its send-by time starts without a '
                                                       'window; wait: it still waits.')):
    """Set what you offer partners that take part: lines, the reserve for other callers, the window and the policy
    for urgent faxes and deadlines."""
    for name, value in (('--urgent', urgent), ('--deadline', deadline)):
        if value is not None and value not in ('go', 'wait'):
            raise typer.BadParameter('Use go or wait.', param_hint=name)
    api = state.api()
    current = _view(api)['settings']
    body = dict(current)
    if lines is not None:
        if lines == 'trunk':
            body['lines'] = None
        elif lines.isdigit():
            body['lines'] = int(lines)
        else:
            raise typer.BadParameter('Use a number or trunk.', param_hint='--lines')
    for key, value in (('reserve', reserve), ('window_minutes', window), ('urgent', urgent), ('deadline', deadline)):
        if value is not None:
            body[key] = value
    result = api.put('/direct/admission/settings', json=body)
    state.out().result(result, lambda out: (out.line(result['lines_sentence']), out.line(result['policy_sentence'])))


@windows.command('partner')
def windows_partner(partner: str = typer.Argument(..., help='Partner organization or id.'),
                    choice: str = typer.Argument(..., metavar='on|off', help='on: take part with this partner.'),
                    refuse: list[str] = typer.Option(None, '--refuse', metavar='CLASS',
                                                     help='A document class you refuse from them; repeat it.'),
                    reason: str = typer.Option(None, '--reason', help='The reason they see for a refusal.')):
    """Take part with a partner (on) or stop (off), and the document classes you refuse from it."""
    if choice not in ('on', 'off'):
        raise typer.BadParameter('Use on or off.', param_hint='on|off')
    api = state.api()
    item = _partner(_view(api), partner)
    result = api.put('/direct/admission/partners/' + segment(item['peer_id']),
                     json={'take_part': choice == 'on', 'refused_classes': list(refuse or item['refused_classes']),
                           'refusal_reason': reason if reason is not None else item['refusal_reason']})
    state.out().result(result, lambda out: _show_partner(out, _partner(result, item['peer_id'])))
