"""Local fakes: a Faxbot REST API that records the forwarded key, and a threaded uvicorn runner."""
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

PDF_BYTES = b"%PDF-1.4\n% synthetic inbound fax\n%%EOF\n"
JOB = {"id": "job-1", "status": "queued", "to": "+15551230000", "backend": "test",
       "pages": None, "error": None, "created_at": "2026-10-03T12:00:00", "updated_at": "2026-10-03T12:00:00"}
INBOUND = {"id": "in-1", "fr": "+15559870000", "to": "+15551230000", "status": "received",
           "backend": "sip", "pages": 1, "received_at": "2026-10-03T11:00:00"}


class FakeFaxbot:
    def __init__(self):
        self.requests = []
        self.inbound_envelope = False
        self.jwks = None
        self.server = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def keys(self):
        return [request["key"] for request in self.requests]


def _handler(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _record(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            fake.requests.append({"method": self.command, "path": self.path.split("?")[0],
                                  "key": self.headers.get("X-API-Key"), "body": body})
            return body

        def do_POST(self):
            body = self._record()
            if self.path == "/fax" and b'name="to"' in body and b'name="file"' in body:
                self._send(202, {"id": JOB["id"], "status": JOB["status"]})
            else:
                self._send(400, {"detail": "bad request"})

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/jwks.json" and fake.jwks:
                self._send(200, fake.jwks)
                return
            self._record()
            if path == f"/fax/{JOB['id']}":
                self._send(200, JOB)
            elif path.startswith("/fax/"):
                self._send(404, {"detail": "Job not found"})
            elif path == "/inbound":
                self._send(200, {"items": [INBOUND]} if fake.inbound_envelope else [INBOUND])
            elif path == f"/inbound/{INBOUND['id']}":
                self._send(200, INBOUND)
            elif path == f"/inbound/{INBOUND['id']}/pdf":
                self._send(200, PDF_BYTES, "application/pdf")
            else:
                self._send(404, {"detail": "Not found"})

    return Handler


@pytest.fixture
def fake_faxbot():
    fake = FakeFaxbot()
    fake.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    thread = threading.Thread(target=fake.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        fake.server.shutdown()
        fake.server.server_close()


@contextmanager
def serve_app(application):
    """Run an ASGI app with uvicorn on an ephemeral loopback port."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(application, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("MCP test server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
