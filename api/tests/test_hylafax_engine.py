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
    assert settings['inbound_secret'] == SECRET and settings['codec'] == 'ulaw'
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
        hylafax_engine.render_engine_conf(values, engine_secret, inbound_secret='short')
    with pytest.raises(ValueError):
        hylafax_engine.render_engine_conf(values, engine_secret, inbound_secret=SECRET, listener='host:1\nlines=8')
    text = hylafax_engine.render_engine_conf(values, engine_secret, inbound_secret=SECRET,
                                             listener='fax.example.net:10443')
    assert 'sslfax_listener=fax.example.net:10443' in text
    off = hylafax_engine.render_engine_conf(values, engine_secret, inbound_secret=SECRET,
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

def test_dialplan_places_engine_calls_only_from_a_stored_plan_and_never_reports_a_fax_result():
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    start = text.index('[faxbot-engine-out]')
    section = text[start:text.index('[faxbot-inbound]')]
    assert '${DB_DELETE(faxbot-engine/${FAXBOT_TAG})}' in section
    assert 'GotoIf($["${FAXBOT_PLAN}" = ""]?refuse)' in section
    assert 'Dial(PJSIP/${FAXBOT_DIAL}@trunk-endpoint' in section
    # T.38 on the trunk: the fax gateway joins the engine's audio to T.38; off: audio end to end.
    assert 'PJSIP_ENDPOINT(trunk-endpoint,t38_udptl)' in section and 'Set(FAXOPT(gateway)=yes)' in section
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
    (tmp_path / 'hylafax' / 'engine.status').write_text(json.dumps({'state': state, 'lines': 2}))
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
            elif verb == 'STOT':
                self.reply('150 FILE: /tmp/doc7.tif (Opening new data connection).')
                connection, _ = listener.accept()
                data = b''
                while chunk := connection.recv(65536):
                    data += chunk
                connection.close()
                listener.close()
                server.uploads.append(data)
                self.reply('226 Transfer complete (FILE: /tmp/doc7.tif).')
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
    # Faxbot's header, with % kept literal (HylaFAX reads % as a format code).
    assert 'JPARM TAGLINE "Faxbot proof 100%%"' in parms and 'JPARM USETAGLINE YES' in parms
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


def test_every_engine_failure_sentence_fits_the_80_characters_a_fax_error_shows():
    texts = ['', 'Busy signal detected', 'No answer from remote', 'No carrier detected', 'Remote hangup']
    sentences = {hylafax_engine.failure_sentence(text, 0) for text in texts}
    sentences |= {hylafax_engine.failure_sentence('', pages) for pages in (1, 2, 99, 9999)}
    assert len(sentences) == 9
    assert all(len(sentence) <= 80 and sentence.endswith('.') for sentence in sentences), sentences


@pytest.mark.parametrize('row, payload, switched', [
    ({'t38': 'yes'}, {'pages': 0, 'status_b64': b64('No receiver protocol (T.30 T1 timeout)')}, True),
    ({'t38': 'no'}, {'pages': 0, 'status_b64': b64('No receiver protocol (T.30 T1 timeout)')}, False),
    ({'t38': 'yes'}, {'pages': 2, 'status_b64': b64('No receiver protocol (T.30 T1 timeout)')}, False),
    ({'t38': 'yes'}, {'pages': 0, 'status_b64': b64('Busy signal detected')}, False),
    (None, {'pages': 0, 'status_b64': b64('No receiver protocol (T.30 T1 timeout)')}, False),
])
def test_a_t38_engine_call_with_no_fax_message_back_feeds_the_audio_switch(monkeypatch, row, payload, switched):
    """The same rule as the built-in engine: T.38, no page, and the far end never sent one fax message."""
    from app import hylafax_http, sip_calls, sip_fax_mode
    seen = []
    monkeypatch.setattr(sip_fax_mode, '_on_fax_event', seen.append)
    hylafax_http._audio_switch_check(row, payload, 'failed')
    assert bool(seen) == switched
    if switched:
        event = seen[0]
        # The built-in engine's own listener reads it as a T.38 call with no data back.
        assert sip_calls.verdict(event) == 'no_t38_data_back'
        assert sip_fax_mode.t38_timeout(sip_calls._reason(event))
    hylafax_http._audio_switch_check({'t38': 'yes'}, payload, 'success')
    assert len(seen) == int(switched)


def test_compose_runs_the_engine_with_no_published_ports_and_the_override_publishes_one():
    import yaml
    base = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())['services']['hylafax']
    assert 'ports' not in base and 'faxdata:/faxdata' in base['volumes']
    override = yaml.safe_load((ROOT / 'docker-compose.sslfax.yml').read_text())['services']
    assert list(override) == ['hylafax']
    ports = override['hylafax']['ports']
    assert len(ports) == 1 and ports[0].endswith('/tcp')
    assert '4559' not in ports[0] and '4569' not in ports[0]
    assert any(item.startswith('FAXBOT_SSLFAX_PUBLISHED_PORT=') for item in override['hylafax']['environment'])


def test_result_tags_name_one_fax_and_attempt():
    assert hylafax_engine.parse_tag(f'{JOB}.{ATTEMPT}') == (JOB, ATTEMPT)
    for bad in (None, '', JOB, f'{JOB}.{ATTEMPT}.x', f'{JOB}.XYZ', 7):
        assert hylafax_engine.parse_tag(bad) is None


def test_result_route_needs_the_internal_secret_and_a_known_job(isolated_installation, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setenv('ASTERISK_INBOUND_SECRET', SECRET)
    url = '/_internal/hylafax/result'
    with TestClient(main.app) as client:
        assert client.post(url, json=result('done'), headers={'X-Internal-Secret': 'wrong'}).status_code == 401
        assert client.post(url, json=result('done')).status_code == 401
        assert client.post(url, json={'tag': 'nope', 'why': 'done'},
                           headers={'X-Internal-Secret': SECRET}).status_code == 400
        # A well-formed tag for a fax this installation never sent.
        assert client.post(url, json=result('done'), headers={'X-Internal-Secret': SECRET}).status_code == 404


def test_engine_scripts_keep_secrets_off_the_command_line_and_one_try_per_job():
    notify = (ROOT / 'hylafax' / 'bin' / 'notify').read_text()
    assert "printf 'X-Internal-Secret: %s\\n' \"$secret\" | curl" in notify and '-H @-' in notify
    entry = (ROOT / 'hylafax' / 'entrypoint.sh').read_text()
    for line in ('MaxDials:\t\t1', 'MaxTries:\t\t1', 'MaxBatchJobs:\t\t1'):
        assert line in entry
    # A job a restart left in the send queue is moved aside, never sent again.
    assert 'mv -f "$spool"/sendq/q* "$moved"/' in entry
    # Session logs carry no HDLC frame dumps (they would hold the SSL Fax passcode).
    assert 'session_tracing=0x08117' in entry and int('0x08117', 16) & 0x40 == 0
    dockerfile = (ROOT / 'hylafax' / 'Dockerfile').read_text()
    assert 'd696df653b1c59664fddd2c703edc77ed42766a16b2ce19c958933d76fd62287' in dockerfile
    assert 'sslfax-policy-test' in dockerfile and 'EXPOSE' not in dockerfile
