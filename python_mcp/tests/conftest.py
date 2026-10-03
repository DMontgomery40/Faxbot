"""A local fake Faxbot API and a threaded uvicorn runner for MCP transport tests."""
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PDF_BYTES = b'%PDF-1.4\n% synthetic inbound fax\n'


class FakeFaxbot:
    """Records every request with the X-API-Key it carried (None when absent)."""
    def __init__(self):
        self.requests = []
        self.inbound_envelope = False
        self.jwks = {'keys': []}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body, content_type='application/json'):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                key = self.headers.get('X-API-Key')
                fake.requests.append({'method': self.command, 'path': self.path, 'key': key, 'body': body})
                return key, body

            def do_POST(self):  # noqa: N802
                key, body = self._record()
                if self.path != '/fax':
                    return self._reply(404, {'detail': 'Not Found'})
                to = re.search(rb'name="to"\r\n\r\n([^\r]*)', body)
                self._reply(202, {'id': f'job-for-{key}', 'status': 'queued',
                                  'to': to.group(1).decode() if to else None})

            def do_GET(self):  # noqa: N802
                if self.path == '/.well-known/jwks.json':
                    return self._reply(200, fake.jwks)
                key, _ = self._record()
                path = self.path.split('?')[0]
                if path == '/health':
                    return self._reply(200, {'status': 'ok'})
                if path == '/fax/missing' or path == '/inbound/missing':
                    return self._reply(404, {'detail': 'Not found'})
                if match := re.fullmatch(r'/fax/([^/]+)', path):
                    return self._reply(200, {'id': match.group(1), 'to': '+15551230000', 'status': 'SUCCESS',
                                             'pages': 1, 'backend': 'sip', 'created_at': '2026-10-03T10:00:00',
                                             'updated_at': '2026-10-03T10:01:00', 'caller': key})
                if path == '/inbound':
                    items = [{'id': 'a1b2c3', 'fr': '+15550001111', 'to': '+15550002222', 'status': 'received',
                              'backend': 'sip', 'pages': 2, 'received_at': '2026-10-03T09:00:00'}]
                    return self._reply(200, {'items': items} if fake.inbound_envelope else items)
                if match := re.fullmatch(r'/inbound/([^/]+)/pdf', path):
                    return self._reply(200, PDF_BYTES, 'application/pdf')
                if match := re.fullmatch(r'/inbound/([^/]+)', path):
                    return self._reply(200, {'id': match.group(1), 'fr': '+15550001111', 'to': '+15550002222',
                                             'status': 'received', 'backend': 'sip', 'pages': 2})
                self._reply(404, {'detail': 'Not Found'})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def keys(self):
        return [request['key'] for request in self.requests]


@pytest.fixture
def fake_faxbot():
    fake = FakeFaxbot()
    fake.thread.start()
    try:
        yield fake
    finally:
        fake.server.shutdown()
        fake.server.server_close()


class RunningApp:
    def __init__(self, app):
        import uvicorn
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, lifespan='on',
                                                    log_level='warning'))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError('MCP test server did not start')
            time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def serve():
    return RunningApp


@pytest.fixture
def stdio_env(fake_faxbot):
    return {'PATH': os.environ.get('PATH', ''), 'FAX_API_URL': fake_faxbot.url, 'API_KEY': 'stdio-integration-key'}
