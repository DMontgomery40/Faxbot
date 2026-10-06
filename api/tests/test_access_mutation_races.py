"""Actual two-store writer races and mutation-driven stale credential proofs."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, local
import hashlib

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_mutations import MutationWorld, M, T
from api.tests.test_access_policy import NOW
from api.app.access.policy import AccessControl
from api.app.access.proofs import CredentialProofs
from api.app.access.store import AccessStore
from api.app.access.types import StaleCredentialError


@pytest.fixture
def mworld(database):
    assert M is not None, 'Task 2B2 mutation module is not implemented'
    return MutationWorld(database)


def race(w, first, second):
    """Hold a real first-store mutation uncommitted while a second attempts lock."""
    store = AccessStore(w.engine)
    store.LOCK_TIMEOUT_MS = 3000
    other = M.AccessMutations(store, AccessControl(store,
        current_bootstrap_fingerprint_on=lambda c: 'f' * 64), w.codec)
    held, attempted, release, done = Event(), Event(), Event(), Event()
    thread = local()
    def listener(connection, cursor, statement, parameters, context, executemany):
        upper = statement.upper()
        if getattr(thread, 'contender', False) and (
            upper.startswith('BEGIN IMMEDIATE') or ('ACCESS_STATE' in upper and 'FOR UPDATE' in upper)):
            attempted.set()
    def holder():
        with w.store.transaction() as connection:
            result = first(connection)
            held.set()
            assert release.wait(3), 'race coordinator failed to release first transaction'
        return result
    def contender():
        assert held.wait(3)
        thread.contender = True
        try:
            with store.transaction() as connection:
                return second(other, connection)
        finally:
            done.set()
    sa.event.listen(w.engine, 'before_cursor_execute', listener)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(holder)
            assert held.wait(3)
            b = executor.submit(contender)
            try:
                assert attempted.wait(3), 'second store never attempted the database lock'
                assert not done.is_set(), 'second store bypassed the held access lock'
            finally:
                release.set()
            return a.result(timeout=5), b.result(timeout=5)
    finally:
        sa.event.remove(w.engine, 'before_cursor_execute', listener)


@pytest.mark.parametrize('entity', ['group', 'role'])
def test_two_store_stale_editor_cannot_overwrite_committed_change(mworld, entity):
    w = mworld
    if entity == 'group':
        receipt = w.call('create_group', T.GroupValues('Original', '', True))
        method = 'update_group_on'
        one, two = T.GroupValues('First', '', True), T.GroupValues('Second', '', True)
        table = 'access_groups'
    else:
        receipt = w.call('create_custom_role', T.CustomRoleValues('Original', '', True, frozenset()))
        method = 'update_custom_role_on'
        one = T.CustomRoleValues('First', '', True, frozenset({'host:terminal'}))
        two = T.CustomRoleValues('Second', '', True, frozenset({'host:restart'}))
        table = 'access_roles'
    expected = w.policy()
    def first(c):
        return getattr(w.mutations, method)(c, w.actor, receipt.target, one,
            expected_policy_version=expected, now=NOW)
    def second(other, c):
        return getattr(other, method)(c, w.actor, receipt.target, two,
            expected_policy_version=expected, now=NOW)
    allowed, denied = race(w, first, second)
    assert allowed.receipt.changed and denied.reason == T.MutationReason.STALE_VERSION
    assert w.row(table, receipt.target.id)['name'] == 'First'
    assert w.row(table, receipt.target.id)['version'] == 2
    assert w.policy() == expected + 1
    if entity == 'role':
        assert [r['permission_id'] for r in w.rows('access_role_permissions') if r['role_id'] == receipt.target.id] == ['host:terminal']


@pytest.mark.parametrize('destruction', ['disable', 'assignment'])
def test_two_store_last_owner_check_uses_after_first_commit(mworld, destruction):
    w = mworld
    actor = w.bootstrap()
    w.user('bob')
    bob_assignment = w.assignment('bob', 'role_owner')
    alice_assignment = next(r['id'] for r in w.rows('access_assignments') if r['principal_id'] == 'alice')
    if destruction == 'disable':
        def mutate(m, c, identity, expected):
            return m.update_user_on(c, actor, T.VersionedEntity(identity, 1), T.UserValues(identity, identity, False), expected_policy_version=expected, now=NOW)
        first_id, second_id = 'alice', 'bob'
    else:
        def mutate(m, c, identity, expected):
            return m.remove_assignment_on(c, actor, T.VersionedEntity(identity, 1), expected_policy_version=expected, now=NOW)
        first_id, second_id = alice_assignment, bob_assignment
    def first(c):
        return mutate(w.mutations, c, first_id, 1)
    def second(other, c):
        # The contender intentionally uses the freshly locked current revision,
        # so LAST_OWNER (not stale-editor rejection) proves the serialized check.
        return mutate(other, c, second_id, other.store.require_lock_on(c))
    allowed, denied = race(w, first, second)
    assert allowed.receipt.changed and denied.reason == T.MutationReason.LAST_OWNER
    assert w.policy() == 2
    if destruction == 'disable':
        assert w.row('access_principals', 'alice')['enabled'] == 0
        assert w.row('access_principals', 'bob')['enabled'] == 1
    else:
        assert not [r for r in w.rows('access_assignments') if r['id'] == alice_assignment]
        assert w.row('access_assignments', bob_assignment)['role_id'] == 'role_owner'


def verification_in_flight(monkeypatch, verify, mutate):
    """Pause a real KDF call while the mutation commits with no KDF/DB nesting."""
    entered, release = Event(), Event()
    actual_scrypt = hashlib.scrypt
    def held_scrypt(*args, **kwargs):
        entered.set()
        assert release.wait(3), 'verification coordinator failed to release KDF'
        return actual_scrypt(*args, **kwargs)
    monkeypatch.setattr(hashlib, 'scrypt', held_scrypt)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(verify)
        assert entered.wait(3), 'real verification never entered its KDF'
        try:
            mutate()
        finally:
            release.set()
        return result.result(timeout=5)


def test_rotation_while_verifying_cannot_launder_old_key_proof(mworld, monkeypatch):
    w = mworld
    receipt, prepared = w.key()
    rotation = w.codec.prepare_key_rotation(receipt.public_key_id)
    proofs = CredentialProofs(w.store, w.codec)
    with w.engine.begin() as c:
        snapshot = proofs.key_snapshot_on(c, receipt.public_key_id)
    token = prepared._token_for_committed_adapter()
    # Secrets can contain underscores; split the exact known token prefix.
    secret = token[len('fbk_live_' + receipt.public_key_id + '_'):]
    proof = verification_in_flight(monkeypatch,
        lambda: proofs.verify_key(snapshot, secret),
        lambda: w.call('rotate_key', receipt.target, rotation))
    assert proof is not None
    assert w.row('access_key_bindings', receipt.target.id)['version'] == 2
    with w.store.transaction() as c, pytest.raises(StaleCredentialError):
        proofs.authenticate_key_on(c, proof, now=NOW)


def test_reset_while_verifying_cannot_launder_old_password_proof(mworld, monkeypatch):
    w = mworld
    w.user('bob')
    chosen = w.codec.prepare_chosen_password('exact old password')
    temporary = w.codec.prepare_temporary_password()
    w.update('access_users', 'bob', password_hash=chosen._password_hash_for_storage())
    proofs = CredentialProofs(w.store, w.codec)
    with w.engine.begin() as c:
        snapshot = proofs.user_password_snapshot_on(c, 'bob')
    proof = verification_in_flight(monkeypatch,
        lambda: proofs.verify_password(snapshot, 'exact old password'),
        lambda: w.call('reset_password', w.version('access_principals', 'bob'), temporary))
    assert proof is not None
    with w.store.transaction() as c, pytest.raises(StaleCredentialError):
        proofs.revalidate_password_on(c, proof, now=NOW)
