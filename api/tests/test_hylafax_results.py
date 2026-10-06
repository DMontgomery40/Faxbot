"""The SSL Fax engine's results: the built-in engine's failure sentences whichever half of a call arrives
first, the audio rule run by the last half, a restart that leaves a fax uncertain (never resent), and
the engine's own secret and folders."""
import base64
from datetime import datetime, timedelta
import itertools
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest
import sqlalchemy as sa

from api.app import schema
from app import hylafax_engine, hylafax_http, hylafax_records, sip_calls, sip_fax_mode
from api.tests.test_schema import database  # noqa: F401 - fixture
from api.tests.test_batching import _submitted, sip  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]
JOB, ATTEMPT = 'a' * 32, 'b' * 32
NOW = datetime(2026, 10, 5, 1, 0, 0)


@pytest.fixture
def calls(database):
    schema.upgrade_schema(database)
    records = sip_calls.SipCallRecords(database)
    records.record_submission({'JobID': JOB, 'AttemptID': ATTEMPT, 'Called': '+15555550199',
                               'CallerID': '+15555550100'}, now=NOW)
    return records


def epoch(moment):
    return str(int((moment - datetime(1970, 1, 1)).total_seconds()))


def engine_event(session):
    """The engine channel's own event: answered, ended, the gateway's T.38 session."""
    return {'Direction': 'out', 'Side': 'engine', 'JobID': JOB, 'AttemptID': ATTEMPT, 'T38Session': session,
            'Cause': '16', 'Started': epoch(NOW), 'Answered': epoch(NOW), 'Ended': epoch(NOW + timedelta(seconds=40)),
            'CallID64': base64.b64encode(b'synthetic-call').decode()}


def trunk_event(state, rtp_rx=''):
    """The trunk channel's own event (its hang-up handler runs in its own thread)."""
    return {'Direction': 'out', 'Side': 'trunk', 'JobID': JOB, 'AttemptID': ATTEMPT, 'T38': state, 'Cause': '16',
            'RtpRx': rtp_rx, 'CallID64': base64.b64encode(b'synthetic-call').decode()}


def engine_side(records, reason, pages=0, station=''):
    records.record_engine_result(JOB, ATTEMPT, success=False, pages=pages, station=station, reason=reason, now=NOW)


CASES = [
    # T.38 ran and the engine never heard the other fax machine: the engine's own verdict, not the network's.
    ('1', 'ENABLED', '', 'No carrier detected {E002}', 0, sip_calls.NO_FAX_SIGNAL, sip_calls.NO_SIGNAL),
    ('1', 'ENABLED', '', 'No receiver protocol (T.30 T1 timeout) {E126}', 0, sip_calls.NO_FAX_SIGNAL,
     sip_calls.NO_SIGNAL),
    # Audio: no sound came back at all.
    ('0', 'DISABLED', '0', 'No carrier detected {E002}', 0, 'no_media_back', sip_calls.NO_SOUND),
    # Audio with sound, but no fax answer: not a fax machine.
    ('0', 'DISABLED', '412', 'No carrier detected {E002}', 0, 'no_fax_answer', sip_calls.NOT_A_FAX),
    # Audio, sound not reported: the engine heard no fax machine.
    ('0', 'DISABLED', '', 'No carrier detected {E002}', 0, sip_calls.NO_FAX_SIGNAL, sip_calls.NO_SIGNAL),
    # The other machine answered (pages or its station ID): it failed, the network did not.
    ('1', 'ENABLED', '', 'No response to MPS repeated 3 tries {E150}', 1, 'remote_fax_failed',
     'The other fax machine answered but the fax did not finish.'),
]
ORDERS = list(itertools.permutations(('engine', 'trunk', 'result')))


@pytest.mark.parametrize('session, state, rtp_rx, reason, pages, verdict, sentence', CASES)
@pytest.mark.parametrize('order', ORDERS, ids='-'.join)
def test_an_engine_calls_verdict_is_the_same_whichever_part_arrives_first(
        calls, session, state, rtp_rx, reason, pages, verdict, sentence, order):
    parts = {'engine': lambda: calls.record_engine_call(engine_event(session), now=NOW),
             'trunk': lambda: calls.record_engine_call(trunk_event(state, rtp_rx), now=NOW),
             'result': lambda: engine_side(calls, reason, pages)}
    for part in order:
        parts[part]()
    row = calls.for_attempt(ATTEMPT)[-1]
    assert row['verdict'] == verdict and row['error_cause'].startswith(verdict + ': '), row
    assert '~h' not in row['error_cause'] and row['t38'] == ('yes' if session == '1' else 'no')
    assert row['connected_seconds'] == 40
    assert sip_calls.verdict_sentence(row['verdict']) == sentence
    # The same reports again change nothing.
    for part in order:
        parts[part]()
    assert calls.for_attempt(ATTEMPT)[-1]['error_cause'] == row['error_cause']


def test_a_sent_fax_has_no_verdict_and_an_unfinished_call_waits_for_its_other_parts(calls):
    calls.record_engine_call(trunk_event('DISABLED', '900'), now=NOW)
    row = calls.for_attempt(ATTEMPT)[-1]
    # What the trunk heard is kept for the result; it is not a verdict and reads as not finished.
    assert row['verdict'] is None and row['disposition'] != 'answered'
    calls.record_engine_call(engine_event('0'), now=NOW)
    calls.record_engine_result(JOB, ATTEMPT, success=True, pages=3, now=NOW)
    row = calls.for_attempt(ATTEMPT)[-1]
    assert row['verdict'] == 'sent' and row['error_cause'] is None and row['pages'] == 3


def test_an_unanswered_engine_call_keeps_its_own_reason(calls):
    calls.record_engine_call(trunk_event('DISABLED'), now=NOW)
    calls.record_engine_call({**engine_event('0'), 'Answered': '', 'DialStatus': 'BUSY', 'Cause': '17'}, now=NOW)
    row = calls.for_attempt(ATTEMPT)[-1]
    assert row['disposition'] == 'busy' and row['error_cause'] == 'busy'


@pytest.mark.parametrize('order', ORDERS, ids='-'.join)
def test_only_the_engine_switches_to_audio_after_its_t38_call_heard_no_fax_machine(calls, monkeypatch, order):
    """Never the installation's T.38 setting (the built-in engine keeps it), whichever part is last."""
    switched, engine = [], []
    monkeypatch.setattr(sip_fax_mode, '_on_fax_event', switched.append)
    monkeypatch.setattr(hylafax_engine, 'engine_t38_failed', lambda at=None: engine.append(at))
    monkeypatch.setattr(sip_calls, '_current', calls)
    monkeypatch.setattr(sip_calls, '_active_preset', lambda: 'telnyx')

    def result():
        engine_side(calls, 'No carrier detected {E002}')
        sip_calls.engine_audio_check(calls.for_attempt(ATTEMPT)[-1])  # what the result route runs
    parts = {'engine': lambda: sip_calls._on_engine_call(engine_event('1')),
             'trunk': lambda: sip_calls._on_engine_call(trunk_event('ENABLED')), 'result': result}
    for part in order:
        parts[part]()
    assert not switched and engine and set(engine) == {'2026-10-05T01:00:40Z'}


def test_an_audio_engine_call_does_not_switch_anything(calls, monkeypatch):
    engine = []
    monkeypatch.setattr(hylafax_engine, 'engine_t38_failed', lambda at=None: engine.append(at))
    calls.record_engine_call(engine_event('0'), now=NOW)
    calls.record_engine_call(trunk_event('DISABLED', ''), now=NOW)
    engine_side(calls, 'No carrier detected {E002}')
    sip_calls.engine_audio_check(calls.for_attempt(ATTEMPT)[-1])
    assert calls.for_attempt(ATTEMPT)[-1]['verdict'] == sip_calls.NO_FAX_SIGNAL and not engine


# Restarts -----------------------------------------------------------------------------------------------

class Store:
    def __init__(self, states):
        self.states, self.unconfirmed = states, []

    def get(self, job_id):
        state, attempt = self.states[job_id]
        return {'state': state, 'attempt_id': attempt}

    def attempt_context(self, job_id, attempt_id):
        return None, SimpleNamespace(id='profile-1')

    def record_unconfirmed(self, job_id, *, attempt_id, profile_id, event_key):
        self.unconfirmed.append((job_id, attempt_id, event_key))
        return True


def test_an_engine_start_leaves_every_fax_it_never_reported_on_uncertain(database, monkeypatch):
    schema.upgrade_schema(database)
    records = hylafax_records.records_for(database)
    taken = {}
    for index, (state, reported) in enumerate((('in_progress', False), ('in_progress', True), ('success', False),
                                               ('in_progress', False))):
        job, attempt = f'{index + 1:032x}', f'{index + 11:032x}'
        records.record_call(direction='outbound', call_key=attempt, job_id=job, engine='hylafax',
                            now=NOW - timedelta(minutes=5) if index < 3 else NOW + timedelta(minutes=1))
        if reported:
            records.record_result(direction='outbound', call_key=attempt, job_id=job, now=NOW,
                                  details={'engine_ref': 'e:1', 'sslfax': False})
        taken[job] = (state, attempt)
    from app.routing import background
    monkeypatch.setattr(background, 'installation_engine', lambda app: (database, None))
    store = Store(taken)
    request = SimpleNamespace(app=None)
    assert hylafax_http._settle_interrupted(request, store, NOW) == 1
    # Only the fax taken before the start, still in progress and never reported on; the one taken
    # after the start belongs to the new engine.
    assert store.unconfirmed == [(f'{1:032x}', f'{11:032x}', f'{11:032x}:hylafax:restarted')]


def test_an_uncertain_engine_fax_still_takes_its_late_result(sip):
    """A report kept through an outage arrives after the restart: the real outcome replaces uncertain."""
    _, delivery, *_ = sip
    jobs, claim = _submitted(sip, 1)
    member = claim.everyone[0]
    assert delivery.record_unconfirmed(member.job_id, attempt_id=claim.attempt_id, profile_id=member.profile_id,
                                       event_key=f'{claim.attempt_id}:hylafax:restarted')
    assert delivery.get(member.job_id)['state'] == 'reconciliation_required'
    assert delivery.observe(member.job_id, attempt_id=claim.attempt_id, profile_id=member.profile_id,
                            provider_sid=member.job_id, status='success', event_key=f'{claim.attempt_id}:hylafax:done')
    assert delivery.get(member.job_id)['state'] == 'success'


class ResultStore:
    def __init__(self):
        self.calls = []

    def attempt_context(self, job_id, attempt_id):
        return None, SimpleNamespace(id='profile-1', configuration=SimpleNamespace(provider_id='sip', manifest=None))

    def record_unconfirmed(self, job_id, *, attempt_id, profile_id, event_key):
        self.calls.append(('uncertain', job_id, attempt_id, event_key))
        return True

    def observe(self, job_id, **fields):
        self.calls.append(('observed', fields['status'], fields['error']))
        return True


def result_route(monkeypatch, payload, row=None):
    import asyncio
    from app import audit
    store = ResultStore()
    switched = []
    monkeypatch.setattr(hylafax_http, '_require_engine', lambda secret: None)
    monkeypatch.setattr(hylafax_http, '_store', lambda request: store)
    monkeypatch.setattr(hylafax_http, '_record', lambda *args: row)
    monkeypatch.setattr(audit, 'audit_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(sip_fax_mode, '_on_fax_event', switched.append)
    answer = asyncio.run(hylafax_http.engine_result(SimpleNamespace(app=None), payload, x_internal_secret='x'))
    return answer, store.calls, switched


def test_a_fax_the_engine_dialed_and_then_dropped_waits_for_a_person_instead_of_staying_in_progress(monkeypatch):
    answer, calls, _ = result_route(monkeypatch, {'tag': f'{JOB}.{ATTEMPT}', 'why': 'killed', 'dials': 1})
    assert answer == {'status': 'uncertain'}
    assert calls == [('uncertain', JOB, ATTEMPT, f'{ATTEMPT}:hylafax:killed')]


def test_a_failed_send_gets_the_engines_sentence_and_only_the_engine_goes_to_audio(monkeypatch):
    row = {'verdict': sip_calls.NO_FAX_SIGNAL, 'ended_at': '2026-10-05T01:00:40Z', 't38': 'yes',
           'error_cause': 'no_fax_signal: No carrier detected E002'}
    payload = {'tag': f'{JOB}.{ATTEMPT}', 'why': 'failed', 'dials': 1, 'pages': 0,
               'status_b64': base64.b64encode(b'No carrier detected {E002}').decode()}
    engine = []
    monkeypatch.setattr(hylafax_engine, 'engine_t38_failed', lambda at=None: engine.append(at))
    answer, calls, switched = result_route(monkeypatch, payload, row)
    assert answer == {'status': 'ok'} and calls == [('observed', 'failed', sip_calls.NO_SIGNAL)]
    assert not switched and engine == ['2026-10-05T01:00:40Z']


def test_a_send_the_other_machine_answered_carries_the_engines_own_reason(monkeypatch):
    row = {'verdict': 'remote_fax_failed', 'ended_at': '2026-10-05T01:00:40Z', 't38': 'yes',
           'error_cause': 'remote_fax_failed: No response to MPS repeated 3 tries E150'}
    payload = {'tag': f'{JOB}.{ATTEMPT}', 'why': 'failed', 'dials': 1, 'pages': 1,
               'status_b64': base64.b64encode(b'No response to MPS repeated 3 tries {E150}').decode()}
    answer, calls, _ = result_route(monkeypatch, payload, row)
    assert calls == [('observed', 'failed', 'The call ended after 1 page; the rest was not confirmed.')]


def test_a_send_the_other_machine_answered_without_a_page_says_it_answered(monkeypatch):
    """No page, but the other machine sent its ID: it answered, so never "did not answer as a fax machine"."""
    row = {'verdict': 'remote_fax_failed', 'ended_at': '2026-10-05T01:00:40Z', 't38': 'no',
           'error_cause': 'remote_fax_failed: No response to EOP repeated 3 tries E151'}
    payload = {'tag': f'{JOB}.{ATTEMPT}', 'why': 'failed', 'dials': 1, 'pages': 0,
               'remote_station_b64': base64.b64encode(b'+1 303 426 5097').decode(),
               'status_b64': base64.b64encode(b'No response to EOP repeated 3 tries {E151}').decode()}
    _, calls, _ = result_route(monkeypatch, payload, row)
    assert calls == [('observed', 'failed', 'The other fax machine answered but the fax did not finish.')]


# The engine's own secret and folders ----------------------------------------------------------------------

def test_the_engine_hands_over_only_images_in_its_out_folder_with_its_own_secret(isolated_installation,
                                                                                    monkeypatch, tmp_path):
    from api.app.main import app
    data = tmp_path / 'faxdata_engine'
    for name, value in (('INBOUND_ENABLED', 'true'), ('ASTERISK_INBOUND_SECRET', 'sekret'),
                        ('FAX_DATA_DIR', str(data)), ('REQUIRE_API_KEY', 'true'), ('API_KEY', 'bootstrap_admin_only')):
        monkeypatch.setenv(name, value)
    engine_secret = hylafax_engine.engine_secrets(SimpleNamespace(fax_data_dir=str(data)), lines=1)['report_secret']
    images = {}
    for name, folder in (('inside', data / 'hylafax-out' / 'inbound'), ('outside', data / 'inbound')):
        folder.mkdir(parents=True, exist_ok=True)
        images[name] = folder / f'engine-{name}.tiff'
        Image.new('1', (20, 10), 1).save(images[name], format='TIFF')

    with TestClient(app, base_url='http://testserver') as client:
        def post(image, secret):
            return client.post('/_internal/hylafax/inbound', headers={'X-Internal-Secret': secret}, json={
                'tiff_path': str(image), 'to_number': '+15555550100', 'from_number': '+15555550199',
                'faxstatus': 'SUCCESS', 'faxpages': 1, 'uniqueid': 'hylafax.0123456789abcdef.000000007-1'})
        # Faxbot's own folders are not the engine's to name, and Asterisk's secret is not the engine's.
        assert post(images['outside'], engine_secret).status_code == 400
        assert post(images['inside'], 'sekret').status_code == 401
        taken = post(images['inside'], engine_secret)
        assert taken.status_code == 200 and taken.json()['status'] == 'ok', taken.text


def test_the_only_shell_command_is_the_built_in_hand_over_and_engine_lines_reach_only_their_context(tmp_path):
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    contexts, current = {}, None
    for line in dialplan.splitlines():
        if line.startswith('[') and line.rstrip().endswith(']'):
            current = line.strip()[1:-1]
            contexts[current] = ''
        elif current:
            contexts[current] += line + '\n'
    with_shell = [name for name, body in contexts.items() if 'SHELL(' in body or 'SYSTEM(' in body]
    assert with_shell == ['faxbot-inbound-done'] and dialplan.count('SHELL(') == 1
    # The engine's lines enter faxbot-engine-out only, and nothing an engine context runs reaches SHELL.
    engine_contexts = [name for name in contexts if name.startswith('faxbot-engine')]
    assert engine_contexts and not any('faxbot-inbound-done' in contexts[name] for name in engine_contexts)
    values = SimpleNamespace(fax_data_dir=str(tmp_path), sip_trunk_dids='+15555550100', fax_default_country='US')
    iax = hylafax_engine.render_iax(values, {'lines': {'1': 'a' * 32, '2': 'b' * 32}}, lines=2)
    peers = [block for block in iax.split('\n[') if block.startswith('faxbot-line')]
    assert len(peers) == 2 and all('context=faxbot-engine-out' in block for block in peers)
    assert 'guest' not in iax.lower() and 'allowguest' not in iax.lower()


# Received calls ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize('value, country, stored', [
    ('3034265097', 'US', '+13034265097'),       # Telnyx's caller as the engine's modem passed it
    ('13034265097', 'US', '+13034265097'),
    ('+13034265097', 'US', '+13034265097'),     # the built-in engine's form stays as it is
    ('17208565062', 'US', '+17208565062'),
    ('02079460000', 'GB', '+442079460000'),     # a UK caller in the UK's own format
    ('442079460000', 'US', '+442079460000'),    # a country code without its plus sign
    ('0061298765432', 'GB', '+61298765432'),    # an international prefix counts as a country code
    ('anonymous', 'US', 'anonymous'),           # anything else is kept as given
    ('', 'US', None),
])
def test_received_numbers_are_stored_the_same_way_for_both_engines_in_every_country(value, country, stored):
    from app.inbound.http import received_number
    assert received_number(value, country) == stored


def inbound_event(**extra):
    return {'Direction': 'in', 'Token': '17911995026', 'DID': '+17208565062', 'Caller': '+13034265097',
            'T38Session': '0', 'T38': 'DISABLED', 'Started': epoch(NOW), 'Answered': epoch(NOW),
            'Ended': epoch(NOW + timedelta(seconds=58)), 'Cause': '16', 'RtpRx': '2400',
            'CallID64': base64.b64encode(b'synthetic-in').decode(), **extra}


@pytest.mark.parametrize('event_first', [True, False])
def test_a_received_fax_reads_received_whichever_report_came_first(database, event_first):
    schema.upgrade_schema(database)
    calls = sip_calls.SipCallRecords(database)

    def receive():
        calls.record_engine_receive('engine.17911995026', success=True, pages=2, station='3034265097',
                                    did='+17208565062', caller='+13034265097', inbound_fax_id='f' * 32, now=NOW)
    if event_first:
        calls.record_engine_call(inbound_event(), now=NOW)
        receive()
    else:
        receive()
        calls.record_engine_call(inbound_event(), now=NOW)
    row = calls.page(limit=5)['items'][0]
    assert row['verdict'] == 'received' and row['pages'] == 2 and row['summary'] == 'Received: 2 pages.'
    assert row['caller'] == '+13034265097' and row['connected_seconds'] == 58 and row['job_id'] == 'f' * 32


@pytest.mark.parametrize('event_first', [True, False])
def test_a_received_call_that_left_no_fax_has_a_record_and_a_sentence(database, event_first):
    schema.upgrade_schema(database)
    calls = sip_calls.SipCallRecords(database)

    def failed():
        calls.record_engine_receive('engine.17911994223', success=False, pages=0,
                                    reason='No sender protocol (T.30 T1 timeout) {E102}', did='+17208565062',
                                    caller='+13034265097', now=NOW)
    event = inbound_event(Token='17911994223')
    if event_first:
        calls.record_engine_call(event, now=NOW)
        failed()
    else:
        failed()
        calls.record_engine_call(event, now=NOW)
    row = calls.page(limit=5)['items'][0]
    assert row['verdict'] == sip_calls.NO_FAX_SIGNAL and row['fax_status'] == 'FAILED'
    assert row['summary'] == 'A fax call from +13034265097 came in, but no pages arrived.'
    assert row['error_cause'].startswith('no_fax_signal: No sender protocol')


def test_the_engines_report_of_a_received_call_that_left_no_fax_reaches_recent_calls(isolated_installation,
                                                                                       monkeypatch, tmp_path):
    from api.app.main import app
    data = tmp_path / 'faxdata_engine'
    for name, value in (('INBOUND_ENABLED', 'true'), ('ASTERISK_INBOUND_SECRET', 'sekret'),
                        ('FAX_DATA_DIR', str(data)), ('REQUIRE_API_KEY', 'true'), ('API_KEY', 'bootstrap_admin_only'),
                        ('FAX_DEFAULT_COUNTRY', 'US')):
        monkeypatch.setenv(name, value)
    secret = hylafax_engine.engine_secrets(SimpleNamespace(fax_data_dir=str(data)), lines=1)['report_secret']
    body = {'engine_id': '0123456789abcdef', 'commid': '000000003', 'key': '000000003-1791199466',
            'token': '17911994223', 'caller': '3034265097', 'called': '17208565062',
            'reason_b64': base64.b64encode(b'No sender protocol (T.30 T1 timeout) {E102}').decode()}
    with TestClient(app, base_url='http://testserver') as client:
        url = '/_internal/hylafax/received-failed'
        assert client.post(url, json=body, headers={'X-Internal-Secret': 'sekret'}).status_code == 401
        assert client.post(url, json={**body, 'key': 'x'}, headers={'X-Internal-Secret': secret}).status_code == 400
        answer = client.post(url, json=body, headers={'X-Internal-Secret': secret})
        assert answer.status_code == 200, answer.text
        assert answer.json()['summary'] == 'A fax call from +13034265097 came in, but no pages arrived.'
        calls = client.get('/admin/sip/calls', headers={'X-API-Key': 'bootstrap_admin_only'}).json()['items']
        assert calls[0]['did'] == '+17208565062' and calls[0]['verdict'] == sip_calls.NO_FAX_SIGNAL


# Received faxes stay apart after an engine restart; a replayed hand-over stays one fax ------------------------

ENGINE_ID = '0123456789abcdef'
ADMIN = {'X-API-Key': 'bootstrap_admin_only'}


def engine_installation(monkeypatch, tmp_path):
    """Receiving over the trunk, with the engine's own report secret; (data folder, secret)."""
    data = tmp_path / 'faxdata_engine'
    for name, value in (('INBOUND_ENABLED', 'true'), ('ASTERISK_INBOUND_SECRET', 'sekret'),
                        ('FAX_DATA_DIR', str(data)), ('REQUIRE_API_KEY', 'true'), ('API_KEY', 'bootstrap_admin_only'),
                        ('FAX_DEFAULT_COUNTRY', 'US')):
        monkeypatch.setenv(name, value)
    return data, hylafax_engine.engine_secrets(SimpleNamespace(fax_data_dir=str(data)), lines=1)['report_secret']


def handed_over(data, arrival, token):
    """bin/handover's body for communication ID 7 received at ``arrival`` (an image in the engine's out folder)."""
    key = f'000000007-{arrival}'
    folder = data / 'hylafax-out' / 'inbound'
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / f'engine-{ENGINE_ID}-{key}.tiff'
    Image.new('1', (20, 10), 1).save(image, format='TIFF')
    return {'tiff_path': str(image), 'to_number': '15555550100', 'from_number': '+15555550199',
            'faxstatus': 'SUCCESS', 'faxpages': 1,
            'uniqueid': f'engine.{token}' if token else f'hylafax.{ENGINE_ID}.{key}',
            'call': {'did': '15555550100', 'caller': '+15555550199', 'pages': 1, 't38': None,
                     'remote_station_id_b64': None},
            'engine': {'engine': 'hylafax', 'engine_ref': f'{ENGINE_ID}:{key}', 'sslfax': False,
                       'sslfax_offered': False, 'transfer_seconds': 12, 'signal_rate_b64': None,
                       'data_format_b64': None}}


def inbound_engine_refs(app):
    from app.routing.background import installation_engine
    engine, _ = installation_engine(app)
    table = hylafax_records.records_for(engine)._table('fax_engine_calls')
    with engine.connect() as connection:
        return sorted(connection.execute(sa.select(table.c.engine_ref).where(
            table.c.direction == 'inbound')).scalars())


@pytest.mark.parametrize('tokens', [(None, None), ('17911995026', '17911998111')], ids=['engine-key', 'asterisk-call'])
def test_a_new_engine_container_repeating_a_communication_id_keeps_two_received_faxes_and_a_replay_one(
        isolated_installation, monkeypatch, tmp_path, tokens):
    """A new engine container starts its communication IDs again; each received fax keeps its own identity and
    engine record (with or without Asterisk's call name), and the same hand-over posted again stays one fax."""
    from api.app.main import app
    data, secret = engine_installation(monkeypatch, tmp_path)
    first, second = handed_over(data, '1791180000', tokens[0]), handed_over(data, '1791183600', tokens[1])
    with TestClient(app, base_url='http://testserver') as client:
        answers = [client.post('/_internal/hylafax/inbound', headers={'X-Internal-Secret': secret}, json=body)
                   for body in (first, second, first)]
        assert [answer.status_code for answer in answers] == [200, 200, 200], [answer.text for answer in answers]
        ids = [answer.json()['id'] for answer in answers]
        assert ids[0] != ids[1] and ids[2] == ids[0]
        assert sorted(item['id'] for item in client.get('/inbound', headers=ADMIN).json()) == sorted(ids[:2])
        assert inbound_engine_refs(app) == [f'{ENGINE_ID}:000000007-1791180000', f'{ENGINE_ID}:000000007-1791183600']


def test_a_new_engine_container_repeating_a_communication_id_keeps_two_calls_that_left_no_fax(
        isolated_installation, monkeypatch, tmp_path):
    from api.app.main import app
    data, secret = engine_installation(monkeypatch, tmp_path)
    reason = base64.b64encode(b'No sender protocol (T.30 T1 timeout) {E102}').decode()
    first, second = ({'engine_id': ENGINE_ID, 'commid': '000000003', 'key': f'000000003-{arrival}', 'token': '',
                      'caller': '3034265097', 'called': '17208565062', 'reason_b64': reason}
                     for arrival in ('1791180000', '1791183600'))
    with TestClient(app, base_url='http://testserver') as client:
        for body in (first, second, first):
            answer = client.post('/_internal/hylafax/received-failed', json=body, headers={'X-Internal-Secret': secret})
            assert answer.status_code == 200, answer.text
        calls = client.get('/admin/sip/calls', headers=ADMIN).json()['items']
        assert len(calls) == 2 and {call['verdict'] for call in calls} == {sip_calls.NO_FAX_SIGNAL}
        assert inbound_engine_refs(app) == [f'{ENGINE_ID}:000000003-1791180000', f'{ENGINE_ID}:000000003-1791183600']


# A received call's numbers read the same whichever report came first ---------------------------------------------

def test_a_failed_engine_receive_names_the_caller_with_the_country_code_when_asterisks_event_came_first(
        isolated_installation, monkeypatch, tmp_path):
    """Live, 6 October 2026: Asterisk's event carried Telnyx's caller as 3034265097 and came before the engine's
    report, so the failed call and its engine record kept it without the country code (the received fax, which
    came the other way round, read +13034265097)."""
    from api.app.main import app
    from app.routing.background import installation_engine
    data, secret = engine_installation(monkeypatch, tmp_path)
    reason = base64.b64encode(b'Failure to receive silence (synchronization failure). {E100}').decode()
    with TestClient(app, base_url='http://testserver') as client:
        engine, _ = installation_engine(app)
        sip_calls.SipCallRecords(engine).record_engine_call(
            inbound_event(Token='17912491111', Caller='3034265097', DID='17208565062'))
        answer = client.post('/_internal/hylafax/received-failed', headers={'X-Internal-Secret': secret}, json={
            'engine_id': ENGINE_ID, 'commid': '000000008', 'key': '000000008-1791249049', 'token': '17912491111',
            'caller': '3034265097', 'called': '17208565062', 'reason_b64': reason})
        assert answer.status_code == 200, answer.text
        assert answer.json()['summary'] == 'A fax call from +13034265097 came in, but no pages arrived.'
        (call,) = client.get('/admin/sip/calls', headers=ADMIN).json()['items']
        assert (call['caller'], call['did'], call['called']) == ('+13034265097', '+17208565062', '+17208565062')
        table = hylafax_records.records_for(engine)._table('fax_engine_calls')
        with engine.connect() as connection:
            assert connection.execute(sa.select(table.c.number)).scalars().all() == ['+13034265097']


@pytest.mark.parametrize('kind', ['engine', 'built-in'])
def test_asterisks_events_store_received_numbers_the_way_the_hand_overs_do(database, kind):
    schema.upgrade_schema(database)
    calls = sip_calls.SipCallRecords(database)
    if kind == 'engine':
        calls.record_engine_call(inbound_event(Caller='3034265097', DID='17208565062'), now=NOW)
    else:
        calls.record_inbound_event({'UniqueID': '1791249049.12', 'DID': '17208565062', 'Caller': '3034265097',
                                    'Started': epoch(NOW), 'Answered': epoch(NOW), 'Ended': epoch(NOW),
                                    'Status': 'FAILED', 'Mode': 'audio'}, now=NOW)
    (row,) = calls.page(limit=5)['items']
    assert (row['caller'], row['did']) == ('+13034265097', '+17208565062')
    # A number that is not a telephone number in this country is kept as Asterisk gave it.
    calls.record_inbound_event({'UniqueID': '1791249050.13', 'DID': '100', 'Caller': '200',
                                'Started': epoch(NOW), 'Ended': epoch(NOW)}, now=NOW)
    assert {(item['caller'], item['did']) for item in calls.page(limit=5)['items']} >= {('200', '100')}


def test_received_calls_stored_without_the_country_code_are_shown_with_it_and_kept_as_stored(database):
    """Recent calls, the trunk's last call and `faxbot providers trunk calls` read the same records. Rows stored
    before received numbers were read for the country keep their stored form (history is never rewritten)."""
    schema.upgrade_schema(database)
    calls = sip_calls.SipCallRecords(database)
    for call_id, status, pages in (('engine.17912490001', 'FAILED', 0), ('engine.17912491682', 'SUCCESS', 2)):
        with database.begin() as connection:
            connection.execute(calls.table.insert().values(
                id=call_id[-12:].rjust(32, '0'), direction='inbound', call_id=call_id, did='17208565062',
                caller='3034265097', called='17208565062', started_at=NOW + timedelta(seconds=int(call_id[-1])),
                answered_at=NOW, ended_at=NOW + timedelta(seconds=61), disposition='answered', connected_seconds=61,
                t38='yes', pages=pages, fax_status=status, fax_preference=0, created_at=NOW, updated_at=NOW,
                error_cause=None if pages else 'Failure to receive silence synchronization'))
    calls.record_submission({'JobID': JOB, 'AttemptID': ATTEMPT, 'Called': '3035550199', 'CallerID': '3035550100'},
                            now=NOW)
    items = calls.page(limit=5)['items']
    received = [item for item in items if item['direction'] == 'inbound']
    assert {(item['caller'], item['did'], item['called']) for item in received} == {
        ('+13034265097', '+17208565062', '+17208565062')}
    assert {item['summary'] for item in received} >= {'A fax call from +13034265097 came in, but no pages arrived.'}
    assert calls.call(received[0]['id'])['caller'] == '+13034265097'
    # A sent call shows what Faxbot dialled, as before.
    (sent,) = [item for item in items if item['direction'] == 'outbound']
    assert sent['called'] == '3035550199'
    with database.connect() as connection:
        stored = connection.execute(sa.select(calls.table.c.caller).where(
            calls.table.c.direction == 'inbound')).scalars().all()
    assert stored == ['3034265097', '3034265097']


# Restarting Asterisk waits for the engine's calls -------------------------------------------------------------

def test_a_restart_waits_while_the_engine_holds_a_fax_it_has_not_reported_on(database, monkeypatch):
    from app import sip_http
    schema.upgrade_schema(database)
    monkeypatch.setattr(sip_calls, '_current', sip_calls.SipCallRecords(database))
    records = hylafax_records.records_for(database)
    assert sip_http._engine_faxes_pending() is False
    records.record_call(direction='outbound', call_key=ATTEMPT, job_id=JOB, engine='hylafax')
    assert sip_http._engine_faxes_pending() is True
    records.record_result(direction='outbound', call_key=ATTEMPT, job_id=JOB,
                          details={'engine_ref': 'e:9', 'sslfax': False})
    assert sip_http._engine_faxes_pending() is False


# The dialplan --------------------------------------------------------------------------------------------------

def test_the_trunks_hang_up_handler_reports_in_its_own_event_and_writes_nothing_to_the_engines_channel():
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    trunk = dialplan.split('[faxbot-engine-trunk-done]', 1)[1].split('\n[', 1)[0]
    assert 'SHARED(' not in trunk and 'Side:trunk' in trunk and 'JobID:${FAXBOT_ENGINE_JOB}' in trunk
    # RTCP is read only on an answered call whose carrier leg is audio (a T.38 leg has no RTP session).
    guard, read = trunk.index('!= "DISABLED" & "${FAXBOT_T38}" != "REJECTED"]?emit)'), trunk.index('rtcp,rxcount')
    assert trunk.index('"${FAXBOT_CALLID64}" = ""]?emit)') < guard < read
    engine = dialplan.split('[faxbot-engine-result]', 1)[1].split('\n[', 1)[0]
    assert 'SHARED(FAXBOT_T38' not in engine and 'SHARED(FAXBOT_RTPRX' not in engine and 'Side:engine' in engine
    assert 'GwStatus:' in engine and 'GwError:' in engine
    # Inherited by the trunk channel, so its event names the fax and attempt.
    assert 'Set(__FAXBOT_ENGINE_JOB=${JOBID})' in dialplan and 'Set(__FAXBOT_ENGINE_ATTEMPT=${FAXATTEMPT})' in dialplan


def test_asterisk_sends_t38_first_when_a_t38_stream_starts():
    """Behind a router that keeps ports and forwards none, the carrier's T.38 data gets in only after Faxbot
    sends from its T.38 port (live, 5 October 2026: zero UDPTL packets either way). Faxbot's Asterisk image
    applies its own patch: three T.38 no-signal packets as soon as a T.38 stream is applied."""
    patch = (ROOT / 'asterisk' / 'patches' / '0001-t38-send-first.patch').read_text()
    assert '--- a/res/res_pjsip_t38.c' in patch and '+#define FAXBOT_T38_OPENING_PACKETS 3' in patch
    assert '+		faxbot_t38_open_pinhole(session_media->udptl);' in patch
    assert 'static const unsigned char no_signal[1] = { 0x00 };' in patch
    # Once per T.38 stream: a session refresh applies the same stream again, and no indicator may interrupt
    # a page; a new T.38 stream (state no longer enabled) sends again.
    assert '+	if (remote_stream->desc.port && state->opened != session_media->udptl) {' in patch
    assert '+		state->opened = NULL;' in patch
    # The T.38 gateway ends a V.21 preamble that carried no frame (spandsp 0.0.6), with the same steps
    # asterisk/tests/t38_gateway_replay.c proves (tests/test_t38_gateway_replay.py).
    gateway = (ROOT / 'asterisk' / 'patches' / '0002-t38-gateway-empty-preamble.patch').read_text()
    replay = (ROOT / 'asterisk' / 'tests' / 't38_gateway_replay.c').read_text()
    for step in ('queue->in == queue->out || !(queue->buf[queue->out].contents & FAXBOT_SPANDSP_FLAG_INDICATOR) '
                 '|| hdlc->len', 'if (hdlc->flag_octets > 2) {', 'hdlc->flag_octets = 2;', 'hdlc_tx_frame(hdlc, NULL, 0);',
                 '#define FAXBOT_SPANDSP_FLAG_INDICATOR 0x100'):
        assert step in gateway and step in replay, step
    assert '+\tfaxbot_end_empty_preamble(s, p);\n \tif ((f->samples = t38_gateway_tx(' in gateway
    # One NOTICE line per T.38 stream at session end, with the packets counted in res_pjsip_t38's own
    # read and write callbacks (so it holds after the call switched back to audio).
    assert '+\t.session_end = t38_session_end,' in patch
    assert patch.count('+\tfaxbot_t38_count(session, ') == 2
    assert not (ROOT / 'asterisk' / 'patches' / '0003-udptl-packet-counts.patch').exists()
    dockerfile = (ROOT / 'asterisk' / 'Dockerfile').read_text()
    applied = dockerfile.index('patch -p1 --forward')
    assert dockerfile.index('COPY patches/ /usr/src/patches/') < applied < dockerfile.index('RUN ./configure')


def test_two_sends_with_the_same_communication_id_both_keep_their_engine_record(database):
    """A new engine container starts its communication IDs again (live, 5 October: the record of a send was
    dropped as a duplicate of an earlier call's); the attempt keeps each reference unique."""
    schema.upgrade_schema(database)
    records = hylafax_records.records_for(database)
    first, second = ('1' * 32, '2' * 32), ('3' * 32, '4' * 32)
    for job, attempt in (first, second):
        records.record_call(direction='outbound', call_key=attempt, job_id=job, engine='hylafax', now=NOW)
        details = hylafax_http._engine_details({'tag': f'{job}.{attempt}', 'engine_id': '0123456789abcdef',
                                                'commid': '000000001', 'why': 'done', 'sslfax': True,
                                                'transfer_seconds': 12})
        assert details['engine_ref'] == f'0123456789abcdef:000000001.{attempt[:12]}'
        records.record_result(direction='outbound', call_key=attempt, job_id=job, details=details, now=NOW)
    for _, attempt in (first, second):
        assert records.for_call('outbound', attempt)['sslfax'] is True


def test_two_received_faxes_with_the_same_communication_id_both_keep_their_engine_record(database):
    """The received side of the same rule: the arrival time keeps each received fax's reference unique after a
    new engine container, and the same hand-over recorded again adds no row and no second observation."""
    schema.upgrade_schema(database)
    records = hylafax_records.records_for(database)

    def handed_over(arrival, call_key):
        return hylafax_engine.record_inbound_engine(
            database, {'engine': {'engine': 'hylafax', 'engine_ref': f'0123456789abcdef:000000007-{arrival}',
                                  'sslfax': True, 'transfer_seconds': 12}},
            call_key=call_key, inbound_fax_id='f' * 32, number='+15555550199')
    first = handed_over('1791180000', 'engine.17911995026')
    second = handed_over('1791183600', 'engine.17911998111')
    assert first and second and first != second
    assert handed_over('1791180000', 'engine.17911995026') == first
    for call_key in ('engine.17911995026', 'engine.17911998111'):
        assert records.for_call('inbound', call_key)['sslfax'] is True
    observations = records._table('sslfax_observations')
    with database.connect() as connection:
        sources = sorted(connection.execute(sa.select(observations.c.source)).scalars())
    assert sources == ['0123456789abcdef:000000007-1791180000', '0123456789abcdef:000000007-1791183600']

