"""Current canonical bootstrap credentials on the caller's access transaction.

Only installation-key cryptographic material is prepared at construction. Active
configuration and principal epochs are read afresh; no key-file I/O, new database
connection, configuration lock or environment lookup occurs during authentication.
"""
import base64
import hmac

import sqlalchemy as sa

from ..config_secrets import ConfigurationCipher
from ..config_store import ConfigurationStoreError
from .types import (AccessUnavailableError, AuthenticationError, BootstrapEvidence,
                    PrincipalContext)


_DOMAIN = b'faxbot/access/bootstrap/v1\0'
_MAX_SECRET_BYTES = 1024 * 1024


class BootstrapCredentials:
    __slots__ = ('store', '_configuration', '_cipher', '_key')

    def __init__(self, configuration_store, *, installation_key: bytes):
        prepared = None
        try:
            if type(installation_key) is bytes:
                cipher = ConfigurationCipher(installation_key)
                key = base64.urlsafe_b64decode(installation_key)
                if len(key) == 32:
                    prepared = cipher, key
        except (ValueError, TypeError):
            pass
        if prepared is None:
            raise AccessUnavailableError()
        self.store = configuration_store.access_store
        self._configuration = configuration_store
        self._cipher, self._key = prepared

    def _fingerprint(self, secret):
        if type(secret) is not str or not 0 < len(secret) <= _MAX_SECRET_BYTES:
            return None
        try:
            encoded = secret.encode('utf-8')
        except UnicodeEncodeError:
            return None
        if len(encoded) > _MAX_SECRET_BYTES:
            return None
        return hmac.digest(self._key, _DOMAIN + encoded, 'sha256').hex()

    def current_fingerprint_on(self, connection):
        self.store.require_lock_on(connection)
        result = None
        available = False
        try:
            head = self._configuration._head(connection)
            if head is not None:
                revision = self._configuration._revision(connection, self._cipher,
                    head['installation_id'], head['active_revision_id'])
                result = self._fingerprint(revision.values.api_key)
                available = revision.values.api_key == '' or result is not None
        except (ConfigurationStoreError, ValueError, sa.exc.SQLAlchemyError):
            pass
        # Raise outside the handler: no retained configuration/SQL exception
        # context that might carry encrypted payloads or submitted credentials.
        if not available:
            raise AccessUnavailableError()
        return result

    def authenticate_on(self, connection, secret):
        self.store.require_lock_on(connection)
        supplied = self._fingerprint(secret)
        if supplied is None:
            raise AuthenticationError()
        current = self.current_fingerprint_on(connection)
        if current is None or not hmac.compare_digest(current, supplied):
            raise AuthenticationError()
        principals = self.store.tables['access_principals']
        epoch = None
        available = False
        try:
            epoch = connection.execute(sa.select(principals.c.security_version).where(
                principals.c.id == 'bootstrap', principals.c.kind == 'bootstrap',
                principals.c.enabled == 1)).scalar_one_or_none()
            available = True
        except sa.exc.SQLAlchemyError:
            pass
        if not available:
            raise AccessUnavailableError()
        if type(epoch) is not int or epoch < 1:
            raise AuthenticationError()
        return PrincipalContext('bootstrap', epoch, BootstrapEvidence(current), 'key:env')
