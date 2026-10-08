"""Finding partners on the command line: suggestions, introductions and directory publishing (Partners → Find partners)."""
import typer

from .. import state
from ..client import segment
from ..errors import CliError
from ..output import local_time

discover = typer.Typer(help='Recipients that run Faxbot, found from your fax calls, introductions and directories you '
                            'trust, and the settings for finding them.', no_args_is_help=True)
publish = typer.Typer(help='Publish your fax number in a directory you control, so other Faxbots can find you.',
                      no_args_is_help=True)

SOURCES = {'call': 'Fax call', 'introduction': 'Introduction', 'directory': 'Directory'}


def _view(api):
    return api.get('/direct/discovery')


def settings_fields(view):
    """The settings rows, each with its sentence."""
    texts, settings = view['texts'], view['settings']
    rows = [('Answer Faxbot lookups', 'On' if settings['well_known'] else 'Off'), ('', texts['well_known']),
            ('Look for Faxbot on calls', 'On' if settings['from_calls'] else 'Off'), ('', texts['from_calls']),
            ('Trusted directories', ', '.join(settings['directories']) or 'None'), ('', texts['directories'])]
    return rows + ([('', texts['private'])] if texts.get('private') else [])


def _switch(value, name):
    if value is None:
        return None
    if value not in ('on', 'off'):
        raise typer.BadParameter('Use on or off.', param_hint=name)
    return value == 'on'


def _suggestion(view, reference):
    digits = ''.join(ch for ch in reference if ch.isdigit())
    matches = [item for item in view['suggestions'] if item['id'] == reference
               or item['organization'].casefold() == reference.casefold()
               or (digits and ''.join(ch for ch in item['number'] if ch.isdigit()) == digits)]
    if len(matches) != 1:
        raise CliError(f"No single suggestion matches '{reference}'. See 'faxbot recipients partners discover list'.")
    return matches[0]


def _partner(view, reference):
    digits = ''.join(ch for ch in reference if ch.isdigit())
    matches = [item for item in view['partners'] if item['id'] == reference
               or item['organization'].casefold() == reference.casefold()
               or (digits and ''.join(ch for ch in item['fax_number'] if ch.isdigit()) == digits)]
    if len(matches) != 1:
        raise CliError(f"No single partner matches '{reference}'. See 'faxbot recipients partners list'.")
    return matches[0]


@discover.command('list')
def discover_list():
    """List recipients that run Faxbot and could become partners, and the latest lookups."""
    view = _view(state.api())

    def human(out):
        out.fields(settings_fields(view))
        out.table(['Recipient', 'Fax number', 'How Faxbot found it', 'Found'],
                  [[item['organization'], item['number'], item['source_text'], local_time(item['created_at'])]
                   for item in view['suggestions']],
                  title='Suggested partners', empty='No recipient that runs Faxbot has been found yet.')
        for item in view['suggestions']:
            if item.get('network_text'):
                out.line(f"{item['organization']}: {item['network_text']} Certificate fingerprint: "
                         f"{item['certificate']}")
        if view['suggestions']:
            out.line(view['suggestions'][0]['sentence'])
        out.table(['When', 'Address', 'Result'],
                  [[local_time(item['when']), item['host'], item['sentence']] for item in view['lookups']],
                  title='Latest lookups', empty='No lookups yet.')
    state.out().result(view, human)


@discover.command('lookup')
def discover_lookup(number: str = typer.Argument(..., help='Fax number to look up, with its country code.')):
    """Look a fax number up now in the directories you trust."""
    result = state.api().post('/direct/discovery/lookup', json={'number': number})
    state.out().result(result, lambda out: out.line(result['detail']))


@discover.command('enroll')
def discover_enroll(suggestion: str = typer.Argument(..., help='Suggested recipient: organization, fax number or '
                                                                'id.')):
    """Add a suggested recipient as a partner. Then send them a code by fax to confirm their number."""
    api = state.api()
    item = _suggestion(_view(api), suggestion)
    result = api.post(f"/direct/discovery/suggestions/{segment(item['id'])}/enroll")
    state.out().result(result, lambda out: out.line(result['detail']))


@discover.command('dismiss')
def discover_dismiss(suggestion: str = typer.Argument(..., help='Suggested recipient: organization, fax number or '
                                                                 'id.')):
    """Stop suggesting a recipient."""
    api = state.api()
    item = _suggestion(_view(api), suggestion)
    result = api.post(f"/direct/discovery/suggestions/{segment(item['id'])}/dismiss")
    state.out().result(result, lambda out: out.line(result['detail']))


@discover.command('settings')
def discover_settings(well_known: str = typer.Option(None, '--answer-lookups', metavar='on|off',
                                                     help='Let Faxbots that fax you read your partner card (on by '
                                                          'default).'),
                      from_calls: str = typer.Option(None, '--from-calls', metavar='on|off',
                                                     help='Look up the other side of your fax calls when it shows '
                                                          'it runs Faxbot (on by default).'),
                      directories: list[str] = typer.Option(None, '--directory', metavar='DOMAIN',
                                                            help='A directory you trust; repeat for several. '
                                                                 'Replaces the list.'),
                      no_directories: bool = typer.Option(False, '--no-directories',
                                                          help='Trust no directory.')):
    """Show or change how Faxbot finds partners."""
    api = state.api()
    body = {}
    if _switch(well_known, '--answer-lookups') is not None:
        body['well_known'] = well_known == 'on'
    if _switch(from_calls, '--from-calls') is not None:
        body['from_calls'] = from_calls == 'on'
    if no_directories and directories:
        raise CliError('Use --no-directories on its own, or give --directory.')
    if no_directories:
        body['directories'] = []
    elif directories:
        body['directories'] = list(directories)
    view = api.put('/direct/discovery/settings', json=body) if body else _view(api)

    def human(out):
        out.fields(settings_fields(view))
    state.out().result(view, human)


def introduce(first: str = typer.Argument(..., help='A partner: organization, fax number or id.'),
              second: str = typer.Argument(..., help='The partner to introduce them to.')):
    """Introduce two of your partners to each other. Both must have agreed to be introduced."""
    api = state.api()
    view = _view(api)
    one, other = _partner(view, first), _partner(view, second)
    result = api.post('/direct/discovery/introductions', json={'first': one['id'], 'second': other['id']})
    state.out().result(result, lambda out: out.line(result['detail']))


def may_introduce(partner: str = typer.Argument(..., help='Partner organization, fax number or id.'),
                  choice: str = typer.Argument(..., metavar='on|off',
                                               help='on once the partner agreed to be introduced to your other '
                                                    'partners; off (the default) never introduces them.')):
    """Set whether a partner may be introduced to your other partners (off by default)."""
    allowed = _switch(choice, 'on|off')
    if allowed is None:
        raise typer.BadParameter('Use on or off.', param_hint='on|off')
    api = state.api()
    item = _partner(_view(api), partner)
    result = api.post(f"/direct/discovery/partners/{segment(item['id'])}/may-introduce", json={'allowed': allowed})
    state.out().result(result, lambda out: out.line(result['detail']))


@publish.command('list')
def publish_list():
    """List the fax numbers you published, with the record each directory needs."""
    view = _view(state.api())

    def human(out):
        out.line(view['publishable']['sentence'])
        out.table(['Fax number', 'Directory', 'Valid until', 'Status'],
                  [[item['number'], item['directory'], item['expires_text'], item['sentence']]
                   for item in view['publications']], title='Published numbers', empty='Nothing is published.')
        for item in view['publications']:
            out.line(item['zone'])
    state.out().result(view['publications'], human)


@publish.command('add')
def publish_add(number: str = typer.Argument(..., help='Your fax number: the one on your partner card.'),
                directory: str = typer.Option(..., '--directory', metavar='DOMAIN',
                                              help='The directory domain you control, such as '
                                                   'faxdirectory.example.org.')):
    """Make the signed record that publishes your fax number in a directory. Add it to that domain's DNS."""
    result = state.api().post('/direct/discovery/publications', json={'number': number, 'directory': directory})

    def human(out):
        out.line(result['detail'])
        out.line(result['zone'])
    state.out().result(result, human)


def _publication(view, reference):
    digits = ''.join(ch for ch in reference if ch.isdigit())
    matches = [item for item in view['publications'] if item['id'] == reference or item['directory'] == reference
               or (digits and ''.join(ch for ch in item['number'] if ch.isdigit()) == digits)]
    if len(matches) != 1:
        raise CliError(f"No single published number matches '{reference}'. See "
                       "'faxbot recipients partners publish list'.")
    return matches[0]


@publish.command('check')
def publish_check(publication: str = typer.Argument(..., help='The directory, the fax number or the id.')):
    """Check that a directory's DNS has the record."""
    api = state.api()
    item = _publication(_view(api), publication)
    result = api.post(f"/direct/discovery/publications/{segment(item['id'])}/check")
    state.out().result(result, lambda out: out.line(result['detail']))


@publish.command('withdraw')
def publish_withdraw(publication: str = typer.Argument(..., help='The directory, the fax number or the id.')):
    """Stop publishing a fax number. Then delete its record from the directory's DNS."""
    api = state.api()
    item = _publication(_view(api), publication)
    result = api.post(f"/direct/discovery/publications/{segment(item['id'])}/withdraw")
    state.out().result(result, lambda out: out.line(result['detail']))
