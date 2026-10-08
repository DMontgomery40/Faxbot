"""Whether a changed fax page still shows everything today's page shows, and how close it looks.

Faxbot changes a document's pages only to send them in less time
(``screens.py``: shaded areas drawn with stripes; ``friendly.lighten``: light
areas made white, only with the administrator's opt-in). Counting changed
pixels says little: a page whose gray table rows became stripes changes a
hundred thousand pixels and loses nothing, while a page that lost one decimal
point changes six pixels and now says 150 instead of 1.50. So each changed
page is checked against today's page (the halftone Faxbot sends unchanged) and
the page drawn in gray, one kind of content at a time:

- ``marks``: every mark that is darker than what is around it and narrower than
  ``2 * MARK_RADIUS + 1`` pixels (small type, faint handwriting, a decimal
  point, a check mark, a box's outline, a barcode's bars, a stamp's lines) is
  found in the gray drawing with a black top-hat, and each of its pixels must be
  exactly as today. ``pale_marks`` names the same loss when the marks lost are
  light (25% gray or lighter): pale text and light marks.
- ``shading``: in every ``BLOCK`` x ``BLOCK`` block of uniform shading (a table
  row, a form field, a highlight), the page must still put ink on the paper,
  close to the shading's own tone (within ``TONE_TOLERANCE``), so a highlight
  stays visibly distinct and a shaded field still reads as shaded.
- ``small_areas``: shaded areas too small for a pattern (a shaded checkbox, a
  narrow cell; under ``screens.MIN_WIDTH`` wide or one stripe period tall) must
  be exactly as today, so no stripe inside a box can read as a mark.
- ``faded``: anywhere on the page (a picture's light parts, a pale scan), a
  block that had ink (at least ``FADED_INK`` of it) must keep at least a third
  of it, so nothing light quietly turns into blank paper.
- ``frozen``: with the renderer's own frozen mask, every frozen pixel must be
  exactly as today.

A page passes (``kept``) when it has none of these losses. ``difference`` says
how far it looks from today's page at reading distance (both blurred by
``BLUR`` pixels, mean absolute difference, 0 to 1); among pages that keep
everything, the smaller difference is the more faithful.
"""
from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter, ImageStat

from . import screens

# Marks: darker than the gray around them by at least MARK_CONTRAST levels, and narrower than 15 pixels
# (about 1.9 mm at 204 dots per inch: wider than any stroke of body text, a decimal point or a barcode bar).
MARK_RADIUS = 7
MARK_CONTRAST = 16
# Gray at or above this is pale (25% gray or lighter, the level AR's whitening made white).
PALE_LEVEL = 191
# Shading is checked in blocks this size (two stripe periods), where at least half the block is shading.
BLOCK = 16
TONE_TOLERANCE = 0.15  # share of the block: the page's ink may differ from the shading's own darkness by this
FADED_INK = 0.03  # share of a block: below this, a block has too little ink to say it faded
BLUR = 2  # pixels: the reading-distance blur for ``difference``

LOSSES = ('frozen', 'marks', 'pale_marks', 'shading', 'small_areas', 'faded')


def _lut(predicate):
    return [255 if predicate(value) else 0 for value in range(256)]


def marks(gray):
    """Mode "L" 0/255 mask of the marks on a gray page (see the module's description)."""
    def extreme(image, darker):
        image = screens._extreme(image, -MARK_RADIUS, MARK_RADIUS, 'x', darker=darker, fill=255)
        return screens._extreme(image, -MARK_RADIUS, MARK_RADIUS, 'y', darker=darker, fill=255)
    background = extreme(extreme(gray, darker=False), darker=True)
    return ImageChops.subtract(background, gray).point(_lut(lambda value: value >= MARK_CONTRAST))


def shading(gray):
    """Mode "L" 0/255 mask of uniform midtone shading: no change of gray within ``screens.BAND`` pixels."""
    midtone = gray.point(_lut(lambda value: 0 < value < 255))
    return ImageChops.darker(midtone, ImageChops.invert(screens.edge_band(gray)))


def _ink(page):
    """Mode "L" 0/255 mask of a fax page's black pixels."""
    from .friendly import bits
    return ImageChops.invert(bits(page).convert('L'))


@dataclass(frozen=True)
class Fidelity:
    kept: bool
    losses: tuple[str, ...]  # names from LOSSES, in that order
    changed: int  # pixels that differ from today's page
    difference: float  # 0 (looks the same at reading distance) to 1

    @property
    def rank(self):
        """Sort key: the most faithful first."""
        return (not self.kept, len(self.losses), self.difference)


UNCHANGED = Fidelity(True, (), 0, 0.0)


def assess(gray, today, candidate, *, frozen=None):
    """How ``candidate`` keeps what ``today`` shows: a Fidelity. ``gray`` is the page drawn in gray (mode "L", the
    fax image's size), ``today`` and ``candidate`` mode "1" fax pages of that size. ``frozen``: the renderer's
    mode "1" mask of pixels it promised to leave as today."""
    from .friendly import bits, fit
    today, candidate = bits(today), bits(candidate)
    if candidate.size != today.size:
        raise ValueError('The changed page is not the size of the page')
    gray = fit(gray, today.size)
    changed_mask = ImageChops.logical_xor(today, candidate)
    changed = screens.count(changed_mask)
    if not changed:
        return UNCHANGED
    changed_mask = changed_mask.convert('L')
    losses = set()
    if frozen is not None and screens.count(ImageChops.logical_and(changed_mask.convert('1'), frozen)):
        losses.add('frozen')
    found = marks(gray)
    lost = ImageChops.darker(found, changed_mask)
    if lost.getbbox() is not None:
        pale = gray.point(_lut(lambda value: value >= PALE_LEVEL))
        if ImageChops.darker(lost, pale).getbbox() is not None:
            losses.add('pale_marks')
        if ImageChops.darker(lost, ImageChops.invert(pale)).getbbox() is not None:
            losses.add('marks')
    shaded = shading(gray)
    roomy = screens._open(shaded, screens.MIN_WIDTH, screens.PERIOD)
    small = ImageChops.subtract(shaded, roomy)
    if ImageChops.darker(small, changed_mask).getbbox() is not None:
        losses.add('small_areas')
    ink_today, ink_after = _ink(today), _ink(candidate)
    if _shading_lost(gray, shaded, ink_today, ink_after):
        losses.add('shading')
    if _faded(ink_today, ink_after):
        losses.add('faded')
    before = today.convert('L').filter(ImageFilter.GaussianBlur(BLUR))
    after = candidate.convert('L').filter(ImageFilter.GaussianBlur(BLUR))
    difference = ImageStat.Stat(ImageChops.difference(before, after)).mean[0] / 255
    ordered = tuple(name for name in LOSSES if name in losses)
    return Fidelity(not ordered, ordered, changed, round(difference, 6))


def _blocks(mask):
    """The mean of a 0/255 mask over each ``BLOCK`` x ``BLOCK`` block (0 to 255), on a canvas padded to whole
    blocks with nothing set."""
    width, height = mask.size
    canvas = Image.new('L', ((width + BLOCK - 1) // BLOCK * BLOCK, (height + BLOCK - 1) // BLOCK * BLOCK), 0)
    canvas.paste(mask, (0, 0))
    return canvas.reduce(BLOCK).tobytes()


def _faded(ink_today, ink_after):
    """Whether any block that had ink lost more than two thirds of it."""
    least = FADED_INK * 255
    return any(before >= least and now * 3 < before for before, now in zip(_blocks(ink_today), _blocks(ink_after)))


def _shading_lost(gray, shaded, ink_today, ink_after):
    """Whether any block of shading lost its tone: no ink where today had some, or ink far from the gray."""
    share = _blocks
    area = share(shaded)
    darkness = share(ImageChops.darker(ImageChops.invert(gray), shaded))
    today = share(ImageChops.darker(ink_today, shaded))
    after = share(ImageChops.darker(ink_after, shaded))
    for covered, dark, before, now in zip(area, darkness, today, after):
        if covered < 128:  # less than half the block is shading
            continue
        tone = dark / covered
        if before and not now:
            return True
        if abs(now / covered - tone) > TONE_TOLERANCE and abs(now - before) / covered > TONE_TOLERANCE:
            return True
    return False


def assess_pages(grays, todays, candidates, *, frozen=None):
    """One Fidelity for a document: kept when every page is, every page's losses, pixels changed in all, and the
    mean difference over the pages."""
    if not (len(grays) == len(todays) == len(candidates)):
        raise ValueError('Every page needs its gray drawing, its fax page and its changed page')
    pages = [assess(gray, today, candidate, frozen=None if frozen is None else frozen[index])
             for index, (gray, today, candidate) in enumerate(zip(grays, todays, candidates))]
    if not pages:
        return UNCHANGED
    losses = {name for page in pages for name in page.losses}
    ordered = tuple(name for name in LOSSES if name in losses)
    return Fidelity(not ordered, ordered, sum(page.changed for page in pages),
                    round(sum(page.difference for page in pages) / len(pages), 6))
