"""The California court profile (``api/app/court``): where it applies, which courts take fax filing, and a court
fax's transmission record. Its home is ``faxbot delivery rules court``; send a court fax with
``faxbot send NUMBER FILE --court details.json``."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

court = typer.Typer(help='The California court profile: faxes filed with a court or served on a party go exactly as '
                         'given, with the MC-005 cover sheet when filing, and keep their transmission record.',
                    no_args_is_help=True)


def _profile(api):
    return api.get('/court/profile')


def _target(profile, kind, name):
    """The mailbox or routing rule ID for a name or ID the person typed."""
    items = profile['mailboxes'] if kind == 'mailbox' else profile['routing_rules']
    label = 'label' if kind == 'mailbox' else 'name'
    wanted = name.strip().casefold()
    matches = [item for item in items if item['id'] == name or item[label].casefold() == wanted]
    if len(matches) != 1:
        what = 'mailbox' if kind == 'mailbox' else 'routing rule'
        raise CliError(f"No single {what} is called '{name}'. Run faxbot delivery rules court show to see them.")
    return matches[0]['id']


@court.command('show')
def court_show():
    """Show where the court profile applies, what it does, and the rules and holidays it rests on."""
    found = _profile(state.api())

    def show(out):
        out.line(found['summary'])
        if found['attached']:
            for item in found['attached']:
                out.line(item['sentence'])
        else:
            out.line('It applies to no mailbox or routing rule yet; a sender can still choose it for one fax with '
                     'faxbot send --court.')
        out.line(found['trend'])
        for rule in found['rules']:
            out.line(f"{rule['rule']}: {rule['says']} ({rule['url']}, read {rule['read_on']})")
        out.line(f"Court holidays counted for {', '.join(str(year) for year in found['holidays']['years'])}, from "
                 f"{found['holidays']['source']['url']} (read {found['holidays']['source']['read_on']}).")
    state.out().result(found, show)


@court.command('attach')
def court_attach(kind: str = typer.Argument(..., metavar='mailbox|rule', help='Attach it to a mailbox or a routing '
                                            'rule.'),
                 name: str = typer.Argument(..., help='The mailbox or routing rule, by its name or ID.')):
    """Send every fax from a mailbox, or whose route a routing rule chooses, under the court profile. Their senders
    must then give the case details."""
    _change(kind, name, True)


@court.command('detach')
def court_detach(kind: str = typer.Argument(..., metavar='mailbox|rule', help='A mailbox or a routing rule.'),
                 name: str = typer.Argument(..., help='The mailbox or routing rule, by its name or ID.')):
    """Stop sending a mailbox's faxes, or a routing rule's, under the court profile."""
    _change(kind, name, False)


def _change(kind, name, on):
    if kind not in ('mailbox', 'rule'):
        raise typer.BadParameter('Use mailbox or rule.', param_hint='mailbox|rule')
    api = state.api()
    target = _target(_profile(api), kind, name)
    result = api.put('/court/profile/attachments', json={'kind': kind, 'target': target, 'on': on})

    def show(out):
        if not result.get('changed'):
            out.line('Nothing changed.')
        for item in result['attached']:
            out.line(item['sentence'])
        if not result['attached']:
            out.line('The court profile applies to no mailbox or routing rule now.')
    state.out().result(result, show)


@court.command('courts')
def court_courts(status: str = typer.Option(None, '--status', metavar='accepts|stopped|does_not_accept',
                                            help='Only courts in this state.')):
    """List which California superior courts take direct fax filing, which stopped and which refuse it, each with
    its source."""
    found = _profile(state.api())
    items = [item for item in found['courts'] if status is None or item['status'] == status]
    words = {'accepts': 'Takes fax filing', 'stopped': 'Stopped', 'does_not_accept': 'Refuses'}

    def show(out):
        out.line(found['trend'])
        out.table(['County', 'State', 'Fax number', 'Cut-off', 'What its rule says', 'Source'],
                  [[item['county'], words[item['status']], item['fax'] or '-', item['cutoff'] or '-', item['note'],
                    item['source']] for item in items], empty='No courts.')
        out.line(f"Not established either way: {', '.join(found['not_established'])}.")
    state.out().result(items, show)


@court.command('deadline')
def court_deadline(deadline: str = typer.Argument(..., metavar='DAY', help='The deadline you worked out from a fax '
                                                   'service date, such as 2026-11-20.')):
    """Extend a deadline that runs from service by fax by two court days (rule 2.306(d)), counting court holidays."""
    try:
        date.fromisoformat(deadline)
    except ValueError:
        raise typer.BadParameter('Give the day as 2026-11-20.', param_hint='DAY') from None
    result = state.api().post('/court/deadline', json={'deadline': deadline})
    state.out().result(result, lambda out: out.line(result['sentence']))


@court.command('record')
def court_record(fax_id: str = typer.Argument(..., help='Fax ID of a court fax.'),
                 output: str = typer.Option(None, '--output', '-o', help='Save the PDF here; the default is '
                                            'transmission-record-<fax>.pdf.')):
    """Save a court fax's transmission record, with a draft proof of transmission or of service, as a PDF."""
    api = state.api()
    response = api.get('/court/faxes/' + segment(fax_id) + '/record', raw=True, headers={'Accept': 'application/pdf'})
    path = Path(output or f'transmission-record-{fax_id[:8]}.pdf')
    path.write_bytes(response.content)
    state.out().result({'fax_id': fax_id, 'saved_to': str(path)},
                       lambda out: out.line(f'Transmission record saved to {path}.'))


def sentences(api, fax_id):
    """What Sent details say about a court fax (``faxbot faxes sent show``); empty for any other fax."""
    try:
        found = (api.get('/court/faxes/' + segment(fax_id)) or {}).get('court')
    except CliError:
        return []
    if not found:
        return []
    lines = [f"{found['purpose_text']}: {found['case']['title']}, {found['case']['county']} County"
             + (f", case {found['case']['case_number']}" if found['case'].get('case_number') else '') + '.']
    lines += found['sentences'] + found['timing']
    if found.get('court_note'):
        lines.append(found['court_note'])
    if found.get('record_ready'):
        lines.append(f'Save its transmission record with faxbot delivery rules court record {fax_id}.')
    return lines


__all__ = ['court', 'sentences', 'local_time']
