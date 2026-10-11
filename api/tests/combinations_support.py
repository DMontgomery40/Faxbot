"""Shared fixtures and read-backs for the combination and fault-injection suite (brief 104). Not a test module.

Every combination case does three things:

1. it records what actually applied, read back from the fax's own records (``applied``), never assumed from what
   the case asked for;
2. it asserts the invariant of its group;
3. when the product refuses the combination, it is recorded as impossible, and the case checks that the refusal (or,
   where the product refuses silently, the records) names the path the fax took instead.

Each case also calls ``record`` with its row for the report table. Nothing is written unless
``FAXBOT_COMBINATIONS_TABLE`` names a file, so CI writes nothing.

Every document, number, account and price here is synthetic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
import sqlalchemy as sa
from PIL import ImageChops

from api.tests.test_schema import database  # noqa: F401 - fixture, re-exported for the group files

NOW = datetime(2026, 10, 10, 9, 0, 0)
JOB, ATTEMPT = 'c0' * 16, 'a7' * 16
PEER = '+15555550199'  # synthetic US number; nothing is dialed
FINE_DPI = (204.0, 196.0)
needs_gs = pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript draws the PDF pages, as acceptance does')


@pytest.fixture
def installation(database):  # noqa: F811 - the re-exported fixture above
    """A new installation's schema on SQLite and PostgreSQL; the engine itself."""
    from api.app.schema import upgrade_schema
    upgrade_schema(database)
    return database


# The report table ------------------------------------------------------------------------------------------------

OUTCOMES = ('pass', 'xfail', 'impossible')


@dataclass(frozen=True)
class Case:
    """One row of the report: group x case -> applied mechanisms -> invariant -> outcome."""
    group: str
    case: str
    requested: tuple
    applied: tuple
    invariant: str
    outcome: str = 'pass'
    alternate: str | None = None  # an impossible combination: the path the fax took instead
    defect: str | None = None

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError('outcome')
        if self.outcome == 'impossible' and not self.alternate:
            raise ValueError('An impossible combination names the path the fax took instead.')


def record(case):
    """Append ``case`` to the report table when ``FAXBOT_COMBINATIONS_TABLE`` names a file; else nothing."""
    target = os.environ.get('FAXBOT_COMBINATIONS_TABLE')
    if not target:
        return case
    with open(target, 'a', encoding='utf-8') as out:
        out.write(json.dumps(asdict(case), sort_keys=True, default=str) + '\n')
    return case


# What applied, read back from the fax's own records --------------------------------------------------------------

def _rows(engine, table, **where):
    try:
        found = sa.Table(table, sa.MetaData(), autoload_with=engine)
    except sa.exc.NoSuchTableError:
        return []
    query = sa.select(found)
    for column, value in where.items():
        query = query.where(found.c[column] == value)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


@dataclass
class Applied:
    """The page mechanisms one attempt's records say applied: ``mechanisms`` and the rows they were read from."""
    change: dict | None
    friendly: dict | None
    codec: dict | None
    coding: dict | None
    mechanisms: frozenset = field(default_factory=frozenset)


def applied(engine, job_id, attempt_id):
    """What the records of ``attempt_id`` say was done to its pages.

    Mechanisms: 'dense' (packed onto long pages), 'codec' (encoded pages), 'trim' (blank page bottoms left out),
    'standard' (kept at standard resolution), 'screened' or 'whitened' (fax-friendly shading), and 'too_long' (the
    fax was too long to change). Read from ``fax_page_changes``, ``fax_friendly_pages``, ``codec_sends`` and
    ``fax_coding_choices``."""
    change = next(iter(_rows(engine, 'fax_page_changes', attempt_id=attempt_id)), None)
    friendly = next(iter(_rows(engine, 'fax_friendly_pages', attempt_id=attempt_id)), None)
    codec = next(iter(_rows(engine, 'codec_sends', id=job_id)), None)
    coding = next(iter(_rows(engine, 'fax_coding_choices', attempt_id=attempt_id)), None)
    found = set()
    if change is not None:
        if change.get('layout') in ('dense', 'codec'):
            found.add(change['layout'])
        if change.get('trimmed_pages'):
            found.add('trim')
        if change.get('resolution') == 'standard':
            found.add('standard')
        if change.get('layout') is None and not change.get('trimmed_pages') and not change.get('resolution') and (
                change.get('original_pages') == change.get('sent_pages')):
            found.add('too_long')
    if friendly is not None:
        found.add(friendly.get('method') or 'whitened')
    return Applied(change, friendly, codec, coding, frozenset(found))


# Files and pixels ------------------------------------------------------------------------------------------------

def digests(*paths):
    """{name: SHA-256} of each file that exists."""
    return {Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in paths if Path(path).is_file()}


def row_bytes(frame):
    """Each row of a mode "1" image as bytes."""
    data, stride = frame.tobytes(), (frame.width + 7) // 8
    return [data[row * stride:(row + 1) * stride] for row in range(frame.height)]


def white_rows(frame, top, bottom):
    """Whether rows ``top``..``bottom`` of a mode "1" image are all white."""
    if bottom <= top:
        return True
    return frame.crop((0, top, frame.width, bottom)).convert('L').getextrema() == (255, 255)


def ink(frame, box=None):
    """Black pixels of a mode "1" image, in ``box`` when given."""
    part = frame.crop(box) if box else frame
    return part.convert('L').histogram()[0]


def difference_box(first, second):
    """The bounding box of the pixels that differ between two mode "1" images of the same size, or None."""
    if first.size != second.size:
        raise ValueError('Compare images of the same size')
    return ImageChops.logical_xor(first.convert('1'), second.convert('1')).getbbox()


def inside(box, region, margin=0):
    """Whether ``box`` (left, top, right, bottom) lies inside ``region`` grown by ``margin`` pixels."""
    if box is None:
        return True
    left, top, right, bottom = region
    return (box[0] >= left - margin and box[1] >= top - margin and box[2] <= right + margin
            and box[3] <= bottom + margin)


# Synthetic documents ---------------------------------------------------------------------------------------------

PAGE_POINTS = (612.0, 792.0)  # US Letter
SHADE_BOX_POINTS = (72.0, 430.0, 540.0, 590.0)  # left, bottom, right, top in PDF points
SHADE_GRAY = 0.82  # light gray: a halftone today, a pattern when screened, white when whitened


@dataclass(frozen=True)
class Statement:
    """A synthetic statement drawn from text and shapes only (so its blank bottoms may be left out): each page has
    a page title, text lines, and, when ``shaded``, one light-gray box whose region in fax pixels is ``shade``."""
    pdf: Path
    pages: int
    shaded: bool
    shade: tuple | None  # (left, top, right, bottom) in fax pixels at 204 x 196 dots per inch
    blank_from: int  # the first fax row below which every page is blank


def to_pixels(box_points, dpi=FINE_DPI):
    left, bottom, right, top = box_points
    x, y = dpi[0] / 72.0, dpi[1] / 72.0
    return (int(left * x), int((PAGE_POINTS[1] - top) * y), int(right * x) + 1, int((PAGE_POINTS[1] - bottom) * y) + 1)


FULL_PAGE_POINTS = (18.0, 18.0, 594.0, 774.0)  # a shaded background over the whole page: a slow page to fax


def statement_pdf(path, *, pages=5, shaded=True, label='Synthetic statement', box=SHADE_BOX_POINTS):
    """Draw the statement (reportlab, text and shapes only). The lower part of every page is blank unless ``box``
    is ``FULL_PAGE_POINTS``, which shades the whole page (each page then takes over a minute to fax)."""
    from reportlab.pdfgen import canvas
    page = canvas.Canvas(str(path), pagesize=PAGE_POINTS)
    page.setTitle(label)
    for number in range(1, pages + 1):
        if shaded and box == FULL_PAGE_POINTS:
            left, bottom, right, top = box
            page.setFillGray(SHADE_GRAY)
            page.rect(left, bottom, right - left, top - bottom, stroke=0, fill=1)
            page.setFillGray(0)
        page.setFont('Helvetica-Bold', 18)
        page.drawString(72, 740, f'{label} - page {number} of {pages}')
        page.setFont('Helvetica', 11)
        for line in range(8):
            page.drawString(72, 710 - line * 14, f'Line {line + 1}: synthetic account 00{number}{line} balance '
                                                 f'{(number * 37 + line * 11) % 900}.{line}0')
        if shaded and box != FULL_PAGE_POINTS:
            left, bottom, right, top = box
            page.setFillGray(SHADE_GRAY)
            page.rect(left, bottom, right - left, top - bottom, stroke=0, fill=1)
            page.setFillGray(0)
        page.setFont('Helvetica', 9)
        page.drawString(72, 400, f'Synthetic footer {number}: nothing below this line.')
        page.showPage()
    page.save()
    blank_from = int((PAGE_POINTS[1] - 380) * FINE_DPI[1] / 72.0)
    if box == FULL_PAGE_POINTS:
        blank_from = int(PAGE_POINTS[1] * FINE_DPI[1] / 72.0)
    return Statement(Path(path), pages, shaded, to_pixels(box) if shaded else None, blank_from)


def place(document, folder, job_id=JOB, *, image=True):
    """The fax's own files as acceptance leaves them: ``<job>.pdf`` and (for Faxbot's own engines) ``<job>.tiff``,
    the TIFF drawn by Ghostscript exactly as acceptance draws it. Returns (pdf, tiff or None)."""
    from api.app import conversion
    folder = Path(folder)
    pdf = folder / f'{job_id}.pdf'
    shutil.copyfile(document.pdf, pdf)
    tiff = None
    if image:
        tiff = folder / f'{job_id}.tiff'
        conversion.pdf_to_tiff(str(pdf), str(tiff))
    return pdf, tiff
