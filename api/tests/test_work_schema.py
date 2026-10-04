"""Actual 0010 to 0011 upgrade, frozen work tables, catalogue seed and namespace refusal."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_work
from api.app.access.catalog import BUILTIN_ROLE_PERMISSIONS, PERMISSIONS
from api.app.access.store import AccessStore
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision


NOW = datetime(2026, 10, 3, 12)
WORK = frozenset(schema_work.PERMISSIONS)


def without_work_catalogue(name, rows):
    """Rows as they were before 0011, which only adds work permissions and their role members."""
    if name == 'access_permissions':
        return [row for row in rows if row['id'] not in WORK]
    if name == 'access_role_permissions':
        return [row for row in rows if row['permission_id'] not in WORK]
    return rows


def add_work_catalogue(engine):
    """Give an older access schema today's catalogue, so the running store can open it."""
    from api.app.schema_access import _identity
    with engine.begin() as connection:
        for permission, description in schema_work.PERMISSIONS.items():
            connection.execute(sa.text('INSERT INTO access_permissions (id, description) VALUES (:id, :d)'),
                               {'id': permission, 'd': description})
        for role_id, granted in schema_work.ROLE_PERMISSIONS:
            for permission in granted:
                connection.execute(sa.text('INSERT INTO access_role_permissions (id, role_id, permission_id) '
                                           'VALUES (:id, :role, :permission)'),
                                   {'id': _identity('role_permission', role_id, permission), 'role': role_id,
                                    'permission': permission})


def test_work_items_are_head_after_inbound_imports():
    assert schema.WORK == schema_work.REVISION == '0011_work_items'
    assert schema.INBOUND == '0010_inbound_imports'
    assert schema_work.TABLES <= schema.STRICT_TABLES
    assert WORK <= PERMISSIONS


def _seed(engine):
    inbound = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(inbound.insert().values(
            id='inbound-1', from_number='+15555550100', to_number='+15555550123', status='received',
            backend='phaxio', pages=1, size_bytes=1024, sha256='a' * 64, pdf_path='/data/inbound-1.pdf',
            created_at=NOW, received_at=NOW, updated_at=NOW))


def test_0011_upgrade_preserves_0010_state_seeds_catalogue_and_validates_frozen_shape(database):
    at_revision(database, '0010_inbound_imports')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name in schema_work.TABLES:
        assert after[name] == [], name
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_work_catalogue(name, after[name]) == rows, name
    assert {row['id'] for row in after['access_permissions']} == PERMISSIONS
    members = {}
    for row in after['access_role_permissions']:
        members.setdefault(row['role_id'], set()).add(row['permission_id'])
    assert members == {role: set(permissions) for role, permissions in BUILTIN_ROLE_PERMISSIONS.items()}
    assert after['access_state'] == before['access_state']
    metadata = schema_work.frozen_metadata(dialect=database.dialect.name)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        for name in schema_work.TABLES:
            table = metadata.tables[name]
            assert inspector.get_pk_constraint(name)['name'] == 'pk_' + name
            assert {c['name'] for c in inspector.get_check_constraints(name)} == {
                c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
            assert {f['name'] for f in inspector.get_foreign_keys(name)} == {
                f.name for f in table.foreign_key_constraints}
        indexes = {(i['name'], tuple(i['column_names']), bool(i['unique']))
                   for name in schema_work.TABLES for i in inspector.get_indexes(name)}
        assert indexes == {(name, columns, unique) for name, _, columns, unique in schema_work.INDEXES}
    schema.upgrade_schema(database)
    assert snapshot(database) == after


def test_0009_database_upgrades_to_head_and_the_access_store_starts(database):
    at_revision(database, '0009_sip_call_records')
    with pytest.raises(Exception):
        AccessStore(database)  # The older catalogue no longer matches the running code.
    schema.upgrade_schema(database)
    store = AccessStore(database)
    with store.transaction() as connection:
        permissions = store.tables['access_permissions']
        assert set(connection.execute(sa.select(permissions.c.id)).scalars()) == PERMISSIONS


@pytest.mark.parametrize('conflict', ['table', 'view', 'permission'])
def test_reserved_work_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0010_inbound_imports')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE work_items (private_data VARCHAR(40))')
        elif conflict == 'view':
            connection.exec_driver_sql('CREATE VIEW work_events AS SELECT id FROM access_state')
        else:
            connection.exec_driver_sql("INSERT INTO access_permissions (id, description) "
                                       "VALUES ('work:read', 'planted')")
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_partial_work_ddl_failure_rolls_back_version_tables_and_catalogue(database, monkeypatch):
    at_revision(database, '0010_inbound_imports')
    before = snapshot(database)
    original = schema_work.upgrade_work

    def fail(connection, operations):
        original(connection, operations)
        raise RuntimeError('synthetic interrupted migration')
    monkeypatch.setattr(schema_work, 'upgrade_work', fail)
    with pytest.raises(RuntimeError, match='synthetic interrupted migration'):
        schema.upgrade_schema(database)
    assert snapshot(database) == before
    monkeypatch.setattr(schema_work, 'upgrade_work', original)
    schema.upgrade_schema(database)
    assert snapshot(database)['alembic_version'] == [{'version_num': schema.HEAD}]


def test_work_checks_reject_unknown_states_and_repeat_dedupe_keys_and_follow_retention(database):
    schema.upgrade_schema(database)
    _seed(database)
    metadata = schema_work.frozen_metadata(dialect=database.dialect.name)
    items, events, settings = (metadata.tables[name] for name in schema_work.ORDER)
    item = dict(id='item-1', inbound_fax_id='inbound-1', state='open', available_at=NOW, version=1,
                created_at=NOW, updated_at=NOW)
    with database.begin() as connection:
        connection.execute(items.insert().values(**item))
    for change in ({'state': 'lost'}, {'due_hours': 0}, {'version': 0}, {'due_source': 'policy'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(items.delete().where(items.c.id == 'item-1'))
                connection.execute(items.insert().values(**{**item, **change}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(items.insert().values(**{**item, 'id': 'item-2'}))  # one item per document
    event = dict(work_item_id='item-1', kind='escalated', occurred_at=NOW, details='{}',
                 dedupe_key='escalated:2026-10-03T12:00:00', created_at=NOW)
    with database.begin() as connection:
        connection.execute(events.insert().values(id='event-1', **event))
        connection.execute(events.insert().values(id='event-2', **{**event, 'kind': 'assigned', 'dedupe_key': None}))
        connection.execute(events.insert().values(id='event-3', **{**event, 'kind': 'assigned', 'dedupe_key': None}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(events.insert().values(id='event-4', **event))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(events.insert().values(id='event-5', **{**event, 'kind': 'forgotten', 'dedupe_key': None}))
    mailboxes = metadata.tables['mailboxes']
    with database.begin() as connection:
        connection.execute(mailboxes.insert().values(id='box', label='Front', created_at=NOW, updated_at=NOW))
        connection.execute(settings.insert().values(id='setting-1', mailbox_id='box', acknowledge_hours=0,
                                                    version=1, created_at=NOW, updated_at=NOW))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(settings.update().values(acknowledge_hours=9000))
    inbound = metadata.tables['inbound_faxes']
    with database.begin() as connection:
        connection.execute(inbound.delete().where(inbound.c.id == 'inbound-1'))
    with database.connect() as connection:
        for table in (items, events):
            assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
