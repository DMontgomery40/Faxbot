"""Actual 0031 to 0034 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0034 adds nullable peer fax columns to ``direct_peers`` and ``direct_deliveries``
and touches no received-fax table.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_peer_fax
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0034 comes after 0031 (encoded pages).
BEFORE = '0031_fax_codec'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def _seed(engine):
    peers, deliveries = _table(engine, 'direct_peers'), _table(engine, 'direct_deliveries')
    with engine.begin() as connection:
        connection.execute(peers.insert().values(
            id='peer-1', organization='Valley Hospital', phone_number='+15550100001',
            endpoint_url='https://valley.example', signing_key='s' * 43, exchange_key='x' * 43, state='verified',
            challenge_failures=0, version=1, created_at=NOW, updated_at=NOW))
        connection.execute(deliveries.insert().values(
            id='delivery-1', direction='inbound', message_id='a' * 32, peer_id='peer-1', recipient_number='+15550100002',
            digest='b' * 64, size_bytes=10, manifest='{}', state='accepted', created_at=NOW, updated_at=NOW))


def test_peer_fax_follows_fax_codec():
    assert schema_peer_fax.REVISION == '0034_peer_fax'
    assert schema.FAX_CODEC == BEFORE and schema_peer_fax.TABLES == frozenset()


def test_0034_upgrade_keeps_every_row_adds_null_columns_and_validates(database):
    at_revision(database, BEFORE)
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name not in ('alembic_version', 'direct_peers', 'direct_deliveries'):
            # Each earlier table keeps every row on its original columns; later revisions may add columns.
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    (peer,) = after['direct_peers']
    assert {name: peer[name] for name, _ in schema_peer_fax.PEER_COLUMNS} == dict.fromkeys(
        (name for name, _ in schema_peer_fax.PEER_COLUMNS), None)
    assert {key: peer[key] for key in before['direct_peers'][0]} == before['direct_peers'][0]
    (delivery,) = after['direct_deliveries']
    assert delivery['kind'] is None
    assert {key: delivery[key] for key in before['direct_deliveries'][0]} == before['direct_deliveries'][0]
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    # The received-fax import table is untouched: still the 0020 constraint, never rebuilt.
    with database.connect() as connection:
        checks = {c['name']: c['sqltext'] for c in sa.inspect(connection).get_check_constraints('inbound_imports')}
    assert "'direct'" not in checks['ck_inbound_imports_source'] and "'local'" in checks['ck_inbound_imports_source']


def test_0034_downgrade_drops_the_columns_and_is_refused_while_fax_images_are_recorded(database):
    at_revision(database, BEFORE)
    _seed(database)
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(sa.text("UPDATE direct_peers SET receive_fax_images = 1, partner_said_at = :now"),
                           {'now': NOW})
    _downgrade(database, BEFORE)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == BEFORE
        names = {c['name'] for c in sa.inspect(connection).get_columns('direct_peers')}
        assert not names & set(dict(schema_peer_fax.PEER_COLUMNS))
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(sa.text("UPDATE direct_deliveries SET kind = 'fax_image'"))
    with pytest.raises(Exception, match='fax images'):
        _downgrade(database, BEFORE)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        assert connection.execute(sa.text("SELECT kind FROM direct_deliveries")).scalar() == 'fax_image'
