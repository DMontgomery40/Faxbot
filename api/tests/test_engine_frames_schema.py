"""Actual 0035 to 0036 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0036 adds what the far end's fax machine said on each built-in engine call and the fax servers approved for
Internet Aware Fax, and changes no stored row.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from api.app import schema, schema_engine_frames
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 12, 0)


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def test_engine_frames_are_head_after_screening():
    assert schema.HEAD == schema_engine_frames.REVISION == '0036_engine_frames'
    assert schema.SCREENING == '0035_junk_screening'
    assert schema_engine_frames.TABLES == frozenset({'fax_call_frames', 'fax_iaf_endpoints'})
    assert schema_engine_frames.TABLES <= schema.STRICT_TABLES


def _refused(connection, statement):
    try:
        with connection.begin_nested():
            connection.execute(sa.text(statement), {'now': NOW})
    except sa.exc.IntegrityError:
        return True
    return False


def test_0036_adds_empty_tables_keeps_every_row_and_downgrades(database):
    at_revision(database, '0035_junk_screening')
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO screened_callers (id, number, reason, created_at, expires_at) "
            "VALUES ('entry-1', '+13035550142', 'Junk', :now, :now)"), {'now': NOW})
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    for name, rows in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    assert after['fax_call_frames'] == [] and after['fax_iaf_endpoints'] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    with database.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO fax_call_frames (id, direction, attempt_id, number, dis, created_at) "
            "VALUES ('out:attempt-1', 'out', 'attempt-1', '+13035550150', 'ff138000eef5c4808001', :now)"), {'now': NOW})
        assert _refused(connection, "INSERT INTO fax_call_frames (id, direction, created_at) "
                                    "VALUES ('x', 'sideways', :now)")
        assert _refused(connection, "INSERT INTO fax_iaf_endpoints (id, number, kind, label, created_at) "
                                    "VALUES ('y', '+13035550150', 'carrier', 'Telnyx', :now)")
    _downgrade(database, '0035_junk_screening')
    assert not schema_engine_frames.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0035_junk_screening'
    schema.upgrade_schema(database)
    assert schema_engine_frames.TABLES <= _tables(database)
