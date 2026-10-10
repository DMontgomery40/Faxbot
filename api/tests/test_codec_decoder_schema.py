"""Migration 0071 (brief 85 M2): the recipient's decoder on encoded-page settings, SQLite and PostgreSQL."""
from datetime import datetime

import sqlalchemy as sa

from api.app import schema, schema_codec_decoder
from api.tests.test_access_schema import at_revision
from api.tests.test_digital_schema import _downgrade, _table
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture

NOW = datetime(2026, 10, 10, 9, 0)
PRIOR = '0072_route_families'  # re-chained at merge (was 0070_closures)


def test_0071_follows_the_closures_and_adds_two_nullable_columns():
    assert schema_codec_decoder.REVISION == '0071_codec_decoder' == schema.HEAD
    assert schema.ROUTE_FAMILIES == PRIOR
    assert schema_codec_decoder.TABLES == frozenset()


def test_0071_keeps_every_setting_reads_old_rows_as_any_decoder_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(_table(database, 'codec_numbers').insert().values(
            id='n' * 32, phone_number='+15555550123', enabled=1, style='dense', fec='medium', version=1,
            created_at=NOW, updated_at=NOW))
        connection.execute(_table(database, 'codec_number_changes').insert().values(
            id='c' * 32, phone_number='+15555550123', action='on', actor='principal:synthetic', recipient_agreed=1,
            style='dense', fec='medium', created_at=NOW))
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert [(row['phone_number'], row['decoder']) for row in after['codec_numbers']] == [('+15555550123', None)]
    assert [row['decoder'] for row in after['codec_number_changes']] == [None]
    from api.app.codec.store import CodecSettings
    assert CodecSettings(database).get('+15555550123')['decoder'] == 'any'
    _downgrade(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
        columns = {column['name'] for column in sa.inspect(connection).get_columns('codec_numbers')}
    assert 'decoder' not in columns
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
