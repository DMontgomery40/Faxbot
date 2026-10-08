"""Actual 0032 to 0058 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0058 adds the nullable ``fax_friendly_pages.method`` (screened or whitened) and changes no stored row: a row written
before it keeps NULL, which reads as whitened (all Faxbot did then). The downgrade refuses while any row records a
method, because that is how an attempt's pages were sent.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_shading_method
from api.tests.test_access_schema import at_revision
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_work_schema import without_later_access_changes
from app.pages import friendly

NOW = datetime(2026, 10, 8, 12, 0)
# The migration chain follows merge order: 0058 comes after 0032 (rules in delivery).
PRIOR = '0032_rules_delivery'
JOB, ATTEMPT = 'a' * 32, 'b' * 32


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _columns(engine):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns('fax_friendly_pages')}


def _old_row(engine):
    """A row as AR's build wrote it before 0058: its pages were made white."""
    with engine.begin() as connection:
        connection.execute(sa.text(
            "INSERT INTO fax_friendly_pages (id, job_id, attempt_id, scope, pages, pages_changed, bits_before, "
            "bits_after, seconds_saved, created_at) VALUES ('old-row', :job, :attempt, 'documents', 1, 1, 873336, "
            "167000, 49, :now)"), {'job': JOB, 'attempt': ATTEMPT, 'now': NOW})


def test_0058_follows_rules_in_delivery_and_is_the_head():
    assert schema_shading_method.REVISION == '0058_shading_method' == schema.HEAD
    assert schema.RULES_DELIVERY == PRIOR
    assert schema_shading_method.METHODS == friendly.METHODS


def test_0058_keeps_every_row_adds_the_method_and_downgrades(database):
    at_revision(database, PRIOR)
    _old_row(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    after = snapshot(database)
    for name, rows in before.items():
        if name == 'alembic_version':
            continue
        rows, kept = without_later_access_changes(name, rows), without_later_access_changes(name, after[name])
        assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert 'method' in _columns(database)
    old = friendly.run_for(database, JOB)
    assert old['method'] is None and friendly.method_of(old) == 'whitened'
    assert friendly.sent_sentence(old).startswith('Light areas on the page were made white')
    # With no row recording a method the revision undoes cleanly, and every earlier row is as it was.
    _downgrade(database, PRIOR)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    assert 'method' not in _columns(database)
    restored = snapshot(database)
    assert {name: rows for name, rows in restored.items() if name != 'alembic_version'} == \
        {name: rows for name, rows in before.items() if name != 'alembic_version'}
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_the_downgrade_refuses_while_an_attempt_records_its_method(database):
    schema.upgrade_schema(database)
    request = friendly.Request('documents')
    request.result = friendly.Result(1, 1, 873_336, 386_000, 'screened')
    friendly.record_send(database, job_id=JOB, attempt_id=ATTEMPT, request=request, now=NOW)
    stored = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError, match='how their shaded areas were sent'):
        _downgrade(database, PRIOR)
    assert snapshot(database) == stored


def test_only_screened_or_whitened_is_recorded(database):
    schema.upgrade_schema(database)
    request = friendly.Request('documents')
    request.result = friendly.Result(1, 1, 873_336, 386_000, 'erased')
    with pytest.raises(ValueError):
        friendly.record_send(database, job_id=JOB, attempt_id=ATTEMPT, request=request, now=NOW)


def test_the_upgrade_refuses_a_table_that_already_has_the_column(database):
    at_revision(database, PRIOR)
    with database.begin() as connection:
        connection.execute(sa.text('ALTER TABLE fax_friendly_pages ADD COLUMN method VARCHAR(16)'))
    # The schema check refuses the unexpected column before the migration runs; nothing is adopted.
    with pytest.raises(schema.SchemaUpgradeError, match='unexpected columns in fax_friendly_pages'):
        schema.upgrade_schema(database)
