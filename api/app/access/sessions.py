"""Locked persistent sessions and narrow current-password lifecycle operations.

No HTTP transport, password KDF, credential disclosure, or nested transactions.
Standalone mutators commit expected denial audit before raising safe errors.
Connection-first callers must roll back an aggregate on denial if they already
made business writes; a denial outcome is not permission to commit partial work.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from .credentials import CredentialCodec, PreparedChosenPassword
from .mutation_types import StaleVersionError
from .mutations import _Graph
from .session_codec import SessionCodec, PreparedSession
from .types import (
    AccessError, AccessUnavailableError, AuthenticationError, BootstrapEvidence,
    InvalidTransactionError, KeyEvidence, KeySessionEvidence,
    PasswordSessionEvidence, PrincipalContext, ResourceRef, StaleCredentialError,
)
from .proofs import VerifiedPasswordProof, VerifiedKeyProof


def _id(value):
    return type(value) is str and 0 < len(value) <= 40 and all(32 <= ord(c) < 127 for c in value)


def _time(value):
    return type(value) is datetime and value.tzinfo is None


def _clock(value):
    if not _time(value):
        return False
    try:
        value + timedelta(hours=12)
        value - timedelta(hours=12)
    except OverflowError:
        return False
    return True


def _epoch(value):
    return type(value) is int and value >= 1


@dataclass(frozen=True)
class SessionReceipt:
    session_id: str
    principal_id: str
    source_kind: str
    expires_at: datetime
    password_change_required: bool


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    source_kind: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    current: bool


@dataclass(frozen=True)
class SessionChangeReceipt:
    session_id: str
    changed: bool
    policy_version: int


class SessionReason(str, Enum):
    UNAUTHENTICATED = 'unauthenticated'
    FORBIDDEN = 'forbidden'
    RESET_REQUIRED = 'reset_required'
    STALE_VERSION = 'stale_version'
    INVALID_TARGET = 'invalid_target'
    INVALID_INPUT = 'invalid_input'


class SessionDeniedError(AccessError):
    def __init__(self, reason: SessionReason):
        if type(reason) is not SessionReason:
            raise ValueError('invalid session reason')
        self.reason, self.code = reason, reason.value
        super().__init__()


@dataclass(frozen=True)
class SessionCursor:
    created_at: datetime
    session_id: str

    def __post_init__(self):
        if not _time(self.created_at) or not _id(self.session_id):
            raise SessionDeniedError(SessionReason.INVALID_INPUT)


@dataclass(frozen=True)
class SessionPage:
    items: tuple[SessionSummary, ...]
    next_cursor: SessionCursor | None


@dataclass(frozen=True)
class SessionOutcome:
    receipt: SessionReceipt | SessionChangeReceipt | None
    reason: SessionReason | None
    policy_version: int

    def __post_init__(self):
        if ((self.receipt is None) == (self.reason is None)
                or (self.reason is not None and type(self.reason) is not SessionReason)
                or not _epoch(self.policy_version)):
            raise ValueError('invalid session outcome')


class _Denied(Exception):
    def __init__(self, reason):
        self.reason = reason


def _deny(reason=SessionReason.INVALID_INPUT):
    raise _Denied(reason)


@dataclass
class _Attempt:
    connection: Connection
    now: datetime
    before: int
    operation: str
    attribution: tuple = (None, None, None)
    target_id: str | None = None


@dataclass
class _Plan:
    receipt: SessionReceipt | SessionChangeReceipt
    writes: object
    details: dict
    policy_version: int


class AccessSessions:
    def __init__(self, store, control, proofs, bootstrap, codec, password_codec):
        if any(getattr(service, 'store', None) is not store for service in (control, proofs, bootstrap)):
            raise InvalidTransactionError()
        if type(codec) is not SessionCodec or type(password_codec) is not CredentialCodec:
            raise InvalidTransactionError()
        self.store, self.control, self.proofs, self.bootstrap = store, control, proofs, bootstrap
        self.codec, self.password_codec, self.tables = codec, password_codec, store.tables

    def _standalone(self, method, *args, **kwargs):
        failed = False
        try:
            with self.store.transaction() as connection:
                outcome = method(connection, *args, **kwargs)
        except InvalidTransactionError:
            raise
        except Exception:
            failed = True
        if failed:
            raise AccessUnavailableError()
        if outcome.reason is SessionReason.UNAUTHENTICATED:
            raise AuthenticationError()
        if outcome.reason is SessionReason.STALE_VERSION:
            raise StaleVersionError()
        if outcome.reason is not None:
            raise SessionDeniedError(outcome.reason)
        return outcome.receipt

    def _read(self, method, *args, **kwargs):
        failed = False
        try:
            with self.store.transaction() as connection:
                result = method(connection, *args, **kwargs)
        except (AuthenticationError, SessionDeniedError, InvalidTransactionError):
            raise
        except Exception:
            failed = True
        if failed:
            raise AccessUnavailableError()
        return result

    def _run(self, connection, operation, planner, *, now):
        before = self.store.require_lock_on(connection)
        attempt = _Attempt(connection, now, before, operation)
        plan, reason = None, None
        try:
            if not _clock(now):
                _deny()
            plan = planner(attempt)
        except _Denied as denied:
            reason = denied.reason
        except AuthenticationError:
            reason = SessionReason.UNAUTHENTICATED
            attempt.attribution = (None, None, None)
        after = before
        if plan is not None:
            plan.writes()
            after = plan.policy_version
            attempt.target_id = plan.receipt.session_id
            if after != before:
                state = self.tables['access_state']
                connection.execute(state.update().where(state.c.id == 'state').values(
                    policy_version=after, updated_at=now))
        details = {'reason': reason.value} if reason is not None else plan.details
        self._audit(attempt, after, reason, details)
        return SessionOutcome(None if plan is None else plan.receipt, reason, after)

    def _audit(self, attempt, after, reason, details):
        encoded = json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
        if len(encoded.encode('utf-8')) > 2048:
            raise AccessUnavailableError()
        principal, binding, session = attempt.attribution
        attempt.connection.execute(self.tables['access_audit'].insert().values(
            id=uuid.uuid4().hex, actor_principal_id=principal,
            actor_key_binding_id=binding, actor_session_id=session,
            operation=attempt.operation, target_kind='session',
            target_id=attempt.target_id if _id(attempt.target_id) else None,
            policy_version_before=attempt.before, policy_version_after=after,
            outcome='denied' if reason else 'allowed', details=encoded,
            created_at=attempt.now if _clock(attempt.now) else datetime.now(timezone.utc).replace(tzinfo=None)))

    def _source(self, attempt, actor):
        source = self.control._current_source_on(attempt.connection, actor, attempt.now)
        attempt.attribution = (actor.principal_id, source.binding_id,
            getattr(actor.credential, 'session_id', None))
        return source

    def _category(self, attempt, actor, permission):
        decision = self.control.authorize_on(attempt.connection, actor, permission,
            ResourceRef('installation'), now=attempt.now)
        if not decision.allowed:
            _deny(SessionReason.RESET_REQUIRED if decision.reason.value == 'reset_required' else SessionReason.FORBIDDEN)

    def _storage(self, attempt, prepared):
        storage = self.codec.storage(prepared)
        sessions = self.tables['access_sessions']
        if attempt.connection.execute(sa.select(sessions.c.id).where(sa.or_(
                sessions.c.id == storage.id, sessions.c.token_hash == storage.token_hash))).first() is not None:
            _deny()
        return storage

    def _new_session(self, attempt, prepared, principal_id, security_version,
            source_kind, *, password_version=None, binding_id=None,
            binding_version=None, fingerprint=None, reset_required=False,
            key_expiry=None, storage=None, preceding=None, policy_version=None):
        storage = storage or self._storage(attempt, prepared)
        expiry = attempt.now + timedelta(hours=12)
        if key_expiry is not None:
            if not _time(key_expiry) or key_expiry <= attempt.now:
                raise AuthenticationError()
            expiry = min(expiry, key_expiry)
        receipt = SessionReceipt(storage.id, principal_id, source_kind, expiry, reset_required)
        def writes():
            if preceding is not None:
                preceding()
            attempt.connection.execute(self.tables['access_sessions'].insert().values(
                id=storage.id, principal_id=principal_id, source_kind=source_kind,
                source_key_id=binding_id, source_key_version=binding_version,
                bootstrap_fingerprint=fingerprint, password_version=password_version,
                principal_security_version=security_version,
                token_hash=storage.token_hash, csrf_hash=storage.csrf_hash,
                created_at=attempt.now, last_used_at=attempt.now, expires_at=expiry, revoked_at=None))
        return _Plan(receipt, writes, {'source_kind': source_kind},
            attempt.before if policy_version is None else policy_version)

    def issue_password_on(self, connection: Connection, proof: VerifiedPasswordProof,
            prepared: PreparedSession, *, now: datetime) -> SessionOutcome:
        def planner(a):
            admission = self.proofs.revalidate_password_on(a.connection, proof, now=a.now)
            a.attribution = (admission.principal_id, None, None)
            return self._new_session(a, prepared, admission.principal_id,
                admission.principal_security_version, 'password',
                password_version=admission.password_version,
                reset_required=admission.password_change_required)
        return self._run(connection, 'issue_password_session', planner, now=now)

    def issue_password(self, proof: VerifiedPasswordProof, prepared: PreparedSession,
            *, now: datetime) -> SessionReceipt:
        return self._standalone(self.issue_password_on, proof, prepared, now=now)

    def issue_key_on(self, connection: Connection, proof: VerifiedKeyProof,
            prepared: PreparedSession, *, now: datetime) -> SessionOutcome:
        def planner(a):
            actor = self.proofs.authenticate_key_on(a.connection, proof, now=a.now)
            source = self._source(a, actor)
            if source.reset_required:
                _deny(SessionReason.RESET_REQUIRED)
            keys = self.tables['api_keys']
            expiry = a.connection.execute(sa.select(keys.c.expires_at).where(
                keys.c.id == actor.credential.binding_id)).scalar_one()
            return self._new_session(a, prepared, actor.principal_id,
                actor.principal_security_version, 'key', binding_id=actor.credential.binding_id,
                binding_version=actor.credential.key_security_version, key_expiry=expiry)
        return self._run(connection, 'issue_key_session', planner, now=now)

    def issue_key(self, proof: VerifiedKeyProof, prepared: PreparedSession,
            *, now: datetime) -> SessionReceipt:
        return self._standalone(self.issue_key_on, proof, prepared, now=now)

    def issue_bootstrap_on(self, connection: Connection, secret: str,
            prepared: PreparedSession, *, now: datetime) -> SessionOutcome:
        def planner(a):
            actor = self.bootstrap.authenticate_on(a.connection, secret)
            self._source(a, actor)
            return self._new_session(a, prepared, actor.principal_id,
                actor.principal_security_version, 'bootstrap', fingerprint=actor.credential.fingerprint)
        return self._run(connection, 'issue_bootstrap_session', planner, now=now)

    def issue_bootstrap(self, secret: str, prepared: PreparedSession,
            *, now: datetime) -> SessionReceipt:
        return self._standalone(self.issue_bootstrap_on, secret, prepared, now=now)

    def authenticate_on(self, connection: Connection, token: str, *, now: datetime,
            touch: bool = False) -> PrincipalContext:
        self.store.require_lock_on(connection)
        if not _clock(now) or type(touch) is not bool:
            raise AuthenticationError()
        token_hash = self.codec.token_hash(token)
        sessions = self.tables['access_sessions']
        row = connection.execute(sa.select(sessions).where(sessions.c.token_hash == token_hash)).mappings().one_or_none()
        if row is None:
            raise AuthenticationError()
        csrf = self.codec.csrf_value(token)
        if not self.codec.verify_csrf(token, csrf, row['csrf_hash']):
            raise AuthenticationError()
        if row['source_kind'] == 'password':
            evidence = PasswordSessionEvidence(row['id'], row['password_version'])
            replay = 'principal:' + row['principal_id']
        elif row['source_kind'] == 'key':
            keys = self.tables['api_keys']
            public_id = connection.execute(sa.select(keys.c.key_id).where(
                keys.c.id == row['source_key_id'])).scalar_one_or_none()
            if type(public_id) is not str:
                raise StaleCredentialError()
            evidence = KeySessionEvidence(row['id'], row['source_key_id'], row['source_key_version'])
            replay = 'key:' + public_id
        elif row['source_kind'] == 'bootstrap':
            evidence = BootstrapEvidence(row['bootstrap_fingerprint'], row['id'])
            replay = 'key:env'
        else:
            raise AuthenticationError()
        actor = PrincipalContext(row['principal_id'], row['principal_security_version'], evidence, replay)
        self.control._current_source_on(connection, actor, now)
        if touch:
            connection.execute(sessions.update().where(sessions.c.id == row['id']).values(last_used_at=now))
        return actor

    def authenticate(self, token: str, *, now: datetime, touch: bool = False) -> PrincipalContext:
        return self._read(self.authenticate_on, token, now=now, touch=touch)

    def logout_on(self, connection: Connection, actor: PrincipalContext, *, now: datetime) -> SessionOutcome:
        def planner(a):
            self._source(a, actor)
            session_id = getattr(actor.credential, 'session_id', None)
            if not _id(session_id):
                _deny(SessionReason.FORBIDDEN)
            a.target_id = session_id
            receipt = SessionChangeReceipt(session_id, True, a.before)
            return _Plan(receipt, lambda: a.connection.execute(self.tables['access_sessions'].update()
                .where(self.tables['access_sessions'].c.id == session_id).values(revoked_at=a.now)),
                {'changed': True}, a.before)
        return self._run(connection, 'logout_session', planner, now=now)

    def logout(self, actor: PrincipalContext, *, now: datetime) -> SessionChangeReceipt:
        return self._standalone(self.logout_on, actor, now=now)

    def change_password_on(self, connection: Connection, actor: PrincipalContext,
            proof: VerifiedPasswordProof, password: PreparedChosenPassword,
            prepared: PreparedSession, *, now: datetime) -> SessionOutcome:
        def planner(a):
            self._source(a, actor)
            if type(actor.credential) is not PasswordSessionEvidence:
                _deny(SessionReason.FORBIDDEN)
            admission = self.proofs.revalidate_password_on(a.connection, proof, now=a.now)
            if admission.principal_id != actor.principal_id:
                raise AuthenticationError()
            if type(password) is not PreparedChosenPassword:
                _deny()
            password_hash = password._password_hash_for_storage()
            if not self.password_codec.supports_hash(password_hash):
                _deny()
            storage = self._storage(a, prepared)
            principals, users, sessions = (self.tables[n] for n in
                ('access_principals', 'access_users', 'access_sessions'))
            def preceding():
                a.connection.execute(principals.update().where(principals.c.id == admission.principal_id)
                    .values(version=admission.principal_version + 1, updated_at=a.now))
                a.connection.execute(users.update().where(users.c.id == admission.principal_id).values(
                    password_hash=password_hash, password_version=admission.password_version + 1,
                    password_change_required=0, updated_at=a.now))
                a.connection.execute(sessions.update().where(sessions.c.principal_id == admission.principal_id,
                    sessions.c.revoked_at.is_(None)).values(revoked_at=a.now))
            return self._new_session(a, prepared, admission.principal_id,
                admission.principal_security_version, 'password',
                password_version=admission.password_version + 1, storage=storage,
                preceding=preceding, policy_version=a.before + 1)
        return self._run(connection, 'change_password', planner, now=now)

    def change_password(self, actor: PrincipalContext, proof: VerifiedPasswordProof,
            password: PreparedChosenPassword, prepared: PreparedSession, *, now: datetime) -> SessionReceipt:
        return self._standalone(self.change_password_on, actor, proof, password, prepared, now=now)

    def revoke_session_on(self, connection: Connection, actor: PrincipalContext,
            session_id: str, *, expected_policy_version: int, now: datetime) -> SessionOutcome:
        def planner(a):
            source = self._source(a, actor)
            if not _epoch(expected_policy_version) or not _id(session_id):
                _deny()
            if expected_policy_version != a.before:
                _deny(SessionReason.STALE_VERSION)
            sessions = self.tables['access_sessions']
            row = a.connection.execute(sa.select(sessions).where(sessions.c.id == session_id)).mappings().one_or_none()
            if row is None:
                _deny(SessionReason.INVALID_TARGET)
            actual_session = getattr(actor.credential, 'session_id', None)
            own_current = session_id == actual_session
            own_password = (type(actor.credential) is PasswordSessionEvidence
                and row['principal_id'] == actor.principal_id and not source.reset_required)
            if not own_current and not own_password:
                self._category(a, actor, 'sessions:revoke')
                graph = _Graph(a.connection, self.tables)
                if graph.protected_principal(row['principal_id']) and not self.control.is_complete_owner_on(
                        a.connection, actor, now=a.now):
                    _deny(SessionReason.FORBIDDEN)
                if not self.control.dominates_principal_on(a.connection, actor,
                        row['principal_id'], now=a.now).allowed:
                    _deny(SessionReason.FORBIDDEN)
            a.target_id = row['id']
            changed = row['revoked_at'] is None
            def writes():
                if changed:
                    a.connection.execute(sessions.update().where(sessions.c.id == row['id']).values(revoked_at=a.now))
            return _Plan(SessionChangeReceipt(row['id'], changed, a.before), writes, {'changed': changed}, a.before)
        return self._run(connection, 'revoke_session', planner, now=now)

    def revoke_session(self, actor: PrincipalContext, session_id: str,
            *, expected_policy_version: int, now: datetime) -> SessionChangeReceipt:
        return self._standalone(self.revoke_session_on, actor, session_id,
            expected_policy_version=expected_policy_version, now=now)

    def list_sessions_on(self, connection: Connection, actor: PrincipalContext, *,
            principal_id: str | None = None, cursor: SessionCursor | None = None,
            limit: int = 50, now: datetime) -> SessionPage:
        before = self.store.require_lock_on(connection)
        if not _clock(now):
            raise AuthenticationError()
        attempt = _Attempt(connection, now, before, 'list_sessions')
        source = self._source(attempt, actor)
        try:
            if (type(limit) is not int or not 1 <= limit <= 100
                    or (cursor is not None and (type(cursor) is not SessionCursor
                        or not _time(cursor.created_at) or not _id(cursor.session_id)))):
                _deny()
            actual_session = getattr(actor.credential, 'session_id', None)
            sessions = self.tables['access_sessions']
            if principal_id is not None:
                if not _id(principal_id):
                    _deny()
                self._category(attempt, actor, 'sessions:read')
                principals = self.tables['access_principals']
                if connection.execute(sa.select(principals.c.id).where(principals.c.id == principal_id)).first() is None:
                    _deny(SessionReason.INVALID_TARGET)
                predicate = sessions.c.principal_id == principal_id
            else:
                if not _id(actual_session):
                    _deny(SessionReason.FORBIDDEN)
                predicate = sessions.c.principal_id == actor.principal_id
                if type(actor.credential) is not PasswordSessionEvidence or source.reset_required:
                    predicate = sa.and_(predicate, sessions.c.id == actual_session)
            query = sa.select(sessions).where(predicate)
            if cursor is not None:
                query = query.where(sa.or_(sessions.c.created_at < cursor.created_at,
                    sa.and_(sessions.c.created_at == cursor.created_at, sessions.c.id < cursor.session_id)))
            rows = connection.execute(query.order_by(sessions.c.created_at.desc(), sessions.c.id.desc())
                .limit(limit + 1)).mappings().all()
        except _Denied as denied:
            raise SessionDeniedError(denied.reason) from None
        items = tuple(SessionSummary(r['id'], r['source_kind'], r['created_at'],
            r['last_used_at'], r['expires_at'], r['revoked_at'], r['id'] == actual_session) for r in rows[:limit])
        next_cursor = SessionCursor(items[-1].created_at, items[-1].session_id) if len(rows) > limit else None
        return SessionPage(items, next_cursor)

    def list_sessions(self, actor: PrincipalContext, *, principal_id: str | None = None,
            cursor: SessionCursor | None = None, limit: int = 50, now: datetime) -> SessionPage:
        return self._read(self.list_sessions_on, actor, principal_id=principal_id,
            cursor=cursor, limit=limit, now=now)
