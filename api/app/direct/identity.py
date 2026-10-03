"""This installation's direct delivery keys, kept in a private file beside the configuration key.

The file is created on first use, published exclusively with mode 0600, and
never replaced or exported. Only the public keys leave the installation.
"""
import base64
import json
import os
from pathlib import Path
import stat
import tempfile

from .crypto import Identity


class IdentityUnavailable(RuntimeError):
    """The key file is missing (and may not be created) or is not private and valid."""


def identity_path(environment, fax_data_dir):
    explicit = (environment or {}).get('FAXBOT_DIRECT_KEY_PATH')
    return Path(explicit) if explicit else Path(fax_data_dir) / '.direct-identity.key'


def _read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 4096:
            raise IdentityUnavailable('The direct delivery key file must be a private regular file.')
        data = os.read(descriptor, 4097)
    finally:
        os.close(descriptor)
    try:
        document = json.loads(data)
        if set(document) != {'version', 'signing', 'exchange'} or document['version'] != 1:
            raise ValueError
        signing = base64.b64decode(document['signing'], validate=True)
        exchange = base64.b64decode(document['exchange'], validate=True)
        return Identity(signing, exchange)
    except (ValueError, TypeError, KeyError):
        raise IdentityUnavailable('The direct delivery key file is not valid.') from None


def load_identity(path, *, create=False):
    path = Path(path)
    try:
        return _read(path)
    except FileNotFoundError:
        if not create:
            raise IdentityUnavailable('Direct delivery has no keys yet.') from None
    except OSError:
        raise IdentityUnavailable('The direct delivery key file cannot be read.') from None
    if path.is_symlink():
        raise IdentityUnavailable('The direct delivery key file must be a private regular file.')
    identity = Identity.generate()
    signing, exchange = identity.private_bytes()
    data = json.dumps({'version': 1, 'signing': base64.b64encode(signing).decode('ascii'),
                       'exchange': base64.b64encode(exchange).decode('ascii')}).encode('ascii')
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.faxbot-direct-', dir=path.parent)
        with os.fdopen(descriptor, 'wb', buffering=0) as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # Another worker published first; use its keys.
        return _read(path)
    except OSError:
        raise IdentityUnavailable('The direct delivery key file cannot be created.') from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
