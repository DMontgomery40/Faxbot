"""Exact-source proofs on isolated migrated databases, never route acceptance."""
import dataclasses
from datetime import datetime, timedelta, timezone
import importlib
import json
import pickle
import uuid

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema, create_database_engine
from api.app.access.credentials import CredentialCodec, InvalidCredentialInputError, CredentialServiceError
from api.app.access.policy import AccessControl
from api.app.access.store import AccessStore
from api.app.access.types import (InvalidTransactionError, AuthenticationError, StaleCredentialError,
                                  KeyEvidence, PrincipalContext, ResourceRef, DecisionReason)

NOW = datetime(2026, 10, 3, 12)
HASH = 'pbkdf2$AAECAwQFBgcICQoLDA0ODw$WB_oMn6_nBke1Dua7Bf4IXYsCiSqNakzyNxknNxg_u4$rounds=200000'
PUBLIC_ID = 'abcde0123456'


def module():
    name = 'api.app.access.proofs'
    assert importlib.util.find_spec(name) is not None, 'exact-source credential proofs are missing'
    return importlib.import_module(name)


class ProofWorld:
    def __init__(self, engine):
        self.engine = engine
        upgrade_schema(engine)
        self.store = AccessStore(engine)
        self.control = AccessControl(self.store)
        self.codec = CredentialCodec()
        self.proofs = module().CredentialProofs(self.store, self.codec)
        self.tables = self.store.tables
        self.insert('access_principals', id='alice', kind='user', display_name='Alice', security_version=1)
        self.insert('access_users', id='alice', login='Alice', normalized_login='alice', password_hash=HASH,
                    password_change_required=0, password_version=1)
        self.insert('access_principals', id='integration', kind='integration', display_name='Integration', security_version=1)
        self.insert('api_keys', id='internal-key-row', key_id=PUBLIC_ID, key_hash=HASH,
                    created_at=NOW - timedelta(minutes=5), expires_at=NOW + timedelta(hours=1), revoked_at=None)
        self.insert('access_key_bindings', id='internal-key-row', principal_id='alice', state='active',
                    security_version=1, revoked_at=None)

    def insert(self, table_name, **values):
        table = self.tables[table_name]
        for field in ('created_at', 'updated_at'):
            if field in table.c:
                values.setdefault(field, NOW - timedelta(minutes=5))
        if 'version' in table.c:
            values.setdefault('version', 1)
        if 'enabled' in table.c:
            values.setdefault('enabled', 1)
        values.setdefault('id', uuid.uuid4().hex)
        with self.engine.begin() as connection:
            connection.execute(table.insert().values(**values))
        return values['id']

    def update(self, name, identity, **values):
        table = self.tables[name]
        with self.engine.begin() as connection:
            connection.execute(table.update().where(table.c.id == identity).values(**values))

    def password_snapshot(self):
        with self.engine.begin() as connection:
            return self.proofs.password_snapshot_on(connection, 'alice')

    def key_snapshot(self):
        with self.engine.begin() as connection:
            return self.proofs.key_snapshot_on(connection, PUBLIC_ID)

    def password_proof(self):
        return self.proofs.verify_password(self.password_snapshot(), 'password')

    def key_proof(self):
        return self.proofs.verify_key(self.key_snapshot(), 'password')


@pytest.fixture
def world(database):
    return ProofWorld(database)


def test_password_completion_is_safe_admission_only(world):
    proof = world.password_proof()
    with world.store.transaction() as connection:
        admission = world.proofs.revalidate_password_on(connection, proof, now=NOW)
    assert type(admission) is module().PasswordAdmission
    assert not isinstance(admission, PrincipalContext)
    assert admission.principal_id == 'alice'
    assert admission.principal_security_version == 1
    assert admission.principal_version == 1
    assert admission.password_version == 1
    assert admission.password_change_required is False
    assert not hasattr(admission, 'credential') and not hasattr(admission, 'replay_scope')
    assert HASH not in repr(admission)
    assert 'password' not in dataclasses.asdict(admission).values()


def test_snapshots_use_one_joined_read_without_installation_lock(world):
    statements = []
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    sa.event.listen(world.engine, 'before_cursor_execute', record)
    try:
        with world.engine.begin() as connection:
            for reader, identity in ((world.proofs.password_snapshot_on, 'alice'),
                                     (world.proofs.user_password_snapshot_on, 'alice'),
                                     (world.proofs.key_snapshot_on, PUBLIC_ID)):
                statements.clear()
                assert reader(connection, identity) is not None
                assert len(statements) == 1
                assert statements[0].lstrip().upper().startswith('SELECT')
                assert 'JOIN' in statements[0].upper()
                with pytest.raises(InvalidTransactionError):
                    world.store.require_lock_on(connection)
    finally:
        sa.event.remove(world.engine, 'before_cursor_execute', record)


def test_hashing_requires_closed_snapshot_transaction_and_performs_no_database_operation(world):
    with world.engine.begin() as connection:
        password = world.proofs.password_snapshot_on(connection, 'alice')
        key = world.proofs.key_snapshot_on(connection, PUBLIC_ID)
        for verifier, snapshot in ((world.proofs.verify_password, password), (world.proofs.verify_key, key)):
            with pytest.raises(InvalidTransactionError):
                verifier(snapshot, 'password')
    def no_database(*args):
        pytest.fail('secret verification accessed database')
    sa.event.listen(world.engine, 'before_cursor_execute', no_database)
    try:
        assert world.proofs.verify_password(password, 'wrong') is None
        assert world.proofs.verify_key(key, 'wrong') is None
        assert world.proofs.verify_password(password, 'password') is not None
        assert world.proofs.verify_key(key, 'password') is not None
    finally:
        sa.event.remove(world.engine, 'before_cursor_execute', no_database)


@pytest.mark.parametrize('login', ['Alice', ' alice ', 'invalid login', 'älice', 'a' * 101, None])
def test_authentication_lookup_requires_valid_already_normalized_login(world, login):
    with world.engine.begin() as connection:
        with pytest.raises(InvalidCredentialInputError) as error:
            world.proofs.password_snapshot_on(connection, login)
    assert str(error.value) == 'invalid_credential_input'
    assert error.value.__context__ is None


def test_unknown_or_unsupported_sources_do_not_mint_snapshots(world):
    with world.engine.begin() as connection:
        assert world.proofs.password_snapshot_on(connection, 'absent') is None
        assert world.proofs.user_password_snapshot_on(connection, 'absent') is None
        assert world.proofs.key_snapshot_on(connection, '012345abcdef') is None
    world.update('access_users', 'alice', password_hash='unsupported')
    world.update('api_keys', 'internal-key-row', key_hash='unsupported')
    assert world.password_snapshot() is None
    assert world.key_snapshot() is None


@pytest.mark.parametrize('kind', ['password', 'key'])
def test_snapshots_and_completion_require_exact_engine_active_non_nested_transaction(world, tmp_path, kind):
    snapshotter = world.proofs.password_snapshot_on if kind == 'password' else world.proofs.key_snapshot_on
    identity = 'alice' if kind == 'password' else PUBLIC_ID
    proof = world.password_proof() if kind == 'password' else world.key_proof()
    completer = world.proofs.revalidate_password_on if kind == 'password' else world.proofs.authenticate_key_on
    with world.engine.connect() as connection:
        with pytest.raises(InvalidTransactionError):
            snapshotter(connection, identity)
        with pytest.raises(InvalidTransactionError):
            completer(connection, proof, now=NOW)
        connection.begin()
        with pytest.raises(InvalidTransactionError):
            completer(connection, proof, now=NOW)
        world.store.lock_on(connection)
        with connection.begin_nested():
            with pytest.raises(InvalidTransactionError):
                snapshotter(connection, identity)
            with pytest.raises(InvalidTransactionError):
                completer(connection, proof, now=NOW)
        connection.rollback()
    with pytest.raises(InvalidTransactionError):
        snapshotter(connection, identity)
    other = create_database_engine('sqlite:///' + str(tmp_path / 'other.db'))
    try:
        upgrade_schema(other)
        other_store = AccessStore(other)
        with other_store.transaction() as connection:
            with pytest.raises(InvalidTransactionError):
                snapshotter(connection, identity)
            with pytest.raises(InvalidTransactionError):
                completer(connection, proof, now=NOW)
    finally:
        other.dispose()


@pytest.mark.parametrize('table,identity,changes', [
    ('access_users', 'alice', {'password_hash': HASH.replace('WB_o', 'XB_o')}),
    ('access_users', 'alice', {'password_version': 2}),
    ('access_users', 'alice', {'login': 'Renamed', 'normalized_login': 'renamed'}),
    ('access_users', 'alice', {'password_change_required': 1}),
    ('access_principals', 'alice', {'security_version': 2}),
    ('access_principals', 'alice', {'version': 2}),
    ('access_principals', 'alice', {'enabled': 0}),
    ('access_principals', 'alice', {'kind': 'integration'}),
])
def test_password_proof_rejects_source_changed_after_verification(world, table, identity, changes):
    proof = world.password_proof()
    world.update(table, identity, **changes)
    with world.store.transaction() as connection:
        with pytest.raises(StaleCredentialError):
            world.proofs.revalidate_password_on(connection, proof, now=NOW)


def test_old_password_proof_cannot_be_laundered_into_current_epochs(world):
    proof = world.password_proof()
    world.update('access_principals', 'alice', security_version=2, version=2)
    world.update('access_users', 'alice', password_version=2, password_change_required=1)
    with world.store.transaction() as connection:
        with pytest.raises(StaleCredentialError):
            world.proofs.revalidate_password_on(connection, proof, now=NOW)
    fresh = world.password_proof()
    with world.store.transaction() as connection:
        admission = world.proofs.revalidate_password_on(connection, fresh, now=NOW)
    assert admission.principal_security_version == 2 and admission.password_version == 2
    assert admission.password_change_required is True


@pytest.mark.parametrize('table,identity,changes', [
    ('api_keys', 'internal-key-row', {'key_hash': HASH.replace('WB_o', 'XB_o')}),
    ('api_keys', 'internal-key-row', {'key_id': '012345abcdef'}),
    ('api_keys', 'internal-key-row', {'revoked_at': NOW}),
    ('api_keys', 'internal-key-row', {'expires_at': NOW}),
    ('access_key_bindings', 'internal-key-row', {'security_version': 2}),
    ('access_key_bindings', 'internal-key-row', {'version': 2}),
    ('access_key_bindings', 'internal-key-row', {'principal_id': 'integration'}),
    ('access_key_bindings', 'internal-key-row', {'state': 'pending_review'}),
    ('access_key_bindings', 'internal-key-row', {'state': 'revoked', 'revoked_at': NOW}),
    ('access_principals', 'alice', {'security_version': 2}),
    ('access_principals', 'alice', {'version': 2}),
    ('access_principals', 'alice', {'enabled': 0}),
])
def test_key_proof_rejects_source_changed_after_verification(world, table, identity, changes):
    proof = world.key_proof()
    world.update(table, identity, **changes)
    with world.store.transaction() as connection:
        with pytest.raises(StaleCredentialError):
            world.proofs.authenticate_key_on(connection, proof, now=NOW)


def test_key_completion_uses_stored_public_id_not_internal_row_id(world):
    proof = world.key_proof()
    with world.store.transaction() as connection:
        actor = world.proofs.authenticate_key_on(connection, proof, now=NOW)
    assert actor == PrincipalContext('alice', 1, KeyEvidence('internal-key-row', 1), 'key:abcde0123456')
    world.update('api_keys', 'internal-key-row', expires_at=NOW)
    with world.store.transaction() as connection:
        with pytest.raises(StaleCredentialError):
            world.proofs.authenticate_key_on(connection, proof, now=NOW)
    world.update('api_keys', 'internal-key-row', expires_at=NOW + timedelta(microseconds=1))
    with world.store.transaction() as connection:
        assert world.proofs.authenticate_key_on(connection, proof, now=NOW).replay_scope == 'key:abcde0123456'


@pytest.mark.parametrize('table,identity,changes', [
    ('access_principals', 'alice', {'enabled': 0}),
    ('access_key_bindings', 'internal-key-row', {'state': 'pending_review'}),
    ('access_key_bindings', 'internal-key-row', {'state': 'revoked', 'revoked_at': NOW}),
    ('api_keys', 'internal-key-row', {'revoked_at': NOW}),
])
def test_disabled_pending_or_revoked_source_has_no_key_snapshot(world, table, identity, changes):
    world.update(table, identity, **changes)
    assert world.key_snapshot() is None
    if table == 'access_principals':
        assert world.password_snapshot() is None


def test_same_hash_rotation_still_rejects_verified_old_binding_epoch(world):
    proof = world.key_proof()
    world.update('access_key_bindings', 'internal-key-row', security_version=2, version=2)
    with world.store.transaction() as connection:
        with pytest.raises(StaleCredentialError):
            world.proofs.authenticate_key_on(connection, proof, now=NOW)
    fresh_proof = world.key_proof()
    with world.store.transaction() as connection:
        fresh = world.proofs.authenticate_key_on(connection, fresh_proof, now=NOW)
    assert fresh.credential.key_security_version == 2


def test_role_change_keeps_proof_valid_and_current_ceiling_and_reset_remain_live(world):
    world.insert('access_roles', id='custom', name='Custom', normalized_name='custom', description='', kind='custom')
    world.insert('access_role_permissions', role_id='custom', permission_id='settings:read')
    world.insert('access_assignments', principal_id='alice', group_id=None, role_id='custom', resource_id='installation')
    world.insert('access_key_grants', key_binding_id='internal-key-row', permission_id='settings:read', resource_id='installation')
    password_proof, key_proof = world.password_proof(), world.key_proof()
    world.insert('access_role_permissions', role_id='custom', permission_id='settings:write')
    world.update('access_roles', 'custom', version=2)
    world.update('access_state', 'state', policy_version=2)
    with world.store.transaction() as connection:
        assert world.proofs.revalidate_password_on(connection, password_proof, now=NOW).password_version == 1
        actor = world.proofs.authenticate_key_on(connection, key_proof, now=NOW)
        assert world.control.authorize_on(connection, actor, 'settings:read', ResourceRef('installation'), now=NOW).allowed
        assert not world.control.authorize_on(connection, actor, 'settings:write', ResourceRef('installation'), now=NOW).allowed
    members = world.tables['access_role_permissions']
    with world.engine.begin() as connection:
        connection.execute(members.delete().where(members.c.role_id == 'custom', members.c.permission_id == 'settings:read'))
    with world.store.transaction() as connection:
        actor = world.proofs.authenticate_key_on(connection, key_proof, now=NOW)
        assert not world.control.authorize_on(connection, actor, 'settings:read', ResourceRef('installation'), now=NOW).allowed
    world.update('access_users', 'alice', password_change_required=1)
    with world.store.transaction() as connection:
        actor = world.proofs.authenticate_key_on(connection, key_proof, now=NOW)
        assert world.control.authorize_on(connection, actor, 'settings:read', ResourceRef('installation'), now=NOW).reason == DecisionReason.RESET_REQUIRED


@pytest.mark.parametrize('kind', ['password', 'key'])
def test_proof_and_snapshot_provenance_cannot_be_forged_or_cross_instance(world, kind):
    other = module().CredentialProofs(world.store, world.codec)
    snapshot = world.password_snapshot() if kind == 'password' else world.key_snapshot()
    verifier = world.proofs.verify_password if kind == 'password' else world.proofs.verify_key
    proof = verifier(snapshot, 'password')
    other_verify = other.verify_password if kind == 'password' else other.verify_key
    complete = world.proofs.revalidate_password_on if kind == 'password' else world.proofs.authenticate_key_on
    other_complete = other.revalidate_password_on if kind == 'password' else other.authenticate_key_on
    with pytest.raises(AuthenticationError):
        other_verify(snapshot, 'password')
    for private in (snapshot, proof):
        assert repr(private) == type(private).__name__ + '()'
        assert not dataclasses.is_dataclass(private)
        with pytest.raises(TypeError):
            dataclasses.asdict(private)
        with pytest.raises(TypeError):
            json.dumps(private)
        with pytest.raises(TypeError):
            pickle.dumps(private)
        with pytest.raises(AttributeError):
            private._source = None
        with pytest.raises(TypeError):
            type(private)()
    forged = object.__new__(type(proof))
    # Even copying the private captured payload cannot mint verified provenance.
    object.__setattr__(forged, '_source', proof._source)
    with world.store.transaction() as connection:
        for invalid in (snapshot, forged, None, {'principal_id': 'alice'}):
            with pytest.raises(AuthenticationError):
                complete(connection, invalid, now=NOW)
        with pytest.raises(AuthenticationError):
            other_complete(connection, proof, now=NOW)


@pytest.mark.parametrize('kind', ['password', 'key'])
def test_completions_reject_non_utc_naive_clock(world, kind):
    proof = world.password_proof() if kind == 'password' else world.key_proof()
    complete = world.proofs.revalidate_password_on if kind == 'password' else world.proofs.authenticate_key_on
    with world.store.transaction() as connection:
        for now in (NOW.replace(tzinfo=timezone.utc), '2026-10-03', None):
            with pytest.raises(AuthenticationError):
                complete(connection, proof, now=now)


def test_kdf_unavailable_never_mints_proof(world, monkeypatch):
    import hashlib
    password, key = world.password_snapshot(), world.key_snapshot()
    def unavailable(*args, **kwargs):
        raise RuntimeError('secret backend details')
    monkeypatch.setattr(hashlib, 'pbkdf2_hmac', unavailable)
    for verifier, snapshot in ((world.proofs.verify_password, password), (world.proofs.verify_key, key)):
        with pytest.raises(CredentialServiceError) as error:
            verifier(snapshot, 'password')
        assert error.value.__context__ is None
