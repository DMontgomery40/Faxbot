"""Saved connection profiles for the command line."""
import sys

import typer

from .. import profiles, state
from ..errors import CliError

config = typer.Typer(help='Save server addresses and API keys as profiles, so you need not type them.',
                     no_args_is_help=True)


@config.command('set-profile')
def set_profile(name: str = typer.Argument('default', help='Profile name.'),
                url: str = typer.Option(None, '--url', help='Server address for this profile. Default: the address in '
                                                            'use now.'),
                key_stdin: bool = typer.Option(False, '--key-stdin', help='Read the key from standard input instead of asking for it.'),
                no_key: bool = typer.Option(False, '--no-key', help='Save the address only.'),
                use: bool = typer.Option(True, '--use/--no-use', help='Make this the default profile.')):
    """Save a server address and key as a profile. You are asked for the key without it being shown."""
    profiles.check_name(name)
    if key_stdin and no_key:
        raise CliError('Choose --key-stdin or --no-key, not both.')
    current = state.current()
    document = profiles.load()
    saved = dict(document['profiles'].get(name, {}))
    saved['url'] = (url or current.url).rstrip('/')
    if key_stdin:
        key = sys.stdin.readline().strip()
    elif no_key:
        key = None
    else:
        key = typer.prompt('API key (leave empty to keep the saved one)', default='', hide_input=True,
                           show_default=False).strip() or saved.get('key')
    if key:
        saved['key'] = key
    else:
        saved.pop('key', None)
    document['profiles'][name] = saved
    if use:
        document['default_profile'] = name
    path = profiles.save(document)
    state.out().result({'profile': name, 'url': saved['url'], 'key_saved': bool(saved.get('key')),
                        'default': document.get('default_profile') == name, 'path': str(path)},
                       lambda out: out.line(f'Profile {name} saved in {path}.'))


@config.command('show')
def show():
    """List saved profiles. Keys are never shown."""
    document = profiles.load()
    rows = [{'profile': name, 'url': values.get('url'), 'key_saved': bool(values.get('key')),
             'default': name == document.get('default_profile')} for name, values in sorted(document['profiles'].items())]
    result = {'path': str(profiles.config_path()), 'profiles': rows, 'in_use': state.current().profile}
    state.out().result(result, lambda out: (out.table(['Profile', 'Server', 'Key saved', 'Default'],
        [[row['profile'], row['url'], row['key_saved'], row['default']] for row in rows],
        empty="No saved profiles. Save one with 'faxbot admin profiles save'."), out.line(f"File: {result['path']}")))


@config.command('use')
def use_profile(name: str = typer.Argument(..., help='Profile name.')):
    """Make a saved profile the default."""
    document = profiles.load()
    profiles.select(document, name)
    document['default_profile'] = name
    profiles.save(document)
    state.out().result({'default': name}, lambda out: out.line(f'Using profile {name} by default.'))


@config.command('remove')
def remove_profile(name: str = typer.Argument(..., help='Profile name.')):
    """Delete a saved profile and its key."""
    document = profiles.load()
    profiles.select(document, name)
    del document['profiles'][name]
    if document.get('default_profile') == name:
        document['default_profile'] = None
    profiles.save(document)
    state.out().result({'removed': name}, lambda out: out.line(f'Profile {name} removed.'))
