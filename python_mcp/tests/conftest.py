"""A local fake Faxbot API and a threaded uvicorn runner for MCP transport tests."""
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PDF_BYTES = b'%PDF-1.4\n% synthetic inbound fax\n'
CONFLICT = 'Idempotency-Key already belongs to a different fax request.'


def _form(content_type, body):
    message = BytesParser(policy=HTTP).parsebytes(b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    return {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
            for part in message.iter_parts()}


class FakeFaxbot:
    """Records every request with the X-API-Key it carried (None when absent).

    POST /fax honors Idempotency-Key per API key like the server contract; each accepted job is one
    provider submission in ``jobs``. ``plan`` scripts the next POST /fax replies: 'accept' (default),
    'drop' (accept, then close without answering), 'uncertain' (accept, then answer 503) or an HTTP
    status (answer it without accepting).
    """
    def __init__(self):
        self.requests = []
        self.inbound_envelope = False
        self.jwks = {'keys': []}
        self.plan = []
        self.jobs = []
        self.ledger = {}
        lock = threading.Lock()
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
                fake.requests.append({'method': self.command, 'path': self.path, 'key': key, 'body': body,
                                      'operation': self.headers.get('Idempotency-Key')})
                return key, body

            def do_POST(self):  # noqa: N802
                key, body = self._record()
                if self.path != '/fax':
                    return self._reply(404, {'detail': 'Not Found'})
                with lock:
                    action = fake.plan.pop(0) if fake.plan else 'accept'
                if isinstance(action, int):
                    return self._reply(action, {'detail': f'synthetic {action}'})
                fields = _form(self.headers['Content-Type'], body)
                to = fields['to'].decode()
                request = (to, hashlib.sha256(fields['file']).hexdigest(), fields.get('queue_only'))
                operation = self.headers.get('Idempotency-Key')
                with lock:
                    entry = fake.ledger.get((key, operation)) if operation is not None else None
                    if entry and entry['request'] != request:
                        return self._reply(409, {'detail': CONFLICT})
                    if entry is None:
                        number = 1 + sum(1 for job in fake.jobs if job['id'].endswith(f'-for-{key}'))
                        entry = {'request': request, 'job': {'id': f'job-{number}-for-{key}', 'status': 'queued',
                                                             'to': to}}
                        fake.jobs.append(entry['job'])  # one provider submission
                        if operation is not None:
                            fake.ledger[(key, operation)] = entry
                if action == 'drop':
                    self.close_connection = True  # the job exists but its response is lost
                    return None
                if action == 'uncertain':
                    return self._reply(503, {'detail': 'Fax acceptance is uncertain; retry with the same key.'})
                self._reply(202, entry['job'])

            def do_GET(self):  # noqa: N802
                if self.path == '/.well-known/jwks.json':
                    return self._reply(200, fake.jwks)
                key, _ = self._record()
                path = self.path.split('?')[0]
                if path == '/health':
                    return self._reply(200, {'status': 'ok'})
                if path in ('/fax/missing', '/fax/a1b2c3', '/inbound/missing'):  # inbound ids are hex too
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
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)

    def keys(self):
        return [request['key'] for request in self.requests]

    def posts(self):
        return [request for request in self.requests if request['path'] == '/fax']


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
