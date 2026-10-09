"""Saved analysis and pending requests survive attempted schema rollback."""
from datetime import datetime

import pytest

from api.app import schema
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401

PRIOR = '0062_polled_transmit'


@pytest.mark.parametrize('state', ['queued', 'running', 'succeeded'])
def test_downgrade_preserves_pending_or_saved_analysis(database, state):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'analysis_state').insert().values(id='installation', state=state))
        if state == 'succeeded':
            connection.execute(_table(database, 'analysis_runs').insert().values(
                id='saved', started_at=datetime(2026, 10, 8), provider='openai', model='synthetic-model',
                config_signature='synthetic', summary='Saved evidence-backed report', evidence='[]', usage='{}'))
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='analysis'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == before


def test_empty_analysis_scheduler_can_downgrade_and_upgrade(database):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, 'analysis_state').insert().values(id='installation', state='idle'))
    _downgrade(database, PRIOR)
    assert not {'analysis_runs', 'analysis_state'} & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
