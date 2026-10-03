"""Server-private exact-source authentication proofs on a supplied AccessStore.

Read one joined source in a short caller-owned transaction, close it, then
verify outside every installation lock. Verification performs no database
operation. Completion re-reads under the exact store lock. PasswordAdmission
contains no authority and is for session issuance in that same transaction.
"""
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import NamedTuple
from weakref import WeakKeyDictionary

import sqlalchemy as sa

from .credentials import (_PrivateValue, _valid_public_key_id, normalize_login,
                          InvalidCredentialInputError)
from .types import (AccessUnavailableError, AuthenticationError, InvalidTransactionError,
                    KeyEvidence, PrincipalContext, StaleCredentialError)


_CONSTRUCTION = object()


def _safe_storage(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sa.exc.SQLAlchemyError:
            pass
        raise AccessUnavailableError()
    return wrapped


def _identity(value):
    return type(value) is str and 0 < len(value) <= 40 and all(32 <= ord(c) < 127 for c in value)


def _epoch(value):
    return type(value) is int and value >= 1


class _PasswordSource(NamedTuple):
    principal_id: str
    login: str
    normalized_login: str
    password_hash: str
    principal_security_version: int
    principal_version: int
    password_version: int
    password_change_required: int

    def __repr__(self):
        return '_PasswordSource()'


class _KeySource(NamedTuple):
    principal_id: str
    principal_kind: str
    principal_security_version: int
    principal_version: int
    binding_id: str
    binding_security_version: int
    binding_version: int
    public_key_id: str
    key_hash: str

    def __repr__(self):
        return '_KeySource()'


class _SourceMaterial(_PrivateValue):
    __slots__ = ('_source',)

    def __init__(self, seam, source):
        if seam is not _CONSTRUCTION:
            raise TypeError('private credential material')
        object.__setattr__(self, '_source', source)


class _Snapshot(_SourceMaterial):
    __slots__ = ('_read_transaction',)

    def __init__(self, seam, source, transaction):
        super().__init__(seam, source)
        object.__setattr__(self, '_read_transaction', transaction)


class PasswordSnapshot(_Snapshot):
    __slots__ = ()


class KeySnapshot(_Snapshot):
    __slots__ = ()


class VerifiedPasswordProof(_SourceMaterial):
    __slots__ = ()


class VerifiedKeyProof(_SourceMaterial):
    __slots__ = ()


@dataclass(frozen=True, repr=False)
class PasswordAdmission:
    """Safe identity/epochs/reset state, for same-transaction session issuance.

    This is neither a PrincipalContext nor a permission grant. A later session
    adapter must consume it only inside the locked completion transaction.
    """
    principal_id: str
    principal_security_version: int
    principal_version: int
    password_version: int
    password_change_required: bool

    def __repr__(self):
        return 'PasswordAdmission()'


class CredentialProofs:
    def __init__(self, store, codec):
        self.store, self.codec, self.tables = store, codec, store.tables
        # Weak identity registries establish provenance, not a credential cache.
        # Store the immutable captured tuple independently of the object's
        # private attributes, so copying/tampering cannot mint a verified source.
        self._password_snapshots = WeakKeyDictionary()
        self._key_snapshots = WeakKeyDictionary()
        self._password_proofs = WeakKeyDictionary()
        self._key_proofs = WeakKeyDictionary()

    def _password_query(self):
        p, u = self.tables['access_principals'], self.tables['access_users']
        return sa.select(p.c.id.label('principal_id'), p.c.kind, p.c.enabled,
                         p.c.security_version.label('principal_security_version'),
                         p.c.version.label('principal_version'), u.c.login, u.c.normalized_login,
                         u.c.password_hash, u.c.password_version, u.c.password_change_required
                         ).select_from(p.join(u, u.c.id == p.c.id))

    def _password_source(self, row):
        if row is None or row.kind != 'user' or row.enabled != 1 or not _identity(row.principal_id):
            return None
        if not all(_epoch(value) for value in (row.principal_security_version, row.principal_version, row.password_version)):
            return None
        if type(row.password_change_required) is not int or row.password_change_required not in (0, 1):
            return None
        try:
            display, normalized = normalize_login(row.login)
        except InvalidCredentialInputError:
            return None
        if display != row.login or normalized != row.normalized_login or not self.codec.supports_hash(row.password_hash):
            return None
        return _PasswordSource(row.principal_id, row.login, row.normalized_login, row.password_hash,
                               row.principal_security_version, row.principal_version,
                               row.password_version, row.password_change_required)

    def _key_query(self):
        p, b, k, u = (self.tables[name] for name in ('access_principals', 'access_key_bindings', 'api_keys', 'access_users'))
        return sa.select(p.c.id.label('principal_id'), p.c.kind, p.c.enabled,
                         p.c.security_version.label('principal_security_version'),
                         p.c.version.label('principal_version'), b.c.id.label('binding_id'),
                         b.c.state, b.c.security_version.label('binding_security_version'),
                         b.c.version.label('binding_version'), b.c.revoked_at.label('binding_revoked_at'),
                         k.c.key_id.label('public_key_id'), k.c.key_hash,
                         k.c.revoked_at.label('key_revoked_at'), k.c.expires_at,
                         u.c.password_version, u.c.password_change_required
                         ).select_from(p.join(b, b.c.principal_id == p.c.id).join(k, k.c.id == b.c.id).outerjoin(u, u.c.id == p.c.id))

    def _key_source(self, row, now=None):
        if (row is None or row.kind not in ('user', 'integration') or row.enabled != 1
                or not _identity(row.principal_id) or not _identity(row.binding_id)
                or not _valid_public_key_id(row.public_key_id)
                or row.state != 'active' or row.binding_revoked_at is not None or row.key_revoked_at is not None
                or not self.codec.supports_hash(row.key_hash)):
            return None
        if not all(_epoch(value) for value in (row.principal_security_version, row.principal_version,
                                               row.binding_security_version, row.binding_version)):
            return None
        if row.kind == 'user' and (not _epoch(row.password_version) or type(row.password_change_required) is not int
                                   or row.password_change_required not in (0, 1)):
            return None
        if row.expires_at is not None:
            if type(row.expires_at) is not datetime or row.expires_at.tzinfo is not None:
                return None
            if now is not None and row.expires_at <= now:
                return None
        return _KeySource(row.principal_id, row.kind, row.principal_security_version, row.principal_version,
                          row.binding_id, row.binding_security_version, row.binding_version,
                          row.public_key_id, row.key_hash)

    def _password_snapshot(self, transaction, row):
        source = self._password_source(row)
        if source is None:
            return None
        snapshot = PasswordSnapshot(_CONSTRUCTION, source, transaction)
        self._password_snapshots[snapshot] = (source, transaction)
        return snapshot

    @_safe_storage
    def password_snapshot_on(self, connection, normalized_login: str) -> PasswordSnapshot | None:
        transaction = self.store._transaction_on(connection)
        _, normalized = normalize_login(normalized_login)
        if normalized != normalized_login:
            raise InvalidCredentialInputError()
        u = self.tables['access_users']
        row = connection.execute(self._password_query().where(u.c.normalized_login == normalized_login)).first()
        return self._password_snapshot(transaction, row)

    @_safe_storage
    def user_password_snapshot_on(self, connection, principal_id: str) -> PasswordSnapshot | None:
        transaction = self.store._transaction_on(connection)
        if not _identity(principal_id):
            raise InvalidCredentialInputError()
        p = self.tables['access_principals']
        row = connection.execute(self._password_query().where(p.c.id == principal_id)).first()
        return self._password_snapshot(transaction, row)

    @_safe_storage
    def key_snapshot_on(self, connection, public_key_id: str) -> KeySnapshot | None:
        transaction = self.store._transaction_on(connection)
        if not _valid_public_key_id(public_key_id):
            raise InvalidCredentialInputError()
        k = self.tables['api_keys']
        row = connection.execute(self._key_query().where(k.c.key_id == public_key_id)).first()
        source = self._key_source(row)
        if source is None:
            return None
        snapshot = KeySnapshot(_CONSTRUCTION, source, transaction)
        self._key_snapshots[snapshot] = (source, transaction)
        return snapshot

    @staticmethod
    def _verified_snapshot(snapshot, expected_type, registry):
        if type(snapshot) is not expected_type or snapshot not in registry:
            raise AuthenticationError()
        source, transaction = registry[snapshot]
        if snapshot._source is not source or snapshot._read_transaction is not transaction:
            raise AuthenticationError()
        if transaction.is_active:
            raise InvalidTransactionError()
        return source

    def verify_password(self, snapshot: PasswordSnapshot, secret: str) -> VerifiedPasswordProof | None:
        source = self._verified_snapshot(snapshot, PasswordSnapshot, self._password_snapshots)
        if not self.codec.verify(secret, source.password_hash):
            return None
        proof = VerifiedPasswordProof(_CONSTRUCTION, source)
        self._password_proofs[proof] = source
        return proof

    def verify_key(self, snapshot: KeySnapshot, secret: str) -> VerifiedKeyProof | None:
        source = self._verified_snapshot(snapshot, KeySnapshot, self._key_snapshots)
        if not self.codec.verify(secret, source.key_hash):
            return None
        proof = VerifiedKeyProof(_CONSTRUCTION, source)
        self._key_proofs[proof] = source
        return proof

    @staticmethod
    def _verified_proof(proof, expected_type, registry, now):
        if type(now) is not datetime or now.tzinfo is not None:
            raise AuthenticationError()
        if type(proof) is not expected_type or proof not in registry or proof._source is not registry[proof]:
            raise AuthenticationError()
        return registry[proof]

    @_safe_storage
    def revalidate_password_on(self, connection, proof: VerifiedPasswordProof, *, now: datetime) -> PasswordAdmission:
        self.store.require_lock_on(connection)
        source = self._verified_proof(proof, VerifiedPasswordProof, self._password_proofs, now)
        p = self.tables['access_principals']
        row = connection.execute(self._password_query().where(p.c.id == source.principal_id)).first()
        current = self._password_source(row)
        if current is None or current != source:
            raise StaleCredentialError()
        return PasswordAdmission(source.principal_id, source.principal_security_version, source.principal_version,
                                 source.password_version, bool(source.password_change_required))

    @_safe_storage
    def authenticate_key_on(self, connection, proof: VerifiedKeyProof, *, now: datetime) -> PrincipalContext:
        self.store.require_lock_on(connection)
        source = self._verified_proof(proof, VerifiedKeyProof, self._key_proofs, now)
        b = self.tables['access_key_bindings']
        row = connection.execute(self._key_query().where(b.c.id == source.binding_id)).first()
        current = self._key_source(row, now)
        if current is None or current != source:
            raise StaleCredentialError()
        return PrincipalContext(source.principal_id, source.principal_security_version,
                                KeyEvidence(source.binding_id, source.binding_security_version),
                                'key:' + source.public_key_id)
