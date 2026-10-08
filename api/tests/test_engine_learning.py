"""What Faxbot learns per number from its own fax calls, and what it changes (T8 memory, T9 rule, epochs).

Synthetic call histories only (555 numbers, made-up frames): call records, engine reports and frames are written
the way the engines' events leave them, then the decision for the next call is read. No call is ever placed.
"""
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import types
import uuid

import pytest
import sqlalchemy as sa

from app import ami, engine_frames, engine_learning, hylafax_engine, hylafax_records
from app.config_values import ConfigurationValues
from app.schema import create_database_engine, upgrade_schema
from api.tests.test_schema import database  # noqa: F401 - fixture (SQLite, and PostgreSQL when configured)

ROOT = Path(__file__).resolve().parents[2]
NUMBER = '+13035550150'
OTHER = '+13035550151'
LINE = '+13035550100'
# A DIS offering V.17, fine, MR, ECM and MMR; a DCS choosing 14,400 V.17, fine, MR and ECM (as test_engine_frames).
DIS_ECM = 'ff138000eef5c4808001'
DIS_NO_ECM = 'ff138000eef5c08080'
DCS_MR_ECM = 'ff138300e29404'
DCS_MR_NO_ECM = 'ff138300e29400'


DATA = {}


@pytest.fixture(autouse=True)
def data_folder(tmp_path):
    """Each test's own data folder: no network check or engine build from elsewhere is read."""
    DATA['folder'] = tmp_path / 'faxdata'
    DATA['folder'].mkdir()
    yield
    DATA.pop('folder', None)


def values(tmp_path=None, **extra):
    base = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser', 'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1',
            'FAX_DATA_DIR': str(tmp_path or DATA['folder'])}
    return ConfigurationValues.from_environment({**base, **extra})


def ago(**delta):
    return datetime.utcnow() - timedelta(**delta)


@pytest.fixture
def db(database):  # noqa: F811
    upgrade_schema(database)
    yield database


@pytest.fixture
def sqlite(tmp_path):
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'learning.db'))
    upgrade_schema(engine)
    yield engine
    engine.dispose()


def record(db, *, number=NUMBER, direction='outbound', mode='t38', status='FAILED', verdict='remote_fax_failed',
           when=None, attempt=None, pages=0, reason='The call dropped prematurely'):
    """One trunk call record as sip_calls leaves it; returns its call key (the attempt, or Asterisk's call ID)."""
    when = when or ago(hours=2)
    sent = direction == 'outbound'
    key = attempt or (uuid.uuid4().hex if sent else f'1791391994.{random.randint(1, 10 ** 6)}')
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=db)
    row = {'id': uuid.uuid4().hex, 'direction': direction, 'call_id': key, 'job_id': ('job' + key[:12]) if sent else None,
           'attempt_id': key if sent else None, 'trunk_preset': 'telnyx', 'did': None,
           'caller': LINE if sent else number, 'called': number if sent else LINE, 'started_at': when,
           'answered_at': when, 'ended_at': when + timedelta(seconds=60), 'disposition': 'answered',
           'connected_seconds': 60, 't38': {'t38': 'yes', 'audio': 'no'}[mode], 'pages': pages, 'fax_status': status,
           'remote_station_id': None, 'error_cause': f'{verdict}:{reason}' if status == 'FAILED' else None,
           'fax_preference': 0, 'created_at': when, 'updated_at': when}
    if 'sip_call_id' in table.c:
        row['sip_call_id'] = None
    with db.begin() as connection:
        connection.execute(table.insert().values(**{name: value for name, value in row.items() if name in table.c}))
    return key


def engine_call(db, key, *, number=NUMBER, direction='outbound', when=None, **negotiated):
    """The SSL Fax engine's record for a call, with what it negotiated (fax_negotiation's values)."""
    records = hylafax_records.records_for(db)
    records.record_call(direction=direction, call_key=key, engine='hylafax', number=number, now=when or ago(hours=2))
    if negotiated:
        records.record_negotiation(direction=direction, call_key=key, engine='hylafax', values=negotiated,
                                   number=number, now=when or ago(hours=2))


def frames(db, key, *, direction='outbound', number=NUMBER, when=None, **fields):
    """The built-in engine's FaxFrames row for a call (patch 0004)."""
    event = {'Direction': 'out' if direction == 'outbound' else 'in', 'JobID': 'job' + key[:12] if direction == 'outbound'
             else '', 'AttemptID': key if direction == 'outbound' else '', 'UniqueID': key if direction == 'inbound'
             else '1791391994.0', 'Mode': 'T38', 'Status': 'SUCCESS', 'Answered': '1791391994', 'T38At': '',
             'T38By': '', 'T38Now': '', 'Iaf': '', 'Dis': DIS_ECM, 'DcsFirst': DCS_MR_ECM, 'DcsLast': DCS_MR_ECM,
             'DcsSent': '1', 'Rates': '20', 'Trainings': '1', 'Ftt': '0', 'Caller': number if direction == 'inbound'
             else ''}
    event.update(fields)
    row = engine_frames.parse_event(event, trunk=engine_frames.trunk_key(values()))
    row['number'] = number
    engine_frames.FrameStore(db).record(row, now=when or ago(hours=2))


def memory_rows(db):
    table = sa.Table('fax_destination_memory', sa.MetaData(), autoload_with=db)
    with db.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(table)).mappings()]


@pytest.fixture
def installation(db, monkeypatch):
    """Faxbot's own database is ``db`` (call_settings and the Originate fields read it there)."""
    monkeypatch.setattr(engine_learning, '_database', lambda: db)
    monkeypatch.setattr(ami, '_database', lambda: db)
    return db


# -- T8: per-number memory ----------------------------------------------------------------------

def test_a_t38_failure_after_the_far_machine_answered_makes_only_that_numbers_new_calls_audio(installation):
    db = installation
    record(db)
    settings = values()
    decision = engine_learning.decide(settings, NUMBER, db=db)
    assert decision.audio and not decision.t38_now
    assert re.fullmatch(r'Fax over IP \(T\.38\) to this number failed on [0-9]{1,2} [A-Z][a-z]+, so Faxbot uses audio '
                        r'fax for it until [0-9]{1,2} [A-Z][a-z]+\.', decision.reasons[0]), decision.reasons
    assert not engine_learning.decide(settings, OTHER, db=db).changed()
    [row] = memory_rows(db)
    assert row['kind'] == 't38_failed' and row['direction'] == 'outbound'
    assert row['expires_at'] - row['learned_at'] == timedelta(days=engine_learning.MEMORY_DAYS)
    # The built-in engine: audio for this call only (SendFAX F), at the audio speed, and no T.38 at once.
    call = hylafax_engine.call_settings(settings, NUMBER)
    assert call.t38 is False and call.max_rate == 9600 and call.learned.audio
    trunk = values(SIP_TRUNK_CALLER_ID=LINE)
    fields = ami.originate_fields_for(trunk, 'job1', NUMBER, '/faxdata/job1.tiff', call=call,
                                      choice=ami.reply_choice(trunk))
    assert 'FAXBOT_AUDIO=yes' in fields['Variable'] and 'FAXBOT_T38_NOW' not in fields['Variable']
    assert 'FAXBOT_MAXRATE=9600' in fields['Variable']
    # The SSL Fax engine: its call plan says audio (no T.38 gateway).
    engine = hylafax_engine.call_settings(settings, NUMBER, engine=True)
    assert engine.t38 is False and engine.learned.audio
    assert hylafax_engine.call_plan(fields, 'a' * 32, 'b' * 32, t38=engine.t38).endswith('/0')
    # Another number keeps T.38 on both.
    assert hylafax_engine.call_settings(settings, OTHER).t38 is True
    assert 'FAXBOT_AUDIO' not in ami.originate_fields_for(trunk, 'job2', OTHER, '/faxdata/job2.tiff',
                                                          choice=ami.reply_choice(trunk))['Variable']


def test_failures_that_moved_the_whole_trunk_or_engine_to_audio_teach_nothing_about_the_number(installation):
    db = installation
    # No T.38 data came back (the trunk-wide switch, sip_fax_mode), the engine heard no fax machine (the engine's
    # own switch), and a call that never reached a fax machine: none of these is about this number.
    record(db, verdict='no_t38_data_back', reason='Timed out waiting for initial communication')
    record(db, verdict='no_fax_signal', reason='E126 No receiver protocol (T.30 T1 timeout)')
    record(db, mode='audio', verdict='no_fax_answer')
    decision = engine_learning.decide(values(), NUMBER, db=db)
    assert not decision.changed() and memory_rows(db) == []
    # So when the network is fixed and Faxbot turns T.38 back on by itself, every number gets T.38 again.
    assert hylafax_engine.call_settings(values(), NUMBER, learn=True).t38 is True


def test_audio_failing_asks_for_t38_at_the_answer_and_both_failing_keeps_the_usual_settings(installation):
    db = installation
    record(db, mode='audio')
    builtin = engine_learning.decide(values(), NUMBER, db=db)
    assert builtin.t38_now and not builtin.audio
    assert builtin.reasons[0].startswith('Audio fax to this number failed on ')
    # The SSL Fax engine cannot ask at the answer: nothing changes there.
    assert not engine_learning.decide(values(), NUMBER, engine='hylafax', db=db).changed()
    # With T.38 off on the trunk, there is nothing to ask for (read only: no new epoch is kept).
    assert not engine_learning.decide(values(SIP_T38_ENABLED='false'), NUMBER, t38=False, db=db, write=False).t38_now
    record(db, mode='t38')
    both = engine_learning.decide(values(), NUMBER, db=db)
    assert not both.audio and not both.t38_now
    assert any(note.startswith('Both fax over IP (T.38) and audio fax to this number failed recently') for note in
               both.notes)


def test_a_memory_expires_ends_when_the_same_mode_goes_through_and_a_person_can_forget_it(db):
    # Older than the memory lasts: nothing.
    record(db, when=ago(days=engine_learning.MEMORY_DAYS + 1))
    assert not engine_learning.decide(values(), NUMBER, db=db).audio
    # A later fax over IP that went through ends it (the row stays, as evidence).
    record(db, number=OTHER, when=ago(hours=3))
    record(db, number=OTHER, status='SUCCESS', when=ago(hours=1), pages=2)
    assert not engine_learning.decide(values(), OTHER, db=db).audio
    epoch = engine_learning.current_epoch(db, values())
    [ended] = engine_learning.memories(db, OTHER, epoch=epoch, views=engine_learning.joined_calls(db, OTHER))
    assert ended['ended'] == 'went_through' and not ended['active']
    # A person tells Faxbot to forget: once, with who, and the next call uses the usual settings.
    record(db, when=ago(hours=1))
    assert engine_learning.decide(values(), NUMBER, db=db).audio
    assert engine_learning.forget(db, '303-555-0150'.replace('-', ''), actor_id='u-1', actor_name='Dana Admin') == 1
    assert not engine_learning.decide(values(), NUMBER, db=db).audio
    forgotten = [row for row in memory_rows(db) if row['number'] == NUMBER and row['forgotten_at']]
    assert [row['forgotten_by_name'] for row in forgotten] == ['Dana Admin']
    assert engine_learning.forget(db, NUMBER) == 0


def test_a_trunk_or_engine_change_starts_learning_again_and_changing_back_does_not_bring_it_back(db, tmp_path):
    version = tmp_path / 'asterisk' / 'engine-version'
    version.parent.mkdir(parents=True)
    version.write_text('Asterisk 22.11.0 patches aaaaaaaaaaaaaaaa\n')
    settings = values(tmp_path)
    record(db)
    first = engine_learning.current_epoch(db, settings)
    assert engine_learning.decide(settings, NUMBER, db=db).audio
    # A changed patch (the image's digest of patches/) starts a new epoch; going back does not revive the old one.
    version.write_text('Asterisk 22.11.0 patches bbbbbbbbbbbbbbbb\n')
    assert not engine_learning.decide(settings, NUMBER, db=db).audio
    version.write_text('Asterisk 22.11.0 patches aaaaaaaaaaaaaaaa\n')
    assert not engine_learning.decide(settings, NUMBER, db=db).audio
    current = engine_learning.current_epoch(db, settings)
    assert current['id'] != first['id'] and not current['first']
    # A build Faxbot cannot read right now (Asterisk still starting) keeps the epoch it knew.
    version.unlink()
    assert engine_learning.current_epoch(db, settings)['id'] == current['id']
    # The SSL Fax engine's build counts too (its status file names it).
    status = hylafax_engine.status_path(settings)
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text(json.dumps({'state': 'running', 'version': 'HylaFAX+ 7.0.11 patches cccccccccccccccc'}))
    assert engine_learning.current_epoch(db, settings)['id'] != current['id']
    # A trunk setting that changes how calls negotiate starts again too; a new failure is learned again.
    changed = values(tmp_path, SIP_T38_ERROR_CORRECTION='fec')
    assert engine_learning.config_key(changed) != engine_learning.config_key(settings)
    assert not engine_learning.decide(changed, NUMBER, db=db).audio
    record(db, when=datetime.utcnow() + timedelta(seconds=1))
    assert engine_learning.decide(changed, NUMBER, db=db).audio
    # The caller ID is not one of them: the same trunk keeps what it learned.
    assert engine_learning.config_key(values(tmp_path, SIP_T38_ERROR_CORRECTION='fec',
                                             SIP_TRUNK_CALLER_ID=LINE)) == engine_learning.config_key(changed)


def test_received_calls_from_a_caller_whose_t38_failed_are_answered_with_audio(db):
    from api.tests.test_screening import FakeAsterisk
    record(db, direction='inbound')
    engine_learning.learn_recent(db, values())
    store = engine_frames.FrameStore(db)
    asterisk = FakeAsterisk({'/faxbot-inmode/19995550000': 'audio'})
    asyncio.run(engine_frames.sync(asterisk, store, db, values()))
    keys = dict(asterisk.database)
    assert keys['/faxbot-inmode/13035550150'] == 'audio' and keys['/faxbot-inmode/3035550150'] == 'audio'
    assert '/faxbot-inmode/19995550000' not in keys
    # Sending to that number is unchanged: memory is per direction.
    assert not engine_learning.decide(values(), NUMBER, db=db).audio
    engine_learning.forget(db, NUMBER)
    asyncio.run(engine_frames.sync(asterisk, store, db, values()))
    assert not any(key.startswith('/faxbot-inmode/') for key in asterisk.database)


# -- stepping down, and the T9 rule ---------------------------------------------------------------

def _rate_call(first, lowest, ftt, hour, status='SUCCESS'):
    return {'direction': 'out', 'rate_first': first, 'rate_lowest': lowest, 'ftt': ftt, 'status': status,
            'created_at': datetime(2026, 10, 1) + timedelta(hours=hour)}


def test_the_starting_speed_steps_down_one_speed_and_stops_only_at_the_floor():
    history = [_rate_call(14400, 9600, 2, 0), _rate_call(14400, 9600, 1, 1)]
    assert engine_frames.learn_rate(history)[0] == 9600
    history.append(_rate_call(9600, 7200, 1, 2))
    rate, reason, _ = engine_frames.learn_rate(history)
    assert rate == 7200 and reason == ('Faxes to this number failed to train at 9,600 bit/s too, so Faxbot starts at '
                                       '7,200 bit/s.')
    history.append(_rate_call(7200, 4800, 1, 3, status='FAILED'))
    assert engine_frames.learn_rate(history)[0] == 4800
    history.append(_rate_call(4800, 2400, 1, 4, status='FAILED'))
    rate, reason, note = engine_frames.learn_rate(history)
    assert rate is None and reason == '' and 'even at 4,800 bit/s' in note
    # Stopped at the floor: later failures change nothing until the evidence ages out.
    history.append(_rate_call(14400, 9600, 2, 5))
    history.append(_rate_call(14400, 9600, 2, 6))
    assert engine_frames.learn_rate(history)[0] is None


def test_the_ssl_fax_engines_own_reports_lower_its_starting_speed(installation):
    db = installation
    for hours in (3, 2):
        key = record(db, status='SUCCESS', pages=2, when=ago(hours=hours))
        engine_call(db, key, when=ago(hours=hours), rate_first=14400, rate_lowest=9600, trainings=2,
                    compression='JBIG', ecm='on')
    settings = values()
    call = hylafax_engine.call_settings(settings, NUMBER, engine=True)
    assert call.max_rate == 9600 and call.learned.reasons[0].startswith('Faxes to this number failed to train at '
                                                                        '14,400 bit/s')
    # The number's own limit (a person's) is lower: it wins, and nothing is said about it.
    limited = hylafax_engine.call_settings(settings, NUMBER, engine=True, recipient={'max_rate': 4800})
    assert limited.max_rate == 4800 and not limited.learned.max_rate


def _view(when, *, engine='hylafax', compression='JBIG', status='FAILED', verdict='remote_fax_failed', ecm='on',
          far_ecm=None, mode='t38'):
    return {'engine': engine, 'answered': True, 'compression': compression, 'status': status,
            'verdict': verdict if status == 'FAILED' else 'sent', 'when': datetime(2026, 10, 1) + timedelta(hours=when),
            'ecm': ecm, 'far_ecm': far_ecm, 'mode': mode}


def test_compression_steps_to_a_more_robust_one_after_repeated_failures_on_the_ssl_fax_engine_only():
    two = [_view(0), _view(1)]
    assert engine_learning.compression_rule(two, 'jbig') == (None, '')  # fewer than MIN_CALLS answered calls
    history = [_view(0, status='SUCCESS'), _view(1), _view(2)]
    setting, reason = engine_learning.compression_rule(history, 'jbig')
    assert setting == 'mmr' and reason.startswith('Faxes to this number with JBIG compression failed 2 times in a row')
    assert 'the pages are the same' in reason
    # Already at that compression or a more robust one: nothing to change.
    assert engine_learning.compression_rule(history, 'mh') == (None, '')
    # A later call that went through with the compact compression ends it.
    assert engine_learning.compression_rule(history + [_view(3, status='SUCCESS')], 'jbig') == (None, '')
    # MMR failing too steps once more, to MR; the built-in engine's calls never count.
    more = history + [_view(3, compression='MMR'), _view(4, compression='MMR')]
    assert engine_learning.compression_rule(more, 'jbig')[0] == 'mr'
    assert engine_learning.compression_rule([dict(view, engine='builtin') for view in history], 'jbig') == (None, '')


def test_error_correction_is_turned_on_where_pages_failed_without_it_and_never_over_a_persons_choice(installation):
    db = installation
    for hours in (4, 3, 2):
        key = record(db, mode='t38', pages=1, when=ago(hours=hours))
        frames(db, key, when=ago(hours=hours), Dis=DIS_ECM, DcsFirst=DCS_MR_NO_ECM, DcsLast=DCS_MR_NO_ECM,
               Status='FAILED')
    # Each failed after the far machine answered with pages lost: only the ECM rule may react (no T.38 memory
    # here is wanted, so forget it first).
    engine_learning.decide(values(SIP_FAX_ECM='false'), NUMBER, db=db)
    engine_learning.forget(db, NUMBER)
    off = values(SIP_FAX_ECM='false')
    call = hylafax_engine.call_settings(off, NUMBER)
    assert call.ecm is True and call.learned.ecm_on
    assert any(reason.startswith('Pages to this number failed 3 times without error correction') for reason in
               call.learned.reasons)
    # The number's own setting says off: a person's choice, kept, and no reason is given.
    kept = hylafax_engine.call_settings(off, NUMBER, recipient={'ecm': False})
    assert kept.ecm is False and not kept.learned.ecm_on
    # A far machine that does not offer error correction: nothing to turn on.
    assert engine_learning.ecm_rule([_view(hour, ecm='off', far_ecm=False) for hour in range(4)]) == ''


def test_error_correction_is_never_turned_off_and_resolution_never_changes(sqlite, monkeypatch):
    """A sweep over random synthetic histories on both engines, with error correction on and off by setting and
    by the number's own limit: no decision ever turns error correction off or changes fine resolution."""
    monkeypatch.setattr(engine_learning, '_database', lambda: sqlite)
    generator = random.Random(20261007)
    for index in range(30):
        number = f'+130355{index:05d}'
        for hour in range(generator.randint(0, 7)):
            mode = generator.choice(('t38', 'audio'))
            status = generator.choice(('SUCCESS', 'FAILED'))
            verdict = generator.choice(('remote_fax_failed', 'no_t38_data_back', 'no_fax_answer'))
            key = record(sqlite, number=number, mode=mode, status=status, verdict=verdict, pages=generator.randint(0, 3),
                         when=ago(hours=hour + 1))
            first = generator.choice((14400, 9600, 7200, 4800))
            if generator.random() < 0.5:
                engine_call(sqlite, key, number=number, when=ago(hours=hour + 1), rate_first=first,
                            rate_lowest=generator.choice([rate for rate in (14400, 9600, 7200, 4800) if rate <= first]),
                            trainings=generator.randint(1, 4), compression=generator.choice(('JBIG', 'MMR', 'MR', 'MH')),
                            ecm=generator.choice(('on', 'off')))
            else:
                dcs = generator.choice((DCS_MR_ECM, DCS_MR_NO_ECM))
                frames(sqlite, key, number=number, when=ago(hours=hour + 1), Dis=generator.choice((DIS_ECM, DIS_NO_ECM)),
                       DcsFirst=dcs, DcsLast=dcs, Status=status, Ftt=str(generator.randint(0, 2)),
                       Rates=generator.choice(('20', '20.24', '24.2c', '2c.08')))
        for ecm_setting in ('true', 'false'):
            settings = values(SIP_FAX_ECM=ecm_setting, SIP_FAX_FINE=generator.choice(('true', 'false')))
            for recipient in (None, {'ecm': True}, {'ecm': False}):
                for engine in (False, True):
                    plain = hylafax_engine.call_settings(settings, number, recipient=recipient, engine=engine, learn=False)
                    learned = hylafax_engine.call_settings(settings, number, recipient=recipient, engine=engine)
                    assert learned.fine == plain.fine, (index, recipient)
                    assert learned.ecm >= plain.ecm, (index, recipient, engine)  # on stays on
                    if recipient and recipient['ecm'] is False:
                        assert learned.ecm is False  # a person's own choice is kept
                    assert learned.max_rate <= plain.max_rate  # a learned speed only ever lowers it


# -- one view per call, and what each call used -------------------------------------------------

def test_one_view_per_call_joins_the_call_record_the_engine_report_and_the_frames(db):
    builtin = record(db, status='SUCCESS', pages=2, when=ago(hours=3))
    frames(db, builtin, when=ago(hours=3), Rates='20.24', Ftt='1', Trainings='2')
    hylafax_records.records_for(db).record_negotiation(direction='outbound', call_key=builtin, engine='builtin',
                                                       values={'rate_last_page': 9600}, number=NUMBER)
    engine = record(db, mode='audio', status='SUCCESS', pages=2, when=ago(hours=2))
    engine_call(db, engine, when=ago(hours=2), rate_first=9600, rate_lowest=9600, trainings=1, compression='JBIG',
                ecm='on', resolution='fine')
    frames(db, 'loose-attempt-1', when=ago(hours=1))
    received = record(db, direction='inbound', status='SUCCESS', pages=1, when=ago(minutes=30))
    views = engine_learning.joined_calls(db, NUMBER)
    assert [view['direction'] for view in views] == ['inbound', 'outbound', 'outbound', 'outbound']
    by_attempt = {view['attempt_id']: view for view in views}
    first = by_attempt[builtin]
    # The built-in engine reports no compression or error correction itself; its frames do (the last DCS).
    assert (first['engine'], first['mode'], first['compression'], first['ecm'], first['resolution']) == \
        ('builtin', 't38', 'MR', 'on', 'fine')
    assert (first['rate_first'], first['rate_lowest'], first['ftt'], first['far_ecm']) == (14400, 9600, 1, True)
    second = by_attempt[engine]
    assert (second['engine'], second['mode'], second['compression'], second['ecm']) == ('hylafax', 'audio', 'JBIG', 'on')
    assert by_attempt['loose-attempt-1']['record'] is None  # frames with no call record are kept
    assert views[0]['number'] == NUMBER and views[0]['record']['call_id'] == received


def test_each_placed_call_keeps_what_it_used_and_the_fax_detail_says_why(installation, monkeypatch):
    db = installation
    record(db)
    client = ami.AMIClient()
    heard = []
    client.on_submission(heard.append)

    async def accepted(fields):
        return None
    monkeypatch.setattr(client, '_send_action', accepted)
    monkeypatch.setattr(ami, 'settings', values(SIP_TRUNK_CALLER_ID=LINE))
    call = hylafax_engine.call_settings(ami.settings, NUMBER)
    asyncio.run(client.originate_sendfax('job9', NUMBER, '/faxdata/job9.tiff', attempt_id='attempt9', call=call))
    [event] = heard
    assert event['Learned']['audio'] is True and event['Learned']['engine'] == 'builtin'
    assert engine_learning.record_choice(db, event) is True and engine_learning.record_choice(db, event) is False
    records = hylafax_records.records_for(db)
    records.record_call(direction='outbound', call_key='attempt9', engine='builtin', job_id='job9',
                        reason=hylafax_engine.NOT_RUNNING, number=NUMBER)
    detail = records.sent_detail('job9')
    assert detail['changes'] and detail['changes'][0].startswith('Fax over IP (T.38) to this number failed on ')
    # A call nothing changed for keeps no row and says nothing.
    plain = hylafax_engine.call_settings(ami.settings, OTHER)
    asyncio.run(client.originate_sendfax('job10', OTHER, '/faxdata/job10.tiff', attempt_id='attempt10', call=plain))
    assert 'Learned' not in heard[-1]
    records.record_call(direction='outbound', call_key='attempt10', engine='builtin', job_id='job10',
                        reason=hylafax_engine.NOT_RUNNING, number=OTHER)
    assert records.sent_detail('job10')['changes'] == []


# -- partner relay identity (Builder AS's direct.relay), on both engines ---------------------------

RELAYED, OWN = 'ab' * 16, 'cd' * 16


@pytest.fixture
def relayed(monkeypatch):
    """A stand-in for direct.relay that relays job 'relayed1' for a partner; the real one is AS's."""
    module = types.ModuleType('app.direct.relay')
    module.sender_identity_for = lambda engine, job_id: ('Partner Clinic', '+13035550199') if job_id == RELAYED \
        else None
    monkeypatch.setitem(sys.modules, 'app.direct.relay', module)
    return module


def test_without_the_relay_every_fax_carries_its_own_header_and_station(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app.direct.relay', None)  # an import that fails, as before AS merges
    assert ami.sender_identity('job1') is None


def test_a_relayed_fax_carries_the_partners_header_and_station_on_the_built_in_engine(relayed, monkeypatch):
    import base64
    monkeypatch.setattr(ami, '_database', lambda: None)
    trunk = values(SIP_TRUNK_CALLER_ID=LINE, FAX_HEADER='Our Office')
    choice = ami.reply_choice(trunk)
    fields = ami.originate_fields_for(trunk, RELAYED, NUMBER, '/faxdata/relayed1.tiff', choice=choice)
    variables = dict(part.split('=', 1) for part in fields['Variable'].split(',') if '=' in part)
    assert base64.b64decode(variables['FAXHEADER64']).decode() == 'Partner Clinic'
    assert base64.b64decode(variables['FAXSTATION64']).decode() == '+13035550199'
    assert fields['CallerID'] == ami.originate_fields_for(trunk, OWN, NUMBER, '/faxdata/own1.tiff',
                                                          choice=choice)['CallerID']  # caller ID unchanged
    own = dict(part.split('=', 1) for part in ami.originate_fields_for(
        trunk, OWN, NUMBER, '/faxdata/own1.tiff', choice=choice)['Variable'].split(',') if '=' in part)
    assert base64.b64decode(own['FAXHEADER64']).decode() == 'Our Office'


def test_a_relayed_fax_carries_the_partners_header_and_station_on_the_ssl_fax_engine(relayed, monkeypatch):
    monkeypatch.setattr(ami, '_database', lambda: None)
    seen = {}

    def create_job(values, **kwargs):
        seen.update(kwargs)
        return hylafax_engine.PreparedJob(session=None)

    class Asterisk:
        async def db_put(self, family, key, value):
            seen['plan'] = value
    monkeypatch.setattr(hylafax_engine, 'create_job', create_job)
    trunk = values(SIP_TRUNK_CALLER_ID=LINE, FAX_HEADER='Our Office')
    settings = hylafax_engine.call_settings(trunk, NUMBER, engine=True, learn=False)
    asyncio.run(hylafax_engine.prepare_job(trunk, Asterisk(), job_id=RELAYED, attempt_id='b' * 32, dest=NUMBER,
                                           tiff_path='/faxdata/relayed1.tiff', settings=settings))
    assert seen['header'] == 'Partner Clinic' and seen['station'] == '+13035550199'
    asyncio.run(hylafax_engine.prepare_job(trunk, Asterisk(), job_id=OWN, attempt_id='c' * 32, dest=NUMBER,
                                           tiff_path='/faxdata/own1.tiff', settings=settings))
    assert seen['header'] == 'Our Office'


# -- the far end's internet address, whole (patch 0004 keeps up to 83 octets of a CSA) ------------------

# An SSL Fax engine's CSA as T.30 5.3.6.2.12 lays it out (spandsp's own example has the same shape): sequence 0,
# type 2 (URL), the length, then the address. 45 octets in all: longer than the 32 every other frame is cut at.
SSL_ADDRESS = b'ssl://Synthetic1Pass@198.51.100.7:10443'
LONG_CSA = bytes([0xFF, 0x03, 0x24, 0x00, 0x02, len(SSL_ADDRESS)]) + SSL_ADDRESS


def test_a_long_internet_address_is_kept_whole_and_read_as_t30_lays_it_out(sqlite):
    assert len(LONG_CSA) == 45 > engine_frames.FRAME_MAX
    row = engine_frames.parse_event({'Direction': 'out', 'JobID': 'job1', 'AttemptID': 'attempt1', 'Mode': 'T38',
                                     'Status': 'SUCCESS', 'Csa': LONG_CSA.hex(), 'Tsa': LONG_CSA.hex()})
    assert row['csa_full'] == LONG_CSA.hex() and row['csa'] == LONG_CSA[:32].hex()
    assert row['tsa_full'] == LONG_CSA.hex()
    assert engine_frames.far_address(row) == {'type': 2, 'address': SSL_ADDRESS.decode()}
    store = engine_frames.FrameStore(sqlite)
    row['number'] = NUMBER
    assert store.record(row)
    [kept] = store.calls(NUMBER)
    assert kept['csa_full'] == LONG_CSA.hex()
    assert f'The far end gave its internet address: {SSL_ADDRESS.decode()}.' in engine_frames.describe(kept)
    # Calls recorded before 0048 have only the first 32 octets: still read, cut short.
    assert engine_frames.far_address({'csa': LONG_CSA[:32].hex()})['address'].startswith('ssl://Synthetic1Pass@')
    # Longer than T.30 allows (more than 77 address octets): cut at 83 octets, as the patch itself cuts it.
    too_long = bytes([0xFF, 0x03, 0x24, 0x00, 0x02, 0x7F]) + b'x' * 78
    cut = engine_frames.parse_event({'Direction': 'out', 'AttemptID': 'a2', 'Csa': too_long.hex()})['csa_full']
    assert cut == too_long[:engine_frames.ADDRESS_MAX].hex()


# -- the dialplan and the engine builds ------------------------------------------------------------

def _context(name):
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    match = re.search(rf'^\[{re.escape(name)}\]\n(.*?)(?=^\[|\Z)', text, re.S | re.M)
    assert match, name
    return [line for line in match.group(1).splitlines() if line.strip() and not line.startswith(';')]


def test_audio_for_one_sent_call_uses_sendfax_f_and_the_audio_speed():
    lines = _context('faxbot-send')
    text = '\n'.join(lines)
    assert '"${FAXBOT_AUDIO}" = "yes"]?9600:14400' in text
    jump = next(i for i, line in enumerate(lines) if '"${FAXBOT_AUDIO}" = "yes"]?audio' in line)
    usual = next(i for i, line in enumerate(lines) if 'SendFAX(${FAXFILE},fz)' in line)
    audio = next(i for i, line in enumerate(lines) if '(audio),SendFAX(${FAXFILE},F)' in line)
    assert jump < usual < audio and 'Hangup()' in lines[usual + 1] and 'Hangup()' in lines[audio + 1]


def test_a_callers_mode_is_read_after_the_junk_screen_so_a_blocked_caller_is_never_answered():
    lines = _context('faxbot-inbound-receive')
    position = {name: next(i for i, line in enumerate(lines) if marker in line)
                for name, marker in (('screen', 'Gosub(faxbot-screen,s,1)'), ('screened', '?screened)'),
                                     ('mode', 'Gosub(faxbot-caller-mode,s,1)'), ('engine', 'Gosub(faxbot-engine-in'),
                                     ('answer', 'Answer()'), ('receive', 'ReceiveFAX('))}
    assert position['screen'] < position['screened'] < position['mode'] < position['engine'] < position['answer']
    assert '${IF($["${FAXBOT_IN_AUDIO}" = "yes"]?F:f)}' in lines[position['receive']]
    mode = '\n'.join(_context('faxbot-caller-mode'))
    assert 'DB_EXISTS(faxbot-inmode/${FAXBOT_SCREEN_KEY})' in mode and 'Set(FAXBOT_IN_AUDIO=yes)' in mode
    assert any('"${FAXBOT_IN_AUDIO}" = "yes"]?dial' in line for line in _context('faxbot-engine-in'))


def _patch_digest(folder):
    """What the images compute: sha256 of every file in patches/ (sorted, as the shell glob lists them)."""
    data = b''.join(path.read_bytes() for path in sorted(folder.iterdir()) if not path.name.startswith('.'))
    return hashlib.sha256(data).hexdigest()[:16]


def test_each_image_writes_its_build_with_a_digest_of_its_patches(tmp_path):
    for image, release in (('asterisk', 'Asterisk 22.11.0'), ('hylafax', 'HylaFAX+ 7.0.11')):
        dockerfile = (ROOT / image / 'Dockerfile').read_text()
        assert f"printf '{release} patches %s\\n' \"$(cat /usr/src/patches/* | sha256sum | cut -c1-16)\"" in dockerfile
        assert '/opt/install/usr/share/faxbot/engine-version' in dockerfile
    # The digest follows any change to a patch.
    folder = tmp_path / 'patches'
    folder.mkdir()
    (folder / '0001-a.patch').write_text('one\n')
    (folder / 'b.h').write_text('two\n')
    before = _patch_digest(folder)
    (folder / 'b.h').write_text('two, changed\n')
    assert _patch_digest(folder) != before


def test_asterisk_start_copies_its_build_for_faxbot_and_removes_a_stale_one(tmp_path):
    from api.tests.test_asterisk_start import start
    build = tmp_path / 'engine-version'
    build.write_text('Asterisk 22.11.0 patches 0123456789abcdef\n')
    result, _, shared = start(tmp_path, FAXBOT_ENGINE_VERSION_FILE=str(build))
    assert result.returncode == 0, result.stderr
    assert (shared / 'engine-version').read_text() == 'Asterisk 22.11.0 patches 0123456789abcdef\n'
    result, _, shared = start(tmp_path, FAXBOT_ENGINE_VERSION_FILE=str(tmp_path / 'missing'))
    assert result.returncode == 0 and not (shared / 'engine-version').exists()


@pytest.mark.native
@pytest.mark.skipif(not (os.environ.get('FAXBOT_SSLFAX_PROOF') == '1' and os.environ.get('FAXBOT_NATIVE_IMAGE')),
                    reason='Set FAXBOT_SSLFAX_PROOF=1 and FAXBOT_NATIVE_IMAGE to read a built image.')
def test_the_built_asterisk_image_names_the_digest_of_this_checkouts_patches():
    context = os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh')
    found = subprocess.run(['docker', '--context', context, 'run', '--rm', '--entrypoint', 'cat',
                            os.environ['FAXBOT_NATIVE_IMAGE'], '/usr/share/faxbot/engine-version'],
                           capture_output=True, text=True, timeout=120)
    assert found.returncode == 0, found.stderr
    assert found.stdout.strip() == f'Asterisk 22.11.0 patches {_patch_digest(ROOT / "asterisk" / "patches")}'


# -- the console's and the command line's API ------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def test_their_fax_machine_joins_both_engines_calls_and_forgets_with_an_audit_row(client):
    from api.tests.test_access_management_http import B
    from app.main import app
    engine = app.state.configuration_runtime.manager.store.engine
    sent = record(engine, when=ago(hours=3))
    frames(engine, sent, when=ago(hours=3), Status='FAILED')
    fast = record(engine, mode='audio', status='SUCCESS', pages=2, when=ago(hours=1))
    engine_call(engine, fast, when=ago(hours=1), rate_first=9600, rate_lowest=9600, trainings=1, compression='JBIG',
                ecm='on', resolution='fine')
    body = client.get('/fax-machines/numbers/303-555-0150', headers=B).json()
    assert [(call['engine'], call['mode_label']) for call in body['calls']] == [
        ('hylafax', 'Audio fax'), ('builtin', 'Fax over IP (T.38)')]
    assert body['calls'][0]['engine_label'] == "Faxbot's fast fax service"
    assert body['calls'][0]['outcome'] == 'Sent: 2 pages confirmed by the receiving machine.'
    assert body['calls'][1]['outcome'].startswith('The other fax machine answered but the fax failed')
    assert "The far end's fax machine accepts up to 14,400 bit/s" in body['calls'][1]['sentences'][0]
    # Reading changes nothing: what the failure taught is kept when a call is placed or by the background work.
    assert body['memory'] == [] and not body['can_forget']
    engine_learning.learn_recent(engine, app.state.configuration_runtime.manager.store.read().active.values)
    body = client.get('/fax-machines/numbers/' + NUMBER, headers=B).json()
    assert body['learned']['audio'] is True and body['can_forget'] is True
    assert body['learned']['sentences'][0].startswith('Fax over IP (T.38) to this number failed on ')
    assert [row['kind'] for row in body['memory']] == ['t38_failed']
    assert client.post(f'/fax-machines/numbers/{NUMBER}/forget').status_code in (401, 403)
    forgot = client.post(f'/fax-machines/numbers/{NUMBER}/forget', headers=B)
    assert forgot.status_code == 200 and forgot.json()['forgotten'] == 1
    assert forgot.json()['sentence'] == f'Faxbot forgot what failed with {NUMBER}; its next calls use the usual settings.'
    again = client.post(f'/fax-machines/numbers/{NUMBER}/forget', headers=B).json()
    assert again['forgotten'] == 0 and again['sentence'] == engine_learning.FORGET_NOTHING
    body = client.get('/fax-machines/numbers/' + NUMBER, headers=B).json()
    assert body['learned']['audio'] is False and not body['can_forget']
    audit = sa.Table('access_audit', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        operations = [row.operation for row in connection.execute(
            sa.select(audit.c.operation).where(audit.c.operation.like('fax_machine.%')))]
    assert operations == ['fax_machine.forget']


def test_the_command_line_shows_what_faxbot_changes_and_forgets_a_number(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    from app.main import app
    for client in _serve(monkeypatch, tmp_path):
        cli = Cli(client)
        engine = app.state.configuration_runtime.manager.store.engine
        record(engine, when=ago(hours=2))
        engine_learning.learn_recent(engine, app.state.configuration_runtime.manager.store.read().active.values)
        shown = cli('recipients', 'fax-machine', NUMBER)
        assert shown.exit_code == 0, (shown.stdout, shown.stderr)
        out = ' '.join(shown.stdout.split())
        assert 'Sent, Fax over IP (T.38). The other fax machine answered but the fax failed' in out
        assert 'What Faxbot changes for this number: Fax over IP (T.38) to this number failed on ' in out
        assert f'To forget what failed with this number: faxbot recipients fax-machine {NUMBER} --forget' in out
        forgot = cli('recipients', 'fax-machine', NUMBER, '--forget')
        assert forgot.exit_code == 0 and 'its next calls use the usual settings' in forgot.stdout
        assert cli.json('recipients', 'fax-machine', NUMBER)['can_forget'] is False
