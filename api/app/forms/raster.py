"""Bilevel page bitmaps and an integer scanline rasterizer.

Everything here is integer or exact rational arithmetic in plain Python, so a
page comes out bit for bit the same on every host and in every process: no
floating point decides a dot, no image library draws, and nothing iterates
over an unordered collection. Coordinates are in device dots, with sub-dot
positions in fixed point (``SUB`` steps per dot). A dot is black when its
centre lies inside a shape under the non-zero winding rule.
"""
from fractions import Fraction
import math


SUB = 64  # fixed-point steps per dot
_TO_TEXT = bytes.maketrans(b'\x00\x01', b'01')
_FROM_TEXT = bytes.maketrans(b'01', b'\x00\x01')
# The 8x8 Bayer matrix: the fixed ordered dither for grey backgrounds.
BAYER = (
    (0, 32, 8, 40, 2, 34, 10, 42),
    (48, 16, 56, 24, 50, 18, 58, 26),
    (12, 44, 4, 36, 14, 46, 6, 38),
    (60, 28, 52, 20, 62, 30, 54, 22),
    (3, 35, 11, 43, 1, 33, 9, 41),
    (51, 19, 59, 27, 49, 17, 57, 25),
    (15, 47, 7, 39, 13, 45, 5, 37),
    (63, 31, 55, 23, 61, 29, 53, 21),
)


def fixed(value):
    """A rational dot coordinate as a fixed-point integer (rounded down)."""
    return math.floor(Fraction(value) * SUB)


class Bitmap:
    """A bilevel page: one byte per dot, 1 for black, 0 for white."""

    def __init__(self, width, height, rows=None):
        self.width, self.height = int(width), int(height)
        self.rows = rows if rows is not None else [bytearray(self.width) for _ in range(self.height)]
        self._ones = b'\x01' * self.width

    def copy(self):
        return Bitmap(self.width, self.height, [bytearray(row) for row in self.rows])

    def span(self, y, x0, x1):
        """Blacken dots x0 <= x < x1 of row y, clipped to the page."""
        if 0 <= y < self.height:
            x0, x1 = max(0, x0), min(self.width, x1)
            if x1 > x0:
                self.rows[y][x0:x1] = self._ones[:x1 - x0]

    def rectangle(self, x, y, width, height):
        for row in range(y, y + height):
            self.span(row, x, x + width)

    def frame(self, x, y, width, height, thickness=1):
        self.rectangle(x, y, width, thickness)
        self.rectangle(x, y + height - thickness, width, thickness)
        self.rectangle(x, y, thickness, height)
        self.rectangle(x + width - thickness, y, thickness, height)

    def blit(self, spans, x, y):
        """Blacken a shape's spans ((row, x0, x1), relative) placed at (x, y)."""
        for row, x0, x1 in spans:
            self.span(y + row, x + x0, x + x1)

    def packed(self):
        """The page as packed rows, most significant bit first, 1 for black, each row padded to a byte."""
        width = self.width
        pad = (-width) % 8
        length = (width + pad) // 8
        out = bytearray()
        tail = '0' * pad
        for row in self.rows:
            out += int(row.translate(_TO_TEXT).decode('ascii') + tail, 2).to_bytes(length, 'big')
        return bytes(out)

    @classmethod
    def from_packed(cls, width, height, data):
        length = (width + 7) // 8
        if len(data) != length * height:
            raise ValueError('the page data does not match its size')
        rows = []
        for index in range(height):
            text = bin(int.from_bytes(data[index * length:(index + 1) * length], 'big'))[2:].zfill(length * 8)
            rows.append(bytearray(text[:width].encode('ascii').translate(_FROM_TEXT)))
        return cls(width, height, rows)

    def halved(self):
        """Standard resolution: each pair of rows joined (a dot is black when either is)."""
        rows = []
        for index in range(0, self.height, 2):
            first = self.rows[index]
            second = self.rows[index + 1] if index + 1 < self.height else bytearray(self.width)
            rows.append(bytearray((int.from_bytes(first, 'big') | int.from_bytes(second, 'big'))
                                  .to_bytes(self.width, 'big')))
        return Bitmap(self.width, len(rows), rows)


def fill(edges):
    """Spans (row, x0, x1) covered by closed outlines under the non-zero rule.

    ``edges`` are (x0, y0, x1, y1) in fixed point. A dot is covered when its
    centre is inside, so the result depends on exact integer comparisons only.
    """
    edges = [edge for edge in edges if edge[1] != edge[3]]
    if not edges:
        return []
    top = min(min(edge[1], edge[3]) for edge in edges)
    bottom = max(max(edge[1], edge[3]) for edge in edges)
    first_row = (top - SUB // 2) // SUB
    last_row = (bottom - SUB // 2) // SUB + 1
    spans = []
    for row in range(first_row, last_row + 1):
        centre = row * SUB + SUB // 2
        crossings = []
        for x0, y0, x1, y1 in edges:
            if y0 < y1:
                low, high, direction = y0, y1, 1
            else:
                low, high, direction = y1, y0, -1
            if low <= centre < high:
                crossings.append((x0 + (centre - y0) * (x1 - x0) // (y1 - y0), direction))
        if not crossings:
            continue
        crossings.sort()
        winding = 0
        for index, (x, direction) in enumerate(crossings):
            before = winding
            winding += direction
            if before == 0 and winding != 0:
                start = x
            elif before != 0 and winding == 0:
                # Dots whose centre x satisfies start <= centre < x.
                first = -(-(start - SUB // 2) // SUB)
                last = -(-(x - SUB // 2) // SUB)
                if last > first:
                    spans.append((row, first, last))
    return spans


def quadratic(p0, control, p1, steps):
    """Points along a quadratic Bezier at t = k/steps, exact rationals."""
    points = []
    for k in range(1, steps + 1):
        t = Fraction(k, steps)
        u = 1 - t
        points.append((u * u * p0[0] + 2 * u * t * control[0] + t * t * p1[0],
                       u * u * p0[1] + 2 * u * t * control[1] + t * t * p1[1]))
    return points


def cubic(p0, c0, c1, p1, steps):
    points = []
    for k in range(1, steps + 1):
        t = Fraction(k, steps)
        u = 1 - t
        points.append((u ** 3 * p0[0] + 3 * u * u * t * c0[0] + 3 * u * t * t * c1[0] + t ** 3 * p1[0],
                       u ** 3 * p0[1] + 3 * u * u * t * c0[1] + 3 * u * t * t * c1[1] + t ** 3 * p1[1]))
    return points


def polygon_edges(points):
    """Closed polygon (dot coordinates, rationals) as fixed-point edges."""
    fixed_points = [(fixed(x), fixed(y)) for x, y in points]
    return [(*fixed_points[index], *fixed_points[(index + 1) % len(fixed_points)])
            for index in range(len(fixed_points))]


def thick_line(x0, y0, x1, y1, width):
    """Fixed-point edges of a line drawn ``width`` dots wide (a rectangle along it, square ends)."""
    x0, y0, x1, y1, width = (Fraction(value) for value in (x0, y0, x1, y1, width))
    dx, dy = x1 - x0, y1 - y0
    length_squared = dx * dx + dy * dy
    if length_squared == 0:
        half = width / 2
        return polygon_edges([(x0 - half, y0 - half), (x0 + half, y0 - half), (x0 + half, y0 + half),
                              (x0 - half, y0 + half)])
    # A rational stand-in for the unit normal: exact, and the same everywhere.
    length = Fraction(math.isqrt(int(length_squared * 1_000_000)), 1000)
    nx, ny = -dy * width / (2 * length), dx * width / (2 * length)
    ex, ey = dx * width / (2 * length), dy * width / (2 * length)
    return polygon_edges([(x0 + nx - ex, y0 + ny - ey), (x1 + nx + ex, y1 + ny + ey),
                          (x1 - nx + ex, y1 - ny + ey), (x0 - nx - ex, y0 - ny - ey)])


def dither(gray_rows, width):
    """Grey rows (0 black .. 255 white, one byte a dot) to bilevel rows with the fixed 8x8 ordered dither."""
    tables = [[bytes.maketrans(bytes(range(256)),
                               bytes(1 if value * 64 < (BAYER[y][x] * 2 + 1) * 128 else 0 for value in range(256)))
               for x in range(8)] for y in range(8)]
    rows = []
    for y, gray in enumerate(gray_rows):
        gray = bytes(gray[:width]).ljust(width, b'\xff')
        out = bytearray(width)
        phase = tables[y % 8]
        for x in range(8):
            out[x::8] = gray[x::8].translate(phase[x])
        rows.append(out)
    return rows


def scale_bits(source, target_width, target_height):
    """Nearest-dot scaling of a small bilevel bitmap; integer arithmetic only."""
    rows = []
    for y in range(target_height):
        row = source.rows[(y * source.height) // target_height]
        rows.append(bytearray(row[(x * source.width) // target_width] for x in range(target_width)))
    return Bitmap(target_width, target_height, rows)


def spans_of(bitmap):
    """The black spans of a bitmap, for blitting it onto a page."""
    spans = []
    for y, row in enumerate(bitmap.rows):
        x, width = 0, bitmap.width
        while x < width:
            start = row.find(1, x)
            if start < 0:
                break
            end = row.find(0, start)
            end = width if end < 0 else end
            spans.append((y, start, end))
            x = end
    return spans
