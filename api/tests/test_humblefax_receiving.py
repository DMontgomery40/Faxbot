"""Receiving from HumbleFax: Faxbot asks HumbleFax for received faxes and stores each one once.

HumbleFax's API is a synthetic ``httpx.MockTransport`` that speaks the shapes
published at https://api.humblefax.com/ (read 2026-10-07): GetUser
(GET /user), GetIncomingFaxes (GET /incomingFaxes), GetIncomingFax
(GET /incomingFax/{id}) and DownloadIncomingFax (GET /incomingFax/{id}/download).
The background tasks are off; checks and fetches run as single steps so each
state is asserted exactly. No test reaches a real HumbleFax account.
"""
import asyncio
import base64
from datetime import datetime, timedelta
import hashlib
import json
import sys

import httpx
import pytest
import sqlalchemy as sa

from app import main
from app.inbound import http as inbound_http
from app.inbound.acquisition import LEASE, utcnow
from api.tests.test_inbound_acquisition import ADMIN, BOOTSTRAP, client, only_fax, pdf_bytes, rows, step
from api.tests.test_schema import database  # noqa: F401 (fixture)


ACCESS, SECRET = 'synthetic-hf-access', 'synthetic-hf-secret'
OTHER_ACCESS, OTHER_SECRET = 'synthetic-hf-other-access', 'synthetic-hf-other-secret'
USER_ID, OTHER_USER_ID = 4242, 5151
NUMBER = '13035550123'


def service_module():
    return sys.modules['app.humblefax_service']


def receiving_module():
    return sys.modules['app.inbound.humblefax']


def epoch(moment):
    return int((moment - datetime(1970, 1, 1)).total_seconds())


class FakeHumbleFax:
    """The HumbleFax API host; holds received faxes per user and records every request."""

    def __init__(self):
        self.requests = []
        self.faxes = {}
        self.owners = {}
        self.documents = {}
        self.fail = {}
        self.users = {(ACCESS, SECRET): {'id': USER_ID, 'inboundAccess': True},
                      (OTHER_ACCESS, OTHER_SECRET): {'id': OTHER_USER_ID, 'inboundAccess': True}}
        self.details = True
        self.clock = None

    def receive(self, fax_id, document=None, *, when=None, owner=USER_ID, **changes):
        moment = when or utcnow() - timedelta(minutes=5)
        self.owners[str(fax_id)] = owner
        self.faxes[str(fax_id)] = {'id': int(fax_id), 'status': 'success', 'time': str(epoch(moment)),
                                   'toNumber': NUMBER, 'fromNameAddressBook': 'Synthetic Address Book Name',
                                   'fromNameIdentity': 'Synthetic Clinic', 'fromNumber': '18005551212',
                                   'numPages': 1, 'transmissionTime': 40, 'bitRate': '14400', **changes}
        self.documents[str(fax_id)] = document if document is not None else pdf_bytes(f'HumbleFax fax {fax_id}')

    def handler(self, request):
        self.requests.append({'method': request.method, 'url': str(request.url), 'host': request.url.host,
                              'path': request.url.path, 'params': dict(request.url.params),
                              'headers': {k.lower(): v for k, v in request.headers.items()},
                              'at': self.clock() if self.clock else None})
        header = request.headers.get('authorization', '')
        try:
            access, _, secret = base64.b64decode(header.split(' ', 1)[1]).decode().partition(':')
        except (IndexError, ValueError):
            access, secret = '', ''
        user = self.users.get((access, secret))
        if user is None:
            return httpx.Response(401, json={'result': 'failure', 'error': 'loginFailure'})
        failure = self.fail.get((request.method, request.url.path))
        if failure is not None:
            return failure(request) if callable(failure) else failure
        path = request.url.path
        if request.method != 'GET':
            return httpx.Response(405, json={})
        if path == '/user':
            return httpx.Response(200, json={'data': {'user': {**user, 'name': 'Synthetic User',
                                                               'assignedFaxNumber': NUMBER}}})
        if path == '/incomingFaxes':
            start = int(request.url.params.get('timeFrom', epoch(utcnow() - timedelta(days=30))))
            end = int(request.url.params.get('timeTo', epoch(utcnow())))
            listed = [fax for key, fax in self.faxes.items()
                      if start <= int(fax['time']) <= end and self.owners[key] == user['id']]
            data = {'numResults': len(listed), 'incomingFaxIds': [str(fax['id']) for fax in listed]}
            if self.details:
                data['incomingFaxes'] = listed
            return httpx.Response(200, json={'data': data})
        parts = path.strip('/').split('/')
        fax = self.faxes.get(parts[1]) if len(parts) > 1 and parts[0] == 'incomingFax' else None
        if fax is None or self.owners[parts[1]] != user['id']:
            return httpx.Response(404, json={'result': 'failure'})
        if parts[2:] == []:
            return httpx.Response(200, json={'data': {'incomingFax': fax}})
        if parts[2:] == ['download']:
            return httpx.Response(200, content=self.documents[parts[1]], headers={'content-type': 'application/pdf'})
        return httpx.Response(404, json={})

    def calls(self):
        return [(request['method'], request['path']) for request in self.requests]


@pytest.fixture
def humblefax(monkeypatch):
    fake = FakeHumbleFax()
    service = service_module() if 'app.humblefax_service' in sys.modules else \
        __import__('app.humblefax_service').humblefax_service
    __import__('app.inbound.humblefax')
    monkeypatch.setattr(service, 'RECEIVE_TRANSPORT', httpx.MockTransport(fake.handler))
    monkeypatch.setattr(service, 'RECEIVE_PACE', service.Pace(0))
    monkeypatch.setattr(inbound_http, 'AUTOMATIC', False)
    monkeypatch.setattr(main, '_rate_buckets', {})
    receiving_module().forget_accounts()
    yield fake
    receiving_module().forget_accounts()


def environment(monkeypatch, **extra):
    values = {'INBOUND_ENABLED': 'true', 'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP,
              'PUBLIC_API_URL': 'https://testserver', 'MAX_REQUESTS_PER_MINUTE': '0', 'FAX_BACKEND': 'humblefax',
              'HUMBLEFAX_ACCESS_KEY': ACCESS, 'HUMBLEFAX_SECRET_KEY': SECRET,
              'INBOUND_LIST_RPM': '0', 'INBOUND_GET_RPM': '0', 'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', **extra}
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def values():
    return main.app.state.configuration_runtime.manager.store.read().active.values


def store():
    return main.app.state.inbound_acquisition.store


def check(**kwargs):
    return asyncio.run(receiving_module().check_once(store(), values(), **kwargs))


def receiver():
    return main.app.state.inbound_acquisition.humblefax


def status(http):
    response = http.get('/admin/inbound/humblefax', headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


# 1. A received fax is listed, recorded once, downloaded by ID and stored -----------
def test_a_received_fax_is_recorded_once_and_its_document_fetched_by_id(isolated_installation, monkeypatch,
                                                                         humblefax):
    environment(monkeypatch)
    document = pdf_bytes('HumbleFax synthetic fax')
    received = utcnow().replace(microsecond=0) - timedelta(minutes=3)
    humblefax.receive(700001, document, when=received)
    with client() as http:
        result = check()
        assert (result.listed, result.added) == (1, 1)
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['status_text'] == 'Waiting for the document from HumbleFax.'
        assert fax['backend'] == 'humblefax' and fax['provider_fax_id'] == '700001'
        assert (fax['fr'], fax['to']) == ('+18005551212', '+13035550123')
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['status_text'] == 'Received.'
        assert fax['sha256'] == hashlib.sha256(document).hexdigest() and fax['provider_note'] is None
        assert fax['source_received_at'] == received.isoformat()
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).content == document
        # Listing again finds the fax but records and fetches nothing new.
        again = check()
        assert (again.listed, again.added) == (1, 0)
        assert len(rows(isolated_installation, 'inbound_imports')) == 1
        assert len(rows(isolated_installation, 'inbound_faxes')) == 1
    assert humblefax.calls() == [('GET', '/user'), ('GET', '/incomingFaxes'),
                                 ('GET', '/incomingFax/700001/download'), ('GET', '/incomingFaxes')]
    download = next(request for request in humblefax.requests if request['path'].endswith('/download'))
    assert download['params'] == {'fileFormat': 'pdf'}
    assert download['headers']['authorization'] == 'Basic ' + base64.b64encode(
        (ACCESS + ':' + SECRET).encode()).decode()
    assert {request['host'] for request in humblefax.requests} == {'api.humblefax.com'}
    # Faxbot never deletes, trashes or changes anything at HumbleFax.
    assert {request['method'] for request in humblefax.requests} == {'GET'}
    [record] = rows(isolated_installation, 'inbound_imports')
    assert (record['source'], record['account'], record['operation_id'], record['state']) == (
        'humblefax', f'humblefax:{USER_ID}', '700001', 'received')
    report = json.loads(record['report'])
    assert report['listed_by'] == 'humblefax' and report['fax']['sender_station'] == 'Synthetic Clinic'
    assert report['account_key'] == 'humblefax'
    assert rows(isolated_installation, 'inbound_faxes')[0]['inbound_backend'] == 'humblefax'
    assert 'Synthetic Address Book Name' not in record['report']
    for secret in (ACCESS, SECRET):
        assert secret not in json.dumps(record, default=str)


def test_the_first_check_lists_thirty_days_and_later_ones_from_a_day_before_the_newest(
        isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    newest = utcnow().replace(microsecond=0) - timedelta(days=3)
    humblefax.receive(700010, when=utcnow() - timedelta(days=40))  # older than HumbleFax's window
    humblefax.receive(700011, when=newest - timedelta(hours=30))
    humblefax.receive(700012, when=newest)
    with client():
        before = utcnow()
        assert check().added == 2
        first = int([r for r in humblefax.requests if r['path'] == '/incomingFaxes'][0]['params']['timeFrom'])
        assert epoch(before - timedelta(days=30)) - 2 <= first <= epoch(utcnow() - timedelta(days=30)) + 2
        assert check().added == 0
        second = int([r for r in humblefax.requests if r['path'] == '/incomingFaxes'][1]['params']['timeFrom'])
        assert second == epoch(newest - timedelta(days=1))
        # A fax HumbleFax lists late, inside the overlap, is still recorded once.
        humblefax.receive(700013, when=newest - timedelta(hours=2))
        assert check().added == 1
        assert sorted(row['operation_id'] for row in rows(isolated_installation, 'inbound_imports')) == [
            '700011', '700012', '700013']
    assert all('timeTo' not in request['params'] for request in humblefax.requests)


def test_a_listing_without_details_reads_only_the_new_faxes(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.receive(700020)
    with client():
        assert check().added == 1
        humblefax.details = False
        humblefax.receive(700021)
        assert check().added == 1
        assert humblefax.calls()[-2:] == [('GET', '/incomingFaxes'), ('GET', '/incomingFax/700021')]
        [new] = rows(isolated_installation, 'inbound_imports', "WHERE operation_id = '700021'")
        assert new['source_received_at'] is not None and new['reported_pages'] == 1


# 2. Listing a fax again never resumes, reschedules or duplicates it ---------------
def test_listing_a_known_fax_again_never_resumes_or_reschedules_it(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.receive(700030)
    with client() as http:
        assert check().added == 1
        humblefax.fail[('GET', '/incomingFax/700030/download')] = httpx.Response(503, json={})
        assert step() is True
        [before] = rows(isolated_installation, 'inbound_imports')
        assert before['state'] == 'pending' and before['next_attempt_at'] is not None
        assert check().added == 0
        [after] = rows(isolated_installation, 'inbound_imports')
        assert (after['next_attempt_at'], after['attempts'], after['updated_at']) == (
            before['next_attempt_at'], 1, before['updated_at'])
        # Stopped for a person: listing it again leaves it stopped and adds no history.
        assert store().abandon(before['id'], 'HumbleFax did not send the document (HTTP 503).') == 'failed'
        [stopped] = rows(isolated_installation, 'inbound_imports')
        assert check().added == 0
        assert rows(isolated_installation, 'inbound_imports') == [stopped]
        assert rows(isolated_installation, 'inbound_import_failures') == []
        assert only_fax(http)['status'] == 'failed'


# 3. Download failures retry on the schedule and keep their history -----------------
def test_download_failures_retry_and_keep_their_history(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.receive(700040)
    with client() as http:
        check()
        humblefax.fail[('GET', '/incomingFax/700040/download')] = httpx.Response(500, json={})
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['problem'] == 'HumbleFax did not send the document (HTTP 500).'
        assert fax['status_text'].startswith('The document could not be fetched; Faxbot will try again at ')
        assert fax['can_fetch_again'] is True
        assert step() is False  # not due yet
        assert step(later=timedelta(minutes=2)) is True  # due, refused again
        assert rows(isolated_installation, 'inbound_imports')[0]['attempts'] == 2
        [record] = rows(isolated_installation, 'inbound_imports')
        assert store().abandon(record['id'], record['last_error']) == 'failed'
        response = http.post(f"/inbound/{fax['id']}/fetch", headers=ADMIN)
        assert response.status_code == 200, response.text
        del humblefax.fail[('GET', '/incomingFax/700040/download')]
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received'
        [failure] = fax['earlier_failures']
        assert failure['attempts'] == 2 and failure['problem'] == 'HumbleFax did not send the document (HTTP 500).'
        assert fax['earlier_failures_text'].startswith('Failed once before ')
    assert humblefax.calls().count(('GET', '/incomingFax/700040/download')) == 3


@pytest.mark.parametrize(('reply', 'problem'), [
    (httpx.Response(200, content=b'not a pdf'), 'HumbleFax sent something that is not a PDF.'),
    (httpx.Response(200, content=b''), 'HumbleFax sent an empty document.'),
    (httpx.Response(404, json={}), 'HumbleFax has no received fax with this ID for the keys in settings.'),
    (httpx.Response(302, headers={'location': 'https://elsewhere.invalid/fax.pdf'}),
     'HumbleFax sent the document from another address, which Faxbot does not follow.'),
    ('larger', 'The document from HumbleFax is larger than Faxbot accepts.'),
])
def test_an_unusable_document_is_never_stored_and_says_why(isolated_installation, monkeypatch, humblefax, reply,
                                                            problem):
    environment(monkeypatch)
    humblefax.receive(700050)
    if reply == 'larger':
        monkeypatch.setattr(service_module(), 'MAX_DOCUMENT_BYTES', 64)
        reply = httpx.Response(200, content=pdf_bytes('Longer than the limit'))
    with client() as http:
        check()
        humblefax.fail[('GET', '/incomingFax/700050/download')] = reply
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['problem'] == problem and fax['sha256'] is None
    assert all(request['host'] == 'api.humblefax.com' for request in humblefax.requests)


def test_a_redirect_to_humblefax_itself_is_followed(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    document = pdf_bytes('Redirected')
    humblefax.receive(700055, document)
    humblefax.fail[('GET', '/incomingFax/700055/download')] = httpx.Response(
        302, headers={'location': '/files/700055.pdf'})
    humblefax.fail[('GET', '/files/700055.pdf')] = httpx.Response(200, content=document)
    with client() as http:
        check()
        assert step() is True
        assert only_fax(http)['sha256'] == hashlib.sha256(document).hexdigest()
    assert humblefax.calls()[-2:] == [('GET', '/incomingFax/700055/download'), ('GET', '/files/700055.pdf')]


def test_a_partial_fax_is_kept_and_says_so(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.receive(700060, status='partial fax received', numPages=3)
    with client() as http:
        check()
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received'
        assert fax['provider_note'] == ('HumbleFax received only part of this fax; ask the sender to send it '
                                        'again if pages are missing.')


# 4. A restart in the middle of a fax never records it twice -----------------------
def test_a_restart_in_the_middle_of_a_fax_does_not_record_it_twice(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.receive(700070)
    humblefax.receive(700071)
    module = receiving_module()
    with client():
        original = store().begin
        calls = []

        def crash_on_second(**kwargs):
            calls.append(kwargs['operation_id'])
            if len(calls) == 2:
                raise RuntimeError('synthetic crash between listing and recording')
            return original(**kwargs)
        monkeypatch.setattr(store(), 'begin', crash_on_second)
        with pytest.raises(RuntimeError):
            check()
        monkeypatch.setattr(store(), 'begin', original)
        assert len(rows(isolated_installation, 'inbound_imports')) == 1
        # The fetch of the first one stops halfway: leased, never completed.
        claim = store().claim()
        assert claim is not None and claim['operation_id'] in ('700070', '700071')
    module.forget_accounts()
    with client() as http:  # Faxbot restarts
        assert check().added == 1  # only the fax the crash left out
        assert step(later=LEASE + timedelta(seconds=1)) is True
        assert step(later=LEASE + timedelta(seconds=2)) is True
        faxes = http.get('/inbound', headers=ADMIN).json()
        assert sorted(fax['provider_fax_id'] for fax in faxes) == ['700070', '700071']
        assert {fax['status'] for fax in faxes} == {'received'}
        assert check().added == 0
    assert len(rows(isolated_installation, 'inbound_imports')) == 2
    assert len(rows(isolated_installation, 'inbound_faxes')) == 2


# 5. When Faxbot asks HumbleFax at all ---------------------------------------------
@pytest.mark.parametrize(('extra', 'reason'), [
    ({'INBOUND_ENABLED': 'false'}, 'Receiving faxes is turned off in Settings, so Faxbot is not checking HumbleFax.'),
    ({'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_BACKEND': 'humblefax', 'PHAXIO_API_KEY': 'synthetic',
      'PHAXIO_API_SECRET': 'synthetic'}, 'Receive faxes from HumbleFax is off.'),
    ({'HUMBLEFAX_SECRET_KEY': ''}, 'Add the HumbleFax access key and secret key so Faxbot can check HumbleFax.'),
])
def test_with_receiving_off_nothing_is_asked_of_humblefax(isolated_installation, monkeypatch, humblefax, extra,
                                                          reason):
    environment(monkeypatch, **extra)
    humblefax.receive(700080)
    with client() as http:
        assert asyncio.run(receiver().step()) == 60
        shown = status(http)
        assert shown['receiving'] is False and shown['reason'] == reason and shown['checked_at'] is None
        assert shown['turned_on'] is (reason != 'Receive faxes from HumbleFax is off.')
        refused = http.post('/admin/inbound/humblefax/check', headers=ADMIN)
        assert refused.status_code == 409 and refused.json()['detail'] == reason
    assert humblefax.requests == []


def test_the_switch_receives_beside_another_receiving_provider(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch, FAX_BACKEND='phaxio', FAX_OUTBOUND_BACKEND='humblefax', PHAXIO_API_KEY='synthetic',
                PHAXIO_API_SECRET='synthetic', HUMBLEFAX_RECEIVE_ENABLED='true', HUMBLEFAX_POLL_SECONDS='120')
    humblefax.receive(700085)
    with client() as http:
        assert values().effective_inbound == 'phaxio'
        assert asyncio.run(receiver().step()) == 120
        shown = status(http)
        assert shown['receiving'] is True and shown['receiving_provider'] is False and shown['found'] == 1
        assert shown['account'] == 'humblefax' and shown['turned_on'] is True
        assert shown['poll_seconds'] == 120 and shown['checked_at'] is not None and shown['problem'] is None
        settings = http.get('/admin/settings', headers=ADMIN).json()['humblefax']
        assert (settings['receive'], settings['poll_seconds']) == (True, 120)
    assert len(rows(isolated_installation, 'inbound_imports')) == 1


def test_check_now_checks_at_once_and_says_what_it_found(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.receive(700090)
    humblefax.receive(700091)
    with client() as http:
        response = http.post('/admin/inbound/humblefax/check', headers=ADMIN)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['receiving'] is True and result['receiving_provider'] is True and result['found'] == 2
        assert result['checked_at'] is not None and result['problem'] is None
        assert http.post('/admin/inbound/humblefax/check', headers=ADMIN).json()['found'] == 0
        assert status(http)['found'] == 0
        assert http.post('/admin/inbound/humblefax/check').status_code == 401


def test_the_receiver_waits_longer_after_problems_and_a_minute_when_humblefax_blocks(
        isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch, HUMBLEFAX_POLL_SECONDS='60')
    with client() as http:
        loop = receiver()
        assert asyncio.run(loop.step()) == 60 and loop.last_checked is not None
        humblefax.fail[('GET', '/incomingFaxes')] = httpx.Response(500, json={})
        assert asyncio.run(loop.step()) == 120
        assert asyncio.run(loop.step()) == 240
        assert loop.last_problem == 'HumbleFax did not list received faxes.'
        assert status(http)['problem'] == 'HumbleFax did not list received faxes.'
        humblefax.fail[('GET', '/incomingFaxes')] = httpx.Response(429, json={})
        assert asyncio.run(loop.step()) == 480
        asked = len(humblefax.requests)
        assert 470 < asyncio.run(loop.step()) <= 480  # held: nothing is asked
        assert len(humblefax.requests) == asked
        refused = http.post('/admin/inbound/humblefax/check', headers=ADMIN)
        assert refused.status_code == 429 and len(humblefax.requests) == asked
        loop.hold_until = 0.0
        loop.failures = 0
        humblefax.fail[('GET', '/incomingFaxes')] = httpx.Response(429, headers={'Retry-After': '5'}, json={})
        assert asyncio.run(loop.step()) == 120 and loop.held() > 100
        loop.hold_until = 0.0
        del humblefax.fail[('GET', '/incomingFaxes')]
        assert asyncio.run(loop.step()) == 60 and loop.failures == 0 and loop.last_problem is None


@pytest.mark.parametrize(('change', 'problem'), [
    ({'HUMBLEFAX_SECRET_KEY': 'synthetic-wrong-secret'}, 'HumbleFax rejected the account access key or secret key.'),
    ('no inbound access', 'The HumbleFax user these keys belong to cannot see received faxes; '
                          'allow it in your HumbleFax account.'),
])
def test_keys_that_cannot_receive_say_so(isolated_installation, monkeypatch, humblefax, change, problem):
    if change == 'no inbound access':
        environment(monkeypatch)
        humblefax.users[(ACCESS, SECRET)]['inboundAccess'] = False
    else:
        environment(monkeypatch, **change)
    humblefax.receive(700095)
    with client() as http:
        assert asyncio.run(receiver().step()) == 120
        assert status(http)['problem'] == problem
    assert humblefax.calls() == [('GET', '/user')]


def test_new_keys_for_another_user_never_fetch_an_earlier_fax(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.receive(700100)
    with client():
        check()
    monkeypatch.setenv('HUMBLEFAX_ACCESS_KEY', OTHER_ACCESS)
    monkeypatch.setenv('HUMBLEFAX_SECRET_KEY', OTHER_SECRET)
    with client() as http:
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['problem'] == (
            'The HumbleFax keys in settings belong to a different HumbleFax user than the one this fax arrived '
            'for, so Faxbot cannot fetch it.')
    assert ('GET', '/incomingFax/700100/download') not in humblefax.calls()


def test_new_keys_for_the_same_user_keep_the_same_account(isolated_installation, monkeypatch, humblefax):
    environment(monkeypatch)
    humblefax.users[('synthetic-hf-rotated', 'synthetic-hf-rotated-secret')] = {'id': USER_ID,
                                                                                 'inboundAccess': True}
    humblefax.receive(700105)
    with client():
        check()
    monkeypatch.setenv('HUMBLEFAX_ACCESS_KEY', 'synthetic-hf-rotated')
    monkeypatch.setenv('HUMBLEFAX_SECRET_KEY', 'synthetic-hf-rotated-secret')
    with client() as http:
        assert check().added == 0
        assert step() is True and only_fax(http)['status'] == 'received'


def test_each_account_is_checked_and_fetched_with_its_own_keys_and_recorded_with_its_key(
        isolated_installation, monkeypatch, humblefax):
    module = receiving_module()
    environment(monkeypatch)
    humblefax.receive(700110)
    humblefax.receive(700111, owner=OTHER_USER_ID)
    second = module.HumbleFaxAccount('humblefax-2', OTHER_ACCESS, OTHER_SECRET, receives=True, poll_seconds=90)
    monkeypatch.setattr(module, 'accounts', lambda values: (module.settings_account(values), second))
    with client() as http:
        first = module.HumbleFaxReceiver(store(), lambda: (values(), {}))
        other = module.HumbleFaxReceiver(store(), lambda: (values(), {}), account_key='humblefax-2')
        assert asyncio.run(first.step()) == 60 and asyncio.run(other.step()) == 90
        assert (first.last_found, other.last_found) == (1, 1)
        assert step() is True and step() is True
        faxes = {fax['provider_fax_id']: fax for fax in http.get('/inbound', headers=ADMIN).json()}
        assert {fax_id: fax['status'] for fax_id, fax in faxes.items()} == {'700110': 'received',
                                                                           '700111': 'received'}
        assert other.status(values())['account'] == 'humblefax-2' and other.status(values())['poll_seconds'] == 90
    records = {row['operation_id']: row for row in rows(isolated_installation, 'inbound_imports')}
    assert records['700111']['account'] == f'humblefax:{OTHER_USER_ID}'
    assert json.loads(records['700111']['report'])['account_key'] == 'humblefax-2'
    backends = {row['provider_sid']: row['inbound_backend'] for row in rows(isolated_installation, 'inbound_faxes')}
    assert backends == {'700110': 'humblefax', '700111': 'humblefax-2'}
    other_basic = 'Basic ' + base64.b64encode((OTHER_ACCESS + ':' + OTHER_SECRET).encode()).decode()
    download = next(request for request in humblefax.requests if request['path'] == '/incomingFax/700111/download')
    assert download['headers']['authorization'] == other_basic


def test_a_fax_whose_account_left_settings_waits_for_a_person(isolated_installation, monkeypatch, humblefax):
    module = receiving_module()
    environment(monkeypatch)
    humblefax.receive(700115, owner=OTHER_USER_ID)
    second = module.HumbleFaxAccount('humblefax-2', OTHER_ACCESS, OTHER_SECRET, receives=True)
    monkeypatch.setattr(module, 'accounts', lambda values: (module.settings_account(values), second))
    with client() as http:
        assert asyncio.run(module.check_once(store(), values(), second)).added == 1
        monkeypatch.setattr(module, 'accounts', lambda values: (module.settings_account(values),))
        assert step() is True
        assert only_fax(http)['problem'] == ('The HumbleFax account this fax arrived on is no longer in settings, '
                                             'so Faxbot cannot fetch it.')
    assert ('GET', '/incomingFax/700115/download') not in humblefax.calls()


# 6. Pacing: never near HumbleFax's five requests a second -------------------------
def test_a_backlog_of_ten_faxes_is_fetched_at_most_two_requests_a_second(isolated_installation, monkeypatch,
                                                                          humblefax):
    service = service_module()
    moment = [1000.0]

    async def advance(seconds):
        moment[0] += seconds
    monkeypatch.setattr(service, 'RECEIVE_PACE', service.Pace(service.RECEIVE_GAP, clock=lambda: moment[0],
                                                              sleep=advance))
    humblefax.clock = lambda: moment[0]
    environment(monkeypatch)
    for number in range(10):
        humblefax.receive(700200 + number)
    with client():
        assert check().added == 10
        for _ in range(10):
            assert step() is True
        assert {row['state'] for row in rows(isolated_installation, 'inbound_imports')} == {'received'}
    times = [request['at'] for request in humblefax.requests]
    assert len(times) == 12
    assert all(later - earlier >= service.RECEIVE_GAP for earlier, later in zip(times, times[1:]))
    assert max(sum(1 for at in times if start <= at < start + 1) for start in times) <= 2


# 7. Sending through the same account still works ---------------------------------
def test_the_same_account_sends_and_receives(tmp_path, monkeypatch, humblefax):
    from app.config_activation import compile_profiles
    from app.config_bootstrap import default_plugin_state
    from app.config_paths import provider_traits_path
    from app.config_profiles import ProviderProfile
    from app.config_values import ConfigurationValues
    from app.provider_catalog import ProviderCatalog
    from app.provider_execution import service_from_profile
    configured = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'humblefax', 'INBOUND_ENABLED': 'true', 'HUMBLEFAX_ACCESS_KEY': ACCESS,
        'HUMBLEFAX_SECRET_KEY': SECRET, 'HUMBLEFAX_RECEIVE_ENABLED': 'true'})
    catalog = ProviderCatalog.load(provider_traits_path(), tmp_path / 'providers')
    profiles = compile_profiles(configured, catalog, default_plugin_state(configured))
    assert profiles['outbound'].provider_id == profiles['inbound'].provider_id == 'humblefax'
    sent = []

    def quick_send(request):
        sent.append((request.method, request.url.path))
        return httpx.Response(200, json={'data': {'result': 'Fax sent to queue',
                                                  'fax': {'id': 99001, 'status': 'in progress'}}})
    adapter = service_from_profile(ProviderProfile('profile', 'account', profiles['outbound']))
    document = tmp_path / 'document.pdf'
    document.write_bytes(pdf_bytes('Outgoing'))
    sending = type(adapter)(adapter.access_key, adapter.secret_key, transport=httpx.MockTransport(quick_send))
    receipt = asyncio.run(sending.send_fax_file('+12025550123', str(document)))
    assert receipt == {'provider_sid': '99001', 'status': 'in_progress'} and sent == [('POST', '/quickSendFax')]
    assert humblefax.requests == []


# 8. The store on migrated SQLite and PostgreSQL -----------------------------------
def test_recording_and_fetching_humblefax_faxes_on_both_databases(database, monkeypatch, humblefax, tmp_path):
    from api.app.config import use_configuration
    from api.app.config_values import ConfigurationValues
    from api.app.inbound import humblefax as receiving
    from api.app.inbound.acquisition import ImportStore
    from api.tests.test_inbound_access import InboundWorld
    service = sys.modules['api.app.humblefax_service']
    monkeypatch.setattr(service, 'RECEIVE_TRANSPORT', httpx.MockTransport(humblefax.handler))
    monkeypatch.setattr(service, 'RECEIVE_PACE', service.Pace(0))
    receiving.forget_accounts()
    configured = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'humblefax', 'INBOUND_ENABLED': 'true', 'HUMBLEFAX_ACCESS_KEY': ACCESS,
        'HUMBLEFAX_SECRET_KEY': SECRET, 'FAX_DATA_DIR': str(tmp_path / 'faxdata')})
    world = InboundWorld(database)
    moment = [utcnow().replace(microsecond=0)]
    humblefax.receive(700300, when=moment[0] - timedelta(hours=1))
    humblefax.receive(700301, when=moment[0] - timedelta(minutes=30))

    def run(coroutine):
        with use_configuration(configured, {}):
            return asyncio.run(coroutine)
    first = ImportStore(world.inbound, clock=lambda: moment[0])
    assert run(receiving.check_once(first, configured)).added == 2
    # A restarted Faxbot is a new store over the same rows: nothing is recorded twice.
    second = ImportStore(world.inbound, clock=lambda: moment[0])
    assert run(receiving.check_once(second, configured)).added == 0
    humblefax.fail[('GET', '/incomingFax/700300/download')] = httpx.Response(500, json={})
    for _ in range(2):
        claim = second.claim()
        try:
            run(receiving.acquire(second, claim, configured))
        except Exception as error:  # the worker's own handling, step by step
            second.fail(claim['id'], str(error), claim_token=claim['claim_token'])
        moment[0] += timedelta(minutes=5)
    with database.connect() as connection:
        states = dict(connection.execute(sa.text(
            "SELECT operation_id, state FROM inbound_imports WHERE source = 'humblefax'")).all())
        failed = connection.execute(sa.text(
            "SELECT last_error, attempts FROM inbound_imports WHERE operation_id = '700300'")).one()
    assert states == {'700300': 'pending', '700301': 'received'}
    assert tuple(failed) == ('HumbleFax did not send the document (HTTP 500).', 1)
    receiving.forget_accounts()
