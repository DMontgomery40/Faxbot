"""Canonical immutable configuration revisions and serialized installation head.

Provider binding and runtime resource activation consume this store; callers
validate candidates before entering its short database transactions.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from threading import Lock
from uuid import uuid4

import sqlalchemy as sa

from .config_secrets import ConfigurationCipher, ConfigurationSecretError, load_installation_key
from .config_values import ConfigurationValues
from .config_profiles import ConfigurationDocument, ProviderConfiguration, ProviderProfile
from .config_lifecycle import InstallationLifecycle
from .access.store import AccessStore
from .access.types import AccessError
from .access.types import (AuthenticationError, InvalidTransactionError, ResourceRef,
                           StaleCredentialError)
from .access.mutation_types import MutationDeniedError, MutationReason
from .access.configuration import configuration_candidate_requirements


_LOCK_ID = 0x464158434F4E46
_STATE_ID = 'installation'
_AUTHORIZED_OPERATIONS = {'settings.update': 'settings:write', 'providers.configure': 'providers:write'}
_CONFIGURATION_MARKERS = object()
_LISTENER_LOCK = Lock()


def _utc_now():
    return datetime.utcnow()


def _invalidate_configuration_locks(connection):
    connection.info.pop(_CONFIGURATION_MARKERS, None)


def _invalidate_configuration_boundary(connection, cursor, statement, parameters, context, executemany):
    # PostgreSQL accepts batched raw control SQL without whitespace between
    # commands. Conservative splitting can invalidate harmless quoted content;
    # it must never preserve evidence across a replaced physical transaction.
    for part in statement.split(';'):
        command = re.match(r'\A(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*([A-Za-z]+)', part, flags=re.S)
        if command and command[1].upper() in {'BEGIN', 'COMMIT', 'ROLLBACK', 'ABORT', 'END'}:
            _invalidate_configuration_locks(connection)
            return


class ConfigurationStoreError(RuntimeError):
    """Sanitized store failure."""


class ConfigurationConflict(ConfigurationStoreError):
    """The editor must reload and validate against the new desired revision."""


class ConfigurationNotInitialized(ConfigurationStoreError):
    """Only this condition permits first-import initialization."""


class ConfigurationCommitUncertain(ConfigurationStoreError):
    """A write acknowledgement failed; reload durable state before retrying."""


class UnboundProviderProfile(ConfigurationStoreError):
    """Historical work cannot be associated with current credentials by inference."""


@dataclass(frozen=True)
class ConfigurationRevision:
    id: str
    values: ConfigurationValues = field(repr=False)
    profiles: tuple[tuple[str, str], ...] = ()
    plugins: ConfigurationDocument = field(default_factory=lambda: ConfigurationDocument({}), repr=False)

    def profile_id(self, role):
        return dict(self.profiles).get(role)


@dataclass(frozen=True)
class ConfigurationSnapshot:
    installation_id: str
    generation: int
    active: ConfigurationRevision
    pending: ConfigurationRevision | None

    @property
    def desired(self):
        return self.pending or self.active


class ConfigurationStore:
    def __init__(self, engine, key_path: str | Path):
        self.engine = engine
        self.key_path = Path(key_path)
        self._lock_marker = object()
        with _LISTENER_LOCK:
            for event, listener in (('begin', _invalidate_configuration_locks),
                                    ('commit', _invalidate_configuration_locks),
                                    ('rollback', _invalidate_configuration_locks),
                                    ('before_cursor_execute', _invalidate_configuration_boundary)):
                if not sa.event.contains(engine, event, listener):
                    sa.event.listen(engine, event, listener)
        # Startup migration validates the schema. Runtime reflection avoids
        # coupling live queries to historical migration implementation code.
        metadata = sa.MetaData()
        try:
            metadata.reflect(engine, only=['configuration_state', 'configuration_revisions', 'provider_profiles',
                                           'fax_jobs', 'fax_job_bindings', 'inbound_fax_bindings',
                                           'outbound_deliveries', 'outbound_attempts', 'outbound_events'])
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot open installation configuration storage.') from None
        self.state = metadata.tables['configuration_state']
        self.revisions = metadata.tables['configuration_revisions']
        self.profiles = metadata.tables['provider_profiles']
        self.jobs = metadata.tables['fax_jobs']
        self.job_bindings = metadata.tables['fax_job_bindings']
        self.delivery_tables = {name: metadata.tables[name] for name in
                                ('outbound_deliveries', 'outbound_attempts', 'outbound_events')}
        # Every canonical writer owns this invariant, including stopped startup
        # promotion. Runtime-only optional callbacks could revive A->B->A sessions.
        try:
            self.access_store = AccessStore(engine)
        except AccessError:
            raise ConfigurationStoreError('Cannot open installation access storage.') from None

    @contextmanager
    def _locked(self):
        connection = None
        committed = False
        try:
            connection = self.engine.connect()
            try:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('BEGIN IMMEDIATE')
                elif connection.dialect.name == 'postgresql':
                    connection.begin()
                    connection.execute(sa.text("SELECT set_config('lock_timeout', '10000ms', true)"))
                    connection.execute(sa.text('SELECT pg_advisory_xact_lock(:key)'), {'key': _LOCK_ID})
                else:
                    raise ConfigurationStoreError('Unsupported configuration database.')
                transaction = self.access_store._transaction_on(connection)
                markers = connection.info.setdefault(_CONFIGURATION_MARKERS, {})
                markers[self._lock_marker] = transaction
                try:
                    yield connection
                finally:
                    markers.pop(self._lock_marker, None)
                try:
                    connection.commit()
                    committed = True
                except sa.exc.SQLAlchemyError:
                    raise ConfigurationCommitUncertain('Configuration commit was not acknowledged; reload before retrying.') from None
            except BaseException:
                # A failed connection may also reject rollback. Preserve the
                # primary failure, especially the uncertain-commit signal.
                try:
                    connection.rollback()
                except sa.exc.SQLAlchemyError:
                    try:
                        connection.invalidate()
                    except sa.exc.SQLAlchemyError:
                        pass
                raise
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Configuration transaction could not complete.') from None
        finally:
            unwinding = sys.exc_info()[0] is not None
            if connection is not None:
                try:
                    connection.close()
                except sa.exc.SQLAlchemyError:
                    if not unwinding:
                        if committed:
                            raise ConfigurationCommitUncertain('Configuration connection cleanup failed after commit; reload before retrying.') from None
                        raise ConfigurationStoreError('Configuration connection cleanup failed.') from None

    def _require_lock_on(self, connection):
        """Require this store's existing configuration lock, never acquire one."""
        transaction = self.access_store._transaction_on(connection)
        if connection.info.get(_CONFIGURATION_MARKERS, {}).get(self._lock_marker) is not transaction:
            raise InvalidTransactionError()
        self.access_store._physical_transaction_on(connection)

    def _activate_bootstrap_on(self, connection, previous_key, active_key, now):
        try:
            version = self.access_store.lock_on(connection)
            tables = self.access_store.tables
            principals = tables['access_principals']
            principal = connection.execute(sa.select(principals).where(
                principals.c.id == 'bootstrap')).mappings().one_or_none()
            if (principal is None or principal['kind'] != 'bootstrap'
                    or principal['enabled'] not in (0, 1)
                    or type(principal['version']) is not int or principal['version'] < 1
                    or type(principal['security_version']) is not int or principal['security_version'] < 1):
                raise ConfigurationStoreError('Invalid installation bootstrap state.')
            if previous_key == active_key:
                return
            connection.execute(principals.update().where(principals.c.id == 'bootstrap').values(
                version=principal['version'] + 1, security_version=principal['security_version'] + 1,
                updated_at=now))
            sessions = tables['access_sessions']
            connection.execute(sessions.update().where(sessions.c.principal_id == 'bootstrap',
                sessions.c.revoked_at.is_(None)).values(revoked_at=now))
            state = tables['access_state']
            connection.execute(state.update().where(state.c.id == 'state').values(
                policy_version=version + 1, updated_at=now))
            # The existing free-form configuration actor label is not verified
            # access identity. This event records the system activation itself.
            connection.execute(tables['access_audit'].insert().values(
                id=uuid4().hex, actor_principal_id=None, actor_key_binding_id=None,
                actor_session_id=None, operation='configuration.bootstrap.activate',
                target_kind='principal', target_id='bootstrap', policy_version_before=version,
                policy_version_after=version + 1, outcome='allowed',
                details='{"source":"canonical_configuration"}', created_at=now))
        except AccessError:
            raise ConfigurationStoreError('Cannot activate installation bootstrap state.') from None

    def _head(self, connection):
        rows = connection.execute(sa.select(self.state)).mappings().all()
        if not rows:
            return None
        if len(rows) != 1 or rows[0]['id'] != _STATE_ID:
            raise ConfigurationStoreError('Invalid installation configuration state.')
        return rows[0]

    def _cipher(self, *, allow_create=False):
        return ConfigurationCipher(load_installation_key(self.key_path, allow_create=allow_create))

    def _revision(self, connection, cipher, installation_id, identity):
        row = connection.execute(sa.select(self.revisions).where(self.revisions.c.id == identity)).mappings().one_or_none()
        if row is None or row['format_version'] != 1 or row['key_id'] != cipher.key_id:
            raise ConfigurationSecretError('Cannot authenticate configuration revision.')
        payload = cipher.open(row['envelope'], installation_id=installation_id, kind='revision', record_id=identity)
        if (set(payload) != {'environment', 'profiles', 'plugins'} or not isinstance(payload['environment'], dict)
                or not isinstance(payload['profiles'], dict)
                or set(payload['profiles']) - {'outbound', 'inbound'}
                or any(not isinstance(value, str) or not value for value in payload['profiles'].values())):
            raise ConfigurationStoreError('Invalid stored configuration revision.')
        return ConfigurationRevision(identity, ConfigurationValues.from_environment(payload['environment']),
            tuple(sorted(payload['profiles'].items())), ConfigurationDocument(payload['plugins']))

    def _snapshot(self, connection, cipher, head):
        installation = head['installation_id']
        return ConfigurationSnapshot(installation, head['generation'],
            self._revision(connection, cipher, installation, head['active_revision_id']),
            self._revision(connection, cipher, installation, head['pending_revision_id'])
            if head['pending_revision_id'] else None)

    def _insert(self, connection, cipher, installation, values, *, parent, actor, profiles, plugins):
        if not isinstance(actor, str) or not actor or len(actor) > 100:
            raise ConfigurationStoreError('Invalid configuration actor.')
        identity = str(uuid4())
        envelope = cipher.seal({'environment': values.to_environment(), 'profiles': dict(profiles), 'plugins': plugins.as_dict()},
                               installation_id=installation, kind='revision', record_id=identity)
        connection.execute(self.revisions.insert().values(id=identity, parent_id=parent,
            format_version=1, key_id=cipher.key_id, envelope=envelope, actor=actor, created_at=datetime.utcnow()))
        return identity

    def _provider_candidates(self, providers):
        if providers is None:
            return None
        if (not isinstance(providers, dict) or set(providers) - {'outbound', 'inbound'}
                or any(not isinstance(value, ProviderConfiguration) for value in providers.values())):
            raise ConfigurationStoreError('Invalid provider selection.')
        return dict(providers)

    def _profile(self, connection, cipher, installation, identity):
        row = connection.execute(sa.select(self.profiles).where(self.profiles.c.id == identity)).mappings().one_or_none()
        if row is None or row['format_version'] != 1 or row['key_id'] != cipher.key_id:
            raise ConfigurationSecretError('Cannot authenticate provider profile.')
        payload = cipher.open(row['envelope'], installation_id=installation, kind='profile', record_id=identity)
        if set(payload) != {'configuration', 'account_id', 'manifest_digest'} or payload['account_id'] != row['account_id']:
            raise ConfigurationSecretError('Cannot authenticate provider account binding.')
        configuration = ProviderConfiguration.from_payload(payload['configuration'])
        if configuration.provider_id != row['provider_id'] or payload['manifest_digest'] != configuration.manifest_digest:
            raise ConfigurationSecretError('Cannot authenticate provider configuration binding.')
        return ProviderProfile(identity, row['account_id'], configuration)

    def _select_profiles(self, connection, cipher, installation, candidates, existing=()):
        if candidates is None:
            return existing
        available = [self._profile(connection, cipher, installation, identity) for identity in set(dict(existing).values())]
        selected = []
        for role, candidate in sorted(candidates.items()):
            profile = next((item for item in available if item.configuration == candidate), None)
            if profile is None:
                # This local identity deliberately asserts no remote-account
                # equivalence across credential or endpoint changes.
                identity, account = str(uuid4()), str(uuid4())
                envelope = cipher.seal({'configuration': candidate.as_dict(), 'account_id': account,
                    'manifest_digest': candidate.manifest_digest}, installation_id=installation, kind='profile', record_id=identity)
                connection.execute(self.profiles.insert().values(id=identity, account_id=account,
                    provider_id=candidate.provider_id, format_version=1, key_id=cipher.key_id,
                    envelope=envelope, created_at=datetime.utcnow()))
                profile = ProviderProfile(identity, account, candidate)
                available.append(profile)
            selected.append((role, profile.id))
        return tuple(selected)

    def read_profile(self, identity):
        try:
            with self.engine.connect() as connection:
                head = self._head(connection)
                if head is None:
                    raise ConfigurationStoreError('Configuration has not been initialized.')
                return self._profile(connection, self._cipher(), head['installation_id'], identity)
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot read provider profile.') from None

    def find_outbound_replay(self, request_identity):
        from .request_identity import find_replay
        with self.engine.connect() as connection:
            return find_replay(connection, self.delivery_tables['outbound_deliveries'], request_identity)

    def outbound_replay_max_bytes(self, request_identity):
        """Use only this scoped key's authenticated accepted upload constraint."""
        from .request_identity import find_scoped_request
        try:
            with self.engine.connect() as connection:
                row = find_scoped_request(connection, self.delivery_tables['outbound_deliveries'], request_identity)
                if row is None:
                    return None
                revision, _ = self._outbound_context(connection, row['id'])
                return revision.values.max_file_size_mb * 1024 * 1024
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot read accepted fax upload limit.') from None

    def accept_outbound(self, prepared_revision: ConfigurationRevision, job: dict, *, request_identity=None):
        """Internal trusted acceptance with no human authorization of its own.

        Human route integration must use AuthorizedOutbound. This retained
        delivery/maintenance primitive does not infer authority or ownership
        from a job or replay namespace.
        """
        with self._locked() as connection:
            return self._accept_outbound_on(connection, prepared_revision, job,
                                            request_identity=request_identity)

    def _accept_outbound_on(self, connection, prepared_revision, job, *, request_identity=None):
        """Write on the caller's existing configuration installation lock.

        AuthorizedOutbound acquires config then access, verifies the current
        creator/replay visibility, and adds resource/audit before this commits.
        No nested transaction or independent commit is opened here.
        """
        self.access_store._transaction_on(connection)
        self.access_store._physical_transaction_on(connection)
        if (not isinstance(job, dict) or not isinstance(job.get('id'), str)
                or not job['id'] or job.get('status') != 'queued'
                or set(job) - set(self.jobs.c.keys())):
            raise ConfigurationStoreError('Invalid prepared fax job.')
        data = dict(job)
        if request_identity is not None:
            from .request_identity import find_replay, IdempotentReplay
            existing = find_replay(connection, self.delivery_tables['outbound_deliveries'], request_identity)
            if existing is not None:
                # Replay wins before current configuration/provider preflight.
                # No new job, artifact binding or history entry is created.
                raise IdempotentReplay(existing)
        head = self._head(connection)
        if head is None or head['active_revision_id'] != prepared_revision.id:
            raise ConfigurationConflict('Configuration changed during fax preparation; prepare again before acceptance.')
        cipher = self._cipher()
        active = self._revision(connection, cipher, head['installation_id'], head['active_revision_id'])
        identity = active.profile_id('outbound')
        if identity is None:
            raise ConfigurationStoreError('Outbound fax delivery is disabled.')
        if identity != prepared_revision.profile_id('outbound'):
            raise ConfigurationConflict('Provider binding changed during fax preparation.')
        profile = self._profile(connection, cipher, head['installation_id'], identity)
        data.update(backend=profile.configuration.provider_id, outbound_backend=profile.configuration.provider_id)
        connection.execute(self.jobs.insert().values(**data))
        connection.execute(self.job_bindings.insert().values(id=data['id'], revision_id=active.id, profile_id=identity))
        from .outbound_store import record_acceptance
        record_acceptance(connection, self.delivery_tables, data['id'], held=active.values.fax_disabled,
                          now=data.get('created_at') or datetime.utcnow())
        if request_identity is not None:
            deliveries = self.delivery_tables['outbound_deliveries']
            connection.execute(deliveries.update().where(deliveries.c.id == data['id']).values(
                principal_scope=request_identity.principal_scope,
                idempotency_digest=request_identity.idempotency_digest,
                request_fingerprint=request_identity.request_fingerprint))
        return profile

    def _outbound_context(self, connection, job_id):
        binding = connection.execute(sa.select(self.job_bindings).where(self.job_bindings.c.id == job_id)).mappings().one_or_none()
        if binding is None:
            raise UnboundProviderProfile('Fax has no verified provider binding; reconcile its original account before provider operations.')
        head = self._head(connection)
        if head is None:
            raise ConfigurationStoreError('Configuration has not been initialized.')
        cipher = self._cipher()
        revision = self._revision(connection, cipher, head['installation_id'], binding['revision_id'])
        if revision.profile_id('outbound') != binding['profile_id']:
            raise ConfigurationSecretError('Cannot authenticate fax provider binding.')
        return revision, self._profile(connection, cipher, head['installation_id'], binding['profile_id'])

    def outbound_context(self, job_id):
        """Return the authenticated acceptance revision and provider together."""
        try:
            with self.engine.connect() as connection:
                return self._outbound_context(connection, job_id)
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot read fax provider binding.') from None

    def outbound_profile(self, job_id):
        return self.outbound_context(job_id)[1]

    def initialize(self, values: ConfigurationValues, *, actor: str, providers=None, plugins=None):
        candidates = self._provider_candidates(providers)
        plugin_document = ConfigurationDocument(plugins if plugins is not None else {})
        with self._locked() as connection:
            head = self._head(connection)
            if head is not None:
                return self._snapshot(connection, self._cipher(), head)
            encrypted_exists = any(connection.execute(sa.select(table.c.id).limit(1)).first()
                                   for table in (self.revisions, self.profiles))
            if encrypted_exists:
                raise ConfigurationStoreError('Encrypted configuration exists without its installation head; restore installation state.')
            cipher = self._cipher(allow_create=True)
            installation = str(uuid4())
            profiles = self._select_profiles(connection, cipher, installation, candidates)
            now = datetime.utcnow()
            self._activate_bootstrap_on(connection, '', values.api_key, now)
            identity = self._insert(connection, cipher, installation, values, parent=None, actor=actor,
                                    profiles=profiles, plugins=plugin_document)
            connection.execute(self.state.insert().values(id=_STATE_ID, installation_id=installation,
                generation=1, active_revision_id=identity, pending_revision_id=None, updated_at=now))
            return self._snapshot(connection, cipher, self._head(connection))

    def read(self):
        try:
            with self.engine.connect() as connection:
                head = self._head(connection)
                if head is None:
                    raise ConfigurationNotInitialized('Configuration has not been initialized.')
                return self._snapshot(connection, self._cipher(), head)
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot read installation configuration.') from None

    def apply(self, expected: ConfigurationSnapshot, values: ConfigurationValues, *, restart_required: bool, actor: str,
              providers=None, plugins=None):
        """Trusted internal write; human adapters must use apply_authorized."""
        candidates = self._provider_candidates(providers)
        plugin_document = ConfigurationDocument(plugins) if plugins is not None else None
        with self._locked() as connection:
            current, cipher = self._checked_current_on(connection, expected)
            return self._write_candidate_on(connection, current, cipher, values, restart_required=restart_required,
                actor=actor, candidates=candidates, plugin_document=plugin_document or current.desired.plugins,
                now=_utc_now())

    def _checked_current_on(self, connection, expected):
        self._require_lock_on(connection)
        head = self._head(connection)
        if (head is None or head['installation_id'] != expected.installation_id
                or head['generation'] != expected.generation
                or (head['pending_revision_id'] or head['active_revision_id']) != expected.desired.id):
            raise ConfigurationConflict('Configuration changed; reload before applying edits.')
        cipher = self._cipher()
        return self._snapshot(connection, cipher, head), cipher

    def _write_candidate_on(self, connection, current, cipher, values, *, restart_required,
                            actor, candidates, plugin_document, now):
        self._require_lock_on(connection)
        profiles = self._select_profiles(connection, cipher, current.installation_id, candidates, current.desired.profiles)
        if (values.to_environment() == current.desired.values.to_environment()
                and profiles == current.desired.profiles and plugin_document == current.desired.plugins):
            return current
        self._activate_bootstrap_on(connection, current.active.values.api_key,
            current.active.values.api_key if restart_required else values.api_key, now)
        identity = self._insert(connection, cipher, current.installation_id, values,
                                parent=current.desired.id, actor=actor, profiles=profiles, plugins=plugin_document)
        connection.execute(self.state.update().where(self.state.c.id == _STATE_ID).values(
            generation=current.generation + 1,
            active_revision_id=current.active.id if restart_required else identity,
            pending_revision_id=identity if restart_required else None, updated_at=now))
        return self._snapshot(connection, cipher, self._head(connection))

    def apply_authorized(self, expected, values, *, principal, control, operation,
                         restart_required, providers, plugins, baseline_providers):
        """Commit one closed human operation and its audit before returning.

        Prepared candidates carry no authority. Config then access locks share
        one Connection; every current permission/source check uses a fresh clock.
        Routine denial commits audit and is raised only outside the transaction.
        """
        if getattr(control, 'store', None) is not self.access_store:
            raise InvalidTransactionError()
        if (type(operation) is not str or operation not in _AUTHORIZED_OPERATIONS
                or type(expected) is not ConfigurationSnapshot or not isinstance(values, ConfigurationValues)
                or type(restart_required) is not bool or providers is None or baseline_providers is None):
            raise ConfigurationStoreError('Invalid authorized configuration operation.')
        candidates = self._provider_candidates(providers)
        baseline = self._provider_candidates(baseline_providers)
        document = plugins if type(plugins) is ConfigurationDocument else ConfigurationDocument(plugins)
        with self._locked() as connection:
            self.access_store.lock_on(connection)
            result, error = self._apply_authorized_on(connection, expected, values, principal=principal,
                control=control, operation=operation, restart_required=restart_required,
                providers=candidates, plugins=document, baseline_providers=baseline)
        if error is not None:
            raise error
        return result

    def _apply_authorized_on(self, connection, expected, values, *, principal, control, operation,
                             restart_required, providers, plugins, baseline_providers):
        self._require_lock_on(connection)
        before = self.access_store.require_lock_on(connection)
        if getattr(control, 'store', None) is not self.access_store:
            raise InvalidTransactionError()
        if type(operation) is not str or operation not in _AUTHORIZED_OPERATIONS:
            raise ConfigurationStoreError('Invalid authorized configuration operation.')
        now = _utc_now()
        attribution, current, requirements, result, error, reason = (None, None, None), None, None, None, None, None
        try:
            decision = control.authorize_on(connection, principal, _AUTHORIZED_OPERATIONS[operation],
                ResourceRef('installation'), now=now)
            attribution = (principal.principal_id, getattr(principal.credential, 'binding_id', None),
                getattr(principal.credential, 'session_id', None))
            if not decision.allowed:
                reason = MutationReason.RESET_REQUIRED if decision.reason.value == 'reset_required' else MutationReason.FORBIDDEN
                error = MutationDeniedError(reason)
            else:
                current, cipher = self._checked_current_on(connection, expected)
                captured = {role: self._profile(connection, cipher, current.installation_id, identity).configuration
                    for role, identity in current.desired.profiles}
                requirements = configuration_candidate_requirements(current.desired.values, values,
                    current.desired.plugins, plugins, profile_drift=captured != baseline_providers)
                for permission in sorted(requirements.permissions):
                    decision = control.authorize_on(connection, principal, permission, ResourceRef('installation'), now=now)
                    if not decision.allowed:
                        reason, error = MutationReason.FORBIDDEN, MutationDeniedError(MutationReason.FORBIDDEN)
                        break
                if error is None and requirements.requires_complete_owner and not control.is_complete_owner_on(
                        connection, principal, now=now):
                    reason, error = MutationReason.OWNER_REQUIRED, MutationDeniedError(MutationReason.OWNER_REQUIRED)
        except AuthenticationError:
            attribution, reason, error = (None, None, None), 'credential_stale', StaleCredentialError()
        except ConfigurationConflict as conflict:
            reason, error = 'configuration_conflict', conflict
        # Routine denials above are candidate-before-write. A later business
        # failure must escape and roll back, even if it uses a denial class.
        if error is None:
            result = self._write_candidate_on(connection, current, cipher, values,
                restart_required=restart_required, actor='principal:' + principal.principal_id,
                candidates=providers, plugin_document=plugins, now=now)
        after = self.access_store.require_lock_on(connection)
        details = {'reason': reason.value if isinstance(reason, MutationReason) else reason} if error else {
            'changed': result.generation != current.generation,
            'configuration_generation_before': current.generation,
            'configuration_generation_after': result.generation,
            'fields': list(requirements.changed_fields[:32]),
            'field_count': len(requirements.changed_fields),
            'plugin_categories': list(requirements.plugin_categories),
            'profile_drift': requirements.profile_drift,
        }
        self._audit_configuration_on(connection, attribution, operation, before, after, error is not None, details, now)
        return result, error

    def _audit_configuration_on(self, connection, attribution, operation, before, after, denied, details, now):
        self._require_lock_on(connection)
        self.access_store.require_lock_on(connection)
        encoded = json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True)
        if len(encoded.encode('utf-8')) > 2048:
            raise ConfigurationStoreError('Configuration audit exceeds its safe bound.')
        principal, key, session = attribution
        connection.execute(self.access_store.tables['access_audit'].insert().values(
            id=uuid4().hex, actor_principal_id=principal, actor_key_binding_id=key, actor_session_id=session,
            operation=operation, target_kind='installation', target_id='installation',
            policy_version_before=before, policy_version_after=after, outcome='denied' if denied else 'allowed',
            details=encoded, created_at=now))

    def promote_pending(self, expected: ConfigurationSnapshot, *, lifecycle: InstallationLifecycle):
        """Publish the exact candidate whose resources the stopped starter prepared.

        The caller keeps lifecycle ownership through resource preparation, this
        transaction and mark_serving. Failed preparation never calls promotion.
        """
        if not isinstance(lifecycle, InstallationLifecycle) or not lifecycle.can_promote:
            raise ConfigurationConflict('Pending settings require a stopped-installation startup.')
        with self._locked() as connection:
            head = self._head(connection)
            if (head is None or head['installation_id'] != expected.installation_id
                    or head['generation'] != expected.generation
                    or (head['pending_revision_id'] or head['active_revision_id']) != expected.desired.id):
                raise ConfigurationConflict('Pending configuration changed during startup preparation.')
            cipher = self._cipher()
            current = self._snapshot(connection, cipher, head)
            if current.pending is None:
                return current
            now = datetime.utcnow()
            self._activate_bootstrap_on(connection, current.active.values.api_key, current.pending.values.api_key, now)
            connection.execute(self.state.update().where(self.state.c.id == _STATE_ID).values(
                active_revision_id=current.pending.id, pending_revision_id=None,
                generation=current.generation + 1, updated_at=now))
            return self._snapshot(connection, cipher, self._head(connection))

    def recover_bootstrap(self, secret: str):
        """Stopped-installation owner recovery: one new revision carrying a fresh bootstrap secret.

        Only the host operator, holding the database and the installation key,
        reaches this; there is no HTTP route. The caller proves the installation is
        stopped and reveals the secret once. The new revision copies the desired
        values with only ``api_key`` replaced and keeps provider bindings. Without a
        pending revision it becomes active at once, which revokes earlier bootstrap
        sessions and advances the access policy version; with one, it stays pending
        and the next stopped-installation startup activates both together. The
        recovery audit row commits in the same transaction.
        """
        if type(secret) is not str or not 32 <= len(secret) <= 256 or not secret.isprintable() or secret.strip() != secret:
            raise ConfigurationStoreError('Invalid installation recovery secret.')
        with self._locked() as connection:
            before = self.access_store.lock_on(connection)
            head = self._head(connection)
            if head is None:
                raise ConfigurationNotInitialized('Configuration has not been initialized.')
            cipher = self._cipher()
            current = self._snapshot(connection, cipher, head)
            values = current.desired.values.with_patch({'api_key': secret})
            pending = current.pending is not None
            now = _utc_now()
            result = self._write_candidate_on(connection, current, cipher, values, restart_required=pending,
                actor='host:owner-recovery', candidates=None, plugin_document=current.desired.plugins, now=now)
            after = self.access_store.require_lock_on(connection)
            self._audit_configuration_on(connection, (None, None, None), 'owner.recover', before, after, False, {
                'source': 'host_terminal', 'configuration_generation_before': current.generation,
                'configuration_generation_after': result.generation, 'pending_restart': pending}, now)
            return result
