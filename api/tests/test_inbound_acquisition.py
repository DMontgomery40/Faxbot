"""Received faxes are acquired durably: authenticated first, resumable, with provenance.

Provider APIs are a synthetic ``httpx.MockTransport``; nothing leaves the test.
The background fetcher and the email delivery worker are held and single steps
are run, so each state is asserted deterministically.
"""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
from datetime import datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from app import main
from app.inbound import fetch as inbound_fetch
from app.inbound import http as inbound_http
from app.inbound.acquisition import LEASE, RETRY_MINUTES, utcnow
from api.tests.test_schema import database  # noqa: F401 (fixture)


BOOTSTRAP = 'synthetic-inbound-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
TOKEN = 'synthetic-phaxio-callback-token'
PHAXIO_KEY = 'synthetic-phaxio-key-one'
CALLBACK_URL = 'https://testserver/phaxio-inbound'
SINCH_PROJECT = 'synthetic-project'
TO, FROM = '+15555550123', '+15555550100'
COMPLETED = '2026-10-03T08:00:00.000-06:00'


def pdf_bytes(text='Synthetic received fax'):
    buffer = io.BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(72, 720, text)
    document.showPage()
    document.save()
    return buffer.getvalue()


class FakeProviders:
    """The Phaxio and Sinch API hosts; records every URL Faxbot requests."""

    def __init__(self):
        self.requests = []
        self.faxes = {}
        self.files = {}

    def phaxio(self, fax_id, **changes):
        self.faxes[('phaxio', str(fax_id))] = {
            'id': int(fax_id), 'direction': 'received', 'num_pages': 1, 'status': 'success', 'is_test': False,
            'created_at': '2026-10-03T07:59:00.000-06:00', 'completed_at': COMPLETED,
            'from_number': FROM, 'to_number': TO, **changes}

    def sinch(self, fax_id, **changes):
        self.faxes[('sinch', fax_id)] = {
            'id': fax_id, 'direction': 'INBOUND', 'from': FROM, 'to': TO, 'numberOfPages': 1,
            'status': 'COMPLETED', 'createTime': '2026-10-03T13:59:00Z', 'completedTime': '2026-10-03T14:00:00Z',
            'projectId': SINCH_PROJECT, **changes}

    def handler(self, request):
        self.requests.append(str(request.url))
        host, path = request.url.host, request.url.path
        match = None
        if host == 'api.phaxio.com':
            match = re.fullmatch(r'/v2\.1/faxes/([0-9]+)(/file)?', path)
            key = ('phaxio', match.group(1)) if match else None
        elif host == 'fax.api.sinch.com':
            match = re.fullmatch(rf'/v3/projects/{SINCH_PROJECT}/faxes/([A-Za-z0-9_-]+)(/file)?', path)
            key = ('sinch', match.group(1)) if match else None
        if match is None:
            return httpx.Response(404)
        if match.group(2):
            status, body = self.files.get(key, (404, b''))
            return httpx.Response(status, content=body, headers={'content-type': 'application/pdf'})
        fax = self.faxes.get(key)
        if fax is None:
            return httpx.Response(404, json={'success': False, 'message': 'Fax not found'})
        if key[0] == 'phaxio':
            return httpx.Response(200, json={'success': True, 'message': 'Retrieved fax', 'data': fax})
        return httpx.Response(200, json=fax)

    def hosts(self):
        return {httpx.URL(url).host for url in self.requests}


@pytest.fixture
def providers(monkeypatch):
    fake = FakeProviders()
    monkeypatch.setattr(inbound_fetch, '_TRANSPORT', httpx.MockTransport(fake.handler))
    monkeypatch.setattr(inbound_http, 'AUTOMATIC', False)
    # The lifespan's email delivery worker feeds received faxes every 5 s; a slow runner let it
    # feed first, so feed() saw 0. The test feeds them itself.
    monkeypatch.setattr('app.intake.worker.IntakeWorker.step', lambda self: False)
    return fake


def environment(monkeypatch, **extra):
    values = {'INBOUND_ENABLED': 'true', 'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP,
              'PUBLIC_API_URL': 'https://testserver', 'MAX_REQUESTS_PER_MINUTE': '0', 'FAX_BACKEND': 'phaxio',
              'PHAXIO_API_KEY': PHAXIO_KEY, 'PHAXIO_API_SECRET': 'synthetic-phaxio-secret',
              'PHAXIO_CALLBACK_TOKEN': TOKEN, 'PHAXIO_INBOUND_VERIFY_SIGNATURE': 'true',
              'SINCH_PROJECT_ID': SINCH_PROJECT, 'SINCH_API_KEY': 'synthetic-sinch-key',
              'SINCH_API_SECRET': 'synthetic-sinch-secret', 'ASTERISK_INBOUND_SECRET': 'synthetic-internal',
              'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', **extra}
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def client():
    return TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'})


def service():
    return main.app.state.inbound_acquisition


def step(*, later=None):
    """Run one fetcher step, optionally as if ``later`` had passed."""
    store = service().store
    if later is not None:
        moment = utcnow() + later
        store.clock = lambda: moment
    try:
        return asyncio.run(service().acquirer.step())
    finally:
        store.clock = utcnow


def engine(installation):
    return sa.create_engine(installation['DATABASE_URL'])


def rows(installation, table, where=''):
    database = engine(installation)
    try:
        with database.connect() as connection:
            return [dict(row) for row in connection.execute(sa.text(f'SELECT * FROM {table} {where}')).mappings()]
    finally:
        database.dispose()


def feed():
    from app.intake.store import IntakeStore
    from app.intake.worker import ConnectorSecrets
    runtime = main.app.state.configuration_runtime
    return IntakeStore(runtime.manager.store.engine, ConnectorSecrets(runtime.manager.store)).feed_inbound()


def phaxio_fields(fax_id, *, completed_at=COMPLETED, extra=()):
    fax = {'id': int(fax_id), 'direction': 'received', 'num_pages': 1, 'status': 'success', 'is_test': False,
           'created_at': '2026-10-03T07:59:00.000-06:00', 'from_number': FROM, 'to_number': TO}
    if completed_at:
        fax['completed_at'] = completed_at
    return [('fax', json.dumps(fax)), ('direction', 'received'), ('is_test', 'false'), ('success', 'true'),
            *extra]


def phaxio_signature(fields, files=(), *, token=TOKEN):
    # Frame the documented scheme independently of the verifier under test.
    message = CALLBACK_URL + ''.join(name + value for name, value in sorted(fields, key=lambda part: part[0]))
    message += ''.join(name + hashlib.sha1(data).hexdigest() for name, data in sorted(files, key=lambda part: part[0]))
    return hmac.new(token.encode(), message.encode(), hashlib.sha1).hexdigest()


def post_phaxio(http, fields, files=(), *, signature=None, sign=True):
    headers = {}
    if sign:
        headers['X-Phaxio-Signature'] = signature or phaxio_signature(fields, files)
    multipart = {name: ('fax.pdf', data, 'application/pdf') for name, data in files}
    return http.post('/phaxio-inbound', data=dict(fields), files=multipart or None, headers=headers)


def only_fax(http):
    items = http.get('/inbound', headers=ADMIN).json()
    assert len(items) == 1, items
    return items[0]


# 1. A failed fetch survives a restart and resumes as the same import ------------
def test_phaxio_fetch_failure_resumes_after_restart_as_one_authentic_document(isolated_installation, monkeypatch,
                                                                              providers):
    environment(monkeypatch)
    providers.phaxio(9001)
    providers.files[('phaxio', '9001')] = (500, b'')
    fields = phaxio_fields(9001)
    with client() as http:
        assert post_phaxio(http, fields).json() == {'status': 'ok'}
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['status_text'] == 'Waiting for the document from Phaxio.'
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['retry_at'] is not None
        assert fax['status_text'].startswith('The document could not be fetched; Faxbot will try again at ')
        missing = http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN)
        assert missing.status_code == 404 and missing.json()['detail'] == 'The document has not been received yet.'
        assert feed() == 0 and rows(isolated_installation, 'intake_items') == []
    document = pdf_bytes()
    providers.files[('phaxio', '9001')] = (200, document)
    with client() as http:  # a restarted installation
        assert post_phaxio(http, fields).json() == {'status': 'ok'}
        assert len(rows(isolated_installation, 'inbound_faxes')) == 1
        assert len(rows(isolated_installation, 'inbound_imports')) == 1
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['status_text'] == 'Received.'
        assert fax['sha256'] == hashlib.sha256(document).hexdigest() and fax['provider_fax_id'] == '9001'
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).content == document
        assert feed() == 1 and feed() == 0
        assert post_phaxio(http, fields).json() == {'status': 'ok'}
        assert only_fax(http)['sha256'] == fax['sha256']
        [record] = rows(isolated_installation, 'inbound_imports')
        assert record['state'] == 'received' and record['attempts'] == 2
        report = json.loads(record['report'])
        assert report['verified_by'] == 'signature' and 'file' not in report['notification']
    assert providers.hosts() == {'api.phaxio.com'}


def test_sinch_fetch_failure_resumes_after_restart_as_one_authentic_document(isolated_installation, monkeypatch,
                                                                             providers):
    environment(monkeypatch, SINCH_INBOUND_BASIC_USER='synthetic-sinch-webhook',
                SINCH_INBOUND_BASIC_PASS='synthetic-webhook-pass')
    auth = {'Authorization': 'Basic ' + base64.b64encode(b'synthetic-sinch-webhook:synthetic-webhook-pass').decode()}
    providers.sinch('01SYNTHETICFAX')
    event = {'event': 'INCOMING_FAX', 'eventTime': '2026-10-03T14:00:01Z',
             'fax': {'id': '01SYNTHETICFAX', 'direction': 'INBOUND', 'from': FROM, 'to': TO, 'numberOfPages': 1,
                     'status': 'COMPLETED', 'completedTime': '2026-10-03T14:00:00Z'}}
    with client() as http:
        assert http.post('/sinch-inbound', json=event, headers=auth).json() == {'status': 'ok'}
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['status_text'].startswith('The document could not be fetched')
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).status_code == 404
        assert feed() == 0
    document = pdf_bytes('Sinch synthetic fax')
    providers.files[('sinch', '01SYNTHETICFAX')] = (200, document)
    with client() as http:
        assert http.post('/sinch-inbound', json=event, headers=auth).json() == {'status': 'ok'}
        assert len(rows(isolated_installation, 'inbound_imports')) == 1
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['sha256'] == hashlib.sha256(document).hexdigest()
        assert fax['source_received_at'] == '2026-10-03T14:00:00'
        assert feed() == 1
        assert http.post('/sinch-inbound', json=event, headers=auth).json() == {'status': 'ok'}
        assert len(rows(isolated_installation, 'inbound_faxes')) == 1


# 2. Nothing unauthenticated is stored or fetched ---------------------------------
def test_unauthenticated_or_unknown_notifications_store_nothing(isolated_installation, monkeypatch, providers):
    environment(monkeypatch)
    providers.phaxio(9002)
    fields = phaxio_fields(9002, extra=(('file_url', 'https://attacker.example/fax.pdf'),))
    with client() as http:
        bad = post_phaxio(http, fields, signature='0' * 40)
        assert bad.status_code == 401
        assert post_phaxio(http, fields, sign=False).status_code == 401
        assert rows(isolated_installation, 'inbound_faxes') == [] and rows(isolated_installation, 'inbound_imports') == []
        assert providers.requests == []
        # A signed notification is kept, but its event-supplied address is never requested.
        providers.files[('phaxio', '9002')] = (200, pdf_bytes())
        assert post_phaxio(http, fields).json() == {'status': 'ok'}
        assert step() is True and only_fax(http)['status'] == 'received'
    assert 'attacker.example' not in providers.hosts()


def _sinch_event(fax_id, **fax):
    return {'event': 'INCOMING_FAX', 'fax': {'id': fax_id, 'direction': 'INBOUND', 'from': FROM, 'to': TO,
                                            'numberOfPages': 1, 'completedTime': '2026-10-03T14:00:00Z', **fax}}


def _basic(user, password):
    return {'Authorization': 'Basic ' + base64.b64encode(f'{user}:{password}'.encode()).decode()}


def test_a_sinch_user_name_without_a_password_authenticates_nothing(isolated_installation, monkeypatch, providers):
    """Only a user name set (from .env): a request with that name and a blank password is not authenticated."""
    environment(monkeypatch, SINCH_INBOUND_BASIC_USER='synthetic-sinch-webhook')
    forged = dict(_sinch_event('01FORGEDFAX'), file=base64.b64encode(pdf_bytes('forged document')).decode())
    with client() as http:
        # Not authenticated, so it is only a hint: Sinch does not know this fax, and nothing is kept.
        assert http.post('/sinch-inbound', json=forged, headers=_basic('synthetic-sinch-webhook', '')).json() == {
            'status': 'ignored'}
        assert rows(isolated_installation, 'inbound_imports') == [] and rows(isolated_installation, 'inbound_faxes') == []
        # A fax Sinch knows is confirmed by lookup and fetched from Sinch, never taken from the request.
        providers.sinch('01SYNTHETICFAX2')
        document = pdf_bytes('the real Sinch document')
        providers.files[('sinch', '01SYNTHETICFAX2')] = (200, document)
        known = dict(_sinch_event('01SYNTHETICFAX2'), file=base64.b64encode(pdf_bytes('forged document')).decode(),
                     padding='x' * 200_000)
        assert http.post('/sinch-inbound', json=known, headers=_basic('synthetic-sinch-webhook', '')).json() == {
            'status': 'ok'}
        assert step() is True
        assert only_fax(http)['sha256'] == hashlib.sha256(document).hexdigest()
        [record] = rows(isolated_installation, 'inbound_imports')
        report = json.loads(record['report'])
        # Only Sinch's own record is kept, not the unauthenticated body.
        assert report == {'verified_by': 'lookup', 'notification': {
            'id': '01SYNTHETICFAX2', 'to_number': TO, 'from_number': FROM, 'pages': 1,
            'completed_at': '2026-10-03T14:00:00Z'}}
        # The settings page does not call a user name alone "configured".
        assert http.get('/admin/settings', headers=ADMIN).json()['inbound']['sinch']['basic_auth_configured'] is False


def test_sinch_basic_auth_with_both_set_authenticates(isolated_installation, monkeypatch, providers):
    environment(monkeypatch, SINCH_INBOUND_BASIC_USER='synthetic-sinch-webhook',
                SINCH_INBOUND_BASIC_PASS='synthetic-webhook-pass')
    with client() as http:
        assert http.post('/sinch-inbound', json=_sinch_event('01BASICFAX'),
                         headers=_basic('synthetic-sinch-webhook', '')).status_code == 401
        assert http.post('/sinch-inbound', json=_sinch_event('01BASICFAX'),
                         headers=_basic('synthetic-sinch-webhook', 'wrong')).status_code == 401
        event = dict(_sinch_event('01BASICFAX'), note='y' * 100_000)
        assert http.post('/sinch-inbound', json=event,
                         headers=_basic('synthetic-sinch-webhook', 'synthetic-webhook-pass')).json() == {'status': 'ok'}
        [record] = rows(isolated_installation, 'inbound_imports')
        report = json.loads(record['report'])
        assert report['verified_by'] == 'basic auth' and report['notification']['fax']['id'] == '01BASICFAX'
        # Authenticated notifications are kept as evidence, cut short: long text is cut, the whole stays small.
        assert report['notification']['note'] == 'y' * 500 and len(record['report']) <= 8 * 1024


def test_saving_a_sinch_user_name_without_a_password_is_refused(isolated_installation, monkeypatch):
    from app.config_values import SINCH_PASSWORD_NEEDED, ConfigurationValueError, ConfigurationValues
    base = ConfigurationValues.from_environment({})
    with pytest.raises(ConfigurationValueError) as refused:
        base.with_patch({'sinch_inbound_basic_user': 'synthetic-sinch-webhook'})
    assert str(refused.value) == SINCH_PASSWORD_NEEDED
    both = base.with_patch({'sinch_inbound_basic_user': 'synthetic-sinch-webhook',
                            'sinch_inbound_basic_pass': 'synthetic-webhook-pass'})
    assert both.sinch_inbound_basic_configured
    # Clearing the password while the user name stays is refused too; clearing both is fine.
    with pytest.raises(ConfigurationValueError):
        both.with_patch({'sinch_inbound_basic_pass': ''})
    assert not both.with_patch({'sinch_inbound_basic_user': '', 'sinch_inbound_basic_pass': ''}).sinch_inbound_basic_configured
    # An unrelated change saves even on an installation whose .env holds only the user name.
    lone = ConfigurationValues.from_environment({'SINCH_INBOUND_BASIC_USER': 'synthetic-sinch-webhook'})
    assert lone.with_patch({'max_file_size_mb': 12}).max_file_size_mb == 12
    environment(monkeypatch)
    with client() as http:
        current = http.get('/admin/settings', headers=ADMIN).json()
        saved = http.put('/admin/settings', headers=ADMIN, json={
            'expected_revision_id': current['_meta']['desired_revision_id'],
            'sinch_inbound_basic_user': 'synthetic-sinch-webhook'})
        assert saved.status_code == 400 and saved.json()['detail'] == SINCH_PASSWORD_NEEDED


def test_with_checks_off_a_notification_is_only_a_hint_confirmed_by_lookup(isolated_installation, monkeypatch,
                                                                         providers):
    environment(monkeypatch, PHAXIO_INBOUND_VERIFY_SIGNATURE='false')
    fields = phaxio_fields(9003, extra=(('file_url', 'https://attacker.example/fax.pdf'),))
    attached = {'file': pdf_bytes('forged document')}
    with client() as http:
        unknown = post_phaxio(http, fields, files=attached.items(), sign=False)
        assert unknown.json() == {'status': 'ignored'}
        assert rows(isolated_installation, 'inbound_faxes') == [] and rows(isolated_installation, 'inbound_imports') == []
        assert providers.requests == ['https://api.phaxio.com/v2.1/faxes/9003']
        # Known to the account: stored from the provider's record, never from the attached file.
        providers.phaxio(9003)
        document = pdf_bytes('the real document')
        providers.files[('phaxio', '9003')] = (200, document)
        assert post_phaxio(http, fields, files=attached.items(), sign=False).json() == {'status': 'ok'}
        assert only_fax(http)['status'] == 'waiting'
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['sha256'] == hashlib.sha256(document).hexdigest()
        [record] = rows(isolated_installation, 'inbound_imports')
        assert json.loads(record['report'])['verified_by'] == 'lookup'
        # A notification whose numbers disagree with the provider's record is ignored.
        providers.phaxio(9004, to_number='+15555550999')
        assert post_phaxio(http, phaxio_fields(9004), sign=False).json() == {'status': 'ignored'}
    assert 'attacker.example' not in providers.hosts()


def test_sinch_without_webhook_auth_confirms_by_lookup_and_ignores_unknown_faxes(isolated_installation, monkeypatch,
                                                                                providers):
    environment(monkeypatch)
    event = {'event': 'INCOMING_FAX', 'fax': {'id': '01UNKNOWN', 'direction': 'INBOUND', 'to': TO, 'from': FROM},
             'file': base64.b64encode(pdf_bytes('forged')).decode(), 'fileType': 'PDF',
             'file_url': 'https://attacker.example/fax.pdf'}
    with client() as http:
        assert http.post('/sinch-inbound', json=event).json() == {'status': 'ignored'}
        assert rows(isolated_installation, 'inbound_faxes') == []
        providers.sinch('01UNKNOWN')
        providers.files[('sinch', '01UNKNOWN')] = (200, pdf_bytes('the real Sinch document'))
        assert http.post('/sinch-inbound', json=event).json() == {'status': 'ok'}
        assert only_fax(http)['status'] == 'waiting'
        assert step() is True and only_fax(http)['status'] == 'received'
    assert providers.hosts() == {'fax.api.sinch.com'}


def test_a_sinch_signing_secret_left_in_env_neither_refuses_nor_authenticates(isolated_installation, monkeypatch,
                                                                              providers):
    """Sinch's Fax API signs no webhooks, so SINCH_INBOUND_HMAC_SECRET is retired. A value left in .env used to
    refuse every real notification (Sinch sends no X-Sinch-Signature); it is ignored, and a header made with it
    proves nothing: the fax is confirmed with Sinch and its document fetched from Sinch."""
    import hmac as hmac_module
    secret = 'synthetic-retired-signing-secret'
    environment(monkeypatch, SINCH_INBOUND_HMAC_SECRET=secret)
    providers.sinch('01SIGNEDFAX')
    document = pdf_bytes('the real Sinch document')
    providers.files[('sinch', '01SIGNEDFAX')] = (200, document)
    event = dict(_sinch_event('01SIGNEDFAX'), file=base64.b64encode(pdf_bytes('forged document')).decode())
    raw = json.dumps(event).encode()
    signed = {'Content-Type': 'application/json',
              'X-Sinch-Signature': hmac_module.new(secret.encode(), raw, hashlib.sha256).hexdigest()}
    with client() as http:
        assert http.post('/sinch-inbound', content=raw, headers=signed).json() == {'status': 'ok'}
        assert step() is True
        assert only_fax(http)['sha256'] == hashlib.sha256(document).hexdigest()
        [record] = rows(isolated_installation, 'inbound_imports')
        assert json.loads(record['report'])['verified_by'] == 'lookup'
        assert 'hmac' not in json.dumps(http.get('/admin/settings', headers=ADMIN).json()['inbound']['sinch'])


# 3. The same provider fax ID under two accounts stays two records ----------------
def test_same_fax_id_under_two_accounts_is_two_records(isolated_installation, monkeypatch, providers):
    environment(monkeypatch)
    fields = phaxio_fields(9005)
    with client() as http:
        assert post_phaxio(http, fields).json() == {'status': 'ok'}
    # The key comes from the environment, so the second account is set there and read at the next start.
    monkeypatch.setenv('PHAXIO_API_KEY', 'synthetic-phaxio-key-two')
    with client() as http:
        assert post_phaxio(http, fields).json() == {'status': 'ok'}
        assert post_phaxio(http, fields).json() == {'status': 'ok'}
        assert len(http.get('/inbound', headers=ADMIN).json()) == 2
    accounts = {record['account'] for record in rows(isolated_installation, 'inbound_imports')}
    assert len(accounts) == 2 and all(account.startswith('phaxio:') and len(account) == 19 for account in accounts)
    assert not any(PHAXIO_KEY in account for account in accounts)


# 4. Invalid bytes are retried, then wait for a person -----------------------------
def test_invalid_bytes_retry_then_stop_and_both_fetch_again_and_replay_resume(isolated_installation, monkeypatch,
                                                                             providers):
    environment(monkeypatch)
    providers.phaxio(9006)
    providers.files[('phaxio', '9006')] = (200, b'<html>not a fax</html>')
    with client() as http:
        assert post_phaxio(http, phaxio_fields(9006)).json() == {'status': 'ok'}
        for attempt in range(len(RETRY_MINUTES)):
            assert step(later=timedelta(hours=2 * attempt)) is True
            if attempt == 0:
                assert only_fax(http)['problem'] == 'Phaxio sent something that is not a PDF.'
        fax = only_fax(http)
        assert fax['status'] == 'failed' and fax['retry_at'] is None
        assert fax['status_text'] == 'Faxbot stopped trying to fetch this document; select Fetch again.'
        assert step(later=timedelta(days=3)) is False and feed() == 0
        reader = http.post('/admin/api-keys', headers=ADMIN, json={'name': 'reader', 'scopes': ['inbound:read']})
        assert http.post(f"/inbound/{fax['id']}/fetch",
                         headers={'X-API-Key': reader.json()['token']}).status_code == 403
        again = http.post(f"/inbound/{fax['id']}/fetch", headers=ADMIN)
        assert again.status_code == 200 and again.json()['status'] == 'waiting'
        providers.files[('phaxio', '9006')] = (200, pdf_bytes())
        assert step() is True and only_fax(http)['status'] == 'received'
        received = http.post(f"/inbound/{fax['id']}/fetch", headers=ADMIN)
        assert received.status_code == 409 and received.json()['detail'] == 'This fax has already been received.'
        assert http.post('/inbound/' + '0' * 32 + '/fetch', headers=ADMIN).status_code == 404
        # A replayed notification resumes a stopped import too.
        providers.phaxio(9007)
        assert post_phaxio(http, phaxio_fields(9007)).json() == {'status': 'ok'}
        record = next(r for r in rows(isolated_installation, 'inbound_imports') if r['operation_id'] == '9007')
        service().store.abandon(record['id'], 'Synthetic stop for this test.')
        assert post_phaxio(http, phaxio_fields(9007)).json() == {'status': 'ok'}
        providers.files[('phaxio', '9007')] = (200, pdf_bytes('second'))
        assert step() is True
        assert {item['status'] for item in http.get('/inbound', headers=ADMIN).json()} == {'received'}
        assert feed() == 2


# 5. Different content under the same source identity is a conflict ----------------
def test_replay_with_different_content_records_a_conflict_and_keeps_the_original(isolated_installation, monkeypatch,
                                                                                 providers):
    environment(monkeypatch)
    first, second = pdf_bytes('first copy'), pdf_bytes('second copy')
    fields = phaxio_fields(9008)
    with client() as http:
        assert post_phaxio(http, fields, files=[('file', first)]).json() == {'status': 'ok'}
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['sha256'] == hashlib.sha256(first).hexdigest()
        assert post_phaxio(http, fields, files=[('file', second)]).json() == {'status': 'ok'}
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['sha256'] == hashlib.sha256(first).hexdigest()
        assert fax['status_text'] == 'This fax arrived earlier and is kept as received.'
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).content == first
        [record] = rows(isolated_installation, 'inbound_imports')
        assert record['state'] == 'conflict' and record['artifact_digest'] == hashlib.sha256(first).hexdigest()
        # A late fetched copy with other bytes cannot replace the stored document either.
        completion = service().store.complete(record['id'], artifact_path='/nonexistent/other.pdf',
                                              digest='c' * 64, size=3, pages=1)
        assert completion.stored is False and completion.kept_path.endswith('.pdf')
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).content == first
    assert providers.requests == []


# 6. Asterisk ---------------------------------------------------------------------
def _tiff(path, valid=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        Image.new('1', (40, 20), 1).save(path, format='TIFF')
    else:
        path.write_bytes(b'II*\x00 not a real image')
    return path


def _asterisk(http, tiff, uniqueid='1791049108.7'):
    return http.post('/_internal/asterisk/inbound', headers={'X-Internal-Secret': 'synthetic-internal'},
                     json={'tiff_path': str(tiff), 'to_number': TO, 'from_number': FROM, 'faxstatus': 'SUCCESS',
                           'faxpages': 1, 'uniqueid': uniqueid,
                           'call': {'started_at': 1791049108, 'ended_at': 1791049134}})


def test_asterisk_is_idempotent_retains_the_tiff_and_refuses_paths_outside_data(isolated_installation, monkeypatch,
                                                                               providers, tmp_path):
    environment(monkeypatch)
    data = os.path.realpath(isolated_installation['FAX_DATA_DIR'])
    with client() as http:
        good = _tiff(tmp_path / 'faxdata' / 'inbound' / 'good.tiff')
        first, again = _asterisk(http, good), _asterisk(http, good)
        assert first.status_code == again.status_code == 200 and first.json()['id'] == again.json()['id']
        assert len(rows(isolated_installation, 'inbound_imports')) == 1
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['source_received_at'] == '2026-10-03T17:38:54'
        broken = _tiff(tmp_path / 'faxdata' / 'inbound' / 'broken.tiff', valid=False)
        waiting = _asterisk(http, broken, uniqueid='1791049200.8')
        assert waiting.status_code == 200
        item = http.get(f"/inbound/{waiting.json()['id']}", headers=ADMIN).json()
        assert item['status'] == 'waiting' and item['problem'] == 'The received fax image could not be turned into a PDF.'
        assert broken.exists() and feed() == 1
        _tiff(broken)  # the image is readable now, as after a disk or converter fix
        assert step(later=timedelta(minutes=2)) is True
        assert http.get(f"/inbound/{waiting.json()['id']}", headers=ADMIN).json()['status'] == 'received'
        before = len(rows(isolated_installation, 'inbound_imports'))
        outside = _tiff(tmp_path / 'elsewhere' / 'in.tiff')
        assert _asterisk(http, outside, uniqueid='1791049300.9').status_code == 400
        link = tmp_path / 'faxdata' / 'inbound' / 'link.tiff'
        link.symlink_to(outside)
        assert _asterisk(http, link, uniqueid='1791049300.10').status_code == 400
        escape = os.path.join(data, 'inbound', '..', '..', 'elsewhere', 'in.tiff')
        assert _asterisk(http, escape, uniqueid='1791049300.11').status_code == 400
        assert len(rows(isolated_installation, 'inbound_imports')) == before


# 7. Source time is kept apart from import time -----------------------------------
def test_source_time_is_stored_only_when_reported(isolated_installation, monkeypatch, providers):
    environment(monkeypatch)
    with client() as http:
        before = datetime.utcnow().replace(microsecond=0)
        assert post_phaxio(http, phaxio_fields(9010), files=[('file', pdf_bytes())]).json() == {'status': 'ok'}
        assert post_phaxio(http, phaxio_fields(9011, completed_at=None),
                           files=[('file', pdf_bytes('other'))]).json() == {'status': 'ok'}
        faxes = {item['provider_fax_id']: item for item in http.get('/inbound', headers=ADMIN).json()}
    assert faxes['9010']['source_received_at'] == '2026-10-03T14:00:00'
    assert faxes['9011']['source_received_at'] is None
    for item in faxes.values():
        assert datetime.fromisoformat(item['received_at']) >= before
    records = {record['operation_id']: record for record in rows(isolated_installation, 'inbound_imports')}
    assert records['9011']['source_received_at'] is None and records['9011']['imported_at'] is not None


# 8. A test fax is a real one-page document, marked as a test ---------------------
def test_simulated_fax_is_a_real_one_page_pdf_marked_as_a_test(isolated_installation, monkeypatch, providers):
    environment(monkeypatch)
    with client() as http:
        created = http.post('/admin/inbound/simulate', headers=ADMIN, json={'fr': FROM, 'to': TO})
        assert created.status_code == 200, created.text
        fax = only_fax(http)
        assert fax['id'] == created.json()['id'] and fax['is_test'] is True and fax['status'] == 'received'
        assert fax['status_text'] == 'A test fax created in Faxbot.' and fax['provider_fax_id'] is None
        document = http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).content
        reader = PdfReader(io.BytesIO(document))
        assert len(reader.pages) == 1 and 'Test fax created in Faxbot on' in reader.pages[0].extract_text()
        assert feed() == 1
    assert providers.requests == []


# The import store on migrated SQLite and PostgreSQL -------------------------------
def test_import_store_resumes_leases_retries_and_conflicts_on_both_databases(database):
    from api.app.inbound.acquisition import ImportStore as Store
    from api.tests.test_inbound_access import InboundWorld
    world = InboundWorld(database)
    moment = [datetime(2026, 10, 3, 12)]
    store = Store(world.inbound, clock=lambda: moment[0])
    begun = store.begin(source='phaxio', account='phaxio:0123456789ab', operation_id='9100', backend='phaxio',
                        to_number='+1 (555) 010-0001', from_number=FROM, report={'file': 'bytes', 'id': '9100'},
                        source_received_at=datetime(2026, 10, 3, 11, 59))
    assert begun.created and begun.state == 'pending'
    assert world.resource_of(begun.inbound_fax_id)['parent_id'] == 'mailbox-front'
    other = store.begin(source='phaxio', account='phaxio:ba9876543210', operation_id='9100', backend='phaxio')
    assert other.created and other.inbound_fax_id != begun.inbound_fax_id
    again = store.begin(source='phaxio', account='phaxio:0123456789ab', operation_id='9100', backend='phaxio')
    assert (again.import_id, again.created) == (begun.import_id, False)
    record = store.get(begun.import_id)
    assert json.loads(record['report']) == {'id': '9100'} and record['to_number'] == '+1 (555) 010-0001'
    claim = store.claim()
    assert claim['id'] in (begun.import_id, other.import_id) and claim['attempts'] == 1
    assert store.fail(claim['id'], 'Synthetic failure.', claim_token='stale') == 'pending'
    assert store.get(claim['id'])['claim_token'] == claim['claim_token']
    assert store.fail(claim['id'], 'Synthetic failure.', claim_token=claim['claim_token']) == 'pending'
    assert store.get(claim['id'])['next_attempt_at'] == moment[0] + timedelta(minutes=RETRY_MINUTES[0])
    second = store.claim()
    assert second is not None and second['id'] != claim['id']
    moment[0] += LEASE + timedelta(seconds=1)
    assert store.recover_expired() == 1 and store.get(second['id'])['claim_token'] is None
    digest = 'a' * 64
    done = store.complete(begun.import_id, artifact_path='/synthetic/one.pdf', digest=digest, size=10, pages=1,
                          source_received_at=datetime(2026, 10, 3, 11, 0))
    assert done.stored and store.get(begun.import_id)['source_received_at'] == datetime(2026, 10, 3, 11, 59)
    replay = store.begin(source='phaxio', account='phaxio:0123456789ab', operation_id='9100', backend='phaxio',
                         artifact_digest='b' * 64)
    assert replay.conflict and store.get(begun.import_id)['state'] == 'conflict'
    with database.connect() as connection:
        fax = connection.execute(sa.text('SELECT status, sha256, pdf_path FROM inbound_faxes WHERE id = :id'),
                                 {'id': begun.inbound_fax_id}).one()
    assert tuple(fax) == ('received', digest, '/synthetic/one.pdf')
    assert store.abandon(other.import_id, 'Synthetic stop.') == 'failed'
    assert store.resume_for_fax(other.inbound_fax_id) == other.import_id
    assert store.get(other.import_id)['attempts'] == 0 and store.get(other.import_id)['state'] == 'pending'
