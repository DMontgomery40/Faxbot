"""Receiving from eFax: Faxbot asks eFax for received faxes and stores each one once.

eFax's API is a synthetic ``httpx.MockTransport`` that speaks the published
shapes (GET /faxes/received, GET /faxes/{id}/metadata, GET /faxes/{id}/image,
PATCH /faxes/{id}/metadata, DELETE /faxes/{id}). The background tasks are off;
checks and fetches run as single steps so each state is asserted exactly.
"""
import asyncio
import base64
from datetime import timedelta
import hashlib
import io
import json
import sys

import httpx
from PIL import Image
import pytest

from app import main
from app.inbound import http as inbound_http
from app.inbound.acquisition import LEASE, account_identity, parse_source_time
from api.tests.test_inbound_acquisition import ADMIN, BOOTSTRAP, client, only_fax, pdf_bytes, rows, step


APP_ID, API_KEY, USER_ID = 'synthetic-efax-app', 'synthetic-efax-key', 'synthetic-efax-user'
FAX_ID = '9def20cc-fe85-43bc-babe-23c26c2a115b'
SECOND_ID = '4c5890ce-0edf-4877-aee1-706415922415'
OTHER_ID = '7e2a03da-95b8-483a-8219-e2762631efe5'
COMPLETED = '2026-10-03T14:00:00.000+0000'


def efax_module():
    return sys.modules['app.efax_service']


class FakeEfax:
    """The eFax API host; holds received faxes and records every request."""

    def __init__(self):
        self.requests = []
        self.faxes = {}
        self.images = {}
        self.fail = {}
        self.deleted = []

    def receive(self, fax_id, document, **changes):
        self.faxes[fax_id] = {'fax_id': fax_id, 'size': len(document), 'duration': 40, 'pages': 1,
                              'image_downloaded': False, 'completed_timestamp': COMPLETED,
                              'originating_fax_number': '18005551212', 'destination_fax_number': '13035550123',
                              'originating_fax_tsid': 'Synthetic Clinic',
                              'resource_url': 'https://elsewhere.invalid/' + fax_id, **changes}
        self.images[fax_id] = document

    def handler(self, request):
        body = request.read()
        self.requests.append({'method': request.method, 'url': str(request.url), 'host': request.url.host,
                              'path': request.url.path, 'params': dict(request.url.params),
                              'headers': {k.lower(): v for k, v in request.headers.items()}, 'body': body})
        path = request.url.path
        if path == '/tokens':
            return httpx.Response(200, json={'access_token': 'synthetic-token', 'expires_in': 86399})
        if request.headers.get('user-id') != USER_ID or request.headers.get('authorization') != 'Bearer synthetic-token':
            return httpx.Response(403, json={'errors': [{'error_code': 'FORBIDDEN'}]})
        failure = self.fail.get((request.method, path))
        if failure is not None:
            return failure() if callable(failure) else failure
        if path == '/faxes/received':
            listed = [fax for fax in self.faxes.values()
                      if not (request.url.params.get('image_downloaded') == 'false' and fax['image_downloaded'])]
            offset = int(request.headers.get('pagination-offset') or 0)
            limit = int(request.headers.get('pagination-limit') or 100)
            page = listed[offset:offset + limit]
            return httpx.Response(200, json={'first_record': offset + 1, 'last_record': offset + len(page),
                                             'faxes': page})
        parts = path.strip('/').split('/')
        fax_id = parts[1] if len(parts) > 1 else None
        fax = self.faxes.get(fax_id)
        if fax is None:
            return httpx.Response(404, json={'errors': [{'error_code': 'NOT_FOUND'}]})
        if parts[2:] == ['metadata'] and request.method == 'GET':
            return httpx.Response(200, json={**fax, 'direction': 'INBOUND', 'fax_status': 'STORED'})
        if parts[2:] == ['metadata'] and request.method == 'PATCH':
            fax['image_downloaded'] = json.loads(body)['image_downloaded']
            return httpx.Response(200, json={**fax, 'direction': 'INBOUND'})
        if parts[2:] == ['image'] and request.method == 'GET':
            fax['image_downloaded'] = True  # documented: reading the image marks the fax downloaded
            document = self.images[fax_id]
            name = fax_id + ('.tiff' if document.startswith(b'II*') else '.pdf')
            return httpx.Response(200, json={'fax_id': fax_id, 'file_name': name,
                                             'image': base64.b64encode(document).decode()})
        if parts[2:] == [] and request.method == 'DELETE':
            self.deleted.append(fax_id)
            del self.faxes[fax_id]
            return httpx.Response(204)
        return httpx.Response(404, json={})

    def calls(self):
        return [(request['method'], request['path']) for request in self.requests if request['path'] != '/tokens']


@pytest.fixture
def efax(monkeypatch):
    fake = FakeEfax()
    module = efax_module() if 'app.efax_service' in sys.modules else __import__('app.efax_service').efax_service
    module.clear_tokens()
    monkeypatch.setattr(module, '_TRANSPORT', httpx.MockTransport(fake.handler))
    monkeypatch.setattr(inbound_http, 'AUTOMATIC', False)
    # These tests read the inbox often; keep their requests out of other tests' per-minute limits.
    monkeypatch.setattr(main, '_rate_buckets', {})
    yield fake
    module.clear_tokens()


def environment(monkeypatch, **extra):
    values = {'INBOUND_ENABLED': 'true', 'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP,
              'PUBLIC_API_URL': 'https://testserver', 'MAX_REQUESTS_PER_MINUTE': '0', 'FAX_BACKEND': 'efax',
              'EFAX_APP_ID': APP_ID, 'EFAX_API_KEY': API_KEY, 'EFAX_USER_ID': USER_ID,
              'INBOUND_LIST_RPM': '0', 'INBOUND_GET_RPM': '0', 'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', **extra}
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def values():
    return main.app.state.configuration_runtime.manager.store.read().active.values


def check(**kwargs):
    from app.inbound.efax import check_once
    return asyncio.run(check_once(main.app.state.inbound_acquisition.store, values(), **kwargs))


def tiff_bytes():
    buffer = io.BytesIO()
    Image.new('1', (200, 100), 1).save(buffer, format='TIFF')
    return buffer.getvalue()


# 1. A received fax is listed, begun, fetched, stored, then marked downloaded ------
def test_a_received_fax_is_stored_once_then_marked_downloaded_in_efax(isolated_installation, monkeypatch, efax):
    environment(monkeypatch)
    document = pdf_bytes('eFax synthetic fax')
    efax.receive(FAX_ID, document)
    with client() as http:
        result = check()
        assert (result.listed, result.added) == (1, 1)
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['status_text'] == 'Waiting for the document from eFax.'
        assert fax['backend'] == 'efax' and fax['provider_fax_id'] == FAX_ID
        assert (fax['fr'], fax['to']) == ('+18005551212', '+13035550123')
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['status_text'] == 'Received.'
        assert fax['sha256'] == hashlib.sha256(document).hexdigest()
        assert fax['source_received_at'] == '2026-10-03T14:00:00'
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).content == document
        # Listing again finds nothing new; the stored fax is not fetched twice.
        assert check().listed == 0
        assert len(rows(isolated_installation, 'inbound_imports')) == 1
    assert efax.calls() == [('GET', '/faxes/received'), ('GET', f'/faxes/{FAX_ID}/metadata'),
                            ('GET', f'/faxes/{FAX_ID}/image'), ('PATCH', f'/faxes/{FAX_ID}/metadata'),
                            ('GET', '/faxes/received')]
    listing = efax.requests[1]
    assert listing['params'] == {'image_downloaded': 'false'}
    assert listing['headers']['pagination-offset'] == '0' and listing['headers']['pagination-limit'] == '100'
    image = next(request for request in efax.requests if request['path'].endswith('/image'))
    assert image['params'] == {'desired_format': 'PDF'}
    patch = next(request for request in efax.requests if request['method'] == 'PATCH')
    assert json.loads(patch['body']) == {'image_downloaded': True}
    assert {request['host'] for request in efax.requests} == {'api.securedocex.com'}
    assert all('elsewhere.invalid' not in request['url'] for request in efax.requests)
    [record] = rows(isolated_installation, 'inbound_imports')
    assert record['source'] == 'efax' and record['operation_id'] == FAX_ID and record['state'] == 'received'
    assert record['account'] == account_identity('efax', APP_ID + '\x00' + USER_ID)
    for secret in (APP_ID, API_KEY, USER_ID, 'synthetic-token'):
        assert secret not in json.dumps(record, default=str)
    assert 'resource_url' not in record['report']
    assert efax.deleted == []


def test_deleting_from_efax_happens_only_when_turned_on_and_after_storing(isolated_installation, monkeypatch, efax):
    environment(monkeypatch, EFAX_DELETE_AFTER_DOWNLOAD='true')
    efax.receive(FAX_ID, pdf_bytes())
    with client() as http:
        check()
        efax.fail[('GET', f'/faxes/{FAX_ID}/image')] = httpx.Response(500, json={})
        assert step() is True
        assert only_fax(http)['status'] == 'waiting' and efax.deleted == []
        del efax.fail[('GET', f'/faxes/{FAX_ID}/image')]
        assert step(later=timedelta(minutes=2)) is True
        assert only_fax(http)['status'] == 'received'
    assert efax.deleted == [FAX_ID]
    assert efax.calls()[-2:] == [('PATCH', f'/faxes/{FAX_ID}/metadata'), ('DELETE', f'/faxes/{FAX_ID}')]


# 2. Listing again never duplicates a fax or moves its retry schedule ------------------
def test_listing_a_known_fax_again_adds_nothing_and_keeps_its_retry_time(isolated_installation, monkeypatch, efax):
    environment(monkeypatch)
    efax.receive(FAX_ID, pdf_bytes())
    with client() as http:
        assert check().added == 1
        assert check().added == 0
        efax.fail[('GET', f'/faxes/{FAX_ID}/image')] = httpx.Response(503, json={})
        assert step() is True
        [before] = rows(isolated_installation, 'inbound_imports')
        assert before['state'] == 'pending' and before['next_attempt_at'] is not None
        assert check().added == 0
        [after] = rows(isolated_installation, 'inbound_imports')
        assert after['next_attempt_at'] == before['next_attempt_at'] and after['attempts'] == 1
        assert len(rows(isolated_installation, 'inbound_faxes')) == 1
        fax = only_fax(http)
        assert fax['status_text'].startswith('The document could not be fetched; Faxbot will try again at ')
        assert fax['can_fetch_again'] is True


def test_a_stored_fax_still_listed_is_marked_downloaded_without_a_second_copy(isolated_installation, monkeypatch,
                                                                               efax):
    environment(monkeypatch)
    efax.receive(FAX_ID, pdf_bytes())
    with client():
        check()
        efax.fail[('PATCH', f'/faxes/{FAX_ID}/metadata')] = httpx.Response(500, json={})
        assert step() is True
        efax.faxes[FAX_ID]['image_downloaded'] = False  # eFax still lists it as not downloaded
        del efax.fail[('PATCH', f'/faxes/{FAX_ID}/metadata')]
        result = check()
        assert (result.added, result.finished) == (0, 1)
        assert efax.faxes[FAX_ID]['image_downloaded'] is True
        assert len(rows(isolated_installation, 'inbound_imports')) == 1
        assert efax.calls().count(('GET', f'/faxes/{FAX_ID}/image')) == 1


# 3. A restart resumes the same import, even after eFax marked the fax downloaded -----
def test_a_fetch_interrupted_after_download_resumes_after_restart(isolated_installation, monkeypatch, efax):
    from app.inbound.acquisition import ImportStore
    environment(monkeypatch)
    document = pdf_bytes('Interrupted eFax fax')
    efax.receive(FAX_ID, document)
    with client() as http:
        check()
        original = ImportStore.complete

        def crash(self, *args, **kwargs):
            raise KeyboardInterrupt('synthetic process stop')

        monkeypatch.setattr(ImportStore, 'complete', crash)
        with pytest.raises(KeyboardInterrupt):
            step()
        monkeypatch.setattr(ImportStore, 'complete', original)
        assert efax.faxes[FAX_ID]['image_downloaded'] is True  # the download already marked it in eFax
        assert only_fax(http)['status'] == 'waiting'
    with client() as http:  # a restarted installation; eFax no longer lists the fax
        assert check().listed == 0
        assert step() is False  # the lease from the stopped process has not run out yet
        assert step(later=LEASE + timedelta(seconds=1)) is True
        fax = only_fax(http)
        assert fax['status'] == 'received' and fax['sha256'] == hashlib.sha256(document).hexdigest()
        [record] = rows(isolated_installation, 'inbound_imports')
        assert record['attempts'] == 2 and record['state'] == 'received'
    assert efax.calls().count(('GET', f'/faxes/{FAX_ID}/image')) == 2


# 4. The account, the document and the address are all checked -----------------------
def test_a_changed_efax_account_is_never_asked_for_an_earlier_fax(isolated_installation, monkeypatch, efax):
    environment(monkeypatch)
    efax.receive(FAX_ID, pdf_bytes())
    with client():
        check()
    monkeypatch.setenv('EFAX_USER_ID', 'synthetic-other-user')
    with client() as http:
        before = len(efax.requests)
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'waiting'
        assert fax['problem'] == 'The eFax account in settings changed after this fax arrived, so Faxbot cannot fetch it.'
        assert len(efax.requests) == before


@pytest.mark.parametrize(('reply', 'problem'), [
    (lambda: httpx.Response(200, json={'fax_id': OTHER_ID, 'image': base64.b64encode(b'%PDF-1.4').decode()}),
     'eFax sent the document of a different fax.'),
    (lambda: httpx.Response(200, json={'fax_id': FAX_ID, 'image': '***not base64***'}),
     'eFax sent a document Faxbot cannot decode.'),
    (lambda: httpx.Response(200, json={'fax_id': FAX_ID, 'image': base64.b64encode(b'<html>').decode()}),
     'eFax sent something that is not a PDF.'),
    (lambda: httpx.Response(404, json={}), 'eFax does not have the document for this fax.'),
    (lambda: httpx.Response(429, headers={'Retry-After': '120'}), 'eFax asked Faxbot to wait; Faxbot will try again.'),
])
def test_unusable_documents_are_never_stored_and_say_why(isolated_installation, monkeypatch, efax, reply, problem):
    environment(monkeypatch)
    efax.receive(FAX_ID, pdf_bytes())
    with client() as http:
        check()
        efax.fail[('GET', f'/faxes/{FAX_ID}/image')] = reply
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'waiting' and fax['problem'] == problem and fax['sha256'] is None
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).status_code == 404
    assert ('PATCH', f'/faxes/{FAX_ID}/metadata') not in efax.calls()


def test_listed_entries_without_a_real_fax_id_are_never_requested(isolated_installation, monkeypatch, efax):
    environment(monkeypatch)
    entries = [{'fax_id': '../' + FAX_ID}, {'fax_id': 'https://elsewhere.invalid/' + FAX_ID}, {'fax_id': None},
               {'fax_id': FAX_ID + '?x=1'}, 'not an object', {'resource_url': 'https://elsewhere.invalid/x'}]
    efax.fail[('GET', '/faxes/received')] = lambda: httpx.Response(200, json={'faxes': entries})
    with client():
        result = check()
        assert (result.listed, result.added) == (0, 0)
        assert rows(isolated_installation, 'inbound_imports') == []
    assert efax.calls() == [('GET', '/faxes/received')]
    assert {request['host'] for request in efax.requests} == {'api.securedocex.com'}


def test_a_tiff_from_efax_is_stored_as_a_pdf(isolated_installation, monkeypatch, efax):
    environment(monkeypatch)
    efax.receive(FAX_ID, tiff_bytes())
    with client() as http:
        check()
        assert step() is True
        fax = only_fax(http)
        assert fax['status'] == 'received'
        assert http.get(f"/inbound/{fax['id']}/pdf", headers=ADMIN).content.startswith(b'%PDF')


def test_a_fax_efax_no_longer_has_waits_for_a_person(isolated_installation, monkeypatch, efax):
    environment(monkeypatch)
    efax.receive(FAX_ID, pdf_bytes())
    with client() as http:
        check()
        del efax.faxes[FAX_ID]
        assert step() is True
        fax = only_fax(http)
        assert fax['problem'] == 'eFax has no received fax with this ID in the configured account.'
        assert fax['status'] == 'waiting'


# 5. Paging, pacing and when checking happens at all ---------------------------------
def test_every_page_is_read_until_a_short_one(isolated_installation, monkeypatch, efax):
    environment(monkeypatch)
    for number in range(150):
        efax.receive(f'00000000-0000-4000-8000-{number:012d}', pdf_bytes())
    with client():
        result = check()
        assert (result.listed, result.added) == (150, 150)
        offsets = [request['headers']['pagination-offset'] for request in efax.requests
                   if request['path'] == '/faxes/received']
        assert offsets == ['0', '100']
        assert len(rows(isolated_installation, 'inbound_imports')) == 150


def test_checking_stops_after_ten_pages(isolated_installation, monkeypatch, efax):
    from app.inbound.efax import MAX_PAGES
    environment(monkeypatch)
    page = [{'fax_id': f'00000000-0000-4000-8000-{n:012d}', 'completed_timestamp': COMPLETED} for n in range(100)]
    efax.fail[('GET', '/faxes/received')] = lambda: httpx.Response(200, json={'faxes': page})
    with client():
        result = check()
        assert result.listed == 100 * MAX_PAGES and result.added == 100
        assert sum(request['path'] == '/faxes/received' for request in efax.requests) == MAX_PAGES


def test_receiver_waits_longer_after_problems_and_as_long_as_efax_asks(isolated_installation, monkeypatch, efax):
    from app.inbound.efax import EfaxReceiver
    environment(monkeypatch, EFAX_POLL_SECONDS='60')
    with client():
        store = main.app.state.inbound_acquisition.store
        frame = lambda: (values(), {})  # noqa: E731
        kicks = []
        receiver = EfaxReceiver(store, frame, kick=lambda: kicks.append(1))
        assert asyncio.run(receiver.step()) == 60 and receiver.last_checked is not None
        efax.fail[('GET', '/faxes/received')] = httpx.Response(500, json={})
        assert asyncio.run(receiver.step()) == 120
        assert asyncio.run(receiver.step()) == 240
        assert receiver.last_problem == 'eFax did not list received faxes.'
        efax.fail[('GET', '/faxes/received')] = httpx.Response(429, headers={'Retry-After': '900'})
        assert asyncio.run(receiver.step()) == 900
        del efax.fail[('GET', '/faxes/received')]
        efax.receive(FAX_ID, pdf_bytes())
        assert asyncio.run(receiver.step()) == 60 and receiver.failures == 0 and kicks == [1]


@pytest.mark.parametrize('extra', [{'INBOUND_ENABLED': 'false'}, {'FAX_INBOUND_BACKEND': 'phaxio',
                                   'PHAXIO_API_KEY': 'synthetic', 'PHAXIO_API_SECRET': 'synthetic'}])
def test_nothing_is_asked_unless_efax_receives(isolated_installation, monkeypatch, efax, extra):
    from app.inbound.efax import EfaxReceiver
    environment(monkeypatch, **extra)
    efax.receive(FAX_ID, pdf_bytes())
    with client():
        receiver = EfaxReceiver(main.app.state.inbound_acquisition.store, lambda: (values(), {}))
        assert asyncio.run(receiver.step()) == 60
    assert efax.requests == []


def test_receiving_through_efax_compiles_an_inbound_profile(tmp_path):
    from app.config_activation import compile_profiles
    from app.config_bootstrap import default_plugin_state
    from app.config_paths import provider_traits_path
    from app.config_values import ConfigurationValues
    from app.provider_catalog import ProviderCatalog
    configured = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'efax', 'INBOUND_ENABLED': 'true', 'EFAX_APP_ID': APP_ID, 'EFAX_API_KEY': API_KEY,
        'EFAX_USER_ID': USER_ID})
    catalog = ProviderCatalog.load(provider_traits_path(), tmp_path / 'providers')
    profiles = compile_profiles(configured, catalog, default_plugin_state(configured))
    assert profiles['inbound'].provider_id == 'efax' and profiles['inbound'].traits['supports_inbound'] is True


def test_efax_completion_times_are_read():
    assert parse_source_time(COMPLETED).isoformat() == '2026-10-03T14:00:00'
    assert parse_source_time('2017-03-01T01:33:49.000+0000').isoformat() == '2017-03-01T01:33:49'
