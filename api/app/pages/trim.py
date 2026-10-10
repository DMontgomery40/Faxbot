"""Leave out the blank bottom of pages Faxbot rendered itself, for receiving machines without error correction.

Without error correction (ECM) every scan line costs at least the receiving
machine's minimum scan line time (T.30 Table 2 bits 21-23: up to 40 ms at
standard resolution, 20 ms at fine), even a white one: 2,156 fine lines at
10 ms is over 20 seconds a page. With error correction a white line costs a
few bits, so nothing is trimmed.

Only pages Faxbot rendered from a PDF or text page that draws no image are
trimmed; a scan, a picture or a fax image someone sent stays exactly as it
is. A trimmed page keeps every row down to its last non-white row, plus a
small white margin; the page count and order never change.
"""
from __future__ import annotations

from PIL import Image

# White rows kept under the last non-white row (about 2 mm at fine resolution).
MARGIN_ROWS = 16
# Trim only when this many rows or more would go (about 4 mm at fine resolution).
MIN_TRIM_ROWS = 32
FORM_DEPTH = 3


def trailing_white_rows(frame):
    data, stride = frame.tobytes(), (frame.width + 7) // 8
    full, rest = frame.width // 8, frame.width % 8
    count = 0
    for row in range(frame.height - 1, -1, -1):
        line = data[row * stride:(row + 1) * stride]
        if line[:full].count(0xFF) != full or (rest and line[full] | (0xFF >> rest) != 0xFF):
            break
        count += 1
    return count


def trim_frames(frames, trimmable):
    """(frames, pages trimmed, rows left out): each trimmable page loses its blank bottom, keeping
    ``MARGIN_ROWS`` white rows (a blank page keeps that many rows); every other row stays exactly."""
    if len(trimmable) != len(frames):
        raise ValueError('Say for every page whether it may be trimmed')
    from ..conversion import FaxFrames
    # Packed pages stay packed (conversion.FaxFrames): a long fax's pages would take gigabytes as images.
    result, pages, rows = (FaxFrames() if isinstance(frames, FaxFrames) else []), 0, 0
    for frame, allowed in zip(frames, trimmable):
        white = trailing_white_rows(frame) if allowed else 0
        drop = min(white - MARGIN_ROWS, frame.height - MARGIN_ROWS) if white > MARGIN_ROWS else 0
        if drop < MIN_TRIM_ROWS:
            result.append(frame)
            continue
        kept = frame.crop((0, 0, frame.width, frame.height - drop))
        kept = Image.frombytes('1', kept.size, kept.tobytes())
        kept.info['dpi'] = frame.info.get('dpi')
        result.append(kept)
        pages += 1
        rows += drop
    return result, pages, rows


def _draws_image(resources, depth=0):
    """Whether a page or form's resources hold an image (or a form that does, or might)."""
    from pypdf.generic import DictionaryObject
    resources = resources.get_object() if resources is not None else None
    if not isinstance(resources, DictionaryObject):
        return False
    objects = resources.get('/XObject')
    objects = objects.get_object() if objects is not None else None
    if not isinstance(objects, DictionaryObject):
        return False
    for name in objects:
        item = objects[name].get_object()
        subtype = item.get('/Subtype') if hasattr(item, 'get') else None
        if subtype == '/Image':
            return True
        if subtype == '/Form' and (depth >= FORM_DEPTH or _draws_image(item.get('/Resources'), depth + 1)):
            return True
    return False


def rendered_pages(pdf_path):
    """For each page of a validated PDF: True when Faxbot drew it from text or shapes only (it may be trimmed),
    False when it draws any image (a scan, a picture, a received fax). None when the PDF cannot be read."""
    from pypdf import PdfReader
    from pypdf.generic import ContentStream
    try:
        with open(pdf_path, 'rb') as source:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                return None
            result = []
            for page in reader.pages:
                if _draws_image(page.get('/Resources')):
                    result.append(False)
                    continue
                content = page.get('/Contents')
                inline = False
                if content is not None:
                    inline = any(operator in (b'INLINE IMAGE', b'BI')
                                 for _, operator in ContentStream(content, reader).operations)
                result.append(not inline)
            return result
    except Exception:
        return None
