"""
PDF text extraction and optional OCR for Faxbot (Python MCP).

Strategies:
1) Try text extraction via pdfminer.six.
2) If text is empty or very short, attempt OCR if pytesseract and rasterization are available.

OCR requirements (optional):
- pytesseract (Python) and Tesseract OCR binary installed on system
- pypdfium2 (Python) to render PDF pages to images (avoids system poppler)
- Pillow (PIL) for image handling

Environment flags:
- FAXBOT_OCR_ENABLE=true|false (default: false) — allow OCR fallback when text extraction is insufficient
- FAXBOT_OCR_DPI=integer (default: 200) — rasterization DPI for OCR
"""
from __future__ import annotations

import os
from typing import Tuple


def _normalize_whitespace(text: str) -> str:
  if not text:
    return ""
  t = text.replace('\r\n', '\n').replace('\r', '\n')
  t = t.replace('\t', ' ')
  # Trim trailing spaces per line
  t = '\n'.join(line.rstrip(' \f\v') for line in t.split('\n'))
  # Collapse 3+ newlines into 2
  while '\n\n\n' in t:
    t = t.replace('\n\n\n', '\n\n')
  # Collapse multiple spaces
  while '  ' in t:
    t = t.replace('  ', ' ')
  return t.strip()


def _try_pdfminer(file_path: str) -> str:
  try:
    from pdfminer.high_level import extract_text
  except Exception as e:  # pragma: no cover
    raise RuntimeError("pdfminer.six is not installed; cannot extract PDF text") from e
  try:
    text = extract_text(file_path) or ""
    return _normalize_whitespace(text)
  except Exception as e:
    raise RuntimeError(f"pdfminer extraction failed: {e}")


def _available(cmd: str) -> bool:
  from shutil import which
  return which(cmd) is not None


def _try_ocr(file_path: str, dpi: int = 200) -> str:
  # Optional OCR pipeline using pypdfium2 + pytesseract
  try:
    import pytesseract  # type: ignore
  except Exception as e:  # pragma: no cover
    raise RuntimeError("pytesseract not installed; cannot run OCR") from e

  if not (_available('tesseract') or os.getenv('TESSERACT_CMD')):
    raise RuntimeError("Tesseract binary not found; install tesseract or set TESSERACT_CMD")

  try:
    import pypdfium2 as pdfium  # type: ignore
  except Exception as e:  # pragma: no cover
    raise RuntimeError("pypdfium2 not installed; cannot rasterize PDF for OCR") from e

  try:
    images_text = []
    pdf = pdfium.PdfDocument(file_path)
    page_indices = list(range(len(pdf)))
    # render_to: PIL is supported via get_bitmap().to_pil()
    for i in page_indices:
      page = pdf[i]
      # scale by DPI (72 default user space). Simple factor dpi/72.
      scale = max(1.0, float(dpi) / 72.0)
      bitmap = page.render(scale=scale).to_bitmap()
      pil_img = bitmap.to_pil()
      text = pytesseract.image_to_string(pil_img) or ""
      images_text.append(text)
    combined = _normalize_whitespace('\n\n'.join(images_text))
    return combined
  except Exception as e:
    raise RuntimeError(f"OCR failed: {e}")


def extract_text_from_pdf(file_path: str) -> Tuple[str, bool]:
  """Extract text from a PDF. Returns (text, used_ocr).

  If FAXBOT_OCR_ENABLE=true and initial extraction is empty/low content,
  attempt OCR fallback.
  """
  text = _try_pdfminer(file_path)
  if len(text) >= 32:  # heuristic: enough text found
    return text, False

  # Optional OCR fallback
  if os.getenv('FAXBOT_OCR_ENABLE', 'false').lower() in {'1', 'true', 'yes', 'on'}:
    dpi = int(os.getenv('FAXBOT_OCR_DPI', '200'))
    try:
      ocr_text = _try_ocr(file_path, dpi=dpi)
      if len(ocr_text) > len(text):
        return ocr_text, True
    except Exception:
      # Swallow OCR errors; return best-effort text
      pass
  return text, False

