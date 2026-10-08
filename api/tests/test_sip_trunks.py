"""Several trunks (provider-rules design §3.6, WP-T): Asterisk's file, dialing per trunk on both engines, the
trunk a received call came in on, and Telnyx's T.38 check per trunk account.

The first trunk keeps its names, so an installation with one trunk renders exactly the file it always did
(``test_sip_trunk.py`` holds those golden files). Each trunk after it is an account with provider ``sip``,
stored in the configuration revision; its sections are named ``trunk-<key>-*`` and its endpoint sets
FAXBOT_TRUNK=<key> on every call. Everything here is synthetic: documentation addresses (RFC 5737), the
555-01xx numbers and made-up credentials.
"""
import asyncio
import json
from pathlib import Path
import re
import shutil

import httpx
import pytest

from app import ami, hylafax_engine, sip_calls, sip_trunk, telnyx_t38
from app.config_profiles import ConfigurationDocument
from app.config_values import ConfigurationValues
from api.app import schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'sip-trunks'
PASSWORD = 'synthetic-Trunk-Pass!42'
JOB, ATTEMPT = 'a' * 32, 'b' * 32
FIRST = {'SIP_TRUNK_CALLER_ID': '+15555550100', 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
         'SIP_TRUNK_PASSWORD': PASSWORD, 'SIP_TRUNK_DIDS': '+15555550100', 'FAX_DATA_DIR': '/faxdata'}
LEEDS = {'provider': 'sip', 'label': 'Leeds trunk (Gamma)', 'receives': True, 'numbers': ['+441132000000'],
         'settings': {'preset': 'gamma', 'host': '192.0.2.40', 'auth': 'ip', 'caller_id': '+441132000000',
                      'codecs': 'alaw'}}
WEST = {'provider': 'sip', 'label': 'West trunk (Flowroute)', 'receives': True, 'numbers': ['+15555550111'],
        'settings': {'preset': 'flowroute', 'auth': 'registration', 'username': '87654321',
                     'caller_id': '+15555550111', 't38': False},
        'credentials': {'password': 'synthetic-West-Pass!7'}}


def trunk_values(documents=None, **environment):
    values = ConfigurationValues.from_environment({**FIRST, **environment})
    return values.with_provider_accounts(ConfigurationDocument(documents or {}))


TWO = {'sip-leeds': LEEDS}
THREE = {'sip-leeds': LEEDS, 'sip-west': WEST}


# -- Asterisk's file ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('case, documents', [('two-trunks', TWO), ('three-trunks', THREE)])
def test_several_trunks_render_their_golden_files_with_the_first_trunk_unchanged(case, documents):
    rendered = sip_trunk.render_pjsip(trunk_values(documents))
    assert rendered == (FIXTURES / f'{case}.conf').read_text()
    # The first trunk's own sections are exactly its one-trunk file's (test_sip_trunk.py's golden case).
    alone = sip_trunk.render_pjsip(trunk_values())
    first = alone.split('[trunk-aor]', 1)[1]
    assert '[trunk-aor]' + first in rendered
    assert sip_trunk.render_pjsip(trunk_values({})) == alone


def test_each_trunk_after_the_first_names_itself_on_every_call_and_keeps_its_own_fax_settings():
    rendered = sip_trunk.render_pjsip(trunk_values(THREE))
    sections = dict(re.findall(r'\[([a-z0-9-]+)\]\n(.*?)(?=\n\n|\Z)', rendered, re.S))
    assert 'set_var=FAXBOT_TRUNK=sip-leeds' in sections['trunk-sip-leeds-endpoint']
    assert 'set_var=FAXBOT_TRUNK=sip-west' in sections['trunk-sip-west-endpoint']
    assert 'set_var' not in sections['trunk-endpoint']
    # Its own codecs and T.38 switch, never the first trunk's.
    assert 'allow=alaw\n' in sections['trunk-sip-leeds-endpoint'] + '\n'
    assert 't38_udptl=no' in sections['trunk-sip-west-endpoint']
    assert 't38_udptl=yes' in sections['trunk-endpoint'] and 't38_udptl=yes' in sections['trunk-sip-leeds-endpoint']
    # The registration trunk registers with its own credentials under its own names.
    assert 'outbound_auth=trunk-sip-west-auth' in sections['trunk-sip-west-reg']
    assert 'endpoint=trunk-sip-west-endpoint' in sections['trunk-sip-west-reg']
    assert 'password=synthetic-West-Pass!7' in sections['trunk-sip-west-auth']
    # A call that came down a registration is matched to that registration's trunk before any address.
    assert 'endpoint_identifier_order=line,ip,username,anonymous' in sections['global']
    assert sip_trunk.rendered_endpoints(trunk_values(THREE)) == (
        'trunk-endpoint', 'trunk-sip-leeds-endpoint', 'trunk-sip-west-endpoint')


def test_a_second_trunk_that_cannot_load_is_left_out_with_a_sentence_and_the_first_is_unchanged():
    alone = sip_trunk.render_pjsip(trunk_values())
    half = {'sip-half': {'provider': 'sip', 'label': 'Half trunk', 'settings': {'preset': 'gamma', 'auth': 'ip'}}}
    values = trunk_values(half)
    # Without a server the second trunk is not loaded; the first trunk's file is exactly as before.
    assert sip_trunk.render_pjsip(values) == alone
    assert sip_trunk.trunk_problems(values) == {'sip-half': 'Half trunk is not loaded yet: fill in its settings.'}
    # A trunk that is off is not loaded either, and has no problem to report.
    off = trunk_values({'sip-leeds': {**LEEDS, 'enabled': False}})
    assert sip_trunk.render_pjsip(off) == alone and sip_trunk.trunk_problems(off) == {}
    # A phone system cannot share the carrier's connection: its address settings would differ.
    pbx = {'sip-pbx': {'provider': 'sip', 'label': 'Office phone system',
                       'settings': {'preset': 'avaya-ipoffice', 'host': '192.168.10.5', 'auth': 'ip',
                                    'transport': 'udp'}}}
    clash = trunk_values(pbx, SIP_TRUNK_TRANSPORT='udp')
    assert sip_trunk.render_pjsip(clash) == sip_trunk.render_pjsip(trunk_values(SIP_TRUNK_TRANSPORT='udp'))
    assert 'cannot share one UDP connection' in sip_trunk.trunk_problems(clash)['sip-pbx']
    # On its own connection type it loads beside the carrier.
    assert '[trunk-sip-pbx-endpoint]' in sip_trunk.render_pjsip(trunk_values(pbx))


def test_two_trunks_at_one_carrier_share_its_addresses_and_the_called_number_decides():
    other = {'provider': 'sip', 'label': 'Second Telnyx trunk', 'receives': True, 'numbers': ['+15555550122'],
             'settings': {'preset': 'telnyx', 'auth': 'ip', 'caller_id': '+15555550122'}}
    values = trunk_values({'telnyx-2': other})
    rendered = sip_trunk.render_pjsip(values)
    # Asterisk would match both identify sections; only the first trunk's is written.
    assert '[trunk-telnyx-2-endpoint]' in rendered and '[trunk-telnyx-2-identify]' not in rendered
    assert sip_trunk.shared_addresses(values) == [['sip', 'telnyx-2']]
    from app.inbound.sip_handover import receiving_trunk
    # Asterisk matched the call to the first trunk; the number it called belongs to the second.
    assert receiving_trunk(values, {'to_number': '+15555550122'}) == 'telnyx-2'
    assert receiving_trunk(values, {'to_number': '+15555550100'}) is None
    assert receiving_trunk(values, {'call': {'did': '15555550122'}}, country='US') == 'telnyx-2'
    # A number on neither trunk stays on the trunk Asterisk chose; a trunk with its own addresses is never moved.
    assert receiving_trunk(values, {'to_number': '+15555550199'}) is None
    leeds = trunk_values(TWO)
    assert receiving_trunk(leeds, {'trunk': 'sip-leeds', 'to_number': '+15555550100'}) == 'sip-leeds'
    assert receiving_trunk(leeds, {'trunk': 'not a key!'}) is None


def test_the_trunk_numbers_and_endpoints_come_from_each_trunk_account():
    values = trunk_values(THREE)
    assert sip_trunk.trunk_numbers(values) == {'sip': ('+15555550100',), 'sip-leeds': ('+441132000000',),
                                               'sip-west': ('+15555550111',)}
    assert sip_trunk.endpoint_name(None) == sip_trunk.endpoint_name('sip') == 'trunk-endpoint'
    assert sip_trunk.endpoint_name('sip-leeds') == 'trunk-sip-leeds-endpoint'
    with pytest.raises(ValueError):
        sip_trunk.endpoint_name('Leeds;evil')


# -- dialing per trunk ----------------------------------------------------------------------------------------

def test_a_fax_over_the_second_trunk_dials_its_endpoint_with_its_own_caller_id_and_number_format():
    values = trunk_values(TWO, FAX_DEFAULT_COUNTRY='GB')
    fields = ami.originate_fields_for(values, JOB, '+442079460000', '/faxdata/x.tiff', attempt_id=ATTEMPT,
                                      trunk='sip-leeds')
    assert fields['Channel'] == 'PJSIP/+442079460000@trunk-sip-leeds-endpoint'
    assert fields['CallerID'] == '+441132000000'
    first = ami.originate_fields_for(values, JOB, '+442079460000', '/faxdata/x.tiff', attempt_id=ATTEMPT)
    assert first['Channel'] == 'PJSIP/+442079460000@trunk-endpoint' and first['CallerID'] == '+15555550100'
    assert ami.originate_fields_for(values, JOB, '+442079460000', '/faxdata/x.tiff', attempt_id=ATTEMPT,
                                    trunk='sip') == first


@pytest.mark.parametrize('documents, trunk', [
    (TWO, 'sip-west'),                                                # not an account at all
    ({'sip-leeds': {**LEEDS, 'enabled': False}}, 'sip-leeds'),        # turned off: not in Asterisk's file
    ({'sip-leeds': {**LEEDS, 'settings': {'preset': 'gamma'}}}, 'sip-leeds'),  # not filled in
])
def test_a_trunk_that_is_not_in_asterisks_file_is_never_dialed(documents, trunk):
    with pytest.raises(ami.UnknownTrunk):
        ami.originate_fields_for(trunk_values(documents), JOB, '+15555550199', '/faxdata/x.tiff',
                                 attempt_id=ATTEMPT, trunk=trunk)
    with pytest.raises(ValueError):
        ami.prepare_originate_fields(JOB, '+15555550199', '/faxdata/x.tiff', caller_id='+15555550100',
                                     endpoint='trunk-endpoint,evil')


def test_the_engines_call_plan_names_the_trunk_and_accepts_only_rendered_endpoints():
    values = trunk_values(TWO)
    endpoints = sip_trunk.rendered_endpoints(values)
    first = ami.originate_fields_for(values, JOB, '+15555550199', '/faxdata/x.tiff', attempt_id=ATTEMPT)
    second = ami.originate_fields_for(values, JOB, '+15555550199', '/faxdata/x.tiff', attempt_id=ATTEMPT,
                                      trunk='sip-leeds')
    # The first trunk's plan is exactly as before: six fields.
    assert hylafax_engine.call_plan(first, JOB, ATTEMPT, endpoints=endpoints) == \
        f'+15555550199/+15555550100/{JOB}/{ATTEMPT}/1/1'
    assert hylafax_engine.call_plan(second, JOB, ATTEMPT, t38=False, endpoints=endpoints) == \
        f'+15555550199/+441132000000/{JOB}/{ATTEMPT}/1/0/trunk-sip-leeds-endpoint'
    for channel in ('PJSIP/+15555550199@trunk-sip-west-endpoint', 'PJSIP/+15555550199@trunk-endpoint;x',
                    'PJSIP/+15555550199@other-endpoint'):
        with pytest.raises(ValueError):
            hylafax_engine.call_plan({**second, 'Channel': channel}, JOB, ATTEMPT, endpoints=endpoints)


class FakeAMI(ami.AMIClient):
    """Keeps every action it is given and acknowledges it, like Asterisk's manager would."""

    def __init__(self):
        super().__init__()
        self.actions = []
        self._connected.set()

    async def _send_action(self, fields):
        self.actions.append(dict(fields))

    async def db_put(self, family, key, value):
        self.actions.append({'Action': 'DBPut', 'Family': family, 'Key': key, 'Val': value})


def test_the_built_in_engine_originates_over_the_trunk_the_fax_was_given(monkeypatch):
    values = trunk_values(TWO)
    monkeypatch.setattr(ami, 'settings', values)
    client = FakeAMI()
    submissions = []
    client.on_submission(submissions.append)
    asyncio.run(client.originate_sendfax(JOB, '+15555550199', '/faxdata/x.tiff', attempt_id=ATTEMPT,
                                         trunk='sip-leeds'))
    [action] = client.actions
    assert action['Channel'] == 'PJSIP/+15555550199@trunk-sip-leeds-endpoint'
    assert submissions[0]['Trunk'] == 'sip-leeds' and submissions[0]['Preset'] == 'gamma'
    asyncio.run(client.originate_sendfax(JOB, '+15555550199', '/faxdata/x.tiff', attempt_id=ATTEMPT))
    assert client.actions[1]['Channel'] == 'PJSIP/+15555550199@trunk-endpoint' and 'Trunk' not in submissions[1]


def test_a_prepared_trunk_fax_is_submitted_over_the_trunk_its_account_names(monkeypatch):
    from types import SimpleNamespace
    from app.outbound_transport import PreparedSubmission
    monkeypatch.setattr(ami, 'settings', trunk_values(TWO))
    client = FakeAMI()
    configuration = SimpleNamespace(provider_id='sip', manifest=None, settings={'trunk': 'sip-leeds'})
    claim = SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT)
    prepared = PreparedSubmission(claim, SimpleNamespace(configuration=configuration), {'to_number': '+15555550199'},
                                  '/faxdata/x.pdf', '/faxdata/x.tiff', None, None, client, trunk='sip-leeds')
    receipt = asyncio.run(prepared.submit())
    assert receipt.status == 'in_progress'
    assert client.actions[0]['Channel'] == 'PJSIP/+15555550199@trunk-sip-leeds-endpoint'


def test_the_ssl_fax_engines_job_stores_a_plan_for_the_trunk_it_was_given(monkeypatch, tmp_path):
    values = trunk_values(TWO, FAX_DATA_DIR=str(tmp_path))
    client = FakeAMI()
    monkeypatch.setattr(hylafax_engine, 'create_job', lambda *a, **k: hylafax_engine.PreparedJob(None, tag=k['tag']))
    from app.routing.reply_number import Choice
    monkeypatch.setattr(ami, 'reply_choice', lambda values, mailbox_id=None: Choice(None, 'line', 'The line.'))
    monkeypatch.setattr(ami, 'sender_identity', lambda job_id: None)
    job = asyncio.run(hylafax_engine.prepare_job(values, client, job_id=JOB, attempt_id=ATTEMPT,
                                                 dest='+15555550199', tiff_path=str(tmp_path / 'x.tiff'),
                                                 trunk='sip-leeds'))
    [put] = client.actions
    assert put['Action'] == 'DBPut' and put['Val'].endswith('/trunk-sip-leeds-endpoint')
    assert put['Val'].split('/')[1] == '+441132000000'
    assert job.submission['Trunk'] == 'sip-leeds' and job.submission['Preset'] == 'gamma'


def test_a_call_record_keeps_the_trunk_a_sent_or_received_call_went_over(database):  # noqa: F811
    import sqlalchemy as sa
    schema.upgrade_schema(database)
    db = database
    records = sip_calls.SipCallRecords(db)
    records.record_submission({'JobID': JOB, 'AttemptID': ATTEMPT, 'Called': '+15555550199',
                               'CallerID': '+441132000000', 'Preset': 'gamma', 'Trunk': 'sip-leeds'})
    records.record_inbound({'did': '+441132000000', 'caller': '+15555550100', 'trunk': 'sip-leeds'},
                           call_id='1791083644.7')
    records.record_inbound({'did': '+15555550100', 'caller': '+15555550199'}, call_id='1791083644.8')
    with db.connect() as connection:
        table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=connection)
        found = {row.call_id: row.trunk_key for row in connection.execute(sa.select(table))}
    assert found == {ATTEMPT: 'sip-leeds', '1791083644.7': 'sip-leeds', '1791083644.8': None}
    assert records.inbound_call('1791083644.7')['trunk'] == 'sip-leeds'
    assert 'trunk' not in records.inbound_call('1791083644.8')


# -- the dialplan ----------------------------------------------------------------------------------------------

def _context(name):
    text = (ROOT / 'asterisk' / 'etc' / 'asterisk' / 'extensions.conf').read_text()
    return text.split(f'[{name}]\n', 1)[1].split('\n[', 1)[0]


def test_the_dialplan_reads_each_calls_own_trunk_and_hands_it_over():
    receive, done, engine_in = (_context(name) for name in ('faxbot-inbound-receive', 'faxbot-inbound-done',
                                                             'faxbot-engine-in'))
    assert 'Set(FAXBOT_TRUNK=${FILTER(abcdefghijklmnopqrstuvwxyz0123456789_-,${FAXBOT_TRUNK})})' in receive
    # T.38 checks read the endpoint the call is on, never the first trunk's by name.
    for section in (receive, engine_in, _context('faxbot-send')):
        assert 'PJSIP_ENDPOINT(trunk-endpoint' not in section
        assert 'PJSIP_ENDPOINT(${CHANNEL(endpoint)},t38_udptl)' in section
    assert ' trunk=${FILTER(abcdefghijklmnopqrstuvwxyz0123456789_-,${FAXBOT_TRUNK})}' in done
    assert done.count('UserEvent(FaxInboundCall,DID:${FAXBOT_DID},Trunk:${FAXBOT_TRUNK},') == 2
    # The SSL Fax engine hears the trunk in the call's name: <token>.<digits>.<trunk>, nothing on the first trunk.
    assert 'Set(CALLERID(name)=${CALLERID(name)}.${FAXBOT_TRUNK})' in engine_in
    assert 'GotoIf($["${FAXBOT_TRUNK}" = ""]?named)' in engine_in
    assert 'Trunk:${FAXBOT_TRUNK}' in _context('faxbot-engine-in-done')


needs_shell = pytest.mark.skipif(shutil.which('sh') is None or shutil.which('curl') is None,
                                 reason='The notifier needs sh and curl.')
from api.tests.test_hylafax_scripts import engine  # noqa: E402,F401 (fixture: the engine scripts' stand-in tools)


@needs_shell
def test_the_notifier_hands_over_the_trunk_key_and_nothing_else(tmp_path):
    from api.tests.test_inbound_handover import _Faxbot, notify
    faxbot = _Faxbot(200)
    try:
        named = notify(tmp_path / 'one', faxbot.url, extra=['trunk=sip-leeds'])
        junk = notify(tmp_path / 'two', faxbot.url, extra=['trunk=Sip-Leeds"},{"x":"y'])
        first = notify(tmp_path / 'three', faxbot.url)
    finally:
        faxbot.close()
    assert [result.stdout for result in (named, junk, first)] == ['ok\n'] * 3
    bodies = [request['body'] for request in faxbot.requests]
    assert [(body['trunk'], body['call']['trunk']) for body in bodies] == [
        ('sip-leeds', 'sip-leeds'), ('ip-eedsxy', 'ip-eedsxy'), (None, None)]


# -- the SSL Fax engine's received faxes ------------------------------------------------------------------------

@pytest.mark.parametrize('name, called, trunk', [
    ('179117219142.441132000000.sip-leeds', '441132000000', 'sip-leeds'),   # a trunk after the first
    ('179117219142.15555550100', '15555550100', None),                       # the first trunk: two parts
    ('179117219142.15555550100.BAD"}{', '15555550100', None),              # anything else is dropped
])
def test_the_engine_hands_over_the_trunk_named_in_the_calls_name(engine, tmp_path, name, called, trunk):
    from api.tests.test_hylafax_scripts import run
    spool, state, data, environment = engine
    (tmp_path / 'answer').write_text('200')
    received = run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000007', '',
                   '+15555550199', name, '', cwd=spool)
    assert received.returncode == 0, received.stderr
    ticket = next((state / 'received').glob('*.ticket')).read_text()
    assert f'called={called}\n' in ticket and f'trunk={trunk or ""}\n' in ticket
    assert run('handover', environment).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert body['uniqueid'] == 'engine.179117219142' and body['to_number'] == called
    assert body['trunk'] == trunk and body['call']['trunk'] == trunk and body['call']['did'] == called


def test_the_engines_call_name_parse_keeps_the_called_number_and_the_trunk_apart():
    """<token>.<digits>.<trunk>: the called number stays digits and the trunk is its own field."""
    script = (ROOT / 'hylafax' / 'bin' / 'received').read_text()
    assert "case $called in *.*) trunk=${called#*.}; called=${called%%.*} ;; esac" in script
    assert "printf 'trunk=%s\\n' \"$trunk\"" in script
    handover = (ROOT / 'hylafax' / 'bin' / 'handover').read_text()
    assert '"trunk":%s,"call":{"did":%s,"trunk":%s,' in handover
    sessions = (ROOT / 'hylafax' / 'bin' / 'sessions').read_text()
    assert '"trunk":"%s"' in sessions


# -- Telnyx's T.38 check per trunk account ------------------------------------------------------------------------

def test_telnyx_is_checked_per_trunk_account_with_its_own_key_and_numbers(monkeypatch, tmp_path):
    seen = []

    def handler(request):
        key = request.headers['Authorization'].split(' ', 1)[1]
        seen.append((key, request.url.path, dict(request.url.params)))
        if request.url.path == '/v2/phone_numbers':
            digits = request.url.params['filter[phone_number]']
            return httpx.Response(200, json={'data': [{'id': 'n' + digits[-4:], 'phone_number': '+' + digits,
                                                       'connection_id': 'c' + key[-1]}]})
        if request.url.path.endswith('/voice'):
            return httpx.Response(200, json={'data': {'media_features': {'t38_fax_gateway_enabled': True}}})
        # The first trunk signs in as faxbotuser; the second authenticates by address and has no user name.
        user = 'faxbotuser' if key.endswith('1') else ''
        return httpx.Response(200, json={'data': {'user_name': user,
                                                  'outbound': {'t38_reinvite_source': 'telnyx'}}})
    monkeypatch.setattr(telnyx_t38, 'TRANSPORT', httpx.MockTransport(handler))
    second = {'provider': 'sip', 'label': 'Denver trunk', 'numbers': ['+15555550122'],
              'settings': {'preset': 'telnyx', 'auth': 'ip', 'caller_id': '+15555550122'},
              'credentials': {'api_key': 'KEY-second-2'}}
    values = trunk_values({'telnyx-2': second, 'sip-leeds': LEEDS}, TELNYX_API_KEY='KEY-first-1',
                          FAX_DATA_DIR=str(tmp_path))
    results = telnyx_t38.check_all(values, now=lambda: 1791180000.0)
    # The Gamma trunk is not a Telnyx trunk: nothing is asked for it.
    assert set(results) == {'sip', 'telnyx-2', 'sip-leeds'} and results['sip-leeds'] is None
    assert {(key, params.get('filter[phone_number]')) for key, path, params in seen if path == '/v2/phone_numbers'} == {
        ('KEY-first-1', '15555550100'), ('KEY-second-2', '15555550122')}
    assert (tmp_path / 'asterisk' / 'telnyx-t38').is_file() and (tmp_path / 'asterisk' / 'telnyx-t38-telnyx-2').is_file()
    own = sip_trunk.trunk_for(values, 'telnyx-2').values
    report = telnyx_t38.report(own, 'telnyx-2')
    assert [entry['number'] for entry in report['numbers']] == ['+15555550122'] and report['ready'] is True
    assert [entry['number'] for entry in telnyx_t38.report(values)['numbers']] == ['+15555550100']
    # A second trunk never borrows the first trunk's Telnyx key.
    keyless = trunk_values({'telnyx-2': {**second, 'credentials': {}}}, TELNYX_API_KEY='KEY-first-1',
                           FAX_DATA_DIR=str(tmp_path))
    assert telnyx_t38.applies(sip_trunk.trunk_for(keyless, 'telnyx-2').values) is False


# -- received over a second trunk, through the API ------------------------------------------------------------------

def _image(folder, name):
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new('1', (20, 10), 1).save(path, format='TIFF')
    return path


def test_a_fax_received_on_a_second_trunk_is_recorded_on_that_trunk_account(isolated_installation, monkeypatch,
                                                                           tmp_path):
    from api.tests.test_inbound_acquisition import ADMIN, client, environment, rows
    from api.tests.test_receiving_accounts import add_account
    data = tmp_path / 'faxdata'
    environment(monkeypatch, FAX_BACKEND='sip', FAX_DATA_DIR=str(data), SIP_TRUNK_PRESET='telnyx',
                SIP_TRUNK_AUTH='ip', SIP_TRUNK_CALLER_ID='+15555550100', SIP_TRUNK_DIDS='+15555550100',
                FAX_DEFAULT_COUNTRY='US')
    secret = {'X-Internal-Secret': 'synthetic-internal'}
    with client() as http:
        add_account(http, key='telnyx-2', provider='sip', label='Second Telnyx trunk', numbers=['+15555550122'],
                    settings={'preset': 'telnyx', 'auth': 'ip', 'caller_id': '+15555550122'})
        add_account(http, key='sip-leeds', provider='sip', label='Leeds trunk', numbers=['+441132000000'],
                    settings={'preset': 'gamma', 'auth': 'ip', 'host': '192.0.2.40'})

        def hand_over(uniqueid, to_number, trunk=None):
            payload = {'tiff_path': str(_image(data / 'inbound', f'{uniqueid}.tiff')), 'to_number': to_number,
                       'from_number': '+15555550199', 'faxstatus': 'SUCCESS', 'faxpages': 1, 'uniqueid': uniqueid,
                       'trunk': trunk, 'call': {'did': to_number, 'caller': '+15555550199', 'pages': 1,
                                                'trunk': trunk}}
            answer = http.post('/_internal/asterisk/inbound', headers=secret, json=payload)
            assert answer.status_code == 200, answer.text
            return answer.json()['id']
        # Named by its endpoint; matched to the first trunk by address but called on the second's number; the first.
        hand_over('1791083644.1', '+441132000000', 'sip-leeds')
        hand_over('1791083644.2', '+15555550122')
        hand_over('1791083644.3', '+15555550100')
        status = http.get('/admin/sip/status', headers=ADMIN, params={'account': 'sip-leeds'})
        assert status.status_code == 200 and status.json()['account'] == 'sip-leeds'
        assert status.json()['preset'] == 'gamma'
        assert [item['key'] for item in status.json()['trunks']] == ['sip', 'sip-leeds', 'telnyx-2']
        assert http.get('/admin/sip/status', headers=ADMIN, params={'account': 'nope'}).status_code == 404
    imports = {row['operation_id']: row['account_key'] for row in rows(isolated_installation, 'inbound_imports')}
    assert imports == {'1791083644.1': 'sip-leeds', '1791083644.2': 'telnyx-2', '1791083644.3': 'sip'}
    calls = {row['call_id']: (row['trunk_key'], row['trunk_preset'])
             for row in rows(isolated_installation, 'sip_call_records')}
    assert calls == {'1791083644.1': ('sip-leeds', 'gamma'), '1791083644.2': ('telnyx-2', 'telnyx'),
                     '1791083644.3': (None, 'telnyx')}
