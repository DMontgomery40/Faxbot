"""Single-use, short-lived capabilities bound to the principal that minted them.

Used for a terminal handshake ticket (an opaque 256-bit token) or a mobile
pairing code (six digits, typed or scanned). The secret is returned once and
only a domain-separated digest is stored; active secrets are unique per kind. Minting authorizes
the caller-named permission at installation in the same locked transaction.
Redemption is one conditional UPDATE, so exactly one worker can consume a secret,
and only while it is unexpired and its principal (and session, if any) is live.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import uuid

import sqlalchemy as sa

from .mutation_types import MutationDeniedError, MutationReason
from .types import AccessUnavailableError, AuthenticationError, DecisionReason, ResourceRef

KINDS = frozenset({'terminal', 'pairing'})
MAX_TTL = timedelta(hours=1)
_DOMAIN = b'faxbot-capability-v1\x00'


class CapabilityError(AuthenticationError):
    """Unknown, wrong-kind, expired, already used or no-longer-live capability."""
    code = 'capability_invalid'


@dataclass(frozen=True)
class CapabilityRecord:
    id: str
    kind: str
    principal_id: str
    session_id: str | None
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    metadata: dict


@dataclass(frozen=True, repr=False)
class IssuedCapability:
    """Show ``secret`` to the requester once; it is never stored or logged."""
    id: str
    expires_at: datetime
    secret: str = field(repr=False)

    def __repr__(self):
        return 'IssuedCapability()'


def _digest(kind, secret):
    return hashlib.sha256(_DOMAIN + kind.encode('ascii') + b'\x00' + secret.encode('utf-8')).hexdigest()


def _secret(kind):
    return f'{secrets.randbelow(10 ** 6):06d}' if kind == 'pairing' else secrets.token_urlsafe(32)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CapabilityService:
    def __init__(self, store, control, *, clock=None):
        if getattr(control, 'store', None) is not store:
            raise AccessUnavailableError()
        self.store, self.control = store, control
        self._clock = clock or _utcnow
        try:
            self.table = sa.Table('access_capabilities', sa.MetaData(), autoload_with=store.engine)
        except sa.exc.SQLAlchemyError:
            raise AccessUnavailableError() from None

    def _audit(self, connection, *, principal_id, binding_id, session_id, operation, target_id, details, now):
        state, audit = self.store.tables['access_state'], self.store.tables['access_audit']
        version = connection.execute(sa.select(state.c.policy_version).where(state.c.id == 'state')).scalar_one()
        connection.execute(audit.insert().values(id=uuid.uuid4().hex, actor_principal_id=principal_id,
            actor_key_binding_id=binding_id, actor_session_id=session_id, operation=operation,
            target_kind='capability', target_id=target_id, policy_version_before=version,
            policy_version_after=version, outcome='allowed',
            details=json.dumps(details, separators=(',', ':'), sort_keys=True), created_at=now))

    def mint(self, kind, actor, ttl, metadata=None, *, permission):
        """Issue a capability after authorizing ``permission`` at installation.

        Returns an IssuedCapability(id, expires_at, secret). Raises
        MutationDeniedError(forbidden|reset_required) when not permitted and
        StaleCredentialError when the actor's credential is no longer current.
        """
        metadata = {} if metadata is None else metadata
        if (kind not in KINDS or type(ttl) is not timedelta or not timedelta(seconds=1) <= ttl <= MAX_TTL
                or type(metadata) is not dict or type(permission) is not str):
            raise MutationDeniedError(MutationReason.INVALID_INPUT)
        try:
            encoded = json.dumps(metadata, separators=(',', ':'), sort_keys=True, ensure_ascii=True)
        except (TypeError, ValueError):
            raise MutationDeniedError(MutationReason.INVALID_INPUT) from None
        if len(encoded) > 2048:
            raise MutationDeniedError(MutationReason.INVALID_INPUT)
        identity = uuid.uuid4().hex
        with self.store.transaction() as connection:
            now = self._clock()
            decision = self.control.authorize_on(connection, actor, permission, ResourceRef('installation'), now=now)
            if not decision.allowed:
                denied = (MutationReason.RESET_REQUIRED if decision.reason is DecisionReason.RESET_REQUIRED
                          else MutationReason.INVALID_INPUT if decision.reason is DecisionReason.UNKNOWN_PERMISSION
                          else MutationReason.FORBIDDEN)
                raise MutationDeniedError(denied)
            evidence = actor.credential
            session_id = getattr(evidence, 'session_id', None)
            connection.execute(self.table.delete().where(self.table.c.expires_at <= now))
            # Minting holds the access lock, so this uniqueness check cannot race.
            for _ in range(32):
                secret = _secret(kind)
                if connection.execute(sa.select(self.table.c.id).where(
                        self.table.c.secret_hash == _digest(kind, secret))).first() is None:
                    break
            else:
                raise AccessUnavailableError()
            expires_at = now + ttl
            connection.execute(self.table.insert().values(id=identity, kind=kind,
                secret_hash=_digest(kind, secret), principal_id=actor.principal_id, session_id=session_id,
                issued_at=now, expires_at=expires_at, consumed_at=None, metadata=encoded))
            self._audit(connection, principal_id=actor.principal_id,
                binding_id=getattr(evidence, 'binding_id', None), session_id=session_id,
                operation='capability.issue', target_id=identity, details={'kind': kind}, now=now)
        return IssuedCapability(identity, expires_at, secret)

    def consume(self, kind, secret, now=None):
        """Redeem a secret exactly once; returns its CapabilityRecord or raises CapabilityError."""
        if kind not in KINDS or type(secret) is not str or not 0 < len(secret) <= 256:
            raise CapabilityError()
        t = self.table
        principals, sessions = self.store.tables['access_principals'], self.store.tables['access_sessions']
        live_principal = sa.exists(sa.select(1).where(principals.c.id == t.c.principal_id, principals.c.enabled == 1))
        live_session = sa.or_(t.c.session_id.is_(None), sa.exists(sa.select(1).where(
            sessions.c.id == t.c.session_id, sessions.c.revoked_at.is_(None))))
        failed = False
        record = None
        try:
            with self.store.engine.begin() as connection:
                now = now or self._clock()
                digest = _digest(kind, secret)
                result = connection.execute(t.update().where(t.c.secret_hash == digest, t.c.kind == kind,
                    t.c.consumed_at.is_(None), t.c.expires_at > now, live_principal, live_session)
                    .values(consumed_at=now))
                if result.rowcount == 1:
                    row = connection.execute(sa.select(t).where(t.c.secret_hash == digest)).mappings().one()
                    record = CapabilityRecord(row['id'], row['kind'], row['principal_id'], row['session_id'],
                        row['issued_at'], row['expires_at'], row['consumed_at'], json.loads(row['metadata']))
                    self._audit(connection, principal_id=record.principal_id, binding_id=None,
                        session_id=record.session_id, operation='capability.consume', target_id=record.id,
                        details={'kind': kind}, now=now)
        except sa.exc.SQLAlchemyError:
            failed = True
        if failed:
            raise AccessUnavailableError()
        if record is None:
            raise CapabilityError()
        return record

    def purge(self, now=None):
        """Delete expired capabilities; returns the number removed."""
        try:
            with self.store.engine.begin() as connection:
                return connection.execute(self.table.delete().where(
                    self.table.c.expires_at <= (now or self._clock()))).rowcount
        except sa.exc.SQLAlchemyError:
            raise AccessUnavailableError() from None
