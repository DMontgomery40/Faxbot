"""A fax Asterisk received always reaches Faxbot, or says why not and is brought in later.

Live acceptance received a fax over the trunk whose image never reached Faxbot:
the notify script found no inbound secret and exited quietly. These tests cover
the secret (created by Faxbot), the notifier's answer for every failure, the
call record that says so, and the recovery of images that were never handed
over, on SQLite and PostgreSQL.
"""
from datetime import datetime, timedelta, timezone
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app.config import use_configuration
from app.config_values import ConfigurationValues
from app.inbound import http as inbound_http
from app.inbound import sip_handover
from app.inbound.acquisition import ImportStore
from api.tests.test_schema import database  # noqa: F401 (fixture)


ROOT = Path(__file__).resolve().parents[2]
NOTIFY = ROOT / 'asterisk' / 'bin' / 'faxbot-inbound-notify'
BOOTSTRAP = 'synthetic-handover-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
NOW = datetime(2026, 10, 4, 3, 30)


def tiff(path, *, modified=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('1', (40, 20), 1).save(path, format='TIFF')
    if modified is not None:
        stamp = modified.replace(tzinfo=timezone.utc).timestamp()
        os.utime(path, (stamp, stamp))
    return path


# The notifier ----------------------------------------------------------------------
class _Faxbot:
    """Answers the hand-over with a fixed status and keeps what it received."""

    def __init__(self, status):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                outer.requests.append({'path': self.path, 'secret': self.headers.get('X-Internal-Secret'),
                                       'body': json.loads(body)})
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_url():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return f'http://127.0.0.1:{probe.getsockname()[1]}'


def notify(data, url, *, file=None, secret='synthetic-file-secret'):
    if secret is not None:
        (data / 'asterisk').mkdir(parents=True, exist_ok=True)
        (data / 'asterisk' / 'inbound.secret').write_text(secret)
    file = file or tiff(data / 'inbound' / '1791083644.1.tiff')
    environment = {'PATH': os.environ['PATH'], 'FAXBOT_DATA_DIR': str(data), 'FAXBOT_API_URL': url,
                   'FAXBOT_NOTIFY_RETRY_SECONDS': '0'}
    return subprocess.run(['sh', str(NOTIFY), f'file={file}', 'did=+15555550199', 'caller=+13035550100',
                           'status=SUCCESS', 'pages=2', 'mode=audio', 'uniqueid=1791083644.1'],
                          env=environment, capture_output=True, text=True, timeout=60)


needs_shell = pytest.mark.skipif(shutil.which('sh') is None or shutil.which('curl') is None,
                                 reason='The notifier needs sh and curl.')


@needs_shell
@pytest.mark.parametrize(('status', 'word'), [
    (200, 'ok'), (401, 'refused'), (404, 'not_receiving'), (400, 'unreadable'), (500, 'failed')])
def test_notifier_prints_ok_or_why_the_hand_over_failed(tmp_path, status, word):
    faxbot = _Faxbot(status)
    try:
        result = notify(tmp_path / 'faxdata', faxbot.url)
    finally:
        faxbot.close()
    assert result.stdout == word + '\n'
    assert result.returncode == (0 if word == 'ok' else 1)
    assert (result.stderr == '') == (word == 'ok')
    assert 'synthetic-file-secret' not in result.stdout + result.stderr
    [request] = faxbot.requests
    assert request['path'] == '/_internal/asterisk/inbound' and request['secret'] == 'synthetic-file-secret'
    assert request['body']['uniqueid'] == '1791083644.1' and request['body']['call']['t38'] is False


@needs_shell
def test_notifier_never_drops_a_fax_quietly(tmp_path):
    data = tmp_path / 'faxdata'
    missing = notify(data, _closed_url(), secret=None)
    assert (missing.stdout, missing.returncode) == ('no_secret\n', 1)
    assert 'was not handed to Faxbot (no_secret)' in missing.stderr
    unreachable = notify(data, _closed_url())
    assert (unreachable.stdout, unreachable.returncode) == ('unreachable\n', 1)
    outside = tiff(tmp_path / 'elsewhere' / '1.1.tiff')
    assert notify(data, _closed_url(), file=outside).stdout == 'unreadable\n'


def test_dialplan_logs_and_reports_a_failed_hand_over():
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    done = text.split('[faxbot-inbound-done]', 1)[1].split('\n[', 1)[0]
    assert 'Set(FAXBOT_HANDOVER=${SHELL(/usr/local/bin/faxbot-inbound-notify ' in done
    assert 'GotoIf($["${FAXBOT_HANDOVER}" = "ok"]?done)' in done
    assert 'Log(ERROR,Received fax ' in done
    assert 'UserEvent(FaxInboundCall,' in done and ',Handover:${FAXBOT_HANDOVER})' in done
    assert 'require => func_shell.so' in (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'modules.conf').read_text()


# Recovery on both databases ----------------------------------------------------------
def _values(data, **extra):
    return ConfigurationValues.from_environment({'FAX_DATA_DIR': str(data), 'INBOUND_ENABLED': 'true',
                                                 'FAX_BACKEND': 'sip', **extra})


def test_images_that_were_never_handed_over_are_brought_in_once_on_both_databases(database, tmp_path):
    from api.tests.test_inbound_access import InboundWorld
    from app.sip_calls import SipCallRecords
    world = InboundWorld(database)
    store = ImportStore(world.inbound, clock=lambda: NOW)
    data = tmp_path / 'faxdata'
    inbound = data / 'inbound'
    old = tiff(inbound / '1791083644.1.tiff', modified=NOW - timedelta(hours=3))
    tiff(inbound / '1791083700.2.tiff', modified=NOW - timedelta(seconds=30))       # may still be arriving
    tiff(inbound / '1791083800.3.tiff', modified=NOW - timedelta(seconds=30))       # its call ended
    tiff(inbound / 'in.tiff', modified=NOW - timedelta(hours=3))                    # not an Asterisk image
    (inbound / '1791083900.4.tiff').symlink_to(old)
    records = SipCallRecords(database)
    records.record_inbound_event({'UniqueID': '1791083800.3', 'DID': '+15555550199', 'Caller': '+13035550100',
                                  'Status': 'SUCCESS', 'Pages': '1', 'Answered': '1791083800', 'Started': '1791083800',
                                  'Ended': '1791083830', 'Handover': 'no_secret'}, now=NOW)
    values = _values(data)
    with use_configuration(values):
        first = sip_handover.recover(store, database, values, now=NOW)
        again = sip_handover.recover(store, database, values, now=NOW)
        late = store.begin(source='sip', account='sip:asterisk', operation_id='1791083644.1', backend='sip')
    assert (first.found, len(first.imported), first.waiting) == (2, 2, 0)
    assert (again.found, again.imported) == (0, ())
    assert late.created is False and late.inbound_fax_id in first.imported
    imports = {row['operation_id']: row for row in _rows(database, 'inbound_imports')}
    assert set(imports) == {'1791083644.1', '1791083800.3'}
    for row in imports.values():
        assert row['state'] == 'received' and row['source'] == 'sip' and row['account'] == 'sip:asterisk'
        assert json.loads(row['report'])['recovered'] is True
        assert json.loads(row['report'])['source_time'] == 'image file modified time'
    assert imports['1791083644.1']['source_received_at'] == NOW - timedelta(hours=3)
    faxes = {row['id']: row for row in _rows(database, 'inbound_faxes')}
    ended = faxes[imports['1791083800.3']['inbound_fax_id']]
    assert (ended['status'], ended['to_number'], ended['from_number']) == ('received', '+15555550199', '+13035550100')
    assert faxes[imports['1791083644.1']['inbound_fax_id']]['to_number'] is None
    [call] = records.page(direction='inbound')['items']
    assert (call['job_id'], call['verdict']) == (imports['1791083800.3']['inbound_fax_id'], 'received')
    with use_configuration(values):
        later = sip_handover.recover(store, database, values, now=NOW + timedelta(minutes=10))
    assert len(later.imported) == 1  # the image that may have been arriving, once it is quiet


def test_recovery_waits_while_receiving_over_the_trunk_is_off(database, tmp_path):
    from api.tests.test_inbound_access import InboundWorld
    world = InboundWorld(database)
    store = ImportStore(world.inbound, clock=lambda: NOW)
    data = tmp_path / 'faxdata'
    tiff(data / 'inbound' / '1791083644.1.tiff', modified=NOW - timedelta(hours=3))
    for values in (_values(data, INBOUND_ENABLED='false'), _values(data, FAX_BACKEND='phaxio')):
        with use_configuration(values):
            assert sip_handover.recover(store, database, values, now=NOW).found == 0
    assert _rows(database, 'inbound_imports') == []


def _rows(engine, name):
    with engine.connect() as connection:
        table = sa.Table(name, sa.MetaData(), autoload_with=connection)
        return [dict(row) for row in connection.execute(sa.select(table)).mappings()]


# Over HTTP: Faxbot creates the secret; recovery from the console and CLI -------------
def _client(monkeypatch, **extra):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'MAX_REQUESTS_PER_MINUTE': '0', 'FAXBOT_CONSOLE_ORIGINS': 'https://testserver',
                        **extra}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(inbound_http, 'AUTOMATIC', False)
    monkeypatch.setattr(main, 'AMI_STARTUP_WAIT_SECONDS', 0.1)
    return TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


def _settings(client):
    return client.get('/admin/settings', headers=ADMIN).json()


def test_apply_creates_the_inbound_secret_and_asterisk_hands_faxes_over_with_it(isolated_installation, monkeypatch):
    from app import sip_http, stun
    monkeypatch.setattr(stun, 'probe', lambda servers, **_: None)
    monkeypatch.setattr(sip_http, '_probes', {})
    data = Path(isolated_installation['FAX_DATA_DIR'])
    secret_file = data / 'asterisk' / 'inbound.secret'
    with _client(monkeypatch) as client:
        assert not secret_file.exists() and _settings(client)['inbound']['sip']['configured'] is False
        current = _settings(client)
        saved = client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': current['_meta']['desired_revision_id'], 'inbound_enabled': True,
            'sip_trunk_preset': 'telnyx', 'sip_trunk_username': 'faxbotuser',
            'sip_trunk_password': 'synthetic-Trunk-Pass!42'})
        assert saved.status_code == 200, saved.text
        applied = client.post('/admin/sip/apply', headers=ADMIN)
        assert applied.status_code == 200, applied.text
        secret = secret_file.read_text()
        assert len(secret) >= 32 and oct(secret_file.stat().st_mode & 0o777) == '0o600'
        view = client.get('/admin/settings', headers=ADMIN)
        assert view.json()['inbound']['sip']['configured'] is True and secret not in view.text
        assert client.post('/admin/sip/apply', headers=ADMIN).status_code == 200
        assert secret_file.read_text() == secret  # created once, then kept
        image = tiff(data / 'inbound' / '1791083644.1.tiff')
        refused = client.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'wrong'},
                              json={'tiff_path': str(image), 'uniqueid': '1791083644.1'})
        assert refused.status_code == 401
        accepted = client.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': secret},
                               json={'tiff_path': str(image), 'uniqueid': '1791083644.1', 'faxpages': 1})
        assert accepted.status_code == 200, accepted.text
        assert client.get(f"/inbound/{accepted.json()['id']}", headers=ADMIN).json()['status'] == 'received'


def test_a_set_secret_wins_and_startup_writes_it_for_asterisk(isolated_installation, monkeypatch):
    data = Path(isolated_installation['FAX_DATA_DIR'])
    with _client(monkeypatch, FAX_BACKEND='sip', INBOUND_ENABLED='true', ASTERISK_INBOUND_SECRET='synthetic-env-secret'):
        assert (data / 'asterisk' / 'inbound.secret').read_text() == 'synthetic-env-secret'


def test_startup_creates_the_secret_when_the_sip_trunk_receives(isolated_installation, monkeypatch):
    data = Path(isolated_installation['FAX_DATA_DIR'])
    with _client(monkeypatch, FAX_BACKEND='sip', INBOUND_ENABLED='true') as client:
        secret = (data / 'asterisk' / 'inbound.secret').read_text()
        assert len(secret) >= 32 and _settings(client)['inbound']['sip']['configured'] is True


def test_recover_brings_in_an_orphan_from_the_console_and_the_cli(isolated_installation, monkeypatch):
    data = Path(isolated_installation['FAX_DATA_DIR'])
    with _client(monkeypatch, FAX_BACKEND='sip', INBOUND_ENABLED='true') as client:
        tiff(data / 'inbound' / '1791083644.1.tiff', modified=datetime.utcnow() - timedelta(hours=3))
        first = client.post('/admin/inbound/recover', headers=ADMIN, json={})
        assert first.status_code == 200, first.text
        assert first.json() == {'found': 1, 'imported': 1, 'waiting': 0, 'message': 'Brought in 1 received fax.'}
        [fax] = client.get('/inbound', headers=ADMIN).json()
        assert fax['status'] == 'received' and fax['backend'] == 'sip'
        again = client.post('/admin/inbound/recover', headers=ADMIN, json={})
        assert again.json()['message'] == 'No received faxes are waiting to be brought in.'
        reader = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'reader', 'scopes': ['inbound:list']})
        assert client.post('/admin/inbound/recover', json={},
                           headers={'X-API-Key': reader.json()['token']}).status_code == 403
        current = _settings(client)
        assert client.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': current['_meta']['desired_revision_id'], 'inbound_enabled': False}).status_code == 200
        refused = client.post('/admin/inbound/recover', headers=ADMIN, json={})
        assert (refused.status_code, refused.json()['detail']) == (409, 'Turn on receiving over the SIP trunk first.')


def test_cli_recover_prints_the_plain_result(monkeypatch):
    from typer.testing import CliRunner
    from app.cli import state
    from app.cli.main import app as cli

    class FakeApi:
        def post(self, path, json=None):
            assert path == '/admin/inbound/recover'
            return {'found': 1, 'imported': 1, 'waiting': 0, 'message': 'Brought in 1 received fax.'}
    monkeypatch.setattr(state, 'api', lambda: FakeApi())
    result = CliRunner().invoke(cli, ['inbound', 'recover'])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == 'Brought in 1 received fax.'


@needs_shell
def test_notifier_passes_the_sip_call_id_in_the_call_object(tmp_path):
    """The SIP Call-ID (base64) rides in the call object so the carrier's charge matches the call exactly."""
    data = tmp_path / 'faxdata'
    (data / 'asterisk').mkdir(parents=True)
    (data / 'asterisk' / 'inbound.secret').write_text('synthetic-file-secret')
    file = tiff(data / 'inbound' / '1791083644.7.tiff')
    faxbot = _Faxbot(200)
    environment = {'PATH': os.environ['PATH'], 'FAXBOT_DATA_DIR': str(data), 'FAXBOT_API_URL': faxbot.url,
                   'FAXBOT_NOTIFY_RETRY_SECONDS': '0'}
    try:
        for extra in (['callid64=M2YwYzVhOGUtMTExMQ=="; touch pwned'], []):
            result = subprocess.run(['sh', str(NOTIFY), f'file={file}', 'did=+15555550199', 'uniqueid=1791083644.7',
                                     *extra], env=environment, capture_output=True, text=True, timeout=60)
            assert result.stdout == 'ok\n'
    finally:
        faxbot.close()
    first, second = (request['body']['call'] for request in faxbot.requests)
    assert first['sip_call_id_b64'] == 'M2YwYzVhOGUtMTExMQ==touchpwned'  # only base64 characters survive
    assert second['sip_call_id_b64'] is None
    assert set(first) - {'sip_call_id_b64'} == {'did', 'caller', 'started_at', 'answered_at', 'ended_at', 'pages',
                                                't38', 'remote_station_id_b64'}

