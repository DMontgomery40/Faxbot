"""0009 SIP call records: frozen schema, AMI-event capture and the read interface."""
import asyncio
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa

from api.app import schema, schema_sip
# The fake AMI peer fixtures bind settings through the ``app`` package.
from app import sip_calls
from app.config import use_configuration
from app.config_values import ConfigurationValues
from api.tests.test_schema import database, snapshot
from api.tests.test_access_schema import at_revision


JOB = '0123456789abcdef0123456789abcdef'
ATTEMPT = 'fedcba9876543210fedcba9876543210'
NOW = datetime(2026, 10, 3, 12, 0, 0)


def test_sip_call_records_are_head_after_delivery_routes():
    assert schema.SIP == schema_sip.REVISION == '0009_sip_call_records'
    assert schema.DELIVERY == '0008_delivery_routes'
    assert schema_sip.TABLES <= schema.STRICT_TABLES


def test_0009_upgrade_preserves_0008_state_and_validates_frozen_shape(database):
    at_revision(database, '0008_delivery_routes')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    assert after['sip_call_records'] == []
    for name, rows in before.items():
        if name != 'alembic_version':
            assert after[name] == rows, name
    metadata = schema_sip.frozen_metadata(dialect=database.dialect.name)
    table = metadata.tables['sip_call_records']
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint('sip_call_records')['name'] == 'pk_sip_call_records'
        assert {c['name'] for c in inspector.get_check_constraints('sip_call_records')} == {
            c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        assert {(i['name'], tuple(i['column_names']), bool(i['unique']))
                for i in inspector.get_indexes('sip_call_records')} == {
            (name, columns, unique) for name, columns, unique in schema_sip.INDEXES}
        columns = {c['name']: c for c in inspector.get_columns('sip_call_records')}
        assert set(columns) == {column.name for column in table.columns}
        assert not columns['disposition']['nullable'] and columns['answered_at']['nullable']
    schema.upgrade_schema(database)
    assert snapshot(database) == after


@pytest.mark.parametrize('conflict', ['table', 'view'])
def test_reserved_sip_namespace_refuses_without_changes(database, conflict):
    at_revision(database, '0008_delivery_routes')
    with database.begin() as connection:
        if conflict == 'table':
            connection.exec_driver_sql('CREATE TABLE sip_call_records (private_data VARCHAR(40))')
        else:
            connection.exec_driver_sql('CREATE VIEW sip_call_records AS SELECT id FROM access_state')
    before = snapshot(database)
    with pytest.raises(schema.SchemaUpgradeError):
        schema.upgrade_schema(database)
    assert snapshot(database) == before


def test_database_refuses_an_unknown_disposition_or_media_value(database):
    schema.upgrade_schema(database)
    row = {'id': 'a' * 32, 'direction': 'outbound', 'call_id': ATTEMPT, 'started_at': NOW,
           'disposition': 'answered', 't38': 'yes', 'fax_preference': 0, 'created_at': NOW, 'updated_at': NOW}
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=database)
    for change in ({'disposition': 'delivered'}, {'t38': 'maybe'}, {'direction': 'sideways'}, {'pages': -1}):
        with pytest.raises(sa.exc.IntegrityError):
            with database.begin() as connection:
                connection.execute(table.insert().values(**{**row, **change}))


@pytest.fixture
def records(database):
    schema.upgrade_schema(database)
    return sip_calls.SipCallRecords(database)


def submission(**extra):
    return {'JobID': JOB, 'AttemptID': ATTEMPT, 'Called': '+15555550123', 'CallerID': '+15555550100',
            'Preset': 'telnyx', 'FaxPreference': 'yes', **extra}


def fax_result(**extra):
    return {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'JobID': JOB, 'AttemptID': ATTEMPT,
            'Status': 'SUCCESS', 'Error': 'HANGUP', 'Pages': '2', 'Mode': 'T38',
            'Station64': 'KzE1NTU1NTUwMTk5', 'Answered': str(_epoch(NOW + timedelta(seconds=8))),
            'Ended': str(_epoch(NOW + timedelta(seconds=73))), 'Cause': '16', **extra}


def _epoch(value):
    return int((value - datetime(1970, 1, 1)).total_seconds())


def test_answered_fax_call_records_times_seconds_media_pages_and_station(records):
    records.record_submission(submission(), now=NOW)
    pending = records.for_attempt(ATTEMPT)
    assert len(pending) == 1 and pending[0]['disposition'] == 'ambiguous' and pending[0]['ended_at'] is None
    records.record_originate_response({'Event': 'OriginateResponse', 'Response': 'Success', 'Reason': '4',
                                       'ActionID': f'faxbot:{JOB}:{ATTEMPT}'}, now=NOW + timedelta(seconds=8))
    records.record_fax_result(fax_result(), now=NOW + timedelta(seconds=74))
    [record] = records.for_attempt(ATTEMPT)
    assert record == {
        'id': record['id'], 'direction': 'outbound', 'job_id': JOB, 'attempt_id': ATTEMPT, 'trunk_preset': 'telnyx',
        'did': '+15555550100', 'caller': '+15555550100', 'called': '+15555550123',
        'started_at': '2026-10-03T12:00:00Z', 'answered_at': '2026-10-03T12:00:08Z',
        'ended_at': '2026-10-03T12:01:13Z', 'disposition': 'answered', 'connected_seconds': 65, 't38': 'yes',
        'pages': 2, 'fax_status': 'SUCCESS', 'remote_station_id': '+15555550199', 'error_cause': None,
        'fax_preference': True}
    assert records.connected_seconds_for(ATTEMPT) == 65


@pytest.mark.parametrize('reason,disposition,text', [
    ('5', 'busy', 'busy'), ('8', 'congestion', 'network congestion'), ('3', 'no_answer', 'no answer'),
    ('0', 'failed', 'call failed'), ('1', 'failed', 'call ended before answer'), ('42', 'failed', 'call failed')])
def test_unanswered_calls_record_the_carrier_disposition_and_zero_connected_seconds(records, reason, disposition, text):
    records.record_submission(submission(), now=NOW)
    records.record_originate_response({'Event': 'OriginateResponse', 'Response': 'Failure', 'Reason': reason,
                                       'ActionID': f'faxbot:{JOB}:{ATTEMPT}'}, now=NOW + timedelta(seconds=30))
    [record] = records.for_attempt(ATTEMPT)
    assert (record['disposition'], record['connected_seconds'], record['error_cause']) == (disposition, 0, text)
    assert record['ended_at'] == '2026-10-03T12:00:30Z' and record['t38'] == 'unknown'


def test_call_with_no_terminal_event_stays_ambiguous_and_has_no_measured_seconds(records):
    records.record_submission(submission(FaxPreference='no'), now=NOW)
    [record] = records.for_attempt(ATTEMPT)
    assert record['disposition'] == 'ambiguous' and record['fax_preference'] is False
    assert records.connected_seconds_for(ATTEMPT) is None


def test_failed_fax_after_answer_keeps_audio_mode_and_a_plain_cause(records):
    records.record_submission(submission(), now=NOW)
    records.record_fax_result(fax_result(Status='FAILED', Error='NO_DATA', Mode='audio', Pages='0', Cause='16'),
                              now=NOW + timedelta(seconds=74))
    [record] = records.for_attempt(ATTEMPT)
    assert record['t38'] == 'no' and record['fax_status'] == 'FAILED' and record['pages'] == 0
    assert record['error_cause'] == 'NO_DATA (cause 16)' and record['disposition'] == 'answered'


def test_late_originate_success_never_reopens_a_finished_call(records):
    records.record_submission(submission(), now=NOW)
    records.record_fax_result(fax_result(), now=NOW + timedelta(seconds=74))
    records.record_originate_response({'Response': 'Failure', 'Reason': '5', 'ActionID': f'faxbot:{JOB}:{ATTEMPT}'})
    [record] = records.for_attempt(ATTEMPT)
    assert record['disposition'] == 'answered' and record['connected_seconds'] == 65


def test_events_without_a_verified_identity_are_ignored(records):
    assert records.record_fax_result({'JobID': 'job,DEST=x', 'AttemptID': ATTEMPT}) is None
    assert records.record_originate_response({'ActionID': 'not-faxbot', 'Response': 'Failure'}) is None
    assert records.record_submission({'JobID': JOB}) is None
    assert records.page()['items'] == []


def test_inbound_call_is_recorded_once_per_asterisk_call(records):
    call = {'did': '+15555550199', 'caller': '+15555550100', 'started_at': _epoch(NOW),
            'answered_at': _epoch(NOW), 'ended_at': _epoch(NOW + timedelta(seconds=26)), 'pages': 2, 't38': True,
            'remote_station_id_b64': 'KzE1NTU1NTUwMTAw'}
    first = records.record_inbound(call, call_id='1791049108.4', inbound_fax_id='f' * 32, preset='telnyx',
                                   fax_status='SUCCESS')
    again = records.record_inbound(call, call_id='1791049108.4', inbound_fax_id='e' * 32)
    assert first == again
    [record] = records.page(direction='inbound')['items']
    assert (record['did'], record['caller'], record['called'], record['connected_seconds'], record['t38'],
            record['pages'], record['remote_station_id'], record['job_id']) == (
        '+15555550199', '+15555550100', '+15555550199', 26, 'yes', 2, '+15555550100', 'f' * 32)
    unsafe = records.record_inbound({'did': '1555; rm -rf /', 'caller': '<script>', 't38': 'yes'},
                                    call_id='1791049108.5')
    row = next(item for item in records.page()['items'] if item['id'] == unsafe)
    assert row['did'] is None and row['caller'] is None and row['t38'] == 'unknown'


def test_cursor_pages_walk_newest_first_without_repeats(records):
    for index in range(7):
        records.record_submission(submission(AttemptID=f'{index:032x}'), now=NOW + timedelta(minutes=index))
    seen, cursor = [], None
    while True:
        page = records.page(limit=3, cursor=cursor)
        seen.extend(item['attempt_id'] for item in page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert seen == [f'{index:032x}' for index in reversed(range(7))]
    with pytest.raises(ValueError):
        records.page(cursor='not-a-cursor')


@pytest.mark.asyncio
async def test_ami_events_flow_into_records_without_touching_delivery_listeners(records, monkeypatch):
    """Fake AMI peer frames reach both the delivery listener and the call recorder."""
    from api.tests.test_native_submission import connected_stream, feed_response
    delivered = []
    async with connected_stream(monkeypatch) as (client, writer):
        client.on_fax_result(delivered.append)
        sip_calls.attach(client, records.engine)
        try:
            values = ConfigurationValues.from_environment({
                'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_CALLER_ID': '+15555550100'})
            with use_configuration(values):
                task = asyncio.create_task(client.originate_sendfax(JOB, '+15555550123', '/fax/a.tif',
                                                                    attempt_id=ATTEMPT))
            await writer.requests.get()
            feed_response(client, f'faxbot:{JOB}:{ATTEMPT}')
            await task
            frame = ''.join(f'{key}: {value}\r\n' for key, value in fax_result().items()) + '\r\n'
            client.reader.feed_data(frame.encode())
            await asyncio.sleep(0.05)
        finally:
            sip_calls.detach()
    assert [event['JobID'] for event in delivered] == [JOB]
    [record] = records.for_attempt(ATTEMPT)
    assert record['disposition'] == 'answered' and record['t38'] == 'yes' and record['pages'] == 2
    assert record['trunk_preset'] == 'telnyx' and record['called'] == '+15555550123'


@pytest.mark.asyncio
async def test_a_broken_record_store_never_blocks_the_call(monkeypatch):
    from api.tests.test_native_submission import connected_stream, feed_response
    engine = sa.create_engine('sqlite://')  # no sip_call_records table
    async with connected_stream(monkeypatch) as (client, writer):
        sip_calls.attach(client, engine)
        try:
            task = asyncio.create_task(client.originate_sendfax(JOB, '+15555550123', '/fax/a.tif',
                                                                attempt_id=ATTEMPT))
            await writer.requests.get()
            feed_response(client, f'faxbot:{JOB}:{ATTEMPT}')
            assert await task is None
        finally:
            sip_calls.detach()


def test_route_cost_capture_uses_the_measured_connected_seconds(records):
    """The routing ledger prices a native fax from the trunk's connected time, not Faxbot's timing."""
    from types import SimpleNamespace
    from app.routing.capture import CostRecorder
    records.record_submission(submission(), now=NOW)
    recorder = CostRecorder(None, observed_seconds=records.observed_seconds)
    assert recorder._observed(SimpleNamespace(attempt_id=ATTEMPT, phase='success')) is None
    records.record_fax_result(fax_result(), now=NOW + timedelta(seconds=74))
    assert recorder._observed(SimpleNamespace(attempt_id=ATTEMPT, phase='success')) == 65
    assert recorder._observed(SimpleNamespace(attempt_id=ATTEMPT, phase='uncertain')) is None
    assert recorder._observed(SimpleNamespace(attempt_id='f' * 32, phase='failed')) is None
