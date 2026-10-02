"""Authenticated private configuration records, bound to their durable identity."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from cryptography.fernet import Fernet, InvalidToken


_MAX_PLAINTEXT = 1024 * 1024
_MAX_ENVELOPE = 2 * 1024 * 1024


class ConfigurationSecretError(ValueError):
    """Safe configuration key/envelope failure; never includes secret input."""


def _read_key(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigurationSecretError('Configuration key must be a private regular file.')
        key = bytearray()
        while len(key) <= 44:
            piece = os.read(descriptor, 45 - len(key))
            if not piece:
                break
            key.extend(piece)
        if len(key) != 44:
            raise ConfigurationSecretError('Invalid configuration encryption key.')
        ConfigurationCipher(bytes(key))
        return bytes(key)
    finally:
        os.close(descriptor)


def _sync_key_directory(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_installation_key(path: str | Path, *, allow_create: bool) -> bytes:
    """Load an operator-owned key, or publish one complete private key exclusively.

    The caller holds the database initialization lock and permits creation only
    before any encrypted rows exist. The parent directory must already exist.
    Initialization retries sync the directory even when a previous publication
    succeeded but its durability acknowledgement failed. A published key is
    never deleted or replaced. These local POSIX semantics do not coordinate
    separate installations or defend an operator directory against hostile edits.
    """
    path = Path(path)
    temporary = None
    try:
        try:
            key = _read_key(path)
        except FileNotFoundError:
            if not allow_create:
                raise ConfigurationSecretError('Configuration encryption key is missing.') from None
            # Distinguish an absent name from a dangling symlink on platforms
            # whose no-follow error behavior differs.
            if path.is_symlink():
                raise ConfigurationSecretError('Configuration key must be a private regular file.')
            descriptor, temporary = tempfile.mkstemp(prefix='.faxbot-key-', dir=path.parent)
            with os.fdopen(descriptor, 'wb', buffering=0):
                os.fchmod(descriptor, 0o600)
                remaining = memoryview(Fernet.generate_key())
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written == 0:
                        raise OSError('Key write made no progress.')
                    remaining = remaining[written:]
                os.fsync(descriptor)
            try:
                os.link(temporary, path)
            except FileExistsError:
                # An independent first initializer won; use its complete key.
                pass
            os.unlink(temporary)
            temporary = None
            key = _read_key(path)
        if allow_create:
            _sync_key_directory(path)
        return key
    except ConfigurationSecretError:
        raise
    except (OSError, ValueError, TypeError):
        raise ConfigurationSecretError('Cannot load or durably create configuration key.') from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class ConfigurationCipher:
    """Small versioned records; historical credentials deliberately never expire."""

    def __init__(self, key: bytes):
        try:
            self._cipher = Fernet(key)
            self.key_id = hashlib.sha256(key).hexdigest()
        except (TypeError, ValueError):
            raise ConfigurationSecretError('Invalid configuration encryption key.') from None

    def seal(self, payload: dict, *, installation_id: str, kind: str, record_id: str) -> str:
        if not isinstance(payload, dict):
            raise ConfigurationSecretError('Invalid configuration payload.')
        try:
            data = json.dumps({
                'version': 1, 'installation_id': installation_id,
                'kind': kind, 'record_id': record_id, 'payload': payload,
            }, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ConfigurationSecretError('Invalid configuration payload.') from None
        if len(data) > _MAX_PLAINTEXT:
            raise ConfigurationSecretError('Configuration payload exceeds the size limit.')
        return self._cipher.encrypt(data).decode('ascii')

    def open(self, envelope: str, *, installation_id: str, kind: str, record_id: str) -> dict:
        try:
            if not isinstance(envelope, str) or len(envelope) > _MAX_ENVELOPE:
                raise ValueError
            plaintext = self._cipher.decrypt(envelope.encode('ascii'))
            if len(plaintext) > _MAX_PLAINTEXT:
                raise ValueError
            record = json.loads(plaintext)
            if (not isinstance(record, dict) or type(record.get('version')) is not int
                    or record['version'] != 1
                    or record.get('installation_id') != installation_id
                    or record.get('kind') != kind or record.get('record_id') != record_id
                    or not isinstance(record.get('payload'), dict)):
                raise ValueError
            return record['payload']
        except (InvalidToken, ValueError, TypeError, UnicodeError, RecursionError):
            raise ConfigurationSecretError('Cannot authenticate configuration record.') from None
