"""Actual 0068 to 0073 upgrade: the header notice (SQLite and PostgreSQL)."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_header_notice
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 10, 9, 0)
# Chained after the integration head when merged (0068_dialing_guard); the lead re-chains it if another lands first.
PRIOR = '0068_dialing_guard'


def test_header_notice_follows_the_dialing_guard_and_is_the_only_head():
    assert schema_header_notice.REVISION == '0073_header_notice' == schema.HEADER_NOTICE
    assert schema.DIALING == PRIOR
    assert schema_header_notice.TABLES <= schema.STRICT_TABLES and len(schema_header_notice.TABLES) == 3
    from pathlib import Path
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    config = Config()
    config.set_main_option('script_location', str(Path(schema.__file__).resolve().parents[1] / 'alembic'))
    assert ScriptDirectory.from_config(config).get_heads() == [schema.HEAD]


def test_0073_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(_table(database, 'fax_jobs').insert().values(
            id='job-1', to_number='+15555550123', file_name='note.txt', tiff_path='', status='queued', pages=1,
            backend='sip', created_at=NOW, updated_at=NOW))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_header_notice.ORDER:
        assert after[name] == [], name
    _downgrade(database, PRIOR)
    assert not (schema_header_notice.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_header_notice.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_a_notice_is_kept(database):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'header_notices').insert().values(
            id='n-1', scope='organization', notice='Confidential.', created_at=NOW))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert 'header_notices' in _tables(database)


def test_scopes_covers_pages_and_flags_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'fax_jobs').insert().values(
            id='job-1', to_number='+15555550123', file_name='note.txt', tiff_path='', status='queued', pages=2,
            backend='sip', created_at=NOW, updated_at=NOW))
    fax = {'id': 'job-1', 'notice': 'Confidential.', 'scope': 'organization', 'cover': 'dropped',
           'original_pages': 3, 'sent_pages': 2, 'created_at': NOW}
    bad = [
        ('header_notices', {'id': 'a', 'scope': 'team', 'notice': 'x', 'created_at': NOW}),
        ('header_notices', {'id': 'a', 'scope': 'mailbox', 'mailbox_id': None, 'notice': 'x', 'created_at': NOW}),
        ('header_notices', {'id': 'a', 'scope': 'organization', 'mailbox_id': 'm-1', 'notice': 'x', 'created_at': NOW}),
        ('fax_header_notices', {**fax, 'cover': 'thrown_away'}),
        ('fax_header_notices', {**fax, 'sent_pages': 4}),
        ('fax_header_notices', {**fax, 'id': 'missing-job'}),
        ('recipient_cover_changes', {'id': 'c', 'phone_number': '+15555550123', 'needs_cover': 2, 'created_at': NOW}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.execute(_table(database, table).insert().values(**row))
