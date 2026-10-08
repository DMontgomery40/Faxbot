#!/usr/bin/env python3
"""Fax-friendly pages benchmark (research inventory X12): what rendering choices cost on the line.

For a corpus of synthetic and public-domain pages (text, forms, shaded tables,
a logo, scans with noise, a photograph, and the pages Faxbot draws itself with
its real renderers), every page is drawn by Ghostscript the way Faxbot draws
it today (``tiffg4``, its halftone for gray) and in gray (``tiffgray``), at
fine (204 x 196) and standard (204 x 98) resolution. Each variant is coded
with libtiff as MH (T.4 one-dimensional), MR (T.4 two-dimensional, K=4 at
fine and 2 at standard) and MMR (T.6), one strip per page, and its bits are
turned into seconds at 9,600 and 14,400 bit/s. Without error correction a
receiving machine may also ask for a minimum time per scan line; the MH
seconds at 10 and 20 ms per line are worked out from each line's own bytes.

Variants: today's halftone; error diffusion (Floyd-Steinberg) and a plain 50%
threshold of the gray page; light shading made white at several gray levels
(``pages/friendly.lighten``); specks removed (``pages/friendly.despeckle``);
and the fax-friendly page Faxbot sends (``pages/friendly.friendly_page``).

Usage (from the repository root, with Faxbot's virtual environment):
    python scripts/fax_friendly_benchmark.py --out report.md [--dejavu path/to/DejaVuSans.ttf]

Needs Ghostscript (``gs``) and Pillow with libtiff. Deterministic: the scans
and the photograph come from a fixed random seed.
"""
from __future__ import annotations

import argparse
from datetime import date
import io
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'api'))

from PIL import Image, ImageChops, ImageDraw, ImageFilter, features  # noqa: E402
from reportlab.lib.pagesizes import A4, letter  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.pages import friendly  # noqa: E402

RATES = (9600, 14400)
CODINGS = ('MH', 'MR', 'MMR')
RESOLUTIONS = {'fine': (204, 196), 'standard': (204, 98)}
LEVELS = (240, 230, 217, 204, 191, 166, 128)
SEED = 20261007

# Public domain: Abraham Lincoln, Gettysburg Address (1863); the Declaration of Independence (1776).
TEXT = (
    "Four score and seven years ago our fathers brought forth on this continent, a new nation, conceived in "
    "Liberty, and dedicated to the proposition that all men are created equal. Now we are engaged in a great "
    "civil war, testing whether that nation, or any nation so conceived and so dedicated, can long endure. We "
    "are met on a great battle-field of that war. We have come to dedicate a portion of that field, as a final "
    "resting place for those who here gave their lives that that nation might live. It is altogether fitting and "
    "proper that we should do this. But, in a larger sense, we can not dedicate -- we can not consecrate -- we "
    "can not hallow -- this ground. The brave men, living and dead, who struggled here, have consecrated it, far "
    "above our poor power to add or detract. The world will little note, nor long remember what we say here, but "
    "it can never forget what they did here. It is for us the living, rather, to be dedicated here to the "
    "unfinished work which they who fought here have thus far so nobly advanced. "
    "When in the Course of human events, it becomes necessary for one people to dissolve the political bands "
    "which have connected them with another, and to assume among the powers of the earth, the separate and "
    "equal station to which the Laws of Nature and of Nature's God entitle them, a decent respect to the "
    "opinions of mankind requires that they should declare the causes which impel them to the separation. We "
    "hold these truths to be self-evident, that all men are created equal, that they are endowed by their "
    "Creator with certain unalienable Rights, that among these are Life, Liberty and the pursuit of Happiness. "
    "That to secure these rights, Governments are instituted among Men, deriving their just powers from the "
    "consent of the governed, That whenever any Form of Government becomes destructive of these ends, it is the "
    "Right of the People to alter or to abolish it, and to institute new Government, laying its foundation on "
    "such principles and organizing its powers in such form, as to them shall seem most likely to effect their "
    "Safety and Happiness."
)


# Corpus -------------------------------------------------------------------------------------------------------

def _wrap(text, font, size, width):
    lines, line = [], ''
    for word in text.split():
        candidate = f'{line} {word}'.strip()
        if pdfmetrics.stringWidth(candidate, font, size) > width and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def _body(pdf, font, size, top, text=TEXT, left=72, width=468, leading=None, limit=72):
    leading = leading or size * 1.3
    y = top
    pdf.setFont(font, size)
    for line in _wrap(text, font, size, width):
        if y < limit:
            break
        pdf.drawString(left, y, line)
        y -= leading
    return y


def font_page(path, font):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.setFillGray(0)
    _body(pdf, font, 11, 720)
    pdf.showPage()
    pdf.save()


def shaded_table(path, pagesize=letter, rotate=0, see_through=False):
    pdf = canvas.Canvas(str(path), pagesize=pagesize)
    pdf.setPageRotation(rotate)
    if see_through:
        # Office programs often draw shading as a see-through black: the same grays, made by transparency.
        pdf.setFillAlpha(0.25)
    pdf.setFont('Helvetica-Bold', 14)
    pdf.drawString(72, 720, 'Statement of account')
    columns = (72, 192, 342, 452, 540)
    top, row_height = 690, 22
    rows = [('Date', 'Description', 'Reference', 'Amount')] + [
        (f'2026-09-{day:02d}', f'Service visit {day}', f'INV-{1000 + day}', f'{(day * 37) % 400 + 25}.00')
        for day in range(1, 19)]
    for index, row in enumerate(rows):
        y = top - index * row_height
        if index == 0:
            pdf.setFillGray(0 if see_through else 0.75)  # header: 25% gray
            pdf.rect(72, y - 6, 468, row_height, fill=1, stroke=0)
        elif index % 2 == 0:
            pdf.setFillGray(0.6 if see_through else 0.9)  # zebra rows: 10% gray
            pdf.rect(72, y - 6, 468, row_height, fill=1, stroke=0)
        pdf.setFillAlpha(1)
        pdf.setFillGray(0)
        pdf.setFont('Helvetica-Bold' if index == 0 else 'Helvetica', 10)
        for left, value in zip(columns, row):
            pdf.drawString(left + 6, y, value)
        if see_through:
            pdf.setFillAlpha(0.25)
    y = top - len(rows) * row_height
    pdf.setFillAlpha(1)
    pdf.setFillGray(0.85)  # total row: 15% gray
    pdf.rect(72, y - 6, 468, row_height, fill=1, stroke=0)
    pdf.setFillGray(0)
    pdf.setFont('Helvetica-Bold', 10)
    pdf.drawString(78, y, 'Total due')
    pdf.drawString(458, y, '4,215.00')
    pdf.setLineWidth(0.5)
    pdf.rect(72, y - 6, 468, (len(rows) + 1) * row_height, fill=0, stroke=1)
    _body(pdf, 'Helvetica', 10, y - 40, TEXT[:600])
    pdf.showPage()
    pdf.save()


def gradient_page(path):
    """A title band shaded from 40% gray to white, as presentation and report templates draw them."""
    from reportlab.lib.colors import Color
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.saveState()
    band = pdf.beginPath()
    band.rect(36, 640, 540, 110)
    pdf.clipPath(band, stroke=0, fill=0)
    pdf.linearGradient(36, 700, 576, 700, (Color(0.6, 0.6, 0.6), Color(1, 1, 1)), extend=False)
    pdf.restoreState()
    pdf.setFillGray(0)
    pdf.setFont('Helvetica-Bold', 22)
    pdf.drawString(54, 690, 'Quarterly referral report')
    _body(pdf, 'Helvetica', 11, 610, TEXT[:1800])
    pdf.showPage()
    pdf.save()


def form_page(path):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.setFont('Helvetica-Bold', 16)
    pdf.drawString(72, 726, 'Patient referral form')
    y = 690
    for section, fields in (('Patient', ('Full name', 'Date of birth', 'Phone', 'Address')),
                            ('Referring provider', ('Name', 'Practice', 'Fax number', 'NPI')),
                            ('Reason for referral', ('Diagnosis', 'Requested service', 'Urgency', 'Notes'))):
        pdf.setFillGray(0.8)  # section bar: 20% gray
        pdf.rect(72, y - 4, 468, 20, fill=1, stroke=0)
        pdf.setFillGray(0)
        pdf.setFont('Helvetica-Bold', 11)
        pdf.drawString(78, y + 2, section)
        y -= 34
        for label in fields:
            pdf.setFont('Helvetica', 9)
            pdf.drawString(78, y + 18, label)
            pdf.setFillGray(0.9)  # field: 10% gray
            pdf.rect(78, y - 2, 456, 16, fill=1, stroke=0)
            pdf.setFillGray(0)
            pdf.setLineWidth(0.5)
            pdf.rect(78, y - 2, 456, 16, fill=0, stroke=1)
            y -= 38
        y -= 6
    pdf.setFont('Helvetica', 9)
    for index, label in enumerate(('Routine', 'Urgent', 'Emergency')):
        pdf.rect(78 + index * 120, y, 9, 9, fill=0, stroke=1)
        pdf.drawString(92 + index * 120, y + 1, label)
    pdf.showPage()
    pdf.save()


def _logo():
    size = 320
    logo = Image.radial_gradient('L').resize((size, size))  # dark centre, light rim
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).ellipse((8, 8, size - 8, size - 8), fill=255)
    out = Image.new('L', (size, size), 255)
    out.paste(logo.point(lambda v: 60 + v * 3 // 4), (0, 0), mask)
    ImageDraw.Draw(out).rectangle((size // 2 - 70, size // 2 - 18, size // 2 + 70, size // 2 + 18), fill=250)
    return out


def letterhead_page(path):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.drawImage(ImageReader(_logo()), 72, 680, 72, 72)
    pdf.setFillGray(0)
    pdf.setFont('Helvetica-Bold', 18)
    pdf.drawString(160, 724, 'Riverside Family Clinic')
    pdf.setFont('Helvetica', 9)
    pdf.drawString(160, 708, '100 Example Street, Springfield  ·  Phone 555-0100  ·  Fax 555-0101')
    pdf.setFillColorRGB(0.8, 0.88, 0.97)  # light blue rule
    pdf.rect(72, 664, 468, 8, fill=1, stroke=0)
    pdf.setFillGray(0)
    _body(pdf, 'Helvetica', 11, 630, TEXT[:1800])
    pdf.setFillColorRGB(0.8, 0.88, 0.97)
    pdf.rect(72, 60, 468, 4, fill=1, stroke=0)
    pdf.showPage()
    pdf.save()


# Elements of the light-content page: (name, how it is drawn); each sits in its own band.
LIGHT_ELEMENTS = (
    ('text, 15% gray', ('gray', 0.85)),
    ('text, 25% gray', ('gray', 0.75)),
    ('text, 35% gray', ('gray', 0.65)),
    ('text, 50% gray', ('gray', 0.5)),
    ('light blue link text', ('rgb', (0.43, 0.66, 0.86))),
    ('standard blue link text', ('rgb', (0.02, 0.39, 0.76))),
    ('red stamp text', ('rgb', (0.8, 0.1, 0.1))),
    ('footer, 8 pt, 40% gray', ('gray', 0.6)),
    ('black text on yellow highlighter', ('highlight', None)),
    ('watermark, 10% gray', ('watermark', None)),
)
BAND_POINTS = 60


def light_content_page(path):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for index, (name, (kind, value)) in enumerate(LIGHT_ELEMENTS):
        top = 756 - index * BAND_POINTS
        base = top - 36
        sample = 'Pay to the order of Example Supply Co., invoice 4471, due 31 October.'
        if kind == 'gray':
            pdf.setFillGray(value)
        elif kind == 'rgb':
            pdf.setFillColorRGB(*value)
        if kind == 'highlight':
            pdf.setFillColorRGB(1, 1, 0)
            pdf.rect(70, base - 4, 400, 16, fill=1, stroke=0)
            pdf.setFillGray(0)
        if kind == 'watermark':
            pdf.setFillGray(0.9)
            pdf.setFont('Helvetica-Bold', 44)
            pdf.drawString(72, base - 6, 'COPY  COPY  COPY')
            continue
        pdf.setFont('Helvetica', 8 if 'footer' in name else 11)
        pdf.drawString(72, base, sample)
    pdf.showPage()
    pdf.save()


def _rng_gray(rng, size, low, high):
    noise = Image.frombytes('L', size, rng.randbytes(size[0] * size[1]))
    span = high - low + 1
    return noise.point(lambda v: low + v % span)


def _text_raster(work, name):
    pdf = work / f'{name}.source.pdf'
    page = canvas.Canvas(str(pdf), pagesize=letter)
    page.setFont('Helvetica-Bold', 14)
    page.drawString(72, 720, 'Scanned letter')
    _body(page, 'Times-Roman', 11, 696, TEXT * 2, limit=90)
    page.showPage()
    page.save()
    out = work / f'{name}.source.tiff'
    _gs(pdf, out, 'tiffgray', (204, 196))
    with Image.open(out) as image:
        return image.convert('L').copy()


def _scan(work, rng):
    text = _text_raster(work, 'scan').filter(ImageFilter.GaussianBlur(0.7))
    paper = _rng_gray(rng, text.size, 222, 240)
    scan = ImageChops.darker(text, paper)
    pixels = scan.load()
    for _ in range(2500):  # dust and scanner noise: single dark dots
        pixels[rng.randrange(2, scan.width - 2), rng.randrange(2, scan.height - 2)] = rng.randrange(0, 90)
    for _ in range(300):  # a few larger specks (2 x 2)
        x, y = rng.randrange(2, scan.width - 3), rng.randrange(2, scan.height - 3)
        for dx in (0, 1):
            for dy in (0, 1):
                pixels[x + dx, y + dy] = 40
    return scan


def _image_page(path, image):
    pdf = canvas.Canvas(str(path), pagesize=letter)
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    buffer.seek(0)
    pdf.drawImage(ImageReader(buffer), 0, 0, letter[0], letter[1])
    pdf.showPage()
    pdf.save()


def scan_gray_page(path, work, rng):
    _image_page(path, _scan(work, rng))


def scan_bilevel_page(path, work, rng):
    # A scanner's black-and-white mode: the same scan thresholded, specks and all.
    scan = _scan(work, rng).point(lambda v: 255 if v >= 150 else 0).convert('1', dither=Image.Dither.NONE)
    _image_page(path, scan.convert('L'))


def photo_page(path, rng):
    width, height = 1734, 1100
    photo = Image.linear_gradient('L').rotate(90).resize((width, height))
    glow = Image.radial_gradient('L').resize((900, 900))
    photo.paste(glow, (500, 100), ImageChops.invert(glow))
    photo = ImageChops.multiply(photo, _rng_gray(rng, (width, height), 225, 255)).filter(ImageFilter.GaussianBlur(2))
    pdf = canvas.Canvas(str(path), pagesize=letter)
    buffer = io.BytesIO()
    photo.save(buffer, 'PNG')
    buffer.seek(0)
    pdf.drawImage(ImageReader(buffer), 36, 330, 540, 342)
    pdf.setFillGray(0)
    _body(pdf, 'Helvetica', 10, 300, TEXT[:900])
    pdf.showPage()
    pdf.save()


def drawn_pages(work):
    """The pages Faxbot draws itself, from its real renderers."""
    from app.batching.image import _separator_pdf, separator_line
    from app.cases.ledger import index_page
    from app.conversion import txt_to_pdf
    from app.direct.service import DirectService
    from datetime import datetime
    pages = {}
    path = work / 'drawn_separator.pdf'
    _separator_pdf([separator_line(2, 3, 'Faxbot 7f3a9c21', 4, 'Front Desk')], path)
    pages['separator page (sending together)'] = path
    plan = SimpleNamespace(
        referenced=tuple({'title': f'Lab results {n}', 'page_count': n + 1, 'digest': f'{n:064x}',
                          'accepted_at': datetime(2026, 9, n + 1)} for n in range(1, 7)),
        included=tuple(SimpleNamespace(title=f'Progress note {n}', pages=n) for n in range(1, 4)))
    path = work / 'drawn_case_index.pdf'
    path.write_bytes(index_page('CASE-2041', '+13035550123', plan, 'Riverside Family Clinic'))
    pages['case index page'] = path
    path = work / 'drawn_challenge.pdf'
    path.write_bytes(DirectService.challenge_document(
        None, {'organization': 'Example Partner Clinic', 'phone_number': '+13035550199'}, '48213907',
        'Riverside Family Clinic'))
    pages['direct delivery code page'] = path
    text = work / 'drawn_text.txt'
    text.write_text('\n'.join(_wrap(TEXT, 'Helvetica', 10, 468)) + '\n', encoding='utf-8')
    path = work / 'drawn_text.pdf'
    txt_to_pdf(str(text), str(path))
    pages['text file Faxbot set (Vera 10 pt)'] = path
    return pages


# Rasterizing and coding ---------------------------------------------------------------------------------------

def _gs(pdf, out, device, dpi):
    subprocess.run([shutil.which('gs'), '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-dPDFSTOPONERROR',
                    f'-sDEVICE={device}', f'-r{dpi[0]}x{dpi[1]}', f'-sOutputFile={out}', '-f', str(pdf)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)


def render(pdf, work, resolution):
    dpi = RESOLUTIONS[resolution]
    halftone_path, gray_path = work / f'{pdf.stem}.{resolution}.g4.tiff', work / f'{pdf.stem}.{resolution}.gray.tiff'
    _gs(pdf, halftone_path, 'tiffg4', dpi)
    _gs(pdf, gray_path, 'tiffgray', dpi)
    with Image.open(halftone_path) as halftone, Image.open(gray_path) as gray:
        today = friendly.bits(halftone)
        today.info['dpi'] = dpi
        return today, friendly.fit(gray.copy(), today.size)


_INVERT = bytes(255 - value for value in range(256))


def _paper_white(page):
    """The page as a fax codes it: libtiff codes 0 bits as white runs, and Pillow keeps white as 1, so invert
    it first (as conversion._g4_data does); otherwise the paper would be coded with black-run codes."""
    return Image.frombytes('1', page.size, friendly.bits(page).tobytes().translate(_INVERT))


def coded_bits(page, coding, dpi):
    """Bits of one page coded as MH, MR or MMR by libtiff (one strip, so MR and MMR code every line)."""
    options = {'MH': {'compression': 'group3'}, 'MR': {'compression': 'group3', 'tiffinfo': {292: 1}},
               'MMR': {'compression': 'group4'}}[coding]
    buffer = io.BytesIO()
    _paper_white(page).save(buffer, 'TIFF', dpi=dpi, strip_size=math.ceil(page.width / 8) * page.height, **options)
    buffer.seek(0)
    with Image.open(buffer) as written:
        counts = written.tag_v2[279]
    return 8 * sum(counts)


def mh_line_bits(page, dpi):
    """Each line's MH bits (with its end-of-line code), rounded up to whole bytes."""
    buffer = io.BytesIO()
    _paper_white(page).save(buffer, 'TIFF', dpi=dpi, compression='group3', strip_size=math.ceil(page.width / 8))
    buffer.seek(0)
    with Image.open(buffer) as written:
        return [8 * count for count in written.tag_v2[279]]


def seconds(bits, rate):
    return bits / rate


def floor_seconds(lines, rate, line_ms):
    return sum(max(bits / rate, line_ms / 1000) for bits in lines)


def black(page):
    return page.histogram()[0]


def variants(today, gray):
    result = {'today (Ghostscript halftone)': today,
              'error diffusion': gray.convert('1'),
              'threshold 50%': gray.point(lambda v: 255 if v >= 128 else 0).convert('1', dither=Image.Dither.NONE)}
    for level in LEVELS:
        result[f'lighten {level}'] = friendly.lighten(gray, today, level)
    result['specks removed'] = friendly.despeckle(today, gray)[0]
    change = friendly.friendly_page(gray, today)
    result['fax-friendly'] = change.page if change is not None else today
    return result, change


def measure(corpus, work):
    rows = {}
    for name, pdf in corpus.items():
        for resolution, dpi in RESOLUTIONS.items():
            today, gray = render(pdf, work, resolution)
            pages, change = variants(today, gray)
            if resolution == 'standard':
                pages = {key: pages[key] for key in ('today (Ghostscript halftone)', 'fax-friendly')}
            entry = {'aligned': change is not None, 'lightened': change.lightened if change else 0,
                     'specks': change.specks if change else 0, 'variants': {}}
            dark = gray.point(lambda v: 255 if v < friendly.LIGHT_LEVEL else 0).convert('1', dither=Image.Dither.NONE)
            pure_black = gray.point(lambda v: 255 if v == 0 else 0).convert('1', dither=Image.Dither.NONE)
            for variant, page in pages.items():
                page.info['dpi'] = dpi
                bits = {coding: coded_bits(page, coding, dpi) for coding in CODINGS}
                difference = ImageChops.logical_xor(friendly.bits(page), today)
                entry['variants'][variant] = {
                    'bits': bits, 'black': black(page),
                    'dark_changed': ImageChops.logical_and(difference, dark).histogram()[255],
                    'text_lost': ImageChops.logical_and(ImageChops.logical_and(difference, pure_black),
                                                        ImageChops.invert(today)).histogram()[255],
                }
                if variant in ('today (Ghostscript halftone)', 'fax-friendly') and resolution == 'fine':
                    entry['variants'][variant]['lines'] = mh_line_bits(page, dpi)
            if name == 'light content (threshold check)' and resolution == 'fine':
                entry['bands'] = bands(today, gray, pages, dpi)
            rows[(name, resolution)] = entry
            print(f'measured {name} at {resolution}', file=sys.stderr)
    return rows


def bands(today, gray, pages, dpi):
    result = []
    for index, (name, _) in enumerate(LIGHT_ELEMENTS):
        top = round((36 + index * BAND_POINTS) * dpi[1] / 72)
        box = (0, top, today.width, min(today.height, top + round(BAND_POINTS * dpi[1] / 72)))
        drawn = gray.crop(box).point(lambda v: 255 if v < 250 else 0).convert('1', dither=Image.Dither.NONE)
        area = drawn.histogram()[255]
        ink = {variant: black(page.crop(box)) for variant, page in pages.items()
               if variant in ('today (Ghostscript halftone)', 'lighten 217', 'lighten 191', 'lighten 166',
                              'threshold 50%', 'fax-friendly')}
        result.append((name, area, ink))
    return result


# Report -------------------------------------------------------------------------------------------------------

def k(bits):
    return f'{bits / 1000:,.0f}'


def pct(before, after):
    return f'{100 * (before - after) / before:.0f}%' if before else '-'


def report(rows, corpus_groups, fonts, out):
    lines = [f'# Fax-friendly pages benchmark (X12), {date.today().isoformat()}', '',
             'Produced by `scripts/fax_friendly_benchmark.py`. Bits are libtiff\'s coded size of one page '
             '(kbit = 1,000 bits); seconds are bits divided by the line speed (no training or page-exchange time, '
             'which is the same for every variant). MMR needs error correction; MH and MR are what a machine '
             'without error correction gets. Light level for the fax-friendly page: gray '
             f'{friendly.LIGHT_LEVEL} (25% gray) and lighter.', '']
    T, F = 'today (Ghostscript halftone)', 'fax-friendly'

    def table(title, names, resolution):
        lines.extend([f'## {title}', '',
                      '| Page | MMR today | MMR friendly | saved | s saved @14,400 | s saved @9,600 | MH today | '
                      'MH friendly | saved | MR saved | MH s saved @9,600 |',
                      '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|'])
        for name in names:
            entry = rows[(name, resolution)]
            before, after = entry['variants'][T]['bits'], entry['variants'][F]['bits']
            lines.append(
                f"| {name} | {k(before['MMR'])} | {k(after['MMR'])} | {pct(before['MMR'], after['MMR'])} | "
                f"{seconds(before['MMR'] - after['MMR'], 14400):.1f} | {seconds(before['MMR'] - after['MMR'], 9600):.1f} | "
                f"{k(before['MH'])} | {k(after['MH'])} | {pct(before['MH'], after['MH'])} | "
                f"{pct(before['MR'], after['MR'])} | {seconds(before['MH'] - after['MH'], 9600):.1f} |")
        lines.append('')

    documents = corpus_groups['documents']
    table('Your documents at fine resolution (kbit per page)', documents, 'fine')
    table('Your documents at standard resolution (kbit per page)', documents, 'standard')
    table('Pages Faxbot draws itself, fine resolution', corpus_groups['drawn'], 'fine')

    lines.extend(['## Seconds on the line, MMR at 14,400 bit/s and MH at 9,600 bit/s, fine', '',
                  '| Page | MMR today s | MMR friendly s | MH today s | MH friendly s | MH 10 ms lines today s | '
                  'friendly s | MH 20 ms lines today s | friendly s |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|'])
    for name in documents + corpus_groups['drawn']:
        entry = rows[(name, 'fine')]
        a, b = entry['variants'][T], entry['variants'][F]
        lines.append(
            f"| {name} | {seconds(a['bits']['MMR'], 14400):.1f} | {seconds(b['bits']['MMR'], 14400):.1f} | "
            f"{seconds(a['bits']['MH'], 9600):.1f} | {seconds(b['bits']['MH'], 9600):.1f} | "
            f"{floor_seconds(a['lines'], 9600, 10):.1f} | {floor_seconds(b['lines'], 9600, 10):.1f} | "
            f"{floor_seconds(a['lines'], 9600, 20):.1f} | {floor_seconds(b['lines'], 9600, 20):.1f} |")
    lines.append('')

    lines.extend(['## Dithering versus thresholding of gray areas (fine, kbit per page)', '',
                  '| Page | today MMR | error diffusion MMR | threshold 50% MMR | friendly MMR | today MH | '
                  'error diffusion MH | threshold 50% MH | friendly MH | threshold: dark pixels changed |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|'])
    for name in documents:
        v = rows[(name, 'fine')]['variants']
        lines.append(
            f"| {name} | {k(v[T]['bits']['MMR'])} | {k(v['error diffusion']['bits']['MMR'])} | "
            f"{k(v['threshold 50%']['bits']['MMR'])} | {k(v[F]['bits']['MMR'])} | {k(v[T]['bits']['MH'])} | "
            f"{k(v['error diffusion']['bits']['MH'])} | {k(v['threshold 50%']['bits']['MH'])} | {k(v[F]['bits']['MH'])} | "
            f"{v['threshold 50%']['dark_changed']:,} |")
    lines.append('')

    lines.extend(['## Light level: what each level saves and what it changes (fine, all your documents together)', '',
                  '| Gray made white | MMR kbit | saved | MH kbit | saved | darker pixels changed | text pixels lost |',
                  '|---|---:|---:|---:|---:|---:|---:|'])
    total_today = {coding: sum(rows[(n, 'fine')]['variants'][T]['bits'][coding] for n in documents) for coding in CODINGS}
    for level in LEVELS:
        variant = f'lighten {level}'
        mmr = sum(rows[(n, 'fine')]['variants'][variant]['bits']['MMR'] for n in documents)
        mh = sum(rows[(n, 'fine')]['variants'][variant]['bits']['MH'] for n in documents)
        lost = sum(rows[(n, 'fine')]['variants'][variant]['text_lost'] for n in documents)
        # Darker than this level: pixels with gray under the fax-friendly level that changed.
        dark = sum(rows[(n, 'fine')]['variants'][variant]['dark_changed'] for n in documents)
        lines.append(f'| {level} ({round(100 * (255 - level) / 255)}% gray) and lighter | {k(mmr)} | '
                     f"{pct(total_today['MMR'], mmr)} | {k(mh)} | {pct(total_today['MH'], mh)} | {dark:,} | {lost:,} |")
    lines.append(f"| today | {k(total_today['MMR'])} | - | {k(total_today['MH'])} | - | - | - |")
    lines.append('')
    lines.append('"Darker pixels changed" counts pixels whose gray is darker than the fax-friendly level '
                 f'({friendly.LIGHT_LEVEL}) and that differ from today; "text pixels lost" counts pure black pixels '
                 'that were black today and are white now.')
    lines.append('')

    entry = rows[('light content (threshold check)', 'fine')]
    lines.extend(['## Light content: ink kept on each element (fine, black pixels)', '',
                  '| Element | drawn area | today | lighten 217 | lighten 191 (fax-friendly) | lighten 166 | '
                  'threshold 50% | today covers |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|'])
    for name, area, ink in entry['bands']:
        lines.append(f"| {name} | {area:,} | {ink[T]:,} | {ink['lighten 217']:,} | {ink['lighten 191']:,} | "
                     f"{ink['lighten 166']:,} | {ink['threshold 50%']:,} | "
                     f"{100 * ink[T] / area if area else 0:.0f}% |")
    lines.append('')
    lines.append('"Today covers" is today\'s black pixels as a share of the area the element covers: text drawn '
                 'at 25% gray or lighter is already only a scatter of dots on today\'s fax.')
    lines.append('')

    lines.extend(['## Specks, removed after light shading is gone (fine)', '',
                  '| Page | specks removed | MMR kbit before | after | saved | MH kbit before | after | saved |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|'])
    for name in documents:
        v = rows[(name, 'fine')]['variants']
        before, after = v[f'lighten {friendly.LIGHT_LEVEL}']['bits'], v[F]['bits']
        lines.append(f"| {name} | {rows[(name, 'fine')]['specks']:,} | {k(before['MMR'])} | {k(after['MMR'])} | "
                     f"{pct(before['MMR'], after['MMR'])} | {k(before['MH'])} | {k(after['MH'])} | "
                     f"{pct(before['MH'], after['MH'])} |")
    lines.append('')
    lines.append('Before light shading is made white, the dots of a light halftone are specks too (a black dot with '
                 'only light gray around it), so specks are counted here after it.')
    lines.append('')

    lines.extend(['## Fonts for pages Faxbot draws (same text, 11 pt, fine)', '',
                  '| Font | characters | MMR kbit | MMR bits per character | MH kbit | MH bits per character | '
                  'MMR vs Helvetica |', '|---|---:|---:|---:|---:|---:|---:|'])
    base = None
    for font, (name, characters) in fonts.items():
        bits = rows[(name, 'fine')]['variants'][T]['bits']
        base = base or bits['MMR'] / characters
        lines.append(f"| {font} | {characters:,} | {k(bits['MMR'])} | {bits['MMR'] / characters:.1f} | "
                     f"{k(bits['MH'])} | {bits['MH'] / characters:.1f} | "
                     f"{100 * (bits['MMR'] / characters - base) / base:+.0f}% |")
    lines.append('')
    lines.append('Alignment: every page\'s gray drawing lined up with today\'s fax image: '
                 + ('yes.' if all(entry['aligned'] for entry in rows.values()) else
                    'no for ' + ', '.join(sorted({n for (n, _), e in rows.items() if not e['aligned']})) + '.'))
    lines.append('')
    Path(out).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--out', default='fax-friendly-benchmark.md')
    parser.add_argument('--work', default=None, help='Keep the corpus and drawings in this folder.')
    parser.add_argument('--dejavu', default=None, help='DejaVuSans.ttf for the font comparison.')
    args = parser.parse_args()
    if shutil.which('gs') is None or not features.check('libtiff'):
        raise SystemExit('Needs Ghostscript and Pillow with libtiff.')
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix='fax-friendly-'))
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    pdfmetrics.registerFont(TTFont('Vera', str(Path(__import__('reportlab').__file__).parent / 'fonts' / 'Vera.ttf')))
    font_names = {'Helvetica (separator, index and code pages)': 'Helvetica', 'Vera (text files)': 'Vera',
                  'Times Roman': 'Times-Roman', 'Courier': 'Courier'}
    candidates = [args.dejavu] if args.dejavu else []
    candidates += [str(ROOT / 'api' / 'app' / 'forms' / 'fonts' / 'DejaVuSans.ttf')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            pdfmetrics.registerFont(TTFont('DejaVuSans', candidate))
            font_names['DejaVu Sans (forms)'] = 'DejaVuSans'
            break
    corpus, groups, fonts = {}, {'documents': [], 'drawn': []}, {}
    builders = (('shaded table', shaded_table), ('shaded table, A4', lambda path: shaded_table(path, A4)),
                ('shaded table, page turned sideways', lambda path: shaded_table(path, rotate=90)),
                ('shaded table, see-through shading', lambda path: shaded_table(path, see_through=True)),
                ('title band shaded from gray to white', gradient_page),
                ('form with shaded fields', form_page),
                ('letterhead with gray logo', letterhead_page),
                ('light content (threshold check)', light_content_page))
    for name, build in builders:
        path = work / (name.split(' ')[0] + '_' + str(len(corpus)) + '.pdf')
        build(path)
        corpus[name] = path
        groups['documents'].append(name)
    for name, build in (('scan, gray, with noise', scan_gray_page), ('scan, black and white, with specks',
                                                                    scan_bilevel_page)):
        path = work / (name.split(',')[0] + '_' + str(len(corpus)) + '.pdf')
        build(path, work, rng)
        corpus[name] = path
        groups['documents'].append(name)
    path = work / 'photo.pdf'
    photo_page(path, rng)
    corpus['photograph with caption'] = path
    groups['documents'].append('photograph with caption')
    for name, path in drawn_pages(work).items():
        corpus[name] = path
        groups['drawn'].append(name)
    for label, font in font_names.items():
        path = work / f'font_{font}.pdf'
        font_page(path, font)
        name = f'font: {label}'
        corpus[name] = path
        drawn = sum(len(line) for line in _wrap(TEXT, font, 11, 468))
        fonts[label] = (name, drawn)
    rows = measure(corpus, work)
    report(rows, groups, fonts, args.out)
    print(f'wrote {args.out}', file=sys.stderr)


if __name__ == '__main__':
    main()
