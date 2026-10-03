"""FaxbotClient paths, headers and status expectations against a local fake Faxbot API."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from faxbot import FaxbotClient  # noqa: E402


@pytest.fixture
def fake():
    state = {'requests': [], 'fax_status': 202}

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
                                      'key': self.headers.get('X-API-Key'), 'body': body})
            return body

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
                return self._reply(state['fax_status'], {'id': 'job-1', 'status': 'queued'})
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state['url'] = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def test_send_fax_posts_multipart_with_the_key_and_requires_202(fake, tmp_path):
    document = tmp_path / 'letter.pdf'
    document.write_bytes(b'%PDF-1.4 synthetic')
    client = FaxbotClient(fake['url'] + '/', api_key='sdk-key')
    assert client.send_fax('+15551234567', str(document)) == {'id': 'job-1', 'status': 'queued'}
    request = fake['requests'][-1]
    assert (request['method'], request['path'], request['key']) == ('POST', '/fax', 'sdk-key')
    assert b'name="to"' in request['body'] and b'%PDF-1.4 synthetic' in request['body']
    fake['fax_status'] = 200
    with pytest.raises(Exception, match='HTTP 200'):
        client.send_fax('+15551234567', str(document))


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
