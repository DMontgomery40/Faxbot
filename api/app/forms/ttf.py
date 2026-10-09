"""A small TrueType reader: character map, advance widths and glyph outlines.

The form renderer draws text from the font file pinned in ``fonts/`` with its
own integer rasterizer (``raster.py``), never with FreeType or the system's
fonts, so the same text gives the same dots on every host. Only what that
needs is read: ``head``, ``hhea``, ``maxp``, ``hmtx``, ``cmap`` (format 4,
the Basic Multilingual Plane), ``loca`` and ``glyf`` (simple and composite
glyphs). Hinting instructions and kerning are ignored on purpose: both would
tie the result to an interpreter's behaviour.

Outline coordinates are exact rationals (``Fraction``): composite glyph
scales are F2Dot14 values, and implied on-curve points sit halfway between
two off-curve points.
"""
from fractions import Fraction
import struct


class FontError(ValueError):
    """The font file is not a TrueType font this reader understands."""


class Font:
    def __init__(self, data):
        if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
            raise FontError('not a font')
        self.data = bytes(data)
        count = struct.unpack('>H', self.data[4:6])[0]
        self.tables = {}
        for index in range(count):
            tag, _, offset, length = struct.unpack('>4sIII', self.data[12 + 16 * index:28 + 16 * index])
            self.tables[tag.decode('latin-1')] = (offset, length)
        for required in ('head', 'hhea', 'maxp', 'hmtx', 'cmap', 'loca', 'glyf'):
            if required not in self.tables:
                raise FontError(f'missing {required}')
        head = self._table('head')
        self.units_per_em = struct.unpack('>H', head[18:20])[0]
        self.long_offsets = struct.unpack('>h', head[50:52])[0] == 1
        hhea = self._table('hhea')
        self.ascender, self.descender = struct.unpack('>hh', hhea[4:8])
        metrics = struct.unpack('>H', hhea[34:36])[0]
        self.glyph_count = struct.unpack('>H', self._table('maxp')[4:6])[0]
        hmtx = self._table('hmtx')
        self.advances = [struct.unpack('>H', hmtx[4 * index:4 * index + 2])[0] for index in range(metrics)]
        last = self.advances[-1]
        self.advances.extend([last] * (self.glyph_count - metrics))
        self.cmap = self._read_cmap()
        loca = self._table('loca')
        if self.long_offsets:
            self.offsets = list(struct.unpack(f'>{self.glyph_count + 1}I', loca[:4 * (self.glyph_count + 1)]))
        else:
            self.offsets = [value * 2 for value in struct.unpack(f'>{self.glyph_count + 1}H',
                                                                 loca[:2 * (self.glyph_count + 1)])]
        self._outlines = {}

    def _table(self, tag):
        offset, length = self.tables[tag]
        return self.data[offset:offset + length]

    def _read_cmap(self):
        cmap = self._table('cmap')
        count = struct.unpack('>H', cmap[2:4])[0]
        chosen = None
        for index in range(count):
            platform, encoding, offset = struct.unpack('>HHI', cmap[4 + 8 * index:12 + 8 * index])
            if struct.unpack('>H', cmap[offset:offset + 2])[0] == 4 and (platform, encoding) in ((3, 1), (0, 3)):
                chosen = offset
                if platform == 3:
                    break
        if chosen is None:
            raise FontError('no Unicode character map')
        table = cmap[chosen:]
        segments = struct.unpack('>H', table[6:8])[0] // 2
        ends = struct.unpack(f'>{segments}H', table[14:14 + 2 * segments])
        base = 16 + 2 * segments
        starts = struct.unpack(f'>{segments}H', table[base:base + 2 * segments])
        deltas = struct.unpack(f'>{segments}h', table[base + 2 * segments:base + 4 * segments])
        ranges_at = base + 4 * segments
        ranges = struct.unpack(f'>{segments}H', table[ranges_at:ranges_at + 2 * segments])
        mapping = {}
        for segment in range(segments):
            start, end, delta, range_offset = starts[segment], ends[segment], deltas[segment], ranges[segment]
            if start == 0xFFFF:
                continue
            for code in range(start, end + 1):
                if range_offset == 0:
                    glyph = (code + delta) & 0xFFFF
                else:
                    at = ranges_at + 2 * segment + range_offset + 2 * (code - start)
                    glyph = struct.unpack('>H', table[at:at + 2])[0]
                    if glyph:
                        glyph = (glyph + delta) & 0xFFFF
                if glyph:
                    mapping[code] = glyph
        return mapping

    def glyph(self, character):
        """The glyph index for one character, or None when the font has no drawing for it."""
        return self.cmap.get(ord(character))

    def advance(self, glyph):
        return self.advances[glyph]

    def outline(self, glyph):
        """Closed contours, each a list of segments: ('L', p0, p1) or ('Q', p0, control, p1), in font units."""
        if glyph not in self._outlines:
            self._outlines[glyph] = tuple(self._contours(glyph, depth=0))
        return self._outlines[glyph]

    def _contours(self, glyph, *, depth):
        if depth > 8 or not 0 <= glyph < self.glyph_count:
            raise FontError('bad glyph reference')
        start, end = self.offsets[glyph], self.offsets[glyph + 1]
        if end <= start:
            return []
        offset = self.tables['glyf'][0] + start
        data = self.data[offset:offset + (end - start)]
        contour_count = struct.unpack('>h', data[0:2])[0]
        if contour_count >= 0:
            return [_segments(points) for points in _simple(data, contour_count)]
        return self._composite(data, depth=depth)

    def _composite(self, data, *, depth):
        contours = []
        at = 10
        while True:
            flags, component = struct.unpack('>HH', data[at:at + 4])
            at += 4
            if flags & 0x0001:  # ARG_1_AND_2_ARE_WORDS
                first, second = struct.unpack('>hh', data[at:at + 4])
                at += 4
            else:
                first, second = struct.unpack('>bb', data[at:at + 2])
                at += 2
            if not flags & 0x0002:  # ARGS_ARE_XY_VALUES; point matching is not used by this font
                raise FontError('point-matched composite glyphs are not supported')
            xx, xy, yx, yy = 1, 0, 0, 1
            if flags & 0x0008:  # WE_HAVE_A_SCALE
                xx = yy = Fraction(struct.unpack('>h', data[at:at + 2])[0], 16384)
                at += 2
            elif flags & 0x0040:  # WE_HAVE_AN_X_AND_Y_SCALE
                xx, yy = (Fraction(value, 16384) for value in struct.unpack('>hh', data[at:at + 4]))
                at += 4
            elif flags & 0x0080:  # WE_HAVE_A_TWO_BY_TWO
                xx, xy, yx, yy = (Fraction(value, 16384) for value in struct.unpack('>hhhh', data[at:at + 8]))
                at += 8

            def place(point, xx=xx, xy=xy, yx=yx, yy=yy, dx=first, dy=second):
                x, y = point
                return (xx * x + yx * y + dx, xy * x + yy * y + dy)
            for contour in self._contours(component, depth=depth + 1):
                contours.append([(kind, *(place(point) for point in points)) for kind, *points in contour])
            if not flags & 0x0020:  # MORE_COMPONENTS
                break
        return contours


def _simple(data, contour_count):
    ends = struct.unpack(f'>{contour_count}H', data[10:10 + 2 * contour_count])
    if not ends:
        return []
    total = ends[-1] + 1
    at = 10 + 2 * contour_count
    instructions = struct.unpack('>H', data[at:at + 2])[0]
    at += 2 + instructions
    flags = []
    while len(flags) < total:
        flag = data[at]
        at += 1
        flags.append(flag)
        if flag & 0x08:
            repeat = data[at]
            at += 1
            flags.extend([flag] * repeat)
    flags = flags[:total]
    xs, ys = [], []
    for values, short, same in ((xs, 0x02, 0x10), (ys, 0x04, 0x20)):
        position = 0
        for flag in flags:
            if flag & short:
                delta = data[at]
                at += 1
                position += delta if flag & same else -delta
            elif not flag & same:
                position += struct.unpack('>h', data[at:at + 2])[0]
                at += 2
            values.append(position)
    contours, begin = [], 0
    for end in ends:
        contours.append([((xs[index], ys[index]), bool(flags[index] & 0x01)) for index in range(begin, end + 1)])
        begin = end + 1
    return contours


def _segments(points):
    """Turn TrueType points (on or off the curve) into line and quadratic segments."""
    if not points:
        return []
    count = len(points)
    # Start from an on-curve point; with none, from the midpoint of the first two.
    first_on = next((index for index, (_, on) in enumerate(points) if on), None)
    if first_on is None:
        (x0, y0), _ = points[0]
        (x1, y1), _ = points[1 % count]
        start = (Fraction(x0 + x1, 2), Fraction(y0 + y1, 2))
        order = points[1:] + points[:1]
    else:
        start = points[first_on][0]
        order = points[first_on + 1:] + points[:first_on + 1]
    segments, current, control = [], start, None
    for point, on in order:
        if on:
            if control is None:
                segments.append(('L', current, point))
            else:
                segments.append(('Q', current, control, point))
            current, control = point, None
        else:
            if control is not None:
                middle = (Fraction(control[0] + point[0], 2), Fraction(control[1] + point[1], 2))
                segments.append(('Q', current, control, middle))
                current = middle
            control = point
    if control is not None:
        segments.append(('Q', current, control, start))
    elif current != start:
        segments.append(('L', current, start))
    return segments
