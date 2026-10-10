"""Diagnostics report: live read-only checks, one sentence and one fix each, never a secret."""
import asyncio
import json
import smtplib
from types import SimpleNamespace

import pytest

from app import diagnostics_report as report
from app.diagnostics_report import ATTENTION, OFF, OK, PROBLEM, Finding


def _run(coroutine):
    return asyncio.run(coroutine)


# --- provider sign-in --------------------------------------------------------

class _Service:
    def __init__(self, outcome, configured=True):
        self.outcome, self.configured = outcome, configured

    def is_configured(self):
        return self.configured

    async def account_numbers(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    authenticate = account_numbers


def _signed_in(monkeypatch, provider, outcome, configured=True):
    import app.provider_execution as execution
    monkeypatch.setattr(execution, 'service_from_profile', lambda profile: _Service(outcome, configured))
    return _run(report._sign_in(SimpleNamespace(provider_id=provider)))


def test_humblefax_sign_in_accepted_names_the_account_number(monkeypatch):
    status, sentence = _signed_in(monkeypatch, 'humblefax', ('+13035550100',))
    assert status == OK and sentence == "HumbleFax accepted Faxbot's sign-in for +13035550100."


@pytest.mark.parametrize('provider, error_class', [
    ('humblefax', 'app.humblefax_service.HumbleFaxCredentialsError'),
    ('efax', 'app.efax_service.EfaxCredentialsError'),
])
def test_refused_sign_in_is_a_problem_with_the_fix(monkeypatch, provider, error_class):
    module, name = error_class.rsplit('.', 1)
    error = getattr(__import__(module, fromlist=[name]), name)()
    status, sentence = _signed_in(monkeypatch, provider, error)
    assert status == PROBLEM and 'did not accept' in sentence and 'Delivery setup' in sentence


def test_unreachable_provider_is_attention_not_a_problem(monkeypatch):
    status, sentence = _signed_in(monkeypatch, 'humblefax', RuntimeError('HumbleFax user request failed.'))
    assert status == ATTENTION and sentence.startswith('Faxbot could not reach HumbleFax')


def test_missing_sign_in_details(monkeypatch):
    status, sentence = _signed_in(monkeypatch, 'humblefax', (), configured=False)
    assert status == PROBLEM and sentence == "Some of HumbleFax's sign-in details are missing. Add them in Delivery setup."


@pytest.mark.parametrize('provider, status', [('freeswitch', ATTENTION), ('sip', OK)])
def test_freeswitch_sending_says_it_is_removed_next_release(monkeypatch, provider, status):
    monkeypatch.setattr(report, '_profile', lambda request, direction: SimpleNamespace(provider_id=provider))
    (finding,) = _run(report.sending(SimpleNamespace(request=None)))
    assert finding.status == status
    if provider == 'freeswitch':
        assert finding.sentence == "FreeSWITCH, your sending provider, won't work after the next update. Choose a new one in Setup."
        assert (finding.fix_label, finding.fix_page) == ('Open Setup', 'system/setup')


def test_slow_provider_times_out(monkeypatch):
    monkeypatch.setattr(report, 'CHECK_SECONDS', 0.05)

    class Slow(_Service):
        async def account_numbers(self):
            await asyncio.sleep(1)

    import app.provider_execution as execution
    monkeypatch.setattr(execution, 'service_from_profile', lambda profile: Slow(()))
    status, sentence = _run(report._sign_in(SimpleNamespace(provider_id='humblefax')))
    assert status == ATTENTION and 'did not answer' in sentence


# --- email sign-in -----------------------------------------------------------

class _SMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        if host == 'unreachable.example':
            raise OSError('no route')
        self.calls = []
        _SMTP.instances.append(self)

    def ehlo(self):
        self.calls.append('ehlo')

    def starttls(self, context=None):
        self.calls.append('starttls')

    def login(self, user, password):
        self.calls.append('login')
        if password != 'right':
            raise smtplib.SMTPAuthenticationError(535, b'no')

    def mail(self, *args):  # pragma: no cover - a sign-in check must never get here
        raise AssertionError('diagnostics must not start a message')

    sendmail = data = rcpt = mail

    def quit(self):
        self.calls.append('quit')

    def close(self):
        self.calls.append('close')


@pytest.mark.parametrize('host, password, expected', [
    ('smtp.example', 'right', None),
    ('smtp.example', 'wrong', 'The email server did not accept the user name or password.'),
    ('unreachable.example', 'right', 'Faxbot could not reach the email server.'),
])
def test_email_sign_in_never_sends_a_message(monkeypatch, host, password, expected):
    _SMTP.instances.clear()
    monkeypatch.setattr(smtplib, 'SMTP', _SMTP)
    settings = SimpleNamespace(host=host, port=587, security='starttls', username='fax@example.com')
    assert report.smtp_sign_in(settings, password) == expected
    for client in _SMTP.instances:
        assert client.calls[:2] == ['ehlo', 'starttls'] and client.calls[-1] == 'quit'


# --- verdict and failures ------------------------------------------------------

def _finding(status):
    return Finding('x', 'server', 'X', status, 'Sentence.')


@pytest.mark.parametrize('statuses, expected', [
    ([OK, OFF], (OK, 'Everything Faxbot checked is working.')),
    ([OK, ATTENTION], (ATTENTION, 'Faxbot is working. 1 thing needs attention.')),
    ([ATTENTION, ATTENTION], (ATTENTION, 'Faxbot is working. 2 things need attention.')),
    ([PROBLEM, ATTENTION], (PROBLEM, '1 problem keeps Faxbot from working fully, and 1 more thing needs attention.')),
])
def test_verdict_sentence(statuses, expected):
    status, sentence = report._verdict([_finding(item) for item in statuses])
    assert status == expected[0]
    assert sentence == expected[1]


def test_a_check_that_breaks_becomes_attention_not_a_failed_page():
    async def broken(context):
        raise RuntimeError('synthetic internal detail')
    findings = _run(report._run_one('email delivery', broken, None))
    assert [item.status for item in findings] == [ATTENTION]
    assert 'synthetic internal detail' not in findings[0].sentence


# --- through the API -----------------------------------------------------------

def test_report_runs_through_the_api_and_keeps_the_last_run(isolated_installation, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    bootstrap = 'synthetic-diagnostics-bootstrap'
    for name, value in {'API_KEY': bootstrap, 'REQUIRE_API_KEY': 'true', 'PUBLIC_API_URL': 'https://testserver',
                        'MAX_REQUESTS_PER_MINUTE': '0', 'AUDIT_LOG_ENABLED': 'false'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    monkeypatch.setattr(report, '_last', None)
    headers = {'X-API-Key': bootstrap}
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        assert client.post('/admin/diagnostics/report').status_code == 401
        empty = client.get('/admin/diagnostics/report', headers=headers).json()
        assert empty['checked_at'] is None and empty['sections'] == []
        ran = client.post('/admin/diagnostics/report', headers=headers)
        assert ran.status_code == 200
        body = ran.json()
        assert client.get('/admin/diagnostics/report', headers=headers).json() == body
    by_id = {item['id']: item for section in body['sections'] for item in section['checks']}
    assert {section['id'] for section in body['sections']} >= {'sending', 'server', 'security'}
    # The installation's Phaxio profile has no keys: a problem with the place to fix it.
    assert by_id['sending.provider']['status'] == PROBLEM
    assert by_id['sending.provider']['fix'] == {'label': 'Open Providers & accounts', 'page': 'providers/sending'}
    assert by_id['sending.recent']['sentence'] == 'No fax has been sent yet.'
    assert by_id['security.audit']['status'] == ATTENTION and by_id['security.audit']['fix']['page'] == 'system/audit'
    assert by_id['server.time_zone']['sentence'] == (
        'No time zone is chosen, so times are shown in world standard time (UTC).')
    assert by_id['engine.running']['status'] == OFF
    assert body['status'] == PROBLEM and body['summary'].startswith('1 problem')
    text = json.dumps(body)
    assert bootstrap not in text and 'synthetic' not in text
    for item in by_id.values():
        assert item['status'] in {OK, ATTENTION, PROBLEM, OFF}
        assert item['sentence'] and (item['sentence'][0].isupper() or item['sentence'][0].isdigit())
        assert item['sentence'].endswith('.')


# --- fax engine views (System → Developer → Scripts & checks) -------------------

def _engine(monkeypatch, response, events, connected=True):
    from app.ami import ami_client

    async def query(fields, *, collect=False):
        if isinstance(response, BaseException):
            raise response
        return response, events
    monkeypatch.setattr(ami_client, 'status_query', query)
    monkeypatch.setattr(ami_client._connected, 'is_set', lambda: connected)


def test_engine_registrations_in_plain_columns(monkeypatch):
    _engine(monkeypatch, {'response': 'Success', 'message': ''}, [
        {'ObjectName': 'trunk-registration', 'Status': 'Registered', 'ServerUri': 'sip:sip.example.com',
         'NextReg': '0', 'Transport': 'trunk-transport-tls'}])
    view = _run(report.engine_rows('registrations'))
    assert view['available'] and view['columns'][0] == 'Name' and view['rows'] == [
        ['trunk-registration', 'Registered', 'sip:sip.example.com', 'trunk-transport-tls']]


def test_engine_contacts_round_trip_in_milliseconds(monkeypatch):
    _engine(monkeypatch, {'response': 'Success', 'message': ''},
            [{'URI': 'sip:sip.example.com:5061;transport=tls', 'Status': 'Reachable', 'RoundtripUsec': '38211'}])
    assert _run(report.engine_rows('contacts'))['rows'] == [['sip:sip.example.com:5061;transport=tls', 'Reachable', '38']]


@pytest.mark.parametrize('view, sentence', [('calls', 'No call is in progress.'), ('faxes', 'No fax is in progress.')])
def test_engine_empty_views_say_so(monkeypatch, view, sentence):
    _engine(monkeypatch, {'response': 'Success', 'message': ''}, [])
    result = _run(report.engine_rows(view))
    assert result['available'] and result['rows'] == [] and result['message'] == sentence


@pytest.mark.parametrize('response, connected, sentence', [
    ({'response': 'Error', 'message': 'Permission denied'}, True, "Faxbot's fax engine login may not read this."),
    (TimeoutError('slow'), True, 'The fax engine did not answer. Check again in a moment.'),
    ({'response': 'Success', 'message': ''}, False, None),
])
def test_engine_view_failures_are_one_sentence(monkeypatch, response, connected, sentence):
    _engine(monkeypatch, response, [], connected=connected)
    result = _run(report.engine_rows('faxes'))
    assert not result['available'] and result['rows'] == []
    assert result['message'] == sentence if sentence else result['message']


def test_engine_status_replies_keep_only_allowlisted_fields():
    """A status reply's sign-in details (AuthDetail events) are dropped as they are read."""
    from app.ami import AMIClient, STATUS_EVENT_FIELDS
    query = {'response': asyncio.Future(loop=asyncio.new_event_loop()), 'done': None, 'events': []}
    query['response'].set_result({})
    AMIClient._collect(query, {'Event': 'AuthDetail', 'Password': 'synthetic-secret'},
                       {'event': 'authdetail', 'password': 'synthetic-secret'})
    AMIClient._collect(query, {'Event': 'CoreShowChannel', 'Channel': 'PJSIP/trunk-1', 'AccountCode': 'x'},
                       {'event': 'coreshowchannel', 'channel': 'PJSIP/trunk-1'})
    assert query['events'] == [{'Channel': 'PJSIP/trunk-1'}]
    assert 'authdetail' not in STATUS_EVENT_FIELDS


# --- recent faxes, from real stored rows ---------------------------------------

def _fax_tables(monkeypatch, sent, received):
    """Minimal outbound_deliveries and inbound_imports tables, written the way Faxbot writes them."""
    from datetime import datetime
    import sqlalchemy as sa
    engine = sa.create_engine('sqlite://', poolclass=sa.pool.StaticPool, connect_args={'check_same_thread': False})
    metadata = sa.MetaData()
    deliveries = sa.Table('outbound_deliveries', metadata, sa.Column('id', sa.String), sa.Column('state', sa.String),
                          sa.Column('updated_at', sa.DateTime))
    imports = sa.Table('inbound_imports', metadata, sa.Column('id', sa.String), sa.Column('source', sa.String),
                       sa.Column('state', sa.String), sa.Column('acquired_at', sa.DateTime),
                       sa.Column('updated_at', sa.DateTime))
    metadata.create_all(engine)
    with engine.begin() as connection:
        for index, (state, at) in enumerate(sent):
            connection.execute(deliveries.insert().values(id=f's{index}', state=state, updated_at=at))
        for index, (source, state, at) in enumerate(received):
            connection.execute(imports.insert().values(id=f'r{index}', source=source, state=state, acquired_at=at,
                                                       updated_at=at))
    monkeypatch.setattr(report, '_store_engine', lambda: engine)
    monkeypatch.setattr(report, 'installation_zone_name', lambda: 'America/Denver')
    import app.people_time as people_time
    monkeypatch.setattr(people_time, 'installation_zone_name', lambda: 'America/Denver')
    return datetime(2026, 10, 5, 4, 0)


def test_recent_sent_from_stored_rows(monkeypatch):
    from datetime import datetime
    now = _fax_tables(monkeypatch, [('success', datetime(2026, 10, 4, 20, 55)), ('failed', datetime(2026, 10, 4, 18, 0)),
                                    ('reconciliation_required', datetime(2026, 10, 4, 19, 0))], [])
    findings = {item.id: item for item in _run(report.recent_sent(SimpleNamespace(now=now)))}
    assert findings['sending.recent'].status == OK
    assert findings['sending.recent'].sentence == (
        'Last fax delivered 4 Oct 2:55 PM MDT. In the last 7 days: 1 delivered, 1 failed.')
    assert findings['sending.confirm'].status == ATTENTION and findings['sending.confirm'].fix_page == 'faxes/sent'


def test_last_fax_failed_is_attention(monkeypatch):
    from datetime import datetime
    now = _fax_tables(monkeypatch, [('success', datetime(2026, 10, 3, 20, 0)), ('failed', datetime(2026, 10, 4, 20, 0))], [])
    finding = _run(report.recent_sent(SimpleNamespace(now=now)))[0]
    assert finding.status == ATTENTION and finding.sentence.startswith('The last fax failed (4 Oct 2:00 PM MDT).')


def test_recent_received_ignores_test_faxes_and_counts_failed_fetches(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(report, '_main', lambda: SimpleNamespace(settings=SimpleNamespace(inbound_enabled=True)))
    now = _fax_tables(monkeypatch, [], [('sip', 'received', datetime(2026, 10, 4, 20, 51)),
                                        ('test', 'received', datetime(2026, 10, 4, 23, 0)),
                                        ('humblefax', 'failed', None)])
    findings = {item.id: item for item in _run(report.recent_received(SimpleNamespace(now=now)))}
    assert findings['receiving.recent'].sentence == 'Last fax received 4 Oct 2:51 PM MDT. In the last 7 days: 1 received.'
    assert findings['receiving.failed'].status == PROBLEM
    assert 'Fetch again' in findings['receiving.failed'].sentence


def test_t38_off_reason_is_a_sentence_not_a_code(monkeypatch):
    import app.sip_http as sip_http
    monkeypatch.setattr(report, '_uses_trunk', lambda request: True)
    monkeypatch.setattr(report, 'installation_zone_name', lambda: 'America/Denver')

    async def status(request, identity):
        return {'preset_label': 'Telnyx', 'asterisk_connected': True, 'registration': 'registered',
                'message': 'The trunk is ready.', 't38_off_reason': 'no_data_back', 't38_off_at': '2026-10-04T03:01:00Z'}
    monkeypatch.setattr(sip_http, 'status', status)
    findings = {item.id: item for item in _run(report.carrier_trunk(SimpleNamespace(request=None, identity=None)))}
    assert findings['engine.trunk'].status == OK
    assert findings['engine.t38'].sentence == (
        'Off: on 3 October a T.38 fax got no fax data back on this network, so Faxbot uses audio fax.')


def test_engine_contacts_without_a_trunk(monkeypatch):
    _engine(monkeypatch, {'response': 'Error', 'message': 'Unable to retrieve endpoint trunk-endpoint'}, [])
    result = _run(report.engine_rows('contacts'))
    assert result['available'] and result['message'] == 'No carrier trunk is set up in the fax engine.'


def test_endpoint_reply_keeps_only_the_contact_status():
    """PJSIPShowEndpoint also sends AuthDetail with the trunk password: only ContactStatusDetail fields stay."""
    from app.ami import AMIClient
    query = {'response': asyncio.Future(loop=asyncio.new_event_loop()), 'done': None, 'events': []}
    query['response'].set_result({})
    for event in ({'Event': 'AuthDetail', 'Password': 'synthetic-secret', 'Username': 'u'},
                  {'Event': 'EndpointDetail', 'ObjectName': 'trunk-endpoint', 'FromUser': 'u'},
                  {'Event': 'ContactStatusDetail', 'URI': 'sip:sip.example.com', 'Status': 'Reachable',
                   'RoundtripUsec': '1000', 'EndpointName': 'trunk-endpoint'}):
        AMIClient._collect(query, event, {key.lower(): value for key, value in event.items()})
    assert query['events'] == [{'URI': 'sip:sip.example.com', 'Status': 'Reachable', 'RoundtripUsec': '1000'}]



@pytest.mark.parametrize('bucket, check, reachable, status', [
    ('', True, True, PROBLEM),
    ('faxes', False, True, ATTENTION),
    ('faxes', True, True, OK),
    ('faxes', True, False, PROBLEM),
])
def test_online_storage_is_checked_only_when_asked_and_never_assumed(monkeypatch, bucket, check, reachable, status):
    """Docs Autopilot finding (2026-10-05): a configured bucket was reported as working without any check."""
    monkeypatch.setattr(report, '_s3_reachable', lambda settings: reachable)
    settings = SimpleNamespace(s3_bucket=bucket, enable_s3_diagnostics=check)
    finding = report._storage_finding(settings)
    assert finding.status == status
    if status == ATTENTION:
        assert 'has not checked' in finding.sentence
