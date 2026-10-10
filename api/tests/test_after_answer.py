"""Digits after answer (N7): keys Faxbot presses once a recipient's phone menu answers (routing/after_answer.py).

The dialplan and call-plan tests read the shipped files. The HTTP and command-line tests run the real POST /fax
path of an installation with a SIP trunk and sending turned off, so nothing is dialed; the dispatch tests run the
real worker, transport and planner (the rules-delivery harness) on SQLite and PostgreSQL. Numbers are synthetic.
Not yet run against a real phone menu: the loopback case in test_after_answer_loopback.py stands in for one.
"""
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from app import ami, hylafax_engine
from app.routing import after_answer, stations
from api.tests.test_reply_number import DID_A, client  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]
TO = '+13035550150'


@pytest.mark.parametrize('text, keys', [
    ('2', '2'), (' 2w105 ', '2w105'), ('2,105#', '2W105#'), ('*9 W 1-2-3', '*9W123'), ('wW2', 'wW2'),
    ('none', None), ('', None), (None, None), ('OFF', None),
])
def test_keys_are_cleaned_to_what_senddtmf_takes(text, keys):
    assert after_answer.clean(text) == keys


@pytest.mark.parametrize('text', ['2p105', 'A', '2;Hangup', 'ww', 'W,', '1' * 33, '2/3', '${EVIL}'])
def test_anything_senddtmf_would_not_take_is_refused_with_one_sentence(text):
    with pytest.raises(ValueError) as refused:
        after_answer.clean(text)
    assert str(refused.value).endswith('.') and str(refused.value).count('.') == 1


def test_keys_read_aloud_with_pauses():
    assert after_answer.spoken('2') == '2'
    assert after_answer.spoken('2w105') == '2, pause, 105'
    assert after_answer.spoken('2WW105#') == '2, pause, 105#'
    assert after_answer.spoken('W9') == 'pause, 9'


def test_the_originate_carries_the_keys_only_when_set_and_only_valid_ones():
    fields = ami.prepare_originate_fields('job1', TO, '/faxdata/job1.tiff', caller_id=DID_A, dtmf='2w105#')
    assert 'FAXBOT_DTMF=2w105#' in fields['Variable'].split(',')
    assert ami.requested_keys(fields) == '2w105#'
    plain = ami.prepare_originate_fields('job1', TO, '/faxdata/job1.tiff', caller_id=DID_A)
    assert 'FAXBOT_DTMF' not in plain['Variable'] and ami.requested_keys(plain) is None
    for wrong in ('2,3', '2;x', '', 'p', 5, '1' * 33):
        with pytest.raises(ValueError):
            ami.prepare_originate_fields('job1', TO, '/faxdata/job1.tiff', caller_id=DID_A, dtmf=wrong)


def test_the_engine_plan_adds_the_keys_after_the_endpoint_even_on_the_first_trunk():
    job, attempt = 'a' * 32, 'b' * 32
    fields = ami.prepare_originate_fields(job, TO, '/faxdata/x.tiff', caller_id=DID_A, attempt_id=attempt,
                                          dial=TO, dtmf='2w105')
    assert hylafax_engine.call_plan(fields, job, attempt) == f'{TO}/{DID_A}/{job}/{attempt}/0/1/trunk-endpoint/2w105'
    plain = ami.prepare_originate_fields(job, TO, '/faxdata/x.tiff', caller_id=DID_A, attempt_id=attempt, dial=TO)
    assert hylafax_engine.call_plan(plain, job, attempt) == f'{TO}/{DID_A}/{job}/{attempt}/0/1'
    second = ami.prepare_originate_fields(job, TO, '/faxdata/x.tiff', caller_id=DID_A, attempt_id=attempt, dial=TO,
                                          endpoint='trunk-line2-endpoint', dtmf='9')
    assert hylafax_engine.call_plan(second, job, attempt, endpoints=('trunk-endpoint', 'trunk-line2-endpoint')) == \
        f'{TO}/{DID_A}/{job}/{attempt}/0/1/trunk-line2-endpoint/9'


def _context(dialplan, name):
    lines, inside = [], False
    for line in dialplan.splitlines():
        if line.startswith('['):
            inside = line.startswith(f'[{name}]')
            continue
        if inside and not line.startswith(';'):
            lines.append(line)
    return lines


def test_the_builtin_engine_presses_the_keys_on_the_voice_call_before_any_sendfax():
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    send = _context(dialplan, 'faxbot-send')
    press = [index for index, line in enumerate(send) if 'SendDTMF(' in line]
    faxes = [index for index, line in enumerate(send) if 'SendFAX(' in line]
    assert len(press) == 1 and send[press[0]].strip() == 'same => n,SendDTMF(${FAXBOT_DTMF})'
    assert send[press[0] - 1].strip() == 'same => n,GotoIf($["${FAXBOT_DTMF}" = ""]?keys-done)'
    assert send[press[0] + 1].strip().startswith('same => n(keys-done),GotoIf($["${FAXBOT_AUDIO}" = "yes"]?audio)')
    # Both SendFAX branches (T.38 first, and audio only) come after it, and nothing before it jumps to a SendFAX.
    assert faxes and press[0] < min(faxes)
    assert not any('?audio' in line for line in send[:press[0]])


def test_the_ssl_fax_engine_dials_with_d_from_the_plans_eighth_field_and_only_when_set():
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    out = _context(dialplan, 'faxbot-engine-out')
    taking = [line.strip() for line in out if 'FAXBOT_DTMF=' in line]
    assert taking == ['same => n,Set(FAXBOT_DTMF=${FILTER(0123456789*#wW,${CUT(FAXBOT_PLAN,/,8)})})']
    [dial] = [line for line in out if 'Dial(PJSIP/' in line]
    assert dial.rstrip().endswith('U(faxbot-engine-answered)${IF($["${FAXBOT_DTMF}" != ""]?D(${FAXBOT_DTMF}))})')
    assert out.index(next(line for line in out if 'FAXBOT_DTMF=' in line)) < out.index(dial)


# -- the real paths --------------------------------------------------------------------------------------------------

def _send(test_client):
    from api.tests.test_access_management_http import B
    sent = test_client.post('/fax', headers=B, data={'to': TO},
                            files={'file': ('note.txt', b'Synthetic page', 'text/plain')})
    assert sent.status_code == 202, sent.text
    return sent.json()['id']


def _variables(fields):
    return dict(item.split('=', 1) for item in fields['Variable'].split(',') if '=' in item and '"' not in item)


def test_a_number_behind_a_phone_menu_gets_its_keys_no_answer_cap_and_no_switchboard_station(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    from app.main import app
    engine = app.state.configuration_runtime.manager.store.engine
    running = app.state.configuration_runtime.manager.store.read().active.values
    view = client.get(f'/routing/after-answer/{TO}', headers=B).json()
    assert (view['digits'], view['sentence']) == (None, 'Faxbot starts the fax as soon as this number answers.')
    first = _send(client)
    plain = _variables(ami.originate_fields_for(running, first, TO, '/faxdata/first.tiff'))
    assert 'FAXBOT_DTMF' not in plain and plain['FAXBOT_T0_MS'] == '50000' and plain['FAXBOT_CSI_EXPECT'] == '13035550150'
    saved = client.put(f'/routing/after-answer/{TO}', headers=B, json={'digits': '2, 105'})
    assert saved.status_code == 200, saved.text
    assert saved.json()['digits'] == '2W105'
    assert saved.json()['saved'] == (
        'Saved. Faxbot will press 2, pause, 105 after this number answers, only on calls over your trunk. The carrier '
        'bills from the moment the call is answered, so the seconds in the phone menu are part of the call.')
    fields = _variables(ami.originate_fields_for(running, first, TO, '/faxdata/first.tiff'))
    assert fields['FAXBOT_DTMF'] == '2W105'
    # The menu's seconds would use up the 50-second cap, and the switchboard's number is not the fax machine's.
    assert 'FAXBOT_T0_MS' not in fields and 'FAXBOT_CSI_EXPECT' not in fields and 'FAXBOT_T38_NOW' not in fields
    # A station the fax machine behind the menu showed on a successful call is expected from then on.
    stations.after_call(engine, job_id=first, attempt_id=None, station='+1 303 555 0177', succeeded=True)
    assert client.put(f'/routing/stations/{TO}', headers=B, json={'mode': 'refuse'}).status_code == 200
    fields = _variables(ami.originate_fields_for(running, first, TO, '/faxdata/first.tiff'))
    assert fields['FAXBOT_CSI_EXPECT'] == '13035550177' and fields['FAXBOT_CSI_REFUSE'] == 'yes'
    assert client.put(f'/routing/after-answer/{TO}', headers=B, json={'digits': 'p9'}).status_code == 400
    cleared = client.put(f'/routing/after-answer/{TO}', headers=B, json={'digits': None})
    assert cleared.json()['saved'] == 'Saved. Faxbot no longer presses any keys after this number answers.'
    assert cleared.json()['sentence'] == 'Faxbot starts the fax as soon as this number answers.'
    assert 'FAXBOT_DTMF' not in _variables(ami.originate_fields_for(running, first, TO, '/faxdata/first.tiff'))
    with engine.connect() as connection:  # every change is kept; none is rewritten
        assert [row[0] for row in connection.execute(sa.text(
            'SELECT digits FROM recipient_after_answer ORDER BY created_at, id'))] == ['2W105', None]


def test_a_setting_that_cannot_be_read_stops_the_call_instead_of_dialling_without_keys(client, monkeypatch):  # noqa: F811
    from app.main import app
    running = app.state.configuration_runtime.manager.store.read().active.values
    job = _send(client)

    def broken(connection, number):
        raise sa.exc.OperationalError('SELECT', {}, Exception('database is locked'))
    monkeypatch.setattr(after_answer, 'current_on', broken)
    with pytest.raises(after_answer.AfterAnswerUnavailable):
        ami.originate_fields_for(running, job, TO, '/faxdata/x.tiff')


def test_sent_details_say_which_keys_each_call_pressed_even_after_the_setting_changes(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    from app.main import app
    engine = app.state.configuration_runtime.manager.store.engine
    job = _send(client)
    event = {'JobID': job, 'AttemptID': 'c' * 32, 'Called': TO, 'Digits': '2w105'}
    assert after_answer.record_submission(engine, event) is True
    assert after_answer.record_submission(engine, event) is False  # once per attempt
    assert after_answer.record_submission(engine, {**event, 'AttemptID': 'd' * 32, 'Engine': 'sslfax',
                                                   'Digits': '9'}) is True
    # A poll request, or an event without keys, is not a sent fax's call.
    assert after_answer.record_submission(engine, {**event, 'JobID': 'e' * 32, 'AttemptID': 'e' * 32}) is False
    assert after_answer.record_submission(engine, {**event, 'AttemptID': 'f' * 32, 'Digits': ''}) is False
    client.put(f'/routing/after-answer/{TO}', headers=B, json={'digits': '7'})
    sentences = client.get(f'/routing/after-answer/faxes/{job}', headers=B).json()['sentences']
    billed = after_answer.BILLED
    assert sentences == [
        f'After the call was answered, Faxbot pressed 2, pause, 105 to reach the fax machine. {billed}',
        f'After the call was answered, Faxbot pressed 9 to reach the fax machine. {billed}']


def test_the_submission_events_carry_the_keys_both_engines_pressed():
    fields = ami.prepare_originate_fields('a' * 32, TO, '/faxdata/x.tiff', caller_id=DID_A, attempt_id='b' * 32,
                                          dtmf='2')
    assert ami.requested_keys(fields) == '2'
    source = (ROOT / 'api' / 'app' / 'hylafax_engine.py').read_text()
    assert "job.submission.update(Digits=keys, Engine='sslfax')" in source
    assert 'submission["Digits"] = keys' in (ROOT / 'api' / 'app' / 'ami.py').read_text()


def test_the_command_line_sets_shows_and_clears_the_keys(client):  # noqa: F811
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    from api.tests.test_access_management_http import BOOTSTRAP

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    done = run('recipients', 'set', TO, '--after-answer', '2w105')
    assert done.exit_code == 0, (done.stdout, done.stderr)
    assert 'Faxbot will press 2, pause, 105 after this number answers' in ' '.join(done.stdout.split())
    shown = run('recipients', 'show', TO)
    assert shown.exit_code == 0, (shown.stdout, shown.stderr)
    assert 'Keys pressed after it answers' in shown.stdout and '2, pause, 105' in shown.stdout
    assert run('recipients', 'set', TO, '--after-answer', 'p9').exit_code != 0
    cleared = run('recipients', 'set', TO, '--after-answer', 'none')
    assert cleared.exit_code == 0 and 'no longer presses any keys' in ' '.join(cleared.stdout.split())


# -- dispatch: only a trunk presses keys ------------------------------------------------------------------------------

def test_only_a_trunk_may_take_a_number_that_needs_keys_and_calls_without_a_call_are_never_refused(database, tmp_path):  # noqa: F811
    from api.tests.test_rules_delivery import BASE, installation
    env = installation(database, tmp_path, {**BASE, 'FAX_OUTBOUND_ROUTES': 'signalwire'})
    values = env.configuration.read().active.values
    for route in ('sip', 'humblefax', 'local', 'direct', 'relay:abc'):
        assert after_answer.dispatch_refusal(env.engine, values, TO, route) is None
    with env.engine.begin() as connection:
        after_answer.set_on(connection, TO, '2', now=datetime.utcnow())
    assert after_answer.dispatch_refusal(env.engine, values, TO, 'sip') is None
    for route in ('signalwire', 'phaxio', 'humblefax', 'relay:abc'):
        assert after_answer.dispatch_refusal(env.engine, values, TO, route) == after_answer.SKIP
    for route in ('local', 'direct'):
        assert after_answer.dispatch_refusal(env.engine, values, TO, route) is None
    assert after_answer.dispatch_refusal(env.engine, values, '+13035550199', 'signalwire') is None


@pytest.mark.asyncio
async def test_without_a_trunk_the_fax_waits_in_sent_and_no_cloud_service_calls_the_menu(database, tmp_path):  # noqa: F811
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing.transport import RoutedTransport
    from api.tests.test_rules_delivery import BASE, Inner, accept, holds, installation
    env = installation(database, tmp_path, {**BASE, 'FAX_OUTBOUND_ROUTES': 'signalwire'})
    with env.engine.begin() as connection:
        after_answer.set_on(connection, TO, '2', now=datetime.utcnow())
    job = accept(env, to=TO)
    inner = Inner(env.delivery)
    assert await OutboundWorker(env.delivery, RoutedTransport(inner, direct=None)).step() is False
    assert inner.used == [] and env.delivery.get(job)['state'] == 'ready'
    [hold] = holds(env, job)
    assert hold['kind'] == 'no_route'
    assert "cannot press the keys this number's phone menu needs after it answers; only a trunk can" in hold['reason']


@pytest.mark.asyncio
async def test_with_a_trunk_the_cloud_routes_are_skipped_and_the_trunk_sends(database, tmp_path, monkeypatch):  # noqa: F811
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing import transport
    from api.app.routing.transport import RoutedTransport
    from api.tests.test_rules_delivery import BASE, Inner, accept, installation
    trunk = {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'sip.telnyx.com',
             'SIP_TRUNK_DIDS': DID_A, 'SIP_TRUNK_CALLER_ID': DID_A, 'FAX_OUTBOUND_ROUTES': 'signalwire, sip'}
    env = installation(database, tmp_path, {**BASE, **trunk})
    monkeypatch.setattr(transport, 'route_ready', lambda configuration, ami=None: True)
    monkeypatch.setattr(transport, 'ensure_route_artifact', lambda revision, configuration, job_id: None)
    with env.engine.begin() as connection:
        after_answer.set_on(connection, TO, '2w105', now=datetime.utcnow())
    job = accept(env, to=TO)
    inner = Inner(env.delivery)
    assert await OutboundWorker(env.delivery, RoutedTransport(inner, direct=None)).step() is True
    assert inner.used == ['sip']
    from api.app.routing import envelope as envelopes
    with env.engine.connect() as connection:
        attempt = connection.execute(sa.text('SELECT id FROM outbound_attempts WHERE job_id = :job'),
                                     {'job': job}).scalar_one()
        recorded = envelopes.choice_on(connection, attempt)
    skipped, _ = envelopes.skipped_of(recorded)
    # Whichever cloud account the planner ranked before the trunk was skipped for the keys, never sent to.
    assert skipped and {why for _, why in skipped} == {'digits'}
    assert {key for key, _ in skipped} <= {'phaxio', 'signalwire'}


def test_the_skip_reads_as_one_sentence_in_sent_and_in_a_hold():
    from app.routing.holds import no_route_sentence
    sentence = no_route_sentence(lambda key: {'signalwire': 'SignalWire'}[key], [('signalwire', 'digits')])
    assert sentence == ("No account your rules allow can send this fax now: SignalWire cannot press the keys this "
                        "number's phone menu needs after it answers; only a trunk can. It waits for you in Sent; "
                        'nothing was sent.')
    from app.routing.envelope import SKIPS
    assert 'digits' in SKIPS
