"""The SSL Fax engine's results: the built-in engine's failure sentences whichever half of a call arrives
first, the audio rule run by the last half, a restart that leaves a fax uncertain (never resent), and
the engine's own secret and folders."""
import base64
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

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


def trunk_side(session, rtp_rx=''):
    return {'Direction': 'out', 'JobID': JOB, 'AttemptID': ATTEMPT, 'T38Session': session, 'Cause': '16',
            'Started': epoch(NOW), 'Answered': epoch(NOW), 'Ended': epoch(NOW + timedelta(seconds=40)),
            'RtpRx': rtp_rx, 'CallID64': base64.b64encode(b'synthetic-call').decode()}


def engine_side(records, reason, pages=0, station=''):
    records.record_engine_result(JOB, ATTEMPT, success=False, pages=pages, station=station, reason=reason, now=NOW)


CASES = [
    # T.38 ran and the far end never sent one fax message: no fax data came back.
    ('1', '', 'No carrier detected {E002}', 0, 'no_t38_data_back', sip_calls.NO_FAX_DATA),
    ('1', '', 'No receiver protocol (T.30 T1 timeout) {E126}', 0, 'no_t38_data_back', sip_calls.NO_FAX_DATA),
    # Audio: no sound came back at all.
    ('0', '0', 'No carrier detected {E002}', 0, 'no_media_back', sip_calls.NO_SOUND),
    # Audio with sound, but no fax answer: not a fax machine.
    ('0', '412', 'No carrier detected {E002}', 0, 'no_fax_answer', sip_calls.NOT_A_FAX),
    # Audio, sound not reported: no fax data came back.
    ('0', '', 'No carrier detected {E002}', 0, 'no_fax_data_back', sip_calls.NO_FAX_DATA),
    # The other machine answered (pages or its station ID): it failed, the network did not.
    ('1', '', 'No response to MPS repeated 3 tries {E150}', 1, 'remote_fax_failed',
     'The other fax machine answered but the fax did not finish.'),
]


@pytest.mark.parametrize('session, rtp_rx, reason, pages, verdict, sentence', CASES)
@pytest.mark.parametrize('trunk_first', [True, False])
def test_the_built_in_engines_verdict_whichever_half_of_the_call_arrives_first(
        calls, session, rtp_rx, reason, pages, verdict, sentence, trunk_first):
    if trunk_first:
        calls.record_engine_call(trunk_side(session, rtp_rx), now=NOW)
        engine_side(calls, reason, pages)
    else:
        engine_side(calls, reason, pages)
        calls.record_engine_call(trunk_side(session, rtp_rx), now=NOW)
    row = calls.for_attempt(ATTEMPT)[-1]
    assert row['verdict'] == verdict and row['error_cause'].startswith(verdict + ': '), row
    assert sip_calls.verdict_sentence(row['verdict']) == sentence
    assert sip_calls.call_summary(row).startswith(sentence[:-1].split(':')[0]) or row['verdict'] == 'remote_fax_failed'
    # The same report again changes nothing.
    calls.record_engine_call(trunk_side(session, rtp_rx), now=NOW)
    engine_side(calls, reason, pages)
    assert calls.for_attempt(ATTEMPT)[-1]['error_cause'] == row['error_cause']


def test_a_sent_fax_has_no_verdict_and_an_unfinished_call_waits_for_its_other_half(calls):
    calls.record_engine_call(trunk_side('0', '900'), now=NOW)
    row = calls.for_attempt(ATTEMPT)[-1]
    # What the trunk heard is kept for the result; it is not a verdict and reads as not finished.
    assert row['verdict'] is None and sip_calls.call_summary(row) == 'The call connected but the fax did not finish.'
    calls.record_engine_result(JOB, ATTEMPT, success=True, pages=3, now=NOW)
    row = calls.for_attempt(ATTEMPT)[-1]
    assert row['verdict'] == 'sent' and row['error_cause'] is None


@pytest.mark.parametrize('trunk_first', [True, False])
def test_the_audio_rule_runs_on_the_last_half_either_way(calls, monkeypatch, trunk_first):
    seen = []
    monkeypatch.setattr(sip_fax_mode, '_on_fax_event', seen.append)
    monkeypatch.setattr(sip_calls, '_current', calls)
    monkeypatch.setattr(sip_calls, '_active_preset', lambda: 'telnyx')
    if trunk_first:
        sip_calls._on_engine_call(trunk_side('1'))
        assert not seen  # The engine's result is not in yet.
        engine_side(calls, 'No carrier detected {E002}')
        sip_calls.engine_audio_check(calls.for_attempt(ATTEMPT)[-1])  # what the result route runs
    else:
        engine_side(calls, 'No carrier detected {E002}')
        sip_calls.engine_audio_check(calls.for_attempt(ATTEMPT)[-1])
        assert not seen  # T.38 is not known until the trunk side arrives.
        sip_calls._on_engine_call(trunk_side('1'))
    assert len(seen) == 1 and sip_calls.verdict(seen[0]) == 'no_t38_data_back'
    assert sip_fax_mode.t38_timeout(calls.for_attempt(ATTEMPT)[-1]['error_cause'])


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


def test_a_failed_send_gets_the_built_in_engines_sentence_and_audio_rule(monkeypatch):
    row = {'verdict': 'no_t38_data_back', 'ended_at': '2026-10-05T01:00:40Z',
           'error_cause': 'no_t38_data_back: No carrier detected E002'}
    payload = {'tag': f'{JOB}.{ATTEMPT}', 'why': 'failed', 'dials': 1, 'pages': 0,
               'status_b64': base64.b64encode(b'No carrier detected {E002}').decode()}
    answer, calls, switched = result_route(monkeypatch, payload, row)
    assert answer == {'status': 'ok'} and calls == [('observed', 'failed', sip_calls.NO_FAX_DATA)]
    assert len(switched) == 1


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
