"""Actual 0014 to 0015 upgrade: the widened inbound source constraint keeps every row as it was."""
from datetime import datetime
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_inbound, schema_inbound_sources, schema_receiving_rules
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 3, 12, 30, 15)
LATER = datetime(2026, 10, 3, 12, 45)
API_DIRECTORY = Path(schema.__file__).resolve().parents[1]
ROOT = API_DIRECTORY.parent


def test_inbound_sources_is_head_after_sending_together():
    assert schema.INBOUND_SOURCES == schema_inbound_sources.REVISION == '0015_inbound_sources'
    assert schema.BATCHING == '0014_send_together'
    assert schema_inbound_sources.TABLES == frozenset()


def test_every_built_in_provider_can_be_recorded_as_a_source():
    traits = json.loads((ROOT / 'config' / 'provider_traits.json').read_text())
    providers = set(traits) - {'_schema'}
    assert set(schema_inbound.SOURCES) <= set(schema_inbound_sources.SOURCES)
    assert providers | {'import', 'test'} == set(schema_inbound_sources.SOURCES)
    assert len(set(schema_inbound_sources.SOURCES)) == len(schema_inbound_sources.SOURCES)
    assert all(len(source) <= 16 for source in schema_inbound_sources.SOURCES)


def _tables(engine):
    metadata = sa.MetaData()
    return (sa.Table('inbound_faxes', metadata, autoload_with=engine),
            sa.Table('inbound_imports', metadata, autoload_with=engine))


def _seed(engine):
    faxes, imports = _tables(engine)
    with engine.begin() as connection:
        for number in (1, 2, 3):
            connection.execute(faxes.insert().values(
                id=f'inbound-{number}', from_number='+15555550100', to_number='+15555550123',
                status='received' if number == 1 else 'waiting', backend='phaxio', provider_sid=f'900{number}',
                created_at=NOW, received_at=NOW, updated_at=NOW))
        connection.execute(imports.insert().values(
            id='import-1', source='phaxio', account='phaxio:0123456789ab', operation_id='9001', revision='',
            state='received', attempts=2, next_attempt_at=None, claim_token=None, claim_expires_at=None,
            imported_at=NOW, source_received_at=datetime(2026, 10, 3, 12, 29, 59), acquired_at=LATER,
            to_number='+15555550123', from_number='+15555550100', reported_pages=3,
            report=json.dumps({'fax': {'id': 9001, 'note': 'synthetic é'}}, sort_keys=True),
            artifact_digest='b' * 64, artifact_size=4096, artifact_media_type='application/pdf',
            inbound_fax_id='inbound-1', last_error=None, created_at=NOW, updated_at=LATER))
        connection.execute(imports.insert().values(
            id='import-2', source='sinch', account='sinch:project-1', operation_id='op-2', revision='r2',
            state='pending', attempts=1, next_attempt_at=LATER, claim_token='c' * 32, claim_expires_at=LATER,
            imported_at=NOW, inbound_fax_id='inbound-2', last_error='Faxbot could not reach Sinch.',
            created_at=NOW, updated_at=NOW))
        connection.execute(imports.insert().values(
            id='import-3', source='sip', account='sip:asterisk', operation_id='call-3', revision='',
            state='failed', attempts=30, imported_at=NOW, inbound_fax_id='inbound-3', created_at=NOW,
            updated_at=NOW))


def _insert(engine, source, number):
    faxes, imports = _tables(engine)
    with engine.begin() as connection:
        connection.execute(faxes.insert().values(id=f'new-{number}', status='waiting', backend=source[:20],
                                                 created_at=NOW, received_at=NOW, updated_at=NOW))
        connection.execute(imports.insert().values(
            id=f'new-import-{number}', source=source, account=source + ':synthetic', operation_id=f'op-{number}',
            revision='', state='pending', attempts=0, imported_at=NOW, inbound_fax_id=f'new-{number}',
            created_at=NOW, updated_at=NOW))


def test_0015_upgrade_keeps_every_row_and_validates_the_frozen_shape(database):
    at_revision(database, '0014_send_together')
    _seed(database)
    with pytest.raises(sa.exc.IntegrityError):
        _insert(database, 'efax', 0)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    metadata = schema_inbound_sources.frozen_metadata(dialect=database.dialect.name)
    table = metadata.tables['inbound_imports']
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint('inbound_imports')['name'] == 'pk_inbound_imports'
        checks = {c['name']: c['sqltext'] for c in inspector.get_check_constraints('inbound_imports')}
        assert set(checks) == {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        assert all(f"'{source}'" in checks['ck_inbound_imports_source'] for source in schema_inbound_sources.SOURCES)
        assert {f['name'] for f in inspector.get_foreign_keys('inbound_imports')} == {'fk_inbound_imports_inbound_fax'}
        # 0030 adds an index on the receiving account.
        assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                for i in inspector.get_indexes('inbound_imports')} == {
            (name, columns, unique) for name, columns, unique in schema_inbound.INDEXES} | {
            (name, columns, unique) for name, table, columns, unique in schema_receiving_rules.ADDED_INDEXES
            if table == 'inbound_imports'}
        assert '_inbound_imports_0015' not in inspector.get_table_names()
    schema.upgrade_schema(database)
    assert snapshot(database) == after


def test_widened_constraint_accepts_every_built_in_source_and_still_refuses_others(database):
    schema.upgrade_schema(database)
    for number, source in enumerate(schema_inbound_sources.SOURCES):
        _insert(database, source, number)
    for bad in ('EFAX', 'efax ', 'twilio', ''):
        with pytest.raises(sa.exc.IntegrityError):
            _insert(database, bad, 99)
    _, imports = _tables(database)
    with database.connect() as connection:
        assert sorted(connection.execute(sa.select(imports.c.source)).scalars()) == sorted(
            schema_inbound_sources.SOURCES)


def test_removing_a_fax_still_removes_its_import_after_the_rebuild(database):
    at_revision(database, '0014_send_together')
    _seed(database)
    schema.upgrade_schema(database)
    faxes, imports = _tables(database)
    with database.begin() as connection:
        connection.execute(faxes.delete().where(faxes.c.id == 'inbound-2'))
    with database.connect() as connection:
        assert sorted(connection.execute(sa.select(imports.c.id)).scalars()) == ['import-1', 'import-3']


def test_downgrade_is_refused_and_deletes_nothing(database):
    at_revision(database, '0014_send_together')
    _seed(database)
    schema.upgrade_schema(database)
    _insert(database, 'efax', 7)
    before = snapshot(database)
    config = Config(str(API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(API_DIRECTORY / 'alembic'))
    with database.connect() as connection:
        config.attributes['connection'] = connection
        with pytest.raises(RuntimeError, match='Destructive schema downgrade is not supported'):
            command.downgrade(config, '0014_send_together')
    assert snapshot(database) == before


def test_interrupted_0015_rolls_back_the_rebuild_and_the_version(database, monkeypatch):
    at_revision(database, '0014_send_together')
    _seed(database)
    before = snapshot(database)
    original = schema_inbound_sources.upgrade_sources

    def fail(connection, operations):
        original(connection, operations)
        raise RuntimeError('synthetic interrupted migration')
    monkeypatch.setattr(schema_inbound_sources, 'upgrade_sources', fail)
    with pytest.raises(RuntimeError, match='synthetic interrupted migration'):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
    with pytest.raises(sa.exc.IntegrityError):
        _insert(database, 'efax', 0)
    monkeypatch.setattr(schema_inbound_sources, 'upgrade_sources', original)
    schema.upgrade_schema(database)
    assert snapshot(database)['alembic_version'] == [{'version_num': schema.HEAD}]


def test_a_leftover_rebuild_table_refuses_without_changes(database):
    at_revision(database, '0014_send_together')
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE _inbound_imports_0015 (private_data VARCHAR(40))')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
