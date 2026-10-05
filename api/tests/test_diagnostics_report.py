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
    assert status == PROBLEM and 'did not accept' in sentence and 'Providers' in sentence


def test_unreachable_provider_is_attention_not_a_problem(monkeypatch):
    status, sentence = _signed_in(monkeypatch, 'humblefax', RuntimeError('HumbleFax user request failed.'))
    assert status == ATTENTION and sentence.startswith('Faxbot could not reach HumbleFax')


def test_missing_sign_in_details(monkeypatch):
    status, sentence = _signed_in(monkeypatch, 'humblefax', (), configured=False)
    assert status == PROBLEM and sentence == "Some of HumbleFax's sign-in details are missing. Add them in Providers."


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
    assert by_id['sending.provider']['fix'] == {'label': 'Open Providers', 'page': 'providers/sending'}
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
         'NextReg': '3540', 'Transport': 'trunk-transport-tls'}])
    view = _run(report.engine_rows('registrations'))
    assert view['available'] and view['columns'][0] == 'Name' and view['rows'] == [
        ['trunk-registration', 'Registered', 'sip:sip.example.com', 'trunk-transport-tls', '3540']]


def test_engine_contacts_round_trip_in_milliseconds(monkeypatch):
    _engine(monkeypatch, {'response': 'Success', 'message': ''},
            [{'ObjectName': 'trunk-aor@@sip:sip.example.com', 'Status': 'Reachable', 'RoundtripUsec': '38211'}])
    assert _run(report.engine_rows('contacts'))['rows'][0][2] == '38'


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
