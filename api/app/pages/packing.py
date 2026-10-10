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

With error correction a page goes as partial pages of 64 KiB of coded data,
and each one past the first costs a turnaround on the line
(``coding.ecm_extra_seconds``). When the coded size of each original is known,
the originals are shared out among the same number of pages so that the pages
cross the fewest of those edges: a page that would end just over an edge
gives its last original to the next page when that page stays under one.
Fewer pages always come first, and with nothing to gain the layout is exactly
the one that fills each page in turn.
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


def plan(heights, width, rows, *, white_rows=None, page_bits=None, piece_bits=0, frame_octets=256):
    """The layout of originals ``heights`` (rows each) on pages of at most ``rows`` rows.

    ``white_rows(original)`` returns the set of all-white rows of that original, used only to choose
    where a too-long original is cut; without it the cut falls exactly at the limit.

    ``page_bits`` (each original's coded bits in the coding the call uses, with error correction on) and
    ``piece_bits`` (what one band, and the white margin above the first, adds in that coding) let the
    originals be shared out among the same number of pages so that the fewest partial-page edges are crossed
    (``_fewest_block_edges``). The cut inside an original longer than the limit stays where it is.
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
    if page_bits is not None:
        if len(page_bits) != len(heights):
            raise ValueError('One coded size is needed for each original')
        sheets = _fewest_block_edges(sheets, heights, rows, page_bits, piece_bits, frame_octets)
    return Layout(tuple(sheets), width, rows, len(heights))


def _fewest_block_edges(sheets, heights, rows, page_bits, piece_bits, frame_octets):
    """``sheets`` (each page filled in turn: the fewest pages) shared out again, in order and on the same number of
    pages, so that the pages cross the fewest partial-page edges; ``sheets`` itself when no sharing crosses fewer.

    A page's coded size is estimated as its originals' coded bits (a split original's piece in proportion to its
    rows) plus ``piece_bits`` for each band. A piece of an original longer than the limit keeps its page to itself
    unless it is that original's last, and a continued piece always starts a page, as when each page is filled."""
    from .coding import ecm_blocks
    units = [piece for sheet in sheets for piece in sheet.pieces]
    alone = [piece.top + piece.rows < heights[piece.original] for piece in units]
    starts = [alone[i] or piece.kind == marks.CONTINUES for i, piece in enumerate(units)]
    bits = [page_bits[piece.original] * piece.rows / heights[piece.original] + piece_bits for piece in units]

    def edges(first, end):
        return ecm_blocks(math.ceil(sum(bits[first:end]) / 8), frame_octets) - 1
    greedy = (len(sheets), 0)
    first = 0
    for sheet in sheets:
        greedy = (greedy[0], greedy[1] + edges(first, first + len(sheet.pieces)))
        first += len(sheet.pieces)
    # best[i]: the fewest (pages, edges) for units[:i], and where its last page starts.
    best = [((0, 0), None)] + [((math.inf, math.inf), None)] * len(units)
    for end in range(1, len(units) + 1):
        height = TOP_ROWS
        for first in range(end - 1, -1, -1):
            height += marks.BAND_ROWS + units[first].rows
            if height > rows or (alone[first] and end - first > 1):
                break
            if best[first][0][0] < math.inf:
                pages, crossed = best[first][0]
                cost = (pages + 1, crossed + edges(first, end))
                if cost < best[end][0]:
                    best[end] = (cost, first)
            if starts[first]:
                break
    if best[-1][0][0] != greedy[0] or best[-1][0] >= greedy:
        return sheets
    shared, end = [], len(units)
    while end:
        first = best[end][1]
        shared.append(Sheet(list(units[first:end])))
        end = first
    return shared[::-1]


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


def layout_for(frames, limit, *, page_bits=None, piece_bits=0):
    """The layout of these fax frames (mode "1") under the receiver's ``limit``; ``page_bits`` and ``piece_bits``
    (error correction on, the call's coding known) share the originals out as ``plan`` says."""
    width, (_, y_dpi) = check_frames(frames)
    rows = limit_rows(limit, y_dpi)
    blanks = {}

    def white_rows(original):
        if original not in blanks:
            blanks[original] = _white_rows(frames[original])
        return blanks[original]
    return plan([frame.height for frame in frames], width, rows, white_rows=white_rows, page_bits=page_bits,
                piece_bits=piece_bits)


def piece_frame(frames):
    """The white margin at the top of a packed page and one band with the widest tag, at the frames' width and
    resolution: measured in a call's coding, at least what each piece adds to a page beyond its original."""
    width, dpi = check_frames(frames)
    total = len(frames)
    image = Image.new('1', (width, TOP_ROWS + marks.BAND_ROWS), 1)
    image.paste(marks.band(marks.Record(marks.START, total, total, width), width), (0, TOP_ROWS))
    image.info['dpi'] = dpi
    return image


def render(frames, layout):
    """The packed pages (mode "1" images, with the frames' resolution), one per sheet of ``layout``."""
    return list(render_sheets(frames, layout))


def render_sheets(frames, layout):
    """``render``, one sheet at a time, so a caller can keep each sheet packed (``conversion.FaxFrames``)."""
    width, dpi = check_frames(frames)
    if width != layout.width or len(frames) != layout.originals:
        raise NotPackable('The layout does not match these pages')
    total = len(frames)
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
        yield image
