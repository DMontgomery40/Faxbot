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


def test_codec_is_head_after_negotiation():
    assert schema.HEAD == schema_fax_codec.REVISION == '0031_fax_codec'
    assert schema.NEGOTIATION == '0023_negotiation'
    assert schema_fax_codec.TABLES == {'codec_numbers', 'codec_number_changes', 'codec_sends', 'codec_receipts'}


def test_0031_adds_the_codec_tables_keeps_every_row_and_downgrades(database):
    at_revision(database, '0023_negotiation')
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
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
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
        command.downgrade(config, '0023_negotiation')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0023_negotiation'
        assert not set(sa.inspect(connection).get_table_names()) & schema_fax_codec.TABLES
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
