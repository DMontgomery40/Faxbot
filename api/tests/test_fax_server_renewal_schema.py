"""Migration 0078 (N20, N24): call records, renewals and routing of another fax server, SQLite and PostgreSQL."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_fax_server_renewal
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture

NOW = datetime(2026, 10, 10, 9, 0)
PRIOR = '0077_line_inventory'  # re-chained at merge


def test_0078_follows_0077_and_validates(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_fax_server_renewal.REVISION == '0078_fax_server_renewal' == schema.FAX_SERVER_RENEWAL
    assert schema.LINE_INVENTORY == PRIOR
    assert schema_fax_server_renewal.TABLES <= schema.STRICT_TABLES
    assert schema_fax_server_renewal.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


@pytest.mark.parametrize('table, row', [
    ('channel_call_imports', {'id': 'i1', 'system': 'X', 'source_format': 'fax', 'file_sha256': 'a' * 64,
                              'calls': 1, 'created_at': NOW}),
    ('channel_call_imports', {'id': 'i1', 'system': 'X', 'source_format': 'csv', 'file_sha256': 'a' * 64,
                              'calls': 1, 'licensed_channels': 0, 'created_at': NOW}),
    ('channel_calls', {'id': 'c1', 'import_id': 'i1', 'started_at': NOW, 'ended_at': NOW, 'direction': 'sideways'}),
    ('fax_server_renewals', {'id': 'r1', 'system': 'X', 'state': 'maybe', 'created_at': NOW}),
])
def test_impossible_rows_are_refused(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, table).insert().values(**row))


def test_downgrade_refuses_while_a_renewal_is_recorded_and_otherwise_goes_back(database):  # noqa: F811
    schema.upgrade_schema(database)
    _downgrade(database, PRIOR)
    assert not (schema_fax_server_renewal.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'fax_server_renewals').insert().values(
            id='r1', system='RightFax at HQ', amount='26756.71', currency='USD', renews_on=NOW, state='active',
            created_at=NOW))
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='renewals or number routing'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == before
