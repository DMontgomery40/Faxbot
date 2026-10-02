# Faxbot Python MCP runtime

Requires Python 3.11 or newer. The supported MCP 1.x maintenance SDK preserves the existing stdio, SSE and Streamable HTTP tool interfaces.

From the repository root, install the API and MCP requirements in one resolver transaction:

```sh
python -m venv /tmp/faxbot-runtime
/tmp/faxbot-runtime/bin/python -m pip install -r api/requirements.txt -r python_mcp/requirements.txt
/tmp/faxbot-runtime/bin/python -m pip check
```

For a standalone MCP environment, use `pip install -r python_mcp/requirements.txt`, or install the package with `pip install -c python_mcp/runtime-constraints.txt ./python_mcp`. The package exposes `faxbot-mcp-py-stdio`, `faxbot-mcp-py-http` (port 3004) and `faxbot-mcp-py-sse` (port 3003); `PORT` overrides the HTTP/SSE port. Set `FAX_API_URL` and `API_KEY` for the backend. The standalone SSE wrapper requires OAuth JWT configuration (`OAUTH_ISSUER`, `OAUTH_AUDIENCE`, and optional `OAUTH_JWKS_URL`).

Embedded API transports are enabled with `ENABLE_MCP_HTTP=true` or `ENABLE_MCP_SSE=true`. With default prefixes, protocol URLs are `/mcp/http/mcp` and `/mcp/sse/sse`; prefix overrides retain the SDK endpoint suffixes. Enabled transports are required startup dependencies. The API explicitly owns their lifespans.

`runtime-constraints.txt` pins the combined Python dependency resolution. Constraints do not install packages: platform/extra dependencies still follow package metadata. To update it, choose compatible stable direct versions from official package metadata, update both requirement declarations and project metadata together, then resolve both requirements in one transaction in **fresh Python 3.11 Linux and macOS environments**. During regeneration omit the existing constraints with temporary copies of the requirements; freeze the union of both resolutions into the constraint file (omit pip/setuptools/wheel). Retain environment markers for any platform-exclusive packages. Check both environments with `pip check`, run the complete API/runtime suite, build `api/Dockerfile`, and verify actual HTTP and MCP handshakes before accepting the update. Do not freeze an unrelated developer environment.
