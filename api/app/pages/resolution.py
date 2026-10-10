"""Lossless resolution matching: a document that is really standard resolution goes at standard resolution.

Faxbot renders every PDF at 204 x 196 dots per inch (fine). A page that came
from a standard-resolution fax (a received fax forwarded, or turned into a PDF
and uploaded again) then has every pair of rows identical: Ghostscript drew
each standard row twice. Sending those pairs doubles the lines, and with them
the time the pages take on a per-minute route, for no extra detail.

When every page of the document is fine resolution with an even number of
rows and each row pair identical, Faxbot keeps one row of each pair and sends
the document at 204 x 98: the receiver gets the same pixels at the same size,
and every G3 machine takes standard resolution. A document with any page that
is really fine stays fine (whole document only: changing resolution between
pages is not reliable on every engine). Nothing else about the pages changes.
"""
from __future__ import annotations

FINE_MIN_DPI = 150


def _y_dpi(frame):
    dpi = frame.info.get('dpi') or (0, 0)
    try:
        return float(dpi[1])
    except (TypeError, ValueError, IndexError):
        return 0.0


def is_standard(frames):
    """Whether these fax pages are standard resolution (fewer than 150 lines per inch)."""
    return bool(frames) and all(0 < _y_dpi(frame) < FINE_MIN_DPI for frame in frames)


def standard_frames(frames):
    """The same pages at standard resolution when every page's rows come in identical pairs; else None."""
    from PIL import Image
    if not frames or any(frame.mode != '1' or _y_dpi(frame) < FINE_MIN_DPI or frame.height % 2 for frame in frames):
        return None
    from ..conversion import FaxFrames
    halved = FaxFrames() if isinstance(frames, FaxFrames) else []  # packed pages stay packed
    for frame in frames:
        data, stride = frame.tobytes(), (frame.width + 7) // 8
        rows = []
        for pair in range(0, frame.height, 2):
            upper = data[pair * stride:(pair + 1) * stride]
            if upper != data[(pair + 1) * stride:(pair + 2) * stride]:
                return None
            rows.append(upper)
        image = Image.frombytes('1', (frame.width, frame.height // 2), b''.join(rows))
        x_dpi = float((frame.info.get('dpi') or (204, 0))[0])
        image.info['dpi'] = (x_dpi, _y_dpi(frame) / 2)
        halved.append(image)
    return halved
