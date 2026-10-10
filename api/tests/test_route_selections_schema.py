"""Migration 0067: the route selection record is additive, validated, and survives an attempted rollback."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401

PRIOR = '0066_analysis'


def _row(**changes):
    return {'id': 'selection-1', 'job_id': 'a' * 32, 'attempt_id': 'b' * 32, 'account_key': 'sip-pages',
            'provider_id': 'sip', 'number': '+12025550123', 'rendering': 'as_is', 'layout': 'dense', 'coding': 'MMR',
            'original_pages': 2, 'sent_pages': 1, 'expected_micros': 4000, 'currency': 'USD', 'measured': 1,
            'compared': 2, 'runner_key': 'sip', 'runner_layout': 'normal', 'runner_pages': 2, 'runner_micros': 5000,
            'runner_currency': 'USD', 'tariff_sha256': 'c' * 64, 'candidates': '[]',
            'created_at': datetime(2026, 10, 9, 12), **changes}


def test_the_head_is_the_route_selection_revision_and_validates(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema.HEAD == '0067_route_selections'
    assert 'fax_route_selections' in _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


@pytest.mark.parametrize('changes', [{'layout': 'tall'}, {'rendering': 'blurred'}, {'sent_pages': 0},
                                     {'expected_micros': -1}, {'measured': 2}, {'plan_unit': 'hours'}])
def test_impossible_rows_are_refused(database, changes):  # noqa: F811
    schema.upgrade_schema(database)
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(_table(database, 'fax_route_selections').insert().values(**_row(**changes)))


def test_one_row_per_attempt(database):  # noqa: F811
    schema.upgrade_schema(database)
    table = _table(database, 'fax_route_selections')
    with database.begin() as connection:
        connection.execute(table.insert().values(**_row()))
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.execute(table.insert().values(**_row(id='selection-2')))


def test_downgrade_keeps_recorded_selections(database):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'fax_route_selections').insert().values(**_row()))
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='route records'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == before


def test_an_empty_table_downgrades_and_upgrades(database):  # noqa: F811
    schema.upgrade_schema(database)
    _downgrade(database, PRIOR)
    assert 'fax_route_selections' not in _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
