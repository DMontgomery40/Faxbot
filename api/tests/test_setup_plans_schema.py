"""Actual 0049 to 0053 upgrade: setup plans and their applications (SQLite and PostgreSQL)."""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_setup_plans
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 8, 9, 0)
# The migration chain follows merge order: 0053 comes after 0049 (send once and reuse).
PRIOR = '0049_send_once'


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _plan(**changes):
    return {'id': 'p' * 32, 'number': 1, 'basis': 'b' * 64, 'context': '{}', 'plan': '{}', 'digest': 'd' * 64,
            'created_at': NOW, **changes}


def test_setup_plans_follow_send_once():
    assert schema_setup_plans.REVISION == '0053_setup_plans'
    assert schema.SEND_ONCE == PRIOR and schema.HEAD == schema_setup_plans.REVISION
    assert schema_setup_plans.TABLES == frozenset({'setup_plans', 'setup_plan_applications'})
    assert schema_setup_plans.TABLES <= schema.STRICT_TABLES


def test_0053_adds_empty_tables_keeps_every_row_and_downgrades(database):  # noqa: F811
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows in before.items():
        if name != 'alembic_version':
            rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert after['setup_plans'] == [] and after['setup_plan_applications'] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, PRIOR)
    assert not schema_setup_plans.TABLES & _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert schema_setup_plans.TABLES <= _tables(database)


def test_the_downgrade_refuses_while_a_plan_is_recorded(database):  # noqa: F811
    schema.upgrade_schema(database)
    plans = sa.Table('setup_plans', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(plans.insert().values(**_plan()))
    with pytest.raises(Exception):
        _downgrade(database, PRIOR)
    assert schema_setup_plans.TABLES <= _tables(database)


def test_numbers_outcomes_and_the_restart_flag_are_checked_by_the_database(database):  # noqa: F811
    schema.upgrade_schema(database)
    metadata = sa.MetaData()
    plans = sa.Table('setup_plans', metadata, autoload_with=database)
    applications = sa.Table('setup_plan_applications', metadata, autoload_with=database)
    with database.begin() as connection:
        connection.execute(plans.insert().values(**_plan()))
    for bad in (_plan(id='q' * 32, number=0), _plan(id='r' * 32)):  # number 0, then a number already used
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(plans.insert().values(**bad))
    good = {'id': 'a' * 32, 'plan_id': 'p' * 32, 'outcome': 'applied', 'items': '[]', 'steps': '[]',
            'restart_required': 0, 'created_at': NOW}
    for bad in ({'outcome': 'done'}, {'restart_required': 2}, {'plan_id': 'x' * 32}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(applications.insert().values(**{**good, **bad}))
    with database.begin() as connection:
        connection.execute(applications.insert().values(**good))


def test_a_table_left_from_elsewhere_stops_the_upgrade(database):  # noqa: F811
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('CREATE TABLE setup_plans (id VARCHAR(32) PRIMARY KEY)'))
    with pytest.raises(Exception):
        schema.upgrade_schema(database)
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalar() == PRIOR
