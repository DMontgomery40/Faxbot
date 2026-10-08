"""Actual 0045 to 0043 upgrade: the subaddress of receiving rules and received faxes (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_accounts
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0043 comes after 0045 (partner discovery).
PRIOR = '0045_discovery'


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
    """A number rule with options and a received fax placed by it, as 0030 stores them."""
    metadata = sa.MetaData()
    tables = {name: sa.Table(name, metadata, autoload_with=engine)
              for name in ('inbound_rules', 'inbound_faxes', 'inbound_rule_options', 'inbound_fax_routing')}
    with engine.begin() as connection:
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


def test_accounts_follow_partner_discovery():
    assert schema_accounts.REVISION == '0043_accounts'
    assert schema.DISCOVERY == PRIOR
    assert schema_accounts.TABLES == frozenset()


def test_0043_adds_empty_subaddress_columns_keeps_every_row_and_downgrades(database):  # noqa: F811
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
    # A rule saved before 0043 matches any subaddress, and a fax placed before it stated none: NULL, never a guess.
    assert [row['subaddress'] for row in after['inbound_rule_options']] == [None]
    assert [row['subaddress'] for row in after['inbound_fax_routing']] == [None]
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert 'subaddress' not in _columns(database, 'inbound_rule_options')
    assert 'subaddress' not in _columns(database, 'inbound_fax_routing')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert 'subaddress' in _columns(database, 'inbound_rule_options')


@pytest.mark.parametrize('table', ['inbound_rule_options', 'inbound_fax_routing'])
def test_the_downgrade_refuses_while_a_subaddress_is_recorded(database, table):  # noqa: F811
    schema.upgrade_schema(database)
    _seed(database)
    rows = sa.Table(table, sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(rows.update().values(subaddress='2001'))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'subaddress' in _columns(database, table)
    with database.begin() as connection:
        connection.execute(rows.update().values(subaddress=None))
    _downgrade(database, PRIOR)
    assert 'subaddress' not in _columns(database, table)


def test_a_column_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('ALTER TABLE inbound_fax_routing ADD COLUMN subaddress VARCHAR(20)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
