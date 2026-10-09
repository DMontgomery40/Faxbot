"""The pinned form renderer: (form version, values, renderer version, resolution) -> fax pages.

The promise partners exchange is the *exact fax raster*: for each page, the
SHA-256 of the canonical page (``page_hash``), a short header naming the
renderer version, resolution and size, then the packed bilevel rows (most
significant bit first, 1 for black, each row padded to a byte). PDF, TIFF and
PNG bytes are never hashed, because encoders and their metadata differ by
version. The header line a fax engine adds at the top of each page (47 CFR
68.318(d), with the time of sending) is not part of the page: it differs on
every call.

What makes the same input give the same dots everywhere:

- the page background is the bilevel raster stored with the form version at
  import (``importer.py``), never rasterized again;
- text is drawn from the font pinned in ``fonts/`` (DejaVu Sans 2.35, checked
  by ``FONT_SHA256``) by this module's own integer rasterizer, never by
  FreeType or a system font, with no hinting, kerning or anti-aliasing;
- signatures are carried as bilevel bitmaps and scaled by integer arithmetic;
- no clock, locale, random number or hash-seeded ordering is read.

Changing anything that moves a dot needs a new ``RENDERER`` version, which is
part of every page hash.
"""
from dataclasses import dataclass
from fractions import Fraction
import base64
import hashlib
import io
import math
from pathlib import Path
import tempfile

from . import model
from .raster import Bitmap, SUB, fill, quadratic, scale_bits, spans_of, thick_line
from .ttf import Font


RENDERER = 'faxbot-forms-1'
RENDERERS = (RENDERER,)
FONT_PATH = Path(__file__).resolve().parent / 'fonts' / 'DejaVuSans.ttf'
FONT_SHA256 = '3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22'
RESOLUTIONS = {'fine': (model.X_DPI, model.Y_DPI), 'standard': (model.X_DPI, model.Y_DPI // 2)}
PADDING = 4  # dots between a field's edge and its text


class RendererUnavailable(RuntimeError):
    """The pinned font is missing or changed; Faxbot cannot promise exact pages."""


_font = None
_glyphs = {}


def font():
    global _font
    if _font is None:
        try:
            data = FONT_PATH.read_bytes()
        except OSError:
            raise RendererUnavailable('The form font is missing from this installation.') from None
        if hashlib.sha256(data).hexdigest() != FONT_SHA256:
            raise RendererUnavailable('The form font on this installation is not the pinned one.')
        _font = Font(data)
    return _font


def missing_characters(value):
    """Characters of ``value`` the pinned font cannot draw, as one string ('' when none)."""
    pinned = font()
    missing = []
    for character in model.text(value):
        if character in ('\n', ' ') or pinned.glyph(character) is not None:
            continue
        if character not in missing:
            missing.append(character)
    return ''.join(missing)


def _scale(size):
    pinned = font()
    return (Fraction(size * model.X_DPI, 72 * pinned.units_per_em),
            Fraction(size * model.Y_DPI, 72 * pinned.units_per_em))


def _glyph(glyph, size):
    """A glyph's black spans relative to its pen position on the baseline (cached)."""
    key = (glyph, size)
    if key not in _glyphs:
        sx, sy = _scale(size)
        steps = 4 if size <= 12 else 8
        edges = []
        for contour in font().outline(glyph):
            points = []
            for segment in contour:
                if segment[0] == 'L':
                    points.append(segment[2])
                else:
                    points.extend(quadratic(segment[1], segment[2], segment[3], steps))
            if len(points) < 2:
                continue
            fixed_points = [(math.floor(x * sx * SUB), math.floor(-y * sy * SUB)) for x, y in points]
            edges.extend((*fixed_points[index], *fixed_points[(index + 1) % len(fixed_points)])
                         for index in range(len(fixed_points)))
        _glyphs[key] = tuple(fill(edges))
    return _glyphs[key]


def text_width(line, size):
    pinned = font()
    sx, _ = _scale(size)
    glyphs = [pinned.glyph(c) for c in line]
    return math.floor(sum(pinned.advance(glyph) for glyph in glyphs if glyph is not None) * sx)


def _metrics(size):
    pinned = font()
    _, sy = _scale(size)
    return math.floor(pinned.ascender * sy), math.floor((pinned.ascender - pinned.descender) * sy)


def _draw_line(page, line, x, baseline, size, clip):
    pinned = font()
    sx, _ = _scale(size)
    pen = Fraction(0)
    left, top, right, bottom = clip
    for character in line:
        glyph = pinned.glyph(character)
        if glyph is None:
            continue
        origin = x + math.floor(pen)
        for row, x0, x1 in _glyph(glyph, size):
            y = baseline + row
            if top <= y < bottom:
                page.span(y, max(left, origin + x0), min(right, origin + x1))
        pen += pinned.advance(glyph) * sx


def _fit(line, size, room):
    while size > model.MIN_FONT and text_width(line, size) > room:
        size -= 1
    return size


def _wrap(value, size, room):
    lines = []
    for paragraph in value.split('\n'):
        words, current = paragraph.split(' '), ''
        for word in words:
            candidate = word if not current else current + ' ' + word
            if current and text_width(candidate, size) > room:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def _draw_text(page, item, value):
    x, y, width, height = item['box']
    room = width - 2 * PADDING
    clip = (x, y, x + width, y + height)
    size = item['font_size']
    shown = model.shown(item, value)
    if item.get('multiline'):
        lines = _wrap(shown, size, room)
        ascent, line_height = _metrics(size)
        top = y + PADDING
    else:
        size = _fit(shown, size, room)
        lines = [shown]
        ascent, line_height = _metrics(size)
        top = y + (height - line_height) // 2
    for index, line in enumerate(lines):
        line_width = text_width(line, size)
        if item['align'] == 'right':
            left = x + width - PADDING - line_width
        elif item['align'] == 'center':
            left = x + (width - line_width) // 2
        else:
            left = x + PADDING
        _draw_line(page, line, left, top + index * line_height + ascent, size, clip)


def _mark(page, box):
    """A cross filling a box: the mark for a checked box or a chosen option."""
    x, y, width, height = box
    margin = max(1, min(width, height) // 5)
    stroke = max(2, min(width, height) // 8)
    for x0, y0, x1, y1 in ((x + margin, y + margin, x + width - margin, y + height - margin),
                           (x + width - margin, y + margin, x + margin, y + height - margin)):
        for row, a, b in fill(thick_line(x0, y0, x1, y1, stroke)):
            if y <= row < y + height:
                page.span(row, max(x, a), min(x + width, b))


def _draw_signature(page, item, value):
    x, y, width, height = item['box']
    source = Bitmap.from_packed(value['width'], value['height'], base64.b64decode(value['bits']))
    room_w, room_h = width - 2 * PADDING, height - 2 * PADDING
    scale = min(Fraction(room_w, source.width), Fraction(room_h, source.height))
    target_w = max(1, math.floor(source.width * scale))
    target_h = max(1, math.floor(source.height * scale))
    scaled = scale_bits(source, target_w, target_h)
    page.blit(spans_of(scaled), x + PADDING, y + (height - target_h) // 2)


@dataclass(frozen=True)
class Rendered:
    pages: tuple
    hashes: tuple
    resolution: str
    renderer: str


def render(form_content, backgrounds, values, *, resolution='fine', renderer=RENDERER):
    """Draw ``values`` (already normalized) on a form's pages.

    ``backgrounds`` holds one Bitmap per page, at 204 by 196 dots per inch.
    """
    if renderer not in RENDERERS:
        raise RendererUnavailable('This installation does not have that form renderer version.')
    if resolution not in RESOLUTIONS:
        raise model.FormError('The resolution must be fine or standard.')
    pages = [background.copy() for background in backgrounds]
    for item in form_content['fields']:
        value = values.get(item['name'])
        if value is None:
            continue
        page = pages[item['page'] - 1]
        if item['type'] == 'checkbox':
            if value is True:
                _mark(page, item['box'])
        elif item['type'] == 'signature':
            _draw_signature(page, item, value)
        elif item['type'] == 'choice' and value in item.get('option_boxes', {}):
            _mark(page, item['option_boxes'][value])
        else:
            _draw_text(page, item, value)
    if resolution == 'standard':
        pages = [page.halved() for page in pages]
    hashes = tuple(page_hash(page, renderer=renderer, resolution=resolution) for page in pages)
    return Rendered(tuple(pages), hashes, resolution, renderer)


def page_hash(page, *, renderer, resolution):
    """SHA-256 of the canonical page: the promise a partner checks."""
    x_dpi, y_dpi = RESOLUTIONS[resolution]
    header = model.canonical({'renderer': renderer, 'resolution': resolution, 'x_dpi': x_dpi, 'y_dpi': y_dpi,
                              'width': page.width, 'height': page.height})
    digest = hashlib.sha256(b'faxbot-page\x00' + header + b'\x00')
    digest.update(page.packed())
    return digest.hexdigest()


def render_address(form_address, values, *, renderer, resolution):
    """One address for (form version, values, renderer version, resolution)."""
    return model.sha256(model.canonical({'form': form_address, 'values': values, 'renderer': renderer,
                                         'resolution': resolution}))


# Output files (not part of the promise) --------------------------------------------------------

def _image(page, resolution):
    from PIL import Image
    inverted = bytes(255 - value for value in range(256))
    image = Image.frombytes('1', (page.width, page.height), page.packed().translate(inverted))
    image.info['dpi'] = RESOLUTIONS[resolution]
    return image


def to_tiff(rendered):
    """A Group 4 fax TIFF of the pages, each at its resolution."""
    images = [_image(page, rendered.resolution) for page in rendered.pages]
    output = io.BytesIO()
    images[0].save(output, 'TIFF', compression='group4', dpi=RESOLUTIONS[rendered.resolution], save_all=True,
                   append_images=images[1:])
    return output.getvalue()


def to_pdf(rendered):
    """A PDF whose pages are the one-bit pages, one image dot to one fax dot.

    Each page is 1728 dots wide at 204 dots per inch, so rasterizing it at the
    fax resolution (as Faxbot's trunk path does) gives back the same dots.
    """
    from ..conversion import tiff_to_pdf
    with tempfile.TemporaryDirectory(prefix='faxbot-form-') as folder:
        tiff, pdf = Path(folder) / 'pages.tiff', Path(folder) / 'pages.pdf'
        tiff.write_bytes(to_tiff(rendered))
        tiff_to_pdf(str(tiff), str(pdf))
        return pdf.read_bytes()


def to_png(page, resolution='fine'):
    """A preview picture of one page."""
    output = io.BytesIO()
    _image(page, resolution).save(output, 'PNG', dpi=RESOLUTIONS[resolution])
    return output.getvalue()
