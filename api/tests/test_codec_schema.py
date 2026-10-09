"""Migration 0031 (experimental fax payload codec): four new tables, SQLite and PostgreSQL."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import sqlalchemy as sa

from api.app import schema, schema_fax_codec
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes

NOW = datetime(2026, 10, 7, 9, 0)
# The migration chain follows merge order: 0031 comes after 0028 (dense pages).
PRIOR = '0028_dense_pages'


def test_codec_follows_dense_pages():
    assert schema_fax_codec.REVISION == '0031_fax_codec'
    assert schema.DENSE_PAGES == PRIOR
    assert schema_fax_codec.TABLES == {'codec_numbers', 'codec_number_changes', 'codec_sends', 'codec_receipts'}


def test_0031_adds_the_codec_tables_keeps_every_row_and_downgrades(database):
    at_revision(database, PRIOR)
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(jobs.insert().values(id='a' * 32, to_number='+15555550123', status='queued', file_name='a.pdf', tiff_path='', backend='sip', pages=23,
                                                created_at=NOW, updated_at=NOW))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            # Each earlier table keeps every row on its original columns; later revisions may add columns.
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    for name in schema_fax_codec.ORDER:
        assert after[name] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    sends = sa.Table('codec_sends', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(sends.insert().values(
            id='a' * 32, phone_number='+15555550123', provider_id='sip', layout='runs', resolution='fine', fec='medium',
            pages_original=23, pages_encoded=1, document_sha256='0' * 64, encrypted=0, format_version=1,
            created_at=NOW))
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with database.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
        assert not set(sa.inspect(connection).get_table_names()) & schema_fax_codec.TABLES
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
