"""What the far end's fax machine said (patch 0004's FaxFrames event) and what Faxbot learns from it.

Synthetic frames and numbers only. The frames below are the shapes the replay proof
(test_t38_terminal_replay.py) gets from spandsp: the DIS of the captured Telnyx engine calls is
``ff138000cefac4809d80808078`` (V.29, ECM, T.6; no real number is in a DIS).
"""
import asyncio
from datetime import datetime, timedelta
import time

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import ami, engine_frames
from app.config_values import ConfigurationValues
from app.schema import create_database_engine, upgrade_schema

NUMBER = '+13035550150'
DIS_V17 = 'ff138000eef5c4808001'
DIS_TELNYX = 'ff138000cefac4809d80808078'
DCS_14400 = 'ff138300e29404'
SUB_4021 = 'ff03c3' + bytes(reversed(b'4021'.ljust(20))).hex()
CSA = 'ff0325' + '01' + b'fax.example.test:4559'.hex()


def values(**extra):
    return ConfigurationValues.from_environment({'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                                                 'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', **extra})


def event(**fields):
    base = {'UserEvent': 'FaxFrames', 'Direction': 'out', 'JobID': 'job1', 'AttemptID': 'attempt1',
            'UniqueID': '1791391994.0', 'Mode': 'T38', 'Status': 'SUCCESS', 'Answered': '1791391994',
            'T38At': '1791392004600', 'T38By': 'faxbot', 'T38Now': '', 'Iaf': '', 'Dis': DIS_V17,
            'DcsFirst': DCS_14400, 'DcsLast': DCS_14400, 'DcsSent': '1', 'Rates': '20', 'Csa': '', 'Tsa': '',
            'Sub': '', 'Nsf': '', 'Trainings': '1', 'Ftt': '0'}
    base.update(fields)
    return base


# -- decoding ---------------------------------------------------------------------------------

def test_the_far_ends_dis_reads_as_its_capabilities():
    v17 = engine_frames.decode_dis(DIS_V17)
    assert (v17['max_rate'], v17['ecm'], v17['mmr'], v17['fine'], v17['widths'], v17['length']) == (
        14400, True, True, True, 'A4 and B4', 'unlimited')
    assert v17['subaddress'] and not v17['jbig']
    telnyx = engine_frames.decode_dis(DIS_TELNYX)
    assert telnyx['max_rate'] == 9600 and telnyx['ecm'] and telnyx['mmr'] and not telnyx['v17']
    assert engine_frames.decode_dis('') is None and engine_frames.decode_dis(DCS_14400) is None
    assert engine_frames.decode_dis('zz') is None


def test_speeds_subaddress_and_internet_address_are_read_from_their_frames():
    assert engine_frames.dcs_rate(DCS_14400) == 14400
    assert engine_frames.decode_rates('20.24.04.0c.08') == [14400, 9600, 9600, 7200, 4800]
    assert engine_frames.decode_rates('20,24') == []
    assert engine_frames.decode_sub(SUB_4021) == '4021'
    assert engine_frames.decode_address(CSA, 0x24) == {'type': 1, 'address': 'fax.example.test:4559'}


def test_an_event_becomes_one_row_with_the_seconds_from_answer_to_t38():
    row = engine_frames.parse_event(event(), trunk='telnyx@')
    assert row['id'] == 'out:attempt1' and row['number'] is None and row['trunk'] == 'telnyx@'
    assert (row['rate_first'], row['rate_lowest'], row['t38_after_ms'], row['t38_by']) == (14400, 14400, 10600, 'faxbot')
    received = engine_frames.parse_event(event(Direction='in', AttemptID='', JobID='', Caller='+13035550150'))
    assert received['id'] == 'in:1791391994.0' and received['number'] == NUMBER
    # Anything malformed is dropped, never guessed.
    assert engine_frames.parse_event(event(Direction='sideways')) is None
    assert engine_frames.parse_event(event(AttemptID='')) is None
    odd = engine_frames.parse_event(event(Dis='not-hex', Rates='20,24', T38By='someone'))
    assert odd['dis'] is None and odd['rates'] is None and odd['t38_by'] is None


# -- learning ---------------------------------------------------------------------------------

def call(**fields):
    base = {'direction': 'out', 'mode': 'T38', 'status': 'SUCCESS', 'rate_first': 14400, 'rate_lowest': 14400,
            'ftt': 0, 't38_after_ms': 10600, 't38_by': 'faxbot', 't38_now': 0}
    base.update(fields)
    return base


def test_a_number_that_never_asks_for_t38_itself_gets_t38_at_once_after_three_calls():
    assert engine_frames.learn([call(), call()], values()).t38_now is False
    learned = engine_frames.learn([call(), call(), call()], values())
    assert learned.t38_now and learned.t38_reason.startswith('The last 3 faxes to this number switched')
    # The far end asked once: it can, so no early T.38. Nor with T.38 off on the trunk.
    assert engine_frames.learn([call(), call(t38_by='far', t38_after_ms=2000), call()], values()).t38_now is False
    assert engine_frames.learn([call()] * 3, values(SIP_T38_ENABLED='false')).t38_now is False
    # Calls Faxbot already sent early don't keep proving it (they never wait now).
    assert engine_frames.learn([call(t38_now=1, t38_after_ms=200)] * 3, values()).t38_now is False


def test_failed_trainings_at_one_speed_start_later_calls_at_the_speed_that_worked():
    failing = call(rate_first=14400, rate_lowest=9600, ftt=2)
    learned = engine_frames.learn([failing, failing], values())
    assert learned.max_rate == 9600 and '14,400' in learned.rate_reason and '9,600' in learned.rate_reason
    assert engine_frames.learn([failing, call()], values()).max_rate is None
    assert engine_frames.learn([call(rate_first=14400, rate_lowest=12000, ftt=1)] * 2, values()).max_rate == 9600


def test_a_caller_whose_line_trained_cleanly_at_14400_over_audio_is_received_at_that_speed():
    clean = call(mode='audio', t38_by=None, t38_after_ms=None)
    assert engine_frames.learn([clean, clean], values()).inbound_rate == 14400
    assert engine_frames.learn([clean, call(mode='audio', ftt=1)], values()).inbound_rate is None


# -- storage, IAF and Asterisk's keys --------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    database = create_database_engine('sqlite:///' + str(tmp_path / 'frames.db'))
    upgrade_schema(database)
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(jobs.insert().values(id='job1', to_number=NUMBER, file_name='a.pdf', tiff_path='a.tiff',
                                                status='SUCCESS', backend='sip',
                                                created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
    yield database
    database.dispose()


def test_each_call_is_recorded_once_with_the_far_ends_number_from_its_fax(engine):
    store = engine_frames.FrameStore(engine)
    row = engine_frames.parse_event(event(), trunk=engine_frames.trunk_key(values()))
    assert store.record(row) is True and store.record(row) is False
    [kept] = store.calls(NUMBER)
    assert kept['number'] == NUMBER and kept['dis'] == DIS_V17
    assert store.calls('3035550150') == store.calls(NUMBER)
    assert engine_frames.describe(kept)[0].startswith("The far end's fax machine accepts up to 14,400 bit/s")


def test_call_options_ask_for_t38_at_once_lower_the_speed_and_use_iaf_only_where_approved(engine, monkeypatch):
    store = engine_frames.FrameStore(engine)
    settings = values()
    now = datetime.utcnow()
    for number in range(3):
        row = engine_frames.parse_event(event(AttemptID=f'attempt{number}', Rates='20.24', Ftt='1'),
                                        trunk=engine_frames.trunk_key(settings))
        store.record(row, now=now - timedelta(minutes=number))
    options = engine_frames.call_options(settings, NUMBER, engine=engine)
    assert options.t38_now and options.max_rate == 9600 and options.iaf is None
    # Another trunk: nothing learned there yet.
    other = values(SIP_TRUNK_PRESET='signalwire', SIP_TRUNK_HOST='example.signalwire.com')
    assert engine_frames.call_options(other, NUMBER, engine=engine) == engine_frames.CallOptions()
    store.add_endpoint(NUMBER, 'endpoint', 'Head office SR140', actor_name='Dana Admin')
    assert engine_frames.call_options(settings, NUMBER, engine=engine).iaf == 'endpoint'
    assert engine_frames.call_options(settings, '+13035550151', engine=engine).iaf is None
    # The Originate fields carry all three; a learned speed only lowers this call's speed.
    monkeypatch.setattr(ami, '_database', lambda: engine)
    trunk = values(SIP_TRUNK_CALLER_ID='+13035550100')
    fields = ami.originate_fields_for(trunk, 'job1', NUMBER, '/faxdata/job1.tiff',
                                      choice=ami.reply_choice(trunk))
    assert 'FAXBOT_T38_NOW=yes' in fields['Variable'] and 'FAXBOT_IAF=endpoint' in fields['Variable']
    assert 'FAXBOT_MAXRATE=9600' in fields['Variable']
    assert ami.frame_options(trunk, NUMBER, max_rate=4800) == {'t38_now': True, 'iaf': 'endpoint'}


def test_iaf_is_refused_for_anything_but_a_peer_or_an_endpoint_with_a_name(engine):
    store = engine_frames.FrameStore(engine)
    with pytest.raises(engine_frames.FramesRefused):
        store.add_endpoint(NUMBER, 'carrier', 'Telnyx')
    with pytest.raises(engine_frames.FramesRefused, match='Name this fax server'):
        store.add_endpoint(NUMBER, 'peer', '  ')
    with pytest.raises(ValueError):
        ami.prepare_originate_fields('job1', NUMBER, '/faxdata/a.tiff', caller_id='', iaf='carrier')


def test_asterisks_iaf_and_received_speed_keys_follow_what_faxbot_knows(engine):
    from api.tests.test_screening import FakeAsterisk
    store = engine_frames.FrameStore(engine)
    settings = values()
    for number in range(2):
        store.record(engine_frames.parse_event(event(AttemptID=f'audio{number}', Mode='audio', T38At='', T38By=''),
                                               trunk=engine_frames.trunk_key(settings)))
    endpoint = store.add_endpoint('+13035550160', 'peer', 'Partner office')
    asterisk = FakeAsterisk({'/faxbot-iaf/19995550000': 'peer'})
    asyncio.run(engine_frames.sync(asterisk, store, engine, settings))
    keys = {key: value for key, value in asterisk.database.items()}
    assert keys['/faxbot-iaf/13035550160'] == 'peer' and keys['/faxbot-iaf/3035550160'] == 'peer'
    assert '/faxbot-iaf/19995550000' not in keys
    assert keys['/faxbot-inrate/13035550150'] == '14400'
    store.remove_endpoint(endpoint['id'], actor_name='Dana Admin')
    asyncio.run(engine_frames.sync(asterisk, store, engine, settings))
    assert not any(key.startswith('/faxbot-iaf/') for key in asterisk.database)


# -- the console's and the command line's API ----------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def test_their_fax_machine_and_iaf_approval_over_http_with_audit_rows(client):
    from api.tests.test_access_management_http import B
    from app.main import app
    engine = app.state.configuration_runtime.manager.store.engine
    work = app.state.frames_work
    work.heard(event(Direction='in', AttemptID='', JobID='', Caller=NUMBER, Sub=SUB_4021))
    deadline = datetime.utcnow() + timedelta(seconds=5)
    while not engine_frames.FrameStore(engine).calls(NUMBER) and datetime.utcnow() < deadline:
        time.sleep(0.05)  # the event is recorded on a worker thread; wait for that, not a fixed time
    view = client.get('/fax-machines/numbers/303-555-0150', headers=B)
    assert view.status_code == 200, view.text
    body = view.json()
    assert body['number'] == NUMBER and body['calls'][0]['subaddress'] == '4021'
    assert 'The sender named subaddress 4021.' in body['calls'][0]['sentences']
    added = client.post('/fax-machines/iaf', headers=B, json={'number': NUMBER, 'kind': 'peer', 'label': 'Partner'})
    assert added.status_code == 200, added.text
    server = added.json()['server']
    assert client.get('/fax-machines/numbers/' + NUMBER, headers=B).json()['iaf'] == 'peer'
    assert [item['id'] for item in client.get('/fax-machines/iaf', headers=B).json()['servers']] == [server['id']]
    assert client.delete('/fax-machines/iaf/' + server['id'], headers=B).status_code == 200
    assert client.delete('/fax-machines/iaf/' + server['id'], headers=B).status_code == 404
    audit = sa.Table('access_audit', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        operations = [row.operation for row in connection.execute(
            sa.select(audit.c.operation).where(audit.c.operation.like('iaf.%')).order_by(audit.c.created_at))]
    assert operations == ['iaf.approve', 'iaf.remove']
    assert client.post('/fax-machines/iaf', json={'number': NUMBER, 'kind': 'peer', 'label': 'x'}).status_code in (401, 403)


# -- faxbot recipients fax-machine / iaf --------------------------------------------------------

def test_the_command_line_shows_a_fax_machine_and_approves_and_stops_fast_fax(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    from app.main import app
    for client in _serve(monkeypatch, tmp_path):
        cli = Cli(client)
        engine = app.state.configuration_runtime.manager.store.engine
        engine_frames.FrameStore(engine).record(engine_frames.parse_event(
            event(Direction='in', AttemptID='', JobID='', Caller=NUMBER, Sub=SUB_4021)))
        shown = cli('recipients', 'fax-machine', NUMBER)
        assert shown.exit_code == 0, (shown.stdout, shown.stderr)
        assert "The far end's fax machine accepts up to 14,400 bit/s" in shown.stdout
        assert 'The sender named subaddress 4021.' in shown.stdout
        refused = cli('recipients', 'iaf', 'add', NUMBER, '--kind', 'carrier', '--name', 'Telnyx')
        assert refused.exit_code != 0 and 'Choose --kind faxbot or --kind server.' in refused.stderr
        added = cli('recipients', 'iaf', 'add', NUMBER, '--kind', 'server', '--name', 'Head office SR140')
        assert added.exit_code == 0 and 'now go as Internet Aware Fax' in added.stdout, (added.stdout, added.stderr)
        assert 'Head office SR140' in cli('recipients', 'iaf', 'list').stdout
        assert 'go as Internet Aware Fax' in cli('recipients', 'fax-machine', NUMBER).stdout
        removed = cli('recipients', 'iaf', 'remove', '303-555-0150')
        assert removed.exit_code == 0 and 'fax line speed again' in removed.stdout
        assert cli.json('recipients', 'iaf', 'list')['servers'] == []
