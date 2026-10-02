"""Canonical immutable configuration revisions and serialized installation head.

Provider binding and runtime resource activation consume this store; callers
validate candidates before entering its short database transactions.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from .config_secrets import ConfigurationCipher, ConfigurationSecretError, load_installation_key
from .config_values import ConfigurationValues


_LOCK_ID = 0x464158434F4E46
_STATE_ID = 'installation'


class ConfigurationStoreError(RuntimeError):
    """Sanitized store failure."""


class ConfigurationConflict(ConfigurationStoreError):
    """The editor must reload and validate against the new desired revision."""


class ConfigurationCommitUncertain(ConfigurationStoreError):
    """A write acknowledgement failed; reload durable state before retrying."""


@dataclass(frozen=True)
class ConfigurationRevision:
    id: str
    values: ConfigurationValues = field(repr=False)


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
        metadata.reflect(engine, only=['configuration_state', 'configuration_revisions', 'provider_profiles'])
        self.state = metadata.tables['configuration_state']
        self.revisions = metadata.tables['configuration_revisions']
        self.profiles = metadata.tables['provider_profiles']

    @contextmanager
    def _locked(self):
        with self.engine.connect() as connection:
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
                except sa.exc.SQLAlchemyError:
                    raise ConfigurationCommitUncertain('Configuration commit was not acknowledged; reload before retrying.') from None
            except sa.exc.SQLAlchemyError:
                connection.rollback()
                raise ConfigurationStoreError('Configuration transaction could not complete.') from None
            except BaseException:
                connection.rollback()
                raise

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
        if set(payload) != {'environment'} or not isinstance(payload['environment'], dict):
            raise ConfigurationStoreError('Invalid stored configuration revision.')
        return ConfigurationRevision(identity, ConfigurationValues.from_environment(payload['environment']))

    def _snapshot(self, connection, cipher, head):
        installation = head['installation_id']
        return ConfigurationSnapshot(installation, head['generation'],
            self._revision(connection, cipher, installation, head['active_revision_id']),
            self._revision(connection, cipher, installation, head['pending_revision_id'])
            if head['pending_revision_id'] else None)

    def _insert(self, connection, cipher, installation, values, *, parent, actor):
        if not isinstance(actor, str) or not actor or len(actor) > 100:
            raise ConfigurationStoreError('Invalid configuration actor.')
        identity = str(uuid4())
        envelope = cipher.seal({'environment': values.to_environment()},
                               installation_id=installation, kind='revision', record_id=identity)
        connection.execute(self.revisions.insert().values(id=identity, parent_id=parent,
            format_version=1, key_id=cipher.key_id, envelope=envelope, actor=actor, created_at=datetime.utcnow()))
        return identity

    def initialize(self, values: ConfigurationValues, *, actor: str):
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
            identity = self._insert(connection, cipher, installation, values, parent=None, actor=actor)
            connection.execute(self.state.insert().values(id=_STATE_ID, installation_id=installation,
                generation=1, active_revision_id=identity, pending_revision_id=None, updated_at=datetime.utcnow()))
            return self._snapshot(connection, cipher, self._head(connection))

    def read(self):
        try:
            with self.engine.connect() as connection:
                head = self._head(connection)
                if head is None:
                    raise ConfigurationStoreError('Configuration has not been initialized.')
                return self._snapshot(connection, self._cipher(), head)
        except sa.exc.SQLAlchemyError:
            raise ConfigurationStoreError('Cannot read installation configuration.') from None

    def apply(self, expected: ConfigurationSnapshot, values: ConfigurationValues, *, restart_required: bool, actor: str):
        with self._locked() as connection:
            head = self._head(connection)
            if (head is None or head['installation_id'] != expected.installation_id
                    or head['generation'] != expected.generation
                    or (head['pending_revision_id'] or head['active_revision_id']) != expected.desired.id):
                raise ConfigurationConflict('Configuration changed; reload before applying edits.')
            cipher = self._cipher()
            current = self._snapshot(connection, cipher, head)
            if values.to_environment() == current.desired.values.to_environment():
                return current
            identity = self._insert(connection, cipher, current.installation_id, values,
                                    parent=current.desired.id, actor=actor)
            connection.execute(self.state.update().where(self.state.c.id == _STATE_ID).values(
                generation=current.generation + 1,
                active_revision_id=current.active.id if restart_required else identity,
                pending_revision_id=identity if restart_required else None, updated_at=datetime.utcnow()))
            return self._snapshot(connection, cipher, self._head(connection))
