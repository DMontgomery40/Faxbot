"""Streamable HTTP and SSE servers forward each caller's own Faxbot key."""
import base64
import json
import os
import time

import httpx
import httpx2
import pytest

from conftest import INBOUND, JOB, PDF_BYTES, serve_app
from mcp import Client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_PROTOCOL_VERSION
from python_mcp import faxbot_tools, http_server, server, stdio_server

TOOLS = set(faxbot_tools.TOOL_NAMES)


def streamable(url, headers):
    return streamable_http_client(url + "/mcp", http_client=httpx2.AsyncClient(headers=headers))


@pytest.mark.parametrize("instance, local", [
    (stdio_server.mcp, True),
    (http_server.create_app(api_base_url="http://example.invalid").state.mcp, False),
    (server.create_app(api_base_url="http://example.invalid", require_oauth=False).state.mcp, False),
])
@pytest.mark.asyncio
async def test_every_transport_lists_the_documented_tools(instance, local):
    tools = {tool.name: tool for tool in await instance.list_tools()}
    assert set(tools) == TOOLS
    send = tools["send_fax"].input_schema
    assert send["type"] == "object" and "to" in send["required"]
    assert ({"filePath", "fileUrl"} <= set(send["properties"])) is local
    for name in ("send_fax", "get_fax_status", "get_fax", "list_inbound"):
        assert tools[name].output_schema["type"] == "object"
    templates = await instance.list_resource_templates()
    assert [template.uri_template for template in templates] == [faxbot_tools.INBOUND_PDF_URI]


@pytest.mark.parametrize("mode, version", [("auto", LATEST_PROTOCOL_VERSION), ("legacy", LATEST_HANDSHAKE_VERSION)])
@pytest.mark.asyncio
async def test_streamable_http_flows_forward_the_callers_key(fake_faxbot, mode, version):
    application = http_server.create_app(api_base_url=fake_faxbot.url, api_key="configured-key-must-not-be-used")
    with serve_app(application) as url:
        async with Client(streamable(url, {"X-API-Key": "caller-a"}), mode=mode) as client:
            assert client.protocol_version == version
            sent = await client.call_tool("send_fax", {
                "to": "+15551230000", "fileName": "note.txt",
                "fileContent": base64.b64encode(b"hello").decode()})
            assert not sent.is_error and sent.structured_content["id"] == JOB["id"]
            status = await client.call_tool("get_fax_status", {"jobId": JOB["id"]})
            assert status.structured_content["status"] == JOB["status"]
            inbound = await client.call_tool("list_inbound", {"limit": 5})
            assert inbound.structured_content == {"items": [INBOUND]}
            lookup = await client.call_tool("get_fax", {"id": INBOUND["id"]})
            assert lookup.structured_content["kind"] == "inbound"
            link = await client.call_tool("get_inbound_pdf", {"inboundId": INBOUND["id"]})
            assert link.content[0].type == "resource_link"
            document = await client.read_resource(link.content[0].uri)
            assert base64.b64decode(document.contents[0].blob) == PDF_BYTES
        async with Client(streamable(url, {"Authorization": "Bearer caller-b"}), mode=mode) as client:
            assert not (await client.call_tool("get_fax_status", {"jobId": JOB["id"]})).is_error
    keys = fake_faxbot.keys()
    assert keys[:-1] == ["caller-a"] * (len(keys) - 1) and keys[-1] == "caller-b"
    assert [r["path"] for r in fake_faxbot.requests[:3]] == ["/fax", f"/fax/{JOB['id']}", "/inbound"]


@pytest.mark.asyncio
async def test_inbound_items_envelope_is_still_accepted(fake_faxbot):
    fake_faxbot.inbound_envelope = True
    with serve_app(http_server.create_app(api_base_url=fake_faxbot.url)) as url:
        async with Client(streamable(url, {"X-API-Key": "caller-a"})) as client:
            result = await client.call_tool("list_inbound", {})
    assert result.structured_content == {"items": [INBOUND]}


@pytest.mark.asyncio
async def test_remote_send_cannot_read_server_files(fake_faxbot, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("server-only")
    with serve_app(http_server.create_app(api_base_url=fake_faxbot.url)) as url:
        async with Client(streamable(url, {"X-API-Key": "caller-a"})) as client:
            result = await client.call_tool("send_fax", {"to": "+15551230000", "filePath": str(secret)})
    assert result.is_error
    assert fake_faxbot.requests == []


@pytest.mark.parametrize("module, path", [(http_server, "/mcp"), (server, "/sse")])
@pytest.mark.asyncio
async def test_requests_without_a_caller_key_are_rejected(module, path):
    application = module.create_app(api_base_url="http://example.invalid", api_key="configured-key",
                                    require_oauth=False)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                     base_url="http://127.0.0.1") as client:
            for headers in ({}, {"X-API-Key": ""}, {"Authorization": "Basic abc"}):
                response = await client.get(path, headers=headers)
                assert response.status_code == 401
                assert response.headers["www-authenticate"].startswith("Bearer")
            assert (await client.get("/health")).json()["status"] == "ok"


@pytest.mark.asyncio
async def test_sse_compatibility_transport_forwards_the_callers_key(fake_faxbot):
    application = server.create_app(api_base_url=fake_faxbot.url, require_oauth=False)
    with serve_app(application) as url:
        async with Client(sse_client(url + "/sse", headers={"X-API-Key": "caller-sse"}), mode="legacy") as client:
            assert {tool.name for tool in (await client.list_tools()).tools} == TOOLS
            result = await client.call_tool("get_fax_status", {"jobId": JOB["id"]})
    assert result.structured_content["id"] == JOB["id"]
    assert fake_faxbot.keys() == ["caller-sse"]


def _signed_token(private_key, claims):
    from jose import jwt
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "synthetic-kid"})


@pytest.mark.asyncio
async def test_oauth_subject_maps_to_its_stored_faxbot_key(fake_faxbot, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    numbers = key.public_key().public_numbers()

    def encoded(value):
        return base64.urlsafe_b64encode(value.to_bytes((value.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

    fake_faxbot.jwks = {"keys": [{"kty": "RSA", "kid": "synthetic-kid", "use": "sig",
                                  "n": encoded(numbers.n), "e": encoded(numbers.e)}]}
    subject_keys = tmp_path / "subject-keys.json"
    subject_keys.write_text(json.dumps({"alice": "alice-faxbot-key"}))
    issuer = "https://issuer.invalid"
    application = http_server.create_app(
        api_base_url=fake_faxbot.url, require_oauth=True, oauth_issuer=issuer, oauth_audience="faxbot-mcp",
        oauth_jwks_url=fake_faxbot.url + "/jwks.json", subject_keys_file=str(subject_keys))
    claims = {"iss": issuer, "aud": "faxbot-mcp", "exp": int(time.time()) + 300}
    alice = _signed_token(private, {**claims, "sub": "alice"})
    bob = _signed_token(private, {**claims, "sub": "bob"})
    with serve_app(application) as url:
        async with Client(streamable(url, {"Authorization": f"Bearer {alice}"})) as client:
            assert not (await client.call_tool("get_fax_status", {"jobId": JOB["id"]})).is_error
        async with httpx.AsyncClient(base_url=url) as raw:
            metadata = (await raw.get("/.well-known/oauth-protected-resource")).json()
            assert metadata["authorization_servers"] == [issuer]
            unmapped = await raw.post("/mcp", headers={"Authorization": f"Bearer {bob}"}, json={})
            assert unmapped.status_code == 403
            raw_key = await raw.post("/mcp", headers={"Authorization": "Bearer not-a-jwt"}, json={})
            assert raw_key.status_code == 401
            assert "resource_metadata=" in raw_key.headers["www-authenticate"]
        async with Client(streamable(url, {"Authorization": f"Bearer {bob}", "X-API-Key": "bob-own-key"})) as client:
            assert not (await client.call_tool("get_fax_status", {"jobId": JOB["id"]})).is_error
    assert fake_faxbot.keys() == ["alice-faxbot-key", "bob-own-key"]


def test_factories_do_not_change_the_environment():
    before = dict(os.environ)
    http_server.create_app(api_base_url="http://example.invalid", api_key="synthetic-key", require_oauth=False)
    server.create_app(api_base_url="http://example.invalid", api_key="synthetic-key", require_oauth=False)
    assert dict(os.environ) == before
