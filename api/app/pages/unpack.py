"""A receiving Faxbot splits marked long pages back into the original pages.

Faxbot finds each band's dotted rule (``marks``), keeps the rows between one
band and the next as one original (cropped back to its own width), joins a
"continued" piece to the original before it, and drops what sits above a
page's first band (the white top margin and the sender's header line). It
splits only when every page of the fax carries bands and the originals come
out complete and in order (1, 2, ... of the stated total); anything else is
delivered exactly as received. The received image itself is never changed.
"""
from __future__ import annotations

from PIL import Image

from . import marks


def _bands(frame):
    """[(first band row, record)] for one received page, top to bottom."""
    data, stride, width = frame.tobytes(), (frame.width + 7) // 8, frame.width
    found, row = [], 0
    while row < frame.height:
        record = marks.read_row(data[row * stride:(row + 1) * stride], width)
        if record is None:
            row += 1
            continue
        start = row - record.row - marks.GAP_TOP
        if start >= 0 and (not found or start >= found[-1][0] + marks.BAND_ROWS):
            found.append((start, record))
        # Skip the rest of this band: the second rule row and the tag cannot start another band.
        row = max(row + 1, start + marks.BAND_ROWS)
    return found


def split_frames(frames):
    """The original pages (mode "1" images with the received resolution), or None to deliver as received."""
    if not frames or any(frame.mode != '1' for frame in frames):
        return None
    pieces = []  # (record, image)
    for frame in frames:
        bands = _bands(frame)
        if not bands:
            return None
        for position, (start, record) in enumerate(bands):
            top = start + marks.BAND_ROWS
            bottom = bands[position + 1][0] if position + 1 < len(bands) else frame.height
            if bottom <= top or record.width > frame.width:
                return None
            piece = frame.crop((0, top, record.width, bottom))
            piece.info['dpi'] = frame.info.get('dpi')
            pieces.append((record, piece))
    total = pieces[0][0].total
    originals = []  # [index, [images]]
    for record, image in pieces:
        if record.total != total:
            return None
        if record.kind == marks.CONTINUES:
            if not originals or originals[-1][0] != record.index:
                return None
            originals[-1][1].append(image)
        elif record.index == len(originals) + 1:
            originals.append([record.index, [image]])
        else:
            return None
    if len(originals) != total or total < 2:
        return None
    result = []
    for _, images in originals:
        if len(images) == 1:
            result.append(images[0])
            continue
        joined = Image.new('1', (images[0].width, sum(image.height for image in images)), 1)
        y = 0
        for image in images:
            joined.paste(image, (0, y))
            y += image.height
        joined.info['dpi'] = images[0].info.get('dpi')
        result.append(joined)
    return result
