"""Bounded, pure credential preparation; never confirms a database commit.

Preparation material is server-private. A future owning mutation adapter may
call the narrowly named internal accessors only after confirmed commit, disclose
once, then discard the preparation. Denial, rollback or uncertain commit must
never disclose it. There is deliberately no caller-provided success flag or
public secret/serialization method here.
"""
import base64
import binascii
import hashlib
import hmac
import re
import secrets

from .types import AccessError


class InvalidCredentialInputError(AccessError):
    code = 'invalid_credential_input'


class CredentialServiceError(AccessError):
    code = 'credential_service_unavailable'


_LOGIN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@+-]*', re.ASCII)
_PUBLIC_ID = re.compile(r'[0-9a-f]{12}', re.ASCII)
_BASE64 = re.compile(r'[A-Za-z0-9_-]+', re.ASCII)
_CONSTRUCTION = object()


def normalize_login(login: str) -> tuple[str, str]:
    """One ASCII syntax for display, authentication lookup and Owner counting."""
    if type(login) is not str:
        raise InvalidCredentialInputError()
    display = login.strip()
    if not 1 <= len(display) <= 100 or _LOGIN.fullmatch(display) is None:
        raise InvalidCredentialInputError()
    return display, display.lower()


def _valid_public_key_id(value):
    return type(value) is str and _PUBLIC_ID.fullmatch(value) is not None


def _b64(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode(value, length):
    # Check encoded length/alphabet before decoding and reject alternate trailing
    # pad bits by canonical round-trip. '=' padding is never a stored format.
    if len(value) != (length * 8 + 5) // 6 or _BASE64.fullmatch(value) is None:
        return None
    try:
        result = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        return None
    return result if len(result) == length and _b64(result) == value else None


def _parse(encoded_hash):
    if type(encoded_hash) is not str or not 0 < len(encoded_hash) <= 256 or not encoded_hash.isascii():
        return None
    parts = encoded_hash.split('$')
    if len(parts) == 6 and parts[0] == 'scrypt' and parts[3:] == ['n=16384', 'r=8', 'p=1']:
        algorithm = 'scrypt'
    elif len(parts) == 4 and parts[0] == 'pbkdf2' and parts[3] == 'rounds=200000':
        algorithm = 'pbkdf2'
    else:
        return None
    salt, digest = _decode(parts[1], 16), _decode(parts[2], 32)
    return None if salt is None or digest is None else (algorithm, salt, digest)


def _secret_bytes(secret):
    if type(secret) is not str or not 0 < len(secret) <= 1024:
        return None
    try:
        result = secret.encode('utf-8')
    except UnicodeEncodeError:
        return None
    return result if len(result) <= 1024 else None


def _kdf(algorithm, secret, salt):
    # A backend may include inputs in its exception. Raise our fixed failure
    # outside the exception handler, with no retained exception chain/context.
    result = None
    try:
        if algorithm == 'scrypt':
            result = hashlib.scrypt(secret, salt=salt, n=16384, r=8, p=1, dklen=32)
        else:
            result = hashlib.pbkdf2_hmac('sha256', secret, salt, 200000, dklen=32)
    except Exception:
        pass
    if type(result) is not bytes or len(result) != 32:
        raise CredentialServiceError()
    return result


def _random_bytes(length):
    result = None
    try:
        result = secrets.token_bytes(length)
    except Exception:
        pass
    if type(result) is not bytes or len(result) != length:
        raise CredentialServiceError()
    return result


class _PrivateValue:
    __slots__ = ('__weakref__',)

    def __setattr__(self, name, value):
        raise AttributeError('immutable credential material')

    def __delattr__(self, name):
        raise AttributeError('immutable credential material')

    def __repr__(self):
        return type(self).__name__ + '()'

    def __reduce_ex__(self, protocol):
        raise TypeError('private credential material')

    def __copy__(self):
        raise TypeError('private credential material')

    def __deepcopy__(self, memo):
        raise TypeError('private credential material')


class PreparedChosenPassword(_PrivateValue):
    __slots__ = ('_password_hash',)

    def __init__(self, seam, password_hash):
        if seam is not _CONSTRUCTION:
            raise TypeError('private credential material')
        object.__setattr__(self, '_password_hash', password_hash)

    def _password_hash_for_storage(self) -> str:
        return self._password_hash


class PreparedTemporaryPassword(PreparedChosenPassword):
    __slots__ = ('_temporary_secret',)

    def __init__(self, seam, password_hash, temporary_secret):
        super().__init__(seam, password_hash)
        object.__setattr__(self, '_temporary_secret', temporary_secret)

    def _temporary_secret_for_committed_adapter(self) -> str:
        """Only the owning adapter can confirm commit and disclose this once."""
        return self._temporary_secret


class PreparedKeyRotation(_PrivateValue):
    __slots__ = ('_public_key_id', '_key_hash', '_token')

    def __init__(self, seam, public_key_id, key_hash, token):
        if seam is not _CONSTRUCTION:
            raise TypeError('private credential material')
        object.__setattr__(self, '_public_key_id', public_key_id)
        object.__setattr__(self, '_key_hash', key_hash)
        object.__setattr__(self, '_token', token)

    @property
    def public_key_id(self) -> str:
        return self._public_key_id

    def _key_hash_for_storage(self) -> str:
        return self._key_hash

    def _token_for_committed_adapter(self) -> str:
        """Only the owning adapter can confirm commit and disclose this once."""
        return self._token


class PreparedNewKey(PreparedKeyRotation):
    __slots__ = ('_row_id',)

    def __init__(self, seam, row_id, public_key_id, key_hash, token):
        super().__init__(seam, public_key_id, key_hash, token)
        object.__setattr__(self, '_row_id', row_id)

    @property
    def row_id(self) -> str:
        return self._row_id


class CredentialCodec:
    def supports_hash(self, encoded_hash: str) -> bool:
        """Structure only; no KDF or service-availability check."""
        return _parse(encoded_hash) is not None

    def verify(self, secret: str, encoded_hash: str) -> bool:
        parsed, data = _parse(encoded_hash), _secret_bytes(secret)
        if parsed is None or data is None:
            return False
        algorithm, salt, expected = parsed
        return hmac.compare_digest(_kdf(algorithm, data, salt), expected)

    def _new_hash(self, secret):
        data = _secret_bytes(secret)
        if data is None:
            raise InvalidCredentialInputError()
        salt = _random_bytes(16)
        digest = _kdf('scrypt', data, salt)
        return f'scrypt${_b64(salt)}${_b64(digest)}$n=16384$r=8$p=1'

    def prepare_temporary_password(self) -> PreparedTemporaryPassword:
        secret = _b64(_random_bytes(32))
        return PreparedTemporaryPassword(_CONSTRUCTION, self._new_hash(secret), secret)

    def prepare_chosen_password(self, password: str) -> PreparedChosenPassword:
        if _secret_bytes(password) is None or len(password) < 12:
            raise InvalidCredentialInputError()
        return PreparedChosenPassword(_CONSTRUCTION, self._new_hash(password))

    def prepare_new_key(self) -> PreparedNewKey:
        public_id, row_id = _random_bytes(6).hex(), _random_bytes(16).hex()
        secret = _b64(_random_bytes(32))
        return PreparedNewKey(_CONSTRUCTION, row_id, public_id, self._new_hash(secret),
                              f'fbk_live_{public_id}_{secret}')

    def prepare_key_rotation(self, public_key_id: str) -> PreparedKeyRotation:
        if not _valid_public_key_id(public_key_id):
            raise InvalidCredentialInputError()
        secret = _b64(_random_bytes(32))
        return PreparedKeyRotation(_CONSTRUCTION, public_key_id, self._new_hash(secret),
                                   f'fbk_live_{public_key_id}_{secret}')
