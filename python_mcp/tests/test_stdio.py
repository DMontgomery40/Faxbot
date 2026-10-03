"""The stdio server is one integration identity and keeps stdout pure JSON-RPC."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import INBOUND, JOB
from mcp import Client, StdioServerParameters

SCRIPT = Path(__file__).resolve().parents[1] / "stdio_server.py"


def _environment(fake_url):
    return {"FAX_API_URL": fake_url, "API_KEY": "integration-key", "PATH": "/usr/bin:/bin"}


@pytest.mark.asyncio
async def test_stdio_forwards_the_configured_integration_key(fake_faxbot, tmp_path):
    document = tmp_path / "cover.txt"
    document.write_text("hello")
    params = StdioServerParameters(command=sys.executable, args=[str(SCRIPT)], env=_environment(fake_faxbot.url))
    async with Client(params) as client:
        assert len((await client.list_tools()).tools) == 5
        sent = await client.call_tool("send_fax", {"to": "+15551230000", "filePath": str(document)})
        assert sent.structured_content["id"] == JOB["id"]
        inbound = await client.call_tool("list_inbound", {})
        assert inbound.structured_content == {"items": [INBOUND]}
    assert fake_faxbot.keys() == ["integration-key", "integration-key"]
    assert b'filename="cover.txt"' in fake_faxbot.requests[0]["body"]


def test_stdio_stdout_carries_only_json_rpc(fake_faxbot):
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "stdout-test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_fax_status", "arguments": {"jobId": JOB["id"]}}},
    ]
    stdin = "".join(json.dumps(message) + "\n" for message in messages)
    result = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True, text=True,
                            timeout=30, env=_environment(fake_faxbot.url))
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    replies = [json.loads(line) for line in lines]
    assert all(reply.get("jsonrpc") == "2.0" for reply in replies)
    assert {reply.get("id") for reply in replies} >= {1, 2, 3}
