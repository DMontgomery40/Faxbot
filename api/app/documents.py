"""Prepare untrusted uploads before accepting a fax job.

Only generated job identities determine disk paths. Conversion runs in private
staging storage; final artifacts are published without replacing existing data.
The caller owns cleanup until its job has been durably accepted.
"""

import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from .conversion import FAX_IMAGE_MODE, DocumentConversionError, pdf_to_tiff, txt_to_pdf, validate_pdf


class UploadPreparationError(Exception):
    """A sanitized HTTP-facing error from document preparation."""

    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _cleanup_paths(paths: Iterable[Path]) -> None:
    failed = False
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            failed = True
    if failed:
        raise UploadPreparationError("Document cleanup failed.", status_code=503) from None


@dataclass(frozen=True)
class PreparedDocument:
    original_name: str
    pdf_path: str
    tiff_path: str | None
    pages: int

    def cleanup(self) -> None:
        """Remove this preparation's artifacts when acceptance is ruled out."""
        _cleanup_paths(Path(name) for name in (self.pdf_path, self.tiff_path) if name is not None)


def _display_name(filename: str | None) -> str:
    name = (filename or "document").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    return name[:200].strip().strip(".") or "document"


def _convert(source: Path, pdf: Path, tiff: Path | None, is_pdf: bool) -> int:
    if is_pdf:
        pages = validate_pdf(str(source))
        source.replace(pdf)
    else:
        txt_to_pdf(str(source), str(pdf))
        pages = validate_pdf(str(pdf))
    if tiff is not None:
        pdf_to_tiff(str(pdf), str(tiff))
    return pages


async def prepare_upload(
    upload: UploadFile, *, job_id: str, data_dir: str, max_bytes: int,
    requires_tiff: bool,
) -> PreparedDocument:
    """Stream, validate and prepare a PDF/TXT upload independently of its name.

    Errors contain no source filename, filesystem path or external tool output.
    Invalid input is 400/415, oversized input 413, operational failures 503.
    The request owns the UploadFile; this function owns its staging artifacts.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or max_bytes <= 0:
        raise UploadPreparationError("Document preparation is unavailable.", status_code=503)

    published: list[Path] = []
    complete = False
    is_pdf = False
    try:
        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".faxbot-upload-", dir=root) as staging:
            stage = Path(staging)
            source, pdf = stage / "source", stage / "document.pdf"
            tiff = stage / "document.tiff" if requires_tiff else None
            total = 0
            header = b""
            with source.open("xb") as output:
                while chunk := await upload.read(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise UploadPreparationError("File exceeds the configured upload limit.", status_code=413)
                    if len(header) < 4:
                        header = (header + chunk)[:4]
                    output.write(chunk)
            if total == 0:
                raise UploadPreparationError("Document is empty.", status_code=400)
            is_pdf = header == b"%PDF"

            # CPU work and bounded external processes must not block the ASGI loop.
            pages = await run_in_threadpool(_convert, source, pdf, tiff, is_pdf)
            final_pdf = root / f"{job_id}.pdf"
            final_tiff = root / f"{job_id}.tiff" if tiff is not None else None
            for staged, final in ((pdf, final_pdf), (tiff, final_tiff)):
                if staged is not None and final is not None:
                    # Asterisk (its own group on the data folder) reads the pages it sends; nobody else.
                    staged.chmod(FAX_IMAGE_MODE if staged is tiff else 0o600)
                    # Hard-link publication is atomic and refuses identity collisions.
                    os.link(staged, final)
                    published.append(final)
            prepared = PreparedDocument(
                original_name=_display_name(upload.filename), pdf_path=str(final_pdf),
                tiff_path=str(final_tiff) if final_tiff else None, pages=pages,
            )
        complete = True
        return prepared
    except UploadPreparationError:
        raise
    except DocumentConversionError as error:
        status = 503 if error.operational else (400 if is_pdf else 415)
        raise UploadPreparationError(str(error), status_code=status) from None
    except (OSError, ValueError):
        raise UploadPreparationError("Document preparation failed.", status_code=503) from None
    finally:
        if not complete:
            _cleanup_paths(published)
