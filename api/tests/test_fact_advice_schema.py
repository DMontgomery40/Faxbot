"""Actual 0052 to 0065 upgrade: answers about your numbers and number moves (SQLite and PostgreSQL)."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.app import schema, schema_fact_advice
from api.tests.test_digital_schema import _downgrade, _table, _tables
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows the recovered integration order.
PRIOR = '0064_expected_faxes'


def test_fact_advice_follows_expected_faxes():
    assert schema_fact_advice.REVISION == '0065_fact_advice' == schema.FACT_ADVICE
    assert schema.EXPECTED_FAXES == PRIOR
    assert schema_fact_advice.TABLES <= schema.STRICT_TABLES and len(schema_fact_advice.TABLES) == 3


def test_0065_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
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
    for name in schema_fact_advice.ORDER:
        assert after[name] == [], name
    _downgrade(database, PRIOR)
    assert not (schema_fact_advice.TABLES & _tables(database))
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_fact_advice.TABLES <= _tables(database)


@pytest.mark.parametrize('table, row', [
    ('number_dependencies', {'id': 'd-1', 'number': '+15555550123', 'question': 'broadband', 'answer': 'no',
                             'created_at': NOW}),
    ('number_moves', {'id': 'm-1', 'number': '+15555550123', 'from_account': 'sip', 'to_account': 'humblefax',
                      'created_at': NOW}),
])
def test_the_downgrade_refuses_while_an_answer_or_a_move_is_kept(database, table, row):  # noqa: F811
    schema.upgrade_schema(database)
    with database.begin() as connection:
        connection.execute(_table(database, table).insert().values(**row))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert table in _tables(database)


def test_questions_answers_steps_and_states_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    answer = {'number': '+15555550123', 'question': 'broadband', 'answer': 'no', 'created_at': NOW}
    with database.begin() as connection:
        connection.execute(_table(database, 'number_moves').insert().values(
            id='m-1', number='+15555550123', from_account='sip', to_account='humblefax', created_at=NOW))
    event = {'move_id': 'm-1', 'step': 'cutover', 'state': 'done', 'created_at': NOW}
    bad = [
        ('number_dependencies', {**answer, 'id': 'a', 'question': 'colour'}),
        ('number_dependencies', {**answer, 'id': 'a', 'answer': 'maybe'}),
        ('number_move_events', {**event, 'id': 'e', 'step': 'port_now'}),
        ('number_move_events', {**event, 'id': 'e', 'state': 'ported'}),
        ('number_move_events', {**event, 'id': 'e', 'move_id': 'missing'}),
    ]
    for table, row in bad:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                if connection.dialect.name == 'sqlite':
                    connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.execute(_table(database, table).insert().values(**row))
