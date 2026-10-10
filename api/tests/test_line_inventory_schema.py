"""Migration 0077 (N19): the line inventory, carrier lists and line notice kinds, SQLite and PostgreSQL."""
from datetime import date, datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_line_inventory
from api.app.routing import closures
from api.tests.test_access_schema import at_revision
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture

NOW = datetime(2026, 10, 10, 9, 0)
PRIOR = '0071_codec_decoder'  # re-chained at merge


def test_0077_follows_0071_and_validates(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_line_inventory.REVISION == '0077_line_inventory' == schema.LINE_INVENTORY
    assert schema.CODEC_DECODER == PRIOR
    assert schema_line_inventory.TABLES <= schema.STRICT_TABLES and schema_line_inventory.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_a_letter_written_before_0077_still_reads_as_a_letter(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(_table(database, 'line_notices').insert().values(
            id='n' * 32, number='+14165550100', carrier='Bell', closes_on=datetime(2026, 11, 4), state='active',
            created_at=NOW))
    before = snapshot(database)['line_notices']
    schema.upgrade_schema(database)
    after = snapshot(database)['line_notices']
    assert [{key: row[key] for key in before[0]} for row in after] == before
    assert after[0]['kind'] is None
    assert closures.notices(database)['+14165550100']['kind'] == 'letter'


@pytest.mark.parametrize('changes', [{'line_use': 'pager'}])
def test_an_unknown_line_use_is_refused(database, changes):  # noqa: F811
    schema.upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, 'line_inventory').insert().values(
                id='l1', import_id='i1', number='+13035550110', created_at=NOW, **changes))


def test_an_unknown_list_kind_is_refused(database):  # noqa: F811
    schema.upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, 'carrier_service_areas').insert().values(
                id='a1', import_id='i1', carrier='att', kind='rumoured', wire_center='ZZTSILAA', created_at=NOW))


def test_downgrade_refuses_while_a_contract_end_is_recorded_and_otherwise_goes_back(database):  # noqa: F811
    schema.upgrade_schema(database)
    _downgrade(database, PRIOR)
    assert not (schema_line_inventory.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
        columns = {column['name'] for column in sa.inspect(connection).get_columns('line_notices')}
    assert not {'kind', 'source_label', 'source_url'} & columns
    schema.upgrade_schema(database)
    closures.record_notice(database, '+13035550110', closes_on=date(2026, 12, 31), kind='contract_end',
                           source_label='Line inventory (lines.csv)')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='Contract end dates'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == before
