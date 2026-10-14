"""Dashboard delivery counts through temporary databases, never HTTP routes."""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.schema import upgrade_schema
from api.tests.test_schema import database


NOW = datetime(2026, 10, 3, 12, 0, 0)


@pytest.fixture
def tables(database):
    upgrade_schema(database)
    metadata = sa.MetaData()
    metadata.reflect(database)
    return metadata.tables


def seed(connection, tables, state, *, updated_at=NOW, legacy_status='queued'):
    identity = uuid4().hex
    connection.execute(tables['fax_jobs'].insert().values(
        id=identity, to_number='+12025550123', file_name='synthetic.txt',
        tiff_path='', status=legacy_status, backend='phaxio',
        created_at=NOW - timedelta(days=3), updated_at=NOW))
    if state is not None:
        connection.execute(tables['outbound_deliveries'].insert().values(
            id=identity, dispatch_mode='held' if state == 'held' else 'normal',
            state=state, version=1, created_at=NOW - timedelta(days=3),
            updated_at=updated_at))


def counts(connection, tables):
    from api.app.outbound_summary import dashboard_counts
    return dashboard_counts(connection, tables['outbound_deliveries'], now=NOW)


def test_empty_installation_has_zero_integer_counts(database, tables):
    with database.connect() as connection:
        result = counts(connection, tables)
    assert result == {'queued': 0, 'in_progress': 0, 'recent_failures': 0,
                      'held': 0, 'reconciliation_required': 0}
    assert all(type(value) is int for value in result.values())


def test_durable_states_override_legacy_queued_projection(database, tables):
    with database.begin() as connection:
        for state in ['ready', 'ready', 'preparing', 'submitting', 'in_progress',
                      'in_progress', 'held', 'held', 'reconciliation_required',
                      'reconciliation_required', 'reconciliation_required',
                      'reconciliation_required', 'success', 'cancelled']:
            seed(connection, tables, state)
        seed(connection, tables, 'failed', updated_at=NOW - timedelta(hours=2))
        seed(connection, tables, 'failed', updated_at=NOW - timedelta(days=3))
        # An unprojected legacy row also cannot manufacture an eligible queue.
        seed(connection, tables, None)
        result = counts(connection, tables)
        legacy_queue = connection.scalar(sa.select(sa.func.count()).select_from(
            tables['fax_jobs']).where(tables['fax_jobs'].c.status == 'queued'))
    assert legacy_queue == 17
    assert result == {'queued': 3, 'in_progress': 3, 'recent_failures': 1,
                      'held': 2, 'reconciliation_required': 4}


def test_failure_window_uses_terminal_delivery_update_in_last_24_hours(database, tables):
    with database.begin() as connection:
        for updated_at in [NOW, NOW - timedelta(hours=24),
                           NOW - timedelta(hours=24, microseconds=1),
                           NOW + timedelta(microseconds=1)]:
            seed(connection, tables, 'failed', updated_at=updated_at,
                 legacy_status='success')
        seed(connection, tables, 'success', legacy_status='failed')
        seed(connection, tables, 'reconciliation_required', legacy_status='failed')
        result = counts(connection, tables)
    assert result == {'queued': 0, 'in_progress': 0, 'recent_failures': 2,
                      'held': 0, 'reconciliation_required': 1}


def _item(connection, tables, job, *, state='open', at=NOW):
    """A certainty item on a new attempt of ``job``, open or settled as can't tell."""
    attempt = uuid4().hex
    connection.execute(tables['outbound_attempts'].insert().values(
        id=attempt, job_id=job, sequence=connection.scalar(sa.select(sa.func.count()).select_from(
            tables['outbound_attempts']).where(tables['outbound_attempts'].c.job_id == job)) + 1,
        phase='uncertain', created_at=at, submitted_at=at))
    connection.execute(tables['certainty_items'].insert().values(
        id=uuid4().hex, job_id=job, attempt_id=attempt, category='transport_ambiguous',
        reference=uuid4().hex[:8].upper(), state=state, outcome='unknown' if state == 'settled' else None,
        settled_at=at if state == 'settled' else None, version=1, created_at=at, updated_at=at))


def test_uncertain_results_count_as_sents_settling_list_has_them(database, tables):
    """With certainty items, an uncertain result counts while a person still has it to settle (the board's rule)."""
    from api.app.outbound_summary import certainty_items, dashboard_counts
    with database.begin() as connection:
        for state in ('reconciliation_required', 'reconciliation_required', 'failed', 'failed'):
            seed(connection, tables, state)
        jobs = connection.execute(sa.select(tables['outbound_deliveries'].c.id, tables['outbound_deliveries'].c.state)
                                  .order_by(tables['outbound_deliveries'].c.state)).all()
        settled, unfiled = [job for job, state in jobs if state == 'reconciliation_required']
        failed_open, failed_reopened = [job for job, state in jobs if state == 'failed']
        _item(connection, tables, settled, state='settled')           # can't tell, settled before it moved the fax
        _item(connection, tables, failed_open)                        # open on a failed delivery
        _item(connection, tables, failed_reopened, state='settled', at=NOW - timedelta(hours=1))
        _item(connection, tables, failed_reopened, at=NOW)            # its latest item is open again
        items = certainty_items(connection)
        result = dashboard_counts(connection, tables['outbound_deliveries'], now=NOW, items=items)
        # unfiled: uncertain, and the certainty worker has not filed an item yet.
        assert result['reconciliation_required'] == 3
        assert dashboard_counts(connection, tables['outbound_deliveries'], now=NOW, items=items,
                                job_ids=[settled, failed_open])['reconciliation_required'] == 1
        # Without the table, the delivery state as before.
        assert counts(connection, tables)['reconciliation_required'] == 2
    assert unfiled
