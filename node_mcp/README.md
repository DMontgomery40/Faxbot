# Faxbot Node MCP (OCR Workflow)

Node-based MCP servers for Faxbot with smart PDF-to-text extraction to avoid base64 token limits.

Features:
- Stdio, Streamable HTTP, and SSE+OAuth transports
- send_fax and get_fax_status tools (backward compatible)
- faxbot_pdf prompt: extracts PDF text locally, sends as TXT fax

## Install

From repo root:

```
cd node_mcp
npm install
```

Environment variables:
- `FAX_API_URL` (default `http://localhost:8080`)
- `API_KEY` (Faxbot API key)
- `MAX_TEXT_SIZE` (default `100000` bytes; extraction truncates with warning)
- SSE only: `OAUTH_ISSUER`, `OAUTH_AUDIENCE`, optional `OAUTH_JWKS_URL`

## Run

```
# Stdio
./scripts/start-stdio.sh

# HTTP (Streamable)
./scripts/start-http.sh

# SSE + OAuth
./scripts/start-sse.sh
```

## Prompts

- `faxbot_pdf(pdf_path, to, header_text?)`
  - Extracts PDF text locally and sends as a text fax.
  - Returns a confirmation message and job ID.

## Tools

- `send_fax(to, fileContent(base64), fileName, fileType?)`
- `get_fax_status(jobId)`

## Notes

- Existing `/api` MCP servers remain as fallback. These servers are the new default target for OCR workflows.
- The OCR path avoids including base64 file bytes in the LLM conversation.
