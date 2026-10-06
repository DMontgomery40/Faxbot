"""Actual 0019 to 0020 upgrade: a ``local`` received-fax source and a per-fax call request (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_inbound, schema_inbound_sources, schema_local_delivery
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 6, 9, 0)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    metadata = sa.MetaData()
    return (sa.Table('fax_jobs', metadata, autoload_with=engine),
            sa.Table('inbound_faxes', metadata, autoload_with=engine),
            sa.Table('inbound_imports', metadata, autoload_with=engine))


def _seed(engine):
    jobs, faxes, imports = _tables(engine)
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='note.txt',
                                                tiff_path='', status='SUCCESS', pages=2, backend='phaxio',
                                                created_at=NOW, updated_at=NOW))
        connection.execute(faxes.insert().values(id='inbound-1', from_number='+15555550100', to_number='+15555550123',
                                                 status='received', backend='efax', created_at=NOW, received_at=NOW,
                                                 updated_at=NOW))
        connection.execute(imports.insert().values(
            id='import-1', source='efax', account='efax:0123456789ab', operation_id='9001', revision='',
            state='received', attempts=1, imported_at=NOW, acquired_at=NOW, artifact_digest='b' * 64, artifact_size=10,
            artifact_media_type='application/pdf', inbound_fax_id='inbound-1', created_at=NOW, updated_at=NOW))


def _insert_local(engine, number):
    _, faxes, imports = _tables(engine)
    with engine.begin() as connection:
        connection.execute(faxes.insert().values(id=f'local-{number}', status='waiting', backend='local',
                                                 created_at=NOW, received_at=NOW, updated_at=NOW))
        connection.execute(imports.insert().values(
            id=f'local-import-{number}', source='local', account='local:installation', operation_id=f'job-{number}',
            revision='', state='pending', attempts=0, imported_at=NOW, inbound_fax_id=f'local-{number}',
            created_at=NOW, updated_at=NOW))


def test_local_delivery_is_head_after_retired_permissions():
    assert schema.HEAD == schema_local_delivery.REVISION == '0020_local_delivery'
    assert schema.RETIRED == '0019_retired_permissions'
    assert schema_local_delivery.TABLES == frozenset()
    assert schema_local_delivery.SOURCES == schema_inbound_sources.SOURCES + ('local',)


def test_0020_upgrade_keeps_every_row_adds_the_column_and_validates_the_frozen_shape(database):
    at_revision(database, '0019_retired_permissions')
    _seed(database)
    with pytest.raises(sa.exc.IntegrityError):
        _insert_local(database, 0)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    assert [row['send_by_call'] for row in after['fax_jobs']] == [None]
    table = schema_local_delivery.frozen_metadata(dialect=database.dialect.name).tables['inbound_imports']
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        checks = {c['name']: c['sqltext'] for c in inspector.get_check_constraints('inbound_imports')}
        assert set(checks) == {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        assert all(f"'{source}'" in checks['ck_inbound_imports_source'] for source in schema_local_delivery.SOURCES)
        assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                for i in inspector.get_indexes('inbound_imports')} == {
            (name, columns, unique) for name, columns, unique in schema_inbound.INDEXES}
        assert '_inbound_imports_0020' not in inspector.get_table_names()
    _insert_local(database, 1)
    schema.upgrade_schema(database)
    assert without_later_access_changes('fax_jobs', snapshot(database)['fax_jobs']) == without_later_access_changes(
        'fax_jobs', after['fax_jobs'])


def test_0020_downgrade_removes_the_column_and_source_and_is_refused_while_local_faxes_exist(database):
    at_revision(database, '0019_retired_permissions')
    _seed(database)
    schema.upgrade_schema(database)
    _downgrade(database, '0019_retired_permissions')
    with database.connect() as connection:
        assert 'send_by_call' not in {c['name'] for c in sa.inspect(connection).get_columns('fax_jobs')}
        assert schema.validate_schema(connection, require_version=True) == '0019_retired_permissions'
    with pytest.raises(sa.exc.IntegrityError):
        _insert_local(database, 2)
    schema.upgrade_schema(database)
    _insert_local(database, 3)
    with pytest.raises(Exception, match='delivered inside Faxbot'):
        _downgrade(database, '0019_retired_permissions')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        assert connection.execute(sa.text("SELECT count(*) FROM inbound_imports WHERE source = 'local'")).scalar() == 1
