"""Canonical immutable configuration revisions and serialized installation head.

Provider binding and runtime resource activation consume this store; callers
validate candidates before entering its short database transactions.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sys
from uuid import uuid4

import sqlalchemy as sa

from .config_secrets import ConfigurationCipher, ConfigurationSecretError, load_installation_key
from .config_values import ConfigurationValues
from .config_profiles import ConfigurationDocument, ProviderConfiguration, ProviderProfile
from .config_lifecycle import InstallationLifecycle


_LOCK_ID = 0x464158434F4E46
_STATE_ID = 'installation'


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
        # Startup migration validates the schema. Runtime reflection avoids
        # coupling live queries to historical migration implementation code.
        metadata = sa.MetaData()
        try:
            metadata.reflect(engine, only=['configuration_state', 'configuration_revisions', 'provider_profiles',
                                           'fax_jobs', 'fax_job_bindings', 'inbound_fax_bindings'])
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot open installation configuration storage.') from None
        self.state = metadata.tables['configuration_state']
        self.revisions = metadata.tables['configuration_revisions']
        self.profiles = metadata.tables['provider_profiles']
        self.jobs = metadata.tables['fax_jobs']
        self.job_bindings = metadata.tables['fax_job_bindings']

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
                yield connection
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

    def accept_outbound(self, prepared_revision: ConfigurationRevision, job: dict):
        """Commit a prepared job and exact provider binding under the Apply lock.

        Preparation and validation occur before entry. A new active revision
        makes that preparation stale, even if it happens to name the same
        provider. Pending edits alone do not alter the active preparation frame.
        """
        if (not isinstance(job, dict) or not isinstance(job.get('id'), str)
                or not job['id'] or job.get('status') != 'queued'
                or set(job) - set(self.jobs.c.keys())):
            raise ConfigurationStoreError('Invalid prepared fax job.')
        data = dict(job)
        with self._locked() as connection:
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
            return profile

    def outbound_profile(self, job_id):
        try:
            with self.engine.connect() as connection:
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
                return self._profile(connection, cipher, head['installation_id'], binding['profile_id'])
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot read fax provider binding.') from None

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
            identity = self._insert(connection, cipher, installation, values, parent=None, actor=actor,
                                    profiles=profiles, plugins=plugin_document)
            connection.execute(self.state.insert().values(id=_STATE_ID, installation_id=installation,
                generation=1, active_revision_id=identity, pending_revision_id=None, updated_at=datetime.utcnow()))
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
        candidates = self._provider_candidates(providers)
        plugin_document = ConfigurationDocument(plugins) if plugins is not None else None
        with self._locked() as connection:
            head = self._head(connection)
            if (head is None or head['installation_id'] != expected.installation_id
                    or head['generation'] != expected.generation
                    or (head['pending_revision_id'] or head['active_revision_id']) != expected.desired.id):
                raise ConfigurationConflict('Configuration changed; reload before applying edits.')
            cipher = self._cipher()
            current = self._snapshot(connection, cipher, head)
            profiles = self._select_profiles(connection, cipher, current.installation_id, candidates, current.desired.profiles)
            plugin_document = plugin_document if plugin_document is not None else current.desired.plugins
            if (values.to_environment() == current.desired.values.to_environment()
                    and profiles == current.desired.profiles and plugin_document == current.desired.plugins):
                return current
            identity = self._insert(connection, cipher, current.installation_id, values,
                                    parent=current.desired.id, actor=actor, profiles=profiles, plugins=plugin_document)
            connection.execute(self.state.update().where(self.state.c.id == _STATE_ID).values(
                generation=current.generation + 1,
                active_revision_id=current.active.id if restart_required else identity,
                pending_revision_id=identity if restart_required else None, updated_at=datetime.utcnow()))
            return self._snapshot(connection, cipher, self._head(connection))

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
            connection.execute(self.state.update().where(self.state.c.id == _STATE_ID).values(
                active_revision_id=current.pending.id, pending_revision_id=None,
                generation=current.generation + 1, updated_at=datetime.utcnow()))
            return self._snapshot(connection, cipher, self._head(connection))
