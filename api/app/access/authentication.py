"""Compose committed admission, closed snapshots and fresh session completion.

The HTTP owner runs each complete synchronous method through AuthenticationWork.
Only a confirmed session commit returns (receipt, private preparation); the
transport owner may then set its cookie. This module never discloses secrets or
establishes Origin/CSRF/cookie policy by itself.
"""
from datetime import datetime, timedelta, timezone
from functools import wraps

import sqlalchemy as sa

from .credentials import normalize_login, InvalidCredentialInputError, _valid_public_key_id
from .mutation_types import StaleVersionError
from .sessions import SessionDeniedError, SessionReason
from .types import (AccessError, AccessUnavailableError, AuthenticationError,
                    InvalidTransactionError, PrincipalContext, PasswordSessionEvidence)


KEY_USE_INTERVAL = timedelta(minutes=1)


class AuthenticationThrottledError(AccessError):
    code = 'authentication_throttled'

    def __init__(self, retry_after_seconds):
        if type(retry_after_seconds) is not int or not 1 <= retry_after_seconds <= 86400:
            raise ValueError('invalid authentication retry')
        self.retry_after_seconds = retry_after_seconds
        super().__init__()


def _safe_backend(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (AuthenticationError, AuthenticationThrottledError, SessionDeniedError,
                StaleVersionError, InvalidCredentialInputError):
            raise
        except Exception:
            pass
        raise AccessUnavailableError()
    return wrapped


def _password_input(secret):
    valid = False
    if type(secret) is str and 0 < len(secret) <= 1024:
        try:
            valid = len(secret.encode('utf-8')) <= 1024
        except UnicodeEncodeError:
            pass
    if not valid:
        raise AuthenticationError()


def _login_input(login):
    normalized = None
    if type(login) is str and 0 < len(login) <= 128:
        try:
            _, normalized = normalize_login(login)
        except InvalidCredentialInputError:
            pass
    if normalized is None:
        raise AuthenticationError()
    return normalized


def _key_input(token):
    # The bootstrap reader has the same bounded Unicode/byte policy. DB-key
    # secrets are checked separately against the bounded credential codec.
    valid = False
    if type(token) is str and 0 < len(token) <= 1024 * 1024:
        try:
            valid = len(token.encode('utf-8')) <= 1024 * 1024
        except UnicodeEncodeError:
            pass
    if not valid:
        raise AuthenticationError()


def _key_parts(token):
    if not token.startswith('fbk_live_'):
        return None
    public_id, separator, secret = token[9:].partition('_')
    if not separator or not _valid_public_key_id(public_id):
        return None
    _password_input(secret)
    return public_id, secret


class AuthenticationService:
    @_safe_backend
    def __init__(self, sessions, admission, *, clock=None):
        if sessions.store is not admission.store or (clock is not None and not callable(clock)):
            raise InvalidTransactionError()
        self.sessions, self.admission = sessions, admission
        self.store, self.proofs = sessions.store, sessions.proofs
        self.codec, self.session_codec = sessions.password_codec, sessions.codec
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc).replace(tzinfo=None)
        # Known dummy input is not a credential: its verification result is
        # always ignored and cannot mint a source proof. Prepare once at startup.
        self._dummy_hash = self.codec.prepare_chosen_password(
            'Faxbot internal dummy verification only')._password_hash_for_storage()

    def _reserve(self, kind, subject):
        receipt = self.admission.reserve(kind, subject)
        if not receipt.allowed:
            raise AuthenticationThrottledError(receipt.retry_after_seconds)

    def _complete(self, method, *args):
        with self.store.transaction() as connection:
            # A contended lock must not authenticate against a pre-wait clock.
            outcome = method(connection, *args, now=self._clock())
        # Expected denial audit must commit before translating it. Unknown
        # commit acknowledgement escapes to _safe_backend, never to disclosure.
        if outcome.reason is SessionReason.UNAUTHENTICATED:
            raise AuthenticationError()
        if outcome.reason is SessionReason.STALE_VERSION:
            raise StaleVersionError()
        if outcome.reason is not None:
            raise SessionDeniedError(outcome.reason)
        return outcome.receipt

    @_safe_backend
    def password_login(self, login, password):
        normalized = _login_input(login)
        _password_input(password)
        self._reserve('password_login', normalized)
        with self.store.engine.begin() as connection:
            snapshot = self.proofs.password_snapshot_on(connection, normalized)
        if snapshot is None:
            self.codec.verify(password, self._dummy_hash)
            proof = None
        else:
            proof = self.proofs.verify_password(snapshot, password)
        prepared = self.session_codec.prepare()
        receipt = self._complete(self.sessions.issue_password_on, proof, prepared)
        return receipt, prepared

    def _bootstrap_actor(self, token):
        with self.store.transaction() as connection:
            try:
                actor = self.sessions.bootstrap.authenticate_on(connection, token)
            except AuthenticationError:
                return None
            self.sessions.control._current_source_on(connection, actor, self._clock())
            return actor

    def _key_proof(self, public_id, secret):
        with self.store.engine.begin() as connection:
            snapshot = self.proofs.key_snapshot_on(connection, public_id)
        if snapshot is None:
            self.codec.verify(secret, self._dummy_hash)
            return None
        return self.proofs.verify_key(snapshot, secret)

    @_safe_backend
    def key_login(self, token):
        _key_input(token)
        bootstrap = self._bootstrap_actor(token)
        parts = None if bootstrap is not None else _key_parts(token)
        if parts is None:
            self._reserve('key_login', 'bootstrap')
            prepared = self.session_codec.prepare()
            receipt = self._complete(self.sessions.issue_bootstrap_on, token, prepared)
        else:
            public_id, secret = parts
            self._reserve('key_login', public_id)
            proof = self._key_proof(public_id, secret)
            prepared = self.session_codec.prepare()
            receipt = self._complete(self.sessions.issue_key_on, proof, prepared)
        return receipt, prepared

    @_safe_backend
    def header_key(self, token):
        _key_input(token)
        bootstrap = self._bootstrap_actor(token)
        if bootstrap is not None:
            return bootstrap
        parts = _key_parts(token)
        if parts is None:
            raise AuthenticationError()
        public_id, secret = parts
        self._reserve('key_request', public_id)
        proof = self._key_proof(public_id, secret)
        with self.store.transaction() as connection:
            now = self._clock()
            actor = self.proofs.authenticate_key_on(connection, proof, now=now)
            # Record use for the Keys screen, at most once a minute per key.
            keys = self.store.tables['api_keys']
            connection.execute(keys.update().where(
                keys.c.id == actor.credential.binding_id,
                sa.or_(keys.c.last_used_at.is_(None), keys.c.last_used_at <= now - KEY_USE_INTERVAL),
            ).values(last_used_at=now))
            return actor

    @_safe_backend
    def change_password(self, actor, current_password, replacement_password):
        if type(actor) is not PrincipalContext or type(actor.credential) is not PasswordSessionEvidence:
            raise AuthenticationError()
        _password_input(current_password)
        with self.store.transaction() as connection:
            self.sessions.control._current_source_on(connection, actor, self._clock())
            users = self.store.tables['access_users']
            normalized = connection.execute(users.select().with_only_columns(users.c.normalized_login)
                .where(users.c.id == actor.principal_id)).scalar_one()
        self._reserve('password_change', normalized)
        with self.store.engine.begin() as connection:
            snapshot = self.proofs.user_password_snapshot_on(connection, actor.principal_id)
        if snapshot is None:
            self.codec.verify(current_password, self._dummy_hash)
            proof = None
        else:
            proof = self.proofs.verify_password(snapshot, current_password)
        # A bad current secret does not spend a second KDF on a replacement.
        password = None if proof is None else self.codec.prepare_chosen_password(replacement_password)
        prepared = self.session_codec.prepare()
        receipt = self._complete(self.sessions.change_password_on, actor, proof, password, prepared)
        return receipt, prepared
