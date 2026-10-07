"""Actual 0023 to 0029 to 0030 upgrade and downgrade with existing rows (SQLite and PostgreSQL).

0029 adds sending rules per scope (revisions, heads, drafts), each fax's routing
decisions, each attempt's account choice and holds. 0030 adds the account a fax
came in on, the trunk of each call, receiving rule options and how each received
fax was placed. Both downgrades refuse while their records hold anything.
"""
from datetime import datetime

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_receiving_rules, schema_routing_rules
from api.app.rules import model
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes


NOW = datetime(2026, 10, 7, 9, 0)
NEW_COLUMNS = {'inbound_imports': 'account_key', 'sip_call_records': 'trunk_key'}


def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def _columns(engine, table):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns(table)}


def _tables(engine):
    with engine.connect() as connection:
        return set(sa.inspect(connection).get_table_names())


def _seed(engine):
    """Rows every table touched by 0029 and 0030 already had: sent and received faxes, rules, calls."""
    jobs, attempts = _table(engine, 'fax_jobs'), _table(engine, 'outbound_attempts')
    faxes, imports = _table(engine, 'inbound_faxes'), _table(engine, 'inbound_imports')
    calls, rules = _table(engine, 'sip_call_records'), _table(engine, 'inbound_rules')
    connectors = _table(engine, 'intake_connectors')
    with engine.begin() as connection:
        connection.execute(jobs.insert().values(id='job-1', to_number='+15555550123', file_name='note.pdf',
                                                tiff_path='', status='queued', pages=2, backend='sip',
                                                created_at=NOW, updated_at=NOW))
        connection.execute(attempts.insert().values(id='attempt-1', job_id='job-1', sequence=1, phase='created',
                                                    created_at=NOW))
        connection.execute(faxes.insert().values(id='inbound-1', from_number='+15555550100',
                                                 to_number='+15555550123', status='received', backend='sinch',
                                                 created_at=NOW, received_at=NOW, updated_at=NOW))
        connection.execute(imports.insert().values(
            id='import-1', source='sinch', account='sinch:project-1', operation_id='fax-1', revision='',
            state='received', attempts=1, imported_at=NOW, acquired_at=NOW, artifact_digest='b' * 64,
            artifact_size=10, artifact_media_type='application/pdf', inbound_fax_id='inbound-1', created_at=NOW,
            updated_at=NOW))
        connection.execute(calls.insert().values(
            id='call-1', direction='inbound', call_id='call-1', started_at=NOW, ended_at=NOW,
            disposition='answered', t38='yes', fax_preference=0, created_at=NOW, updated_at=NOW))
        connection.execute(rules.insert().values(id='rule-1', to_number='+15555550123', mailbox_label='Front desk',
                                                 created_at=NOW))
        connection.execute(connectors.insert().values(id='connector-1', kind='email', name='Front desk email',
                                                      enabled=1, settings='{}', version=1, created_at=NOW,
                                                      updated_at=NOW))


def _without_new_columns(name, rows):
    column = NEW_COLUMNS.get(name)
    return [{key: value for key, value in row.items() if key != column} for row in rows]


def _write_rules_records(engine):
    """One revision, head, draft, decision, choice and hold, and the 0030 records, through plain SQL."""
    with engine.begin() as connection:
        insert = lambda table, **values: connection.execute(_table(engine, table).insert().values(**values))  # noqa: E731
        insert('routing_rule_revisions', id='rev-1', scope_kind='organization', scope_id='', number=1,
               format_version=1, document='{"format":1}', digest='d' * 64, note='First rules',
               actor_principal_id='person-1', actor_name='Jane Smith', created_at=NOW)
        insert('routing_rule_revisions', id='rev-2', scope_kind='mailbox', scope_id='mailbox-1', number=1,
               parent_id=None, format_version=1, document='{"format":1}', digest='e' * 64, created_at=NOW)
        insert('routing_rule_state', id='state-1', scope_kind='organization', scope_id='', active_revision_id='rev-1',
               generation=1, updated_at=NOW)
        insert('routing_rule_drafts', id='draft-1', scope_kind='organization', scope_id='', base_revision_id='rev-1',
               document='{"format":1}', version=1, created_at=NOW, updated_at=NOW)
        insert('fax_job_rule_decisions', id='decision-1', job_id='job-1', sequence=1,
               revisions='{"organization":"rev-1"}', facts='{}', facts_digest='f' * 64, decision='{}',
               page_layout='as_receiver_allows', outcome='held', created_at=NOW)
        insert('delivery_rule_choices', id='attempt-1', job_id='job-1', decision_id='decision-1',
               scope_kind='organization', rule_id='r-uk', account_key='sinch-uk', mode='ordered', place=0,
               created_at=NOW)
        insert('outbound_holds', id='hold-1', job_id='job-1', kind='approval', decision_id='decision-1',
               rule_id='l-big', state='open', separate_approver=1, requested_at=NOW, version=1, updated_at=NOW)
        insert('inbound_rule_options', id='rule-1', place=0, enabled=1, any_number=0, account_key='sinch-uk',
               days='mon,tue,wed,thu,fri', start_minute=480, end_minute=1080, email_connector_id='connector-1',
               email_off=0, urgent=1, keep_days=30, version=1, created_at=NOW, updated_at=NOW)
        insert('inbound_fax_routing', id='inbound-1', rule_id='rule-1', rule_version=1, rule_snapshot='{}',
               account_key='sinch-uk', mailbox_id='mailbox-1', urgent=1, keep_days=30,
               received_time_source='provider', created_at=NOW)
        connection.execute(sa.text("UPDATE inbound_imports SET account_key = 'sinch-uk'"))
        connection.execute(sa.text("UPDATE sip_call_records SET trunk_key = 'sip'"))


def test_rules_follow_negotiation():
    assert schema.NEGOTIATION == '0023_negotiation'
    assert schema.ROUTING_RULES == schema_routing_rules.REVISION == '0029_routing_rules'
    assert schema.HEAD == schema_receiving_rules.REVISION == '0030_receiving_accounts'
    assert schema_routing_rules.TABLES == frozenset({
        'routing_rule_revisions', 'routing_rule_state', 'routing_rule_drafts', 'fax_job_rule_decisions',
        'delivery_rule_choices', 'outbound_holds'})
    assert schema_receiving_rules.TABLES == frozenset({'inbound_rule_options', 'inbound_fax_routing'})
    assert (schema_routing_rules.TABLES | schema_receiving_rules.TABLES) <= schema.STRICT_TABLES


def test_frozen_literals_equal_the_rules_model():
    """The frozen schema never imports runtime code, so its CHECK literals are compared here."""
    assert schema_routing_rules.SCOPE_KINDS == model.SCOPE_KINDS
    assert schema_routing_rules.OUTCOMES == model.OUTCOMES
    assert schema_routing_rules.MODES == model.MODES
    assert schema_routing_rules.PAGE_LAYOUTS == model.PAGE_LAYOUTS
    assert schema_routing_rules.HOLD_KINDS == model.HOLD_KINDS
    assert schema_routing_rules.HOLD_STATES == model.HOLD_STATES


def test_0029_alone_is_a_valid_schema(database):
    at_revision(database, '0029_routing_rules')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0029_routing_rules'
    assert schema_routing_rules.TABLES <= _tables(database)
    assert not schema_receiving_rules.TABLES & _tables(database)
    assert 'account_key' not in _columns(database, 'inbound_imports')
    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_0029_and_0030_keep_every_row_add_empty_columns_and_downgrade(database):
    at_revision(database, '0027_dialed_number')
    _seed(database)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    for name, rows in before.items():
        if name == 'alembic_version':
            continue
        assert without_later_access_changes(name, _without_new_columns(name, after[name])) == \
            without_later_access_changes(name, rows), name
    # Faxes received and calls made before 0030 gain an empty column: not recorded, never guessed.
    assert [row['account_key'] for row in after['inbound_imports']] == [None]
    assert [row['trunk_key'] for row in after['sip_call_records']] == [None]
    for name in schema_routing_rules.TABLES | schema_receiving_rules.TABLES:
        assert after[name] == [], name

    # Empty, both revisions undo cleanly and the earlier rows read exactly as before.
    _downgrade(database, '0027_dialed_number')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0027_dialed_number'
    assert not (schema_routing_rules.TABLES | schema_receiving_rules.TABLES) & _tables(database)
    assert 'account_key' not in _columns(database, 'inbound_imports')
    assert 'trunk_key' not in _columns(database, 'sip_call_records')
    restored = snapshot(database)
    assert {name: rows for name, rows in restored.items() if name != 'alembic_version'} == \
        {name: rows for name, rows in before.items() if name != 'alembic_version'}

    schema.upgrade_schema(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD


def test_downgrades_refuse_while_rules_or_receiving_records_exist(database):
    at_revision(database, '0027_dialed_number')
    _seed(database)
    schema.upgrade_schema(database)
    _write_rules_records(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    stored = snapshot(database)

    with pytest.raises(schema.SchemaUpgradeError, match='Receiving accounts or receiving rules are recorded'):
        _downgrade(database, '0029_routing_rules')
    assert snapshot(database) == stored

    # With the receiving records gone, 0030 undoes; 0029 still refuses while decisions are stored.
    with database.begin() as connection:
        for name in ('inbound_fax_routing', 'inbound_rule_options'):
            connection.execute(sa.text(f'DELETE FROM {name}'))
        connection.execute(sa.text('UPDATE inbound_imports SET account_key = NULL'))
        connection.execute(sa.text('UPDATE sip_call_records SET trunk_key = NULL'))
    _downgrade(database, '0029_routing_rules')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0029_routing_rules'
    with pytest.raises(schema.SchemaUpgradeError, match='Sending rules or routing decisions are recorded'):
        _downgrade(database, '0027_dialed_number')
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0029_routing_rules'
    schema.upgrade_schema(database)


def test_cleaning_up_a_sent_fax_takes_its_decisions_choices_and_holds(database):
    """Job cleanup deletes fax_jobs; everything 0029 keeps under a fax goes with it."""
    at_revision(database, '0027_dialed_number')
    _seed(database)
    schema.upgrade_schema(database)
    _write_rules_records(database)
    with database.begin() as connection:
        connection.execute(sa.text("DELETE FROM fax_jobs WHERE id = 'job-1'"))
        connection.execute(sa.text("DELETE FROM inbound_faxes WHERE id = 'inbound-1'"))
        connection.execute(sa.text("DELETE FROM inbound_rules WHERE id = 'rule-1'"))
    after = snapshot(database)
    for name in ('fax_job_rule_decisions', 'delivery_rule_choices', 'outbound_holds', 'outbound_attempts',
                 'inbound_fax_routing', 'inbound_rule_options'):
        assert after[name] == [], name
    # Revisions are not under a fax; they stay.
    assert [row['id'] for row in after['routing_rule_revisions']] == ['rev-1', 'rev-2']


def test_constraints_refuse_what_the_model_cannot_say(database):
    at_revision(database, '0027_dialed_number')
    _seed(database)
    schema.upgrade_schema(database)
    _write_rules_records(database)
    refused = [
        "INSERT INTO routing_rule_revisions (id, scope_kind, scope_id, number, format_version, document, digest, "
        "created_at) VALUES ('rev-x', 'organization', '', 1, 1, '{}', 'x', :now)",  # number 1 again in one scope
        "INSERT INTO routing_rule_revisions (id, scope_kind, scope_id, number, format_version, document, digest, "
        "created_at) VALUES ('rev-x', 'team', '', 1, 1, '{}', 'x', :now)",
        "INSERT INTO routing_rule_revisions (id, scope_kind, scope_id, number, format_version, document, digest, "
        "created_at) VALUES ('rev-x', 'workflow', 'referrals', 0, 1, '{}', 'x', :now)",
        "INSERT INTO routing_rule_state (id, scope_kind, scope_id, generation, updated_at) "
        "VALUES ('state-x', 'organization', '', 0, :now)",  # a second head for one scope
        "INSERT INTO fax_job_rule_decisions (id, job_id, sequence, revisions, facts, facts_digest, decision, outcome, "
        "created_at) VALUES ('decision-x', 'job-1', 1, '{}', '{}', 'f', '{}', 'route', :now)",  # sequence 1 again
        "INSERT INTO fax_job_rule_decisions (id, job_id, sequence, revisions, facts, facts_digest, decision, outcome, "
        "created_at) VALUES ('decision-x', 'job-1', 2, '{}', '{}', 'f', '{}', 'maybe', :now)",
        "INSERT INTO fax_job_rule_decisions (id, job_id, sequence, revisions, facts, facts_digest, decision, outcome, "
        "page_layout, created_at) VALUES ('decision-x', 'job-1', 2, '{}', '{}', 'f', '{}', 'route', 'tiny', :now)",
        "INSERT INTO outbound_holds (id, job_id, kind, state, separate_approver, requested_at, version, updated_at) "
        "VALUES ('hold-x', 'job-1', 'someday', 'open', 0, :now, 1, :now)",
        "INSERT INTO outbound_holds (id, job_id, kind, state, separate_approver, requested_at, version, updated_at) "
        "VALUES ('hold-x', 'job-1', 'window', 'forgotten', 0, :now, 1, :now)",
        "INSERT INTO delivery_rule_choices (id, job_id, account_key, mode, place, created_at) "
        "VALUES ('attempt-x', 'job-1', 'sip', 'ordered', 0, :now)",  # no such attempt
        "UPDATE inbound_rule_options SET start_minute = 1440",
        "UPDATE inbound_rule_options SET keep_days = 0",
        "UPDATE inbound_fax_routing SET received_time_source = 'guess'",
    ]
    for statement in refused:
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(sa.text(statement), {'now': NOW})
