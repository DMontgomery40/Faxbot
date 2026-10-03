"""Shared pre-KDF budgets; an admission never authenticates a credential.

Standalone reservations return only after confirmed commit. The caller of the
existing-transaction seam must confirm commit before expensive work. Failed,
abandoned or uncertain requests never refund a committed reservation.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import json
import re
import uuid

import sqlalchemy as sa

from .credentials import normalize_login, InvalidCredentialInputError
from .types import AccessUnavailableError, AuthenticationError, InvalidTransactionError


_DOMAIN = b'faxbot/access/auth-admission/v1\0'
_KEY_ID = re.compile(r'[0-9a-f]{12}', re.ASCII)
_BUCKET_ID = re.compile(r'[0-9a-f]{64}', re.ASCII)
_IDLE = timedelta(minutes=10)
_MAX_ROWS = 10000
_CLEANUP_LIMIT = 200
_MAX_RETRY = 86400


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    """Nonsecret reservation result, not a permission or credential proof."""
    allowed: bool
    retry_after_seconds: int

    def __post_init__(self):
        if (type(self.allowed) is not bool or type(self.retry_after_seconds) is not int
                or (self.allowed and self.retry_after_seconds != 0)
                or (not self.allowed and not 1 <= self.retry_after_seconds <= _MAX_RETRY)):
            raise ValueError('invalid admission receipt')


@dataclass(frozen=True)
class _Budget:
    label: str
    capacity: int
    interval: timedelta


_PASSWORD = _Budget('password', 5, timedelta(seconds=12))
_BUDGETS = {
    'password_login': _PASSWORD,
    'password_change': _PASSWORD,
    'key_login': _Budget('key-login', 5, timedelta(seconds=12)),
    'key_request': _Budget('key-request', 10, timedelta(milliseconds=200)),
}
_GLOBAL = _Budget('global', 20, timedelta(milliseconds=100))


def _safe_backend(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (AuthenticationError, InvalidTransactionError):
            raise
        except Exception:
            pass
        # Backend/SQL/commit errors may retain private parameters. The new fixed
        # error is raised outside the handler, without their exception context.
        raise AccessUnavailableError()
    return wrapped


def _input(kind, subject):
    if type(kind) is not str or not 0 < len(kind) <= 15 or kind not in _BUDGETS:
        raise AuthenticationError()
    if type(subject) is not str:
        raise AuthenticationError()
    if kind.startswith('password_'):
        normalized = None
        if 0 < len(subject) <= 100:
            try:
                _, normalized = normalize_login(subject)
            except InvalidCredentialInputError:
                pass
        if normalized is None or normalized != subject:
            raise AuthenticationError()
    elif not (kind == 'key_login' and subject == 'bootstrap'):
        if len(subject) != 12 or _KEY_ID.fullmatch(subject) is None:
            raise AuthenticationError()
    return _BUDGETS[kind]


def _clock(now):
    if type(now) is not datetime or now.tzinfo is not None:
        raise AuthenticationError()
    valid = False
    try:
        now - _IDLE
        now + _PASSWORD.interval
        valid = True
    except OverflowError:
        pass
    if not valid:
        raise AuthenticationError()
    return now


def _utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _retry(wait):
    # No float conversion or unbounded Retry-After value, including distant
    # corrupted/future clocks. A denial always retains a positive wait.
    micros = (wait.days * 86400 + wait.seconds) * 1000000 + wait.microseconds
    return min(_MAX_RETRY, max(1, (micros + 999999) // 1000000))


def _validate_row(row):
    identity, next_at, updated_at = row['id'], row['next_at'], row['updated_at']
    if (type(identity) is not str or (identity != 'global' and
            (len(identity) != 64 or _BUCKET_ID.fullmatch(identity) is None))
            or type(next_at) is not datetime or next_at.tzinfo is not None
            or type(updated_at) is not datetime or updated_at.tzinfo is not None
            or next_at < updated_at):
        raise AccessUnavailableError()


def _plan(row, budget, now):
    if row is None:
        next_at, updated_at = now, now
    else:
        _validate_row(row)
        next_at, updated_at = row['next_at'], row['updated_at']
    earliest = max(updated_at, next_at - (budget.capacity - 1) * budget.interval)
    if now < earliest:
        return None, earliest - now
    return max(now, next_at) + budget.interval, timedelta()


class AuthenticationAdmission:
    __slots__ = ('store', '_key', '_clock', '_buckets')

    @_safe_backend
    def __init__(self, store, *, installation_key: bytes, clock=None):
        if type(installation_key) is not bytes or len(installation_key) != 32:
            raise AccessUnavailableError()
        if clock is not None and not callable(clock):
            raise AccessUnavailableError()
        metadata = sa.MetaData()
        metadata.reflect(store.engine, only=['access_auth_buckets'])
        buckets = metadata.tables['access_auth_buckets']
        if set(buckets.columns.keys()) != {'id', 'next_at', 'updated_at'}:
            raise AccessUnavailableError()
        self.store, self._key = store, installation_key
        self._clock, self._buckets = clock if clock is not None else _utc_now, buckets

    def __repr__(self):
        return 'AuthenticationAdmission()'

    def _subject_id(self, budget, subject):
        digest = hmac.digest(self._key, _DOMAIN + budget.label.encode('ascii') + b'\0'
                             + subject.encode('ascii'), 'sha256')
        if type(digest) is not bytes or len(digest) != 32:
            raise AccessUnavailableError()
        return digest.hex()

    @_safe_backend
    def reserve(self, kind: str, subject: str) -> AdmissionReceipt:
        _input(kind, subject)  # Reject malformed input before acquiring a lock.
        with self.store.transaction() as connection:
            # The transaction context has acquired the exact access lock first.
            receipt = self.reserve_on(connection, kind, subject, now=self._clock())
        return receipt  # The context must have confirmed commit before return.

    def _cleanup_on(self, connection, identities, now):
        buckets = self._buckets
        # Keep this reservation's existing pair: it was already validated and
        # planned, and successful admission renews it. Do not delete then issue
        # an UPDATE against a missing planned row.
        rows = connection.execute(sa.select(buckets).where(
            buckets.c.next_at <= now - _IDLE, buckets.c.id.not_in(identities))
            .order_by(buckets.c.next_at, buckets.c.id).limit(_CLEANUP_LIMIT)).mappings().all()
        for row in rows:
            _validate_row(row)
        if rows:
            connection.execute(buckets.delete().where(buckets.c.id.in_([r['id'] for r in rows])))

    @_safe_backend
    def reserve_on(self, connection, kind: str, subject: str, *, now: datetime) -> AdmissionReceipt:
        """Caller must confirm commit before any KDF, or roll back on failure."""
        budget, now = _input(kind, subject), _clock(now)
        version = self.store.require_lock_on(connection)
        identity = self._subject_id(budget, subject)
        identities = ('global', identity)
        buckets = self._buckets
        rows = {r['id']: r for r in connection.execute(sa.select(buckets).where(
            buckets.c.id.in_(identities))).mappings()}
        plans = [_plan(rows.get(i), b, now) for i, b in zip(identities, (_GLOBAL, budget))]
        self._cleanup_on(connection, identities, now)
        wait = max(p[1] for p in plans)
        if wait > timedelta():
            return AdmissionReceipt(False, _retry(wait))
        missing = sum(i not in rows for i in identities)
        count = connection.execute(sa.select(sa.func.count()).select_from(buckets)).scalar_one()
        if count + missing > _MAX_ROWS:
            return AdmissionReceipt(False, 60)
        for identity, (next_at, _) in zip(identities, plans):
            values = {'next_at': next_at, 'updated_at': now}
            if identity in rows:
                connection.execute(buckets.update().where(buckets.c.id == identity).values(**values))
            else:
                connection.execute(buckets.insert().values(id=identity, **values))
        if kind != 'key_request':
            connection.execute(self.store.tables['access_audit'].insert().values(
                id=uuid.uuid4().hex, actor_principal_id=None, actor_key_binding_id=None,
                actor_session_id=None, operation='authentication.admit',
                target_kind='authentication', target_id=None,
                policy_version_before=version, policy_version_after=version,
                outcome='allowed', details=json.dumps({'kind': kind}, separators=(',', ':')),
                created_at=now))
        return AdmissionReceipt(True, 0)
