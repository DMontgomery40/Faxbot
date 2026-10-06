"""Saving settings from the command line: one revision-checked write, and secrets kept out of shell history."""
import sys

from .errors import CliError, EXIT_CONFLICT

ENV_MANAGED = 'This key is set in .env. Change it there, then run docker compose up -d.'


def write_settings(api, changes, *, current=None):
    """Save ``changes`` against the settings revision just read (``current``, read now when not given).

    A setting kept in .env is refused with one sentence before anything is sent.
    """
    current = current if current is not None else api.get('/admin/settings')
    if set(changes) & set(current.get('_meta', {}).get('env_managed') or []):
        raise CliError(ENV_MANAGED, EXIT_CONFLICT)
    return api.put('/admin/settings', json={**changes, 'expected_revision_id': current['_meta']['desired_revision_id']})


def secret_names():
    """Every name, configuration or request, of a setting that holds a password or key."""
    from ..config_values import ConfigurationValues
    names = set()
    for name, field in ConfigurationValues.model_fields.items():
        extra = field.json_schema_extra or {}
        if extra.get('secret'):
            names |= {name, extra.get('patch_name', name)}
    return names


def refuse_secret_in_command(name, shown=None):
    """A password or key typed as NAME=VALUE stays in shell history and the process list: say how to give it."""
    if name in secret_names():
        shown = shown or name
        raise CliError(f'{shown} is a password or key, and typed on the command line it stays in your shell history. '
                       f'Use --secret {shown} to type it without showing it, or --secret-stdin {shown} to read it '
                       'from standard input.')


def secret_from_stdin(name):
    """One line from standard input, for scripts: never echoed and never on the command line."""
    value = sys.stdin.readline().rstrip('\r\n')
    if not value:
        raise CliError(f'No value for {name} arrived on standard input.')
    return value
