"""Actual 0052 to 0054 upgrade: the subaddress a call asked for, peer calls and diverted calls (SQLite and
PostgreSQL). 0054 adds nullable columns only, keeps every row, and refuses to downgrade while one holds a value."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_engine_extras
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0054 comes after 0052 (digital routes).
PRIOR = '0052_digital_routes'
COLUMNS = {table: [column for name, column, _ in schema_engine_extras.ADDED_COLUMNS if name == table]
           for table in ('sip_call_records', 'inbound_rule_options', 'inbound_fax_routing')}


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def _seed(engine):
    """A sent call, a number rule with options and a received fax it placed, as 0033 stores them."""
    metadata = sa.MetaData()
    tables = {name: sa.Table(name, metadata, autoload_with=engine)
              for name in ('sip_call_records', 'inbound_rules', 'inbound_faxes', 'inbound_rule_options',
                           'inbound_fax_routing')}
    with engine.begin() as connection:
        connection.execute(tables['sip_call_records'].insert().values(
            id='call-1', direction='outbound', call_id='attempt-1', job_id='job-1', attempt_id='attempt-1',
            started_at=NOW, disposition='answered', t38='yes', fax_preference=0, created_at=NOW, updated_at=NOW))
        connection.execute(tables['inbound_rules'].insert().values(id='rule-1', to_number='+15555550123',
                                                                   mailbox_label='Front desk', created_at=NOW))
        connection.execute(tables['inbound_faxes'].insert().values(id='fax-1', status='received', backend='sip',
                                                                   created_at=NOW, received_at=NOW, updated_at=NOW))
        connection.execute(tables['inbound_rule_options'].insert().values(
            id='rule-1', place=0, enabled=1, any_number=0, email_off=0, urgent=1, version=1, created_at=NOW,
            updated_at=NOW))
        connection.execute(tables['inbound_fax_routing'].insert().values(
            id='fax-1', rule_id='rule-1', rule_version=1, rule_snapshot='{}', urgent=1, created_at=NOW))
    return tables


def test_engine_extras_follow_digital_routes():
    assert schema_engine_extras.REVISION == '0054_engine_extras'
    assert schema.DIGITAL_ROUTES == PRIOR
    assert schema_engine_extras.TABLES == frozenset()


def test_0054_adds_empty_columns_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    # Rows from before 0054 asked for no subaddress, went over a carrier and were never forwarded: NULL, no guess.
    for table, columns in COLUMNS.items():
        for column in columns:
            assert [row[column] for row in after[table]] == [None], (table, column)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    for table, columns in COLUMNS.items():
        assert not set(columns) & _columns(database, table), table
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert 'diverted_from' in _columns(database, 'inbound_rule_options')


@pytest.mark.parametrize('table, column, value', [
    ('sip_call_records', 'subaddress', '4021'), ('sip_call_records', 'peer_id', 'a' * 32),
    ('inbound_rule_options', 'diverted_from', '+15555550199'), ('inbound_rule_options', 'diversion_unsigned', 1),
    ('inbound_fax_routing', 'diverted_from', '+15555550199'), ('inbound_fax_routing', 'diversion', 'signed'),
])
def test_the_downgrade_refuses_while_a_value_is_recorded(database, table, column, value):  # noqa: F811
    schema.upgrade_schema(database)
    _seed(database)
    rows = sa.Table(table, sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(rows.update().values(**{column: value}))
    with pytest.raises(Exception, match='cannot be undone'):
        _downgrade(database, PRIOR)
    assert column in _columns(database, table)
    with database.begin() as connection:
        connection.execute(rows.update().values(**{column: None}))
    _downgrade(database, PRIOR)
    assert column not in _columns(database, table)


def test_a_column_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('ALTER TABLE sip_call_records ADD COLUMN peer_id VARCHAR(64)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
