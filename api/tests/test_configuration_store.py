"""Configuration storage behavior on real supported databases."""
from datetime import datetime

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database, load_history, seed_history, snapshot
from api.app.schema import upgrade_schema


def test_configuration_migration_preserves_legacy_jobs_without_inventing_bindings(database):
    load_history(database, 'dd8bd991')
    seed_history(database)
    before = snapshot(database)
    upgrade_schema(database)
    after = snapshot(database)
    for table, rows in before.items():
        assert after[table] == rows
    assert after['fax_job_bindings'] == []
    assert after['inbound_fax_bindings'] == []
    assert after['configuration_revisions'] == []
    assert after['provider_profiles'] == []
    assert after['configuration_state'] == []


def test_valid_installed_provider_id_is_not_truncated_by_database(database):
    upgrade_schema(database)
    provider = 'synthetic-provider.v1'
    with database.begin() as connection:
        connection.execute(sa.text('''INSERT INTO fax_jobs
            (id,to_number,file_name,tiff_path,status,backend,outbound_backend,created_at,updated_at)
            VALUES (:id,:to,:name,:path,:status,:backend,:backend,:now,:now)'''),
            {'id': 'long-provider-job', 'to': '+15551230001', 'name': 'test.txt',
             'path': '', 'status': 'queued', 'backend': provider, 'now': datetime.utcnow()})
    assert snapshot(database)['fax_jobs'][0]['backend'] == provider


def upgrade_foundation(database):
    from alembic import command
    from alembic.config import Config
    from api.app.schema import API_DIRECTORY
    config = Config(str(API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(API_DIRECTORY / 'alembic'))
    with database.connect() as connection:
        config.attributes['connection'] = connection
        command.upgrade(config, '0002_schema_foundation')


def test_failed_configuration_migration_restores_column_shapes_and_original_revision(database, monkeypatch):
    from api.app import schema_configuration
    from api.tests.test_schema import schema_description
    upgrade_foundation(database)
    before, definitions = snapshot(database), schema_description(database)
    original = schema_configuration.upgrade_configuration

    def fail_after_widening_and_creating(connection, operations):
        original(connection, operations)
        raise RuntimeError('synthetic migration failure')

    monkeypatch.setattr(schema_configuration, 'upgrade_configuration', fail_after_widening_and_creating)
    with pytest.raises(RuntimeError, match='synthetic migration failure'):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


@pytest.mark.parametrize('damage', ['premature_table', 'missing_table', 'missing_foreign_keys', 'unsafe_delete'])
def test_incompatible_configuration_schema_is_refused_before_mutation(database, damage):
    from api.app.schema import SchemaUpgradeError
    from api.tests.test_schema import schema_description
    if damage == 'premature_table':
        upgrade_foundation(database)
        with database.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE configuration_state (id VARCHAR(40) PRIMARY KEY)')
    else:
        upgrade_schema(database)
        with database.begin() as connection:
            connection.exec_driver_sql('DROP TABLE fax_job_bindings')
            if damage == 'missing_foreign_keys':
                connection.exec_driver_sql('CREATE TABLE fax_job_bindings (id VARCHAR(40) NOT NULL PRIMARY KEY, revision_id VARCHAR(40) NOT NULL, profile_id VARCHAR(40) NOT NULL)')
            elif damage == 'unsafe_delete':
                connection.exec_driver_sql('''CREATE TABLE fax_job_bindings (
                    id VARCHAR(40) NOT NULL PRIMARY KEY REFERENCES fax_jobs(id) ON DELETE CASCADE,
                    revision_id VARCHAR(40) NOT NULL REFERENCES configuration_revisions(id) ON DELETE RESTRICT,
                    profile_id VARCHAR(40) NOT NULL REFERENCES provider_profiles(id) ON DELETE CASCADE)''')
    before, definitions = snapshot(database), schema_description(database)
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
    assert snapshot(database) == before
    assert schema_description(database) == definitions


def test_binding_cannot_reference_missing_profile_or_delete_a_referenced_profile(database):
    upgrade_schema(database)
    now = datetime.utcnow()
    with database.begin() as connection:
        connection.execute(sa.text('''INSERT INTO fax_jobs
            (id,to_number,file_name,tiff_path,status,backend,created_at,updated_at)
            VALUES ('job','+15551230001','test.txt','','queued','phaxio',:now,:now)'''), {'now': now})
        connection.execute(sa.text('''INSERT INTO configuration_revisions
            (id,format_version,key_id,envelope,actor,created_at)
            VALUES ('revision',1,'test-key-id','opaque-test-envelope','test-actor',:now)'''), {'now': now})
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.exec_driver_sql("INSERT INTO fax_job_bindings VALUES ('job','revision','missing-profile')")
    with database.begin() as connection:
        connection.execute(sa.text('''INSERT INTO provider_profiles
            (id,account_id,provider_id,format_version,key_id,envelope,created_at)
            VALUES ('profile','unverified-local-account','phaxio',1,'test-key-id','opaque-test-envelope',:now)'''), {'now': now})
        connection.exec_driver_sql("INSERT INTO fax_job_bindings VALUES ('job','revision','profile')")
    with pytest.raises(sa.exc.IntegrityError):
        with database.begin() as connection:
            connection.exec_driver_sql("DELETE FROM provider_profiles WHERE id='profile'")
    assert snapshot(database)['fax_job_bindings'] == [{'id': 'job', 'revision_id': 'revision', 'profile_id': 'profile'}]


def test_unvalidated_postgres_foreign_key_is_refused(database):
    if database.dialect.name != 'postgresql':
        pytest.skip('PostgreSQL constraint validation metadata')
    from api.app.schema import SchemaUpgradeError
    upgrade_schema(database)
    with database.begin() as connection:
        constraint = next(item for item in sa.inspect(connection).get_foreign_keys('fax_job_bindings')
                          if item['constrained_columns'] == ['profile_id'])
        quoted = connection.dialect.identifier_preparer.quote(constraint['name'])
        connection.exec_driver_sql(f'ALTER TABLE fax_job_bindings DROP CONSTRAINT {quoted}')
        connection.exec_driver_sql(f'''ALTER TABLE fax_job_bindings ADD CONSTRAINT {quoted}
            FOREIGN KEY (profile_id) REFERENCES provider_profiles(id) ON DELETE RESTRICT NOT VALID''')
    with pytest.raises(SchemaUpgradeError):
        upgrade_schema(database)
