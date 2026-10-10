"""Fax-friendly screens: shaded areas kept with a pattern the fax codec codes in a few runs; text is never touched.

A fax codes each scan line as runs of white and black (MH, MR, MMR). The
halftone Ghostscript draws for a gray area is a dot for every few pixels, so a
shaded table row or a tinted form field costs a short run for every dot and can
take longer on the line than all the text on the page. Making light areas white
(``friendly.lighten``) removes that cost but also erases pale text: in AR's
benchmark the 15% and 25% gray text bands lost every fax pixel. This module
keeps the shading and changes only how it is drawn:

- Ghostscript draws the page in gray as well as the fax image Faxbot sends
  today. A pixel is *frozen* (left exactly as today) when it lies within
  ``BAND`` pixels of any change of gray, when it is pure black or pure white,
  when it belongs to a text or annotation object the PDF draws
  (``object_masks``), when it fills a gap of at most ``2 * WORD_GAP`` pixels
  between such marks on a line (so no stripe runs between two words like an
  underline), or when the pattern could not draw its gray with both black and
  white.
- What is left are uniform midtone interiors. Only those, and only where the
  interior is at least ``MIN_WIDTH`` pixels wide and one pattern period tall
  (so a shaded checkbox never gets a line inside it), are drawn with a screen:
  by default horizontal stripes with one global phase (``lines``, every
  ``PERIOD`` rows), which give the codec one run per stripe and line up across
  regions.
- Unknown or ambiguous areas (a scan's noise, a photograph, a gradient) have no
  uniform interior and stay as they are. A page whose gray drawing does not
  line up with its fax image is not changed at all.

Measured on AR's synthetic corpus (MMR bits / 14,400 bit/s, the figures Codex's
probe reported, ``research/faxbot-next-experiments-2026-10-08``): a shaded table
61 -> 26 seconds and a tinted form 51 -> 12, while the pale-text bands stay
pixel for pixel; a noisy scan, a photograph and black text do not change.

Prior art: G. Feng, M. G. Fuchs and C. A. Bouman, "Image Rendering for Digital
Fax", Proc. SPIE-IS&T Electronic Imaging (2003),
https://engineering.purdue.edu/~bouman/publications/pdf/ei03Feng.pdf. Their
ReadableFax keeps detected text and edges and fills the remaining background
with a 3 x 6 45-degree clustered-dot screen (their Figure 5) because clustered
dots compress far better under G3/G4 than error diffusion. Both are here for
comparison (``feng`` and the coarser orthogonal ``dots``); on these pages
horizontal stripes code smallest (``tests/test_fax_screens.py`` measures all
three), so stripes are what Faxbot sends.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import subprocess
import warnings

from PIL import Image, ImageChops

# Pixels frozen around every change of gray: a 9 x 9 window, as Codex's probe (``probe.py``) measured.
BAND = 4
# A gap between marks on one line of at most twice this is frozen too (word spaces, digits in a date).
WORD_GAP = 12
# A screened interior is at least this wide (about 3 mm at 204 dots per inch)...
MIN_WIDTH = 25
# ... and one stripe period tall: rows per period of the line screen.
PERIOD = 8

# Feng, Fuchs and Bouman's ReadableFax screen (Figure 5): a 3 x 6 45-degree clustered dot, 18 levels.
FENG_MATRIX = ((10, 12, 11, 9, 7, 8),
               (13, 18, 17, 6, 1, 2),
               (14, 15, 16, 5, 4, 3),
               (9, 7, 8, 10, 12, 11),
               (6, 1, 2, 13, 18, 17),
               (5, 4, 3, 14, 15, 16))
DOT_CELL = 8  # the orthogonal clustered-dot screen's cell, in pixels
PATTERNS = ('lines', 'dots', 'feng')
DEFAULT_PATTERN = 'lines'


def _lut(predicate):
    return [255 if predicate(value) else 0 for value in range(256)]


_ANY = _lut(lambda value: value > 0)
_NONE = _lut(lambda value: value == 0)


# Morphology on mode "L" images, separable and in a few whole-image steps ------------------------------------

def _shifted(image, dx, dy, fill):
    """``image`` moved so that pixel (x, y) of the result is pixel (x + dx, y + dy) of it; ``fill`` outside."""
    out = Image.new(image.mode, image.size, fill)
    out.paste(image, (-dx, -dy))
    return out


def _extreme(image, low, high, axis, *, darker=False, fill=0):
    """Each pixel's maximum (or minimum, ``darker``) of ``image`` over the offsets ``low`` to ``high`` along
    ``axis`` ('x' or 'y'), worked out by doubling: a window of n pixels takes about log2(n) image operations."""
    if high < low:
        raise ValueError('Empty window')
    op = ImageChops.darker if darker else ImageChops.lighter

    def moved(source, offset):
        return _shifted(source, offset, 0, fill) if axis == 'x' else _shifted(source, 0, offset, fill)
    # Worked out on a canvas padded with ``fill`` on both sides, so a pixel near the page's edge still sees every
    # pixel of the page inside its window.
    pad = max(abs(low), abs(high))
    width, height = image.size
    canvas = Image.new(image.mode, (width + 2 * pad, height) if axis == 'x' else (width, height + 2 * pad), fill)
    canvas.paste(image, (pad, 0) if axis == 'x' else (0, pad))
    size = high - low + 1
    covered, span = canvas, 1  # ``covered`` holds the extreme over offsets 0 .. span - 1
    while span * 2 <= size:
        covered = op(covered, moved(covered, span))
        span *= 2
    if span < size:
        covered = op(covered, moved(covered, size - span))
    if low:
        covered = moved(covered, low)
    return covered.crop((pad, 0, pad + width, height) if axis == 'x' else (0, pad, width, pad + height))


def dilate(mask, left, right, up=None, down=None):
    """Set pixels of a 0/255 mask grown by ``left``/``right`` pixels across and ``up``/``down`` (default the same)
    down the page: a pixel is set when any pixel in that window around it is set."""
    up = left if up is None else up
    down = right if down is None else down
    grown = _extreme(mask, -right, left, 'x') if left or right else mask
    return _extreme(grown, -down, up, 'y') if up or down else grown


def _close_across(mask, radius):
    """Gaps of at most 2 x ``radius`` pixels between set pixels on the same row filled (a closing)."""
    grown = _extreme(mask, -radius, radius, 'x')
    return _extreme(grown, -radius, radius, 'x', darker=True, fill=255)


def _open(mask, width, height):
    """Only set pixels that belong to a fully set ``width`` x ``height`` rectangle kept (an opening)."""
    kept = mask
    if width > 1:
        kept = _extreme(_extreme(kept, 0, width - 1, 'x', darker=True), -(width - 1), 0, 'x')
    if height > 1:
        kept = _extreme(_extreme(kept, 0, height - 1, 'y', darker=True), -(height - 1), 0, 'y')
    return kept


def count(mask):
    """Set pixels of a mode "1" or 0/255 mask."""
    return mask.histogram()[255]


# What may change ----------------------------------------------------------------------------------------------

def tonal_edges(gray):
    """(across, down): 0/255 masks of the pixels whose gray differs from the next pixel to the right, or below.
    Beyond the page is white paper."""
    right = _shifted(gray, 1, 0, 255)
    below = _shifted(gray, 0, 1, 255)
    return ImageChops.difference(gray, right).point(_ANY), ImageChops.difference(gray, below).point(_ANY)


def edge_band(gray):
    """0/255 mask of the pixels within ``BAND`` of a change of gray: exactly the pixels whose 9 x 9 window is not
    all one gray (Codex's ``MaxFilter(9) - MinFilter(9)``). What lies beyond the page is unknown, so the
    ``BAND`` pixels along every edge of the page are in the band too."""
    across, down = tonal_edges(gray)
    # A pair (x, x + 1) that differs is inside the window of every pixel from x - 3 to x + 4 across, and from
    # 4 rows above to 4 below; a pair (y, y + 1) the same with the axes swapped.
    band = ImageChops.lighter(dilate(across, BAND - 1, BAND, BAND, BAND),
                              dilate(down, BAND, BAND, BAND - 1, BAND))
    width, height = gray.size
    if width > 2 * BAND and height > 2 * BAND:
        frame = Image.new('L', gray.size, 255)
        frame.paste(0, (BAND, BAND, width - BAND, height - BAND))
        return ImageChops.lighter(band, frame)
    return Image.new('L', gray.size, 255)


def thresholds(size, pattern=DEFAULT_PATTERN):
    """The screen as a mode "L" plane: a pixel is black where the page's gray is darker than the plane. One
    global phase, so screened areas line up wherever they meet."""
    width, height = size
    if pattern == 'lines':
        column = bytes(round(((y % PERIOD) + .5) * 255 / PERIOD) for y in range(height))
        return Image.frombytes('L', (1, height), column).resize(size, Image.Resampling.NEAREST)
    if pattern == 'feng':
        matrix = FENG_MATRIX
    elif pattern == 'dots':
        matrix = _clustered_dot(DOT_CELL)
    else:
        raise ValueError('Unknown screen')
    levels = len(matrix) * len(matrix[0])
    tile = Image.new('L', (len(matrix[0]), len(matrix)))
    tile.putdata([round(255 - (value - .5) * 255 / levels) for row in matrix for value in row])
    strip = Image.new('L', (width, tile.height))
    for x in range(0, width, tile.width):
        strip.paste(tile, (x, 0))
    plane = Image.new('L', size)
    for y in range(0, height, tile.height):
        plane.paste(strip, (0, y))
    return plane


def _clustered_dot(cell):
    """An orthogonal clustered-dot threshold matrix: cells fill from the centre outward (a round dot that grows)."""
    centre = (cell - 1) / 2
    order = sorted(((x - centre) ** 2 + (y - centre) ** 2, math.atan2(y - centre, x - centre), x, y)
                   for y in range(cell) for x in range(cell))
    matrix = [[0] * cell for _ in range(cell)]
    for rank, (_, _, x, y) in enumerate(order, 1):
        matrix[y][x] = rank
    return tuple(tuple(row) for row in matrix)


def drawable(pattern=DEFAULT_PATTERN):
    """(lightest, darkest) gray the screen draws with both black and white pixels; outside, a flat area would come
    out all white or all black, so it is left as it is."""
    plane = thresholds((len(FENG_MATRIX[0]) * DOT_CELL, PERIOD * DOT_CELL), pattern)
    low, high = plane.getextrema()
    return low, high - 1


def frozen_mask(gray, objects=None, pattern=DEFAULT_PATTERN, *, word_gap=WORD_GAP, min_width=MIN_WIDTH,
                min_height=PERIOD):
    """Mode "1" mask of every pixel that must stay exactly as today (see the module's description). The keyword
    arguments exist to measure what each protection costs (``tests/test_fax_screens.py``); Faxbot uses the
    defaults."""
    lightest, darkest = drawable(pattern)
    detail = edge_band(gray)
    marks = ImageChops.lighter(*tonal_edges(gray))
    if objects is not None:
        objects = objects.convert('L') if objects.mode != 'L' else objects
        marks = ImageChops.lighter(marks, objects)
        detail = ImageChops.lighter(detail, objects)
    if word_gap:
        detail = ImageChops.lighter(detail, _close_across(marks, word_gap))
    outside = gray.point(_lut(lambda value: not lightest <= value <= darkest))
    frozen = ImageChops.lighter(detail, outside)
    # What is left must be wide enough and one period tall; everything else is frozen too.
    screenable = _open(ImageChops.invert(frozen), min_width, min_height)
    return ImageChops.invert(screenable).convert('1')


@dataclass(frozen=True)
class Screened:
    page: Image.Image  # mode "1", the fax image's size and resolution
    changed: int  # pixels that differ from today's image
    screened: int  # pixels in screened interiors
    frozen: Image.Image  # mode "1": the pixels that were left exactly as today
    pattern: str


def screen_page(gray, halftone, *, objects=None, pattern=DEFAULT_PATTERN, **protections):
    """The protected rendering of one page: today's fax image with only its uniform midtone interiors drawn with
    ``pattern``. ``gray`` is the page drawn in gray (mode "L", the fax image's size), ``halftone`` today's fax image
    (mode "1"), ``objects`` an optional mode "1" mask of the PDF's text and annotations on this page. None when the
    two drawings do not line up (send the page as it is)."""
    from .friendly import aligned, bits, fit
    dpi = halftone.info.get('dpi')
    today = bits(halftone)
    gray = fit(gray, today.size)
    if not aligned(gray, today):
        return None
    if objects is not None and objects.size != today.size:
        raise ValueError('The text mask is not the size of the page')
    frozen = frozen_mask(gray, objects, pattern, **protections)
    screen = ImageChops.subtract(thresholds(today.size, pattern), gray).point(_NONE, '1')
    page = Image.composite(today, screen, frozen)
    page = bits(page)
    page.info['dpi'] = dpi
    return Screened(page, count(ImageChops.logical_xor(page, today)), count(ImageChops.invert(frozen)), frozen,
                    pattern)


# Text and annotation objects, from the PDF itself ---------------------------------------------------------------

class MasksUnavailable(Exception):
    """The PDF's text and annotations could not be drawn on their own; its pages are not screened."""


def _render(gs, pdf_path, out_path, options):
    from ..conversion import GHOSTSCRIPT_TIMEOUT_SECONDS, ghostscript_slot
    with ghostscript_slot():  # at most conversion.GHOSTSCRIPT_SLOTS drawings at once
        subprocess.run(
            [gs, '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-dPDFSTOPONERROR', *options, '-sDEVICE=tiffgray',
             '-sCompression=lzw', '-r204x196', f'-sOutputFile={out_path}', '-f', str(Path(pdf_path).resolve())],
            check=True, timeout=GHOSTSCRIPT_TIMEOUT_SECONDS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _frames(path, sizes):
    """The gray pages of a Ghostscript drawing, each on its fax page's canvas, one at a time; refuses a different
    page count (fewer pages before the missing one, more once every page was read)."""
    from .friendly import fit
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as drawing:
            for index, size in enumerate(sizes):
                try:
                    drawing.seek(index)
                except EOFError:
                    raise MasksUnavailable('The drawing has fewer pages') from None
                yield fit(drawing.copy(), size)
            try:
                drawing.seek(len(sizes))
            except EOFError:
                return
    raise MasksUnavailable('The drawing has more pages')


def has_annotations(pdf_path):
    """Whether any page of the PDF carries annotations (form fields, stamps, highlights, notes, links)."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    try:
        return any('/Annots' in page for page in PdfReader(str(pdf_path)).pages)
    except (PyPdfError, OSError, ValueError, KeyError) as error:
        raise MasksUnavailable('The PDF could not be read for annotations') from error


def object_masks(pdf_path, gray_pages, gs, folder):
    """One mode "1" mask per page of the pixels the PDF's text and annotation objects draw.

    Text: Ghostscript draws the page again with images and vector graphics left out (``-dFILTERIMAGE
    -dFILTERVECTOR``); every pixel that is not white paper is text. Annotations (only when the PDF has any): the
    page drawn without annotations and form fields, compared with ``gray_pages`` (the full gray drawing); every
    pixel that differs is an annotation's. Raises MasksUnavailable when either drawing fails."""
    import os
    import tempfile
    from ..conversion import FaxFrames
    sizes = list(gray_pages.sizes) if hasattr(gray_pages, 'path') else [page.size for page in gray_pages]
    handle, scratch = tempfile.mkstemp(prefix='.faxbot-screens-', suffix='.tiff', dir=str(folder))
    os.close(handle)
    try:
        try:
            _render(gs, pdf_path, scratch, ('-dFILTERIMAGE', '-dFILTERVECTOR'))
        except (subprocess.SubprocessError, OSError) as error:
            raise MasksUnavailable('The text could not be drawn on its own') from error
        # Packed (FaxFrames): one mask made at a time, as the pages are screened.
        masks = FaxFrames(text.point(_lut(lambda value: value < 255), '1') for text in _frames(scratch, sizes))
        if has_annotations(pdf_path):
            try:
                _render(gs, pdf_path, scratch, ('-dShowAnnots=false', '-dShowAcroForm=false'))
            except (subprocess.SubprocessError, OSError) as error:
                raise MasksUnavailable('The page could not be drawn without its annotations') from error
            for index, plain in enumerate(_frames(scratch, sizes)):
                drawn = ImageChops.difference(gray_pages[index], plain).point(_ANY, '1')
                masks[index] = ImageChops.logical_or(masks[index], drawn)
        return masks
    finally:
        Path(scratch).unlink(missing_ok=True)
