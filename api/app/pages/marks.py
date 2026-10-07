"""The band between original pages on a packed fax page: a faint dotted rule and a small "page 2 of 5" tag.

The rule is a dotted line across the page, two identical rows tall. Each
8-pixel cell is one bit: one black pixel at the cell's left edge is 0, three
black pixels are 1, so the rule reads as a faint dotted line on paper and as
data to a receiving Faxbot. A row holds the same 76-bit record twice:

    magic (16, 0xFA5B) | version (3) | row (1: which of the two rule rows)
    | kind (2: 1 = an original page starts below, 2 = the same original continues below)
    | index (12: that original's page number, from 1) | total (12: original pages in the fax)
    | width (14: that original's width in pixels) | CRC-16/CCITT of the 60 bits before it (16)

Fax scan lines arrive pixel for pixel (T.4 codes every pel of every line), so a
receiving Faxbot finds the rule exactly where it was drawn. A rule damaged by
line errors on a call without error correction fails its CRC and is ignored.
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

MAGIC = 0xFA5B
VERSION = 1
START, CONTINUES = 1, 2
RECORD_BITS = 76
ZERO, ONE = 0x7F, 0x1F  # Pillow packs mode "1" with 1 for white: one or three black pixels, then white.
# Rows of the band, at fine resolution (7.7 lines/mm): white, two rule rows, white, the tag, white.
GAP_TOP = 8
RULE_ROWS = 2
GAP_RULE = 4
TAG_SCALE = 2
TAG_ROWS = 7 * TAG_SCALE
GAP_BOTTOM = 8
BAND_ROWS = GAP_TOP + RULE_ROWS + GAP_RULE + TAG_ROWS + GAP_BOTTOM  # 36 rows, about 4.7 mm
TAG_RIGHT_MARGIN = 48
# Narrowest page that holds two copies of the record.
MIN_WIDTH = 2 * RECORD_BITS * 8

# A 5 by 7 font for the tag, so the tag looks the same on every installation.
_GLYPHS = {
    '0': ('.###.', '#...#', '#..##', '#.#.#', '##..#', '#...#', '.###.'),
    '1': ('..#..', '.##..', '..#..', '..#..', '..#..', '..#..', '.###.'),
    '2': ('.###.', '#...#', '....#', '...#.', '..#..', '.#...', '#####'),
    '3': ('#####', '...#.', '..#..', '...#.', '....#', '#...#', '.###.'),
    '4': ('...#.', '..##.', '.#.#.', '#..#.', '#####', '...#.', '...#.'),
    '5': ('#####', '#....', '####.', '....#', '....#', '#...#', '.###.'),
    '6': ('..##.', '.#...', '#....', '####.', '#...#', '#...#', '.###.'),
    '7': ('#####', '....#', '...#.', '..#..', '.#...', '.#...', '.#...'),
    '8': ('.###.', '#...#', '#...#', '.###.', '#...#', '#...#', '.###.'),
    '9': ('.###.', '#...#', '#...#', '.####', '....#', '...#.', '.##..'),
    'p': ('.....', '####.', '#...#', '####.', '#....', '#....', '#....'),
    'a': ('.....', '.....', '.###.', '....#', '.####', '#...#', '.####'),
    'g': ('.....', '.####', '#...#', '.####', '....#', '#...#', '.###.'),
    'e': ('.....', '.....', '.###.', '#...#', '#####', '#....', '.###.'),
    'o': ('.....', '.....', '.###.', '#...#', '#...#', '#...#', '.###.'),
    'f': ('..##.', '.#...', '####.', '.#...', '.#...', '.#...', '.#...'),
    'c': ('.....', '.....', '.###.', '#....', '#....', '#...#', '.###.'),
    'n': ('.....', '.....', '#.##.', '##..#', '#...#', '#...#', '#...#'),
    't': ('.#...', '.#...', '####.', '.#...', '.#...', '.#..#', '..##.'),
    'i': ('..#..', '.....', '.##..', '..#..', '..#..', '..#..', '.###.'),
    'u': ('.....', '.....', '#...#', '#...#', '#...#', '#..##', '.##.#'),
    'd': ('....#', '....#', '.##.#', '#..##', '#...#', '#...#', '.####'),
    ',': ('.....', '.....', '.....', '.....', '.##..', '..#..', '.#...'),
    ' ': ('.....',) * 7,
}


@dataclass(frozen=True)
class Record:
    kind: int
    index: int
    total: int
    width: int
    row: int = 0


def _crc16(bits):
    crc = 0xFFFF
    for bit in bits:
        top = (crc >> 15) & 1
        crc = (crc << 1) & 0xFFFF
        if top ^ bit:
            crc ^= 0x1021
    return crc


def _put(bits, value, count):
    bits.extend((value >> shift) & 1 for shift in range(count - 1, -1, -1))


def _take(bits, start, count):
    value = 0
    for bit in bits[start:start + count]:
        value = (value << 1) | bit
    return value


def encode(record: Record):
    """The record's 76 bits."""
    if record.kind not in (START, CONTINUES) or record.row not in (0, 1):
        raise ValueError('Unsupported band record')
    if not (1 <= record.index <= record.total < 4096 and 0 < record.width < 16384):
        raise ValueError('Unsupported band record')
    bits = []
    _put(bits, MAGIC, 16)
    _put(bits, VERSION, 3)
    _put(bits, record.row, 1)
    _put(bits, record.kind, 2)
    _put(bits, record.index, 12)
    _put(bits, record.total, 12)
    _put(bits, record.width, 14)
    _put(bits, _crc16(bits), 16)
    return bits


def decode_bits(bits):
    """The record from 76 bits, or None when the magic, version or CRC is wrong."""
    if len(bits) != RECORD_BITS or any(bit not in (0, 1) for bit in bits):
        return None
    if _take(bits, 0, 16) != MAGIC or _take(bits, 16, 3) != VERSION or _take(bits, 60, 16) != _crc16(bits[:60]):
        return None
    kind, index, total, width = _take(bits, 20, 2), _take(bits, 22, 12), _take(bits, 34, 12), _take(bits, 46, 14)
    if kind not in (START, CONTINUES) or not 1 <= index <= total or not width:
        return None
    return Record(kind, index, total, width, row=_take(bits, 19, 1))


def rule_row(record: Record, width: int) -> bytes:
    """One rule row, packed as Pillow packs mode "1" (1 for white), ``width`` pixels wide."""
    if width < MIN_WIDTH:
        raise ValueError('The page is too narrow for the band')
    bits = encode(record) * 2
    cells = width // 8
    bits += [0] * (cells - len(bits))
    stride = (width + 7) // 8
    data = bytes(ONE if bit else ZERO for bit in bits[:cells])
    return data + b'\xff' * (stride - cells)


def read_row(data: bytes, width: int):
    """The record a rule row holds, or None; ``data`` is one row packed as Pillow packs mode "1"."""
    cells = width // 8
    if cells < 2 * RECORD_BITS:
        return None
    row = data[:cells]
    # Fast rejection: nearly every cell of a rule row is one of the two dot patterns.
    if row.count(ZERO) + row.count(ONE) < cells * 0.9:
        return None
    bits = [1 if value == ONE else 0 if value == ZERO else None for value in row[:2 * RECORD_BITS]]
    first, second = bits[:RECORD_BITS], bits[RECORD_BITS:]
    for candidate in (first, second, [a if a is not None else b for a, b in zip(first, second)]):
        if None not in candidate:
            record = decode_bits(candidate)
            if record is not None:
                return record
    return None


def tag_text(record: Record) -> str:
    text = f'page {record.index} of {record.total}'
    return text + ', continued' if record.kind == CONTINUES else text


def _draw_tag(image, text, top):
    pixels = image.load()
    cell = 6 * TAG_SCALE
    left = image.width - TAG_RIGHT_MARGIN - len(text) * cell
    if left < 0:
        return
    for position, character in enumerate(text):
        glyph = _GLYPHS[character]
        for y, line in enumerate(glyph):
            for x, mark in enumerate(line):
                if mark != '#':
                    continue
                for dy in range(TAG_SCALE):
                    for dx in range(TAG_SCALE):
                        pixels[left + position * cell + x * TAG_SCALE + dx, top + y * TAG_SCALE + dy] = 0


def band(record: Record, width: int):
    """The band image (mode "1", white with a dotted rule and the tag), ``BAND_ROWS`` rows tall."""
    image = Image.new('1', (width, BAND_ROWS), 1)
    for row in range(RULE_ROWS):
        line = Image.frombytes('1', (width, 1), rule_row(Record(record.kind, record.index, record.total,
                                                                record.width, row=row), width))
        image.paste(line, (0, GAP_TOP + row))
    _draw_tag(image, tag_text(record), GAP_TOP + RULE_ROWS + GAP_RULE)
    return image
