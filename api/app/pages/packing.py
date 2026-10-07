"""Stack whole original pages onto long fax pages, as long as the receiving machine allows.

Deterministic and pixel exact: each original keeps every pixel it had after
the usual fax conversion (no scaling, no reflow); narrower originals sit at
the left edge of a page as wide as the widest one. Every packed page starts
with a white margin (the sender's header line is printed there: HylaFAX
writes it over the top rows, spandsp adds its rows above the page) and then,
before each original, a band with a faint dotted rule and a "page 2 of 5" tag
(``marks``). Pages are cut at original page boundaries. A single original is
split only when it is longer than the receiver's limit (the owner's "chop it
up"); its pieces are cut at a white row near the limit when there is one, and
each piece after the first starts with a "continued" band.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from PIL import Image

from . import marks

MM_PER_INCH = 25.4
# The longest page T.30 lets a machine accept (DIS bits 19-20). "Unlimited" is capped at one metre:
# 3 letter pages a sheet, well inside Faxbot's 25-megapixel page limit and PDF's 200-inch page limit.
LIMITS_MM = {'a4': 297.0, 'b4': 364.0, 'unlimited': 1000.0}
# White rows at the top of each packed page: the sender's header line (HylaFAX images about 24 rows of
# its tag line over the top of a fine page) never touches a band.
TOP_ROWS = 48
# spandsp (the built-in engine) adds 16 header rows, twice at fine resolution, above every page it sends.
HEADER_ROWS = 32
# When an original must be split, look this far above the cut for an all-white row (about 8 mm).
CUT_SEARCH_ROWS = 64
# Standard (3.85 lines/mm) and fine pages both pack; the band is drawn in rows, so it reads back exactly.
MIN_Y_DPI = 90


class NotPackable(ValueError):
    """These pages cannot be packed (resolution, width or size); send them as they are."""


@dataclass(frozen=True)
class Piece:
    original: int  # 0-based original page
    top: int  # first row of the original in this piece
    rows: int
    kind: int  # marks.START or marks.CONTINUES


@dataclass
class Sheet:
    pieces: list = field(default_factory=list)

    def height(self):
        return TOP_ROWS + sum(marks.BAND_ROWS + piece.rows for piece in self.pieces)


@dataclass(frozen=True)
class Layout:
    sheets: tuple
    width: int
    limit_rows: int
    originals: int

    @property
    def pages(self):
        return len(self.sheets)


def limit_rows(limit, y_dpi):
    """Rows a page may have under ``limit`` ('a4', 'b4' or 'unlimited') at ``y_dpi``, after the header rows."""
    if limit not in LIMITS_MM or not y_dpi or y_dpi < MIN_Y_DPI:
        raise NotPackable('Unsupported page limit or resolution')
    return math.floor(LIMITS_MM[limit] * y_dpi / MM_PER_INCH) - HEADER_ROWS


def plan(heights, width, rows, *, white_rows=None):
    """The layout of originals ``heights`` (rows each) on pages of at most ``rows`` rows.

    ``white_rows(original)`` returns the set of all-white rows of that original, used only to choose
    where a too-long original is cut; without it the cut falls exactly at the limit.
    """
    if not heights or any(height <= 0 for height in heights):
        raise NotPackable('No pages to pack')
    if width < marks.MIN_WIDTH:
        raise NotPackable('Pages are too narrow to mark')
    room = rows - TOP_ROWS - marks.BAND_ROWS  # the most of one original a single page holds
    if room < marks.BAND_ROWS:
        raise NotPackable('The page limit is too short')
    sheets, current = [], Sheet()
    for original, height in enumerate(heights):
        if height <= room:
            if current.pieces and current.height() + marks.BAND_ROWS + height > rows:
                sheets.append(current)
                current = Sheet()
            current.pieces.append(Piece(original, 0, height, marks.START))
            continue
        # Longer than any page the receiver takes: it starts on a page of its own and is cut where the
        # limit falls, at a white row just above the limit when there is one.
        if current.pieces:
            sheets.append(current)
            current = Sheet()
        blank = white_rows(original) if white_rows is not None else set()
        top, kind = 0, marks.START
        while height - top > room:
            cut = top + room
            for row in range(cut, max(top + room - CUT_SEARCH_ROWS, top + 1) - 1, -1):
                if row in blank:
                    cut = row
                    break
            current.pieces.append(Piece(original, top, cut - top, kind))
            sheets.append(current)
            current = Sheet()
            top, kind = cut, marks.CONTINUES
        current.pieces.append(Piece(original, top, height - top, kind))
    if current.pieces:
        sheets.append(current)
    return Layout(tuple(sheets), width, rows, len(heights))


def _white_rows(frame):
    data, stride = frame.tobytes(), (frame.width + 7) // 8
    full = frame.width // 8
    rest = frame.width % 8
    white = set()
    for row in range(frame.height):
        line = data[row * stride:(row + 1) * stride]
        if line[:full].count(0xFF) == full and (not rest or line[full] | (0xFF >> rest) == 0xFF):
            white.add(row)
    return white


def check_frames(frames):
    """(width, (x_dpi, y_dpi)) shared by every frame, or NotPackable."""
    if not frames:
        raise NotPackable('No pages to pack')
    resolutions = {_dpi(frame) for frame in frames}
    if len(resolutions) != 1:
        raise NotPackable('Pages have different resolutions')
    x_dpi, y_dpi = resolutions.pop()
    if y_dpi < MIN_Y_DPI or any(frame.mode != '1' for frame in frames):
        raise NotPackable('Pages are not fine-resolution fax images')
    return max(frame.width for frame in frames), (x_dpi, y_dpi)


def _dpi(frame):
    dpi = frame.info.get('dpi')
    if not dpi:
        raise NotPackable('Pages have no resolution')
    return tuple(round(float(value)) for value in dpi)


def layout_for(frames, limit):
    """The layout of these fax frames (mode "1") under the receiver's ``limit``."""
    width, (_, y_dpi) = check_frames(frames)
    rows = limit_rows(limit, y_dpi)
    blanks = {}

    def white_rows(original):
        if original not in blanks:
            blanks[original] = _white_rows(frames[original])
        return blanks[original]
    return plan([frame.height for frame in frames], width, rows, white_rows=white_rows)


def render(frames, layout):
    """The packed pages (mode "1" images, with the frames' resolution), one per sheet of ``layout``."""
    width, dpi = check_frames(frames)
    if width != layout.width or len(frames) != layout.originals:
        raise NotPackable('The layout does not match these pages')
    total = len(frames)
    pages = []
    for sheet in layout.sheets:
        image = Image.new('1', (width, sheet.height()), 1)
        y = TOP_ROWS
        for piece in sheet.pieces:
            frame = frames[piece.original]
            record = marks.Record(piece.kind, piece.original + 1, total, frame.width)
            image.paste(marks.band(record, width), (0, y))
            y += marks.BAND_ROWS
            image.paste(frame.crop((0, piece.top, frame.width, piece.top + piece.rows)), (0, y))
            y += piece.rows
        image.info['dpi'] = dpi
        pages.append(image)
    return pages
