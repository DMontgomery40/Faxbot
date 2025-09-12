---
layout: default
title: Images and PDFs (Conversion)
nav_order: 40
permalink: /IMAGES_AND_PDFS.html
---

# Images vs Text PDFs

Most real‑world faxes in 2025 are images — scans of insurance cards, lab results, driver’s licenses, or signed forms. Faxbot supports two content types only: PDF and TXT. This guide explains how to handle image content correctly and how to balance file size with HIPAA concerns.

Quick guidance
- If you have a PDF that is a scan/photo (image‑based): send it as‑is. Use `send_fax` with `filePath` (stdio) or upload the PDF via the REST API/SDKs.
- If you have a pure text document: paste text into a `.txt` file and send as TXT, or export as a PDF and send that.
- If you have images (PNG/JPG): convert to PDF first, then send.

Conversions
- macOS Preview: File → Export As… → Format: PDF
- macOS CLI: `sips -s format pdf "in.png" --out "out.pdf"`
- Linux: `img2pdf in.png -o out.pdf` or `magick convert in.png out.pdf`
- Windows: open image → Print → “Microsoft Print to PDF”

MCP tooling: which option to use?
- `send_fax` with `filePath` (stdio) — recommended for all PDFs (image or text). Sends the original file.
- HTTP/SSE MCP transports — require base64; keep files small (≤ ~100 KB), or prefer stdio with `filePath` for local usage.

File size and quality
- API default limit is `MAX_FILE_SIZE_MB=10`.
- For scans, use 150–200 DPI and grayscale when possible to reduce size without losing readability.
- For personal/one‑off use (patients): local stdio MCP with `filePath` avoids base64 bloat; token limits don’t apply.

HIPAA considerations
- Healthcare providers (Covered Entities/Business Associates): use HIPAA‑aligned transports and controls.
  - Prefer OAuth‑protected SSE MCP for assistant integrations or use the REST API/SDKs directly.
  - Use HTTPS for `PUBLIC_API_URL` (Phaxio) and enable signature verification.
- Patients (sending their own documents): HIPAA obligations differ; using local stdio MCP is generally acceptable because you are not a covered entity and Faxbot does not receive faxes. Providers still must protect inbound faxes on their side.

Troubleshooting
- “Unsupported file type”: only PDF and TXT are accepted. Convert images to PDF.
- Shell path issues with spaces/odd characters: quote the entire path or rename using a wildcard (e.g., `cp Screenshot*.pdf card.pdf`).
