# Faxbot Document Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development for the bounded tasks below; the primary agent owns architectural and cross-task decisions. Steps use checkbox syntax for tracking.

**Goal:** Every accepted PDF/TXT and converted TIFF preserves supported source contents; filenames and disabled sending never select fake output, and invalid/unconvertible documents fail honestly.

**Architecture:** Keep the existing conversion interface while replacing its placeholder and silent-data-loss implementation. Centralize HTTP document preparation behind one module and integrate it into the existing send route without altering the successful public response contract. This is the first complete implementation slice of the full refresh, not release completion.

**Tech Stack:** Python 3.11, FastAPI, ReportLab, pypdf 6.19.0, Pillow 12.3.0, Ghostscript for PDF-to-fax-TIFF rasterization. pypdf and Pillow versions verified against official PyPI metadata on October 2, 2026.

**Spec:** `docs/superpowers/specs/2026-10-02-faxbot-refresh-design.md` (incorporating complete RBAC, permitted UI improvements, and TestFlight compatibility).

## Global Constraints

- Preserve successful existing public HTTP request/response shapes; no UI rebrand.
- FAX_DISABLED suppresses external fax transmission only; it never substitutes document contents.
- No filename or filesystem path selects test behavior.
- No placeholder, empty artifact, or malformed PDF/TIFF can count as successful conversion.
- Conversion errors are explicit, sanitized, and clean partial output.
- No real fax sends, provider credentials, production data, or external publication in these tasks.
- Use `gpt-6.1-sol` at `xhigh` for bounded implementation and independent review; primary agent handles context-heavy integration decisions.
- Tests prove behavior through conversion and HTTP interfaces. Real document inspection is independent of the implementation.
- The complete refresh, including RBAC and all release gates, remains open after this slice.

## Review Focus

1. Filenames such as contest.txt, nested test directories, and FAX_DISABLED=true must preserve actual content. Task 1 owns these tests.
2. Long lines and page overflow must preserve the final words and produce correct page counts. Task 1 owns these tests.
3. Invalid UTF-8, unsupported glyphs, encrypted/broken PDF, and corrupt TIFF must fail explicitly rather than lose information. Tasks 1 and 2 own these tests.
4. Conversion-tool absence, timeout, nonzero exit, and partial output must not report success or leak filesystem/command details. Task 1 owns these tests.
5. Upload filename traversal, misleading extension/content-type, empty files and conversion failure must not escape the data directory or leave accepted jobs/partial artifacts. Task 2 owns these tests.

## File responsibilities

- `api/app/conversion.py`: real format conversion/validation and a sanitized conversion exception; preserves existing caller signatures.
- `api/app/documents.py` (new): upload preparation, validated artifact paths, PDF metadata, lifecycle cleanup for unaccepted uploads.
- `api/app/main.py`: existing send HTTP route delegates document preparation and maps errors; no unrelated route refactor in this slice.
- `api/requirements.txt`: explicit pinned document-inspection dependencies.
- `api/tests/test_conversion.py`, `api/tests/test_documents.py` (new): behavior tests at conversion and HTTP interfaces.
- Existing API tests: replace invalid placeholder fixtures with real generated documents where required; do not weaken expectations.

### Task 1: Real, content-preserving document conversion

**Files:** modify `api/app/conversion.py`, `api/requirements.txt`; create `api/tests/test_conversion.py`; update invalid fixture construction in affected API tests only.

**Interfaces:**
- Retain `txt_to_pdf(txt_path: str, pdf_path: str) -> None`.
- Retain `pdf_to_tiff(pdf_path: str, tiff_path: str) -> tuple[int, str]`.
- Retain `tiff_to_pdf(tiff_path: str, pdf_path: str) -> tuple[int, str]`.
- Retain `count_pdf_pages(pdf_path: str) -> int | None`; return a real count or an explicit inability to count, never invent one.
- Add `DocumentConversionError(Exception)` for a safe user-facing conversion failure.
- Add `validate_pdf(pdf_path: str) -> int`, returning the real positive page count or raising DocumentConversionError.

- [ ] Write and run a failing test that writes `contest.txt` containing `First line\nFinal clinical billing marker`, calls txt_to_pdf with FAX_DISABLED both true and false, and uses pypdf to assert both original lines occur in the output. Repeat with a parent directory containing `test`.
- [ ] Replace environment/path-selected stubs with real conversion. Decode text strictly. Preserve line contents with wrapping and pagination rather than truncating at 120 characters. Use a real embedded font for supported Unicode and reject unsupported characters explicitly rather than render silent replacement glyphs; do not depend on host-specific font installation. A bundled ReportLab font may be used if its glyph coverage is checked. Record the supported text contract.
- [ ] Add a failing long-line/multipage test with a literal final marker beyond the former truncation limit; implement until parsed output retains all content and correct page count. Add supported accented text and invalid UTF-8/unsupported-character rejection tests.
- [ ] Add invalid/encrypted/zero-page PDF and corrupt TIFF tests. Implement validate_pdf and accurate page counting with pypdf; no success for magic-header-only files.
- [ ] Convert a real multipage TIFF through Pillow into a real PDF, preserving page order and image content; validate with independent pixel/page observations. This removes the invalid Ghostscript-TIFF-as-PostScript approach and placeholder fallback.
- [ ] Add tests for missing Ghostscript, nonzero exit, timeout, and partial raster output. Keep real Ghostscript PDF→TIFF with safe argv, explicit timeout, `-dSAFER`, validated output and page count. External-process failure tests can replace the process seam; successful conversion proof must run real Ghostscript when installed. Use temporary outputs and atomic replacement; clean partial output on failure.
- [ ] Use synthetic bounded documents. Preserve Pillow's decompression-bomb checks and reject invalid/oversized raster inputs rather than disabling its protections. Do not introduce an unbounded subprocess or whole-document allocation without an explicit limit.
- [ ] Update existing inbound fixtures that relied on TIFF_PLACEHOLDER to use a valid tiny TIFF. Run `python -m pytest tests/test_conversion.py tests/test_inbound_internal.py tests/test_freeswitch.py -q` from api, with fresh temporary DB/data directory and no external credentials.
- [ ] Run the full existing API suite once after the task. Report tests actually run, missing-tool checks, exact commit and any unresolved requirement. Commit the reviewed scope with a factual message.

### Task 2: Safe upload preparation and HTTP integration

**Blocked by:** Task 1.

**Files:** create `api/app/documents.py`, `api/tests/test_documents.py`; modify send_fax in `api/app/main.py` and affected fixtures.

**Interfaces:**
- Consume Task 1's real conversion and validation interfaces.
- Add immutable `PreparedDocument` carrying original display name, PDF path, optional TIFF path and real page count.
- Add `async prepare_upload(upload: UploadFile, *, job_id: str, data_dir: str, max_bytes: int, requires_tiff: bool) -> PreparedDocument`.
- Add explicit cleanup for an unaccepted PreparedDocument; accepted artifacts retain existing `{job_id}.pdf` / `{job_id}.tiff` retrieval compatibility.
- Existing POST /fax remains multipart `to` + `file`, HTTP 202 with existing FaxJobOut on success. Existing authentication remains in force; complete RBAC is a subsequent required slice.

- [ ] Add a failing HTTP test: submit contest.txt with synthetic content in disabled-send mode, then retrieve the job PDF through the authorized admin endpoint and assert its actual text via pypdf. Implement delegation to prepare_upload; remove the send route's independent dummy-PDF/TIFF branches.
- [ ] Add failing tests for misleading filename extensions and content types: valid PDF bytes with a .txt filename remain a valid original PDF; invalid header-only PDF is rejected; binary/invalid UTF-8 input is rejected; empty upload is rejected. Select supported content by validated bytes, not extension.
- [ ] Add failing path tests for `../../name.txt`, Windows-style traversal, very long names, and filenames containing separators/control characters. Preserve a safe display name only; generate all disk paths from the internal job identity. Assert artifacts stay within the test data directory and no outside file is created.
- [ ] Retain the configured MAX_FILE_SIZE_MB enforcement while streaming. Rejected oversized or invalid input leaves no partial artifacts and creates no accepted job. Verify that through HTTP responses, job listing, and the controlled external storage interface where necessary.
- [ ] Map invalid/unsupported document errors to clear 400/415 responses consistent with current error envelope; missing conversion tooling or internal conversion failure to a sanitized operational error rather than HTTP 202. Do not expose commands, credentials or host paths.
- [ ] Keep configured outbound selection and response fields unchanged; no provider submission is performed in these tests. Store actual page count from preparation.
- [ ] Run `python -m pytest tests/test_documents.py tests/test_conversion.py -q`, followed by the full API suite on the final task revision. Run type/static checks if configured and `git diff --check`.
- [ ] Primary agent verifies an actual uploaded/returned PDF, checks the integration diff, dispatches independent review, resolves findings, and records evidence. Commit the slice after verification.

## Preflight integration check

| Pair/task | Producer / consumer relationship | Resolution |
|---|---|---|
| Task 1 → Task 2 | conversion signatures / prepare_upload | Existing signatures retained; additive validation/exception documented above |
| Task 1 and legacy tests | invalid placeholder fixtures / real conversion | Replace fixtures with real synthetic documents; retain behavioral assertions |
| Task 2 and existing retrieval | generated PDF paths / download endpoints | Preserve job-id artifact names and response shapes |
| Task 1 internal consistency | disabled send / real conversion | Transmission flag never disables conversion; tests explicitly verify this |
| Task 2 internal consistency | validation errors / accepted jobs | Only persist a job after successful preparation; failures clean pending artifacts |

## Review and evidence

Use one writer at a time in the integration worktree. Task reviewers receive the exact task brief, implementation report, fixed base/head diff package and these global constraints. The primary agent resolves cross-task questions. End this slice with all required regression tests and review passing; do not label the overall goal complete or release it while remaining product/RBAC/docs/deployment gates are open.
