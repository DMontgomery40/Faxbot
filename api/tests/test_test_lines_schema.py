"""Actual 0072 to 0075 upgrade: public test lines and the answer cap's calls (SQLite and PostgreSQL)."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_test_lines
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision


NOW = datetime(2026, 10, 10, 9, 0)
PRIOR = '0071_codec_decoder'  # re-chained at merge (was 0072_route_families)


def test_test_lines_follow_the_station_check_and_are_the_only_head():
    assert schema_test_lines.REVISION == '0075_test_lines' == schema.HEAD
    assert schema.CODEC_DECODER == PRIOR
    assert schema_test_lines.TABLES <= schema.STRICT_TABLES and len(schema_test_lines.TABLES) == 3
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option('script_location', str(Path(schema.__file__).resolve().parents[1] / 'alembic'))
    assert ScriptDirectory.from_config(config).get_heads() == [schema.HEAD]


def _job(connection, job_id):
    connection.execute(sa.table('fax_jobs', sa.column('id'), sa.column('to_number'), sa.column('file_name'),
                                sa.column('tiff_path'), sa.column('status'), sa.column('pages'), sa.column('backend'),
                                sa.column('created_at'), sa.column('updated_at')).insert().values(
        id=job_id, to_number='+13035550150', file_name='test.pdf', tiff_path='', status='queued', pages=1,
        backend='sip', created_at=NOW, updated_at=NOW))


def test_0075_adds_empty_tables_downgrades_and_refuses_while_a_row_is_kept(database):  # noqa: F811
    at_revision(database, PRIOR)
    schema.upgrade_schema(database)
    assert schema_test_lines.TABLES <= _tables(database)
    _downgrade(database, PRIOR)
    assert not (schema_test_lines.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    with database.begin() as connection:
        _job(connection, 'j' * 32)
        connection.execute(_table(database, 'answer_cap_calls').insert().values(
            id='a' * 32, job_id='j' * 32, cap_seconds=50, increment_seconds=60, minimum_seconds=60, created_at=NOW))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'answer_cap_calls' in _tables(database)


def test_how_a_reply_was_labelled_and_the_reply_wait_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        _job(connection, 'k' * 32)
        connection.execute(_table(database, 'test_line_sends').insert().values(
            id='s-1', line_id='hp-us', job_id='k' * 32, number='+18884732963', reply_number='+13035550100',
            reply_minutes=20, created_at=NOW))
    send = {'line_id': 'hp-us', 'job_id': 'k' * 32, 'number': '+18884732963', 'created_at': NOW}
    bad = [
        ('test_line_sends', {**send, 'id': 's-2', 'reply_minutes': 0}),
        ('test_line_sends', {**send, 'id': 's-3'}),                        # one test record per fax
        ('test_line_replies', {'id': 'missing-inbound', 'send_id': 's-1', 'how': 'number', 'created_at': NOW}),
        ('answer_cap_calls', {'id': 'b' * 32, 'job_id': 'k' * 32, 'cap_seconds': 0, 'created_at': NOW}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.execute(_table(database, table).insert().values(**row))
