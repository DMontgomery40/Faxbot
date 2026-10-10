"""Payload pages (format v1): render a container onto bilevel fax pages and read it back.

Every page has, from the top:

- 8 mm left white, where HylaFAX and many providers print their header line;
- a two-line caption a person can read ("This page carries an encoded
  document. ..."), with "Page 1 of 2" and "experimental";
- the ladder, the finder and alignment pattern: three rows of a black bar,
  a white spacer, ``columns`` alternating one-cell runs (black first, odd
  count), a white spacer and a black bar. Any one of its scan lines gives the
  exact position of every column, wherever the page was shifted or scaled
  across;
- three copies of the page header, written as grid rows;
- the data, in the page's layout (grid, runs, picture or enumerative);
- 4 mm of white at the bottom.

A grid or picture row carries ``columns`` bits: a 32-bit stream bit offset,
then groups of up to 32 bytes, each followed by a CRC-16/CCITT (initial value
0xFFFF) over the document tag, the offset, the group number and the group's
bytes. Each scan line of a row is read on its own and every group is checked
on its own, so a dropped, repeated or partly damaged line costs only the
groups it breaks. Run-coded lines are described in ``runs``.

The page header (32 bytes, big-endian): magic 'FXP', version 1, layout
(1 grid, 2 runs, 3 picture, 4 enumerative), parity symbols, page index, page count, the
document tag (the first four bytes of the container's SHA-256), the codeword
count, the container length, the first and the end stream bit this page
carries, the run limit and profile byte (0 for older layouts, 1 for the
immutable enumerative profile). Header rows use offsets
0xFFFFFFF0-0xFFFFFFF2 and a zero tag in their CRC.
"""
from binascii import crc_hqx
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import operator
from pathlib import Path
import re
import struct

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import runs as runcode
from . import enumerative
from . import stream as streams
from . import t4


@dataclass(frozen=True)
class Resolution:
    name: str
    xdpi: int
    ydpi: int
    width: int
    lines: int  # a US letter page (11 inches), shorter than A4

    def mm_lines(self, mm):
        return max(1, round(mm * self.ydpi / 25.4))

    def mm_dots(self, mm):
        return max(1, round(mm * self.xdpi / 25.4))


# ITU-T T.4 section 2 and T.30 Table 2: 1728 pels across 215 mm at 8 pels/mm
# (about 204 dpi) for standard, fine and superfine; 2592 pels at 300 dpi and
# 3456 pels at 400 dpi for an A4-wide scan line.
RESOLUTIONS = {
    'standard': Resolution('standard', 204, 98, 1728, 1078),
    'fine': Resolution('fine', 204, 196, 1728, 2156),
    'superfine': Resolution('superfine', 204, 391, 1728, 4301),
    '300': Resolution('300', 300, 300, 2592, 3300),
    '400': Resolution('400', 400, 400, 3456, 4400),
}
LAYOUTS = {'grid': 1, 'runs': 2, 'picture': 3, 'enumerative': 4}
LAYOUT_NAMES = {value: key for key, value in LAYOUTS.items()}
HEADER = struct.Struct('>3sBBBHH4sIIIIBB')
HEADER_OFFSETS = (0xFFFFFFF0, 0xFFFFFFF1, 0xFFFFFFF2)
ZERO_TAG = bytes(4)
OFFSET_BITS = 32
GROUP_BYTES = 32
CAPTION = ('This page carries an encoded document. Faxbot decodes it automatically; '
           'or decode it at faxbot.net/decode.')
DEFAULT_RUN_LIMIT = 15
_BLACK = bytes.maketrans(bytes(range(256)), b'1' * 128 + b'0' * 128)
_RUN_PATTERN = re.compile(r'0+|1+')


class PageError(ValueError):
    """A page could not be read as a payload page (one sentence, safe to show)."""


class _NoHeader(PageError):
    """The ladder is there but no header copy reads: the page may be upside down."""


NO_PATTERN = 'This page has no payload pattern.'
NO_HEADER = 'The payload page header could not be read.'
# Run-coded and enumerative pages carry their data in exact run lengths, so a page drawn again at another size
# cannot be read; the grid and picture layouts survive it.
RESIZED = ('This page was resized after it arrived, and these encoded pages can be read only from the fax image '
           'exactly as received, such as the PDF a fax service offers for download.')
PREVIEW = ('This is a small preview of the fax rather than the fax itself, so decode the full-size fax file, such '
           'as the PDF a fax service offers for download.')
# Narrower than any fax page Faxbot draws (1728 dots across) by a wide margin: a picture this small is a preview.
PREVIEW_WIDTH = 1000


@dataclass(frozen=True)
class Geometry:
    resolution: Resolution
    layout: str
    unit: int          # dots across one column
    height: int        # scan lines per data row (1 for run-coded lines)
    ladder_height: int  # scan lines per ladder or header row
    quiet: int         # white dots at each side
    columns: int
    data_top: int      # first data scan line
    bottom: int        # white scan lines at the bottom
    run_limit: int = DEFAULT_RUN_LIMIT

    @property
    def max_data_lines(self):
        # The header copies and the ladder are repeated below the data.
        return self.resolution.lines - self.data_top - self.bottom - 6 * self.ladder_height

    @property
    def rows_per_page(self):
        return self.max_data_lines // self.height

    @property
    def row_layout(self):
        return group_sizes(self.columns)

    @property
    def row_bytes(self):
        return sum(self.row_layout)


def group_sizes(columns):
    """Bytes in each group of a row of ``columns`` bits."""
    available = columns - OFFSET_BITS
    count = max(1, round(available / (8 * GROUP_BYTES + 16)))
    total = (available - 16 * count) // 8
    if total < 1:
        raise PageError('A row is too narrow to carry data.')
    return [total // count + (1 if index < total % count else 0) for index in range(count)]


def geometry(resolution='fine', layout='grid', *, sturdy=False, run_limit=DEFAULT_RUN_LIMIT):
    """The page geometry for a resolution and layout.

    Grid cells are 2 dots across at every resolution (about 0.25 mm at
    204 dpi) and 2 scan lines tall, 1 at standard resolution. ``sturdy``
    doubles them to about 0.5 mm for pages a provider renders itself, which
    may rescale them. Picture cells are about 0.5 mm across.
    """
    res = RESOLUTIONS[resolution] if isinstance(resolution, str) else resolution
    if layout not in LAYOUTS:
        raise PageError('Unknown page layout.')
    if layout == 'picture':
        unit = max(4, res.mm_dots(0.5))
        height = 1 if res.ydpi < 150 else max(2, res.mm_lines(0.26))
    elif sturdy and layout == 'grid':
        unit = max(4, res.mm_dots(0.5))
        height = max(2, res.mm_lines(0.5))
    else:
        unit = 2
        height = 1 if res.ydpi < 150 else 2
    if layout in ('runs', 'enumerative'):
        height = 1
    if layout == 'enumerative':
        run_limit = enumerative.RUN_LIMIT
        if res.width not in enumerative.WIDTHS:
            raise PageError('This page width does not support the enumerative layout.')
    ladder_height = max(height, 1 if res.ydpi < 150 else 2)
    quiet = res.mm_dots(3)
    columns = (res.width - 2 * quiet) // unit - 8
    if columns % 2 == 0:
        columns -= 1
    top = res.mm_lines(8) + res.mm_lines(7) + res.mm_lines(1)
    data_top = top + 6 * ladder_height
    return Geometry(res, layout, unit, height, ladder_height, quiet, columns, data_top, res.mm_lines(4),
                    run_limit)


# --- rendering --------------------------------------------------------------------------------------------------

def _crc(tag, offset, group, data):
    return crc_hqx(tag + struct.pack('>IB', offset & 0xFFFFFFFF, group) + data, 0xFFFF)


def row_bits(tag, offset, data, sizes):
    """The bits of one grid or picture row ('1' black), ``data`` exactly sum(sizes) bytes."""
    parts = [format(offset, '032b')]
    position = 0
    for index, size in enumerate(sizes):
        chunk = data[position:position + size]
        position += size
        parts.append(format(int.from_bytes(chunk, 'big'), f'0{8 * size}b') if size else '')
        parts.append(format(_crc(tag, offset, index, chunk), '016b'))
    return ''.join(parts)


def _pack(pixels):
    """PIL mode "1" raw bytes from a string of pixels ('1' white, '0' black)."""
    return int(pixels, 2).to_bytes(len(pixels) // 8, 'big')


def _grid_line(geo, bits):
    width, unit, quiet = geo.resolution.width, geo.unit, geo.quiet
    cells = bits.ljust(geo.columns, '0').translate({ord('1'): '0' * unit, ord('0'): '1' * unit})
    left = '1' * (quiet + 4 * unit)
    return _pack((left + cells).ljust(width, '1'))


def _ladder_line(geo):
    width, unit, quiet = geo.resolution.width, geo.unit, geo.quiet
    cells = ''.join(('0' if index % 2 == 0 else '1') * unit for index in range(geo.columns))
    bar = '0' * (3 * unit)
    spacer = '1' * unit
    return _pack(('1' * quiet + bar + spacer + cells + spacer + bar).ljust(width, '1'))


def _picture_line(geo, bits, tones):
    unit = geo.unit
    cells = []
    for bit, dark in zip(bits.ljust(geo.columns, '0'), tones):
        if bit == '1':
            cells.append('1' * (unit - dark) + '0' * dark)
        else:
            cells.append('0' * dark + '1' * (unit - dark))
    left = '1' * (geo.quiet + 4 * unit)
    return _pack((left + ''.join(cells)).ljust(geo.resolution.width, '1'))


@lru_cache(maxsize=1)
def _font_path():
    import reportlab
    return str(Path(reportlab.__file__).parent / 'fonts' / 'Vera.ttf')


def _caption_lines(geo, page_index, page_count):
    res = geo.resolution
    height_square = round(7 * res.xdpi / 25.4)
    canvas = Image.new('L', (res.width, height_square), 255)
    draw = ImageDraw.Draw(canvas)
    size = max(10, round(res.xdpi * 7 / 72))
    # Keep these fixed captions identical whether optional RAQM shaping is installed or not.
    font = ImageFont.truetype(_font_path(), size, layout_engine=ImageFont.Layout.BASIC)
    caption = ('Encoded document: needs a Faxbot decoder supporting enumerative profile 1.'
               if geo.layout == 'enumerative' else CAPTION)
    draw.text((geo.quiet, 0), caption, font=font, fill=0)
    draw.text((geo.quiet, round(size * 1.35)),
              f'Page {page_index + 1} of {page_count} · experimental · Faxbot payload format 1',
              font=font, fill=0)
    scaled = canvas.resize((res.width, res.mm_lines(7)), Image.Resampling.BOX)
    return scaled.point(lambda value: 255 if value >= 160 else 0).convert('1').tobytes()


def _picture_tones(geo, picture, rows):
    """Black dots per cell, 1..unit-1, by error diffusion of the picture's darkness."""
    # Fit the picture inside the data area keeping its proportions; a cell is unit dots wide and
    # ``height`` lines tall, so it is not square on paper.
    res = geo.resolution
    area_width, area_height = geo.columns * geo.unit / res.xdpi, rows * geo.height / res.ydpi
    source = picture.convert('L')
    scale = min(area_width / source.size[0], area_height / source.size[1])
    cells_across = max(1, min(geo.columns, round(source.size[0] * scale * res.xdpi / geo.unit)))
    cells_down = max(1, min(rows, round(source.size[1] * scale * res.ydpi / geo.height)))
    fitted = Image.new('L', (geo.columns, rows), 255)
    fitted.paste(source.resize((cells_across, cells_down), Image.Resampling.LANCZOS),
                 ((geo.columns - cells_across) // 2, (rows - cells_down) // 2))
    values = fitted.tobytes()
    unit = geo.unit
    span = unit - 2
    tones = []
    carry = [0.0] * (geo.columns + 2)
    for row in range(rows):
        following = [0.0] * (geo.columns + 2)
        out = []
        base = row * geo.columns
        for column in range(geo.columns):
            wanted = 1 + (255 - values[base + column]) / 255 * span + carry[column + 1]
            chosen = min(unit - 1, max(1, round(wanted)))
            error = wanted - chosen
            carry[column + 2] += error * 7 / 16
            following[column] += error * 3 / 16
            following[column + 1] += error * 5 / 16
            following[column + 2] += error / 16
            out.append(chosen)
        carry = following
        tones.append(out)
    return tones


@dataclass
class EncodedPages:
    pages: list
    geometry: Geometry
    parity: int
    codewords: int
    container_length: int
    stream_bytes: int
    tag: bytes
    page_bits: list = field(default_factory=list)  # payload bits per page

    @property
    def page_count(self):
        return len(self.pages)


def _image(geo, lines):
    res = geo.resolution
    image = Image.frombytes('1', (res.width, len(lines)), b''.join(lines))
    image.info['dpi'] = (res.xdpi, res.ydpi)
    return image


def encode(container, *, resolution='fine', layout='grid', fec='medium', sturdy=False, picture=None,
           run_limit=DEFAULT_RUN_LIMIT, max_pages=200):
    """Payload pages for a container: a list of PIL mode "1" images with their DPI set."""
    geo = geometry(resolution, layout, sturdy=sturdy, run_limit=run_limit)
    parity = streams.FEC_LEVELS[fec] if isinstance(fec, str) else int(fec)
    data, codewords = streams.build(container, parity)
    tag = hashlib.sha256(container).digest()[:4]
    total_bits = len(data) * 8
    plans = []  # (first bit, end bit, data lines)
    if layout in ('grid', 'picture'):
        per_row = geo.row_bytes
        per_page = per_row * geo.rows_per_page
        pages = max(1, -(-len(data) // per_page))
        for index in range(pages):
            start = index * per_page
            end = min(len(data), start + per_page)
            rows = []
            for offset in range(start, end, per_row):
                chunk = data[offset:offset + per_row]
                rows.append((offset * 8, chunk + bytes(per_row - len(chunk))))
            if layout == 'picture':
                # The whole picture shows: rows past the stream carry zeros the decoder ignores.
                filler = start + per_row * len(rows)
                while len(rows) < geo.rows_per_page:
                    rows.append((filler * 8, bytes(per_row)))
                    filler += per_row
            plans.append((start * 8, end * 8, rows))
    else:
        source = format(int.from_bytes(data, 'big'), f'0{total_bits}b')
        position = 0
        while position < total_bits:
            first = position
            lines = []
            while position < total_bits and len(lines) < geo.max_data_lines:
                if layout == 'enumerative':
                    changes, used = enumerative.encode_line(tag, position, source, position, geo.resolution.width)
                else:
                    changes, used = runcode.encode_line(tag, position, source, position, geo.resolution.width,
                                                       geo.run_limit)
                lines.append(changes)
                position += used
            plans.append((first, min(position, total_bits), lines))
    if len(plans) > max_pages:
        raise PageError(f'The document needs {len(plans)} payload pages; the limit is {max_pages}.')
    pictures = None
    if layout == 'picture':
        if picture is None:
            picture = Image.new('L', (geo.columns, geo.rows_per_page), 200)
        pictures = _picture_tones(geo, picture, geo.rows_per_page)
    images = []
    page_bits = []
    for index, (first, end, rows) in enumerate(plans):
        header = HEADER.pack(b'FXP', 1, LAYOUTS[layout], parity, index, len(plans), tag, codewords,
                             len(container), first, end, geo.run_limit,
                             enumerative.PROFILE if layout == 'enumerative' else 0)
        lines = [b'\xff' * (geo.resolution.width // 8)] * geo.resolution.mm_lines(8)
        caption = _caption_lines(geo, index, len(plans))
        stride = geo.resolution.width // 8
        lines += [caption[i:i + stride] for i in range(0, len(caption), stride)]
        lines += [b'\xff' * stride] * geo.resolution.mm_lines(1)
        ladder = _ladder_line(geo)
        lines += [ladder] * (3 * geo.ladder_height)
        header_sizes = geo.row_layout
        header_lines = []
        for offset in HEADER_OFFSETS:
            payload = header + bytes(sum(header_sizes) - len(header))
            header_lines += [_grid_line(geo, row_bits(ZERO_TAG, offset, payload, header_sizes))] * geo.ladder_height
        lines += header_lines
        if layout == 'grid':
            for offset, chunk in rows:
                line = _grid_line(geo, row_bits(tag, offset, chunk, geo.row_layout))
                lines += [line] * geo.height
        elif layout == 'picture':
            for number, (offset, chunk) in enumerate(rows):
                line = _picture_line(geo, row_bits(tag, offset, chunk, geo.row_layout), pictures[number])
                lines += [line] * geo.height
        else:
            width = geo.resolution.width
            lines += [t4.packed_from_changes(changes, width) for changes in rows]
        lines += header_lines + [ladder] * (3 * geo.ladder_height)
        lines += [b'\xff' * stride] * geo.bottom
        images.append(_image(geo, lines))
        page_bits.append(end - first)
    return EncodedPages(images, geo, parity, codewords, len(container), len(data), tag, page_bits)


# --- reading ----------------------------------------------------------------------------------------------------

def _line_runs(line):
    """(start, length, black) runs of one 8-bit grayscale line."""
    text = line.translate(_BLACK).decode('ascii')
    return [(match.start(), match.end() - match.start(), text[match.start()] == '1')
            for match in _RUN_PATTERN.finditer(text)]


def _ladder(line):
    """(module edges, unit) when this scan line is a ladder line, else None."""
    found = _line_runs(line)
    if len(found) < 40:
        return None
    lengths = [length for _, length, _ in found]
    count = len(found)
    index = 0
    while index < count:
        start, length, black = found[index]
        if not black or length < 3:
            index += 1
            continue
        unit = length / 3.0
        tolerance = max(1.0, unit * 0.5)
        end = index + 1
        while end < count and abs(lengths[end] - unit) <= tolerance:
            end += 1
        # found[index] is the left bar; index+1..end-1 are the spacer, cells, spacer; found[end] the right bar.
        cells = end - index - 3
        if (cells >= 31 and cells % 2 == 1 and end < count and found[end][2]
                and abs(lengths[end] - 3 * unit) <= 3 * tolerance and not found[index + 1][2]):
            edges = [found[i][0] for i in range(index + 2, end)]
            return edges, unit
        index = max(index + 1, end)
    return None


def _grid_bits(line, centres):
    return bytes(centres(line)).translate(_BLACK).decode('ascii')


def _picture_bits(line, edges):
    text = line[edges[0]:edges[-1]].translate(_BLACK).decode('ascii')
    base = edges[0]
    bits = []
    for left, right in zip(edges, edges[1:]):
        middle = (left + right) // 2
        first = text[left - base:middle - base].count('1')
        second = text[middle - base:right - base].count('1')
        bits.append('1' if second > first else '0')
    return ''.join(bits)


def parse_row(bits, sizes, tag):
    """(offset, [(group, byte position in row, data)]) of the groups whose CRC passes."""
    offset = int(bits[:OFFSET_BITS], 2)
    position = OFFSET_BITS
    good = []
    start = 0
    for index, size in enumerate(sizes):
        chunk_bits = bits[position:position + 8 * size]
        crc = int(bits[position + 8 * size:position + 8 * size + 16], 2)
        chunk = int(chunk_bits, 2).to_bytes(size, 'big')
        if _crc(tag, offset, index, chunk) == crc:
            good.append((index, start, chunk))
        position += 8 * size + 16
        start += size
    return offset, good


@dataclass
class PageRead:
    header: dict
    segments: list          # (stream bit offset, bit count, value as int)
    lines_read: int = 0
    lines_damaged: int = 0
    # What was undone to read the page as it was sent: 'inverted', 'rotated' (180 degrees), 'moved' (dots).
    corrections: dict = field(default_factory=dict)


def paper_white(image):
    """(the page in grayscale with white paper, True when it had to be inverted). Every payload page has white
    margins at both sides, so a page whose left and right edges are mostly dark arrived inverted."""
    gray = image if image.mode == 'L' else image.convert('L')
    width, height = gray.size
    edge = max(1, min(width // 50, 16))
    dark = sum(gray.crop((0, 0, edge, height)).histogram()[:128]) + sum(
        gray.crop((width - edge, 0, width, height)).histogram()[:128])
    if dark * 2 > 2 * edge * height:
        return ImageOps.invert(gray), True
    return gray, False


@lru_cache(maxsize=1)
def _exact_widths():
    """{ladder columns: (page width, expected first-cell dot)} for the run-coded layouts (2-dot cells): the column
    count names the page width the rows were drawn for, and where the ladder starts on an unmoved page."""
    found = {}
    for res in RESOLUTIONS.values():
        geo = geometry(res, 'runs')
        found[geo.columns] = (res.width, geo.quiet + 4 * geo.unit)
    return found


def _aligned(line, shift, width):
    """One scan line moved back by ``shift`` dots and cut or filled to ``width``: white where the receiver cut the
    left edge, and the last dot's colour on the right, where a run-coded row ends in its padding run."""
    if shift == 0 and len(line) == width:
        return line
    part = line[shift:shift + width] if shift >= 0 else b'\xff' * -shift + line[:width + shift]
    if len(part) < width:
        part += (part[-1:] or b'\xff') * (width - len(part))
    return part


def find_ladder(image, *, limit=None):
    """(scan line, edges, unit) of the first ladder line, or None. Cheap enough to probe every fax."""
    gray = image if image.mode == 'L' else image.convert('L')
    width, height = gray.size
    data = gray.tobytes()
    last = height if limit is None else min(height, limit)
    for y in range(last):
        line = data[y * width:(y + 1) * width]
        found = _ladder(line)
        if found is not None:
            return y, found[0], found[1]
    return None


def _decode_header(payload):
    magic, version, layout, parity, index, count, tag, codewords, length, first, end, limit, profile = \
        HEADER.unpack_from(payload)
    if magic != b'FXP' or version != 1 or layout not in LAYOUT_NAMES or not 1 <= parity <= 128:
        return None
    if count < 1 or index >= count or length < 1 or end < first:
        return None
    # The header is untrusted: its sizes must be the ones a real container of that length has.
    from .container import MAX_DOCUMENT_BYTES
    if (length > MAX_DOCUMENT_BYTES + 4096 or parity >= 255
            or codewords != streams.codeword_count(length, parity) or not 1 <= limit <= 63):
        return None
    if layout == LAYOUTS['enumerative'] and (profile != enumerative.PROFILE
            or limit != enumerative.RUN_LIMIT or not first < end <= codewords * 255 * 8):
        return None
    return {'layout': LAYOUT_NAMES[layout], 'parity': parity, 'page': index, 'pages': count, 'tag': tag,
            'codewords': codewords, 'container_length': length, 'first_bit': first, 'end_bit': end,
            'run_limit': limit, 'profile': profile}


def read_page(image):
    """Read one page; raises PageError when it is not a payload page.

    The ladder and the page header are each written above and below the data,
    so a page whose top lines were damaged or covered still reads. A page that
    arrived inverted or upside down is turned back first, and the rows of a
    run-coded page moved sideways or padded to another width are moved back.
    """
    gray, inverted = paper_white(image)
    corrections = {'inverted': True} if inverted else {}
    try:
        return _read_oriented(gray, corrections)
    except _NoHeader:
        # Upside down: the ladder reads either way, the header only the right way up.
        try:
            return _read_oriented(gray.rotate(180), {**corrections, 'rotated': True})
        except _NoHeader:
            raise PageError(NO_HEADER) from None


def _read_oriented(gray, corrections):
    width, height = gray.size
    data = gray.tobytes()
    ladder = find_ladder(gray)
    if ladder is None:
        raise PageError(NO_PATTERN)
    y0, edges, unit = ladder
    columns = len(edges) - 1
    centres = operator.itemgetter(*[(edges[i] + edges[i + 1]) // 2 for i in range(columns)])
    sizes = group_sizes(columns)
    header = None
    for y in range(height):
        line = data[y * width:(y + 1) * width]
        bits = _grid_bits(line, centres)
        if not bits.strip('0'):
            continue
        offset, good = parse_row(bits, sizes, ZERO_TAG)
        if offset in HEADER_OFFSETS and len(good) == len(sizes):
            header = _decode_header(b''.join(chunk for _, _, chunk in good))
            if header is not None:
                break
    if header is None:
        raise _NoHeader(NO_HEADER)
    tag = header['tag']
    layout = header['layout']
    row_width, shift = width, 0
    if len({edges[i + 1] - edges[i] for i in range(columns)}) != 1:
        # Cells of unequal width: the page was drawn again at another size (said if the page then fails to decode).
        corrections = {**corrections, 'resized': True}
    if layout in ('runs', 'enumerative'):
        # Exact rows: every cell must be exactly as drawn (two dots), and the rows are read at the width they were
        # drawn for, moved back to where the ladder says they started.
        exact = _exact_widths().get(columns)
        if exact is None or any(edges[i + 1] - edges[i] != 2 for i in range(columns)):
            raise PageError(RESIZED)
        row_width, expected = exact
        shift = edges[0] - expected
        if shift:
            corrections = {**corrections, 'moved': shift}
    segments = {}
    lines_read = lines_damaged = since_good = 0
    if layout == 'enumerative':
        try:
            row_bits_count = enumerative.payload_bits(row_width, header['profile'])
        except ValueError as error:
            raise PageError(str(error)) from None
        if header['first_bit'] % row_bits_count:
            raise PageError('The enumerative page starts at an invalid stream offset.')
    for y in range(y0, height):
        line = data[y * width:(y + 1) * width]
        if layout == 'enumerative':
            result = enumerative.decode_line(t4.changes_from_pixels(_aligned(line, shift, row_width)), row_width, tag,
                                             profile=header['profile'])
            if (result is None or result[0] % row_bits_count
                    or not header['first_bit'] <= result[0] < header['end_bit']):
                lines_damaged += 1 if lines_read else 0
                since_good += 1 if lines_read else 0
                continue
            offset, payload = result
            segment = (offset, len(payload), int(payload, 2))
            key = (offset, len(payload))
            if key in segments and segments[key] != segment:
                raise PageError('Conflicting enumerative payload rows were received.')
            segments[key] = segment
            lines_read += 1
            since_good = 0
            continue
        if layout == 'runs':
            changes = t4.changes_from_pixels(_aligned(line, shift, row_width))
            if len(changes) < 8:
                continue
            result = runcode.decode_line(changes, row_width, header['run_limit'])
            if result is None or not runcode.check(tag, result[0], result[1], result[2]):
                lines_damaged += 1 if lines_read else 0
                since_good += 1 if lines_read else 0
                continue
            offset, payload, _ = result
            lines_read += 1
            since_good = 0
            if payload:
                segments[(offset, len(payload))] = (offset, len(payload), int(payload, 2))
            continue
        bits = _grid_bits(line, centres) if layout == 'grid' else _picture_bits(line, edges)
        if not bits.strip('0'):
            continue
        offset, good = parse_row(bits, sizes, tag)
        if not good:
            # Lines before the first data line are the ladder and the other header copies.
            lines_damaged += 1 if lines_read else 0
            since_good += 1 if lines_read else 0
            continue
        lines_read += 1
        since_good = 0
        for index, start, chunk in good:
            bit_offset = offset + 8 * start
            segments[(bit_offset, 8 * len(chunk))] = (bit_offset, 8 * len(chunk), int.from_bytes(chunk, 'big'))
    # Failures after the last data line are the header copies and the ladder below the data.
    return PageRead(header, list(segments.values()), lines_read, lines_damaged - since_good, corrections)


@dataclass
class DecodedStream:
    container: bytes
    header: dict
    pages_read: int
    pages_expected: int
    report: dict
    corrections: dict = field(default_factory=dict)  # {'inverted' | 'rotated' | 'moved': pages it was done to}


def assemble(reads):
    """The container from several page reads (any order; duplicates and other documents are ignored)."""
    if not reads:
        raise PageError('No payload pages were found.')
    tags = {}
    for read in reads:
        tags[read.header['tag']] = tags.get(read.header['tag'], 0) + 1
    tag = max(tags, key=tags.get)
    chosen = [read for read in reads if read.header['tag'] == tag]
    header = chosen[0].header
    if any(read.header['layout'] == 'enumerative' for read in chosen):
        fields = ('layout', 'parity', 'pages', 'codewords', 'container_length', 'run_limit', 'profile')
        seen = {}
        for read in chosen:
            if any(read.header.get(key) != header.get(key) for key in fields):
                raise PageError('Conflicting enumerative page headers were received.')
            for offset, count, value in read.segments:
                key = (offset, count)
                if key in seen and seen[key] != value:
                    raise PageError('Conflicting enumerative payload rows were received.')
                seen[key] = value
    total = header['codewords'] * 255
    stream = bytearray(total)
    covered = bytearray(total)
    for read in chosen:
        for offset, count, value in read.segments:
            if count <= 0 or offset >= total * 8:
                continue
            if offset + count > total * 8:
                value >>= offset + count - total * 8
                count = total * 8 - offset
            first = offset // 8
            last = (offset + count + 7) // 8
            span = (last - first) * 8
            shift = span - (offset - first * 8) - count
            mask = ((1 << count) - 1) << shift
            region = int.from_bytes(stream[first:last], 'big')
            region = (region & ~mask) | (value << shift)
            stream[first:last] = region.to_bytes(last - first, 'big')
            cover = int.from_bytes(covered[first:last], 'big') | mask
            covered[first:last] = cover.to_bytes(last - first, 'big')
    known = covered.translate(bytes.maketrans(bytes(range(256)), b'\x00' * 255 + b'\x01'))
    pages = {read.header['page'] for read in chosen}
    try:
        container, report = streams.recover(stream, known, header['codewords'], header['parity'],
                                            header['container_length'])
    except streams.StreamError as error:
        missing = header['pages'] - len(pages)
        if missing:
            raise PageError(f'{missing} of the {header["pages"]} payload pages are missing '
                            'and the rest could not make up for them.') from None
        raise PageError(f'The payload pages are too damaged to decode ({error}).') from None
    report['lines_read'] = sum(read.lines_read for read in chosen)
    report['lines_damaged'] = sum(read.lines_damaged for read in chosen)
    corrections = {}
    for read in chosen:
        for name in read.corrections:
            corrections[name] = corrections.get(name, 0) + 1
    return DecodedStream(container, header, len(pages), header['pages'], report, corrections)


def decode(images):
    """The container from received page images. With no page readable, the refusal says why when the reason is
    one a person can act on: a resized page of an exact layout, or a small preview instead of the fax."""
    reads, refusals = [], []
    for image in images:
        try:
            reads.append(read_page(image))
        except PageError as error:
            refusals.append(str(error))
    small = bool(images) and all(image.size[0] < PREVIEW_WIDTH for image in images)
    if not reads:
        if small:
            raise PageError(PREVIEW)
        if RESIZED in refusals:
            raise PageError(RESIZED)
    try:
        return assemble(reads)
    except PageError:
        if small:
            raise PageError(PREVIEW) from None
        if any(read.corrections.get('resized') for read in reads):
            raise PageError(RESIZED) from None
        raise
