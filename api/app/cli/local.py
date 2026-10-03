"""Local administration of a stopped Faxbot installation: status, upgrade, recovery, backup, restore.

These functions open the installation's database and files directly. Callers
first prove that no Faxbot server uses the installation (see ``stopped``).
Nothing here prints secrets; the recovery secret is returned to the caller,
which shows it once.
"""
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
from urllib.parse import quote

import sqlalchemy as sa

from .errors import EXIT_CONFLICT, EXIT_FAILURE, EXIT_NOT_FOUND, EXIT_RUNNING, CliError

LOCK_FILES = frozenset({'.faxbot-startup.lock', '.faxbot-serving.lock'})
BACKUP_FORMAT = 'faxbot-backup'
BACKUP_VERSION = 1
_SQLITE_COMPANIONS = ('', '-wal', '-shm', '-journal')


@dataclass(frozen=True)
class Installation:
    database_url: str
    data_dir: Path
    key_path: Path
    direct_key_path: Path

    @property
    def dialect(self):
        return sa.engine.make_url(self.database_url).get_backend_name()

    @property
    def sqlite_path(self):
        if self.dialect != 'sqlite':
            return None
        database = sa.engine.make_url(self.database_url).database
        return Path(database).absolute() if database and database != ':memory:' else None

    def describe_database(self):
        """The database without credentials, for people to read."""
        url = sa.engine.make_url(self.database_url)
        if self.dialect == 'sqlite':
            return f'SQLite file {self.sqlite_path}'
        return f'PostgreSQL database {url.database or ""} on {url.host or "localhost"}'.replace('  ', ' ')


def locate(environment, *, database_url=None, data_dir=None, key_path=None, direct_key_path=None):
    """The installation the server would use with this environment, unless overridden."""
    from ..config_values import ConfigurationValues
    values = ConfigurationValues.from_environment({name: environment[name] for name in ('DATABASE_URL', 'FAX_DATA_DIR')
                                                   if environment.get(name)})
    url = database_url or values.database_url
    directory = Path(data_dir or values.fax_data_dir).absolute()
    key = Path(key_path or environment.get('FAXBOT_INSTALLATION_KEY_PATH') or directory / '.configuration.key')
    direct = Path(direct_key_path or environment.get('FAXBOT_DIRECT_KEY_PATH') or directory / '.direct-identity.key')
    installation = Installation(url, directory, key.absolute(), direct.absolute())
    if installation.dialect not in {'sqlite', 'postgresql'}:
        raise CliError('The database must be SQLite or PostgreSQL.')
    return installation


def engine_for(installation):
    from ..schema import SchemaUpgradeError, create_database_engine
    try:
        return create_database_engine(installation.database_url)
    except (SchemaUpgradeError, sa.exc.ArgumentError):
        raise CliError('Cannot open the installation database. Check DATABASE_URL.') from None


@contextmanager
def stopped(installation, probe, *, create=False):
    """Hold the installation's startup and serving locks, proving no Faxbot server uses it.

    probe() raises CliError when a server answers on the configured address. The
    locks also stop a server from starting while the command runs.
    """
    from ..config_lifecycle import InstallationLifecycle, InstallationLifecycleError
    probe()
    if not installation.data_dir.is_dir():
        if not create:
            raise CliError(f'There is no Faxbot data folder at {installation.data_dir}. Set FAX_DATA_DIR or use '
                           '--data-dir.', EXIT_NOT_FOUND)
        try:
            installation.data_dir.mkdir(parents=True, mode=0o700)
        except OSError:
            raise CliError(f'Cannot create {installation.data_dir}.') from None
    running = CliError('Faxbot is running on this installation. Stop it, then run this command again.', EXIT_RUNNING)
    lifecycle = InstallationLifecycle(installation.data_dir, startup_timeout_seconds=1.0)
    try:
        lifecycle.acquire()
    except InstallationLifecycleError:
        raise running from None
    try:
        if not lifecycle.can_promote:
            raise running
        yield
    finally:
        try:
            lifecycle.close()
        except InstallationLifecycleError:
            pass


# -- status and upgrade ---------------------------------------------------------------

def schema_revision(connection):
    if not sa.inspect(connection).has_table('alembic_version'):
        return None
    versions = connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalars().all()
    return versions[0] if len(versions) == 1 else None


def _count(connection, tables, name, *conditions):
    if name not in tables:
        return None
    table = tables[name]
    query = sa.select(sa.func.count()).select_from(table)
    for condition in conditions:
        query = query.where(condition(table))
    return connection.execute(query).scalar_one()


def _owner_count(connection, tables):
    needed = {'access_assignments', 'access_principals', 'access_memberships', 'access_groups', 'access_users'}
    if not needed <= set(tables):
        return None
    a, p, m, g, u = (tables[name] for name in ('access_assignments', 'access_principals', 'access_memberships',
                                               'access_groups', 'access_users'))
    direct = set(connection.execute(sa.select(a.c.principal_id).where(
        a.c.role_id == 'role_owner', a.c.resource_id == 'installation', a.c.principal_id.is_not(None))).scalars())
    groups = set(connection.execute(sa.select(a.c.group_id).select_from(a.join(g, g.c.id == a.c.group_id)).where(
        a.c.role_id == 'role_owner', a.c.resource_id == 'installation', g.c.enabled == 1)).scalars())
    via_groups = set(connection.execute(sa.select(m.c.principal_id).where(m.c.group_id.in_(groups))).scalars()) if groups else set()
    candidates = direct | via_groups
    if not candidates:
        return 0
    return connection.execute(sa.select(sa.func.count()).select_from(p.join(u, u.c.id == p.c.id)).where(
        p.c.id.in_(candidates), p.c.enabled == 1)).scalar_one()


def status(installation):
    from ..schema import HEAD
    engine = engine_for(installation)
    try:
        with engine.connect() as connection:
            revision = schema_revision(connection)
            metadata = sa.MetaData()
            metadata.reflect(connection)
            tables = metadata.tables
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            counts = {
                'users': _count(connection, tables, 'access_principals', lambda t: t.c.kind == 'user'),
                'owners': _owner_count(connection, tables),
                'integrations': _count(connection, tables, 'access_principals', lambda t: t.c.kind == 'integration'),
                'api_keys': _count(connection, tables, 'api_keys', lambda t: t.c.revoked_at.is_(None)),
                'active_sessions': _count(connection, tables, 'access_sessions', lambda t: t.c.revoked_at.is_(None),
                                          lambda t: t.c.expires_at > now),
                'mailboxes': _count(connection, tables, 'mailboxes'),
                'sent_faxes': _count(connection, tables, 'fax_jobs'),
                'received_faxes': _count(connection, tables, 'inbound_faxes'),
            }
            head = None
            if 'configuration_state' in tables:
                head = connection.execute(sa.select(tables['configuration_state'])).mappings().first()
    except sa.exc.SQLAlchemyError:
        raise CliError('Cannot read the installation database. Check DATABASE_URL and that the database is '
                       'reachable.') from None
    result = {'database': installation.describe_database(), 'data_dir': str(installation.data_dir),
              'schema_revision': revision, 'schema_current': revision == HEAD, 'expected_revision': HEAD,
              'counts': counts, 'configuration': None}
    if head is not None:
        configuration = {'generation': head['generation'], 'restart_pending': head['pending_revision_id'] is not None,
                         'installation_key_set': None, 'key_file_found': installation.key_path.is_file()}
        if configuration['key_file_found']:
            try:
                from ..config_store import ConfigurationStore
                snapshot = ConfigurationStore(engine, installation.key_path).read()
                configuration['installation_key_set'] = bool(snapshot.active.values.api_key)
            except Exception:
                configuration['installation_key_set'] = None
        result['configuration'] = configuration
    engine.dispose()
    return result


def migrate(installation):
    from ..schema import HEAD, SchemaUpgradeError, upgrade_schema
    engine = engine_for(installation)
    try:
        with engine.connect() as connection:
            before = schema_revision(connection)
        try:
            upgrade_schema(engine)
        except SchemaUpgradeError as error:
            raise CliError(str(error)) from None
        with engine.connect() as connection:
            after = schema_revision(connection)
    except sa.exc.SQLAlchemyError:
        raise CliError('Cannot reach the installation database. Check DATABASE_URL.') from None
    finally:
        engine.dispose()
    return {'before': before, 'after': after, 'current': after == HEAD, 'changed': before != after}


# -- owner recovery ----------------------------------------------------------------------

def recover_owner(installation):
    """Write a fresh installation key (bootstrap secret) in a new configuration revision; return it."""
    from ..config_secrets import ConfigurationSecretError
    from ..config_store import ConfigurationNotInitialized, ConfigurationStore, ConfigurationStoreError
    if not installation.key_path.is_file():
        raise CliError(f'The installation key file {installation.key_path} was not found. Set '
                       'FAXBOT_INSTALLATION_KEY_PATH or use --key-file.', EXIT_NOT_FOUND)
    engine = engine_for(installation)
    secret = secrets.token_urlsafe(32)
    try:
        store = ConfigurationStore(engine, installation.key_path)
        snapshot = store.recover_bootstrap(secret)
    except ConfigurationNotInitialized:
        raise CliError('This installation has never started, so there is nothing to recover. Set API_KEY and start '
                       'Faxbot to create the first owner.', EXIT_NOT_FOUND) from None
    except ConfigurationSecretError:
        raise CliError('The installation key file does not match this database.') from None
    except ConfigurationStoreError as error:
        raise CliError(str(error)) from None
    finally:
        engine.dispose()
    return secret, snapshot.pending is not None


# -- backup and restore ------------------------------------------------------------------

def _digest(path):
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def _private_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as output, open(source, 'rb') as handle:
        shutil.copyfileobj(handle, output, 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(target, 0o600)


def _empty_directory(path, *, ignore=LOCK_FILES):
    return not path.exists() or not any(entry.name not in ignore for entry in path.iterdir())


class _Encoder(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, datetime):
            return {'$datetime': value.isoformat()}
        if isinstance(value, date):
            return {'$date': value.isoformat()}
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {'$bytes': base64.b64encode(bytes(value)).decode('ascii')}
        if isinstance(value, Decimal):
            return {'$decimal': str(value)}
        return super().default(value)


def _decode(value):
    if isinstance(value, dict) and len(value) == 1:
        (tag, item), = value.items()
        if tag == '$datetime':
            return datetime.fromisoformat(item)
        if tag == '$date':
            return date.fromisoformat(item)
        if tag == '$bytes':
            return base64.b64decode(item)
        if tag == '$decimal':
            return Decimal(item)
    return value


def _dump_postgresql(installation, target):
    engine = engine_for(installation)
    tables_dir = target / 'database' / 'tables'
    tables_dir.mkdir(parents=True, mode=0o700)
    order = []
    try:
        with engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                revision = schema_revision(connection)
                metadata = sa.MetaData()
                metadata.reflect(connection)
                for table in metadata.sorted_tables:
                    order.append(table.name)
                    path = tables_dir / f'{table.name}.jsonl'
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                        for row in connection.execute(sa.select(table)).mappings():
                            handle.write(json.dumps(dict(row), cls=_Encoder, ensure_ascii=False,
                                                    separators=(',', ':')) + '\n')
    except sa.exc.SQLAlchemyError:
        raise CliError('Cannot read the installation database for the backup.') from None
    finally:
        engine.dispose()
    descriptor = os.open(target / 'database' / 'tables.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump({'order': order}, handle)
    return revision


def _dump_sqlite(installation, target):
    source = installation.sqlite_path
    if source is None or not source.is_file():
        raise CliError(f'There is no database file at {source}. Check DATABASE_URL.', EXIT_NOT_FOUND)
    destination = target / 'database' / 'faxbot.sqlite3'
    destination.parent.mkdir(parents=True, mode=0o700)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        reader = sqlite3.connect('file:' + quote(str(source)) + '?mode=ro', uri=True)
        writer = sqlite3.connect(destination)
        try:
            reader.backup(writer)
            revision = None
            try:
                revision = writer.execute('SELECT version_num FROM alembic_version').fetchone()
            except sqlite3.Error:
                pass
        finally:
            writer.close()
            reader.close()
    except sqlite3.Error:
        raise CliError('Cannot copy the installation database for the backup.') from None
    return revision[0] if revision else None


def _excluded_data(installation):
    """Data folder entries a backup must not copy as data: locks, the live database and the keys."""
    excluded = {installation.data_dir / name for name in LOCK_FILES}
    if installation.sqlite_path is not None:
        excluded |= {Path(str(installation.sqlite_path) + suffix) for suffix in _SQLITE_COMPANIONS}
    return excluded | {installation.key_path, installation.direct_key_path}


def backup(installation, target):
    target = Path(target).resolve()
    if target.is_relative_to(installation.data_dir.resolve()):
        raise CliError('Choose a backup folder outside the data folder.', EXIT_CONFLICT)
    if not installation.key_path.is_file():
        raise CliError(f'The installation key file {installation.key_path} was not found. A backup without it '
                       'cannot be restored. Set FAXBOT_INSTALLATION_KEY_PATH or use --key-file.', EXIT_NOT_FOUND)
    if target.exists() and (not target.is_dir() or not _empty_directory(target, ignore=frozenset())):
        raise CliError(f'{target} is not an empty folder. Choose a new folder for the backup.', EXIT_CONFLICT)
    try:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target, 0o700)
    except OSError:
        raise CliError(f'Cannot create {target}.') from None
    if installation.dialect == 'sqlite':
        revision = _dump_sqlite(installation, target)
    else:
        revision = _dump_postgresql(installation, target)
    excluded = _excluded_data(installation)
    skipped = 0
    for root, directories, names in os.walk(installation.data_dir):
        root_path = Path(root)
        directories[:] = [name for name in directories if not (root_path / name).is_symlink()]
        for name in names:
            path = root_path / name
            if path in excluded:
                continue
            if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                skipped += 1
                continue
            _private_copy(path, target / 'data' / path.relative_to(installation.data_dir))
    _private_copy(installation.key_path, target / 'keys' / 'installation.key')
    if installation.direct_key_path.is_file():
        _private_copy(installation.direct_key_path, target / 'keys' / 'direct-identity.key')
    files = {}
    for path in sorted(target.rglob('*')):
        if path.is_file():
            files[path.relative_to(target).as_posix()] = {'sha256': _digest(path), 'bytes': path.stat().st_size}
    manifest = {'format': BACKUP_FORMAT, 'version': BACKUP_VERSION,
                'created_at': datetime.now(timezone.utc).isoformat(), 'database': installation.dialect,
                'schema_revision': revision, 'files': files}
    descriptor = os.open(target / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')
    data_files = sum(1 for name in files if name.startswith('data/'))
    return {'folder': str(target), 'database': installation.describe_database(), 'schema_revision': revision,
            'data_files': data_files, 'files': len(files) + 1, 'skipped_links': skipped,
            'direct_delivery_key': 'keys/direct-identity.key' in files}


def verify(source):
    """The manifest of a backup folder, after checking every file against it."""
    source = Path(source).absolute()
    try:
        manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise CliError(f'{source} is not a Faxbot backup folder (manifest.json is missing or unreadable).',
                       EXIT_NOT_FOUND) from None
    if (not isinstance(manifest, dict) or manifest.get('format') != BACKUP_FORMAT
            or manifest.get('version') != BACKUP_VERSION or not isinstance(manifest.get('files'), dict)
            or manifest.get('database') not in {'sqlite', 'postgresql'}):
        raise CliError(f'{source} is not a backup this version of Faxbot can restore.')
    present = {path.relative_to(source).as_posix() for path in source.rglob('*')
               if path.is_file() or path.is_symlink()} - {'manifest.json'}
    listed = set(manifest['files'])
    if present != listed:
        raise CliError('The backup folder does not match its manifest: files were added or removed. It was not '
                       'restored.')
    for name, expected in manifest['files'].items():
        path = source / name
        if path.is_symlink() or not path.is_file() or _digest(path) != expected.get('sha256'):
            raise CliError(f'The backup file {name} does not match its manifest. It was not restored.')
    if 'keys/installation.key' not in listed:
        raise CliError('The backup has no installation key, so it cannot be restored.')
    return manifest


def _postgresql_tables(engine):
    with engine.connect() as connection:
        metadata = sa.MetaData()
        metadata.reflect(connection)
        return metadata


def _self_ordered(table, rows):
    """Rows ordered so a row referring to another row of the same table comes after it."""
    references = [([column.name for column in fk.columns], [element.column.name for element in fk.elements])
                  for fk in table.foreign_key_constraints if fk.referred_table is table]
    if not references:
        return rows
    emitted, ordered, waiting = set(), [], list(rows)
    while waiting:
        progress = []
        for row in waiting:
            ready = True
            for local, remote in references:
                key = tuple(row.get(name) for name in local)
                if any(value is not None for value in key) and (tuple(remote), key) not in emitted:
                    ready = False
            if ready:
                progress.append(row)
        if not progress:
            raise CliError(f'The backup of {table.name} refers to rows it does not contain.')
        for row in progress:
            ordered.append(row)
            for _, remote in references:
                emitted.add((tuple(remote), tuple(row.get(name) for name in remote)))
        progress_ids = {id(row) for row in progress}
        waiting = [row for row in waiting if id(row) not in progress_ids]
    return ordered


def _restore_postgresql(installation, source, manifest, force):
    from ..schema import HEAD, SchemaUpgradeError, upgrade_schema
    if manifest.get('schema_revision') != HEAD:
        raise CliError('This backup was made by a different Faxbot version. Restore it with the matching version, '
                       'then upgrade.')
    engine = engine_for(installation)
    try:
        existing = _postgresql_tables(engine)
        if existing.tables:
            if not force:
                raise CliError('The target database already has tables. Add --force to replace them.', EXIT_CONFLICT)
            with engine.begin() as connection:
                for table in reversed(existing.sorted_tables):
                    connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
        try:
            upgrade_schema(engine)
        except SchemaUpgradeError as error:
            raise CliError(str(error)) from None
        metadata = _postgresql_tables(engine)
        order = json.loads((source / 'database' / 'tables.json').read_text(encoding='utf-8'))['order']
        with engine.begin() as connection:
            names = [name for name in order if name in metadata.tables and name != 'alembic_version']
            if names:
                connection.exec_driver_sql('TRUNCATE TABLE ' + ', '.join(f'"{name}"' for name in names))
            for name in names:
                table = metadata.tables[name]
                path = source / 'database' / 'tables' / f'{name}.jsonl'
                rows = []
                with open(path, encoding='utf-8') as handle:
                    for line in handle:
                        if line.strip():
                            rows.append({key: _decode(value) for key, value in json.loads(line).items()})
                rows = _self_ordered(table, rows)
                for start in range(0, len(rows), 500):
                    connection.execute(table.insert(), rows[start:start + 500])
                for column in table.columns:
                    sequence = connection.execute(sa.text('SELECT pg_get_serial_sequence(:table, :column)'),
                                                  {'table': name, 'column': column.name}).scalar()
                    if sequence:
                        connection.execute(sa.text(
                            f'SELECT setval(:sequence, COALESCE((SELECT MAX("{column.name}") FROM "{name}"), 1), '
                            f'(SELECT MAX("{column.name}") FROM "{name}") IS NOT NULL)'), {'sequence': sequence})
    except sa.exc.SQLAlchemyError:
        raise CliError('Cannot write the restored database. Nothing after this point was restored; check the '
                       'database and run the restore again with --force.', EXIT_FAILURE) from None
    finally:
        engine.dispose()



def _restore_sqlite(installation, source):
    target = installation.sqlite_path
    for suffix in _SQLITE_COMPANIONS[1:]:
        companion = Path(str(target) + suffix)
        if companion.exists():
            companion.unlink()
    temporary = target.with_name(target.name + '.restoring')
    _private_copy(source / 'database' / 'faxbot.sqlite3', temporary)
    os.replace(temporary, target)


def restore(installation, source, *, force=False):
    """Put a verified backup in place. Without force, refuse to replace anything that exists."""
    from ..schema import HEAD
    source = Path(source).resolve()
    if source.is_relative_to(installation.data_dir.resolve()):
        raise CliError('Move the backup folder out of the data folder before restoring it.', EXIT_CONFLICT)
    manifest = verify(source)
    if manifest['database'] != installation.dialect:
        raise CliError(f"This backup holds a {manifest['database']} database, but DATABASE_URL points to "
                       f'{installation.dialect}. Point DATABASE_URL at the right kind of database.')
    keep = _excluded_data(installation)
    conflicts = []
    if any(entry not in keep and entry.name not in LOCK_FILES for entry in installation.data_dir.iterdir()):
        conflicts.append(f'the data folder {installation.data_dir} is not empty')
    if installation.key_path.exists():
        conflicts.append(f'the installation key file {installation.key_path} exists')
    if installation.direct_key_path.exists() and 'keys/direct-identity.key' in manifest['files']:
        conflicts.append(f'the direct delivery key file {installation.direct_key_path} exists')
    if installation.dialect == 'sqlite':
        if installation.sqlite_path is None:
            raise CliError('Restoring needs a SQLite database file path in DATABASE_URL.')
        if installation.sqlite_path.exists() and installation.sqlite_path.stat().st_size > 0:
            conflicts.append(f'the database file {installation.sqlite_path} exists')
    if conflicts and not force:
        raise CliError('Not restored: ' + '; '.join(conflicts) + '. Add --force to replace them.', EXIT_CONFLICT)
    if installation.dialect == 'postgresql':
        _restore_postgresql(installation, source, manifest, force)
    if force:
        for entry in list(installation.data_dir.iterdir()):
            if entry in keep or entry.name in LOCK_FILES:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        for path in (installation.key_path, installation.direct_key_path):
            if path.exists() or path.is_symlink():
                path.unlink()
    restored = 0
    for name in sorted(manifest['files']):
        if name.startswith('data/'):
            _private_copy(source / name, installation.data_dir / Path(name).relative_to('data'))
            restored += 1
    _private_copy(source / 'keys' / 'installation.key', installation.key_path)
    if 'keys/direct-identity.key' in manifest['files']:
        _private_copy(source / 'keys' / 'direct-identity.key', installation.direct_key_path)
    if installation.dialect == 'sqlite':
        _restore_sqlite(installation, source)
    revision = manifest.get('schema_revision')
    return {'database': installation.describe_database(), 'data_files': restored, 'schema_revision': revision,
            'needs_upgrade': revision != HEAD, 'created_at': manifest.get('created_at')}
