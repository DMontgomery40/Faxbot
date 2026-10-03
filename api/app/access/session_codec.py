"""Bounded, private session preparation; never attests a database commit.

The installation key is supplied as exact raw bytes before any transaction.
Only the owning adapter can confirm commit, disclose cookie/CSRF material and
discard the preparation. Denial, rollback or uncertain commit must not disclose
it. This module has no database, environment or file dependencies.
"""
import base64
import binascii
import hmac
import re
import secrets
from weakref import WeakKeyDictionary

from .credentials import _PrivateValue
from .types import AccessUnavailableError, AuthenticationError


_CONSTRUCTION = object()
_BASE64 = re.compile(r'[A-Za-z0-9_-]{43}', re.ASCII)
_HASH = re.compile(r'[0-9a-f]{64}', re.ASCII)
_TOKEN_DOMAIN = b'faxbot/access/session-token/v1\0'
_CSRF_DOMAIN = b'faxbot/access/session-csrf/v1\0'
_CSRF_HASH_DOMAIN = b'faxbot/access/session-csrf-hash/v1\0'


def _b64(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _canonical(value):
    # Bound work before regex/decoding. An exact str also excludes caller-
    # controlled subclass methods. Round-trip rejects alternate trailing bits.
    if type(value) is not str or len(value) != 43 or _BASE64.fullmatch(value) is None:
        return False
    try:
        decoded = base64.b64decode(value + '=', altchars=b'-_', validate=True)
    except (ValueError, binascii.Error):
        return False
    return len(decoded) == 32 and _b64(decoded) == value


def _valid_token(value):
    return (type(value) is str and len(value) == 47
            and value.startswith('fbs_') and _canonical(value[4:]))


def _valid_hash(value):
    return type(value) is str and len(value) == 64 and _HASH.fullmatch(value) is not None


def _random_bytes(length):
    result = None
    try:
        result = secrets.token_bytes(length)
    except Exception:
        pass
    # Raise outside the handler: RNG exception details may contain inputs.
    if type(result) is not bytes or len(result) != length:
        raise AccessUnavailableError()
    return result


class PreparedSession(_PrivateValue):
    """Codec-owned material, with no public secret or commit attestation."""
    __slots__ = ('_material',)

    def __init__(self, seam, material):
        if seam is not _CONSTRUCTION:
            raise TypeError('private session material')
        object.__setattr__(self, '_material', material)

    def _token_for_committed_adapter(self) -> str:
        """Owning adapter must confirm DB commit, then disclose and discard.

        Preparation itself cannot confirm commit; never call after denial,
        rollback or uncertain commit.
        """
        return self._material[1]

    def _csrf_for_committed_adapter(self) -> str:
        """Owning adapter must confirm DB commit, then disclose and discard.

        Preparation itself cannot confirm commit; never call after denial,
        rollback or uncertain commit.
        """
        return self._material[2]


class SessionStorage(_PrivateValue):
    """Private SQL verification material; contains no raw token or CSRF."""
    __slots__ = ('_material',)

    def __init__(self, seam, material):
        if seam is not _CONSTRUCTION:
            raise TypeError('private session material')
        object.__setattr__(self, '_material', material)

    @property
    def id(self) -> str:
        return self._material[0]

    @property
    def token_hash(self) -> str:
        return self._material[1]

    @property
    def csrf_hash(self) -> str:
        return self._material[2]


class SessionCodec:
    __slots__ = ('_key', '_preparations')

    def __init__(self, *, installation_key: bytes):
        if type(installation_key) is not bytes or len(installation_key) != 32:
            raise AccessUnavailableError()
        self._key = installation_key
        # Capture the original immutable tuple independently, not the object's
        # current attributes. Weak keys let discarding preparation release it.
        self._preparations = WeakKeyDictionary()

    def _digest(self, domain, value):
        result = None
        try:
            result = hmac.digest(self._key, domain + value.encode('ascii'), 'sha256')
        except Exception:
            pass
        if type(result) is not bytes or len(result) != 32:
            raise AccessUnavailableError()
        return result

    def token_hash(self, token: str) -> str:
        if not _valid_token(token):
            raise AuthenticationError()
        return self._digest(_TOKEN_DOMAIN, token).hex()

    def csrf_value(self, token: str) -> str:
        if not _valid_token(token):
            raise AuthenticationError()
        return _b64(self._digest(_CSRF_DOMAIN, token))

    def csrf_hash(self, csrf: str) -> str:
        if not _canonical(csrf):
            raise AuthenticationError()
        return self._digest(_CSRF_HASH_DOMAIN, csrf).hex()

    def verify_csrf(self, token: str, supplied: str, stored_hash: str) -> bool:
        if not _valid_token(token) or not _canonical(supplied) or not _valid_hash(stored_hash):
            return False
        expected = self.csrf_value(token)
        supplied_hash = self.csrf_hash(supplied)
        # Perform both constant-time comparisons even when the first fails.
        value_matches = hmac.compare_digest(supplied, expected)
        hash_matches = hmac.compare_digest(supplied_hash, stored_hash)
        return value_matches and hash_matches

    def prepare(self) -> PreparedSession:
        token_bytes, id_bytes = _random_bytes(32), _random_bytes(16)
        token = 'fbs_' + _b64(token_bytes)
        csrf = self.csrf_value(token)
        material = (id_bytes.hex(), token, csrf, self.token_hash(token), self.csrf_hash(csrf))
        prepared = PreparedSession(_CONSTRUCTION, material)
        self._preparations[prepared] = material
        return prepared

    def storage(self, prepared: PreparedSession) -> SessionStorage:
        if type(prepared) is not PreparedSession:
            raise AuthenticationError()
        material = self._preparations.get(prepared)
        current = getattr(prepared, '_material', None)
        if material is None or current is not material:
            raise AuthenticationError()
        return SessionStorage(_CONSTRUCTION, (material[0], material[3], material[4]))
