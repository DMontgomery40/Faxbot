# Document integrity verification — October 2, 2026

Status: implemented and independently approved at `6a0afbba`. This completed slice is part of the active full refresh; production release gates remain open.

## Source identities

- Conversion implementation: fa9ba8ce.
- Independent review correction for high-bit-depth TIFF clipping: 5d36d6f0; independently approved.
- Upload/API integration: 47a34e65.
- Final reviewed correction: 6a0afbba1befd99b0cde509e15df903d81283dc0.
- Independent verdict: spec compliant and task quality approved; no remaining Critical or Important findings in this slice.
- Integration branch: feat/faxbot-refresh.

## Verified behavior

Real TXT/PDF/TIFF contents replace all conversion and outbound-upload placeholders. Filenames and FAX_DISABLED never select fake content. Long lines wrap and paginate without truncation. Supported text uses an embedded bundled font, with explicit errors for unsupported encoding/glyphs. TIFF frame order and supported pixel/alpha contents are independently inspected; high-bit-depth modes that would silently lose detail are rejected before decode.

Uploads are streamed into private generated staging paths. Only the generated job identity determines final filenames. Original names are safe, bounded display metadata. Original valid PDF bytes survive misleading filename extensions and Content-Type headers unchanged. Invalid/empty/unsupported/oversized inputs fail before job creation and leave no artifacts. Missing rendering tools and operational failures return sanitized errors. Atomic publication refuses to overwrite prior artifacts on identity collision.

Preparation finishes before persistence. Actual page count is stored. Failures before any commit attempt clean prepared artifacts. After a commit attempt, an immediate missing-row result cannot prove rejection: missing or unavailable confirmation retains artifacts and returns a sanitized uncertainty error with the job identity. Confirmed existing jobs retain their files and continue acceptance. Cleanup attempts every owned path before reporting a sanitized failure. Durable job recovery/idempotency is a separate required subsystem still to be implemented.

## Tests and actual artifact observations

- Final API test run at revision `6a0afbba`: **109 passed**, zero skips, four existing FastAPI on_event deprecation warnings, 9.34 seconds. Explicit environment allowlist, new temporary SQLite/data directory, external transmission disabled.
- Focused document/conversion/affected-Phaxio run: **81 passed**.
- Regression failures observed before fixes: bogus contest.txt output, lost content under disabled-send mode, incorrect/missing page count, misleading PDF filename interpretation, accepted malformed files, unsafe/pathological filenames, and I;16 TIFF saturation.
- Three final-review regression failures reproduced before correction: premature deletion after an uncertain commit, retained files before any commit attempt, and raw cleanup errors. All 25 upload tests passed after correction.
- Independent loopback Uvicorn HTTP check repeated at exact final revision `6a0afbba`: synthetic contest.txt upload 202 and authorized download 200, all 131 original lines intact, PDF 3 pages, actual Group4 TIFF 3 frames, malformed PDF 400, unauthorized download 401. Synthetic job: `f47ebd89c18b40b6b950857a8849fbed`.
- Primary rendered downloaded PDF pages 1 and 3 using Ghostscript and visually inspected legibility, pagination and the final accented marker.
- Real tools: Python 3.11, Ghostscript 10.08.0, pypdf 6.19.0, Pillow 12.3.0. No external fax was sent.
- diff checks and compileall passed. No configured API type-check command was present; no type-check success is claimed.

Scratch reports, fixed review packages, exact test logs and synthetic PDF/PNG artifacts are retained under `.superpowers/sdd/2026-10-02-faxbot-document-integrity/` in the integration worktree. They are not production document data.

## Explicit supported boundaries

Text is strict UTF-8, rendered left to right using bundled Vera glyph coverage. CR/LF line endings normalize; tabs expand to eight-column stops. Unsupported glyphs, combining/RTL/control sequences and empty/whitespace-only text fail explicitly. No claim of full multilingual typesetting is made.

PDF checks are bounded structural/content-stream parsing, not complete PDF graphics-semantic certification. Actual TIFF generation uses Ghostscript with safe argv, PDFSTOPONERROR, a 120-second timeout, output validation and frame-count comparison. Original cloud-provider PDFs remain byte-identical.

Limits: HTTP-configured upload size (default 10 MiB); converter TXT/TIFF input 32 MiB; validated/generated PDF/output 64 MiB; 500 pages subject to stricter raster/content limits; 25M pixels/frame and 100M total pixels. The conservative geometry preflight permits 26 standard letter fax pages. Actual fax TIFF width follows Ghostscript's documented standard-width adjustment. These limits reject unsupported inputs rather than truncate them.

## Separate release gates

This evidence does not establish CI publication, production installation/upgrade, provider delivery, physical receipt, complete RBAC, client/TestFlight acceptance, automatic main-commit docs publication, or recipient handoff readiness. Those remain mandatory in the full approved task graph.

## Runtime foundation finding

The actual `api/Dockerfile` built from tracked revision `47a34e65` into local image `564fe7680958`, but that image cannot start the API. Its second, unbounded MCP dependency installation combines Starlette 1.7.0 with FastAPI 0.112.2 (which requires Starlette <0.39). Container `pip check` fails; importing `app.main` raises `Router.__init__() got an unexpected keyword argument on_startup`. This is a confirmed pre-existing release blocker, now the next implementation priority. A successful Docker build is not startup or deployment proof.
