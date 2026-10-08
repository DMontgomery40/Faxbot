"""Fax-friendly pages: shaded areas kept with a pattern the fax codes quickly; light areas made white only on request.

Faxes are coded line by line in runs of white and black (MH, MR, MMR). A
page of black text on white is a few long runs per line; a gray area that
Ghostscript draws as a halftone of dots is a short run for every dot, so a
shaded table header or a tinted form field can cost more than all the text
on the page. Measured with ``scripts/fax_friendly_benchmark.py`` and Codex's
probe (``research/faxbot-next-experiments-2026-10-08``) on synthetic pages at
204 x 196 dots per inch, MMR at 14,400 bit/s:

- drawing the uniform inside of each shaded area with horizontal stripes
  (``screens.py``), while every pixel near text, lines, edges and marks stays
  exactly as Faxbot draws it today, took a shaded table from 61 to 27 seconds
  and a form with tinted fields from 51 to 13; pale text stays pixel for pixel;
- making every pixel that is 25% gray or lighter white saves more (61 to 12,
  51 to 2) but erases pale text: the 15% and 25% gray text bands lost every
  fax pixel, so it is only an opt-in (``fax_friendly_whiten``), with a warning;
- removing specks (a lone black pixel with only light gray around it) also
  removes the dots of pale text, so it goes only with that opt-in;
- the pages Faxbot draws itself and the experimental encoded pages are never
  changed.

Which pages go: the layout chooser of each attempt (``conversion.choose_layout``)
prices the pages as they are, the screened pages, and the whitened pages when
the opt-in is on, each with its own coding, and keeps the most faithful of the
ones whose expected bill is the lowest (``decision.choose``). A faster one wins
at the same bill only for a named reason (``named_reason``): a send-by time
within the hour, other faxes waiting for a line, or the administrator's choice
(the setting or the recipient at Always). The fax's own files never change.

How a page is changed: Ghostscript draws the page twice at the fax
resolution, once as today's fax image and once in gray, and (for the screen)
once more with only its text, to find each mark. When the two drawings do not
line up (a pure black or pure white gray pixel that is not the same in today's
image), the page goes exactly as it would have. Every changed page is checked
by ``fidelity.assess``; a screened page that would lose anything goes as it is.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from uuid import uuid4
import warnings

from PIL import Image, ImageChops, ImageFilter
import sqlalchemy as sa

# Gray at or lighter than this (0 black, 255 white) is light shading and becomes white: 25% gray or lighter.
LIGHT_LEVEL = 191
# Specks: a black pixel with no black neighbour and no gray around it darker than LIGHT_LEVEL.
_NEIGHBOURS = ImageFilter.Kernel((3, 3), [1, 1, 1, 1, 0, 1, 1, 1, 1], scale=1)

SCOPES = ('drawn', 'documents')
# 'screened': shaded areas kept with a pattern (nothing removed); 'whitened': light areas made white, specks removed.
METHODS = ('screened', 'whitened')


def _lut(predicate):
    return [255 if predicate(value) else 0 for value in range(256)]


_LIGHT = _lut(lambda value: value >= LIGHT_LEVEL)
_DARK = _lut(lambda value: value < LIGHT_LEVEL)
_PURE_BLACK = _lut(lambda value: value == 0)
_PURE_WHITE = _lut(lambda value: value == 255)


def fit(gray, size):
    """The gray page on the fax image's canvas: Ghostscript's fax devices make a page about 1728 dots wide
    exactly 1728 (cut or padded with white on the right); the gray device does not."""
    if gray.mode != 'L':
        gray = gray.convert('L')
    if gray.size == size:
        return gray
    canvas = Image.new('L', size, 255)
    canvas.paste(gray.crop((0, 0, min(gray.width, size[0]), min(gray.height, size[1]))), (0, 0))
    return canvas


def bits(image):
    """A mode "1" copy whose pixels are exactly 0 (black) or 255 (white), whatever values it was made with."""
    image = image if image.mode == '1' else image.convert('1', dither=Image.Dither.NONE)
    return Image.frombytes('1', image.size, image.tobytes())


def _count(mask):
    """Set pixels of a mode "1" mask."""
    return mask.histogram()[255]


def aligned(gray, halftone):
    """Whether the gray drawing and today's fax image are the same page, dot for dot: every pure black gray
    pixel is black and every pure white one is white in today's image."""
    if gray.size != halftone.size:
        return False
    white_today = bits(halftone)
    black_today = ImageChops.invert(white_today)
    wrong = ImageChops.logical_or(
        ImageChops.logical_and(gray.point(_PURE_BLACK, '1'), white_today),
        ImageChops.logical_and(gray.point(_PURE_WHITE, '1'), black_today))
    return _count(wrong) == 0


def lighten(gray, halftone, level=LIGHT_LEVEL):
    """Today's fax image with every pixel whose gray is ``level`` or lighter made white."""
    light = gray.point(_LIGHT if level == LIGHT_LEVEL else _lut(lambda value: value >= level), '1')
    return ImageChops.logical_or(bits(halftone), light)


def specks(bilevel, gray, level=LIGHT_LEVEL):
    """Mode "1" mask of specks: black pixels with no black neighbour whose eight neighbours are all lighter
    than ``level`` in the gray drawing. Pixels on the page's edge are never specks."""
    ink = ImageChops.invert(bits(bilevel)).convert('L')
    dark = gray.point(_DARK if level == LIGHT_LEVEL else _lut(lambda value: value < level))
    lonely = ink.filter(_NEIGHBOURS).point(_lut(lambda value: value == 0), '1')
    clear = dark.filter(_NEIGHBOURS).point(_lut(lambda value: value == 0), '1')
    found = ImageChops.logical_and(ImageChops.logical_and(ink.point(_lut(lambda value: value > 0), '1'), lonely),
                                   clear)
    # Kernel filters copy the outermost rows and columns unchanged: never call an edge pixel a speck.
    edge = Image.new('1', found.size, 0)
    if found.width > 2 and found.height > 2:
        edge.paste(255, (1, 1, found.width - 1, found.height - 1))
    return ImageChops.logical_and(found, edge)


def despeckle(bilevel, gray, level=LIGHT_LEVEL):
    """(page without specks, specks removed)."""
    found = specks(bilevel, gray, level)
    return ImageChops.logical_or(bits(bilevel), found), _count(found)


@dataclass(frozen=True)
class PageChange:
    page: Image.Image  # mode "1", the fax image's size and resolution
    changed: bool
    lightened: int  # black pixels made white because their gray is light
    specks: int


def friendly_page(gray, halftone, *, despeckle_page=True, level=LIGHT_LEVEL):
    """The whitened version of one page (light areas made white, and specks removed with ``despeckle_page``), or
    None when the two drawings do not line up (send it as it is). Only with the whitening opt-in.

    ``gray`` is the page drawn in gray (mode "L"), ``halftone`` today's fax image of it (mode "1")."""
    dpi = halftone.info.get('dpi')
    halftone = bits(halftone)
    gray = fit(gray, halftone.size)
    if not aligned(gray, halftone):
        return None
    light = lighten(gray, halftone, level)
    lightened = _count(ImageChops.logical_xor(light, halftone))
    removed = 0
    page = light
    if despeckle_page:
        page, removed = despeckle(light, gray, level)
    page = bits(page)
    page.info['dpi'] = dpi
    return PageChange(page, bool(lightened or removed), lightened, removed)


@dataclass
class Request:
    """Asks ``conversion.pdf_to_tiff`` (or ``lightened_pages``) for fax-friendly pages; ``result`` says what changed.

    ``scope``: 'documents' (your documents) or 'drawn' (pages Faxbot drew itself: never changed). ``method``:
    'screened' (the default: shaded areas kept with a pattern) or 'whitened' (light areas made white; only with
    the opt-in). ``despeckle``: also remove specks; only ever with whitening."""
    scope: str = 'documents'
    despeckle: bool | None = None
    method: str = 'screened'
    result: 'Result | None' = field(default=None)

    def __post_init__(self):
        if self.scope not in SCOPES:
            raise ValueError('Unsupported fax-friendly scope')
        if self.method not in METHODS:
            raise ValueError('Unsupported fax-friendly method')
        if self.despeckle is None or self.method != 'whitened':
            self.despeckle = self.method == 'whitened' and self.scope == 'documents'


@dataclass(frozen=True)
class Result:
    pages: int  # pages in the document
    pages_changed: int
    bits_before: int  # MMR (Group 4) bits of every page, as Faxbot sends them
    bits_after: int
    method: str = 'screened'
    losses: tuple = ()  # what the changed pages lost (``fidelity.LOSSES``); always empty for screened pages
    difference: float = 0.0  # how far the pages look from today's at reading distance (``fidelity``)

    @property
    def bits_saved(self):
        return max(0, self.bits_before - self.bits_after)


def seconds_saved(result):
    """Whole seconds the changed pages save on the line at 14,400 bit/s, the speed most calls reach."""
    from .decision import LINE_BITS_PER_SECOND
    return result.bits_saved // LINE_BITS_PER_SECOND


def fidelity_of(result):
    """The changed pages' ``fidelity.Fidelity`` as the layout chooser ranks them (kept, losses, difference)."""
    from .fidelity import Fidelity
    return Fidelity(not result.losses, tuple(result.losses), 0, result.difference)


# The hook: conversion.pdf_to_tiff calls apply() after Ghostscript made today's fax image -------------------------

def _render_gray(pdf_path, out_path, gs):
    from ..conversion import GHOSTSCRIPT_TIMEOUT_SECONDS
    subprocess.run(
        [gs, '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-dPDFSTOPONERROR', '-sDEVICE=tiffgray', '-sCompression=lzw',
         '-r204x196', f'-sOutputFile={out_path}', '-f', str(Path(pdf_path).resolve())],
        check=True, timeout=GHOSTSCRIPT_TIMEOUT_SECONDS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _gray_pages(gray_path, today):
    """The gray drawing's pages on today's canvases, or None when they are not the same pages."""
    pages = []
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(gray_path) as gray:
            for index, frame in enumerate(today):
                try:
                    gray.seek(index)
                except EOFError:
                    return None
                if gray.mode != 'L' or gray.height != frame.height:
                    return None
                pages.append(fit(gray.copy(), frame.size))
            try:
                gray.seek(len(today))
                return None
            except EOFError:
                pass
    return pages


def _screened(pdf_path, grays, today, gs, folder):
    """(pages, how many changed, Fidelity) with shaded areas screened, or None when a page does not line up.
    A page the fidelity measure would not keep goes as it is."""
    from . import fidelity, screens
    masks = screens.object_masks(pdf_path, grays, gs, folder)
    pages, changed, kept = [], 0, []
    for gray, frame, mask in zip(grays, today, masks):
        screened = screens.screen_page(gray, frame, objects=mask)
        if screened is None:
            return None
        found = fidelity.assess(gray, frame, screened.page, frozen=screened.frozen)
        if screened.changed and found.kept:
            pages.append(screened.page)
            changed += 1
            kept.append(found)
            continue
        if screened.changed:
            logging.getLogger(__name__).warning('A screened page would have lost %s; it goes as it is.',
                                                ', '.join(found.losses))
        pages.append(frame)
        kept.append(fidelity.UNCHANGED)
    return pages, changed, _overall(kept)


def _whitened(grays, today, despeckle_page):
    """(pages, how many changed, Fidelity) with light areas made white, or None when a page does not line up. The
    measure records what whitening lost; the administrator chose it with its warning."""
    from . import fidelity
    pages, changed, found = [], 0, []
    for gray, frame in zip(grays, today):
        change = friendly_page(gray, frame, despeckle_page=despeckle_page)
        if change is None:
            return None
        pages.append(change.page if change.changed else frame)
        changed += int(change.changed)
        found.append(fidelity.assess(gray, frame, change.page) if change.changed else fidelity.UNCHANGED)
    return pages, changed, _overall(found)


def _overall(pages):
    from . import fidelity
    losses = {name for page in pages for name in page.losses}
    ordered = tuple(name for name in fidelity.LOSSES if name in losses)
    difference = sum(page.difference for page in pages) / len(pages) if pages else 0.0
    return fidelity.Fidelity(not ordered, ordered, sum(page.changed for page in pages), round(difference, 6))


# Ghostscript, the image files and the drawing checks refuse in these ways; anything else is a bug and is raised.
_REFUSED = (subprocess.SubprocessError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning)


def _refusals():
    from ..conversion import DocumentConversionError
    from .screens import MasksUnavailable
    return _REFUSED + (DocumentConversionError, MasksUnavailable)


def apply(pdf_path, tiff_path, request, *, gs):
    """Make the fax image Ghostscript just wrote at ``tiff_path`` (from ``pdf_path``, at 204 x 196) fax-friendly
    with ``request.method``, in place, and set ``request.result``; returns the Result or None. When Ghostscript or
    the image refuses, or no page changes, or the pages would not be smaller, the image stays exactly as it was."""
    request.result = None
    try:
        result = _apply(pdf_path, tiff_path, request, gs)
    except _refusals():
        logging.getLogger(__name__).warning('Fax-friendly pages could not be made; the pages go as they are.',
                                            exc_info=True)
        return None
    request.result = result
    return result


def _apply(pdf_path, tiff_path, request, gs):
    from .. import conversion
    folder = Path(tiff_path).parent
    today = conversion.read_fax_frames(str(tiff_path))
    if not today:
        return None
    if request.scope == 'drawn':
        return Result(len(today), 0, 0, 0, request.method)  # pages Faxbot drew itself are never changed
    handle, gray_path = tempfile.mkstemp(prefix='.faxbot-friendly-', suffix='.tiff', dir=folder)
    os.close(handle)
    handle, out_path = tempfile.mkstemp(prefix='.faxbot-friendly-', suffix='.tiff', dir=folder)
    os.close(handle)
    try:
        _render_gray(pdf_path, gray_path, gs)
        grays = _gray_pages(gray_path, today)
        found = None
        if grays is not None:
            found = (_screened(pdf_path, grays, today, gs, folder) if request.method == 'screened'
                     else _whitened(grays, today, request.despeckle))
        if found is None:
            # Said once per document, without its details, so a live check can see that it went unchanged.
            logging.getLogger(__name__).warning(
                'Fax-friendly pages were not made: the gray drawing did not line up with the fax image.')
            return None
        pages, changed, assessment = found
        if not changed:
            return Result(len(today), 0, 0, 0, request.method)
        before, after = sum(conversion.frame_bits(today)), sum(conversion.frame_bits(pages))
        if after >= before:
            return Result(len(today), 0, before, before, request.method)
        conversion.write_fax_tiff(pages, out_path)
        os.replace(out_path, tiff_path)
        return Result(len(today), changed, before, after, request.method, assessment.losses, assessment.difference)
    finally:
        for path in (gray_path, out_path):
            Path(path).unlink(missing_ok=True)


def lighten_image(pdf_path, tiff_path, out_path, request):
    """A fax-friendly copy of a fax image Faxbot made from ``pdf_path`` (``tiff_path``) at ``out_path``, for one
    send; sets ``request.result``. The fax's own image is never changed; on any problem the copy is unchanged."""
    import shutil
    shutil.copyfile(tiff_path, out_path)
    gs = shutil.which('gs')
    if gs is None:
        request.result = None
        return None
    return apply(pdf_path, out_path, request, gs=gs)


# The changed pages of a fax, made once for each method and kept with the attempt files ---------------------------

CACHE = 'packed-friendly-'  # sending.PREFIX ('packed-'): the retention cleanup removes it with the attempt files
_HEX32 = re.compile(r'[a-f0-9]{32}')


def _cache_paths(root, job_id, method):
    # Named by method: an earlier build's ``packed-friendly-<fax>.tiff`` holds whitened pages and is never read.
    stem = Path(root) / f'{CACHE}{job_id}-{method}'
    return stem.with_suffix('.tiff'), stem.with_suffix('.json')


def _cached(image, facts, method):
    """(Result, image or None) from the cache, or None when there is nothing usable."""
    try:
        known = json.loads(facts.read_text())
        result = Result(**{name: int(known[name]) for name in ('pages', 'pages_changed', 'bits_before',
                                                                'bits_after')},
                        method=str(known['method']), losses=tuple(str(name) for name in known.get('losses', ())),
                        difference=float(known.get('difference', 0.0)))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if result.method != method:
        return None
    if not result.pages_changed:
        return result, None
    if image.is_file() and not image.is_symlink():
        return result, image
    return None


def lightened_pages(root, job_id, pdf_path, tiff_path, request):
    """The fax's changed pages for one attempt, made with ``request.method``: the path of the fax image to send, or
    None when no page changes or they could not be made; ``request.result`` says which. Made once for each document
    and method (Faxbot's own image when there is one, else its PDF drawn again) and kept as
    ``packed-friendly-<fax>-<method>`` beside the attempt files, so a retry draws nothing again. The fax's own files
    are never changed."""
    from .. import conversion
    request.result = None
    if not _HEX32.fullmatch(str(job_id)):
        return None
    image, facts = _cache_paths(root, job_id, request.method)
    found = _cached(image, facts, request.method)
    if found is not None:
        request.result, path = found
        return path
    handle, temporary = tempfile.mkstemp(prefix='.faxbot-friendly-', suffix='.tiff', dir=str(root))
    os.close(handle)
    try:
        try:
            if tiff_path is not None and Path(tiff_path).is_file():
                lighten_image(str(pdf_path), str(tiff_path), temporary, request)
            else:
                conversion.pdf_to_tiff(str(pdf_path), temporary, friendly=request)
        except (conversion.DocumentConversionError, OSError):
            logging.getLogger(__name__).warning('Fax-friendly pages could not be made; the pages go as they are.',
                                                exc_info=True)
            request.result = None
            return None
        result = request.result
        if result is None:
            return None
        if result.pages_changed:
            os.replace(temporary, image)
        written = Path(temporary).with_suffix('.json')
        written.write_text(json.dumps(asdict(result)))
        os.replace(written, facts)
        return image if result.pages_changed else None
    finally:
        for path in (Path(temporary), Path(temporary).with_suffix('.json')):
            path.unlink(missing_ok=True)


def call_lightener(engine, values, route, number, root, claim):
    """For a shared call (batching/image.py): ``lighten(job_id)``, each fax's screened image (or None), when this
    call's route and recipient say so; else None. A shared call carries several faxes and is not priced fax by
    fax, so it gets the screen (nothing removed), never whitening. Each fax screened is recorded for its attempt."""
    lighten, _ = should_lighten(engine, values, route, number)
    if not lighten:
        return None
    attempts = {member.job_id: member.attempt_id for member in getattr(claim, 'everyone', ())}

    def lighten_member(job_id):
        folder = Path(root)
        request = Request('documents')
        path = lightened_pages(folder, job_id, folder / f'{job_id}.pdf', folder / f'{job_id}.tiff', request)
        if path is not None and job_id in attempts:
            try:
                record_send(engine, job_id=job_id, attempt_id=attempts[job_id], request=request)
            except sa.exc.SQLAlchemyError:
                logging.getLogger(__name__).warning('Fax-friendly pages could not be recorded for this fax.')
        return path
    return lighten_member


# When: the setting for your documents, the whitening opt-in, each recipient's choice, and each attempt's route ----

CHOICES = ('where_it_saves', 'always', 'never')
RECIPIENT_CHOICES = ('always', 'never')
_LEGACY = {'true': 'always', 'on': 'always', 'yes': 'always', '1': 'always',
           'false': 'never', 'off': 'never', 'no': 'never', '0': 'never'}


def documents_choice(values):
    """'where_it_saves' (the default), 'always' or 'never'; the earlier on and off read as always and never."""
    value = getattr(values, 'fax_friendly_documents', 'where_it_saves')
    if isinstance(value, bool):
        return 'always' if value else 'never'
    value = _LEGACY.get(str(value).strip().lower(), str(value).strip().lower())
    return value if value in CHOICES else 'where_it_saves'


def whiten_allowed(values):
    """Whether the administrator opted in to making light areas white (``fax_friendly_whiten``; off by default).
    It never applies while the setting for your documents is Never."""
    return getattr(values, 'fax_friendly_whiten', False) is True and documents_choice(values) != 'never'


PHONE_ROUTES = frozenset({'sip', 'freeswitch'})


def billed_by_time(card, route=None):
    """Whether a route bills a call by its time (per minute or per second).

    A phone line (``sip``, ``freeswitch``, and phone systems through them) bills by the minute or second whether
    or not Faxbot knows its price: it does unless its rate card has a per-page rate. A cloud provider follows its
    card: a per-minute price is billed by time; a per-page price, a flat plan or no card at all is not."""
    if route in PHONE_ROUTES:
        return not (card is not None and getattr(card, 'per_page_micros', 0))
    if card is None or getattr(card, 'flat_plan', None):
        return False
    return bool(getattr(card, 'per_minute_micros', 0))


def should_lighten(engine, values, route, number, *, card=..., ecm=...):
    """``decide`` for one attempt by ``route`` to ``number``: (make candidates, why). A rate card or machine
    record that cannot be read counts as unknown (logged), never as a reason."""
    if card is ...:
        from ..routing.store import RouteStore
        try:
            card = RouteStore(engine).card_for(route)
        except (sa.exc.SQLAlchemyError, ValueError):  # ValueError: costs.InvalidRateCard, a stored card it refuses
            logging.getLogger(__name__).warning('The route rate card could not be read for fax-friendly pages.')
            card = None
    if ecm is ...:
        from .capability import records_for
        try:
            ecm = records_for(engine).capability(number).ecm
        except sa.exc.SQLAlchemyError:
            logging.getLogger(__name__).warning('The fax machine record could not be read for fax-friendly pages.')
            ecm = None
    return decide(documents_choice(values), recipient_choice(engine, number), by_time=billed_by_time(card, route),
                  ecm=ecm)


def decide(choice, recipient, *, by_time, ecm):
    """Whether one attempt's changed pages are made and priced at all, and why: (True, 'recipient' | 'always' |
    'time' | 'ecm') or (False, None). A recipient's never always wins and its always beats the setting; "where it
    saves time" makes them for a call billed by time or a machine without error correction (``ecm`` False), and
    the layout chooser keeps them only at a lower expected bill or for a named reason."""
    if recipient == 'never':
        return False, None
    if recipient == 'always':
        return True, 'recipient'
    if choice == 'never':
        return False, None
    if choice == 'always':
        return True, 'always'
    if by_time:
        return True, 'time'
    if ecm is False:
        return True, 'ecm'
    return False, None


# Named reasons for a faster page at the same expected bill (decision.choose): nothing else trades fidelity for time.
REASONS = ('administrator', 'deadline', 'capacity')


def named_reason(engine, values, job, route, why, *, now=None):
    """The reason a faster candidate may win at the same expected bill, or None.

    'administrator': the setting for your documents, or this recipient, is Always (``why``). 'deadline': the fax
    has a send-by time within ``schedule.DEADLINE_FIRST`` (the hour in which the scheduler sends it first).
    'capacity': it goes over your phone line while other faxes wait for a line (``capacity.waiting_for_line``)."""
    if why in ('always', 'recipient'):
        return 'administrator'
    now = now or utcnow()
    from ..routing.schedule import DEADLINE_FIRST
    send_by = (job or {}).get('send_by')
    if isinstance(send_by, datetime) and send_by.replace(tzinfo=None) - now <= DEADLINE_FIRST:
        return 'deadline'
    if route in PHONE_ROUTES and engine is not None:
        from ..capacity import for_engine
        try:
            if for_engine(engine).waiting_for_line(values, now) > 0:
                return 'capacity'
        except sa.exc.SQLAlchemyError:
            logging.getLogger(__name__).warning('Faxes waiting for a line could not be counted.')
    return None


# The record (migrations 0042 and 0058): one row for each attempt whose pages changed, and recipients' choices ------

TABLE = 'fax_friendly_pages'
RECIPIENTS = 'fax_friendly_recipients'
_ID = re.compile(r'[A-Za-z0-9_-]{1,40}', re.ASCII)
_NUMBER = re.compile(r'\+?[0-9]{3,20}', re.ASCII)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(job_id, attempt_id, scope, result):
    if not _ID.fullmatch(str(job_id or '')) or (attempt_id is not None and not _ID.fullmatch(str(attempt_id))):
        raise ValueError('Unsupported fax-friendly record')
    if scope not in SCOPES or not isinstance(result, Result) or not 1 <= result.pages_changed <= result.pages:
        raise ValueError('Unsupported fax-friendly record')
    if not (0 <= result.bits_before < 2 ** 31 and 0 <= result.bits_after < 2 ** 31):
        raise ValueError('Unsupported fax-friendly record')
    if result.method not in METHODS:
        raise ValueError('Unsupported fax-friendly record')
    return {'id': uuid4().hex, 'job_id': job_id, 'attempt_id': attempt_id, 'scope': scope, 'pages': result.pages,
            'pages_changed': result.pages_changed, 'bits_before': result.bits_before,
            'bits_after': result.bits_after, 'seconds_saved': seconds_saved(result), 'method': result.method}


def _table(connection, name=TABLE):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def record_send(engine, *, job_id, attempt_id, request, now=None):
    """Record the changed pages one attempt sent, and how; once per attempt. Returns the row's ID, or None."""
    result = getattr(request, 'result', None)
    if engine is None or result is None or result.pages_changed < 1:
        return None
    row = _row(job_id, attempt_id, request.scope, result)
    with engine.begin() as connection:
        table = _table(connection)
        found = connection.execute(sa.select(table.c.id).where(
            table.c.job_id == job_id, table.c.attempt_id == attempt_id)).scalar()
        if found is not None:
            return found
        if 'method' not in table.c:
            raise ValueError('The fax-friendly pages table is older than migration 0058.')
        connection.execute(table.insert().values(**row, created_at=now or utcnow()))
    return row['id']


def _newest_attempt(connection, job_id):
    try:
        attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=connection)
    except sa.exc.NoSuchTableError:
        return None
    return connection.execute(sa.select(attempts.c.id).where(attempts.c.job_id == job_id).order_by(
        attempts.c.sequence.desc()).limit(1)).scalar()


def run_for(engine, job_id):
    """What the fax's newest attempt sent: its row when that attempt's pages changed, else None (the Sent detail
    follows the attempt). Without attempt records, the newest row."""
    if engine is None or not _ID.fullmatch(str(job_id or '')):
        return None
    try:
        with engine.connect() as connection:
            table = _table(connection)
            query = sa.select(table).where(table.c.job_id == job_id)
            newest = _newest_attempt(connection, job_id)
            if newest is not None:
                query = query.where(table.c.attempt_id == newest)
            row = connection.execute(query.order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)
                                     ).mappings().first()
    except sa.exc.SQLAlchemyError:
        return None
    return dict(row) if row is not None else None


def recipient_choice(engine, number):
    """This recipient's own choice, 'always' or 'never', or None (as set for all faxes). Never raises."""
    if engine is None or not _NUMBER.fullmatch(str(number or '')):
        return None
    try:
        with engine.connect() as connection:
            table = _table(connection, RECIPIENTS)
            value = connection.execute(sa.select(table.c.shading).where(table.c.number == number)).scalar()
    except sa.exc.SQLAlchemyError:
        return None
    return value if value in RECIPIENT_CHOICES else None


def set_recipient_choice(engine, number, shading, *, actor=None, now=None):
    """Set this recipient's choice: 'always', 'never', or None to follow the setting for all faxes."""
    if not _NUMBER.fullmatch(str(number or '')):
        raise ValueError('Enter the fax number with its country code.')
    if shading is not None and shading not in RECIPIENT_CHOICES:
        raise ValueError("Choose 'As set for all faxes', 'Always' or 'Never'.")
    with engine.begin() as connection:
        table = _table(connection, RECIPIENTS)
        connection.execute(table.delete().where(table.c.number == number))
        if shading is not None:
            connection.execute(table.insert().values(id=uuid4().hex, number=number, shading=shading,
                                                     updated_at=now or utcnow(),
                                                     updated_by=str(actor or '')[:100] or None))
    return recipient_choice(engine, number)


# What people read: the one source of the setting's words (console, command line, guided setup) ------------------

SETTING_LABEL = 'Fax-friendly shading on documents you send'
CHOICE_LABELS = {'where_it_saves': 'Where it saves time', 'always': 'Always', 'never': 'Never'}
CHOICE_SENTENCES = {
    'where_it_saves': ('Faxbot keeps shaded areas with a fax-friendly pattern when that makes the call cost less, or '
                       'when a fax must go by its send-by time or others are waiting for your phone line. Text and '
                       'marks stay exactly as they are.'),
    'always': ('Faxbot keeps shaded areas with a fax-friendly pattern on every document whose pages it makes, '
               'whenever that shortens the call. Text and marks stay exactly as they are.'),
    'never': 'Faxbot sends the pages of your documents as they are.',
}
SETTING_SENTENCE = (
    'Shaded table rows, tinted form fields and highlights take most of a page\'s time on the line. Faxbot draws '
    'their inside with fine stripes that the fax line sends quickly and leaves every letter, line and mark exactly '
    'as it was: in Faxbot\'s tests a page with a shaded table went from 61 to 27 seconds, and pale text stayed as '
    'it was.')
WHITEN_LABEL = 'Also make light areas white'
WHITEN_WARNING = 'This may erase pale text and light marks, such as faint handwriting, and removes specks.'
WHITEN_SENTENCE = (
    'Light shading then prints white where that makes the call cost less; in Faxbot\'s tests a shaded table went '
    'from 61 to 12 seconds. ' + WHITEN_WARNING)
RECIPIENT_LABEL = 'Fax-friendly shading for this recipient'
WHERE = 'under Providers, In use, Delivery routes'


def describe_setting():
    """The setting as other screens describe it (guided setup reads its words here; keep this the one source).

    ``default`` is the choice a new installation has, ``off`` the one that changes no page, and ``choices`` maps
    each choice to its label (as Providers → In use shows it) and one sentence saying what it does. ``whiten`` is
    the separate opt-in to make light areas white, off by default, with its warning.
    """
    return {'setting': 'fax_friendly_documents', 'label': SETTING_LABEL, 'default': 'where_it_saves', 'off': 'never',
            'choices': {value: (CHOICE_LABELS[value], CHOICE_SENTENCES[value]) for value in CHOICES},
            'whiten': {'setting': 'fax_friendly_whiten', 'label': WHITEN_LABEL, 'default': False,
                       'warning': WHITEN_WARNING}}


def recipient_view(engine, number, values):
    """Recipients, Details: this recipient's choice and the setting for all faxes."""
    return {'shading': recipient_choice(engine, number), 'shading_default': documents_choice(values)}


def duration(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return '1 second' if seconds == 1 else f'{seconds} seconds'
    minutes = round(seconds / 60)
    return f'{minutes} minutes'


def _pages_text(changed, pages):
    if pages == 1:
        return 'the page'
    if changed == pages:
        return f'all {pages} pages'
    return f'{changed} of the {pages} pages'


RATES = range(2400, 33601)


def call_rate(engine, job_id):
    """The speed the fax's newest trunk call negotiated, in bit/s, when its engine reported one (the call's first
    speed; the built-in engine reports only its last page's); None for cloud providers and unreported calls."""
    try:
        from ..hylafax_records import records_for
        negotiation = (records_for(engine).sent_detail(job_id) or {}).get('negotiation') or {}
    except Exception:
        return None
    for name in ('rate_first', 'rate_last_page'):
        value = negotiation.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value in RATES:
            return value
    return None


def seconds_at(run, rate=None):
    """The estimated seconds a row saved: at the call's own speed when known, else as stored (14,400 bit/s)."""
    if rate in RATES and run.get('bits_before') is not None and run.get('bits_after') is not None:
        return max(0, run['bits_before'] - run['bits_after']) // rate
    return run.get('seconds_saved')


def method_of(run):
    """'screened' or 'whitened'; a row from before migration 0058 (no method) was whitened."""
    return 'screened' if (run or {}).get('method') == 'screened' else 'whitened'


def sent_sentence(run, rate=None):
    """The Sent detail's sentence for one row of fax_friendly_pages, or None. ``rate``: the speed the call went
    at (``call_rate``); without it the estimate is at full fax speed (14,400 bit/s)."""
    if not run:
        return None
    pages = _pages_text(run['pages_changed'], run['pages'])
    if run['scope'] != 'documents':
        head = f'Shaded areas on {pages} Faxbot drew were lightened before sending'
    elif method_of(run) == 'screened':
        head = f'Shaded areas on {pages} were kept with a fax-friendly pattern'
    else:
        head = f'Light areas on {pages} were made white and specks removed before sending'
    known = rate in RATES and run.get('bits_before') is not None
    seconds = seconds_at(run, rate if known else None)
    if not seconds:
        return head + '.'
    speed = f"at this call's speed of {rate:,} bit/s" if known else 'at full fax speed'
    return f'{head}: an estimated {duration(seconds)} less on the line {speed}.'


# The recommendation (Costs, Recommendations): only while the setting is Never -----------------------------------

DAYS = 30
FAXES = 10  # recent faxes measured at most
PAGE_BUDGET = 30  # pages drawn again at most, for each look at the recommendation
MIN_SECONDS = 10  # recommend only when the recent faxes would have saved at least this long together
_MEASURED = OrderedDict()  # job ID -> Result (or None: could not be measured); the documents never change
_LOCK = threading.Lock()


def measure_document(pdf_path):
    """What the fax-friendly pattern would do to a fax's PDF, drawn as Faxbot draws it today: a Result, or None."""
    from .. import conversion
    request = Request('documents')
    with tempfile.TemporaryDirectory(prefix='.faxbot-friendly-check-', dir=str(Path(pdf_path).parent)) as folder:
        conversion.pdf_to_tiff(str(pdf_path), str(Path(folder) / 'check.tiff'), friendly=request)
    return request.result


def _measured(job_id, pdf_path, measure):
    with _LOCK:
        if job_id in _MEASURED:
            _MEASURED.move_to_end(job_id)
            return _MEASURED[job_id]
    try:
        result = measure(pdf_path)
    except Exception:
        result = None
    with _LOCK:
        _MEASURED[job_id] = result
        while len(_MEASURED) > 512:
            _MEASURED.popitem(last=False)
    return result


def _recent_faxes(engine, since):
    with engine.connect() as connection:
        jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=connection)
        backend = jobs.c.backend if 'backend' in jobs.c else sa.null()
        return connection.execute(sa.select(jobs.c.id, jobs.c.pages, backend, jobs.c.to_number).where(
            jobs.c.created_at >= since).order_by(jobs.c.created_at.desc(), jobs.c.id.desc()).limit(FAXES * 5)).all()


def where_it_saves(engine):
    """``saves(route, number)``: whether "Where it saves time" would make fax-friendly pages for a fax by that route
    to that number (a rate card that bills by time, or a machine without error correction). Answers are kept per
    call."""
    from ..routing.store import RouteStore
    from .capability import records_for
    cards, machines = {}, {}

    def saves(route, number):
        if route not in cards:
            try:
                cards[route] = billed_by_time(RouteStore(engine).card_for(route), route) if route else False
            except Exception:
                cards[route] = False
        if cards[route]:
            return True
        if number not in machines:
            try:
                machines[number] = records_for(engine).capability(number).ecm
            except Exception:
                machines[number] = None
        return machines[number] is False
    return saves


def recommendation(engine, data_dir, *, choice, now=None, measure=None, saves=None):
    """Costs, Recommendations: with the setting at Never, whether "Where it saves time" would have saved time on
    your recent faxes (at most FAXES faxes and PAGE_BUDGET pages, each drawn again once). Faxes that went by a
    provider charging per page save nothing and are not counted. With any other choice there is nothing to say."""
    now = now or utcnow()
    view = {'choice': choice, 'label': SETTING_LABEL, 'measured_sentence': SETTING_SENTENCE, 'days': DAYS,
            'recommend': False, 'faxes_checked': 0, 'faxes_changed': 0, 'seconds_saved': 0, 'sentence': None,
            'action': None}
    if choice != 'never':
        return view
    measure = measure or measure_document
    saves = saves or where_it_saves(engine)
    budget, checked, shaded, changed, seconds = PAGE_BUDGET, 0, 0, 0, 0
    for job_id, pages, route, number in _recent_faxes(engine, now - timedelta(days=DAYS)):
        if checked >= FAXES:
            break
        pdf = Path(data_dir) / f'{job_id}.pdf'
        if not re.fullmatch(r'[a-f0-9]{32}', str(job_id)) or pdf.is_symlink() or not pdf.is_file():
            continue
        if not isinstance(pages, int) or pages < 1 or pages > budget:
            continue
        result = _measured(job_id, pdf, measure)
        if result is None:
            continue
        budget -= pages
        checked += 1
        if result.pages_changed:
            shaded += 1
            if saves(route, number):
                changed += 1
                seconds += seconds_saved(result)
    view.update(faxes_checked=checked, faxes_changed=changed, seconds_saved=seconds)
    if not checked:
        return view
    one = checked == 1
    faxes = 'Your last fax' if one else f'Your last {checked} faxes'
    if seconds >= MIN_SECONDS:
        view['recommend'] = True
        which = ('it has shaded areas' if one else
                 f'{changed} of them {"has" if changed == 1 else "have"} shaded areas')
        view['sentence'] = (f'{faxes} would have taken an estimated {duration(seconds)} less on the line with shaded '
                            f'areas kept in a fax-friendly pattern on calls billed by time; {which}.')
        view['action'] = (f'Choose "{CHOICE_LABELS["where_it_saves"]}" for "{SETTING_LABEL}" {WHERE}. Text and marks '
                          'stay exactly as they are; only the inside of shaded areas is drawn differently.')
    elif shaded:
        view['sentence'] = (f'{faxes} went by providers that charge per page, to machines with error correction, so '
                            'the fax-friendly pattern would have saved nothing.' if not one else
                            'Your last fax went by a provider that charges per page, to a machine with error '
                            'correction, so the fax-friendly pattern would have saved nothing.')
    else:
        view['sentence'] = (f'{faxes} {"has" if one else "have"} no shaded areas that slow '
                            f'{"it" if one else "them"} down.')
    return view
