"""The SSL Fax engine (HylaFAX+): rendered files, engine choice, call plans, job submission and results.

Everything here runs without containers: a small stand-in speaks the engine's
job protocol (hfaxd), a stand-in manager connection records call plans. The
real engine is exercised by ``make sslfax-proof`` (tests/test_sslfax_loopback.py).
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading

import pytest

from app import ami, hylafax_engine, sip_trunk
from app.config_values import ConfigurationValues

ROOT = Path(__file__).resolve().parents[2]
SECRET = 'synthetic-inbound-secret-0123456789'
JOB, ATTEMPT = 'a' * 32, 'b' * 32
PEER = '+15555550199'


def trunk_values(tmp_path, **extra):
    return ConfigurationValues.from_environment({
        'FAX_DATA_DIR': str(tmp_path), 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
        'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_CALLER_ID': '+15555550100',
        'ASTERISK_INBOUND_SECRET': SECRET, **extra})


# -- rendered files ---------------------------------------------------------------------------------

def test_apply_writes_the_engine_settings_and_one_iax_peer_per_line_privately(tmp_path):
    values = trunk_values(tmp_path)
    sip_trunk.write_asterisk_configuration(values)
    iax = (tmp_path / 'asterisk' / 'iax.conf').read_text()
    engine = (tmp_path / 'hylafax' / 'engine.conf').read_text()
    stored = json.loads((tmp_path / 'hylafax' / 'secrets.json').read_text())
    for path in ('asterisk/iax.conf', 'hylafax/engine.conf', 'hylafax/secrets.json'):
        assert stat.S_IMODE(os.stat(tmp_path / path).st_mode) == 0o600
    # Two lines by default, each a peer that may only enter the engine's outbound context.
    assert iax.count('type=friend') == 2
    for number in (1, 2):
        line_secret = stored['lines'][str(number)]
        assert f'[faxbot-line{number}]' in iax and f'secret={line_secret}' in iax
        assert f'line{number}_secret={line_secret}' in engine
    assert iax.count('context=faxbot-engine-out') == 2 and 'context=faxbot-inbound' not in iax
    assert 'allow=ulaw\nallow=alaw' in iax  # the trunk's codec order (North America: mu-law first)
    settings = dict(line.split('=', 1) for line in engine.splitlines() if line and not line.startswith('#'))
    assert settings['lines'] == '2' and settings['asterisk_host'] == 'asterisk'
    assert settings['station_id'] == '+15555550100' and settings['fax_number'] == '15555550100'
    assert settings['sslfax'] == 'yes' and settings['sslfax_listener'] == ''
    assert settings['submit_user'] == 'faxbot' and settings['submit_password'] == stored['submit_password']
    # The engine reports with its own secret, never Asterisk's inbound secret.
    assert settings['report_secret'] == stored['report_secret'] != SECRET and 'inbound_secret' not in settings
    assert settings['codec'] == 'ulaw'
    assert hylafax_engine.report_secret(tmp_path) == stored['report_secret']
    assert settings['api_url'] == 'http://api:8080'


def test_engine_secrets_are_made_once_and_kept_across_apply(tmp_path):
    values = trunk_values(tmp_path)
    sip_trunk.write_asterisk_configuration(values)
    first = (tmp_path / 'hylafax' / 'secrets.json').read_text()
    iax = (tmp_path / 'asterisk' / 'iax.conf').read_text()
    sip_trunk.write_asterisk_configuration(values)
    assert (tmp_path / 'hylafax' / 'secrets.json').read_text() == first
    assert (tmp_path / 'asterisk' / 'iax.conf').read_text() == iax
    secrets_ = json.loads(first)
    assert len(secrets_['submit_password']) == 48 and all(len(v) == 32 for v in secrets_['lines'].values())


def test_no_inbound_secret_means_no_engine_files(tmp_path):
    sip_trunk.write_asterisk_configuration(trunk_values(tmp_path))
    sip_trunk.write_asterisk_configuration(trunk_values(tmp_path, ASTERISK_INBOUND_SECRET=''))
    assert not (tmp_path / 'hylafax' / 'engine.conf').exists()
    assert not (tmp_path / 'asterisk' / 'iax.conf').exists()


def test_engine_settings_refuse_values_that_could_break_the_file(tmp_path):
    values = trunk_values(tmp_path)
    engine_secret = hylafax_engine.engine_secrets(values)
    with pytest.raises(ValueError):
        hylafax_engine.render_engine_conf(values, engine_secret, report_secret='short')
    with pytest.raises(ValueError):
        hylafax_engine.render_engine_conf(values, engine_secret, report_secret=SECRET, listener='host:1\nlines=8')
    text = hylafax_engine.render_engine_conf(values, engine_secret, report_secret=SECRET,
                                             listener='fax.example.net:10443')
    assert 'sslfax_listener=fax.example.net:10443' in text
    off = hylafax_engine.render_engine_conf(values, engine_secret, report_secret=SECRET,
                                            listener='fax.example.net:10443', sslfax=False)
    assert 'sslfax=no' in off and 'sslfax_listener=\n' in off
    # A hand-set inbound secret the engine cannot carry leaves the engine not set up; Apply still works.
    path = sip_trunk.write_asterisk_configuration(trunk_values(tmp_path, ASTERISK_INBOUND_SECRET='with spaces ok?'))
    assert path.exists()


def test_asterisk_must_have_loaded_the_lines_before_the_trunk_counts_as_current(tmp_path):
    values = trunk_values(tmp_path)
    sip_trunk.write_asterisk_configuration(values)
    shared = tmp_path / 'asterisk'
    (shared / 'pjsip.conf.started').write_text(sip_trunk.render_pjsip(values))
    (tmp_path / 'asterisk' / 'public-address.applied').write_text('')
    assert not sip_trunk.engine_uses_current(values)
    (shared / 'iax.conf.started').write_bytes((shared / 'iax.conf').read_bytes())
    assert hylafax_engine.asterisk_loaded_lines(values)
    # The fax options for received calls (and the engine's lines) must be loaded too.
    assert not hylafax_engine.iax_current(values)
    (shared / 'extensions-options.conf.started').write_bytes((shared / 'extensions-options.conf').read_bytes())
    assert hylafax_engine.iax_current(values)


# -- Asterisk side: modules, dialplan, start script ----------------------------------------------------

def test_dialplan_tries_the_engine_lines_in_turn_and_reports_a_call_no_free_line_answered():
    """Ringing every line at once started two engine sessions for one call (6 October 2026). The lines are
    tried one at a time, first free line first, all of them within 20 s as before; a free line that rang and
    did not answer is reported (FaxEngineMissed) and the built-in engine answers the call."""
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    start = text.index('[faxbot-engine-in]')
    section = text[start:text.index('[faxbot-engine-in-answered]')]
    assert 'Dial(${FAXBOT_ENGINE_LINES}' not in section
    assert ('Dial(IAX2/faxbot-line${FAXBOT_LINE}/${FAXBOT_ENGINE_DID},${FAXBOT_RING},U(faxbot-engine-in-answered))'
            in section)
    assert '${DEVICE_STATE(IAX2/faxbot-line${FAXBOT_LINE})}' in section
    # Each line rings for what is left of the 20 s, 12 s at most.
    assert 'Set(FAXBOT_RING=$[20 - (${EPOCH} - ${FAXBOT_HUNT_START})])' in section
    assert 'GotoIf($[${FAXBOT_RING} < 1]?none)' in section
    assert 'Set(FAXBOT_RING=${IF($[${FAXBOT_RING} > 12]?12:${FAXBOT_RING})})' in section
    assert 'UserEvent(FaxEngineMissed,' in section
    # Every line Faxbot can set up is tried.
    assert f'${{FAXBOT_LINE}} > {hylafax_engine.MAX_LINES}]?none)' in section
    assert section.index('UserEvent(FaxEngineMissed,') < section.index('(builtin),')
    import re
    for line in section.splitlines():
        if line.strip() and not line.lstrip().startswith(';'):
            assert re.search(r'(?<!\\);', line) is None, line


def test_dialplan_places_engine_calls_only_from_a_stored_plan_and_never_reports_a_fax_result():
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    start = text.index('[faxbot-engine-out]')
    section = text[start:text.index('[faxbot-inbound]')]
    assert '${DB_DELETE(faxbot-engine/${FAXBOT_TAG})}' in section
    assert 'GotoIf($["${FAXBOT_PLAN}" = ""]?refuse)' in section
    # The plan's trunk (a seventh field after the first trunk), else the first trunk's endpoint; only a loaded
    # endpoint named trunk-...-endpoint is dialed.
    assert 'Dial(PJSIP/${FAXBOT_DIAL}@${FAXBOT_ENDPOINT}' in section
    assert '${CUT(FAXBOT_PLAN,/,7)}' in section and '?trunk-endpoint:${FAXBOT_ENDPOINT}' in section
    assert 'GotoIf($["${PJSIP_ENDPOINT(${FAXBOT_ENDPOINT},context)}" = ""]?refuse)' in section
    assert 'GotoIf($[${REGEX("^trunk-([a-z0-9_-]+-)?endpoint$" ${FAXBOT_ENDPOINT})} = 0]?refuse)' in section
    # T.38 on the trunk: the fax gateway joins the engine's audio to T.38; off: audio end to end.
    assert 'PJSIP_ENDPOINT(${FAXBOT_ENDPOINT},t38_udptl)' in section and 'Set(FAXOPT(gateway)=yes)' in section
    # The delivery result comes from the engine, never from this dialplan.
    assert 'UserEvent(FaxResult' not in section and 'UserEvent(FaxEngineCall,' in section
    assert 'SendFAX' not in section and 'ReceiveFAX' not in section
    # In extensions.conf a bare ; starts a comment and silently cuts the line short.
    import re
    for line in section.splitlines():
        if line.strip() and not line.lstrip().startswith(';'):
            assert re.search(r'(?<!\\);', line) is None, line
    modules = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'modules.conf').read_text()
    assert 'load => chan_iax2.so' in modules and 'noload => chan_iax2.so' not in modules


def test_call_plan_carries_the_carrier_number_caller_id_and_preference(tmp_path):
    values = trunk_values(tmp_path, SIP_FAX_PREFERENCE_HEADER='true')
    fields = ami.originate_fields_for(values, JOB, '+15555550199', '/faxdata/x.tiff', attempt_id=ATTEMPT)
    plan = hylafax_engine.call_plan(fields, JOB, ATTEMPT)
    dial, caller, job, attempt, preference, t38 = plan.split('/')
    assert t38 == '1'
    assert hylafax_engine.call_plan(fields, JOB, ATTEMPT, t38=False).endswith('/1/0')
    assert fields['Channel'] == f'PJSIP/{dial}@trunk-endpoint'
    assert caller == '+15555550100' and (job, attempt, preference) == (JOB, ATTEMPT, '1')
    fields_off = ami.originate_fields_for(trunk_values(tmp_path, SIP_FAX_PREFERENCE_HEADER='false'), JOB,
                                          '+15555550199', '/faxdata/x.tiff', attempt_id=ATTEMPT)
    assert hylafax_engine.call_plan(fields_off, JOB, ATTEMPT).endswith('/0/1')
    with pytest.raises(ValueError):
        hylafax_engine.call_plan({**fields, 'Channel': 'PJSIP/1/2@trunk-endpoint'}, JOB, ATTEMPT)
    with pytest.raises(ValueError):
        hylafax_engine.call_plan(fields, 'not-hex', ATTEMPT)


def test_call_tags_are_sixteen_digits_and_never_repeat():
    tags = {hylafax_engine.new_tag() for _ in range(2000)}
    assert len(tags) == 2000 and all(len(tag) == 16 and tag.isdigit() and tag[0] != '0' for tag in tags)


# -- engine choice --------------------------------------------------------------------------------------

class FakeAmi:
    def __init__(self, ready=2, fail=None):
        self.ready, self.fail, self.puts, self.deletes = ready, fail, [], []

    async def iax_lines_ready(self, prefix):
        assert prefix == 'faxbot-line'
        if self.fail:
            raise self.fail
        return self.ready

    async def db_put(self, family, key, value):
        self.puts.append((family, key, value))

    async def db_del(self, family, key):
        self.deletes.append((family, key))


def running(tmp_path, values, state='running'):
    sip_trunk.write_asterisk_configuration(values)
    (tmp_path / 'hylafax-out').mkdir(exist_ok=True)
    (tmp_path / 'hylafax-out' / 'engine.status').write_text(json.dumps({'state': state, 'lines': 2}))
    (tmp_path / 'asterisk' / 'iax.conf.started').write_bytes((tmp_path / 'asterisk' / 'iax.conf').read_bytes())


@pytest.mark.asyncio
async def test_engine_choice_says_why_the_built_in_engine_places_a_call(tmp_path):
    values = trunk_values(tmp_path)
    choose = hylafax_engine.choose
    assert await choose(values, ami=FakeAmi()) == hylafax_engine.EngineChoice('builtin', hylafax_engine.NOT_RUNNING)
    running(tmp_path, values)
    assert await choose(values, ami=FakeAmi()) == hylafax_engine.EngineChoice('hylafax')
    assert (await choose(values, members=True, ami=FakeAmi())).reason == hylafax_engine.SENDING_TOGETHER
    assert (await choose(values, ami=FakeAmi(ready=0))).reason == hylafax_engine.LINES_NOT_READY
    assert (await choose(values, ami=FakeAmi(fail=ConnectionError()))).reason == hylafax_engine.LINES_NOT_READY
    (tmp_path / 'asterisk' / 'iax.conf.started').write_text('older lines')
    assert (await choose(values, ami=FakeAmi())).reason == hylafax_engine.ASTERISK_NOT_CURRENT
    running(tmp_path, values, state='failed')
    assert (await choose(values, ami=FakeAmi())).reason == hylafax_engine.NOT_RUNNING


@pytest.mark.asyncio
async def test_the_engine_a_sender_chose_for_one_fax_is_kept_or_said_why_not(tmp_path):
    """POST /fax engine: builtin always gets the built-in engine; hylafax gets this engine when it is ready, else the
    built-in engine as with no choice, with a sentence; auto chooses as before; sending together stays built-in."""
    values = trunk_values(tmp_path)
    choose, choice = hylafax_engine.choose, hylafax_engine.EngineChoice
    # Not running: a hylafax choice goes on the built-in engine and says so; never waits, never fails.
    assert await choose(values, ami=FakeAmi(), requested='hylafax') == choice('builtin',
                                                                               hylafax_engine.CHOSE_ENGINE_NOT_READY)
    assert await choose(values, ami=FakeAmi(), requested='builtin') == choice('builtin', hylafax_engine.CHOSE_BUILTIN)
    running(tmp_path, values)
    # Running: builtin still gets the built-in engine; hylafax and auto get this engine.
    assert await choose(values, ami=FakeAmi(), requested='builtin') == choice('builtin', hylafax_engine.CHOSE_BUILTIN)
    assert await choose(values, ami=FakeAmi(), requested='hylafax') == choice('hylafax')
    assert await choose(values, ami=FakeAmi(), requested='auto') == choice('hylafax')
    assert await choose(values, ami=FakeAmi(), requested=None) == choice('hylafax')
    # Still starting: said as starting.
    assert (await choose(values, ami=FakeAmi(ready=0), requested='hylafax')).reason == (
        hylafax_engine.CHOSE_ENGINE_STARTING)
    # Faxes sent together in one call go on the built-in engine whatever was chosen.
    for requested in ('hylafax', 'builtin', 'auto'):
        assert (await choose(values, members=True, ami=FakeAmi(), requested=requested)).reason == (
            hylafax_engine.SENDING_TOGETHER)


@pytest.fixture
def chosen_store(tmp_path):
    """A delivery store with one fax to the trunk whose sender chose an engine; ``accept(engine)`` adds one."""
    from datetime import datetime
    from uuid import uuid4
    import sqlalchemy as sa  # noqa: F401
    from api.app.config_profiles import ProviderConfiguration
    from api.app.config_store import ConfigurationStore
    from api.app.config_values import ConfigurationValues as StoreValues
    from api.app.outbound_store import OutboundStore, engine_request_recorder
    from api.app.schema import create_database_engine, upgrade_schema
    database = create_database_engine('sqlite:///' + str(tmp_path / 'chosen.db'))
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    snapshot = configuration.initialize(StoreValues.from_environment({'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false'}),
                                        actor='test', providers={'outbound': ProviderConfiguration(
                                            'phaxio', credentials={'api_key': 'synthetic-key'})})

    def accept(engine):
        job_id, now = uuid4().hex, datetime.utcnow()
        configuration.accept_outbound(snapshot.active, {'id': job_id, 'to_number': '+15551230009',
                                                        'file_name': 'synthetic.pdf', 'tiff_path': '',
                                                        'status': 'queued', 'pages': 1, 'created_at': now,
                                                        'updated_at': now})
        record = engine_request_recorder(job_id, engine)
        if record is not None:
            with database.begin() as connection:
                record(connection, now)
        return job_id
    yield configuration, OutboundStore, accept
    database.dispose()


def test_the_engine_choice_survives_a_retry_and_a_claim_after_a_restart_and_the_call_honours_it(chosen_store,
                                                                                               tmp_path):
    """The choice is kept with the fax, so every attempt reads it (load_dispatch), including one claimed by a new
    worker after a restart; the trunk call honours it: builtin while the engine runs, hylafax while it is down goes
    built-in with its sentence, and faxes sent together go built-in."""
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from app import outbound_transport
    configuration, OutboundStore, accept = chosen_store
    job = accept('builtin')
    first = OutboundStore(configuration)
    claim = first.claim('worker-before-restart')
    assert claim.job_id == job and first.load_dispatch(claim)[2]['engine_request'] == 'builtin'
    # The worker stops (a restart); its lease runs out and a new worker claims the fax again.
    after = OutboundStore(configuration)
    after.recover_expired(now=datetime.utcnow() + timedelta(minutes=5))
    again = after.claim('worker-after-restart')
    assert again.job_id == job and again.attempt_id != claim.attempt_id
    _, _, dispatched = after.load_dispatch(again)
    assert dispatched['engine_request'] == 'builtin'
    values = trunk_values(tmp_path)
    running(tmp_path, values)
    transport = SimpleNamespace(store=after, ami=FakeAmi())

    def prepare(job_row, members=()):
        call = SimpleNamespace(job_id=job, attempt_id=again.attempt_id, members=members)
        return asyncio.run(outbound_transport.CapturedTransport._prepare_engine(
            transport, values, call, job_row, str(tmp_path / 'synthetic.tiff')))
    engine_job, choice, _, _ = prepare(dispatched)
    assert engine_job is None and (choice.engine, choice.reason) == ('builtin', hylafax_engine.CHOSE_BUILTIN)
    # Sending together always goes on the built-in engine.
    _, choice, _, _ = prepare({**dispatched, 'engine_request': 'hylafax'}, members=(again, again))
    assert (choice.engine, choice.reason) == ('builtin', hylafax_engine.SENDING_TOGETHER)
    # The SSL Fax engine chosen while it is down: the built-in engine, and the sentence says why.
    running(tmp_path, values, state='failed')
    _, choice, _, _ = prepare({**dispatched, 'engine_request': 'hylafax'})
    assert (choice.engine, choice.reason) == ('builtin', hylafax_engine.CHOSE_ENGINE_NOT_READY)
    # Auto keeps nothing: the engine is chosen as before.
    from api.app.outbound_store import requested_engine_on
    automatic = accept('auto')
    with configuration.engine.connect() as connection:
        assert requested_engine_on(connection, automatic) is None
        assert requested_engine_on(connection, job) == 'builtin'


@pytest.fixture
def engine_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def test_post_fax_and_faxbot_send_keep_the_engine_choice_with_the_fax(engine_cli, tmp_path):
    """POST /fax engine and faxbot send --engine: kept with the fax at acceptance, said in Sent details and in
    faxbot faxes sent show; auto (the default) keeps nothing; anything else is refused."""
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = engine_cli, {'X-API-Key': BOOTSTRAP}

    def post(**extra):
        return cli.client.post('/fax', headers=admin, data={'to': '+15551230001', 'queue_only': 'true', **extra},
                               files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    for value, sentence in (('builtin', hylafax_engine.CHOSE_BUILTIN), ('hylafax', hylafax_engine.CHOSE_ENGINE)):
        accepted = post(engine=value)
        assert accepted.status_code == 202, accepted.text
        job = accepted.json()['id']
        view = cli.client.get(f'/admin/fax-jobs/{job}', headers=admin).json()
        assert view['engine_choice'] == {'engine': value, 'sentence': sentence}
        assert sentence in ' '.join(cli('sent', 'show', job).stdout.split())
    for extra in ({'engine': 'auto'}, {}):
        job = post(**extra).json()['id']
        assert cli.client.get(f'/admin/fax-jobs/{job}', headers=admin).json()['engine_choice'] is None
    refused = post(engine='fast')
    assert refused.status_code == 422
    # The same idempotency key with another engine is another request.
    first = cli.client.post('/fax', headers={**admin, 'Idempotency-Key': 'engine-key'},
                            data={'to': '+15551230001', 'queue_only': 'true', 'engine': 'builtin'},
                            files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    other = cli.client.post('/fax', headers={**admin, 'Idempotency-Key': 'engine-key'},
                            data={'to': '+15551230001', 'queue_only': 'true', 'engine': 'hylafax'},
                            files={'file': ('note.txt', b'Synthetic page\n', 'text/plain')})
    assert first.status_code == 202 and other.status_code == 409
    # The command line.
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic command line fax\n')
    sent = cli.json('send', '+15551230002', note, '--queue', '--engine', 'builtin')
    shown = ' '.join(cli('sent', 'show', sent['id']).stdout.split())
    assert 'Fax engine' in shown and hylafax_engine.CHOSE_BUILTIN in shown
    plain = cli.json('send', '+15551230002', note, '--queue')
    assert 'Fax engine' not in cli('sent', 'show', plain['id']).stdout
    wrong = cli('send', '+15551230002', note, '--queue', '--engine', 'fast')
    assert wrong.exit_code != 0 and 'Choose --engine auto, builtin or hylafax.' in wrong.stderr + wrong.stdout
    # D20: after the call, the built-in engine's reason is the choice itself: said once, not twice. A choice the
    # engine could not keep is said with what happened.
    import app.main as main_module
    from app.hylafax_records import records_for
    database = main_module.app.state.configuration_runtime.manager.store.engine
    records_for(database).record_call(direction='outbound', call_key='a' * 32, engine='builtin', job_id=sent['id'],
                                      reason=hylafax_engine.CHOSE_BUILTIN, number='+15551230002')
    shown = ' '.join(cli('sent', 'show', sent['id']).stdout.split())
    assert shown.count(hylafax_engine.CHOSE_BUILTIN) == 1
    wanted = cli.json('send', '+15551230002', note, '--queue', '--engine', 'hylafax')
    records_for(database).record_call(direction='outbound', call_key='b' * 32, engine='builtin', job_id=wanted['id'],
                                      reason=hylafax_engine.CHOSE_ENGINE_NOT_READY, number='+15551230002')
    shown = ' '.join(cli('sent', 'show', wanted['id']).stdout.split())
    assert hylafax_engine.CHOSE_ENGINE in shown and hylafax_engine.CHOSE_ENGINE_NOT_READY in shown
    # The same idempotency key for a different fax request: one plain sentence, never the server's own words.
    first = cli('send', '+15551230002', note, '--queue', '--engine', 'builtin', '--idempotency-key', 'cli-engine')
    assert first.exit_code == 0, first.stdout
    other = cli('send', '+15551230002', note, '--queue', '--engine', 'hylafax', '--idempotency-key', 'cli-engine')
    said = ' '.join((other.stdout + other.stderr).split())
    assert other.exit_code != 0 and 'This idempotency key was already used for a different fax.' in said
    assert 'Idempotency-Key already belongs' not in said


@pytest.mark.asyncio
async def test_a_call_the_engine_did_not_answer_restarts_it_and_the_trunk_page_says_why(tmp_path, monkeypatch):
    """Asterisk's FaxEngineMissed (a free line rang and did not answer): Faxbot asks the engine to start again
    (it reads the request once no call is up) and says what happened in one sentence for a day."""
    import asyncio
    import sqlalchemy as sa
    from app import sip_calls
    from tests.test_native_submission import connected_stream
    values = trunk_values(tmp_path, FAX_TIME_ZONE='America/Denver')
    running(tmp_path, values)
    monkeypatch.setattr('app.config.configuration_values', lambda: values)
    audits = []
    monkeypatch.setattr('app.audit.audit_event', lambda name, **details: audits.append((name, details)))
    missed_at = 1791256440  # 6 October 2026 03:14 UTC: 9:14 PM MDT on 5 October
    frame = {'Event': 'UserEvent', 'UserEvent': 'FaxEngineMissed', 'Token': '17912564401',
             'DID': '+15555550100', 'Caller': '+13035550100', 'Started': str(missed_at),
             'Lines': '1:NOANSWER 2:NOANSWER '}
    async with connected_stream(monkeypatch) as (client, _writer):
        sip_calls.attach(client, sa.create_engine('sqlite://'))
        try:
            client.reader.feed_data((''.join(f'{key}: {value}\r\n' for key, value in frame.items()) + '\r\n')
                                    .encode())
            for _ in range(50):
                if hylafax_engine.restart_request(values):
                    break
                await asyncio.sleep(0.02)
        finally:
            sip_calls.detach()
    assert audits == [('sip_engine_restart_requested',
                       {'backend': 'sip', 'reason': 'missed_call', 'lines': '1:NOANSWER 2:NOANSWER'})]
    request = hylafax_engine.restart_request(values)
    assert request['reason'] == 'missed_call' and request['at'] == missed_at
    status = tmp_path / 'hylafax-out' / 'engine.status'
    status.write_text(json.dumps({'state': 'running', 'lines': 2, 'started': request['asked'] - 5}))
    monkeypatch.setattr('time.time', lambda: missed_at + 60)
    state, sentence = await hylafax_engine.engine_summary(values, FakeAmi())
    assert state == 'running' and sentence == (
        "Faxbot's fax engine did not answer the 9:14 PM MDT fax call, so that fax was received the ordinary "
        "way; Faxbot is restarting the fax engine.")
    status.write_text(json.dumps({'state': 'running', 'lines': 2, 'started': request['asked'] + 5}))
    assert (await hylafax_engine.engine_summary(values, FakeAmi()))[1].endswith(
        'Faxbot restarted the fax engine.')
    # An engine on audio fax by itself still says so (each screen adds its own way to try T.38 again).
    monkeypatch.setattr(hylafax_engine, 'engine_audio', lambda values: True)
    assert (await hylafax_engine.engine_summary(values, FakeAmi()))[1].endswith(
        'Faxbot restarted the fax engine. ' + hylafax_engine.ENGINE_AUDIO)
    monkeypatch.setattr(hylafax_engine, 'engine_audio', lambda values: False)
    # A day later the page is back to the usual sentence.
    monkeypatch.setattr('time.time', lambda: missed_at + hylafax_engine.MISSED_SHOWN + 60)
    assert (await hylafax_engine.engine_summary(values, FakeAmi()))[1].startswith(
        "Faxbot's fax engine is running on 2 fax lines")


@pytest.mark.asyncio
async def test_the_engines_own_states_while_it_starts_or_has_lost_a_line_are_shown(tmp_path):
    """The engine writes "starting" while its lines get ready and its own sentence for a line it lost."""
    values = trunk_values(tmp_path)
    running(tmp_path, values)
    status = tmp_path / 'hylafax-out' / 'engine.status'
    status.write_text(json.dumps({'state': 'starting', 'reason': '', 'lines': 2}))
    assert await hylafax_engine.engine_summary(values, FakeAmi()) == ('starting', hylafax_engine.STARTING)
    status.write_text(json.dumps({'state': 'restarting', 'reason': hylafax_engine.LINE_DOWN, 'lines': 2}))
    assert await hylafax_engine.engine_summary(values, FakeAmi()) == ('starting', hylafax_engine.LINE_DOWN)


def test_a_manual_restart_request_is_kept_for_the_engine_and_refuses_other_reasons(tmp_path):
    values = trunk_values(tmp_path)
    assert hylafax_engine.request_restart(values, reason='manual')
    request = hylafax_engine.restart_request(values)
    assert request['reason'] == 'manual'
    assert stat.S_IMODE(hylafax_engine.restart_request_path(values).stat().st_mode) == 0o644
    with pytest.raises(ValueError):
        hylafax_engine.request_restart(values, reason='because')


# -- job submission against a stand-in engine ------------------------------------------------------------

class FakeEngine(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, password):
        self.password, self.commands, self.uploads = password, [], []
        super().__init__(('127.0.0.1', 0), FakeSession)


class FakeSession(socketserver.StreamRequestHandler):
    def reply(self, text):
        self.wfile.write((text + '\r\n').encode())

    def handle(self):
        server = self.server
        self.reply('220 stand-in engine ready.')
        listener = None
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode().strip()
            server.commands.append(command)
            verb = command.split(' ', 1)[0].upper()
            if verb == 'USER':
                self.reply('331 Password required for faxbot.')
            elif verb == 'PASS':
                self.reply('230 User faxbot logged in.' if command[5:] == server.password else '530 Login incorrect.')
            elif verb == 'TYPE':
                self.reply('200 Type set to Image.')
            elif verb == 'PASV':
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                port = listener.getsockname()[1]
                self.reply(f'227 Entering Passive Mode (127,0,0,1,{port >> 8},{port & 255})')
            elif verb in {'STOT', 'STOR'}:
                # STOT names the temporary document itself; STOR (a held fax in pollq) keeps the client's name.
                name = command.split(' ', 1)[1] if verb == 'STOR' else '/tmp/doc7.tif'
                self.reply(f'150 FILE: {name} (Opening new data connection).')
                connection, _ = listener.accept()
                data = b''
                while chunk := connection.recv(65536):
                    data += chunk
                connection.close()
                listener.close()
                server.uploads.append(data if verb == 'STOT' else (name, data))
                self.reply(f'226 Transfer complete (FILE: {name}).')
            elif verb == 'DELE':
                name = command.split(' ', 1)[1]
                if any(isinstance(item, tuple) and item[0] == name for item in server.uploads):
                    server.uploads = [item for item in server.uploads if not (isinstance(item, tuple) and item[0] == name)]
                    self.reply(f'250 {name} deleted.')
                else:
                    self.reply(f'550 {name}: No such file or directory.')
            elif verb == 'JNEW':
                self.reply('200 New job created: jobid: 7 groupid: 7.')
            elif verb in {'JPARM', 'JSUBM', 'JDELE'}:
                self.reply('200 Job 7 submitted.' if verb == 'JSUBM' else '213 Job parameter set.'
                           if verb == 'JPARM' else '200 Job 7 deleted.')
            elif verb == 'QUIT':
                self.reply('221 Goodbye.')
                return
            else:
                self.reply('500 Unknown command.')


@pytest.fixture
def engine(tmp_path):
    values = trunk_values(tmp_path)
    password = hylafax_engine.engine_secrets(values)['submit_password']
    server = FakeEngine(password)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield values, server
    server.shutdown()
    server.server_close()


def test_a_job_is_created_with_one_dial_one_try_and_the_tag_then_submitted_only_on_submit(engine, tmp_path):
    values, server = engine
    image = tmp_path / 'fax.tiff'
    image.write_bytes(b'II*\x00synthetic')
    tag = hylafax_engine.new_tag()
    job = hylafax_engine.create_job(values, tag=tag, job_id=JOB, attempt_id=ATTEMPT, tiff_path=str(image),
                                    header='Faxbot proof 100%', host='127.0.0.1', port=server.server_address[1])
    assert job.engine_job == '7' and server.uploads == [image.read_bytes()]
    assert 'JSUBM' not in server.commands
    parms = [command for command in server.commands if command.startswith('JPARM')]
    assert f'JPARM DIALSTRING "{tag}"' in parms and f'JPARM JOBINFO "{JOB}.{ATTEMPT}"' in parms
    assert 'JPARM MAXDIALS 1' in parms and 'JPARM MAXTRIES 1' in parms
    assert 'JPARM NOTIFY "DONE+REQUEUE"' in parms and 'JPARM DOCUMENT /tmp/doc7.tif' in parms
    # Faxbot's header line (47 CFR 68.318(d): date and time, who sends, the station ID, the page), with % kept
    # literal through HylaFAX's strftime and its own % codes (routing/reply_number.tagline).
    assert ('JPARM TAGLINE "%d %b %Y %H:%M|Faxbot proof 100%%%%|%%l|Page %%P of %%T"' in parms
            and 'JPARM USETAGLINE YES' in parms)
    assert job.submit() == '7' and server.commands[-1] == 'JSUBM'
    job.close()
    # No header in Faxbot: no header line from the engine either.
    server.commands.clear()
    hylafax_engine.create_job(values, tag=tag, job_id=JOB, attempt_id=ATTEMPT, tiff_path=str(image),
                              host='127.0.0.1', port=server.server_address[1]).discard()
    assert 'JPARM USETAGLINE NO' in server.commands
    assert not any(command.startswith('JPARM TAGLINE') for command in server.commands)


def test_a_job_that_is_never_submitted_is_removed_and_a_wrong_login_is_an_engine_error(engine, tmp_path):
    values, server = engine
    image = tmp_path / 'fax.tiff'
    image.write_bytes(b'II*\x00synthetic')
    job = hylafax_engine.create_job(values, tag=hylafax_engine.new_tag(), job_id=JOB, attempt_id=ATTEMPT,
                                    tiff_path=str(image), host='127.0.0.1', port=server.server_address[1])
    job.discard()
    assert 'JDELE 7' in server.commands and 'JSUBM' not in server.commands
    server.password = 'something-else'
    with pytest.raises(hylafax_engine.EngineError):
        hylafax_engine.create_job(values, tag=hylafax_engine.new_tag(), job_id=JOB, attempt_id=ATTEMPT,
                                  tiff_path=str(image), host='127.0.0.1', port=server.server_address[1])
    with pytest.raises(ValueError):
        hylafax_engine.create_job(values, tag='12345', job_id=JOB, attempt_id=ATTEMPT, tiff_path=str(image))


@pytest.mark.asyncio
async def test_the_call_plan_is_removed_when_the_engine_cannot_take_the_job(tmp_path, monkeypatch):
    values = trunk_values(tmp_path)
    manager = FakeAmi()
    monkeypatch.setattr(hylafax_engine, 'ENGINE_HOST', '127.0.0.1')
    closed = socket.socket()
    closed.bind(('127.0.0.1', 0))
    port = closed.getsockname()[1]
    closed.close()
    # host, port and timeout are keyword-only: point them at a port nothing listens on.
    monkeypatch.setattr(hylafax_engine.create_job, '__kwdefaults__',
                        {**hylafax_engine.create_job.__kwdefaults__, 'host': '127.0.0.1', 'port': port,
                         'timeout': 2.0})
    image = tmp_path / 'fax.tiff'
    image.write_bytes(b'II*\x00synthetic')
    with pytest.raises(hylafax_engine.EngineError):
        await hylafax_engine.prepare_job(values, manager, job_id=JOB, attempt_id=ATTEMPT, dest='+15555550199',
                                         tiff_path=str(image))
    assert len(manager.puts) == 1 and manager.deletes == [(manager.puts[0][0], manager.puts[0][1])]
    family, tag, plan = manager.puts[0]
    assert family == 'faxbot-engine' and len(tag) == 16 and plan.split('/')[2:4] == [JOB, ATTEMPT]


# -- results ---------------------------------------------------------------------------------------------

def result(why, **fields):
    return {'tag': f'{JOB}.{ATTEMPT}', 'why': why, **fields}


def b64(text):
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize('payload, outcome', [
    (result('done', dials=1, pages=2), ('success', None, None)),
    (result('requeued', dials=1), ('in_progress', None, None)),
    (result('failed', dials=1, pages=0, status_b64=b64('Busy signal detected')),
     ('failed', 'The fax did not go through: the line was busy.', None)),
    (result('failed', dials=1, pages=0, status_b64=b64('No answer from remote')),
     ('failed', 'The fax did not go through: no one answered.', None)),
    (result('failed', dials=1, pages=2, total_pages=5),
     ('failed', 'The call ended after 2 pages; the rest was not confirmed.', 'partly_sent')),
    (result('timedout', dials=0), ('failed', 'The other fax machine did not confirm the pages.', None)),
    # Removed or rejected after a dial (an engine restart, a person's faxrm): uncertain, never failed.
    (result('rejected', dials=1), (hylafax_engine.UNCERTAIN, None, None)),
    (result('killed', total_dials=1), (hylafax_engine.UNCERTAIN, None, None)),
    (result('something-new', dials=1), (hylafax_engine.UNCERTAIN, None, None)),
])
def test_engine_results_map_to_delivery_outcomes(payload, outcome):
    assert hylafax_engine.result_outcome(payload) == outcome


UNCONFIRMED = (hylafax_engine.UNCERTAIN, hylafax_engine.NOT_CONFIRMED, 'pages_unconfirmed')


@pytest.mark.parametrize('status, code', [
    ('No response to EOP repeated 3 tries {E151}', 'E151'),
    ('No response to MPS repeated 3 tries {E150}', 'E150'),
    ('No response to PPS repeated 3 times. {E147}', 'E147'),
    ('Unable to transmit page (giving up after 3 attempts) {E131}', 'E131'),
    ('Remote fax disconnected prematurely {E128}', 'E128'),
])
def test_a_send_whose_page_went_out_unconfirmed_waits_for_a_person_and_is_never_resent(status, code):
    """HylaFAX counts only confirmed pages: "No response to EOP" with no page counted follows a page the other
    machine may have printed. It waits for a person (pages_unconfirmed), never another route."""
    payload = result('failed', dials=1, pages=0, status_b64=b64(status), status_code=code)
    assert hylafax_engine.result_outcome(payload) == UNCONFIRMED
    # Without the code, the words alone keep it open too.
    assert hylafax_engine.result_outcome({**payload, 'status_code': ''})[0] == hylafax_engine.UNCERTAIN


@pytest.mark.parametrize('extra', [
    {'remote_station_b64': b64('+1 303 426 5097')},
    {'signal_rate_b64': b64('9600 bit/s')},
])
def test_a_send_that_reached_the_fax_exchange_is_never_a_certain_failure(extra):
    """The other machine named itself, or a speed was agreed: pages may have arrived, whatever the code."""
    payload = result('failed', dials=1, pages=0, status_b64=b64('No carrier detected {E002}'), status_code='E002',
                     **extra)
    assert hylafax_engine.result_outcome(payload)[0] == hylafax_engine.UNCERTAIN
    assert hylafax_engine.result_outcome(payload)[2] == 'pages_unconfirmed'


@pytest.mark.parametrize('status, code, sentence', [
    ('Busy signal detected {E001}', 'E001', 'The fax did not go through: the line was busy.'),
    ('No answer from remote {E003}', 'E003', 'The fax did not go through: no one answered.'),
    ('No carrier detected {E002}', 'E002', 'The other end did not answer as a fax machine.'),
    ('No receiver protocol (T.30 T1 timeout) {E126}', 'E126', hylafax_engine.NOT_CONFIRMED),
    ('Busy signal detected', '', 'The fax did not go through: the line was busy.'),
])
def test_a_send_that_ended_before_any_fax_data_failed_for_certain(status, code, sentence):
    """Busy, no answer, no carrier, no T.30 answer (HylaFAX+ 7.0.11's codes): nothing was sent, so another
    route may send it."""
    payload = result('failed', dials=1, pages=0, status_b64=b64(status), status_code=code)
    assert hylafax_engine.result_outcome(payload) == ('failed', sentence, None)


TRAINING = [('Failure to train modems {E105}', 'E105'),
            ('Failure to train remote modem at 2400 bps or minimum speed {E137}', 'E137'),
            ('Unspecified failure to train with receiver {E156}', 'E156')]


@pytest.mark.parametrize('status, code', TRAINING)
@pytest.mark.parametrize('with_code', [True, False], ids=['code', 'words'])
def test_a_send_whose_training_never_succeeded_failed_for_certain_after_the_other_machine_named_itself(
        status, code, with_code):
    """#73, acceptance 10 October 2026: HP's test line sent its CSI, then HylaFAX gave up with "Unspecified failure
    to train with receiver {E156}", 0 pages. No page data follows failed training, so another route may send it;
    it waited for a person instead, with the number held for 30 minutes."""
    payload = result('failed', dials=1, pages=0, status_b64=b64(status), status_code=code if with_code else '',
                     remote_station_b64=b64('HP TEST LINE'), session_seconds=45)
    assert hylafax_engine.result_outcome(payload) == (
        'failed', 'The fax machines could not agree on a speed; no page was sent.', None)


# HP's test line +18884732963, 11 October 2026 03:19Z, as the SSL Fax engine reported it (defect #49): the job
# file (doneq/q19) said "errorcode:E156" and "status:Unspecified failure to train with receiver ; too many
# attempts to dial" (HylaFAX+ 7.0.11 takes "{E156}" out of status), and bin/notify read a "statuscode" field
# that does not exist, so the result carried no code. The other machine named itself (CSI "HPFAX"), none of
# eight trainings was answered, and the session log was read (87 seconds, bin/negotiation's summary below).
HP_NEGOTIATION = {'rate_first': None, 'rate_lowest': None, 'rate_last': None, 'trainings': 8, 'compression': None,
                  'resolution': None, 'ecm': None, 'session': 87, 'page_length': 'unlimited', 'page_width': 'A3',
                  'fine': 1, 'remote_ecm': 0, 'scan_ms': 0, 'remote_codings': 'MH,MR', 'boundary_ms': None,
                  'boundaries': None}


def hp_result(**changes):
    return result('failed', **{
        'state': 8, 'pages': 0, 'total_pages': 1, 'dials': 0, 'total_dials': 1, 'total_tries': 1,
        'status_b64': b64('Unspecified failure to train with receiver ; too many attempts to dial'),
        'status_code': '', 'remote_station_b64': b64('HPFAX'), 'signal_rate_b64': '', 'data_format_b64': '',
        'sslfax': False, 'sslfax_offered': False, 'sslfax_refused': False, 'transfer_seconds': None,
        'session_seconds': 87, 'negotiation_b64': b64(json.dumps(HP_NEGOTIATION)), **changes})


@pytest.mark.parametrize('code', ['', 'E156'], ids=['as-reported', 'with-errorcode'])
def test_hps_send_that_never_trained_failed_for_certain(code):
    """#82's rule needed the code, which never arrived: HP's result waited for a person, its call record kept no
    status, and engine learning never saw the failure. A call that never trained sent no page, whatever the code
    says; with bin/notify reading errorcode it now arrives too."""
    assert hylafax_engine.result_outcome(hp_result(status_code=code)) == (
        'failed', 'The fax machines could not agree on a speed; no page was sent.', None)


@pytest.mark.parametrize('changes', [
    # A retrain after RTN: the call trained at 14,400 first and sent a page the other machine may have printed.
    {'signal_rate_b64': b64('14400 bit/s'), 'data_format_b64': b64('2-D MR'), 'status_code': 'E156'},
    {'data_format_b64': b64('2-D MR')},
    # No session log: nothing shows the call never trained.
    {'session_seconds': None, 'negotiation_b64': ''},
    # Pages over SSL Fax train no modem.
    {'sslfax': True, 'signal_rate_b64': b64('SSL Fax')},
    # GotFreeFax, 11 October 03:22Z: trained at 14,400, then "No response to PPS repeated 3 times" after CTC.
    {'signal_rate_b64': b64('14400 bit/s'), 'data_format_b64': b64('1-D MH'), 'status_code': 'E147',
     'status_b64': b64('No response to PPS repeated 3 times. ; too many attempts to dial'),
     'remote_station_b64': b64('+1.888.293.3691'), 'session_seconds': 149},
], ids=['retrain-after-rtn', 'coding-agreed', 'no-session-log', 'sslfax', 'after-ctc'])
def test_a_send_that_trained_or_shows_no_session_log_stays_uncertain(changes):
    assert hylafax_engine.result_outcome(hp_result(**changes)) == UNCONFIRMED


@pytest.mark.parametrize('status, code', TRAINING)
@pytest.mark.parametrize('trained, session', [
    ({'signal_rate_b64': b64('14400 bit/s'), 'data_format_b64': b64('2-D MR')}, 60),
    ({'data_format_b64': b64('2-D MR')}, 60),
    ({}, None),
], ids=['retrain-after-rtn', 'coding-agreed', 'no-session-log'])
def test_a_training_failure_after_a_page_may_have_gone_out_stays_uncertain(status, code, trained, session):
    """The same codes end a retrain after the other machine asked for one (RTN) on a page it may have printed: that
    call trained first, so the engine reports what it agreed. Without the session log nothing shows the call never
    trained. Either way the fax waits for a person and is never sent again by itself."""
    payload = result('failed', dials=1, pages=0, status_b64=b64(status), status_code=code,
                     remote_station_b64=b64('HP TEST LINE'), session_seconds=session, **trained)
    assert hylafax_engine.result_outcome(payload) == UNCONFIRMED


@pytest.mark.parametrize('status, code', [
    ('No response to EOP repeated 3 tries {E151}', 'E151'),
    ('No response to MPS repeated 3 tries {E150}', 'E150'),
    ('Unable to transmit page (giving up after RTN) {E130}', 'E130'),
    ('Unable to transmit page (NAK at all possible signalling rates) {E132}', 'E132'),
])
def test_a_send_whose_page_may_have_gone_out_stays_uncertain_after_the_other_machine_named_itself(status, code):
    """The guard beside #73: an ending after page data could have gone out keeps waiting for a person."""
    payload = result('failed', dials=1, pages=0, status_b64=b64(status), status_code=code,
                     remote_station_b64=b64('HP TEST LINE'))
    assert hylafax_engine.result_outcome(payload) == UNCONFIRMED


def test_no_response_to_means_the_pages_were_not_confirmed():
    assert hylafax_engine.failure_sentence('No response to EOP repeated 3 tries {E151}', 0) == \
        hylafax_engine.NOT_CONFIRMED


def test_every_engine_failure_sentence_fits_the_80_characters_a_fax_error_shows():
    texts = ['', 'Busy signal detected', 'No answer from remote', 'No carrier detected', 'Remote hangup']
    sentences = {hylafax_engine.failure_sentence(text, 0) for text in texts}
    sentences |= {hylafax_engine.failure_sentence('', pages) for pages in (1, 2, 99, 9999)}
    assert len(sentences) == 9
    assert all(len(sentence) <= 80 and sentence.endswith('.') for sentence in sentences), sentences


@pytest.mark.parametrize('verdict, t38, direction, change', [
    ('no_fax_signal', 'yes', 'outbound', 'audio'), ('no_fax_signal', 'yes', 'inbound', 'audio'),
    # On audio, every way a send heard no fax machine puts the engine back on T.38 (2026-10-11: Telnyx ended
    # each audio call 3 s after its own T.38 request, which nothing answered).
    ('no_fax_signal', 'no', 'outbound', 't38'), ('cleared_at_once', 'no', 'outbound', 't38'),
    ('person_answered', 'no', 'outbound', 't38'), ('no_media_back', 'no', 'outbound', 't38'),
    ('no_fax_answer', 'no', 'outbound', 't38'),
    # A received call that left no fax may be a person or a nuisance call: nothing changes.
    ('no_fax_signal', 'no', 'inbound', None), ('no_media_back', 'no', 'inbound', None),
    # A fax machine answered, the call is not over, or nothing failed: nothing changes.
    ('remote_fax_failed', 'no', 'outbound', None), ('wrong_station', 'no', 'outbound', None),
    ('sent', 'no', 'outbound', None), ('no_fax_signal', 'unknown', 'outbound', None),
    ('remote_fax_failed', 'yes', 'outbound', None), ('cleared_at_once', 'yes', 'outbound', None),
    ('sent', 'yes', 'outbound', None), (None, None, None, None),
])
def test_an_engine_call_that_heard_no_fax_machine_moves_only_the_engine_between_t38_and_audio(
        monkeypatch, verdict, t38, direction, change):
    from app import sip_calls, sip_fax_mode
    installation, audio, back = [], [], []
    monkeypatch.setattr(sip_fax_mode, '_on_fax_event', installation.append)
    monkeypatch.setattr(hylafax_engine, 'engine_t38_failed', lambda at=None: audio.append(at))
    monkeypatch.setattr(hylafax_engine, 'engine_t38_restored', lambda: back.append(True))
    row = None if verdict is None else {'verdict': verdict, 't38': t38, 'direction': direction,
                                        'ended_at': '2026-10-05T11:19:12Z'}
    sip_calls.engine_audio_check(row)
    assert not installation
    assert (audio, back) == {'audio': (['2026-10-05T11:19:12Z'], []), 't38': ([], [True]), None: ([], [])}[change]


@pytest.mark.asyncio
async def test_an_audio_call_that_heard_no_fax_machine_puts_the_engine_back_on_t38_only_from_audio(
        tmp_path, monkeypatch):
    from app import audit, config, sip_calls

    class Connected(FakeAmi):
        def __init__(self):
            super().__init__()
            self._connected = asyncio.Event()
            self._connected.set()
    values, ami_now, events = trunk_values(tmp_path), Connected(), []
    monkeypatch.setattr(config, 'settings', values)
    monkeypatch.setattr(ami, 'ami_client', ami_now)
    monkeypatch.setattr(audit, 'audit_event', lambda event, **fields: events.append((event, fields)))
    audio_failed = {'verdict': sip_calls.NO_FAX_SIGNAL, 't38': 'no', 'direction': 'outbound',
                    'ended_at': '2026-10-11T04:29:18Z'}
    # On T.38 already: nothing to undo, nothing recorded, nothing sent to Asterisk.
    assert hylafax_engine.engine_t38_restored() is False
    sip_calls.engine_audio_check(audio_failed)
    await asyncio.gather(*hylafax_engine._pending_tasks)
    assert events == [] and ami_now.deletes == [] and hylafax_engine.engine_t38_off(values) is None
    # On audio (one T.38 call heard no fax machine): the next audio failure puts it back, once.
    hylafax_engine.note_t38_failure(values, '2026-10-11T03:56:56Z')
    sip_calls.engine_audio_check(audio_failed)
    sip_calls.engine_audio_check(audio_failed)  # the other half of the same call
    await asyncio.gather(*hylafax_engine._pending_tasks)
    assert hylafax_engine.engine_t38_off(values) is None
    assert hylafax_engine.call_settings(values, PEER, engine=True).t38 is True
    assert events == [('sslfax_engine_t38_restored', {'backend': 'sip', 'reason': 'no_fax_signal_on_audio'})]
    assert ami_now.deletes == [('faxbot-engine', 't38')] and ami_now.puts == []


@pytest.mark.asyncio
async def test_the_engine_alternates_between_t38_and_audio_and_never_stays_on_a_mode_that_fails(
        tmp_path, monkeypatch):
    """T.38 fails, audio fails, T.38 fails again: each failure moves the engine's next call to the other mode,
    and Asterisk's copy for received calls follows each move."""
    from app import audit, config, sip_calls

    class Connected(FakeAmi):
        def __init__(self):
            super().__init__()
            self._connected = asyncio.Event()
            self._connected.set()
    values, ami_now = trunk_values(tmp_path), Connected()
    monkeypatch.setattr(config, 'settings', values)
    monkeypatch.setattr(ami, 'ami_client', ami_now)
    monkeypatch.setattr(audit, 'audit_event', lambda event, **fields: None)
    modes = []
    for t38 in ('yes', 'no', 'yes', 'no'):
        sip_calls.engine_audio_check({'verdict': sip_calls.NO_FAX_SIGNAL, 't38': t38, 'direction': 'outbound',
                                      'ended_at': None})
        await asyncio.gather(*hylafax_engine._pending_tasks)
        modes.append(hylafax_engine.call_settings(values, PEER, engine=True).t38)
    assert modes == [False, True, False, True]
    assert ami_now.puts == [('faxbot-engine', 't38', 'audio')] * 2
    assert ami_now.deletes == [('faxbot-engine', 't38')] * 2


def test_an_audio_engine_call_never_asks_the_carrier_for_t38():
    """The engine's dial handler adds T.38 negotiation and the RFC 6913 T.38 preference only on a gateway call."""
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    lines = dialplan.split('[faxbot-engine-dial]\n', 1)[1].split('\n\n', 1)[0].splitlines()
    steps = [line.split(',', 2)[2] if line.startswith('exten') else line.strip()[len('same => n'):].lstrip(',')
             for line in lines]

    def run(gateway, preference):
        done, at, labels = [], 0, {}
        for index, step in enumerate(steps):
            if step.startswith('('):
                labels[step[1:step.index(')')]] = index
        while at < len(steps):
            step = steps[at].split(')', 1)[1].lstrip(',') if steps[at].startswith('(') else steps[at]
            if step.startswith('GotoIf('):
                variable, wanted, label = (step.split('${')[1].split('}')[0], step.split('!= "')[1].split('"')[0],
                                           step.split('?')[1].rstrip(')'))
                value = {'FAXBOT_GATEWAY': gateway, 'FAXBOT_PREFERENCE': preference}[variable]
                if value != wanted:
                    at = labels[label]
                    continue
            elif step.startswith('Return('):
                break
            else:
                done.append(step)
            at += 1
        return [step for step in done if 'FAXOPT' in step or 'Accept-Contact' in step]
    asks = ['Set(FAXOPT(negotiate_both)=yes)', 'Set(PJSIP_HEADER(add,Accept-Contact)=*\\;+sip.fax="t38")']
    assert run('yes', '1') == asks and run('yes', '0') == asks[:1]
    assert run('no', '1') == run('no', '0') == []


def test_the_engines_own_audio_choice_changes_only_its_calls_and_apply_clears_it(tmp_path, monkeypatch):
    values = trunk_values(tmp_path)
    assert hylafax_engine.call_settings(values, PEER, engine=True).t38 is True
    assert hylafax_engine.note_t38_failure(values, '2026-10-05T11:19:12Z') is True
    assert hylafax_engine.note_t38_failure(values) is False  # recorded once
    assert hylafax_engine.engine_t38_off(values) == {'mode': 'audio', 'reason': 'no_fax_signal',
                                                       'at': '2026-10-05T11:19:12Z'}
    assert stat.S_IMODE(os.stat(hylafax_engine.engine_t38_path(values)).st_mode) == 0o600
    # The engine's calls go audio (9600 at most); the built-in engine keeps the installation's T.38.
    engine_call = hylafax_engine.call_settings(values, PEER, engine=True)
    assert engine_call.t38 is False and engine_call.max_rate == 9600
    assert hylafax_engine.call_settings(values, PEER).t38 is True
    assert hylafax_engine.clear_engine_t38(values) is True and hylafax_engine.engine_t38_off(values) is None
    assert hylafax_engine.clear_engine_t38(values) is False


@pytest.mark.asyncio
async def test_received_calls_learn_the_engines_audio_choice_from_asterisks_database(tmp_path):
    values = trunk_values(tmp_path)

    class Connected(FakeAmi):
        def __init__(self, connected=True):
            super().__init__()
            self._connected = asyncio.Event()
            if connected:
                self._connected.set()
    ami = Connected()
    hylafax_engine.note_t38_failure(values)
    await hylafax_engine.sync_engine_t38(values, ami)
    hylafax_engine.clear_engine_t38(values)
    await hylafax_engine.sync_engine_t38(values, ami)
    assert ami.puts == [('faxbot-engine', 't38', 'audio')] and ami.deletes == [('faxbot-engine', 't38')]
    # Never over a connection that is down (that would mark it unreachable).
    offline = Connected(connected=False)
    await hylafax_engine.sync_engine_t38(values, offline)
    assert offline.puts == offline.deletes == []
    dialplan = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    assert '${DB(faxbot-engine/t38)}" = "audio"]?dial)' in dialplan


def test_compose_runs_the_engine_with_no_published_ports_and_the_override_publishes_one():
    import yaml
    services = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']
    base = services['hylafax']
    # The engine parses fax and TLS data from strangers: only its own folders, its settings read-only,
    # and never Faxbot's data folder (so never Asterisk's files there either). The one host path is /dev/null,
    # mounted as the spool's dev/null for unprivileged containers (#70; test_sip_compose.py).
    assert 'ports' not in base and sorted(base['volumes']) == [
        '/dev/null:/var/spool/hylafax/dev/null', 'hylafax-out:/faxdata/hylafax-out',
        'hylafax-settings:/faxdata/hylafax:ro', 'hylafax:/var/lib/faxbot-engine']
    assert not any(item.startswith('faxdata') for item in base['volumes'])
    assert 'hylafax-settings:/faxdata/hylafax' in services['api']['volumes']
    assert 'hylafax-out:/faxdata/hylafax-out:ro' in services['api']['volumes']
    assert 'hylafax-settings:/faxdata/hylafax' in services['asterisk']['volumes']
    override = yaml.safe_load((ROOT / 'docker-compose.sslfax.yml').read_text())['services']
    assert list(override) == ['hylafax'] and 'volumes' not in override['hylafax']
    ports = override['hylafax']['ports']
    assert len(ports) == 1 and ports[0].endswith('/tcp')
    assert '4559' not in ports[0] and '4569' not in ports[0]
    assert any(item.startswith('FAXBOT_SSLFAX_PUBLISHED_PORT=') for item in override['hylafax']['environment'])


def test_result_tags_name_one_fax_and_attempt():
    assert hylafax_engine.parse_tag(f'{JOB}.{ATTEMPT}') == (JOB, ATTEMPT)
    for bad in (None, '', JOB, f'{JOB}.{ATTEMPT}.x', f'{JOB}.XYZ', 7):
        assert hylafax_engine.parse_tag(bad) is None


def test_result_route_needs_the_engines_own_secret_and_a_known_job(isolated_installation, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.config import settings
    monkeypatch.setenv('ASTERISK_INBOUND_SECRET', SECRET)
    url = '/_internal/hylafax/result'
    with TestClient(main.app) as client:
        # Before the engine is set up there is no engine secret, and Asterisk's secret is not one.
        assert client.post(url, json=result('done'), headers={'X-Internal-Secret': SECRET}).status_code == 401
        values = trunk_values(Path(settings.fax_data_dir))
        engine_secret = hylafax_engine.engine_secrets(values)['report_secret']
        engine = {'X-Internal-Secret': engine_secret}
        assert client.post(url, json=result('done'), headers={'X-Internal-Secret': 'wrong'}).status_code == 401
        assert client.post(url, json=result('done'), headers={'X-Internal-Secret': SECRET}).status_code == 401
        assert client.post(url, json=result('done')).status_code == 401
        assert client.post(url, json={'tag': 'nope', 'why': 'done'}, headers=engine).status_code == 400
        # A well-formed tag for a fax this installation never sent.
        assert client.post(url, json=result('done'), headers=engine).status_code == 404
        # The engine's start: nothing taken earlier, nothing to settle; a bad time is refused.
        started = client.post('/_internal/hylafax/started', json={'engine_id': 'a' * 16, 'started': 1791180000},
                              headers=engine)
        assert started.status_code == 200 and started.json() == {'status': 'ok', 'uncertain': 0}
        assert client.post('/_internal/hylafax/started', json={'started': 'now'}, headers=engine).status_code == 400
        assert client.post('/_internal/hylafax/started', json={'started': 1791180000},
                           headers={'X-Internal-Secret': SECRET}).status_code == 401


def test_engine_scripts_keep_secrets_off_the_command_line_and_one_try_per_job():
    notify = (ROOT / 'hylafax' / 'bin' / 'notify').read_text()
    deliver = (ROOT / 'hylafax' / 'bin' / 'deliver').read_text()
    # notify keeps the report (no secret in it); deliver sends it with the secret from standard input.
    assert 'secret' not in notify.split('outbox=', 1)[1] and '/deliver' in notify
    assert "printf 'X-Internal-Secret: %s\\n' \"$secret\" | curl" in deliver and '-H @-' in deliver
    assert '--data-binary "@$report"' in deliver
    entry = (ROOT / 'hylafax' / 'entrypoint.sh').read_text()
    for line in ('MaxDials:\t\t1', 'MaxTries:\t\t1', 'MaxBatchJobs:\t\t1'):
        assert line in entry
    # Each line loads a tag line font (HylaFAX+ has none by default, and then prints no header line), and the image
    # build checks the font is there.
    assert "printf 'TagLineFont:\\t\\tetc/lutRS18.pcf\\n'" in entry
    # A job a restart left in the send queue is moved aside, never sent again.
    assert 'mv -f "$spool"/sendq/q* "$moved"/' in entry
    # Session logs carry no HDLC frame dumps (they would hold the SSL Fax passcode).
    assert 'session_tracing=0x08117' in entry and int('0x08117', 16) & 0x40 == 0
    dockerfile = (ROOT / 'hylafax' / 'Dockerfile').read_text()
    assert 'd696df653b1c59664fddd2c703edc77ed42766a16b2ce19c958933d76fd62287' in dockerfile
    assert 'sslfax-policy-test' in dockerfile and 'EXPOSE' not in dockerfile
