"""
Faxbot MCP stdio server (Python) for local assistants.

The stdio server is one integration identity: it sends the configured API_KEY
to Faxbot on every call and may read local files (filePath) or URLs (fileUrl).
Stdout carries only JSON-RPC; diagnostics go to stderr.

Usage:
    pip install -r requirements.txt
    export FAX_API_URL=http://localhost:8080
    export API_KEY=your_integration_key
    python stdio_server.py
"""
import os
import sys
from pathlib import Path

if not __package__:
    # Allow `python python_mcp/stdio_server.py` from any working directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from faxbot_tools import build_server, environment_api_url
else:
    from .faxbot_tools import build_server, environment_api_url

FAX_API_URL = environment_api_url()
API_KEY = os.getenv("API_KEY", "")

mcp = build_server(api_base_url=lambda: FAX_API_URL, stdio_api_key=API_KEY)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
