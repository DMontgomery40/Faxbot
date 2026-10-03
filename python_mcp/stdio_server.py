"""Faxbot MCP stdio server (Python).

For local assistants that launch the server as a subprocess. The stdio server
is one integration identity: every tool call uses ``API_KEY`` from the
environment. Diagnostics go to stderr; stdout carries only JSON-RPC.

Environment: FAX_API_URL (default http://localhost:8080), API_KEY.
Run: python stdio_server.py
"""
from mcp.server.mcpserver import MCPServer

if __package__:
    from .faxbot_tools import SERVER_NAME, SERVER_VERSION, environment_configuration, register_tools
else:
    from faxbot_tools import SERVER_NAME, SERVER_VERSION, environment_configuration, register_tools


def build_server() -> MCPServer:
    configuration = environment_configuration()
    server = MCPServer(SERVER_NAME, version=SERVER_VERSION, log_level='WARNING')
    return register_tools(server, lambda _ctx: configuration, local_files=True)


mcp = build_server()


def main() -> None:
    mcp.run('stdio')


if __name__ == '__main__':
    main()
