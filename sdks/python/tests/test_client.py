"""FaxbotClient paths, headers, status expectations and operation ids against a local fake Faxbot API."""
import hashlib
import json
import os
import re
import socket
import sys
import threading
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from requests.adapters import HTTPAdapter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from faxbot import FaxbotClient, FaxOperationConflict, FaxSubmissionUncertain  # noqa: E402

CONFLICT = 'Idempotency-Key already belongs to a different fax request.'


def _form(content_type, body):
    message = BytesParser(policy=HTTP).parsebytes(b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    return {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
            for part in message.iter_parts()}


@pytest.fixture
def fake():
    """A fake Faxbot that honors Idempotency-Key per API key, like the server contract.

    Each accepted job is one provider submission. ``plan`` scripts the next POST /fax replies:
    'accept' (default), 'drop' (accept, then close without answering), 'uncertain' (accept, then
    answer 503) or an HTTP status (answer it without accepting).
    """
    state = {'requests': [], 'fax_status': 202, 'plan': [], 'jobs': [], 'ledger': {}}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _record(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else b''
            state['requests'].append({'method': self.command, 'path': self.path,
                                      'key': self.headers.get('X-API-Key'),
                                      'operation': self.headers.get('Idempotency-Key'), 'body': body})
            return body

        def _fax(self, body):
            if state['fax_status'] != 202:
                return self._reply(state['fax_status'], {'id': 'job-1', 'status': 'queued'})
            with lock:
                action = state['plan'].pop(0) if state['plan'] else 'accept'
            if isinstance(action, int):
                return self._reply(action, {'detail': f'synthetic {action}'})
            fields = _form(self.headers['Content-Type'], body)
            request = (fields['to'].decode(), hashlib.sha256(fields['file']).hexdigest(), fields.get('queue_only'))
            scope = (self.headers.get('X-API-Key'), self.headers.get('Idempotency-Key'))
            with lock:
                entry = state['ledger'].get(scope) if scope[1] is not None else None
                if entry and entry['request'] != request:
                    return self._reply(409, {'detail': CONFLICT})
                if entry is None:
                    entry = {'request': request, 'job': {'id': f"job-{len(state['jobs']) + 1}", 'status': 'queued'}}
                    state['jobs'].append(entry['job'])  # one provider submission
                    if scope[1] is not None:
                        state['ledger'][scope] = entry
            if action == 'drop':
                self.close_connection = True  # the job exists but its response is lost
                return None
            if action == 'uncertain':
                return self._reply(503, {'detail': 'Fax acceptance is uncertain; retry with the same Idempotency-Key.'})
            return self._reply(202, entry['job'])

        def do_GET(self):  # noqa: N802
            self._record()
            if self.path == '/health':
                return self._reply(200, {'status': 'ok'})
            if self.path == '/plugins':
                return self._reply(200, {'items': [{'id': 'phaxio', 'enabled': True}]})
            if self.path == '/fax/missing':
                return self._reply(404, {'detail': 'Fax job not found'})
            if self.path.startswith('/fax/'):
                return self._reply(200, {'id': self.path[5:], 'status': 'SUCCESS'})
            self._reply(404, {'detail': 'Not Found'})

        def do_POST(self):  # noqa: N802
            body = self._record()
            if self.path == '/fax':
                return self._fax(body)
            if self.path == '/admin/plugins/http/install':
                manifest = json.loads(body)['manifest']
                return self._reply(200, {'ok': True, 'id': manifest['id'], 'path': '/providers/x.json'})
            self._reply(404, {'detail': 'Not Found'})

        def do_PUT(self):  # noqa: N802
            body = self._record()
            if self.path == '/plugins/phaxio/config':
                return self._reply(200, {'ok': True, 'changed': bool(json.loads(body))})
            self._reply(404, {'detail': 'Not Found'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    state['url'] = f'http://127.0.0.1:{server.server_address[1]}'
    state['posts'] = lambda: [request for request in state['requests'] if request['path'] == '/fax']
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def document(tmp_path):
    path = tmp_path / 'letter.pdf'
    path.write_bytes(b'%PDF-1.4 synthetic')
    return path


def test_send_fax_posts_multipart_with_the_key_and_requires_202(fake, document):
    client = FaxbotClient(fake['url'] + '/', api_key='sdk-key')
    assert client.send_fax('+15551234567', str(document)) == {'id': 'job-1', 'status': 'queued'}
    request = fake['requests'][-1]
    assert (request['method'], request['path'], request['key']) == ('POST', '/fax', 'sdk-key')
    assert b'name="to"' in request['body'] and b'%PDF-1.4 synthetic' in request['body']
    assert str(uuid.UUID(request['operation'], version=4)) == request['operation']
    fake['fax_status'] = 200
    with pytest.raises(Exception, match='HTTP 200'):
        client.send_fax('+15551234567', str(document))


def test_a_lost_response_is_recovered_with_the_same_operation_id(fake, document):
    fake['plan'] = ['drop']
    client = FaxbotClient(fake['url'], api_key='sdk-key', retry_backoff=0)
    job = client.send_fax('+15551234567', str(document))
    posts = fake['posts']()
    assert len(posts) == 2 and posts[0]['operation'] == posts[1]['operation']
    # The retry reopened the file: an exhausted handle would upload an empty document.
    assert b'%PDF-1.4 synthetic' in posts[1]['body']
    assert fake['jobs'] == [job] == [{'id': 'job-1', 'status': 'queued'}]


def test_an_unconfirmed_fax_is_finished_with_its_operation_id(fake, document):
    fake['plan'] = ['uncertain'] * 3
    client = FaxbotClient(fake['url'], api_key='sdk-key', retries=2, retry_backoff=0)
    with pytest.raises(FaxSubmissionUncertain) as caught:
        client.send_fax('+15551234567', str(document))
    error = caught.value
    assert [post['operation'] for post in fake['posts']()] == [error.operation_id] * 3
    assert error.status == 503
    assert str(error) == (f"Faxbot did not confirm this fax, so call send_fax again with "
                          f"operation_id='{error.operation_id}' to finish the same fax without sending it twice.")
    assert len(fake['jobs']) == 1
    assert client.resume_fax(error.operation_id, '+15551234567', str(document)) == fake['jobs'][0]
    assert len(fake['jobs']) == 1


def test_a_send_that_never_reached_faxbot_is_finished_later(fake, document):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        closed = f'http://127.0.0.1:{probe.getsockname()[1]}'
    with pytest.raises(FaxSubmissionUncertain) as caught:
        FaxbotClient(closed, api_key='sdk-key', retries=1, retry_backoff=0).send_fax('+15551234567', str(document))
    assert caught.value.status is None
    assert isinstance(caught.value.__cause__, requests.ConnectionError)
    client = FaxbotClient(fake['url'], api_key='sdk-key')
    first = client.send_fax('+15551234567', str(document), operation_id=caught.value.operation_id)
    again = client.resume_fax(caught.value.operation_id, '+15551234567', str(document))
    assert first == again and len(fake['jobs']) == 1


def test_an_operation_id_belongs_to_one_fax(fake, document, tmp_path):
    client = FaxbotClient(fake['url'], api_key='sdk-key', retry_backoff=0)
    operation_id = FaxbotClient.new_operation_id()
    client.send_fax('+15551234567', str(document), operation_id=operation_id)
    other = tmp_path / 'other.pdf'
    other.write_bytes(b'%PDF-1.4 a different document')
    for to, path in (('+15551234567', other), ('+15559876543', document)):
        with pytest.raises(FaxOperationConflict, match=re.escape(f'Conflict (409): {CONFLICT}')) as caught:
            client.send_fax(to, str(path), operation_id=operation_id)
        assert caught.value.operation_id == operation_id
    assert len(fake['posts']()) == 3, 'a conflict is never retried'
    assert len(fake['jobs']) == 1


def test_each_send_without_an_operation_id_is_a_new_fax(fake, document):
    client = FaxbotClient(fake['url'], api_key='sdk-key')
    first = client.send_fax('+15551234567', str(document))
    second = client.send_fax('+15551234567', str(document))
    assert first['id'] != second['id'] and len(fake['jobs']) == 2
    first_key, second_key = (post['operation'] for post in fake['posts']())
    assert first_key != second_key


@pytest.mark.parametrize('status, message', [
    (400, 'Bad Request (400): synthetic 400'), (401, 'Unauthorized (401)'), (404, 'Not Found (404)'),
    (408, 'HTTP 408'), (409, 'Conflict (409): synthetic 409'), (413, 'Payload Too Large (413)'),
    (415, 'Unsupported Media Type (415)'), (429, 'HTTP 429'), (500, 'HTTP 500'),
])
def test_only_unconfirmed_answers_are_retried(fake, document, status, message):
    fake['plan'] = [status]
    client = FaxbotClient(fake['url'], api_key='sdk-key', retries=2, retry_backoff=0)
    with pytest.raises(Exception, match=re.escape(message)) as caught:
        client.send_fax('+15551234567', str(document))
    assert not isinstance(caught.value, FaxSubmissionUncertain)
    assert len(fake['posts']()) == 1


@pytest.mark.parametrize('status', [502, 503, 504])
def test_gateway_failures_are_retried_with_the_same_operation_id(fake, document, status):
    fake['plan'] = [status, status]
    client = FaxbotClient(fake['url'], api_key='sdk-key', retries=2, retry_backoff=0)
    assert client.send_fax('+15551234567', str(document)) == {'id': 'job-1', 'status': 'queued'}
    assert len({post['operation'] for post in fake['posts']()}) == 1 and len(fake['posts']()) == 3


def test_every_request_goes_through_the_given_session(fake, document):
    sent = []

    class Recording(HTTPAdapter):
        def send(self, request, **kwargs):
            sent.append((request.method, request.path_url))
            return super().send(request, **kwargs)

    session = requests.Session()
    session.mount('http://', Recording())
    client = FaxbotClient(fake['url'], 'sdk-key', session=session)
    client.send_fax('+15551234567', str(document))
    client.get_status('job-1')
    client.check_health()
    client.plugins.list_plugins()
    assert sent == [('POST', '/fax'), ('GET', '/fax/job-1'), ('GET', '/health'), ('GET', '/plugins'),
                    ('GET', '/plugins')]
    assert 'X-API-Key' not in session.headers


def test_invalid_operation_ids_and_retry_settings_are_refused(fake, document):
    client = FaxbotClient(fake['url'], api_key='sdk-key')
    for bad in ('has space', '', 'x' * 129, 'café'):
        with pytest.raises(ValueError):
            client.send_fax('+15551234567', str(document), operation_id=bad)
    with pytest.raises(ValueError):
        client.resume_fax('', '+15551234567', str(document))
    with pytest.raises(ValueError):
        FaxbotClient(fake['url'], retries=-1)
    assert fake['posts']() == []


def test_status_and_health(fake):
    client = FaxbotClient(fake['url'], api_key='sdk-key')
    assert client.get_status('job-1') == {'id': 'job-1', 'status': 'SUCCESS'}
    with pytest.raises(Exception, match='404'):
        client.get_status('missing')
    assert client.check_health() is True
    health = next(request for request in fake['requests'] if request['path'] == '/health')
    assert health['key'] is None


def test_no_key_sends_no_header(fake):
    FaxbotClient(fake['url']).get_status('job-1')
    assert fake['requests'][-1]['key'] is None


def test_plugins_use_live_paths_and_the_typed_patch(fake):
    client = FaxbotClient(fake['url'], api_key='admin-key')
    assert client.plugins.list_plugins() == [{'id': 'phaxio', 'enabled': True}]
    client.plugins.update_plugin_config('phaxio', {'api_key': 'new'}, enabled=True, expected_revision_id='rev-1')
    put = fake['requests'][-1]
    assert put['path'] == '/plugins/phaxio/config'
    assert json.loads(put['body']) == {'settings': {'api_key': 'new'}, 'enabled': True, 'expected_revision_id': 'rev-1'}
    result = client.plugins.install_plugin({'id': 'acme-fax', 'name': 'Acme'})
    post = fake['requests'][-1]
    assert (post['method'], post['path'], post['key']) == ('POST', '/admin/plugins/http/install', 'admin-key')
    assert json.loads(post['body']) == {'manifest': {'id': 'acme-fax', 'name': 'Acme'}}
    assert result['id'] == 'acme-fax'
    with pytest.raises(TypeError):
        client.plugins.install_plugin('acme-fax')
    assert '/plugins/install' not in [request['path'] for request in fake['requests']]
