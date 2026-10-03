"""Internal session storage/lifecycle on isolated migrated databases; no routes."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import importlib
from threading import Event, local

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_mutations import MutationWorld, audit_failure
from api.tests.test_access_policy import NOW
from api.app.access.bootstrap import BootstrapCredentials
from api.app.access.credentials import CredentialCodec
from api.app.access.mutations import AccessMutations
from api.app.access.mutation_types import UserValues, StaleVersionError
from api.app.access.policy import AccessControl
from api.app.access.proofs import CredentialProofs
from api.app.access.store import AccessStore
from api.app.access.types import (
    AccessUnavailableError, AuthenticationError, InvalidTransactionError,
    KeyEvidence, KeySessionEvidence, PasswordSessionEvidence, ResourceRef,
    ScopedPermission,
)
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_secrets import load_installation_key

try:
    S = importlib.import_module('api.app.access.sessions')
except ModuleNotFoundError:
    S = None


class SessionWorld(MutationWorld):
    def __init__(self, engine, directory):
        super().__init__(engine)
        from api.app.access.session_codec import SessionCodec
        self.configuration = ConfigurationStore(engine, directory / 'session.key')
        self.configuration.initialize(
            ConfigurationValues.from_environment({'API_KEY': 'bootstrap-A'}), actor='test')
        self.store = self.configuration.access_store
        key = load_installation_key(self.configuration.key_path, allow_create=False)
        self.bootstrap_source = BootstrapCredentials(self.configuration, installation_key=key)
        self.control = AccessControl(self.store,
            current_bootstrap_fingerprint_on=self.bootstrap_source.current_fingerprint_on)
        self.mutations = AccessMutations(self.store, self.control, self.codec)
        self.proofs = CredentialProofs(self.store, self.codec)
        self.session_codec = SessionCodec(installation_key=base64.urlsafe_b64decode(key))
        self.sessions = S.AccessSessions(self.store, self.control, self.proofs,
            self.bootstrap_source, self.session_codec, self.codec)
        self.passwords = {}

    def password_proof(self, principal='alice', *, proofs=None):
        if principal not in self.passwords:
            password = 'current secret for ' + principal
            prepared = self.codec.prepare_chosen_password(password)
            self.update('access_users', principal, password_hash=prepared._password_hash_for_storage())
            self.passwords[principal] = password
        proofs = proofs or self.proofs
        with self.engine.begin() as c:
            snapshot = proofs.user_password_snapshot_on(c, principal)
        return proofs.verify_password(snapshot, self.passwords[principal])

    def key_proof(self, receipt, preparation, *, proofs=None):
        proofs = proofs or self.proofs
        with self.engine.begin() as c:
            snapshot = proofs.key_snapshot_on(c, receipt.public_key_id)
        token = preparation._token_for_committed_adapter()
        return proofs.verify_key(snapshot, token[len('fbk_live_' + receipt.public_key_id + '_'):])

    def login(self, principal='alice', *, now=NOW):
        prepared = self.session_codec.prepare()
        receipt = self.sessions.issue_password(self.password_proof(principal), prepared, now=now)
        token = prepared._token_for_committed_adapter()
        return receipt, token, self.sessions.authenticate(token, now=now)

    def service(self, store):
        from api.app.access.session_codec import SessionCodec
        # A second bootstrap service must be backed by this exact second store.
        configuration = ConfigurationStore(self.engine, self.configuration.key_path)
        configuration.access_store = store
        key = load_installation_key(configuration.key_path, allow_create=False)
        bootstrap = BootstrapCredentials(configuration, installation_key=key)
        control = AccessControl(store, current_bootstrap_fingerprint_on=bootstrap.current_fingerprint_on)
        codec = SessionCodec(installation_key=base64.urlsafe_b64decode(key))
        return S.AccessSessions(store, control, CredentialProofs(store, self.codec),
            bootstrap, codec, self.codec)


@pytest.fixture
def sworld(database, tmp_path):
    assert S is not None, 'Task 3B session service is not implemented'
    return SessionWorld(database, tmp_path)


def test_session_service_exists():
    assert S is not None, 'Task 3B session service is not implemented'


def test_password_issuance_is_persistent_nonsecret_and_policy_inert(sworld):
    w = sworld
    policy = w.policy()
    receipt, token, actor = w.login()
    row = w.row('access_sessions', receipt.session_id)
    assert receipt.expires_at == NOW + timedelta(hours=12)
    assert row['created_at'] == row['last_used_at'] == NOW
    assert row['source_kind'] == 'password' and row['password_version'] == 1
    assert row['source_key_id'] is None and row['bootstrap_fingerprint'] is None
    assert actor.replay_scope == 'principal:alice'
    assert type(actor.credential) is PasswordSessionEvidence
    assert w.policy() == policy
    assert not any(token in repr(value) for value in (receipt, row, w.rows('access_audit')))
    assert set(asdict(receipt)) == {'session_id', 'principal_id', 'source_kind', 'expires_at', 'password_change_required'}
    other = w.service(AccessStore(w.engine))
    assert other.authenticate(token, now=NOW) == actor


def test_key_session_keeps_ceiling_replay_and_expiry(sworld):
    w = sworld
    grants = (ScopedPermission('fax:read', ResourceRef('installation')),)
    key, preparation = w.key(ceiling=grants, expires=NOW + timedelta(hours=2))
    prepared = w.session_codec.prepare()
    receipt = w.sessions.issue_key(w.key_proof(key, preparation), prepared, now=NOW)
    actor = w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW)
    assert receipt.expires_at == NOW + timedelta(hours=2)
    assert actor.replay_scope == 'key:' + key.public_key_id
    assert type(actor.credential) is KeySessionEvidence
    fax = w.outbound('visible')
    assert w.control.authorize(actor, 'fax:read', fax, now=NOW).allowed
    assert not w.control.authorize(actor, 'fax:document', fax, now=NOW).allowed
    w.call('rotate_key', key.target, w.codec.prepare_key_rotation(key.public_key_id))
    with pytest.raises(AuthenticationError):
        w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW)


def test_bootstrap_session_tracks_configuration_rotation(sworld):
    w = sworld
    prepared = w.session_codec.prepare()
    receipt = w.sessions.issue_bootstrap('bootstrap-A', prepared, now=NOW)
    token = prepared._token_for_committed_adapter()
    actor = w.sessions.authenticate(token, now=NOW)
    assert receipt.source_kind == 'bootstrap' and actor.replay_scope == 'key:env'
    current = w.configuration.read()
    w.configuration.apply(current, current.desired.values.with_patch({'api_key': 'bootstrap-B'}),
        restart_required=False, actor='test')
    with pytest.raises(AuthenticationError):
        w.sessions.authenticate(token, now=NOW)


@pytest.mark.parametrize('boundary', ['idle', 'absolute', 'key'])
def test_expiry_equality_rejects_without_touch(sworld, boundary):
    w = sworld
    receipt, token, _ = w.login()
    if boundary == 'idle':
        now = NOW + timedelta(minutes=30)
    elif boundary == 'absolute':
        now = NOW + timedelta(hours=12)
        w.update('access_sessions', receipt.session_id, last_used_at=now - timedelta(minutes=1))
    else:
        key, preparation = w.key(expires=NOW + timedelta(minutes=10))
        prepared = w.session_codec.prepare()
        receipt = w.sessions.issue_key(w.key_proof(key, preparation), prepared, now=NOW)
        token = prepared._token_for_committed_adapter()
        now = NOW + timedelta(minutes=10)
    before = w.row('access_sessions', receipt.session_id)
    with pytest.raises(AuthenticationError):
        w.sessions.authenticate(token, now=now, touch=True)
    assert w.row('access_sessions', receipt.session_id) == before


def test_touch_keeps_absolute_expiry_and_live_roles(sworld):
    w = sworld
    receipt, token, actor = w.login()
    policy, audits = w.policy(), len(w.rows('access_audit'))
    w.sessions.authenticate(token, now=NOW + timedelta(minutes=29), touch=True)
    row = w.row('access_sessions', receipt.session_id)
    assert row['last_used_at'] == NOW + timedelta(minutes=29)
    assert row['expires_at'] == NOW + timedelta(hours=12)
    assert w.policy() == policy and len(w.rows('access_audit')) == audits
    assignment = next(r for r in w.rows('access_assignments') if r['principal_id'] == 'alice')
    with w.engine.begin() as c:
        c.execute(w.tables['access_assignments'].delete().where(w.tables['access_assignments'].c.id == assignment['id']))
    actor = w.sessions.authenticate(token, now=NOW + timedelta(minutes=30))
    assert not w.control.authorize(actor, 'users:manage', ResourceRef('installation'), now=NOW + timedelta(minutes=30)).allowed


@pytest.mark.parametrize('damage', ['csrf', 'source', 'principal', 'password', 'revoked', 'future', 'token'])
def test_corrupt_or_stale_rows_never_authenticate(sworld, damage):
    w = sworld
    receipt, token, _ = w.login()
    if damage == 'csrf': w.update('access_sessions', receipt.session_id, csrf_hash='a' * 64)
    elif damage == 'source': w.update('access_sessions', receipt.session_id, password_version=2)
    elif damage == 'principal': w.update('access_principals', 'alice', enabled=0)
    elif damage == 'password': w.update('access_users', 'alice', password_version=2)
    elif damage == 'revoked': w.update('access_sessions', receipt.session_id, revoked_at=NOW)
    elif damage == 'future': w.update('access_sessions', receipt.session_id, created_at=NOW + timedelta(minutes=1), last_used_at=NOW + timedelta(minutes=1))
    else: token = token + ' '
    with pytest.raises(AuthenticationError): w.sessions.authenticate(token, now=NOW, touch=True)


def test_reset_required_password_login_is_restricted_and_key_login_denied(sworld):
    w = sworld
    w.update('access_users', 'alice', password_change_required=1)
    receipt, token, actor = w.login()
    assert receipt.password_change_required
    assert not w.control.authorize(actor, 'users:manage', ResourceRef('installation'), now=NOW).allowed
    assert [i.session_id for i in w.sessions.list_sessions(actor, now=NOW).items] == [receipt.session_id]
    # Explicit management by the verified bootstrap source can issue an inert key.
    bootstrap = w.bootstrap_source
    with w.store.transaction() as c:
        admin = bootstrap.authenticate_on(c, 'bootstrap-A')
    from api.app.access.mutation_types import KeyValues
    preparation = w.codec.prepare_new_key()
    key = w.call('issue_key', KeyValues(w.version('access_principals', 'alice'), None, None, None, ()), preparation, actor=admin)
    proof = w.key_proof(key, preparation)
    with pytest.raises(S.SessionDeniedError) as caught:
        w.sessions.issue_key(proof, w.session_codec.prepare(), now=NOW)
    assert caught.value.reason == S.SessionReason.RESET_REQUIRED
    assert w.sessions.authenticate(token, now=NOW) == actor


def test_self_password_change_revokes_sessions_preserves_keys_and_rotates_password(sworld):
    w = sworld
    w.user('bob')
    first, token, actor = w.login('bob')
    second, other_token, _ = w.login('bob')
    key, key_preparation = w.key('bob')
    key_proof = w.key_proof(key, key_preparation)
    with w.store.transaction() as c:
        key_actor = w.proofs.authenticate_key_on(c, key_proof, now=NOW)
    proof = w.password_proof('bob')
    chosen = w.codec.prepare_chosen_password('a new exact password')
    prepared = w.session_codec.prepare()
    before_key, before_binding = w.row('api_keys', key.target.id), w.row('access_key_bindings', key.target.id)
    before_principal, policy = w.row('access_principals', 'bob'), w.policy()
    changed = w.sessions.change_password(actor, proof, chosen, prepared, now=NOW)
    assert changed.session_id not in (first.session_id, second.session_id)
    assert w.policy() == policy + 1
    p, u = w.row('access_principals', 'bob'), w.row('access_users', 'bob')
    assert p['security_version'] == before_principal['security_version']
    assert p['version'] == before_principal['version'] + 1 and u['password_version'] == 2
    assert u['password_change_required'] == 0
    assert w.row('api_keys', key.target.id) == before_key and w.row('access_key_bindings', key.target.id) == before_binding
    for old_token in (token, other_token):
        with pytest.raises(AuthenticationError): w.sessions.authenticate(old_token, now=NOW)
    assert w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW).replay_scope == 'principal:bob'
    with w.store.transaction() as c:
        assert w.control._current_source_on(c, key_actor, NOW).binding_id == key.target.id
    assert w.codec.verify('a new exact password', u['password_hash'])


@pytest.mark.parametrize('bad', ['wrong_user', 'temporary', 'key_actor', 'foreign_proof', 'foreign_prepared', 'stale'])
def test_password_change_rejects_wrong_or_stale_authority_before_write(sworld, bad):
    w = sworld
    w.user('bob')
    _, _, actor = w.login('bob')
    proof = w.password_proof('bob')
    chosen = w.codec.prepare_chosen_password('replacement password')
    prepared = w.session_codec.prepare()
    if bad == 'wrong_user': proof = w.password_proof('alice')
    elif bad == 'temporary': chosen = w.codec.prepare_temporary_password()
    elif bad == 'foreign_proof': proof = w.password_proof('bob', proofs=CredentialProofs(w.store, w.codec))
    elif bad == 'foreign_prepared':
        from api.app.access.session_codec import SessionCodec
        prepared = SessionCodec(installation_key=b'x' * 32).prepare()
    elif bad == 'stale': w.update('access_users', 'bob', password_version=2)
    else:
        key, material = w.key('bob')
        key_proof = w.key_proof(key, material)
        with w.store.transaction() as c: actor = w.proofs.authenticate_key_on(c, key_proof, now=NOW)
    before = w.row('access_users', 'bob'), w.row('access_principals', 'bob'), w.policy()
    with pytest.raises((S.SessionDeniedError, AuthenticationError)):
        w.sessions.change_password(actor, proof, chosen, prepared, now=NOW)
    assert (w.row('access_users', 'bob'), w.row('access_principals', 'bob'), w.policy()) == before


def test_reset_required_self_change_removes_only_lifecycle_restriction(sworld):
    w = sworld
    w.user('bob')
    w.update('access_users', 'bob', password_change_required=1)
    _, _, actor = w.login('bob')
    prepared = w.session_codec.prepare()
    receipt = w.sessions.change_password(actor, w.password_proof('bob'),
        w.codec.prepare_chosen_password('restricted replacement'), prepared, now=NOW)
    assert not receipt.password_change_required
    actor = w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW)
    assert not w.control.authorize(actor, 'users:manage', ResourceRef('installation'), now=NOW).allowed


def test_logout_revokes_actual_session_without_policy_bump(sworld):
    w = sworld
    receipt, token, actor = w.login()
    policy = w.policy()
    changed = w.sessions.logout(actor, now=NOW)
    assert changed.session_id == receipt.session_id and changed.changed
    assert changed.policy_version == w.policy() == policy
    with pytest.raises(AuthenticationError): w.sessions.authenticate(token, now=NOW)
    key, material = w.key()
    proof = w.key_proof(key, material)
    with w.store.transaction() as c: header_actor = w.proofs.authenticate_key_on(c, proof, now=NOW)
    with pytest.raises(S.SessionDeniedError): w.sessions.logout(header_actor, now=NOW)


def test_owned_visibility_and_descending_keyset_pagination(sworld):
    w = sworld
    w.user('bob')
    _, _, actor = w.login('bob')
    receipts = [w.login('bob', now=NOW + timedelta(seconds=i))[0] for i in range(1, 5)]
    w.login('alice')
    page = w.sessions.list_sessions(actor, limit=2, now=NOW + timedelta(seconds=5))
    assert [i.session_id for i in page.items] == [receipts[-1].session_id, receipts[-2].session_id]
    assert page.next_cursor.session_id == receipts[-2].session_id
    rest = w.sessions.list_sessions(actor, cursor=page.next_cursor, limit=4, now=NOW + timedelta(seconds=5))
    assert len(rest.items) == 4 and rest.next_cursor is None
    assert len({i.session_id for i in page.items + rest.items}) == 6
    assert all(set(asdict(i)) == {'session_id', 'source_kind', 'created_at', 'last_used_at', 'expires_at', 'revoked_at', 'current'} for i in rest.items)
    w.sessions.revoke_session(actor, receipts[0].session_id, expected_policy_version=w.policy(), now=NOW + timedelta(seconds=5))
    history = w.sessions.list_sessions(actor, now=NOW + timedelta(seconds=5))
    assert next(i for i in history.items if i.session_id == receipts[0].session_id).revoked_at is not None
    assert any(i.current for i in history.items)


@pytest.mark.parametrize('bad', [True, 0, 101, '2'])
def test_invalid_page_limit_has_fixed_input_error(sworld, bad):
    w = sworld
    _, _, actor = w.login()
    with pytest.raises(S.SessionDeniedError) as caught:
        w.sessions.list_sessions(actor, limit=bad, now=NOW)
    assert caught.value.reason == S.SessionReason.INVALID_INPUT


def test_key_self_service_never_enumerates_or_revokes_other_sessions(sworld):
    w = sworld
    password, _, _ = w.login()
    key, material = w.key(ceiling=())
    prepared = w.session_codec.prepare()
    key_session = w.sessions.issue_key(w.key_proof(key, material), prepared, now=NOW)
    actor = w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW)
    assert [i.session_id for i in w.sessions.list_sessions(actor, now=NOW).items] == [key_session.session_id]
    with pytest.raises(S.SessionDeniedError):
        w.sessions.revoke_session(actor, password.session_id, expected_policy_version=w.policy(), now=NOW)
    with pytest.raises(S.SessionDeniedError):
        w.sessions.list_sessions(actor, principal_id='alice', now=NOW)
    assert w.sessions.revoke_session(actor, key_session.session_id, expected_policy_version=w.policy(), now=NOW).changed


def test_administrative_revocation_requires_category_dominance_owner_and_revision(sworld):
    w = sworld
    w.user('bob')
    target, _, _ = w.login('bob')
    manager = w.restricted('manager', {'sessions:read', 'sessions:revoke'})
    assert len(w.sessions.list_sessions(manager, principal_id='bob', now=NOW).items) == 2
    w.assignment('bob', 'role_host_operator')
    with pytest.raises(S.SessionDeniedError) as caught:
        w.sessions.revoke_session(manager, target.session_id, expected_policy_version=w.policy(), now=NOW)
    assert caught.value.reason == S.SessionReason.FORBIDDEN
    all_custom = w.restricted('custom', frozenset())
    from api.app.access.catalog import PERMISSIONS
    w.role('custom-full', PERMISSIONS); w.assignment('custom', 'custom-full')
    w.group('disabled-owner', 0); w.assignment(None, 'role_owner', group='disabled-owner')
    w.insert('access_memberships', group_id='disabled-owner', principal_id='bob')
    with pytest.raises(S.SessionDeniedError):
        w.sessions.revoke_session(all_custom, target.session_id, expected_policy_version=w.policy(), now=NOW)
    with pytest.raises(StaleVersionError):
        w.sessions.revoke_session(w.actor, target.session_id, expected_policy_version=w.policy() + 1, now=NOW)
    assert w.sessions.revoke_session(w.actor, target.session_id, expected_policy_version=w.policy(), now=NOW).changed
    assert not w.sessions.revoke_session(w.actor, target.session_id, expected_policy_version=w.policy(), now=NOW).changed


def test_expected_denial_audit_is_safe_and_durable(sworld):
    w = sworld
    before = len(w.rows('access_audit'))
    with pytest.raises(AuthenticationError):
        w.sessions.issue_bootstrap('private-wrong-marker', w.session_codec.prepare(), now=NOW)
    new = w.rows('access_audit')[before:]
    assert len(new) == 1 and new[0]['outcome'] == 'denied'
    assert all(new[0][f] is None for f in ('actor_principal_id', 'actor_key_binding_id', 'actor_session_id'))
    assert 'private-wrong-marker' not in repr(new)
    assert new[0]['policy_version_before'] == new[0]['policy_version_after']


@pytest.mark.parametrize('operation', ['issue', 'change', 'denial'])
def test_audit_failure_rolls_back_session_password_and_versions(sworld, operation):
    w = sworld
    _, _, actor = w.login()
    proof, prepared = w.password_proof(), w.session_codec.prepare()
    chosen = w.codec.prepare_chosen_password('after audit failure')
    before = w.row('access_users', 'alice'), w.row('access_principals', 'alice'), w.rows('access_sessions'), w.policy()
    audit_failure(w)
    with pytest.raises(AccessUnavailableError) as caught:
        if operation == 'issue': w.sessions.issue_password(proof, prepared, now=NOW)
        elif operation == 'change': w.sessions.change_password(actor, proof, chosen, prepared, now=NOW)
        else: w.sessions.issue_bootstrap('private-marker', prepared, now=NOW)
    assert caught.value.__context__ is None
    assert (w.row('access_users', 'alice'), w.row('access_principals', 'alice'), w.rows('access_sessions'), w.policy()) == before


def test_unknown_commit_returns_no_receipt_and_never_discloses_or_retries(sworld, monkeypatch):
    w = sworld
    proof, prepared = w.password_proof(), w.session_codec.prepare()
    storage = w.session_codec.storage(prepared)
    calls = []
    real = w.store.transaction
    @contextmanager
    def lost_ack():
        calls.append('transaction')
        with real() as c: yield c
        raise sa.exc.OperationalError('private-marker', {}, Exception('private-marker'))
    monkeypatch.setattr(w.store, 'transaction', lost_ack)
    monkeypatch.setattr(type(prepared), '_token_for_committed_adapter', lambda self: pytest.fail('disclosed uncertain commit'))
    with pytest.raises(AccessUnavailableError) as caught: w.sessions.issue_password(proof, prepared, now=NOW)
    assert calls == ['transaction'] and caught.value.__context__ is None
    assert w.row('access_sessions', storage.id)['source_kind'] == 'password'
    assert not hasattr(caught.value, 'receipt')


def test_existing_exact_lock_and_service_store_identity_are_required(sworld):
    w = sworld
    with pytest.raises(InvalidTransactionError):
        S.AccessSessions(AccessStore(w.engine), w.control, w.proofs, w.bootstrap_source, w.session_codec, w.codec)
    with w.engine.begin() as c:
        with pytest.raises(InvalidTransactionError):
            w.sessions.issue_password_on(c, object(), object(), now=NOW)
        with pytest.raises(InvalidTransactionError): w.sessions.authenticate_on(c, 'invalid', now=NOW)
    prepared, proof = w.session_codec.prepare(), w.password_proof()
    with w.store.transaction() as c:
        outcome = w.sessions.issue_password_on(c, proof, prepared, now=NOW)
        assert outcome.receipt is not None
        c.rollback()
    assert not [r for r in w.rows('access_sessions') if r['id'] == outcome.receipt.session_id]


@pytest.mark.parametrize('now', [NOW.replace(tzinfo=timezone.utc), datetime.max])
def test_invalid_or_overflowing_clock_denies_before_issuance(sworld, now):
    w = sworld
    before = len(w.rows('access_sessions'))
    with pytest.raises(S.SessionDeniedError):
        w.sessions.issue_password(w.password_proof(), w.session_codec.prepare(), now=now)
    assert len(w.rows('access_sessions')) == before


@pytest.mark.parametrize('change', ['reset', 'disable', 'change'])
def test_two_store_source_change_invalidates_waiting_password_change(sworld, change):
    w = sworld
    w.user('bob')
    _, _, actor = w.login('bob')
    proof = w.password_proof('bob')
    chosen = w.codec.prepare_chosen_password('contender password')
    holder_choice, holder_prepared = w.codec.prepare_chosen_password('holder password'), w.session_codec.prepare()
    reset = w.codec.prepare_temporary_password()
    target = w.version('access_principals', 'bob')
    other_store = AccessStore(w.engine)
    other_store.LOCK_TIMEOUT_MS = 3000
    other = w.service(other_store)
    prepared = other.codec.prepare()
    other_proof = w.password_proof('bob', proofs=other.proofs)
    def first(c):
        if change == 'reset':
            return w.mutations.reset_password_on(c, w.actor, target,
                reset, expected_policy_version=w.store.require_lock_on(c), now=NOW)
        if change == 'disable':
            return w.mutations.update_user_on(c, w.actor, target,
                UserValues('bob', 'bob', False), expected_policy_version=w.store.require_lock_on(c), now=NOW)
        return w.sessions.change_password_on(c, actor, proof, holder_choice, holder_prepared, now=NOW)
    held, attempted, release, done = Event(), Event(), Event(), Event()
    thread = local()
    def listener(connection, cursor, statement, parameters, context, executemany):
        upper = statement.upper()
        if getattr(thread, 'contender', False) and (upper.startswith('BEGIN IMMEDIATE')
                or ('ACCESS_STATE' in upper and 'FOR UPDATE' in upper)):
            attempted.set()
    def holder():
        with w.store.transaction() as c:
            outcome = first(c)
            held.set()
            assert release.wait(3)
        return outcome
    def contender():
        assert held.wait(3)
        thread.contender = True
        try:
            with other_store.transaction() as c:
                return other.change_password_on(c, actor, other_proof, chosen, prepared, now=NOW)
        finally:
            done.set()
    sa.event.listen(w.engine, 'before_cursor_execute', listener)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(holder)
            assert held.wait(3)
            b = executor.submit(contender)
            try:
                assert attempted.wait(3)
                assert not done.is_set()
            finally:
                release.set()
            allowed, denied = a.result(timeout=5), b.result(timeout=5)
    finally:
        sa.event.remove(w.engine, 'before_cursor_execute', listener)
    assert allowed.receipt is not None and denied.reason == S.SessionReason.UNAUTHENTICATED
    assert w.row('access_users', 'bob')['password_hash'] != chosen._password_hash_for_storage()


@pytest.mark.parametrize('damage', ['proof', 'prepared', 'tampered', 'disabled', 'rotated'])
def test_issuance_rejects_foreign_or_stale_material_without_insertion(sworld, damage):
    w = sworld
    proof, prepared = w.password_proof(), w.session_codec.prepare()
    if damage == 'proof': proof = w.password_proof(proofs=CredentialProofs(w.store, w.codec))
    elif damage == 'prepared':
        from api.app.access.session_codec import SessionCodec
        prepared = SessionCodec(installation_key=b'y' * 32).prepare()
    elif damage == 'tampered': proof = object()
    elif damage == 'disabled': w.update('access_principals', 'alice', enabled=0)
    else: w.update('access_users', 'alice', password_version=2)
    before = len(w.rows('access_sessions'))
    with pytest.raises(AuthenticationError): w.sessions.issue_password(proof, prepared, now=NOW)
    assert len(w.rows('access_sessions')) == before


@pytest.mark.parametrize('source', ['key', 'bootstrap'])
def test_compatibility_session_self_service_and_admin_are_distinct(sworld, source):
    w = sworld
    password, _, _ = w.login()
    prepared = w.session_codec.prepare()
    if source == 'key':
        from api.app.access.catalog import PERMISSIONS
        key, material = w.key(ceiling=tuple(ScopedPermission(p, ResourceRef('installation')) for p in sorted(PERMISSIONS)))
        receipt = w.sessions.issue_key(w.key_proof(key, material), prepared, now=NOW)
    else:
        receipt = w.sessions.issue_bootstrap('bootstrap-A', prepared, now=NOW)
    actor = w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW)
    assert [s.session_id for s in w.sessions.list_sessions(actor, now=NOW).items] == [receipt.session_id]
    assert password.session_id in [s.session_id for s in w.sessions.list_sessions(actor, principal_id='alice', now=NOW).items]
    assert w.sessions.revoke_session(actor, password.session_id, expected_policy_version=w.policy(), now=NOW).changed
    assert w.sessions.logout(actor, now=NOW).changed


def test_restricted_self_revoke_requires_revision_and_cannot_revoke_other_sessions(sworld):
    w = sworld
    w.user('bob')
    first, _, _ = w.login('bob')
    second, _, actor = w.login('bob')
    w.update('access_users', 'bob', password_change_required=1)
    with pytest.raises(StaleVersionError):
        w.sessions.revoke_session(actor, second.session_id, expected_policy_version=w.policy() + 1, now=NOW)
    with pytest.raises(S.SessionDeniedError) as caught:
        w.sessions.revoke_session(actor, first.session_id, expected_policy_version=w.policy(), now=NOW)
    assert caught.value.reason == S.SessionReason.RESET_REQUIRED
    with pytest.raises(S.SessionDeniedError) as caught:
        w.sessions.list_sessions(actor, principal_id='bob', now=NOW)
    assert caught.value.reason == S.SessionReason.RESET_REQUIRED
    assert w.sessions.revoke_session(actor, second.session_id, expected_policy_version=w.policy(), now=NOW).changed


def test_pagination_id_ties_visibility_expired_history_and_touch_order(sworld):
    w = sworld
    w.user('bob')
    _, _, actor = w.login('bob')
    receipts = [w.login('bob')[0] for _ in range(4)]
    ordered = sorted([r.session_id for r in receipts] + [actor.credential.session_id], reverse=True)
    w.update('access_sessions', receipts[0].session_id, expires_at=NOW)
    page = w.sessions.list_sessions(actor, limit=2, now=NOW)
    assert [s.session_id for s in page.items] == ordered[:2]
    # Foreign cursor IDs and times remain filters, never authorization.
    foreign = S.SessionCursor(NOW + timedelta(days=1), 'zz')
    full = w.sessions.list_sessions(actor, cursor=foreign, limit=100, now=NOW)
    assert len(full.items) == 6 and all(s.session_id != 'session-alice' for s in full.items)
    with w.store.transaction() as c:
        c.execute(w.tables['access_sessions'].update().where(
            w.tables['access_sessions'].c.id == receipts[-1].session_id).values(last_used_at=NOW + timedelta(seconds=1)))
    again = w.sessions.list_sessions(actor, limit=2, now=NOW + timedelta(seconds=1))
    assert [s.session_id for s in again.items] == ordered[:2]
    rest = w.sessions.list_sessions(actor, cursor=page.next_cursor, limit=100, now=NOW + timedelta(seconds=1))
    assert [s.session_id for s in rest.items[:3]] == ordered[2:]
    assert next(s for s in full.items if s.session_id == receipts[0].session_id).expires_at == NOW


@pytest.mark.parametrize('cursor', [object(), 'unsafe'])
def test_invalid_cursor_is_fixed_input_failure(sworld, cursor):
    w = sworld
    _, _, actor = w.login()
    with pytest.raises(S.SessionDeniedError) as caught: w.sessions.list_sessions(actor, cursor=cursor, now=NOW)
    assert caught.value.reason == S.SessionReason.INVALID_INPUT
    with pytest.raises(S.SessionDeniedError): S.SessionCursor(NOW.replace(tzinfo=timezone.utc), 'id')


def test_authenticate_exact_touch_validation_and_mutator_lock_contract(sworld):
    w = sworld
    _, token, actor = w.login()
    with pytest.raises(AuthenticationError): w.sessions.authenticate(token, now=NOW, touch=1)
    with w.engine.begin() as c:
        calls = [
            lambda: w.sessions.issue_key_on(c, object(), object(), now=NOW),
            lambda: w.sessions.issue_bootstrap_on(c, 'secret', object(), now=NOW),
            lambda: w.sessions.logout_on(c, actor, now=NOW),
            lambda: w.sessions.change_password_on(c, actor, object(), object(), object(), now=NOW),
            lambda: w.sessions.revoke_session_on(c, actor, 'id', expected_policy_version=1, now=NOW),
            lambda: w.sessions.list_sessions_on(c, actor, now=NOW),
        ]
        for call in calls:
            with pytest.raises(InvalidTransactionError): call()


def test_denial_after_aggregate_business_write_requires_rollback(sworld):
    w = sworld
    before = len(w.rows('access_sessions')), len(w.rows('access_audit'))
    prepared, proof = w.session_codec.prepare(), w.password_proof()
    denied_prepared = w.session_codec.prepare()
    with w.store.transaction() as c:
        allowed = w.sessions.issue_password_on(c, proof, prepared, now=NOW)
        denied = w.sessions.issue_bootstrap_on(c, 'incorrect', denied_prepared, now=NOW)
        assert allowed.receipt is not None and denied.reason == S.SessionReason.UNAUTHENTICATED
        c.rollback()
    assert (len(w.rows('access_sessions')), len(w.rows('access_audit'))) == before


def test_session_preparation_disclosure_occurs_only_in_owning_confirmed_adapter(sworld, monkeypatch):
    w = sworld
    prepared, proof = w.session_codec.prepare(), w.password_proof()
    storage = w.session_codec.storage(prepared)
    original = type(prepared)._token_for_committed_adapter
    def disclose(material):
        assert w.row('access_sessions', storage.id)['revoked_at'] is None
        return original(material)
    monkeypatch.setattr(type(prepared), '_token_for_committed_adapter', disclose)
    receipt = w.sessions.issue_password(proof, prepared, now=NOW)
    # Mutation did not need secret access; the adapter owns this one call.
    assert w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW).credential.session_id == receipt.session_id


def test_preparation_uniqueness_denial_does_not_rewrite_existing_session(sworld):
    w = sworld
    prepared, proof = w.session_codec.prepare(), w.password_proof()
    receipt = w.sessions.issue_password(proof, prepared, now=NOW)
    before = w.row('access_sessions', receipt.session_id)
    with pytest.raises(S.SessionDeniedError) as caught: w.sessions.issue_password(proof, prepared, now=NOW)
    assert caught.value.reason == S.SessionReason.INVALID_INPUT
    assert w.row('access_sessions', receipt.session_id) == before


def test_unexpected_storage_boundary_failure_is_fixed_without_context(sworld, monkeypatch):
    w = sworld
    prepared, proof = w.session_codec.prepare(), w.password_proof()
    real = w.store.transaction
    @contextmanager
    def broken_commit():
        with real() as c: yield c
        raise RuntimeError('private-runtime-marker')
    monkeypatch.setattr(w.store, 'transaction', broken_commit)
    with pytest.raises(AccessUnavailableError) as caught: w.sessions.issue_password(proof, prepared, now=NOW)
    assert str(caught.value) == 'access_unavailable' and caught.value.__context__ is None
