"""What each fax call negotiated (migration 0023): every engine's report parsed from synthetic logs and events,
unknown kept unknown, last-page values kept apart from whole-call ones, the summary's arithmetic, and the rule
that recording never changes delivery. SQLite and PostgreSQL."""
import asyncio
import base64
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
import sqlalchemy as sa

from api.app import schema, schema_negotiation
from app import fax_negotiation, hylafax_engine, hylafax_http, hylafax_records, sip_calls
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes
from api.tests.test_hylafax_scripts import QFILE, engine, run  # noqa: F401 - fixture

JOB, ATTEMPT = 'a' * 32, 'b' * 32
NOW = datetime(2026, 10, 6, 9, 0, 0)
PEER = '+15555550199'


def log(*lines, start='10:00:00.00', end='10:01:00.00'):
    """A synthetic HylaFAX session log: the protocol lines HylaFAX+ 7.0.11 writes, with made-up values."""
    body = [f'Oct 06 {start}: [  200]: SESSION BEGIN 000000011 15555550199',
            'Oct 06 10:00:00.01: [  200]: HylaFAX (tm) Version 7.0.11']
    body += [f'Oct 06 10:00:{10 + index // 10:02d}.{index % 10}0: [  200]: {line}' for index, line in enumerate(lines)]
    if end:
        body.append(f'Oct 06 {end}: [  200]: SESSION END')
    return '\n'.join(body) + '\n'


SENT = log(
    'REMOTE CSI "+1 555 555 0199"', 'REMOTE best rate 14400 bit/s', 'REMOTE max A4 page width (215 mm)',
    'REMOTE supports T.30 Annex A, 256-byte ECM', 'USE 14400 bit/s', 'USE error correction mode',
    'SEND file "docq/doc12.tif"', 'USE A4 page width (215 mm)', 'USE unlimited page length', 'USE 7.7 line/mm',
    'USE 2-D MMR', 'USE 0ms/scanline',
    'SEND training at v.17 14400 bit/s', 'TRAINING failed', 'SEND training at v.17 12000 bit/s', 'TRAINING failed',
    'SEND training at v.29 9600 bit/s', 'TRAINING succeeded',
    'SEND FAX (000000011): FROM x TO 15555550199 (page 1 of 2 sent in 0:00:13)',
    # The other machine asked for a retrain after page 1: page 2 goes slower.
    'SEND training at v.29 7200 bit/s', 'TRAINING succeeded',
    'SEND FAX (000000011): FROM x TO 15555550199 (page 2 of 2 sent in 0:00:12)',
    'SEND FAX (000000011): FROM x TO 15555550199 (docq/doc12.tif;10 sent in 0:00:34)')

RECEIVED = log(
    'RECV FAX: begin', 'RECV recv TSI (sender id)', 'REMOTE TSI "+1 555 555 0199"', 'RECV recv DCS (command signal)',
    'REMOTE wants 9600 bit/s', 'REMOTE wants A4 page width (215 mm)', 'REMOTE wants unlimited page length',
    'REMOTE wants 7.7 line/mm', 'REMOTE wants 2-D MR', 'RECV training at v.29 9600 bit/s', 'TRAINING succeeded',
    'RECV FAX (000000011): from +1 555 555 0199, page 1 in 0:00:20, A4 x INF, 7.7 line/mm, 2-D MR, 9600 bit/s',
    'RECV FAX (000000011): from +1 555 555 0199, page 2 in 0:00:15, A4 x INF, 7.7 line/mm, 2-D MR, 9600 bit/s',
    end='10:00:45.00')


def parsed(spool, environment, text):
    path = spool / 'log' / 'c000000011'
    path.write_text(text)
    result = run('negotiation', environment, str(path))
    assert result.returncode == 0, result.stderr
    return json.loads(base64.b64decode(result.stdout)) if result.stdout else None


# The SSL Fax engine's session log -------------------------------------------------------------------------------

def test_a_sent_call_reports_its_whole_call_speeds_compression_resolution_and_error_correction(engine):
    spool, _, _, environment = engine
    assert parsed(spool, environment, SENT) == {
        'rate_first': 14400, 'rate_lowest': 7200, 'rate_last': 7200, 'trainings': 4, 'compression': 'MMR',
        'resolution': 'fine', 'ecm': 'on', 'session': 60}


def test_a_received_call_reports_each_pages_values_and_no_error_correction_when_none_was_asked(engine):
    spool, _, _, environment = engine
    assert parsed(spool, environment, RECEIVED) == {
        'rate_first': 9600, 'rate_lowest': 9600, 'rate_last': 9600, 'trainings': 1, 'compression': 'MR',
        'resolution': 'fine', 'ecm': 'off', 'session': 45}


def test_pages_that_differ_are_mixed_and_the_senders_own_id_is_never_read(engine):
    spool, _, _, environment = engine
    values = parsed(spool, environment, log(
        'REMOTE wants 9600 bit/s', 'REMOTE wants T.30 Annex A, 256-byte ECM', 'RECV training at v.29 9600 bit/s',
        'TRAINING succeeded',
        # The sender's ID is free text: commas and look-alike values in it change nothing.
        'RECV FAX (000000011): from A, page 9 in 0:00:01, 15.4 line/mm, JBIG, 2400 bit/s, page 1 in 0:00:20, '
        'A4 x INF, 3.85 line/mm, 2-D MMR, 9600 bit/s',
        'REMOTE wants 9600 bit/s', 'RECV training at v.29 9600 bit/s', 'TRAINING succeeded',
        'RECV FAX (000000011): from A, page 2 in 0:00:15, A4 x INF, 7.7 line/mm, 2-D MMR, 9600 bit/s'))
    assert values['resolution'] == 'mixed' and values['compression'] == 'MMR' and values['ecm'] == 'mixed'


def test_a_call_that_never_trained_reports_only_its_tries(engine):
    spool, _, _, environment = engine
    values = parsed(spool, environment, log('USE 14400 bit/s', 'USE 2-D MMR', 'SEND training at v.17 14400 bit/s',
                                            'TRAINING failed', 'SEND training at v.17 12000 bit/s', 'TRAINING failed',
                                            end=None))
    assert values == {'rate_first': None, 'rate_lowest': None, 'rate_last': None, 'trainings': 2,
                      'compression': None, 'resolution': None, 'ecm': None, 'session': None}
    assert fax_negotiation.engine_values(base64.b64encode(json.dumps(values).encode()).decode()) == {'trainings': 2}


def test_no_session_log_reports_nothing(engine):
    spool, _, _, environment = engine
    result = run('negotiation', environment, str(spool / 'log' / 'c000000099'))
    assert result.returncode == 0 and result.stdout == ''


def test_notify_sends_the_negotiation_with_the_job_result(engine, tmp_path):
    spool, _, _, environment = engine
    (spool / 'doneq' / 'q12').write_text(QFILE.replace('commid:000000007', 'commid:000000011'))
    (spool / 'log' / 'c000000011').write_text(SENT)
    (tmp_path / 'answer').write_text('200')
    assert run('notify', environment, 'doneq/q12', 'done', '0:00:41', cwd=spool).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert fax_negotiation.engine_values(body['negotiation_b64']) == {
        'rate_first': 14400, 'rate_lowest': 7200, 'rate_last_page': 7200, 'trainings': 4, 'compression': 'MMR',
        'resolution': 'fine', 'ecm': 'on', 'session_seconds': 60}


def test_a_received_fax_carries_its_negotiation_through_the_ticket_and_the_hand_over(engine, tmp_path):
    spool, state, _, environment = engine
    (spool / 'log' / 'c000000007').write_text(RECEIVED)
    assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '', '+15555550199',
               '179117219142.15555550100', '', cwd=spool).returncode == 0
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    values = fax_negotiation.engine_values(body['engine']['negotiation_b64'])
    assert values['compression'] == 'MR' and values['rate_lowest'] == 9600 and values['session_seconds'] == 45


# What Faxbot keeps -------------------------------------------------------------------------------------------

@pytest.mark.parametrize('encoded', [None, '', 'not base64!', base64.b64encode(b'[1, 2]').decode(),
                                     base64.b64encode(b'{"rate_first": "fast"}').decode()])
def test_anything_but_the_engines_own_report_is_not_reported(encoded):
    assert fax_negotiation.engine_values(encoded) == {}


def test_impossible_speeds_and_unknown_names_are_dropped_never_guessed():
    report = {'rate_first': 9600, 'rate_lowest': 14400, 'rate_last': 1234, 'trainings': 0, 'compression': 'zip',
              'resolution': 'retina', 'ecm': True, 'session': -4}
    assert fax_negotiation.engine_values(base64.b64encode(json.dumps(report).encode()).decode()) == {}


@pytest.mark.parametrize('rate, resolution, pages, expected', [
    ('14400', '8031x7700', '2', {'rate_last_page': 14400, 'resolution_last_page': 'fine'}),
    ('9600', '8031x3850', '1', {'rate_last_page': 9600, 'resolution_last_page': 'standard'}),
    ('14400', '8031x15400', '3', {'rate_last_page': 14400, 'resolution_last_page': 'superfine'}),
    # No page confirmed: spandsp's starting speed and an empty image are not measurements.
    ('14400', '0x0', '0', {}), ('14400', '8031x7700', '', {}), ('', '', '2', {}),
    ('14401', '0x0', '2', {}),
])
def test_the_built_in_engine_reports_the_last_pages_speed_and_resolution_only_after_a_page(rate, resolution, pages,
                                                                                         expected):
    assert fax_negotiation.builtin_values(rate, resolution, pages) == expected


@pytest.fixture
def installation(database):
    schema.upgrade_schema(database)
    return sip_calls.SipCallRecords(database), hylafax_records.records_for(database)


def epoch(moment):
    return str(int((moment - datetime(1970, 1, 1)).total_seconds()))


def fax_result(**fields):
    return {'JobID': JOB, 'AttemptID': ATTEMPT, 'Status': 'SUCCESS', 'Error': '', 'Pages': '2', 'Mode': 'T38',
            'Answered': epoch(NOW), 'Ended': epoch(NOW + timedelta(seconds=58)), 'Rate': '14400',
            'Resolution': '8031x7700', **fields}


def row(database, direction, call_key):
    table = sa.Table('fax_engine_calls', sa.MetaData(), autoload_with=database)
    with database.connect() as connection:
        found = connection.execute(sa.select(table).where(table.c.direction == direction,
                                                          table.c.call_key == call_key)).mappings().first()
    return dict(found) if found else None


def built_in_send(calls, records, monkeypatch):
    monkeypatch.setattr(sip_calls, '_current', calls)
    calls.record_submission({'JobID': JOB, 'AttemptID': ATTEMPT, 'Called': PEER, 'CallerID': '+15555550100'},
                            now=NOW)
    records.record_call(direction='outbound', call_key=ATTEMPT, job_id=JOB, engine='builtin', number=PEER,
                        reason=hylafax_engine.NOT_RUNNING, now=NOW)


def test_a_built_in_send_keeps_last_page_values_apart_and_says_so(installation, database, monkeypatch):
    calls, records = installation
    built_in_send(calls, records, monkeypatch)
    sip_calls._on_fax_result(fax_result())
    kept = row(database, 'outbound', ATTEMPT)
    assert kept['negotiation_by'] == 'builtin'
    assert (kept['rate_last_page'], kept['resolution_last_page']) == (14400, 'fine')
    # Whole-call values stay unknown: this engine never reports them.
    assert all(kept[name] is None for name in ('rate_first', 'rate_lowest', 'trainings', 'compression',
                                                'resolution', 'ecm', 'transfer_seconds', 'session_seconds'))
    detail = records.sent_detail(JOB)['negotiation']
    assert detail['sentence'] == ('The last page went at 14400 bit/s and had fine resolution; compression and '
                                  'error correction are not reported by this engine; 2 pages in a 58 s call.')
    assert detail['engine'] == 'builtin' and detail['rate_first'] is None and detail['ecm'] is None


def test_a_built_in_call_with_no_page_keeps_every_value_unknown(installation, database, monkeypatch):
    calls, records = installation
    built_in_send(calls, records, monkeypatch)
    sip_calls._on_fax_result(fax_result(Status='FAILED', Error='T30_ERR_RX_NOCARRIER', Pages='0'))
    kept = row(database, 'outbound', ATTEMPT)
    assert kept['negotiation_by'] is None and kept['rate_last_page'] is None and kept['resolution_last_page'] is None
    assert records.sent_detail(JOB)['negotiation']['sentence'] == (
        'Speed, compression and error correction were not reported by this engine; no pages confirmed in a 58 s '
        'call.')


def test_a_report_is_kept_once_and_never_rewritten_by_a_repeat_or_another_engine(installation, database,
                                                                                monkeypatch):
    calls, records = installation
    built_in_send(calls, records, monkeypatch)
    sip_calls._on_fax_result(fax_result())
    before = row(database, 'outbound', ATTEMPT)
    sip_calls._on_fax_result(fax_result(Rate='9600', Resolution='8031x3850'))
    records.record_negotiation(direction='outbound', call_key=ATTEMPT, engine='hylafax',
                               values={'rate_first': 9600, 'rate_lowest': 9600, 'compression': 'MH'})
    after = row(database, 'outbound', ATTEMPT)
    assert {name: after[name] for name in fax_negotiation.COLUMNS} == {
        name: before[name] for name in fax_negotiation.COLUMNS}


def test_a_fax_the_built_in_engine_received_gets_its_own_engine_record(installation, database):
    calls, records = installation
    call = {'did': '+15555550100', 'caller': PEER, 'started_at': epoch(NOW), 'answered_at': epoch(NOW),
            'ended_at': epoch(NOW + timedelta(seconds=44)), 'pages': 1, 't38': True, 'rate': 9600,
            'resolution': '8031x3850'}
    sip_calls.record_inbound_call(database, call, call_id='1791083644.7', inbound_fax_id='fax-1', fax_status='SUCCESS')
    kept = row(database, 'inbound', '1791083644.7')
    assert kept['engine'] == 'builtin' and kept['job_id'] == 'fax-1' and kept['negotiation_by'] == 'builtin'
    assert (kept['rate_last_page'], kept['resolution_last_page']) == (9600, 'standard')
    view = fax_negotiation.received_view(database, 'fax-1')
    assert view['sentence'] == ('The last page went at 9600 bit/s and had standard resolution; compression and '
                                'error correction are not reported by this engine; 1 page in a 44 s call.')
    # An SSL Fax engine call is reported through its own hand-over, never as the built-in engine.
    sip_calls.record_inbound_call(database, {**call, 'rate': None}, call_id='engine.179117219142',
                                  inbound_fax_id='fax-2', fax_status='SUCCESS')
    assert row(database, 'inbound', 'engine.179117219142') is None


def test_a_fax_the_ssl_fax_engine_received_records_its_whole_call_values(installation, database):
    calls, records = installation
    calls.record_engine_receive('engine.179117219142', success=True, pages=2, did='+15555550100', caller=PEER,
                                inbound_fax_id='fax-2', now=NOW)
    encoded = base64.b64encode(json.dumps({'rate_first': 14400, 'rate_lowest': 9600, 'rate_last': 9600,
                                           'trainings': 2, 'compression': 'MR', 'resolution': 'fine', 'ecm': 'off',
                                           'session': 51}).encode()).decode()
    payload = {'engine': {'engine': 'hylafax', 'engine_ref': '0123456789abcdef:000000011-1791180000',
                          'sslfax': False, 'transfer_seconds': 35, 'negotiation_b64': encoded}}
    hylafax_engine.record_inbound_engine(database, payload, call_key='engine.179117219142', inbound_fax_id='fax-2',
                                         number=PEER)
    kept = row(database, 'inbound', 'engine.179117219142')
    assert kept['negotiation_by'] == 'hylafax' and kept['session_seconds'] == 51 and kept['rate_last_page'] == 9600
    assert fax_negotiation.received_view(database, 'fax-2')['sentence'] == (
        'The call used MR compression without error correction starting at 14400 bit/s and dropping to 9600 bit/s, '
        'fine resolution; 2 pages in 35 s.')


@pytest.mark.parametrize('values, sentence', [
    ({'rate_first': 14400, 'rate_lowest': 14400, 'compression': 'MMR', 'ecm': 'on', 'resolution': 'fine'},
     'The call used MMR compression with error correction at 14400 bit/s, fine resolution; 2 pages in 41 s.'),
    ({'rate_first': 9600, 'rate_lowest': 9600, 'resolution': 'mixed'},
     'The call ran at 9600 bit/s (compression and error correction not reported by this engine), more than one '
     'resolution; 2 pages in 41 s.'),
    ({'trainings': 3}, 'The call tried 3 times to agree a speed with the other fax machine and never did; 2 pages '
                       'in 41 s.'),
    ({}, 'Speed, compression and error correction were not reported by this engine; 2 pages in 41 s.'),
])
def test_the_detail_sentence_names_what_is_known_and_what_this_engine_does_not_report(values, sentence):
    assert fax_negotiation.call_sentence({**values, 'transfer_seconds': 41}, {'pages': 2}) == sentence


# The summary ---------------------------------------------------------------------------------------------

def add_call(database, *, direction='outbound', job=None, status='SUCCESS', pages=2, seconds=40, answered=True,
             when=NOW, **values):
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=database)
              for name in ('fax_engine_calls', 'sip_call_records')}
    key = uuid4().hex
    with database.begin() as connection:
        connection.execute(tables['sip_call_records'].insert().values(
            id=uuid4().hex, direction=direction, call_id=key, job_id=job, attempt_id=key if job else None,
            started_at=when, answered_at=when if answered else None, ended_at=when, connected_seconds=seconds,
            disposition='answered' if answered else 'busy', t38='yes', pages=pages, fax_status=status,
            fax_preference=0, created_at=when, updated_at=when))
        connection.execute(tables['fax_engine_calls'].insert().values(
            id=uuid4().hex, direction=direction, call_key=key, job_id=job,
            engine=values.pop('engine', 'hylafax'), created_at=when, updated_at=when, **values))


def test_the_summary_counts_calls_success_seconds_per_page_and_attempts_per_delivered_fax(installation, database):
    clean = {'negotiation_by': 'hylafax', 'compression': 'MMR', 'ecm': 'on', 'rate_first': 14400, 'rate_lowest': 14400}
    slow = {'negotiation_by': 'hylafax', 'compression': 'MR', 'ecm': 'off', 'rate_first': 14400, 'rate_lowest': 9600}
    # Fax x: failed slow, then delivered clean (2 attempts). Fax y: delivered clean at once.
    add_call(database, job='x', status='FAILED', pages=0, seconds=30, **slow)
    add_call(database, job='x', pages=2, seconds=40, **clean)
    add_call(database, job='y', pages=3, seconds=50, **clean)
    add_call(database, job='z', status='FAILED', pages=0, seconds=20, **clean)
    # Received: counted in calls and success, never in attempts per delivered fax.
    add_call(database, direction='inbound', pages=1, seconds=25, **clean)
    # The built-in engine: its last page's speed, a group of its own.
    add_call(database, job='w', pages=1, seconds=60, engine='builtin', negotiation_by='builtin', rate_last_page=14400)
    # Nothing reported: its own row, not dropped.
    add_call(database, job='v', status='FAILED', pages=0, seconds=15)
    # Not counted: unanswered, or older than the period.
    add_call(database, job='u', status='FAILED', pages=0, seconds=0, answered=False, **clean)
    add_call(database, job='t', pages=9, seconds=90, when=NOW - timedelta(days=40), **clean)
    result = fax_negotiation.summary(database, days=30, now=NOW + timedelta(minutes=1))
    assert result['calls'] == 7 and result['measured_calls'] == 6
    assert result['sentence'] == ('Measured on 6 calls in the last 30 days; the engine reported nothing for 1 more '
                                  'call.')
    groups = {(group['coding_label'], group['speed_label']): group for group in result['groups']}
    best = groups[('MMR compression with error correction', '14400 bit/s')]
    assert (best['calls'], best['sent'], best['received'], best['delivered']) == (4, 3, 1, 3)
    assert best['success_percent'] == 75
    # (40 + 50 + 20 + 25) s over 2 + 3 + 0 + 1 pages, the failed call's time included.
    assert best['seconds_per_page'] == 22.5
    # x took 2 calls, y took 1.
    assert best['attempts_per_delivered'] == 1.5
    worse = groups[('MR compression without error correction', '9600 bit/s')]
    assert (worse['calls'], worse['delivered'], worse['seconds_per_page'], worse['attempts_per_delivered']) == (
        1, 0, None, None)
    built_in = groups[('not reported by this engine', '14400 bit/s on the last page')]
    assert built_in['seconds_per_page'] == 60.0 and built_in['attempts_per_delivered'] == 1.0
    unknown = groups[('not reported by this engine', 'not reported by this engine')]
    assert unknown['calls'] == 1 and unknown['success_percent'] == 0
    assert result['note'] == fax_negotiation.MEASURE_ONLY
    with pytest.raises(ValueError):
        fax_negotiation.summary(database, days=12)


def test_an_empty_period_says_so(installation, database):
    result = fax_negotiation.summary(database, days=7, now=NOW)
    assert result['groups'] == [] and result['sentence'] == 'No answered fax calls on your phone line in the last 7 days.'


# Recording never changes delivery ------------------------------------------------------------------------------

class ResultStore:
    def __init__(self):
        self.calls = []

    def attempt_context(self, job_id, attempt_id):
        return None, SimpleNamespace(id='profile-1', configuration=SimpleNamespace(provider_id='sip', manifest=None))

    def record_unconfirmed(self, job_id, **fields):
        self.calls.append(('uncertain', job_id, fields['attempt_id']))
        return True

    def observe(self, job_id, **fields):
        self.calls.append(('observed', fields['status'], fields['error'], fields['error_category']))
        return True


@pytest.mark.parametrize('why, pages', [('done', 2), ('failed', 1), ('failed', 0)])
def test_the_engines_negotiation_never_changes_a_sent_faxs_outcome(installation, database, monkeypatch, why, pages):
    from app import audit
    from app.routing import background
    monkeypatch.setattr(hylafax_http, '_require_engine', lambda secret: None)
    monkeypatch.setattr(hylafax_http, '_record', lambda *args: None)
    monkeypatch.setattr(audit, 'audit_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(background, 'installation_engine', lambda app: (database, None))
    payload = {'tag': f'{JOB}.{ATTEMPT}', 'why': why, 'dials': 1, 'pages': pages, 'engine_id': '0123456789abcdef',
               'commid': '000000011', 'status_b64': base64.b64encode(b'No response to MPS repeated 3 tries').decode()}
    outcomes = []
    for variant in ('without', 'with', 'failing'):
        store = ResultStore()
        monkeypatch.setattr(hylafax_http, '_store', lambda request: store)
        body = dict(payload)
        if variant != 'without':
            body['negotiation_b64'] = base64.b64encode(json.dumps(
                {'rate_first': 14400, 'rate_lowest': 2400, 'trainings': 9, 'ecm': 'off'}).encode()).decode()
        if variant == 'failing':
            def broken(*args, **kwargs):
                raise RuntimeError('synthetic recording failure')
            monkeypatch.setattr(hylafax_records.FaxEngineRecords, 'record_negotiation', broken)
        answer = asyncio.run(hylafax_http.engine_result(SimpleNamespace(app=None), body, x_internal_secret='x'))
        outcomes.append((answer, store.calls))
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert row(database, 'outbound', ATTEMPT)['rate_lowest'] == 2400


def test_the_built_in_engines_negotiation_never_changes_a_sent_faxs_outcome(monkeypatch):
    from app import main
    observed = []
    monkeypatch.setattr(main.batching_results, 'apply_fax_result', lambda *args, **kwargs: False)
    monkeypatch.setattr(main, '_deliveries', lambda: None)
    monkeypatch.setattr(main, '_observe_native', lambda *args, **kwargs: observed.append((args, kwargs)))
    for event in (fax_result(Status='FAILED', Error='T30_ERR_RX_NOCARRIER', Pages='1'),):
        bare = {name: value for name, value in event.items() if name not in ('Rate', 'Resolution')}
        main._handle_fax_result(bare)
        main._handle_fax_result(event)
        main._handle_fax_result({**event, 'Rate': '\x00junk', 'Resolution': '9' * 400})
    assert len(observed) == 3 and observed[0] == observed[1] == observed[2]


def test_a_recording_failure_on_a_built_in_call_keeps_the_call_record_and_never_raises(installation, database,
                                                                                     monkeypatch):
    calls, records = installation
    built_in_send(calls, records, monkeypatch)

    def broken(*args, **kwargs):
        raise RuntimeError('synthetic recording failure')
    monkeypatch.setattr(hylafax_records.FaxEngineRecords, 'record_negotiation', broken)
    sip_calls._on_fax_result(fax_result())
    assert calls.for_attempt(ATTEMPT)[-1]['pages'] == 2
    assert row(database, 'outbound', ATTEMPT)['negotiation_by'] is None


# Migration 0023 ------------------------------------------------------------------------------------------------

def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def _columns(engine):
    with engine.connect() as connection:
        return {column['name'] for column in sa.inspect(connection).get_columns('fax_engine_calls')}


def test_negotiation_is_head_after_capacity():
    assert schema.HEAD == schema_negotiation.REVISION == '0023_negotiation'
    assert schema.CAPACITY == '0021_capacity' and schema_negotiation.TABLES == frozenset()


def test_0023_adds_empty_columns_keeps_every_row_and_downgrades(database):
    at_revision(database, '0021_capacity')
    table = sa.Table('fax_engine_calls', sa.MetaData(), autoload_with=database)
    with database.begin() as connection:
        connection.execute(table.insert().values(id='call-1', direction='outbound', call_key=ATTEMPT, job_id=JOB,
                                                 engine='hylafax', signal_rate='9600 bit/s', data_format='2-D MR',
                                                 created_at=NOW, updated_at=NOW))
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    added = [name for name, _ in schema_negotiation.COLUMNS]
    for name, rows in before.items():
        if name == 'fax_engine_calls':
            assert [{key: value for key, value in item.items() if key not in added} for item in after[name]] == rows
            assert all(item[column] is None for item in after[name] for column in added)
        elif name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(name, rows), name
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, '0021_capacity')
    assert not set(added) & _columns(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0021_capacity'
    schema.upgrade_schema(database)
    assert set(added) <= _columns(database)
