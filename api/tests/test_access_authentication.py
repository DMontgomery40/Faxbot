"""Internal committed-admission/proof composition; no HTTP acceptance."""
import base64
import importlib
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.tests.test_access_sessions import SessionWorld, NOW
from api.app.access.admission import AuthenticationAdmission
from api.app.access.types import AuthenticationError, AccessUnavailableError
from api.app.access.types import ScopedPermission, ResourceRef
from api.app.config_secrets import load_installation_key

try:
    A = importlib.import_module('api.app.access.authentication')
except ModuleNotFoundError:
    A = None


@pytest.fixture
def world(database, tmp_path):
    assert A is not None, 'Authentication composition missing'
    w = SessionWorld(database, tmp_path)
    w.password_proof()
    key = base64.urlsafe_b64decode(load_installation_key(w.configuration.key_path, allow_create=False))
    w.admission = AuthenticationAdmission(w.store, installation_key=key, clock=lambda: NOW)
    w.authentication = A.AuthenticationService(w.sessions, w.admission, clock=lambda: NOW)
    w.initial_sessions = w.rows('access_sessions')
    return w


def test_module_exists():
    assert A is not None, 'Authentication composition missing'


def test_committed_reservation_and_closed_snapshot_precede_password_kdf(world, monkeypatch):
    w = world
    active = set()
    sa.event.listen(w.engine, 'begin', lambda c: active.add(id(c)))
    sa.event.listen(w.engine, 'commit', lambda c: active.discard(id(c)))
    sa.event.listen(w.engine, 'rollback', lambda c: active.discard(id(c)))
    original = w.codec.verify
    calls = []
    def verify(secret, hashed):
        assert not active
        with w.engine.connect() as c:
            assert c.scalar(sa.text('SELECT count(*) FROM access_auth_buckets')) == 2
            assert c.scalar(sa.text("SELECT count(*) FROM access_audit WHERE operation='authentication.admit'")) == 1
        calls.append(True)
        return original(secret, hashed)
    monkeypatch.setattr(w.codec, 'verify', verify)
    receipt, prepared = w.authentication.password_login(' ALICE ', w.passwords['alice'])
    assert calls == [True]
    assert receipt.principal_id == 'alice'
    actor = w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW)
    assert actor.principal_id == 'alice'


@pytest.mark.parametrize('failure', ['unknown', 'disabled', 'unsupported_hash', 'wrong_password'])
def test_rejected_password_uses_one_bounded_verification_and_no_session(world, monkeypatch, failure):
    w = world
    login, password = 'alice', w.passwords['alice']
    if failure == 'unknown':
        login = 'unknown-private-login'
    elif failure == 'disabled':
        w.update('access_principals', 'alice', enabled=0)
    elif failure == 'unsupported_hash':
        w.update('access_users', 'alice', password_hash='unsupported-private-hash')
    else:
        password = 'wrong-private-password'
    original, calls = w.codec.verify, []
    def verify(secret, hashed):
        calls.append(True)
        return original(secret, hashed)
    monkeypatch.setattr(w.codec, 'verify', verify)
    with pytest.raises(AuthenticationError) as error:
        w.authentication.password_login(login, password)
    assert error.value.args == ('unauthenticated',)
    assert calls == [True]
    assert w.rows('access_sessions') == w.initial_sessions
    audits = w.rows('access_audit')
    denied = [a for a in audits if a['operation'] == 'issue_password_session']
    assert len(denied) == 1 and denied[0]['outcome'] == 'denied'
    assert denied[0]['actor_principal_id'] is None
    assert all(marker not in repr(audits) for marker in ('unknown-private-login', 'unsupported-private-hash', 'wrong-private-password'))


def test_throttled_password_never_starts_another_kdf(world, monkeypatch):
    w = world
    for _ in range(5):
        assert w.admission.reserve('password_login', 'alice').allowed
    monkeypatch.setattr(w.codec, 'verify', lambda *_: pytest.fail('KDF after admission denied'))
    with pytest.raises(A.AuthenticationThrottledError) as error:
        w.authentication.password_login('alice', w.passwords['alice'])
    assert error.value.retry_after_seconds == 12
    assert w.rows('access_sessions') == w.initial_sessions


def test_unknown_reservation_commit_does_not_verify_or_disclose(world, monkeypatch):
    w = world
    original = AuthenticationAdmission.reserve
    def lost(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError('private commit acknowledgement')
    monkeypatch.setattr(AuthenticationAdmission, 'reserve', lost)
    monkeypatch.setattr(w.codec, 'verify', lambda *_: pytest.fail('KDF after uncertain admission'))
    with pytest.raises(AccessUnavailableError) as error:
        w.authentication.password_login('alice', w.passwords['alice'])
    assert error.value.__context__ is None
    assert error.value.__cause__ is None
    assert w.rows('access_sessions') == w.initial_sessions


def test_kdf_backend_failure_is_unavailable_not_bad_password(world, monkeypatch):
    def fail(*_):
        raise RuntimeError('private verification backend data')
    monkeypatch.setattr(world.codec, 'verify', fail)
    with pytest.raises(AccessUnavailableError) as error:
        world.authentication.password_login('alice', world.passwords['alice'])
    assert error.value.args == ('access_unavailable',)
    assert error.value.__context__ is None
    assert world.rows('access_sessions') == world.initial_sessions


def test_key_header_and_compatibility_session_preserve_ceiling_and_namespace(world):
    w = world
    issued, material = w.key(ceiling=(ScopedPermission('fax:read', ResourceRef('installation')),))
    token = material._token_for_committed_adapter()
    actor = w.authentication.header_key(token)
    assert actor.replay_scope == 'key:' + issued.public_key_id
    receipt, prepared = w.authentication.key_login(token)
    session_actor = w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW)
    assert session_actor.replay_scope == actor.replay_scope
    assert receipt.source_kind == 'key'
    with w.store.transaction() as c:
        assert not w.control.authorize_on(c, session_actor, 'settings:read', ResourceRef('installation'), now=NOW).allowed


def test_bootstrap_authentication_never_uses_password_kdf(world, monkeypatch):
    w = world
    monkeypatch.setattr(w.codec, 'verify', lambda *_: pytest.fail('Bootstrap must not use KDF'))
    assert w.authentication.header_key('bootstrap-A').replay_scope == 'key:env'
    receipt, prepared = w.authentication.key_login('bootstrap-A')
    assert receipt.source_kind == 'bootstrap'
    assert w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW).replay_scope == 'key:env'


@pytest.mark.parametrize('method', ['header_key', 'key_login'])
def test_unknown_db_key_uses_dummy_without_upgrading_to_bootstrap(world, monkeypatch, method):
    w = world
    calls, original = [], w.codec.verify
    def verify(secret, hashed):
        calls.append(True)
        return original(secret, hashed)
    monkeypatch.setattr(w.codec, 'verify', verify)
    with pytest.raises(AuthenticationError):
        getattr(w.authentication, method)('fbk_live_012345abcdef_unknown-secret')
    assert calls == [True]
    assert w.rows('access_sessions') == w.initial_sessions


def test_self_password_replacement_preserves_delegated_keys_and_replaces_session(world):
    w = world
    _, token, actor = w.login()
    issued, material = w.key()
    key_actor = w.authentication.header_key(material._token_for_committed_adapter())
    before_keys = w.rows('api_keys')
    receipt, prepared = w.authentication.change_password(actor, w.passwords['alice'], 'replacement chosen password')
    assert receipt.principal_id == 'alice'
    with pytest.raises(AuthenticationError):
        w.sessions.authenticate(token, now=NOW)
    assert w.sessions.authenticate(prepared._token_for_committed_adapter(), now=NOW).principal_id == 'alice'
    assert w.rows('api_keys') == before_keys
    with w.store.transaction() as c:
        w.control._current_source_on(c, key_actor, NOW)


def test_wrong_current_password_does_not_hash_replacement_or_write_session(world, monkeypatch):
    w = world
    _, _, actor = w.login()
    before = w.rows('access_sessions')
    monkeypatch.setattr(w.codec, 'prepare_chosen_password', lambda *_: pytest.fail('Hashing replacement before current-password proof'))
    with pytest.raises(AuthenticationError):
        w.authentication.change_password(actor, 'wrong-password', 'replacement chosen password')
    assert w.rows('access_sessions') == before


@pytest.mark.parametrize('committed', [False, True])
def test_uncertain_session_commit_returns_no_preparation_or_receipt(world, monkeypatch, committed):
    w = world
    original, calls = w.store.transaction, []
    @contextmanager
    def transaction():
        calls.append(True)
        final = len(calls) == 2  # reservation first, final issuance second
        with original() as c:
            yield c
            if final and not committed:
                raise RuntimeError('private completion before commit')
        if final:
            raise RuntimeError('private completion acknowledgement')
    monkeypatch.setattr(w.store, 'transaction', transaction)
    with pytest.raises(AccessUnavailableError) as error:
        w.authentication.password_login('alice', w.passwords['alice'])
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert len(w.rows('access_sessions')) == len(w.initial_sessions) + int(committed)


def test_source_rotation_between_verification_and_completion_never_issues(world, monkeypatch):
    w = world
    original = w.codec.verify
    def verify(secret, hashed):
        result = original(secret, hashed)
        w.update('access_principals', 'alice', security_version=2)
        return result
    monkeypatch.setattr(w.codec, 'verify', verify)
    with pytest.raises(AuthenticationError):
        w.authentication.password_login('alice', w.passwords['alice'])
    assert w.rows('access_sessions') == w.initial_sessions


def test_db_shaped_canonical_bootstrap_keeps_bootstrap_precedence(world, monkeypatch):
    w = world
    token = 'fbk_live_012345abcdef_canonical-bootstrap-secret'
    snapshot = w.configuration.read()
    w.configuration.apply(snapshot, snapshot.desired.values.with_patch({'api_key': token}),
        actor='test', restart_required=False)
    monkeypatch.setattr(w.codec, 'verify', lambda *_: pytest.fail('DB-shaped bootstrap used KDF'))
    assert w.authentication.header_key(token).replay_scope == 'key:env'
    receipt, _ = w.authentication.key_login(token)
    assert receipt.source_kind == 'bootstrap'


def test_known_dummy_plaintext_still_cannot_mint_a_password_proof(world):
    with pytest.raises(AuthenticationError):
        world.authentication.password_login('no-such-user', 'Faxbot internal dummy verification only')
    assert world.rows('access_sessions') == world.initial_sessions


def test_completion_clock_is_sampled_under_the_exact_access_lock(world, monkeypatch):
    w = world
    active = []
    original = w.store.transaction
    @contextmanager
    def transaction():
        with original() as c:
            active.append(c)
            try:
                yield c
            finally:
                active.pop()
    monkeypatch.setattr(w.store, 'transaction', transaction)
    def now():
        assert active and w.store.require_lock_on(active[-1]) >= 1
        return NOW
    w.authentication._clock = now
    receipt, _ = w.authentication.password_login('alice', w.passwords['alice'])
    assert receipt.principal_id == 'alice'
