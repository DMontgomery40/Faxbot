"""Internal installation-owned service composition; no HTTP/GUI acceptance."""
import asyncio
import base64
from datetime import datetime, timezone
import importlib

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore
from api.app.config_profiles import ProviderConfiguration
from api.app.config_values import ConfigurationValues
from api.app.config_secrets import load_installation_key
from api.app.access.credentials import CredentialCodec
from api.app.access.fax_resources import FaxAccessError
from api.app.access.mutation_types import KeyValues, OwnerEnrollment, VersionedEntity
from api.app.access.session_codec import SessionCodec
from api.app.access.store import AccessStore
from api.app.access.types import (
    AccessUnavailableError, AuthenticationError, ResourceRef, ScopedPermission)

try:
    R = importlib.import_module('api.app.access.runtime')
except ModuleNotFoundError:
    R = None


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def rows(configuration, name):
    with configuration.engine.connect() as connection:
        table = sa.Table(name, sa.MetaData(), autoload_with=connection)
        return [dict(row) for row in connection.execute(sa.select(table)).mappings()]


def state(configuration):
    return {name: rows(configuration, name) for name in (
        'configuration_state', 'configuration_revisions', 'provider_profiles',
        'access_state', 'access_principals', 'access_sessions', 'access_audit',
        'access_auth_buckets', 'fax_jobs', 'outbound_deliveries', 'outbound_attempts')}


@pytest.fixture
def configuration(database, tmp_path):
    assert R is not None, 'Installation-owned access runtime is missing'
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    configuration.initialize(ConfigurationValues.from_environment({
        'API_KEY': 'synthetic bootstrap A', 'FAX_DISABLED': 'true',
    }), actor='internal-test', providers={'outbound': ProviderConfiguration(
        'phaxio', credentials={'api_key': 'synthetic-provider-key'})})
    return configuration


def test_runtime_module_exists():
    assert R is not None, 'Installation-owned access runtime is missing'


@pytest.mark.parametrize('invalid', [None, object(), {'store': 'not canonical'}])
def test_invalid_configuration_input_is_safe(invalid):
    assert R is not None
    with pytest.raises(AccessUnavailableError) as error:
        R.AccessRuntime(invalid)
    assert error.value.args == ('access_unavailable',)
    assert error.value.__context__ is None and error.value.__cause__ is None


def test_exact_existing_store_and_service_identity_without_publication(configuration, monkeypatch):
    before = state(configuration)
    monkeypatch.setattr(AccessStore, '__init__',
        lambda *_: pytest.fail('Runtime must not construct another access store'))
    runtime = R.AccessRuntime(configuration)
    assert runtime.configuration is configuration
    assert runtime.store is configuration.access_store
    for service in (runtime.bootstrap, runtime.control, runtime.proofs, runtime.mutations,
                    runtime.sessions, runtime.admission, runtime.authentication,
                    runtime.fax_resources, runtime.outbound):
        assert service.store is runtime.store
    assert runtime.proofs.codec is runtime.credential_codec
    assert runtime.mutations.codec is runtime.credential_codec
    assert runtime.sessions.control is runtime.control
    assert runtime.sessions.proofs is runtime.proofs
    assert runtime.sessions.bootstrap is runtime.bootstrap
    assert runtime.sessions.codec is runtime.session_codec
    assert runtime.sessions.password_codec is runtime.credential_codec
    assert runtime.authentication.sessions is runtime.sessions
    assert runtime.authentication.admission is runtime.admission
    assert runtime.authentication.codec is runtime.credential_codec
    assert runtime.authentication.session_codec is runtime.session_codec
    assert runtime.fax_resources.control is runtime.control
    assert runtime.outbound.configuration is configuration
    assert runtime.outbound.resources is runtime.fax_resources
    assert state(configuration) == before
    assert not hasattr(runtime, 'serving')


def test_key_loaded_once_and_dummy_hash_prepared_outside_transactions(configuration, monkeypatch):
    calls, active, preparations, gates = [], set(), [], []
    sa.event.listen(configuration.engine, 'begin', lambda c: active.add(id(c)))
    sa.event.listen(configuration.engine, 'commit', lambda c: active.discard(id(c)))
    sa.event.listen(configuration.engine, 'rollback', lambda c: active.discard(id(c)))
    def load(path, *, allow_create):
        calls.append((path, allow_create))
        return load_installation_key(path, allow_create=allow_create)
    original = CredentialCodec.prepare_chosen_password
    def prepare(codec, password):
        assert not active, 'Startup KDF must not retain a database transaction'
        preparations.append(True)
        return original(codec, password)
    original_work = R.AuthenticationWork
    def work():
        gate = original_work()
        gates.append(gate)
        return gate
    monkeypatch.setattr(R, 'load_installation_key', load)
    monkeypatch.setattr(CredentialCodec, 'prepare_chosen_password', prepare)
    monkeypatch.setattr(R, 'AuthenticationWork', work)
    runtime = R.AccessRuntime(configuration)
    assert calls == [(configuration.key_path, False)]
    assert preparations == [True]
    assert gates == [runtime.work]
    monkeypatch.setattr('api.app.config_store.load_installation_key',
        lambda *_a, **_k: pytest.fail('Authentication must not read the key file'))
    actor = runtime.authentication.header_key('synthetic bootstrap A')
    receipt, prepared = runtime.authentication.key_login('synthetic bootstrap A')
    authenticated = runtime.sessions.authenticate(prepared._token_for_committed_adapter(), now=utc_now())
    assert actor.replay_scope == authenticated.replay_scope == 'key:env'
    assert receipt.source_kind == 'bootstrap'
    assert runtime.control.authorize(authenticated, 'settings:read', ResourceRef('installation'), now=utc_now()).allowed
    assert calls == [(configuration.key_path, False)]


@pytest.mark.parametrize('damage', ['missing', 'public', 'malformed', 'raw32', 'symlink'])
def test_invalid_key_fails_safely_without_creation(configuration, tmp_path, damage):
    key_path = configuration.key_path
    if damage == 'missing':
        key_path.unlink()
    elif damage == 'public':
        key_path.chmod(0o644)
    elif damage == 'malformed':
        key_path.write_bytes(b'!' * 44)
    elif damage == 'raw32':
        key_path.write_bytes(b'x' * 32)
    else:
        destination = tmp_path / 'synthetic-target.key'
        key_path.rename(destination)
        key_path.symlink_to(destination)
    before = {path.name for path in tmp_path.iterdir()}
    with pytest.raises(AccessUnavailableError) as error:
        R.AccessRuntime(configuration)
    assert error.value.args == ('access_unavailable',)
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert {path.name for path in tmp_path.iterdir()} == before
    if damage == 'missing':
        assert not key_path.exists()


@pytest.mark.parametrize('stage', ['load_installation_key', 'BootstrapCredentials',
                                  'SessionCodec', 'AuthenticationService', 'AuthenticationWork'])
def test_preparation_failure_is_fixed_and_has_no_retained_private_context(configuration, monkeypatch, capsys, stage):
    before = state(configuration)
    def fail(*_args, **_kwargs):
        raise RuntimeError('synthetic private key or storage detail')
    monkeypatch.setattr(R, stage, fail)
    with pytest.raises(AccessUnavailableError) as error:
        R.AccessRuntime(configuration)
    assert error.value.args == ('access_unavailable',)
    assert error.value.__context__ is None and error.value.__cause__ is None
    assert state(configuration) == before
    output = capsys.readouterr()
    assert output.out == output.err == ''


def test_missing_admission_storage_fails_safely(configuration):
    with configuration.engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE access_auth_buckets')
    with pytest.raises(AccessUnavailableError) as error:
        R.AccessRuntime(configuration)
    assert error.value.args == ('access_unavailable',)
    assert error.value.__context__ is None and error.value.__cause__ is None


def test_separate_runtime_reuses_installation_identity_not_process_gate(configuration):
    first, second = R.AccessRuntime(configuration), R.AccessRuntime(configuration)
    assert first.store is second.store is configuration.access_store
    assert first.work is not second.work
    receipt, prepared = first.authentication.key_login('synthetic bootstrap A')
    token, csrf = prepared._token_for_committed_adapter(), prepared._csrf_for_committed_adapter()
    actor = second.sessions.authenticate(token, now=utc_now())
    assert actor.principal_id == 'bootstrap'
    raw_key = base64.urlsafe_b64decode(load_installation_key(configuration.key_path, allow_create=False))
    expected = SessionCodec(installation_key=raw_key)
    row = next(row for row in rows(configuration, 'access_sessions') if row['id'] == receipt.session_id)
    assert row['token_hash'] == expected.token_hash(token)
    assert expected.verify_csrf(token, csrf, row['csrf_hash'])
    assert token not in repr(row) and csrf not in repr(row)
    assert repr(first) == 'AccessRuntime()'


def test_cached_installation_material_does_not_cache_active_bootstrap_authority(configuration):
    runtime = R.AccessRuntime(configuration)
    _, prepared = runtime.authentication.key_login('synthetic bootstrap A')
    token = prepared._token_for_committed_adapter()
    for key in ('synthetic bootstrap B', 'synthetic bootstrap A'):
        current = configuration.read()
        configuration.apply(current, current.desired.values.with_patch({'api_key': key}),
            restart_required=False, actor='internal-test')
    with pytest.raises(AuthenticationError):
        runtime.sessions.authenticate(token, now=utc_now())
    assert runtime.authentication.header_key('synthetic bootstrap A').replay_scope == 'key:env'


def test_real_password_reset_authorization_and_held_acceptance_compose(configuration):
    runtime = R.AccessRuntime(configuration)
    bootstrap = runtime.authentication.header_key('synthetic bootstrap A')
    temporary = runtime.credential_codec.prepare_temporary_password()
    policy = rows(configuration, 'access_state')[0]['policy_version']
    created = runtime.mutations.enroll_owner(bootstrap,
        OwnerEnrollment('Runtime.Owner', 'Synthetic owner'), temporary,
        expected_policy_version=policy, now=utc_now())
    initial_password = temporary._temporary_secret_for_committed_adapter()
    async def login_and_replace():
        receipt, prepared = await runtime.work.run(lambda:
            runtime.authentication.password_login(' runtime.owner ', initial_password))
        token = prepared._token_for_committed_adapter()
        actor = runtime.sessions.authenticate(token, now=utc_now())
        assert receipt.password_change_required
        with runtime.store.transaction() as connection:
            with pytest.raises(FaxAccessError) as error:
                runtime.fax_resources.authorize_send_on(connection, actor, now=utc_now())
            assert error.value.code == 'reset_required'
        replacement_receipt, replacement = await runtime.work.run(lambda:
            runtime.authentication.change_password(actor, initial_password, 'synthetic chosen replacement'))
        with pytest.raises(AuthenticationError):
            runtime.sessions.authenticate(token, now=utc_now())
        assert not replacement_receipt.password_change_required
        return runtime.sessions.authenticate(replacement._token_for_committed_adapter(), now=utc_now())
    actor = asyncio.run(login_and_replace())
    assert actor.principal_id == created.target.id
    assert runtime.control.authorize(actor, 'settings:read', ResourceRef('installation'), now=utc_now()).allowed
    now = utc_now()
    job = dict(id='runtime-held', to_number='+12025550123', file_name='synthetic.pdf',
        tiff_path='', status='queued', created_at=now, updated_at=now)
    profile = runtime.outbound.accept(actor, configuration.read().active, job)
    assert profile.configuration.provider_id == 'phaxio'
    delivery = rows(configuration, 'outbound_deliveries')[0]
    assert delivery['dispatch_mode'] == 'held' and delivery['state'] == 'held'
    assert rows(configuration, 'outbound_attempts') == []
    audits = rows(configuration, 'access_audit')
    accepted = [row for row in audits if row['operation'] == 'fax.accept']
    assert len(accepted) == 1 and accepted[0]['actor_principal_id'] == actor.principal_id
    with runtime.store.transaction() as connection:
        fax_resource = runtime.fax_resources.require_outbound_on(connection, actor, job['id'],
            'fax:read', now=utc_now())
    assert 'synthetic chosen replacement' not in repr(audits)
    principal = next(row for row in rows(configuration, 'access_principals')
                     if row['id'] == actor.principal_id)
    prepared_key = runtime.credential_codec.prepare_new_key()
    key = runtime.mutations.issue_key(actor, KeyValues(
        VersionedEntity(principal['id'], principal['version']), 'Synthetic reader', '', None,
        (ScopedPermission('fax:read', ResourceRef('installation')),)), prepared_key,
        expected_policy_version=rows(configuration, 'access_state')[0]['policy_version'], now=utc_now())
    key_token = prepared_key._token_for_committed_adapter()
    key_actor = runtime.authentication.header_key(key_token)
    receipt, prepared = runtime.authentication.key_login(key_token)
    session_actor = runtime.sessions.authenticate(prepared._token_for_committed_adapter(), now=utc_now())
    assert key_actor.replay_scope == session_actor.replay_scope == 'key:' + key.public_key_id
    assert receipt.source_kind == 'key'
    assert runtime.control.authorize(session_actor, 'fax:read', fax_resource, now=utc_now()).allowed
    assert not runtime.control.authorize(session_actor, 'settings:read', ResourceRef('installation'), now=utc_now()).allowed
