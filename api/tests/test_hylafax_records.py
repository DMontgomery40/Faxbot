"""The SSL Fax engine's records (migration 0017): call records from engine events, engine records,
SSL Fax observations and recipient limits, on SQLite and PostgreSQL, plus the per-call fax settings."""
import base64
from datetime import datetime, timedelta

import pytest

from api.app import schema
from app import hylafax_engine, hylafax_records, sip_calls, sip_trunk
from app.config_values import ConfigurationValues
from api.tests.test_schema import database  # noqa: F401 - fixture

JOB, ATTEMPT = 'a' * 32, 'b' * 32
NOW = datetime(2026, 10, 5, 1, 0, 0)
PEER = '+15555550199'


@pytest.fixture
def installation(database):
    schema.upgrade_schema(database)
    return sip_calls.SipCallRecords(database), hylafax_records.records_for(database)


def b64(text):
    return base64.b64encode(text.encode()).decode()


def epoch(moment):
    return str(int((moment - datetime(1970, 1, 1)).total_seconds()))


def engine_call(**extra):
    return {'Direction': 'out', 'JobID': JOB, 'AttemptID': ATTEMPT, 'Engine': 'hylafax', 'Gateway': 'yes',
            'T38': 'ENABLED', 'Started': epoch(NOW), 'Answered': epoch(NOW + timedelta(seconds=4)),
            'Ended': epoch(NOW + timedelta(seconds=34)), 'Cause': '16', 'CallID64': b64('call-1@carrier'), **extra}


def row(records, direction='outbound', key=ATTEMPT):
    with records.engine.connect() as connection:
        return records._find(connection, records.table, direction, key)


def test_an_engine_call_completes_the_attempt_row_with_the_carrier_call_and_t38(installation):
    calls, _ = installation
    calls.record_submission({'JobID': JOB, 'AttemptID': ATTEMPT, 'Called': PEER, 'CallerID': '+15555550100',
                             'Preset': 'telnyx', 'FaxPreference': 'yes'}, now=NOW)
    calls.record_engine_call(engine_call(), now=NOW)
    calls.record_engine_result(JOB, ATTEMPT, success=True, pages=3, station='PEER FAX', now=NOW)
    record = row(calls)
    assert (record['disposition'], record['connected_seconds'], record['t38']) == ('answered', 30, 'yes')
    assert (record['sip_call_id'], record['pages'], record['fax_status']) == ('call-1@carrier', 3, 'SUCCESS')
    assert record['remote_station_id'] == 'PEER FAX' and record['called'] == PEER
    public = calls.for_attempt(ATTEMPT)[0]
    assert public['summary'] == 'Sent: 3 pages confirmed by the receiving machine.'
    # Charges and Spending read the measured seconds exactly as for the built-in engine.
    assert calls.connected_seconds_for(ATTEMPT) == 30


@pytest.mark.parametrize('cause, disposition', [('17', 'busy'), ('19', 'no_answer'), ('34', 'congestion'),
                                                ('21', 'failed')])
def test_an_engine_call_that_never_answered_says_how_it_ended(installation, cause, disposition):
    calls, _ = installation
    calls.record_engine_call(engine_call(Answered='', Cause=cause, T38='DISABLED', CallID64=''), now=NOW)
    record = row(calls)
    assert record['disposition'] == disposition and record['connected_seconds'] == 0 and record['t38'] == 'no'
    assert record['sip_call_id'] is None


def test_a_refused_t38_request_is_recorded_as_audio(installation):
    calls, _ = installation
    calls.record_engine_call(engine_call(T38='REJECTED', T38Session='0'), now=NOW)
    assert row(calls)['t38'] == 'no'


def test_t38_comes_from_the_gateways_fax_session_not_the_state_at_hang_up(installation):
    """The gateway turns the call back to audio at the end, so the trunk state at hang-up says DISABLED."""
    calls, _ = installation
    calls.record_engine_call(engine_call(T38='DISABLED', T38Session='4'), now=NOW)
    assert row(calls)['t38'] == 'yes'


def test_a_received_engine_call_and_its_hand_over_meet_in_either_order(installation):
    calls, _ = installation
    event = engine_call(Direction='in', Token='179117219142', DID='+15555550100', Caller=PEER, T38='REJECTED')
    # Event first, then the hand-over (as inbound/http.py records it).
    calls.record_engine_call(event, now=NOW)
    sip_calls.record_inbound_call(calls.engine, {'did': '+15555550100', 'caller': PEER, 'pages': 2},
                                  call_id='engine.179117219142', inbound_fax_id='f' * 32)
    record = row(calls, 'inbound', 'engine.179117219142')
    assert record['job_id'] == 'f' * 32 and record['sip_call_id'] == 'call-1@carrier' and record['t38'] == 'no'
    assert record['connected_seconds'] == 30
    # Hand-over first, then the event: the event fills what the hand-over did not know.
    sip_calls.record_inbound_call(calls.engine, {'did': '+15555550100', 'caller': PEER, 'pages': 1},
                                  call_id='engine.5', inbound_fax_id='e' * 32)
    calls.record_engine_call({**event, 'Token': '5', 'T38': 'ENABLED'}, now=NOW)
    record = row(calls, 'inbound', 'engine.5')
    assert record['job_id'] == 'e' * 32 and record['sip_call_id'] == 'call-1@carrier' and record['t38'] == 'yes'
    assert record['answered_at'] is not None and record['connected_seconds'] == 30


def test_engine_records_fill_once_and_observations_say_whether_a_number_takes_sslfax(installation):
    _, records = installation
    records.record_call(direction='outbound', call_key=ATTEMPT, engine='hylafax', job_id=JOB, number=PEER, now=NOW)
    details = {'engine_ref': 'abcdef0123456789:1', 'sslfax': True, 'sslfax_offered': True, 'transfer_seconds': 9,
               'session_seconds': 18, 'signal_rate': 'SSL Fax', 'data_format': 'JBIG'}
    records.record_result(direction='outbound', call_key=ATTEMPT, details=details, job_id=JOB, number=PEER, now=NOW)
    # The same result again (a retried report) and a different later value change nothing.
    records.record_result(direction='outbound', call_key=ATTEMPT, details={**details, 'transfer_seconds': 50},
                          job_id=JOB, number=PEER, now=NOW)
    found = records.for_call('outbound', ATTEMPT)
    assert found['sslfax'] is True and found['transfer_seconds'] == 9 and found['signal_rate'] == 'SSL Fax'
    assert records.accepts_sslfax(PEER) == {'accepts': True, 'observed_at': '2026-10-05T01:00:00Z',
                                            'direction': 'outbound'}
    # A later call shows the machine without SSL Fax: the newest observation wins.
    records.record_result(direction='inbound', call_key='engine.7',
                          details={'engine_ref': 'abcdef0123456789:2', 'sslfax': False, 'sslfax_offered': False},
                          number=PEER, now=NOW + timedelta(days=1))
    assert records.accepts_sslfax(PEER)['accepts'] is False
    with records.engine.connect() as connection:
        count = connection.exec_driver_sql('SELECT COUNT(*) FROM sslfax_observations').scalar_one()
    assert count == 2


def test_the_built_in_engine_record_says_why(installation):
    _, records = installation
    records.record_call(direction='outbound', call_key=ATTEMPT, engine='builtin', job_id=JOB,
                        reason=hylafax_engine.SENDING_TOGETHER, number=PEER, now=NOW)
    found = records.for_call('outbound', ATTEMPT)
    assert found['engine'] == 'builtin' and found['reason'] == hylafax_engine.SENDING_TOGETHER
    assert found['sslfax'] is None
    with pytest.raises(ValueError):
        records.record_call(direction='sideways', call_key=ATTEMPT, engine='builtin')


def test_recipient_limits_are_saved_cleared_and_checked(installation):
    _, records = installation
    assert records.recipient_settings(PEER) is None
    records.set_recipient_settings(PEER, max_rate=9600, ecm=False, actor='principal:p1', now=NOW)
    assert records.recipient_settings(PEER) == {'max_rate': 9600, 'ecm': False, 'updated_at': '2026-10-05T01:00:00Z'}
    records.set_recipient_settings(PEER, max_rate=4800, now=NOW)
    assert records.recipient_settings(PEER)['max_rate'] == 4800 and records.recipient_settings(PEER)['ecm'] is None
    records.set_recipient_settings(PEER)
    assert records.recipient_settings(PEER) is None
    for bad in ({'max_rate': 12000}, {'ecm': 'yes'}):
        with pytest.raises(ValueError):
            records.set_recipient_settings(PEER, **bad)
    with pytest.raises(ValueError):
        records.set_recipient_settings('not a number', max_rate=9600)


# -- per-call fax settings (both engines) ------------------------------------------------------------

def values(**extra):
    return ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1',
        'SIP_TRUNK_CALLER_ID': '+15555550100', **extra})


def test_call_settings_take_the_trunk_the_network_and_the_recipient_into_account(monkeypatch):
    from app import sip_network
    monkeypatch.setattr(sip_network, 'network_allows_t38', lambda values: None)
    settings = hylafax_engine.call_settings(values(), PEER)
    assert (settings.t38, settings.max_rate, settings.ecm) == (True, 14400, True)
    # A troublesome machine's own limits win, but never above the installation's setting.
    settings = hylafax_engine.call_settings(values(SIP_FAX_MAX_RATE='9600'), PEER,
                                            recipient={'max_rate': 14400, 'ecm': False})
    assert (settings.max_rate, settings.ecm) == (9600, False)
    # T.38 off on the trunk: audio, so at most 9600.
    assert hylafax_engine.call_settings(values(SIP_T38_ENABLED='false'), PEER).max_rate == 9600
    # The network check says T.38 data cannot come back: this call stays audio.
    monkeypatch.setattr(sip_network, 'network_allows_t38', lambda values: False)
    settings = hylafax_engine.call_settings(values(), PEER)
    assert (settings.t38, settings.max_rate) == (False, 9600)


def test_fax_settings_reach_the_trunk_the_built_in_engine_and_the_ssl_fax_engine(tmp_path):
    from app import ami
    configured = values(SIP_T38_ERROR_CORRECTION='fec', SIP_T38_MAX_DATAGRAM='320', SIP_FAX_ECM='false',
                        SIP_FAX_COMPRESSION='mr', SIP_FAX_MAX_RATE='7200', FAX_DATA_DIR=str(tmp_path),
                        ASTERISK_INBOUND_SECRET='synthetic-inbound-secret-0123456789')
    pjsip = sip_trunk.render_pjsip(configured)
    assert 't38_udptl_ec=fec' in pjsip and 't38_udptl_maxdatagram=320' in pjsip
    call = hylafax_engine.CallSettings(t38=True, max_rate=7200, ecm=False, fine=True, compression='mr')
    fields = ami.originate_fields_for(configured, JOB, PEER, '/faxdata/x.tiff', attempt_id=ATTEMPT, call=call)
    assert 'FAXBOT_MAXRATE=7200' in fields['Variable'] and 'FAXBOT_ECM=no' in fields['Variable']
    secrets_ = hylafax_engine.engine_secrets(configured)
    conf = hylafax_engine.render_engine_conf(configured, secrets_, inbound_secret='synthetic-inbound-secret-0123456789')
    assert 'max_rate=7200\n' in conf and 'ecm=no\n' in conf and 'compression=mr\n' in conf
    options = hylafax_engine.render_options(configured, lines=2)
    assert 'FAXBOT_IN_RATE=7200' in options and 'FAXBOT_IN_AUDIO_RATE=7200' in options
    assert 'FAXBOT_IN_ECM=no' in options
    assert 'IAX2/faxbot-line1/${FAXBOT_ENGINE_DID}&IAX2/faxbot-line2/${FAXBOT_ENGINE_DID}' in options
    assert 'FAXBOT_ENGINE_LINES=)' in hylafax_engine.render_options(configured)
    with pytest.raises(Exception):
        values(SIP_FAX_MAX_RATE='12000')


def test_the_listener_is_advertised_only_with_an_address_and_sslfax_on(tmp_path):
    configured = values(FAX_DATA_DIR=str(tmp_path), SIP_EXTERNAL_ADDRESS='203.0.113.10')
    assert hylafax_engine.listener_address(configured) == '203.0.113.10:10443'
    assert hylafax_engine.listener_address(values(FAX_DATA_DIR=str(tmp_path), SIP_EXTERNAL_ADDRESS='203.0.113.10',
                                                  SIP_SSLFAX_ENABLED='false')) == ''
    assert hylafax_engine.listener_address(values(FAX_DATA_DIR=str(tmp_path))) == ''
