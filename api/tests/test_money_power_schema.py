"""Migration 0081 (brief 99): exchange rates, Data Connect and power-hold records, append-only and validated."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_money_power
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401 (fixture)

NOW = datetime(2026, 10, 10, 12)


def test_0081_is_the_head_follows_0079_and_validates(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_money_power.REVISION == '0081_money_power' == schema.HEAD
    assert schema.POTS_QUOTE == '0079_pots_quote'
    assert schema_money_power.TABLES <= schema.STRICT_TABLES and schema_money_power.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


@pytest.mark.parametrize('table, row', [
    ('exchange_rates', dict(id='r1', base='EUR', quote='USD', rate='1.1', source='bank', state='active',
                            created_at=NOW)),
    ('exchange_rates', dict(id='r2', base='EUR', quote='USD', rate='1.1', source='ecb', state='gone',
                            created_at=NOW)),
    ('exchange_rate_fetches', dict(id='f1', source='ecb', attempted_at=NOW, outcome='maybe')),
    ('exchange_rate_settings', dict(id='s1', reference_enabled=2, created_at=NOW)),
    ('destination_data_connect', dict(id='d1', destination='+81312345678', capable=1, source='guess',
                                      created_at=NOW)),
    ('data_connect_trunks', dict(id='t1', account_key='sip', enabled=5, created_at=NOW)),
])
def test_impossible_rows_are_refused(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, table).insert().values(**row))


def test_a_rate_keeps_its_date_and_the_downgrade_refuses_while_rows_exist(database):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'exchange_rates').insert().values(
            id='r1', base='EUR', quote='USD', rate='1.1612', rate_date=datetime(2026, 10, 9), source='ecb',
            state='active', created_at=NOW))
    with database.connect() as connection:
        stored = connection.execute(sa.text('SELECT rate, rate_date FROM exchange_rates')).one()
    assert stored[0] == '1.1612' and str(stored[1])[:10] == '2026-10-09'
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='cannot be undone'):
        _downgrade(database, '0079_pots_quote')
    assert snapshot(database) == before
    with database.begin() as connection:
        connection.execute(sa.text('DELETE FROM exchange_rates'))
    _downgrade(database, '0079_pots_quote')
    assert not (schema_money_power.TABLES & _tables(database))
    schema.upgrade_schema(database)
    assert schema_money_power.TABLES <= _tables(database)
