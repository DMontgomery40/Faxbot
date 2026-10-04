"""Saved connection profiles in ~/.config/faxbot/config.toml (private to the user).

The file holds a default profile name and, per profile, the server address and
optionally an API key. It is written atomically with mode 0600 inside a 0700
directory. FAXBOT_CLI_CONFIG points at another file.
"""
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib

from .errors import CliError

DEFAULT_URL = 'http://localhost:8080'
_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')


def config_path():
    explicit = os.environ.get('FAXBOT_CLI_CONFIG')
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
    return Path(base).expanduser() / 'faxbot' / 'config.toml'


def check_name(name):
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise CliError('Profile names use letters, digits, hyphens and underscores only.')
    return name


def load(path=None):
    path = path or config_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {'default_profile': None, 'profiles': {}}
    except OSError:
        raise CliError(f'Cannot read the profile file {path}.') from None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = 0
    if mode & 0o077:
        print(f'Warning: {path} can be read by other users. Run: chmod 600 {path}', file=sys.stderr)
    try:
        document = tomllib.loads(data.decode('utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        raise CliError(f'The profile file {path} is not valid. Fix or remove it.') from None
    profiles = {}
    for name, values in (document.get('profiles') or {}).items():
        if isinstance(values, dict) and _NAME.fullmatch(name):
            profiles[name] = {key: values[key] for key in ('url', 'key') if isinstance(values.get(key), str)}
    default = document.get('default_profile')
    return {'default_profile': default if isinstance(default, str) else None, 'profiles': profiles}


def _string(value):
    # A JSON string with ASCII escapes is also a valid TOML basic string.
    return json.dumps(value, ensure_ascii=True)


def save(document, path=None):
    path = path or config_path()
    lines = ['# Faxbot command line profiles. Keep this file private (mode 600).']
    if document.get('default_profile'):
        lines.append('default_profile = ' + _string(document['default_profile']))
    for name in sorted(document.get('profiles', {})):
        values = document['profiles'][name]
        lines += ['', f'[profiles.{check_name(name)}]']
        for key in ('url', 'key'):
            if values.get(key):
                lines.append(f'{key} = {_string(values[key])}')
    content = '\n'.join(lines) + '\n'
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.config-', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
    except OSError:
        raise CliError(f'Cannot save the profile file {path}.') from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return path


def select(document, requested):
    """The profile in use: the requested one, else the saved default, else 'default' if present."""
    profiles = document['profiles']
    if requested:
        check_name(requested)
        if requested not in profiles:
            raise CliError(f"There is no saved profile named '{requested}'. See 'faxbot system profiles list'.")
        return requested, profiles[requested]
    for name in (document.get('default_profile'), 'default'):
        if name and name in profiles:
            return name, profiles[name]
    return None, {}
