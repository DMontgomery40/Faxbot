"""Runtime ownership and SDK handshakes over the actual advertised transports."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import socket
import signal
import struct
import subprocess
import sys

import httpx
import pytest

from app import main
from app.ami import AMIClient
from app import ami
from app.config import use_configuration
from app.config_values import ConfigurationValues


@pytest.mark.asyncio
async def test_api_lifespan_stops_cleanup_before_reentering(isolated_installation, monkeypatch):
    """An unowned cleanup loop must not survive shutdown or double on restart."""
    monkeypatch.setenv("ARTIFACT_TTL_DAYS", "1")
    before = asyncio.all_tasks()
    owned = set()
    try:
        for _ in range(2):
            async with main.app.router.lifespan_context(main.app):
                await asyncio.sleep(0)
                current = asyncio.all_tasks() - before
                cleanup = {t for t in current if t.get_coro().__name__ == "_artifact_cleanup_loop"}
                assert len(cleanup) == 1
                delivery = {t for t in current if t.get_name() in {
                    'faxbot-outbound-worker', 'faxbot-outbound-poller'}}
                assert len(delivery) == 2
                owned.update(cleanup | delivery)
            assert all(task.done() for task in owned), "owned task survived API shutdown"
    finally:
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_access_preparation_failure_releases_installation_for_restart(isolated_installation, monkeypatch):
    """Internal lifecycle ownership only; no request or user-flow acceptance."""
    from app.access.types import AccessUnavailableError
    original = main.AccessRuntime
    def fail(configuration, *, docs_base):
        raise AccessUnavailableError()
    monkeypatch.setattr(main, 'AccessRuntime', fail)
    with pytest.raises(AccessUnavailableError):
        async with main.app.router.lifespan_context(main.app):
            pytest.fail('Incomplete access runtime must not publish readiness')
    assert main.app.state.runtime_active is False
    assert main.app.state.configuration_runtime is None
    assert main.app.state.access_runtime is None
    assert main.app.state.credential_transport is None
    monkeypatch.setattr(main, 'AccessRuntime', original)
    async with main.app.router.lifespan_context(main.app):
        configuration = main.app.state.configuration_runtime
        access = main.app.state.access_runtime
        assert configuration.serving
        assert access.store is configuration.manager.store.access_store
    assert main.app.state.access_runtime is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_first_login", [False, True])
@pytest.mark.parametrize("reset_on_shutdown", [False, True])
async def test_ami_login_reconnect_and_shutdown_use_owned_connections(monkeypatch, reject_first_login, reset_on_shutdown):
    """Login cannot recurse into connect; reconnect and sockets must stop on close."""
    logins = asyncio.Queue()
    disconnected = asyncio.Queue()
    connections = []
    peers = set()

    async def peer(reader, writer):
        peers.add(asyncio.current_task())
        connections.append(writer)
        try:
            logins.put_nowait(await reader.readuntil(b"\r\n\r\n"))
            denied = reject_first_login and len(connections) == 1
            writer.write(b"Response: Error\r\nMessage: Authentication failed\r\n\r\n" if denied else
                         b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n")
            await writer.drain()
            await reader.read()
        except ConnectionError:
            # An abortive TCP close is still a completed peer disconnection.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            disconnected.put_nowait(True)
            peers.discard(asyncio.current_task())

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    values = ConfigurationValues.from_environment({
        "ASTERISK_AMI_HOST": "127.0.0.1",
        "ASTERISK_AMI_PORT": str(server.sockets[0].getsockname()[1]),
        "ASTERISK_AMI_USERNAME": "synthetic-runtime",
        "ASTERISK_AMI_PASSWORD": "synthetic-password",
    })
    client = AMIClient()
    before = asyncio.all_tasks()
    with use_configuration(values):
        connecting = asyncio.create_task(client.connect())
    try:
        try:
            await asyncio.wait_for(asyncio.shield(connecting), 2)
        except asyncio.TimeoutError:
            pass
        assert client._connected.is_set(), "AMI login deadlocked before connecting"
        expected = b"Action: Login\r\nUsername: synthetic-runtime\r\nSecret: synthetic-password\r\n\r\n"
        assert await asyncio.wait_for(logins.get(), 2) == expected
        if reject_first_login:
            assert await asyncio.wait_for(logins.get(), 2) == expected
            await asyncio.wait_for(disconnected.get(), 2)
        connections[-1].close()
        await connections[-1].wait_closed()
        assert await asyncio.wait_for(logins.get(), 3) == expected
        if reset_on_shutdown:
            client.writer.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
        await client.close()
        assert not client._connected.is_set()
        assert client.writer is None
        await asyncio.wait_for(disconnected.get(), 2)
        await asyncio.wait_for(disconnected.get(), 2)
        assert not [t for t in asyncio.all_tasks() - before if t.get_coro().__qualname__.startswith("AMIClient.")]
    finally:
        connecting.cancel()
        await asyncio.gather(connecting, return_exceptions=True)
        if hasattr(client, "close"):
            await client.close()
        elif client.writer:
            client.writer.close()
            await client.writer.wait_closed()
        for writer in connections:
            writer.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*peers, return_exceptions=True)


@pytest.mark.asyncio
async def test_api_lifespan_closes_ami_and_does_not_duplicate_callbacks(isolated_installation, monkeypatch):
    """The API must close its real AMI supervisor when sequential lifespans end."""
    results = asyncio.Queue()
    peers = set()

    async def peer(reader, writer):
        peers.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
                         b"Event: UserEvent\r\nUserEvent: FaxResult\r\nJobID: synthetic-runtime-job\r\n\r\n")
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            peers.discard(asyncio.current_task())

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    client = AMIClient()
    monkeypatch.setattr(main, "ami_client", client)
    monkeypatch.setattr(main, "providerHasTrait", lambda *args: True)
    monkeypatch.setattr(main, "_handle_fax_result", results.put_nowait)
    monkeypatch.setenv("FAX_DISABLED", "false")
    monkeypatch.setenv("ASTERISK_AMI_HOST", "127.0.0.1")
    monkeypatch.setenv("ASTERISK_AMI_PORT", str(server.sockets[0].getsockname()[1]))
    monkeypatch.setenv("API_KEY", "synthetic-runtime-key")
    before = asyncio.all_tasks()
    try:
        for _ in range(2):
            async with main.app.router.lifespan_context(main.app):
                event = await asyncio.wait_for(results.get(), 3)
                assert event["JobID"] == "synthetic-runtime-job"
                assert results.empty()
            assert not client._connected.is_set()
            assert client.writer is None
            assert not [t for t in asyncio.all_tasks() - before if t.get_coro().__qualname__.startswith("AMIClient.")]
    finally:
        if hasattr(client, "close"):
            await client.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*peers, return_exceptions=True)


@pytest.mark.asyncio
async def test_ami_cancel_during_connection_retry_leaves_no_task(monkeypatch):
    """Shutdown must stop retries even when no AMI peer is available."""
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        values = ConfigurationValues.from_environment({
            "ASTERISK_AMI_HOST": "127.0.0.1",
            "ASTERISK_AMI_PORT": str(unavailable.getsockname()[1]),
        })
        client = AMIClient()
        with use_configuration(values):
            connecting = asyncio.create_task(client.connect())
        await asyncio.sleep(0.05)
        try:
            assert hasattr(client, "close"), "AMI has no shutdown lifecycle"
            await client.close()
            await asyncio.wait_for(asyncio.gather(connecting, return_exceptions=True), 2)
            assert not client._connected.is_set()
            assert client.writer is None
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("response, accepted", [
    (b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n", True),
    (b"Response: Error\r\nMessage: Authentication failed\r\n\r\n", False),
    (b"", False),
])
async def test_ami_probe_requires_successful_bounded_login(monkeypatch, response, accepted):
    """A denied or absent Login reply cannot pass the AMI readiness probe."""
    monkeypatch.setattr(ami, "LOGIN_TIMEOUT_SECONDS", 0.1, raising=False)
    closed = asyncio.Event()

    async def peer(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(response)
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    try:
        result = await asyncio.wait_for(ami.test_ami_connection(
            "127.0.0.1", server.sockets[0].getsockname()[1], "synthetic", "synthetic-password",
        ), 1)
        assert result is accepted
        await asyncio.wait_for(closed.wait(), 1)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_standalone_http_repeated_lifespans_release_sessions():
    """Each lifespan owns a fresh stateless SDK manager; no request task survives shutdown."""
    import json
    from python_mcp import http_server
    from sse_starlette.sse import AppStatus

    before = asyncio.all_tasks()
    server_watchers = set()
    try:
        for _ in range(2):
            async with http_server.app.router.lifespan_context(http_server.app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=http_server.app), base_url="http://127.0.0.1:8080") as client:
                    headers = {"Accept": "application/json, text/event-stream", "X-API-Key": "synthetic-caller-key"}
                    response = await client.post("/mcp", headers=headers, json={
                        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "runtime-test", "version": "1"},
                        },
                    })
                    assert response.status_code == 200
                    assert "Mcp-Session-Id" not in response.headers, "remote transport must be stateless"
                    notification = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    assert notification.status_code == 202
                    response = await client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                    assert response.status_code == 200
                    body = response.text
                    payload = json.loads(next((line[6:] for line in body.splitlines() if line.startswith("data: ")), body))
                    assert {t["name"] for t in payload["result"]["tools"]} == {
                        "send_fax", "get_fax_status", "get_fax", "list_inbound", "get_inbound_pdf"}
            await asyncio.sleep(0)
            remaining = asyncio.all_tasks() - before
            watchers = {task for task in remaining if task.get_coro().__qualname__ == "_shutdown_watcher"
                        and task.get_coro().cr_code.co_filename.endswith("sse_starlette/sse.py")}
            assert len(watchers) <= 1
            assert remaining == watchers, "application/session task survived shutdown"
            if server_watchers:
                assert watchers == server_watchers, "repeated lifespan created another framework watcher"
            server_watchers = watchers
    finally:
        # This ASGI test owns the server loop: use the documented public shutdown
        # signal to end the one server-lifetime watcher, without cancelling internals.
        AppStatus.should_exit = True
        try:
            await asyncio.wait_for(asyncio.gather(*server_watchers), 2)
        finally:
            AppStatus.should_exit = False


@pytest.mark.asyncio
async def test_failed_enabled_mcp_startup_cleans_up_api_tasks(isolated_installation, monkeypatch):
    from python_mcp import http_server

    def unavailable(**configuration):
        raise RuntimeError("synthetic MCP startup failure")

    monkeypatch.setattr(http_server, "create_app", unavailable)
    monkeypatch.setenv("ENABLE_MCP_HTTP", "true")
    monkeypatch.setenv("ARTIFACT_TTL_DAYS", "1")
    before = asyncio.all_tasks()
    with pytest.raises(RuntimeError, match="synthetic MCP startup failure"):
        async with main.app.router.lifespan_context(main.app):
            pytest.fail("API entered lifespan after enabled MCP failed")
    assert not (asyncio.all_tasks() - before), "API task survived failed startup"


@pytest.mark.parametrize("flag", ["ENABLE_MCP_SSE", "ENABLE_MCP_HTTP"])
def test_explicitly_enabled_mcp_import_failure_stops_startup(flag, tmp_path):
    """An explicitly enabled transport cannot silently disappear from a healthy API."""
    root = Path(__file__).resolve().parents[2]
    script = """
import asyncio, builtins
original = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'python_mcp':
        raise ImportError('synthetic enabled MCP import failure')
    return original(name, *args, **kwargs)
builtins.__import__ = blocked
from app.main import app
async def start():
    async with app.router.lifespan_context(app):
        pass
asyncio.run(start())
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True, timeout=10, env={
        "PATH": os.environ["PATH"], "PYTHONPATH": os.pathsep.join((str(root), str(root / "api"))),
        "FAX_DISABLED": "true", "FAX_DATA_DIR": str(tmp_path / "data"),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'runtime.db'}", "ENABLE_PERSISTED_SETTINGS": "false", flag: "true",
    })
    assert result.returncode != 0, "API started after explicitly enabled MCP import failed"
    assert "synthetic enabled MCP import failure" in result.stderr


@asynccontextmanager
async def running_server(module, tmp_path, **flags):
    """Use a prebound loopback socket; never load developer credentials."""
    root = Path(__file__).resolve().parents[2]
    with socket.socket() as listener, (tmp_path / "server-output.txt").open("w+") as output:
        listener.bind(("127.0.0.1", 0))
        env = {
            "PATH": os.environ["PATH"], "PYTHONPATH": os.pathsep.join((str(root), str(root / "api"))),
            "FAX_DISABLED": "true", "FAX_DATA_DIR": str(tmp_path / "data"),
            "DATABASE_URL": f"sqlite:///{tmp_path / 'runtime.db'}",
            "ENFORCE_PUBLIC_HTTPS": "false", "REQUIRE_API_KEY": "false",
            "ENABLE_PERSISTED_SETTINGS": "false", "REQUIRE_MCP_OAUTH": "false",
            "PYTHONDONTWRITEBYTECODE": "1", **flags,
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--fd", str(listener.fileno()), "--lifespan", "on"],
            cwd=root, env=env, pass_fds=(listener.fileno(),), stdout=output, stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        try:
            async with httpx.AsyncClient() as client:
                for _ in range(100):
                    if process.poll() is not None:
                        break
                    try:
                        if (await client.get(url + "/health", timeout=0.2)).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("server did not become healthy")
            assert process.poll() is None, output.read()
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            output.seek(0)
            log = output.read()
            assert process.returncode in (0, -signal.SIGTERM), log
            assert "Application shutdown complete" in log, log
            assert not any(marker in log for marker in ("ERROR:", "WARNING:", "RuntimeWarning", "Task was destroyed")), log


@pytest.mark.asyncio
async def test_enabled_embedded_mcp_sdk_initialize_and_tool_listing(tmp_path):
    """Both mounted transports must initialize and enumerate the documented tools."""
    import httpx2
    from mcp import Client
    from mcp.client.sse import sse_client
    from mcp.client.streamable_http import streamable_http_client

    caller = {"X-API-Key": "synthetic-caller-key"}
    names = {"send_fax", "get_fax_status", "list_inbound", "get_fax", "get_inbound_pdf"}
    async with running_server("app.main:app", tmp_path, ENABLE_MCP_HTTP="true", ENABLE_MCP_SSE="true") as url:
        for transport in (
            streamable_http_client(url + "/mcp/http/mcp", http_client=httpx2.AsyncClient(headers=caller)),
            sse_client(url + "/mcp/sse/sse", headers=caller),
        ):
            async with Client(transport, cache=None) as client:
                assert client.server_info.name == "Faxbot MCP (Python)"
                result = await client.list_tools()
                assert {tool.name for tool in result.tools} == names
                send = next(t for t in result.tools if t.name == "send_fax")
                assert "to" in send.input_schema["required"]


def test_embedded_mcp_health_paths_answer_the_console_without_credentials(isolated_installation, monkeypatch):
    """The MCP screen checks <mount>/health on the API origin; only that path is open."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("API_KEY", "synthetic-bootstrap")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("ENABLE_MCP_HTTP", "true")
    monkeypatch.setenv("ENABLE_MCP_SSE", "true")
    with TestClient(main.app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        for path, transport in (("/mcp/http/health", "streamable-http"), ("/mcp/sse/health", "sse")):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            assert response.json()["server"] == "faxbot-mcp"
            assert response.json()["transport"] == transport
        assert client.post("/mcp/http/mcp", json={}).status_code in {401, 403}


@pytest.mark.asyncio
async def test_standalone_http_lifespan_initializes_session_manager(tmp_path):
    """The standalone wrapper must enter the mounted SDK app lifespan as well."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with running_server("python_mcp.http_server:app", tmp_path) as url:
        transport = streamable_http_client(url + "/mcp", http_client=httpx2.AsyncClient(
            headers={"Authorization": "Bearer synthetic-caller-key"}))
        async with Client(transport, cache=None) as client:
            assert {tool.name for tool in (await client.list_tools()).tools} == {
                "send_fax", "get_fax_status", "get_fax", "list_inbound", "get_inbound_pdf"}


@pytest.mark.asyncio
async def test_embedded_sse_compatibility_adapter_keeps_oauth_required(tmp_path):
    """The streaming adapter must retain the wrapper's OAuth boundary."""
    async with running_server("app.main:app", tmp_path, ENABLE_MCP_SSE="true", REQUIRE_MCP_OAUTH="true") as url:
        async with httpx.AsyncClient() as client:
            for headers in ({}, {"Authorization": "Bearer synthetic-invalid-token"}):
                response = await client.get(url + "/mcp/sse/sse", headers=headers)
                assert response.status_code == 401
                assert response.json() == {"error": "Unauthorized"}
            response = await client.post(url + "/mcp/sse/messages/", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_stdio_sdk_initialize_and_original_tool_listing():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(__file__).resolve().parents[2]
    params = StdioServerParameters(
        command=sys.executable, args=[str(root / "python_mcp/stdio_server.py")],
        env={"FAX_DISABLED": "true", "FAX_API_URL": "http://127.0.0.1:1"},
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            assert {tool.name for tool in (await session.list_tools()).tools} == {
                "send_fax", "get_fax_status", "list_inbound", "get_fax", "get_inbound_pdf",
            }


class _RefusingAsterisk:
    """A manager port that answers every login with an error, as Asterisk does for a wrong password."""

    def __init__(self):
        import socketserver
        import threading
        self.logins = 0
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'Asterisk Call Manager/9.0.0\r\n')
                data = b''
                while b'\r\n\r\n' not in data:
                    chunk = self.rfile.read1(1024)
                    if not chunk:
                        return
                    data += chunk
                outer.logins += 1
                self.wfile.write(b'Response: Error\r\nMessage: Authentication failed\r\n\r\n')

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _closed_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize('engine', ['refuses_login', 'not_running'])
def test_startup_without_the_fax_engine_still_serves_the_console(isolated_installation, monkeypatch, engine):
    """A wrong manager password or a stopped Asterisk never locks people out of the console that fixes it."""
    import time
    from fastapi.testclient import TestClient
    from app.ami import ami_client, ENGINE_LOGIN_REJECTED, ENGINE_UNREACHABLE
    asterisk = _RefusingAsterisk() if engine == 'refuses_login' else None
    expected = ENGINE_LOGIN_REJECTED if asterisk else ENGINE_UNREACHABLE
    bootstrap = 'synthetic-runtime-bootstrap'
    for name, value in {'FAX_BACKEND': 'sip', 'FAX_DISABLED': 'false', 'ASTERISK_AMI_HOST': '127.0.0.1',
                        'ASTERISK_AMI_PORT': str(asterisk.port if asterisk else _closed_port()),
                        'ASTERISK_AMI_USERNAME': 'synthetic-runtime',
                        'ASTERISK_AMI_PASSWORD': 'synthetic-wrong-password', 'API_KEY': bootstrap,
                        'REQUIRE_API_KEY': 'true', 'PUBLIC_API_URL': 'https://testserver',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    # A refused login must end the startup wait at once, so that case gets a 30-second wait to stop early from;
    # a stopped engine waits the whole (short) wait.
    wait = 30.0 if asterisk else 0.5
    monkeypatch.setattr(main, 'AMI_STARTUP_WAIT_SECONDS', wait)
    headers = {'X-API-Key': bootstrap}
    started = time.monotonic()
    try:
        with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
            if asterisk:
                # A refused login ends the startup wait at once instead of after the timeout: 15 s is well under
                # the 30-second engine wait, and loaded CI machines need the room.
                assert time.monotonic() - started < 15 < wait
            assert client.get('/health').json() == {'status': 'ok'}
            ready = client.get('/health/ready')
            assert ready.status_code == 503 and ready.json()['message'] == expected
            assert ready.json()['checks']['outbound']['ami_connected'] is False
            assert client.get('/admin/settings', headers=headers).status_code == 200
            health = client.get('/admin/health-status', headers=headers).json()
            assert health['backend_healthy'] is False and health['backend_message'] == expected
            report = client.post('/admin/diagnostics/report', headers=headers).json()
            sentences = [check['sentence'] for section in report['sections'] for check in section['checks']]
            assert sentences.count(expected) == 1
            # A readable document: since rules in delivery (WP-C) the engine is checked once the pages are known,
            # because a sending rule may allow another route.
            from api.tests.test_work_http import pdf
            sent = client.post('/fax', headers=headers, data={'to': '+15555550123'},
                               files={'file': ('synthetic.pdf', pdf('Synthetic page'), 'application/pdf')})
            assert sent.status_code == 503 and sent.json()['detail'] == expected
            if asterisk:
                # The client keeps signing in again in the background (after 1 s, then 2 s).
                deadline = time.monotonic() + 6
                while asterisk.logins < 2 and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert asterisk.logins >= 2
                assert ami_client.problem == 'login_rejected'
        assert ami_client.problem is None and not ami_client._connected.is_set()
    finally:
        if asterisk:
            asterisk.close()
