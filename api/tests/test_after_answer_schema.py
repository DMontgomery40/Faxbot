"""Actual 0071 to 0076 upgrade: digits after answer (SQLite and PostgreSQL)."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_after_answer
from api.tests.test_access_schema import at_revision
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database  # noqa: F401 - fixture


NOW = datetime(2026, 10, 10, 9, 0)
PRIOR = '0075_test_lines'  # re-chained at merge (was 0071_codec_decoder)


def test_0076_follows_the_codec_decoder_and_is_the_only_head():
    assert schema_after_answer.REVISION == '0076_after_answer' == schema.HEAD
    assert schema.TEST_LINES == PRIOR
    assert schema_after_answer.TABLES <= schema.STRICT_TABLES and len(schema_after_answer.TABLES) == 2
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option('script_location', str(Path(schema.__file__).resolve().parents[1] / 'alembic'))
    assert ScriptDirectory.from_config(config).get_heads() == [schema.HEAD]


def test_0076_adds_empty_tables_downgrades_and_refuses_while_a_row_is_kept(database):  # noqa: F811
    at_revision(database, PRIOR)
    schema.upgrade_schema(database)
    assert schema_after_answer.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not (schema_after_answer.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'recipient_after_answer').insert().values(
            id='k-1', phone_number='+13035550150', digits='2w105', created_at=NOW))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'recipient_after_answer' in _tables(database)


def test_engines_and_the_fax_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    call = {'id': 'c-1', 'job_id': 'missing-job', 'phone_number': '+13035550150', 'digits': '2', 'engine': 'builtin',
            'created_at': NOW}
    for row in (call, {**call, 'id': 'c-2', 'engine': 'cloud'}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.execute(_table(database, 'after_answer_calls').insert().values(**row))
