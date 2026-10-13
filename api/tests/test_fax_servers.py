"""Fax servers that send through Faxbot (RightFax with an SR140 and others; migration 0086, store and forward).

Safety first: only an enrolled server, from its one private address, with digest authentication; every dialled number
through the emergency and short-number refusal, the server's countries, Faxbot's dialing guard and the sending rules;
per-hour and per-day caps; one fax per SIP Call-ID; only whole faxes sent on; and the real outcome, with one notice to
the server's contact when a fax is not delivered. Every name, address and number is synthetic; nothing here has run
against a real RightFax server or SR140 (tools/sr140-emulator is the stand-in for the live test).
"""
import base64
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import sqlalchemy as sa

from api.app import fax_servers, schema, schema_fax_servers, sip_trunk
from api.tests.test_digital_schema import _downgrade, _tables
from api.tests.test_inbound_handover import _Faxbot
from api.tests.test_schema import database  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'asterisk' / 'bin' / 'faxbot-fax-server'
NOW = datetime(2026, 10, 13, 15, 0)
OUTSIDE = '+13035550199'
OWN = '+13035550160'


def _values(**extra):
    from api.app.config_values import ConfigurationValues
    environment = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                   'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_DIDS': OWN, 'INBOUND_ENABLED': 'true',
                   'FAX_DEFAULT_COUNTRY': 'US', **extra}
    return ConfigurationValues.from_environment(environment)


def _server(database, **extra):  # noqa: F811
    fields = {'name': 'RightFax', 'address': '192.168.68.50', 'countries': ['US'], 'per_hour': 3, 'per_day': 5,
              **extra}
    return fax_servers.add(database, **fields)


# Enrolment ---------------------------------------------------------------------------------------------------------

def test_0086_is_the_head_and_its_downgrade_keeps_fax_servers(database):  # noqa: F811
    schema.upgrade_schema(database)
    assert schema_fax_servers.REVISION == '0086_fax_servers' == schema.HEAD
    assert schema.COPIER_EXTENSIONS == '0085_copier_extensions'
    assert schema_fax_servers.TABLES <= schema.STRICT_TABLES and schema_fax_servers.TABLES <= _tables(database)
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    _downgrade(database, '0085_copier_extensions')
    assert not schema_fax_servers.TABLES & _tables(database)
    schema.upgrade_schema(database)
    _server(database)
    with pytest.raises(schema.SchemaUpgradeError, match='cannot be'):
        _downgrade(database, '0085_copier_extensions')


def test_a_fax_server_gets_its_password_once_and_only_from_a_private_address(database):  # noqa: F811
    schema.upgrade_schema(database)
    server, password = _server(database, contact_email='fax-admin@example.com', dial_prefix='9')
    assert server.username.startswith('faxserver-') and len(password) >= 20 and server.endpoint.startswith('faxserver-')
    assert server.md5_cred == hashlib.md5(f'{server.username}:faxbot:{password}'.encode()).hexdigest()
    with database.connect() as connection:
        stored = json.dumps([dict(row) for row in connection.execute(
            sa.text('SELECT * FROM fax_servers')).mappings()], default=str)
    assert password not in stored
    for address, message in (('8.8.8.8', 'private network'), ('fe80::1', 'private network'),
                             ('127.0.0.1', 'private network'), ('server.example.com', 'address on your network'),
                             ('192.168.68.50', 'already a fax server')):
        with pytest.raises(fax_servers.FaxServerError, match=message):
            _server(database, address=address)
    with pytest.raises(fax_servers.FaxServerError, match='phone system or a partner'):
        _server(database, address='10.1.1.1', taken={'10.1.1.1'})
    for extra, message in (({'countries': ['XX']}, 'country code'), ({'per_hour': 0}, 'from 1 to'),
                           ({'contact_email': 'nobody'}, 'email address'),
                           ({'contact_webhook': 'http://203.0.113.9/hook'}, 'https://'),
                           ({'dial_prefix': '9a'}, 'outside-line prefix')):
        with pytest.raises(fax_servers.FaxServerError, match=message):
            _server(database, address='10.2.2.2', **extra)
    changed = fax_servers.change(database, server.id, per_hour=10, contact_webhook='https://example.com/hook')
    assert (changed.per_hour, changed.per_day, changed.contact_webhook, changed.md5_cred) == (
        10, 5, 'https://example.com/hook', server.md5_cred)
    fax_servers.remove(database, server.id)
    assert fax_servers.servers(database) == []


def test_pjsip_finds_a_fax_server_only_by_its_address_and_never_holds_its_password():
    server = fax_servers.FaxServer('a' * 32, 'RightFax', '192.168.68.50', 'faxserver-aaaaaaaa', 'b' * 32, None,
                                   ('US',), 60, 500, None, None, None, 'active')
    text = fax_servers.render([server])
    endpoint = text.split(f"[faxserver-{'a' * 32}]\n", 1)[1].split('\n\n', 1)[0]
    assert 'identify_by=ip' in endpoint and 'context=faxbot-fax-server' in endpoint
    assert 'deny=0.0.0.0/0.0.0.0' in endpoint and 'permit=192.168.68.50/255.255.255.255' in endpoint
    assert f"auth=faxserver-{'a' * 32}-auth" in endpoint and 'transport=transport-fax-server' in endpoint
    assert 'set_var=FAXBOT_FAX_SERVER_ADDRESS=192.168.68.50' in endpoint and 'aors=' not in endpoint
    assert 'auth_type=md5' in text and 'md5_cred=' + 'b' * 32 in text and 'password=' not in text
    assert 'match=192.168.68.50' in text and 'type=registration' not in text and 'anonymous' not in text
    assert 'bind=0.0.0.0:5080' in text and 'external_media_address=@FAXBOT_FAX_SERVER_ADDRESS@' in text
    assert 'local_net' not in text and 'transport-udp' not in text  # never the carrier's connection
    assert fax_servers.render([]) == ''
    rendered = sip_trunk.rendered_configuration(_values(), peers=[], copiers=[], servers=[server])
    assert rendered.index('[transport-fax-server]') > rendered.index('[trunk-endpoint]')
    assert rendered.count('[transport-fax-server]') == 1


def test_the_dialplan_asks_before_answering_and_never_dials_out():
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions-fax-server.conf').read_text()
    main = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    assert main.count('#include extensions-fax-server.conf') == 1
    assert 'faxbot-fax-server' not in main  # nothing on the carrier's or anyone else's path reaches it
    context = text.split('[faxbot-fax-server]', 1)[1].split('\n[', 1)[0]
    admit = context.index('faxbot-fax-server admit')
    assert admit < context.index('Answer()') < context.index('ReceiveFAX(${FAXBOT_FILE},f)')
    assert context.index('"${FAXBOT_REMOTE}" != "${FAXBOT_FAX_SERVER_ADDRESS}"') < admit
    assert 'Set(FAXBOT_FILE=/faxdata/fax-server/${FAXBOT_TOKEN}.tiff)' in context  # never the inbound folder
    assert ':503)})' in context and 'PJSIPHangup(${FAXBOT_CODE})' in context
    assert 'Goto(faxbot-inbound-receive,s,1)' in context  # your own numbers stay received faxes
    assert 'Dial(' not in text
    assert 'REGEX("^[a-f0-9]{32}$" ${FAXBOT_FAX_SERVER})' in context
    dockerfile = (ROOT / 'asterisk' / 'Dockerfile').read_text()
    assert 'bin/faxbot-fax-server' in dockerfile and '/usr/local/bin/faxbot-fax-server' in dockerfile
    start = (ROOT / 'asterisk' / 'start.sh').read_text()
    assert '@FAXBOT_FAX_SERVER_ADDRESS@/d' in start and 'chown asterisk:asterisk "$data_dir/fax-server"' in start
    overlay = (ROOT / 'docker-compose.fax-server.yml').read_text()
    assert ':5080/udp' in overlay and '${FAXBOT_LAN_ADDRESS:?' in overlay and '0.0.0.0' not in overlay


# The number dialled ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(('dialled', 'expected'), [
    ('3035550199', OUTSIDE), ('13035550199', OUTSIDE), ('+13035550199', OUTSIDE), ('93035550199', OUTSIDE),
    ('011442079460000', '+442079460000'), ('+442079460000', '+442079460000')])
def test_the_number_is_read_after_the_outside_line_prefix(dialled, expected):
    server = fax_servers.FaxServer('a' * 32, 'RightFax', '192.168.68.50', 'u', 'm', None, ('US',), 1, 1, '9', None,
                                   None, 'active')
    assert fax_servers.read_number(server, dialled, 'US') == expected


@pytest.mark.parametrize(('dialled', 'home', 'code'), [
    ('911', 'US', 403), ('9911', 'US', 403), ('112', 'GB', 403), ('999', 'GB', 403), ('000', 'AU', 403),
    ('411', 'US', 403), ('*67', 'US', 484), ('', 'US', 484), ('1234567890123456789', 'US', 484)])
def test_emergency_short_and_unreadable_numbers_are_refused(dialled, home, code):
    server = fax_servers.FaxServer('a' * 32, 'RightFax', '192.168.68.50', 'u', 'm', None, (home,), 1, 1, '9', None,
                                   None, 'active')
    with pytest.raises(fax_servers._Refuse) as refused:
        fax_servers.read_number(server, dialled, home)
    assert refused.value.code == code


# Before answering ------------------------------------------------------------------------------------------------

def _admit(database, server, dialled='3035550199', *, call='call-1@192.168.68.50', address=None, rules=None,  # noqa: F811
           now=NOW, values=None):
    return fax_servers.admit(database, values or _values(), server.id, address=address or server.address,
                             dialled=dialled, call_id=call, uniqueid='1791100000.' + call[-1], rules=rules, now=now)


def test_unknown_servers_and_addresses_never_relay(database):  # noqa: F811
    schema.upgrade_schema(database)
    server, _ = _server(database)
    assert fax_servers.admit(database, _values(), 'f' * 32, address='192.168.68.50', dialled='3035550199',
                             call_id='x', now=NOW).answer == 'refuse:403'
    assert _admit(database, server, address='192.168.68.51').answer == 'refuse:403'
    fax_servers.remove(database, server.id)
    assert _admit(database, server).answer == 'refuse:403'
    with database.connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM fax_server_calls')).scalar() == 0


def test_every_number_passes_the_countries_the_guard_and_the_rules(database):  # noqa: F811
    schema.upgrade_schema(database)
    server, _ = _server(database, per_hour=50, per_day=50)
    assert _admit(database, server, OWN.lstrip('+1'), call='c1').answer == f'receive:{OWN}'
    assert _admit(database, server, '011442079460000', call='c2').answer == 'refuse:403'  # GB is not its country
    widened = fax_servers.change(database, server.id, countries=['US', 'GB'])
    # GB is allowed for this server, but Faxbot's guard has never sent there: still refused.
    assert _admit(database, widened, '011442079460000', call='c3').answer == 'refuse:403'
    assert _admit(database, widened, '19005550100', call='c4').answer == 'refuse:403'  # premium-rate, fenced
    assert _admit(database, widened, '911', call='c5').answer == 'refuse:403'
    assert _admit(database, widened, call='c6', rules=lambda number: 'your rules hold it for approval').answer == \
        'refuse:403'
    seen = []
    assert _admit(database, widened, call='c7', rules=lambda number: seen.append(number)).answer == f'send:{OUTSIDE}'
    assert seen == [OUTSIDE]
    views = fax_servers.calls(database)
    turned = [view for view in views if view['state'] == 'turned_away']
    assert len(turned) == 5 and all(view['sentence'].startswith('Turned away before answering') for view in turned)
    assert any('may not send to GB' in view['sentence'] for view in turned)
    assert sorted(view['state'] for view in views if view['to'] == OUTSIDE) == ['admitted', 'turned_away']


def test_caps_per_hour_and_per_day_and_one_fax_per_call_id(database):  # noqa: F811
    schema.upgrade_schema(database)
    server, _ = _server(database, per_hour=2, per_day=3)
    assert [_admit(database, server, call=f'h{i}', now=NOW + timedelta(minutes=i)).answer for i in range(3)] == [
        f'send:{OUTSIDE}', f'send:{OUTSIDE}', 'refuse:486']
    # A refused call never counts toward the caps; an hour later the hourly cap has room, the daily one fills.
    assert _admit(database, server, call='h3', now=NOW + timedelta(minutes=61)).answer == f'send:{OUTSIDE}'
    assert _admit(database, server, call='h4', now=NOW + timedelta(minutes=130)).answer == 'refuse:486'
    # The same Call-ID again is never a second fax.
    assert _admit(database, server, call='h0', now=NOW + timedelta(minutes=131)).answer == 'refuse:482'
    # A new day (UTC here) starts the daily count again.
    assert _admit(database, server, call='d1', now=NOW + timedelta(days=1)).answer == f'send:{OUTSIDE}'


# The hand-over, the outcome and the notice, through the server --------------------------------------------------

SECRET = {'X-Internal-Secret': 'sekret'}


@pytest.fixture
def sip_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    for name, value in {'FAX_BACKEND': 'sip', 'FAX_OUTBOUND_BACKEND': 'sip', 'FAX_INBOUND_BACKEND': 'sip',
                        'INBOUND_ENABLED': 'true', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                        'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_CALLER_ID': OWN,
                        'SIP_TRUNK_DIDS': OWN, 'AMI_PASSWORD': 'synthetic-ami-pass',
                        'ASTERISK_INBOUND_SECRET': 'sekret', 'FAX_HEADER': 'Example Clinic'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def _b():
    from api.tests.test_access_management_http import B
    return B


def _engine():
    from app.main import app
    return app.state.configuration_runtime.manager.store.engine


def _tiff(name, pages=2):
    from PIL import Image
    folder = Path(os.environ['FAX_DATA_DIR']) / 'fax-server'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    frames = [Image.new('1', (1728, 200), 1) for _ in range(pages)]
    frames[0].save(path, format='TIFF', save_all=True, append_images=frames[1:], compression='group4',
                   dpi=(204, 196))
    return path


def _enrol(client, **extra):
    created = client.post('/admin/sip/fax-servers', headers=_b(), json={
        'name': 'RightFax', 'address': '192.168.68.50', 'countries': ['US'],
        'contact_email': 'fax-admin@example.com', **extra})
    assert created.status_code == 201, created.text
    return created.json()


def _admit_http(client, server, call, dialled='3035550199', address='192.168.68.50', headers=SECRET):
    return client.post('/_internal/asterisk/fax-server/admit', headers=headers, json={
        'server': server['id'], 'address': address, 'dialed': dialled, 'caller': '100',
        'callid64': base64.b64encode(call.encode()).decode(), 'uniqueid': '1791100000.' + str(len(call))})


def _handover(client, server, call, tiff=None, status='SUCCESS'):
    return client.post('/_internal/asterisk/fax-server/handover', headers=SECRET, json={
        'server': server['id'], 'tiff_path': str(tiff) if tiff else None, 'faxstatus': status, 'faxpages': 2,
        'callid64': base64.b64encode(call.encode()).decode(), 'uniqueid': '1791100000.' + str(len(call))})


def test_a_fax_from_rightfax_is_sent_on_once_and_its_real_outcome_is_kept(sip_client):
    server = _enrol(sip_client)
    assert server['password'] and server['username'].startswith('faxserver-') and 'password' not in json.dumps(
        sip_client.get('/admin/sip/fax-servers', headers=_b()).json())
    assert server['password_text'].startswith('Enter this user name and password on the fax server now')
    assert _admit_http(sip_client, server, 'a1', headers={'X-Internal-Secret': 'wrong'}).status_code == 401
    admitted = _admit_http(sip_client, server, 'a1')
    assert admitted.status_code == 200 and admitted.text == f'send:{OUTSIDE}'  # the real rules and guard said yes
    tiff = _tiff('1791100000.2.tiff')
    handed = _handover(sip_client, server, 'a1', tiff)
    assert handed.status_code == 200, handed.text
    job = handed.json()['queued']
    assert handed.json()['status'] == 'accepted' and not tiff.exists()
    with _engine().connect() as connection:
        row = connection.execute(sa.text('SELECT to_number, pages FROM fax_jobs WHERE id = :id'), {'id': job}).one()
        received = connection.execute(sa.text('SELECT count(*) FROM inbound_faxes')).scalar()
    assert tuple(row) == (OUTSIDE, 2) and received == 0
    # The script repeats a hand-over when Faxbot was slow: the same call is the same fax.
    again = _handover(sip_client, server, 'a1', _tiff('again.tiff'))
    assert again.json()['queued'] == job
    with _engine().connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM fax_jobs')).scalar() == 1
    # It is in Sent under the server's own sender, and the server's calls list says what became of it.
    listed = sip_client.get('/admin/sip/fax-servers/calls', headers=_b()).json()['calls']
    assert [(item['state'], item['fax_id'], item['to']) for item in listed] == [('accepted', job, OUTSIDE)]
    # A call that ended before every page arrived sends nothing; the fax server saw the failure itself.
    assert _admit_http(sip_client, server, 'b22').text == f'send:{OUTSIDE}'
    partial = _handover(sip_client, server, 'b22', _tiff('1791100000.3.tiff'), status='FAILED')
    assert partial.json()['status'] == 'not_received' and partial.json()['queued'] is None
    with _engine().connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM fax_jobs')).scalar() == 1
    # A number the guard won't dial is refused unanswered, and your own number is a received fax.
    assert _admit_http(sip_client, server, 'c333', dialled='19005550100').text == 'refuse:403'
    assert _admit_http(sip_client, server, 'd4444', dialled=OWN).text == f'receive:{OWN}'
    assert _admit_http(sip_client, server, 'e55555', address='192.168.68.99').text == 'refuse:403'


def test_a_fax_that_is_not_delivered_tells_the_server_contact_once(sip_client, monkeypatch):
    server = _enrol(sip_client, contact_webhook='https://example.com/fax-hook')
    assert _admit_http(sip_client, server, 'n1').text == f'send:{OUTSIDE}'
    job = _handover(sip_client, server, 'n1', _tiff('1791100000.2.tiff')).json()['queued']
    engine = _engine()
    from app.main import app
    values = app.state.configuration_runtime.manager.store.read().active.values
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'failed' WHERE id = :id"), {'id': job})
        connection.execute(sa.text("UPDATE fax_jobs SET error = 'The line was busy' WHERE id = :id"), {'id': job})
    sent = []

    def email(values, address, body):
        sent.append(('email', address, body))
        return 'failed', 'The email server refused the message.'

    def webhook(url, body):
        sent.append(('webhook', url, body))
        return 'uncertain', 'Your notice address stopped answering after the notice was sent; it is not sent again.'
    changed = fax_servers.settle(engine, values, email=email, webhook=webhook)
    assert sorted(kind for _, kind in changed) == ['failed', 'notice_uncertain']
    body = sent[0][2]
    assert body['state'] == 'failed' and body['to'] == OUTSIDE and 'was not delivered' in body['sentence']
    assert 'The line was busy' in body['sentence'] and 'pdf' not in json.dumps(body).lower()
    assert [kind for kind, _, _ in sent] == ['email', 'webhook']
    # An uncertain notice is never sent again; the fax itself is never sent again either.
    assert fax_servers.settle(engine, values, email=email, webhook=webhook) == []
    assert len(sent) == 2
    with engine.connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM fax_jobs')).scalar() == 1
    [view] = sip_client.get('/admin/sip/fax-servers/calls', headers=_b()).json()['calls']
    assert (view['state'], view['notice']) == ('failed', 'uncertain')


def test_an_uncertain_outcome_and_a_lost_hand_over_are_told_and_never_resent(sip_client):
    server = _enrol(sip_client)
    engine = _engine()
    from app.main import app
    values = app.state.configuration_runtime.manager.store.read().active.values
    assert _admit_http(sip_client, server, 'u1').text == f'send:{OUTSIDE}'
    job = _handover(sip_client, server, 'u1', _tiff('1791100000.2.tiff')).json()['queued']
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state = 'reconciliation_required' WHERE id = :id"),
                           {'id': job})
    # A call answered whose hand-over never came: its image is there, but nothing is sent late.
    assert _admit_http(sip_client, server, 'l22').text == f'send:{OUTSIDE}'
    lost = _tiff('1791100000.3.tiff')
    later = datetime.utcnow() + timedelta(minutes=30)
    tries = []

    def email(values, address, body):
        tries.append(body['state'])
        return 'failed', 'Faxbot could not reach the email server.'
    changed = dict(fax_servers.settle(engine, values, now=later, email=email))
    assert sorted(changed.values()) == ['notice_failed', 'notice_failed']
    assert not lost.exists()
    with engine.connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM fax_jobs')).scalar() == 1
    views = {view['to'] + view['state']: view for view in fax_servers.calls(engine)}
    assert f'{OUTSIDE}uncertain' in views and f'{OUTSIDE}refused' in views
    assert 'never finished' in views[f'{OUTSIDE}refused']['sentence']
    # A failed notice is tried again after ten minutes, up to three tries in all, then left for you to see.
    for minutes in (11, 22, 33, 44):
        fax_servers.settle(engine, values, now=later + timedelta(minutes=minutes), email=email)
    assert len(tries) == 6  # two calls: three tries each, never a fourth


def test_the_server_screens_and_the_routes_need_the_right_permissions(sip_client):
    assert sip_client.get('/admin/sip/fax-servers').status_code in (401, 403)
    bad = sip_client.post('/admin/sip/fax-servers', headers=_b(), json={'name': 'RightFax', 'address': '8.8.8.8'})
    assert bad.status_code == 400 and 'private network' in bad.json()['detail']
    server = _enrol(sip_client)
    assert sip_client.put(f"/admin/sip/fax-servers/{server['id']}", headers=_b(),
                          json={'per_day': 0}).status_code == 422
    changed = sip_client.put(f"/admin/sip/fax-servers/{server['id']}", headers=_b(), json={'per_day': 20})
    assert changed.status_code == 200 and changed.json()['per_day'] == 20
    removed = sip_client.post(f"/admin/sip/fax-servers/{server['id']}/remove", headers=_b())
    assert removed.status_code == 200 and 'Press Apply' in removed.json()['detail']
    assert _admit_http(sip_client, server, 'r1').text == 'refuse:403'


# The script Asterisk runs ------------------------------------------------------------------------------------------

needs_shell = pytest.mark.skipif(shutil.which('sh') is None or shutil.which('curl') is None,
                                 reason='The script needs sh and curl.')


def _run(data, url, *words, secret='synthetic-file-secret'):
    if secret is not None:
        (data / 'asterisk').mkdir(parents=True, exist_ok=True)
        (data / 'asterisk' / 'inbound.secret').write_text(secret)
    environment = {'PATH': os.environ['PATH'], 'FAXBOT_DATA_DIR': str(data), 'FAXBOT_API_URL': url,
                   'FAXBOT_NOTIFY_RETRY_SECONDS': '0'}
    return subprocess.run(['sh', str(SCRIPT), *words], env=environment, capture_output=True, text=True, timeout=60)


class _Answer(_Faxbot):
    """A stand-in Faxbot that answers every admit with one fixed body."""

    def __init__(self, status, text):
        import http.server
        import threading
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                outer.requests.append({'path': self.path, 'secret': self.headers.get('X-Internal-Secret'),
                                       'body': json.loads(body)})
                self.send_response(status)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(text.encode())

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@needs_shell
@pytest.mark.parametrize(('status', 'text', 'expected'), [
    (200, f'send:{OUTSIDE}', f'send:{OUTSIDE}\n'), (200, 'refuse:486', 'refuse:486\n'),
    (200, 'send:+1;rm -rf /', 'refuse:503\n'), (200, 'receive:+1303', 'refuse:503\n'),
    (200, 'refuse:200', 'refuse:503\n'), (500, 'oops', 'refuse:503\n'), (401, '', 'refuse:503\n')])
def test_the_script_answers_only_what_faxbot_said_and_refuses_otherwise(tmp_path, status, text, expected):
    faxbot = _Answer(status, text)
    try:
        result = _run(tmp_path, faxbot.url, 'admit', 'server=' + 'a' * 32, 'address=192.168.68.50;x',
                      'dialed=3035550199"}', 'callid64=YWJj', 'uniqueid=1.2')
    finally:
        faxbot.close()
    assert result.stdout == expected
    [request] = faxbot.requests
    assert request['secret'] == 'synthetic-file-secret' and request['path'].endswith('/fax-server/admit')
    assert request['body']['address'] == '192.168.68.50' and request['body']['dialed'] == '3035550199'


@needs_shell
def test_the_script_refuses_when_faxbot_cannot_be_asked(tmp_path):
    assert _run(tmp_path, 'http://127.0.0.1:9', 'admit', 'server=' + 'a' * 32).stdout == 'refuse:503\n'
    assert _run(tmp_path, 'http://127.0.0.1:9', 'admit', 'server=' + 'a' * 32, secret=None).stdout == 'refuse:503\n'


@needs_shell
def test_the_hand_over_sends_only_images_from_the_fax_server_folder(tmp_path):
    faxbot = _Answer(200, '{}')
    image = tmp_path / 'fax-server' / '1.2.tiff'
    image.parent.mkdir()
    image.write_bytes(b'II*\x00')
    try:
        good = _run(tmp_path, faxbot.url, 'handover', 'server=' + 'a' * 32, f'file={image}', 'status=SUCCESS',
                    'pages=2', 'uniqueid=1.2', 'callid64=YWJj')
        outside = _run(tmp_path, faxbot.url, 'handover', 'server=' + 'a' * 32, f'file={tmp_path}/inbound/1.2.tiff',
                       'status=SUCCESS')
    finally:
        faxbot.close()
    assert (good.stdout, outside.stdout) == ('ok\n', 'ok\n')
    first, second = (request['body'] for request in faxbot.requests)
    assert (first['tiff_path'], first['faxstatus'], first['faxpages']) == (str(image), 'SUCCESS', 2)
    assert second['tiff_path'] is None  # a file outside its folder is never sent


# The SR140 stand-in for the live test (tools/sr140-emulator) ----------------------------------------------------

def test_the_sr140_stand_in_calls_like_an_sr140_and_has_every_refusal_case():
    import re
    import yaml
    folder = ROOT / 'tools' / 'sr140-emulator'
    template = (folder / 'etc' / 'pjsip.conf.template').read_text()
    run = (folder / 'run.sh').read_text()
    # Every value run.sh fills in is one it names for envsubst, so nothing is left as ${...} in pjsip.conf.
    named = run.split('envsubst', 1)[1].split('\n', 1)[0]
    names = set(re.findall(r'\$\{([A-Z0-9_]+)\}', template))
    assert names and all('${' + name + '}' in named for name in names)
    # No registration (SR140 Primary Gateway mode), T.38 offered, and the three endpoints send.sh may use.
    assert 'type=registration' not in template and 't38_udptl=yes' in template
    endpoints = set(re.findall(r'^\[([a-z0-9-]+)\]\ntype=endpoint', template, re.M))
    assert endpoints == {'faxbot-gateway', 'faxbot-gateway-wrong', 'faxbot-gateway-none'}
    send = (folder / 'send.sh').read_text()
    assert 'faxbot-gateway|faxbot-gateway-wrong|faxbot-gateway-none' in send
    assert 'outbound_auth=faxbot-gateway-auth' in template and 'password=${SR140_PASSWORD}' in template
    none = template.split('[faxbot-gateway-none]\ntype=endpoint', 1)[1]
    assert 'outbound_auth' not in none
    dialplan = (folder / 'etc' / 'extensions.conf').read_text()
    assert 'SendFAX(${FAXFILE},z)' in dialplan and 'exten => failed,1' in dialplan
    assert 'Context: sr140-send' in send and 'Setvar: SR140_TO=' in send
    compose = yaml.safe_load((folder / 'docker-compose.yml').read_text())
    service = compose['services']['sr140']
    assert service['network_mode'] == 'host' and service['entrypoint'] == ['/bin/sh', '/emulator/run.sh']
    assert (folder / 'fax' / 'test-fax.tiff').stat().st_size < 20000
    for script in ('run.sh', 'send.sh'):
        assert subprocess.run(['sh', '-n', str(folder / script)], capture_output=True).returncode == 0


# asterisk/start.sh fills in where fax servers reach Faxbot ---------------------------------------------------------

def _fax_server_trunk(tmp_path):
    server = fax_servers.FaxServer('a' * 32, 'RightFax', '192.168.68.50', 'faxserver-aaaaaaaa', 'b' * 32, None,
                                   ('US',), 60, 500, None, None, None, 'active')
    shared = tmp_path / 'data' / 'asterisk'
    shared.mkdir(parents=True, exist_ok=True)
    text = sip_trunk.rendered_configuration(_values(), peers=[], copiers=[], servers=[server])
    (shared / 'pjsip.conf').write_text(text)
    # The image ships these two; start.sh narrows them to the published range.
    (tmp_path / 'etc').mkdir(exist_ok=True)
    for name in ('rtp.conf', 'udptl.conf'):
        shutil.copy(ROOT / 'asterisk' / 'etc' / 'asterisk' / name, tmp_path / 'etc' / name)
    return text


@pytest.mark.skipif(shutil.which('bash') is None or shutil.which('envsubst') is None,
                    reason='bash and envsubst are needed to run the start script.')
def test_start_names_the_published_address_and_port_or_drops_the_lines(tmp_path):
    from api.tests.test_asterisk_start import start
    text = _fax_server_trunk(tmp_path / 'one')
    result, etc, shared = start(tmp_path / 'one', FAXBOT_FAX_SERVER_ADDRESS='192.168.68.230',
                                FAXBOT_FAX_SERVER_PORT='5070', FAXBOT_MEDIA_PORTS='6000-6039')
    assert result.returncode == 0, result.stderr
    loaded = (etc / 'pjsip.conf').read_text()
    transport = loaded.split('[transport-fax-server]\n', 1)[1].split('\n\n', 1)[0]
    assert 'external_media_address=192.168.68.230' in transport and 'external_signaling_port=5070' in transport
    assert '@FAXBOT_FAX_SERVER' not in loaded and (shared / 'pjsip.conf.started').read_text() == text
    assert (tmp_path / 'one' / 'data' / 'fax-server').is_dir() and 'media range' not in result.stderr
    # Without docker-compose.fax-server.yml the lines go, and the transport names no address at all.
    _fax_server_trunk(tmp_path / 'two')
    result, etc, _ = start(tmp_path / 'two')
    loaded = (etc / 'pjsip.conf').read_text()
    assert result.returncode == 0 and '@FAXBOT_FAX_SERVER' not in loaded
    assert 'external_signaling_port' not in loaded.split('[transport-fax-server]\n', 1)[1].split('\n\n', 1)[0]
    # No published media range: Asterisk still starts, and says why fax servers' faxes would fail.
    _fax_server_trunk(tmp_path / 'three')
    result, _, _ = start(tmp_path / 'three', FAXBOT_FAX_SERVER_ADDRESS='192.168.68.230')
    assert result.returncode == 0 and 'Fax servers need a published media range' in result.stderr
    for number, (environment, message) in enumerate((
            ({'FAXBOT_FAX_SERVER_ADDRESS': '192.168.68.300'}, 'Unsupported fax server address'),
            ({'FAXBOT_FAX_SERVER_ADDRESS': '192.168.68.2/s/x/'}, 'Unsupported fax server address'),
            ({'FAXBOT_FAX_SERVER_ADDRESS': '192.168.68.2', 'FAXBOT_FAX_SERVER_PORT': '70000'},
             'Unsupported fax server port'))):
        folder = tmp_path / f'bad{number}'
        _fax_server_trunk(folder)
        result, _, _ = start(folder, **environment)
        assert result.returncode != 0 and message in result.stderr
