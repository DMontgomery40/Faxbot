"""Actual 0040 to 0044 upgrade: partner relay agreements, signed statements and relayed faxes (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_partner_relay
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0044 comes after 0040 (recipient schedules) at this branch's base.
PRIOR = '0040_destination_schedule'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _peer(engine):
    peers = sa.Table('direct_peers', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(peers.insert().values(
            id='peer-1', organization='Sydney office', phone_number='+61255501234',
            endpoint_url='https://sydney.example', signing_key='s' * 43, exchange_key='e' * 43, state='verified',
            challenge_failures=0, version=1, created_at=NOW, updated_at=NOW))


def _agreement(**changes):
    return {'id': 'a' * 32, 'peer_id': 'peer-1', 'role': 'sender', 'state': 'offered', 'terms': '{}',
            'send_together': 0, 'version': 1, 'created_at': NOW, 'updated_at': NOW, **changes}


def test_relay_follows_recipient_schedules():
    assert schema_partner_relay.REVISION == '0044_partner_relay'
    assert schema.HEAD == schema_partner_relay.REVISION
    assert schema.SCHEDULE == PRIOR
    assert schema_partner_relay.TABLES == frozenset({'relay_agreements', 'relay_statements', 'relay_faxes'})
    assert schema_partner_relay.TABLES <= schema.STRICT_TABLES


def test_0044_adds_three_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    _peer(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_partner_relay.TABLES:
        assert after[name] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_partner_relay.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_partner_relay.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_an_agreement_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    _peer(database)
    agreements = sa.Table('relay_agreements', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(agreements.insert().values(**_agreement()))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'relay_agreements' in _tables(database)


def test_roles_states_and_flags_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    _peer(database)
    agreements = sa.Table('relay_agreements', sa.MetaData(), autoload_with=database)
    for bad in ({'role': 'both'}, {'state': 'maybe'}, {'send_together': 2}, {'withdrawn_by': 'someone'},
                {'version': 0}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(agreements.insert().values(**_agreement(**bad)))
    faxes = sa.Table('relay_faxes', sa.MetaData(), autoload_with=database)
    row = {'id': 'f-1', 'role': 'relay', 'message_id': 'b' * 32, 'agreement_id': 'a' * 32, 'peer_id': 'peer-1',
           'job_id': 'c' * 32, 'destination': '+61255509999', 'state': 'accepted', 'shared': 0, 'created_at': NOW,
           'updated_at': NOW}
    with database.begin() as connection:
        connection.execute(faxes.insert().values(**row))
    # One relayed fax per message id and side.
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(faxes.insert().values(**{**row, 'id': 'f-2'}))
    for bad in ({'state': 'resent'}, {'charge_basis': 'guess'}, {'shared': 3}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(faxes.insert().values(**{**row, 'id': 'f-3', 'message_id': 'd' * 32, **bad}))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE relay_faxes (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
