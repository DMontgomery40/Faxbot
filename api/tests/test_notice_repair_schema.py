"""Actual 0032 to 0046 upgrade: notice faxes, resumable transfers and repaired calls (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_notice_repair
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0046 comes after 0032 (sending rules in delivery).
PRIOR = '0032_rules_delivery'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _peer_columns(engine):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns('direct_peers')}


def _peer(engine):
    peers = sa.Table('direct_peers', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(peers.insert().values(
            id='peer-1', organization='County Clinic', phone_number='+15550100002',
            endpoint_url='https://clinic.example', signing_key='s' * 43, exchange_key='e' * 43, state='verified',
            challenge_failures=0, version=1, created_at=NOW, updated_at=NOW))


def _notice(**changes):
    return {'id': 'n-1', 'role': 'receiver', 'notice_id': '1' * 20, 'message_id': 'a' * 32, 'peer_id': 'peer-1',
            'document_sha256': 'b' * 64, 'state': 'waiting', 'link_statement': '{}', 'link_signature': 'x' * 86,
            'created_at': NOW, 'updated_at': NOW, **changes}


def _transfer(**changes):
    return {'id': 't-1', 'role': 'receiver', 'message_id': 'c' * 32, 'peer_id': 'peer-1', 'manifest': '{}',
            'signature': 'x' * 86, 'offer': '{}', 'offer_signature': 'y' * 86, 'size': 1000, 'piece_size': 400,
            'pieces': 3, 'confirmed': 0, 'state': 'open', 'created_at': NOW, 'updated_at': NOW, **changes}


def test_notice_repair_follows_rules_delivery():
    assert schema_notice_repair.REVISION == '0046_notice_repair'
    assert schema.RULES_DELIVERY == PRIOR
    assert schema_notice_repair.TABLES == frozenset({'direct_notices', 'direct_notice_scans', 'direct_transfers',
                                                     'direct_transfer_pieces', 'direct_call_repairs'})
    assert schema_notice_repair.TABLES <= schema.STRICT_TABLES


def test_0046_adds_empty_tables_and_null_columns_keeps_every_row_and_downgrades(database):  # noqa: F811
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
    for name in schema_notice_repair.TABLES:
        assert after[name] == []
    (peer,) = after['direct_peers']
    assert peer['notice_fax'] is None and peer['certificate_changed_sha256'] is None
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_notice_repair.TABLES & _tables(database)
    assert 'notice_fax' not in _peer_columns(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_notice_repair.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_a_notice_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    _peer(database)
    notices = sa.Table('direct_notices', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(notices.insert().values(**_notice()))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'direct_notices' in _tables(database)


def test_roles_states_and_pieces_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    _peer(database)
    notices = sa.Table('direct_notices', sa.MetaData(), autoload_with=database)
    for bad in ({'role': 'both'}, {'state': 'maybe'}, {'matched_by': 'guess', 'paired_at': NOW},
                {'matched_by': 'sub'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(notices.insert().values(**_notice(**bad)))
    with database.begin() as connection:
        connection.execute(notices.insert().values(**_notice()))
    # One notice per message id and side.
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(notices.insert().values(**_notice(id='n-2')))
    transfers = sa.Table('direct_transfers', sa.MetaData(), autoload_with=database)
    pieces = sa.Table('direct_transfer_pieces', sa.MetaData(), autoload_with=database)
    for bad in ({'state': 'resent'}, {'size': 0}, {'confirmed': -1}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(transfers.insert().values(**_transfer(**bad)))
    with database.begin() as connection:
        connection.execute(transfers.insert().values(**_transfer()))
        connection.execute(pieces.insert().values(id='p-1', transfer_id='t-1', sequence=0, sha256='d' * 64, size=400,
                                                  received_at=NOW))
    # A piece is held once: the same sequence again is a duplicate the database refuses.
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(pieces.insert().values(id='p-2', transfer_id='t-1', sequence=0, sha256='d' * 64, size=400,
                                                      received_at=NOW))
    repairs = sa.Table('direct_call_repairs', sa.MetaData(), autoload_with=database)
    repair = {'id': 'r-1', 'role': 'sender', 'repair_id': 'e' * 32, 'peer_id': 'peer-1', 'total_pages': 10,
              'pages_held': 6, 'state': 'confirmed', 'statement': '{}', 'signature': 'x' * 86, 'created_at': NOW,
              'updated_at': NOW}
    for bad in ({'pages_held': 11}, {'total_pages': 0}, {'state': 'resent'}, {'ecm': 2}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(repairs.insert().values(**{**repair, **bad}))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE direct_transfers (id VARCHAR(40) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
