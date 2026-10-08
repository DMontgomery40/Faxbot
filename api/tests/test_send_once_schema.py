"""Actual 0046 to 0049 upgrade: send-once agreements, sends and saved bytes (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_send_once
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0049 comes after 0046 (notice faxes and repair).
PRIOR = '0046_notice_repair'


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
            id='peer-1', organization='County Clinic', phone_number='+15550100002',
            endpoint_url='https://clinic.example', signing_key='s' * 43, exchange_key='e' * 43, state='verified',
            challenge_failures=0, version=1, created_at=NOW, updated_at=NOW))


def _agreement(**changes):
    return {'id': 'a' * 32, 'peer_id': 'peer-1', 'role': 'sender', 'state': 'offered',
            'numbers': '["+15550100003"]', 'intake': 'Central intake', 'offer_digest': 'b' * 64, 'version': 1,
            'created_at': NOW, 'updated_at': NOW, **changes}


def _saving(**changes):
    return {'id': 's-1', 'message_id': 'c' * 32, 'peer_id': 'peer-1', 'carriage': 'reference',
            'document_sha256': 'd' * 64, 'full_bytes': 1000, 'sent_bytes': 0, 'created_at': NOW, **changes}


def test_send_once_follows_notice_repair():
    assert schema_send_once.REVISION == '0049_send_once'
    assert schema.NOTICE_REPAIR == PRIOR and schema.HEAD == schema_send_once.REVISION
    assert schema_send_once.TABLES == frozenset({'distribution_agreements', 'distribution_statements',
                                                 'distribution_sends', 'distribution_members',
                                                 'direct_byte_savings'})
    assert schema_send_once.TABLES <= schema.STRICT_TABLES


def test_0049_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
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
    for name in schema_send_once.TABLES:
        assert after[name] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_send_once.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_send_once.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_an_agreement_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    _peer(database)
    agreements = sa.Table('distribution_agreements', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(agreements.insert().values(**_agreement()))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'distribution_agreements' in _tables(database)


def test_roles_states_and_bytes_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    _peer(database)
    agreements = sa.Table('distribution_agreements', sa.MetaData(), autoload_with=database)
    for bad in ({'role': 'both'}, {'state': 'maybe'}, {'withdrawn_by': 'someone'}, {'version': 0}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(agreements.insert().values(**_agreement(**bad)))
    with database.begin() as connection:
        connection.execute(agreements.insert().values(**_agreement()))
    sends = sa.Table('distribution_sends', sa.MetaData(), autoload_with=database)
    members = sa.Table('distribution_members', sa.MetaData(), autoload_with=database)
    send = {'id': 'send-1', 'role': 'sender', 'message_id': 'e' * 32, 'agreement_id': 'a' * 32,
            'peer_id': 'peer-1', 'document_sha256': 'f' * 64, 'size_bytes': 2000, 'routing_statement': '{}',
            'routing_signature': 'x' * 86, 'created_at': NOW}
    with database.begin() as connection:
        connection.execute(sends.insert().values(**send))
        connection.execute(members.insert().values(id='m-1', send_id='send-1', place=0,
                                                   recipient_number='+15550100003', created_at=NOW))
    # One send per message and side, and one member per recipient number of a send.
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(sends.insert().values(**{**send, 'id': 'send-2'}))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(members.insert().values(id='m-2', send_id='send-1', place=1,
                                                       recipient_number='+15550100003', created_at=NOW))
    savings = sa.Table('direct_byte_savings', sa.MetaData(), autoload_with=database)
    for bad in ({'carriage': 'money'}, {'full_bytes': 0}, {'sent_bytes': -1}, {'carriage': 'patch'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(savings.insert().values(**_saving(**bad)))
    with database.begin() as connection:
        connection.execute(savings.insert().values(**_saving(carriage='patch', base_sha256='0' * 64, sent_bytes=40)))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(savings.insert().values(**_saving(id='s-2')))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE direct_byte_savings (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
