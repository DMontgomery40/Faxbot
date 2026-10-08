"""Protected screens (pages/screens.py) and the fidelity measure (pages/fidelity.py).

Codex's six synthetic pages and the pale-text challenge (research/faxbot-next-experiments-2026-10-08): the shaded
table, the tinted form and the pale-text page are AR's synthetic PDFs (``scripts/fax_friendly_benchmark.py``,
copied to ``fixtures/shading``); the noisy scan and the photograph are drawn here from a fixed seed; the black text
page is Faxbot's own text renderer. Every page goes through Ghostscript as Faxbot draws it. Then one synthetic page
per kind of content (small type, faint handwriting, decimal points, checkbox state, barcodes, stamps, highlights,
annotations) checks the fidelity measure, and the two protections Codex's probe did not have (no pattern in an area
too small to show it: a shaded checkbox, a thin strip) are shown to be why they exist. All synthetic."""
from pathlib import Path
import random
import shutil
import subprocess

from PIL import Image, ImageChops, ImageDraw, ImageFilter
import pytest

from app import conversion
from app.pages import fidelity, friendly, screens

FIXTURES = Path(__file__).parent / 'fixtures' / 'shading'
needs_gs = pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript draws the PDF pages')
LINE = 14400


def seconds(page):
    return sum(conversion.frame_bits([page])) / LINE


def render(pdf, folder):
    """(gray drawing, today's fax image) of a one-page PDF, as Faxbot draws them."""
    folder = Path(folder)
    conversion.pdf_to_tiff(str(pdf), str(folder / f'{Path(pdf).stem}.tiff'))
    today = conversion.read_fax_frames(str(folder / f'{Path(pdf).stem}.tiff'))[0]
    friendly._render_gray(str(pdf), str(folder / f'{Path(pdf).stem}.gray.tiff'), shutil.which('gs'))
    with Image.open(folder / f'{Path(pdf).stem}.gray.tiff') as drawing:
        gray = friendly.fit(drawing.copy(), today.size)
    return gray, today


def canvas(path, size=(612, 792)):
    from reportlab.pdfgen import canvas as pdfcanvas
    return pdfcanvas.Canvas(str(path), pagesize=size)


def changed_in(box, before, after):
    return screens.count(ImageChops.logical_xor(friendly.bits(before).crop(box), friendly.bits(after).crop(box)))


def ink(page, box=None):
    page = friendly.bits(page)
    return (page.crop(box) if box else page).histogram()[0]


# Codex's six pages and the pale-text challenge -----------------------------------------------------------------

def scan_pdf(path):
    """A gray scan: blurred text on paper with grain, dust and specks (a fixed seed, as AR's benchmark draws it)."""
    rng = random.Random(20261008)
    width, height = 1734, 2244
    text = Image.new('L', (width, height), 255)
    draw = ImageDraw.Draw(text)
    for line in range(60):
        draw.text((200, 220 + line * 30), 'Scanned letter, line %d: four score and seven years ago' % line, fill=0)
    text = text.filter(ImageFilter.GaussianBlur(0.7))
    paper = Image.frombytes('L', (width, height), bytes(222 + rng.randrange(19) for _ in range(width * height)))
    scan = ImageChops.darker(text, paper)
    pixels = scan.load()
    for _ in range(2500):
        pixels[rng.randrange(2, width - 2), rng.randrange(2, height - 2)] = rng.randrange(0, 90)
    _image_page(path, scan)


def photo_pdf(path):
    """A photograph with a caption: a gradient, a glow and grain, blurred (a fixed seed)."""
    rng = random.Random(20261009)
    width, height = 1734, 1100
    photo = Image.linear_gradient('L').rotate(90).resize((width, height))
    glow = Image.radial_gradient('L').resize((900, 900))
    photo.paste(glow, (500, 100), ImageChops.invert(glow))
    grain = Image.frombytes('L', (width, height), bytes(225 + rng.randrange(31) for _ in range(width * height)))
    photo = ImageChops.multiply(photo, grain).filter(ImageFilter.GaussianBlur(2))
    from reportlab.lib.utils import ImageReader
    import io
    buffer = io.BytesIO()
    photo.save(buffer, 'PNG')
    buffer.seek(0)
    pdf = canvas(path)
    pdf.drawImage(ImageReader(buffer), 36, 330, 540, 342)
    pdf.setFont('Helvetica', 10)
    pdf.drawString(72, 300, 'Site photograph, north wall, taken 2026-09-14.')
    pdf.showPage()
    pdf.save()


def _image_page(path, image):
    from reportlab.lib.utils import ImageReader
    import io
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    buffer.seek(0)
    pdf = canvas(path)
    pdf.drawImage(ImageReader(buffer), 0, 0, 612, 792)
    pdf.showPage()
    pdf.save()


@pytest.fixture(scope='module')
def corpus(tmp_path_factory):
    """Codex's six pages, each (gray, today) as Faxbot draws them."""
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages')
    folder = tmp_path_factory.mktemp('corpus')
    scan_pdf(folder / 'scan.pdf')
    photo_pdf(folder / 'photo.pdf')
    text = folder / 'text.txt'
    text.write_text('\n'.join(['Four score and seven years ago our fathers brought forth on this continent,'] * 40))
    conversion.txt_to_pdf(str(text), str(folder / 'text.pdf'))
    pages = {name: FIXTURES / f'{name}.pdf' for name in ('shaded_table', 'tinted_form', 'pale_text')}
    pages.update(scan=folder / 'scan.pdf', photo=folder / 'photo.pdf', text=folder / 'text.pdf')
    return {name: render(pdf, folder) for name, pdf in pages.items()}


# (today's seconds, Codex's eight-row screen, Faxbot's protected screen): MMR bits at 14,400 bit/s. Codex measured
# the first two with Ghostscript 10 and libtiff 4.7; another Ghostscript may draw a slightly different halftone.
FIGURES = {'shaded_table': (60.65, 25.72, 26.79), 'tinted_form': (51.12, 12.44, 12.61)}
UNPROTECTED = {'word_gap': 0, 'min_width': 1, 'min_height': 1}


@pytest.mark.parametrize('name', sorted(FIGURES))
def test_shaded_pages_save_codex_figures_unprotected_and_close_to_them_protected(corpus, name):
    gray, today = corpus[name]
    before, codex, protected = FIGURES[name]
    assert seconds(today) == pytest.approx(before, rel=0.03)
    loose = screens.screen_page(gray, today, **UNPROTECTED)
    shipped = screens.screen_page(gray, today)
    # Without the two extra protections the renderer is Codex's probe: at least his saving.
    assert 1 - seconds(loose.page) / seconds(today) >= 1 - codex / before - 0.005
    # With them, about a second short of it on the table (the reasons are tested below).
    assert seconds(shipped.page) == pytest.approx(protected, rel=0.03)
    assert seconds(loose.page) <= seconds(shipped.page) < seconds(today) * 0.46
    # Every frozen pixel is as today, and the fidelity measure keeps the page.
    assert screens.count(ImageChops.logical_and(ImageChops.logical_xor(shipped.page, friendly.bits(today)),
                                                shipped.frozen)) == 0
    assert fidelity.assess(gray, today, shipped.page, frozen=shipped.frozen).kept


def test_the_band_is_codex_band_pixel_for_pixel(corpus):
    for name in ('shaded_table', 'tinted_form', 'pale_text', 'scan'):
        gray, _ = corpus[name]
        codex = ImageChops.difference(gray.filter(ImageFilter.MaxFilter(9)), gray.filter(ImageFilter.MinFilter(9)))
        codex = codex.point(lambda value: 255 if value else 0)
        inner = (screens.BAND, screens.BAND, gray.width - screens.BAND, gray.height - screens.BAND)
        assert ImageChops.difference(codex.crop(inner), screens.edge_band(gray).crop(inner)).getbbox() is None, name
        # The edges of the page itself are always in the band: what lies beyond them is unknown.
        assert screens.edge_band(gray).crop((0, 0, gray.width, screens.BAND)).getextrema() == (255, 255)


# The pale-text challenge: three bands of text at 15%, 25% and 35% gray (AR's light-content page, 60 points each).
BANDS = [(0, round((36 + index * 60) * 196 / 72), 1728, round((36 + index * 60) * 196 / 72) + round(60 * 196 / 72))
         for index in range(3)]


def test_the_pale_text_bands_stay_pixel_for_pixel_and_whitening_erases_them(corpus):
    gray, today = corpus['pale_text']
    assert [ink(today, box) for box in BANDS] == [657, 968, 1577]
    for page in (screens.screen_page(gray, today).page, screens.screen_page(gray, today, **UNPROTECTED).page):
        assert [changed_in(box, today, page) for box in BANDS] == [0, 0, 0]
    whitened = friendly.friendly_page(gray, today, despeckle_page=False).page
    assert [ink(whitened, box) for box in BANDS] == [0, 0, 1577]
    found = fidelity.assess(gray, today, whitened)
    assert not found.kept and 'pale_marks' in found.losses


@pytest.mark.parametrize('name', ['scan', 'photo', 'text'])
def test_a_noisy_scan_a_photograph_and_black_text_do_not_change(corpus, name):
    gray, today = corpus[name]
    shipped = screens.screen_page(gray, today)
    assert shipped.changed == 0 and shipped.page.tobytes() == friendly.bits(today).tobytes()
    assert fidelity.assess(gray, today, shipped.page) == fidelity.UNCHANGED


def test_stripes_code_smaller_than_feng_fuchs_and_bouman_clustered_dots(corpus):
    # Feng, Fuchs and Bouman (2003) fill backgrounds with a 3 x 6 45-degree clustered dot because it compresses
    # far better than error diffusion under G3/G4. Measured here against horizontal stripes on the same pages.
    for name in ('shaded_table', 'tinted_form'):
        gray, today = corpus[name]
        sizes = {pattern: seconds(screens.screen_page(gray, today, pattern=pattern).page)
                 for pattern in screens.PATTERNS}
        assert sizes['lines'] < sizes['dots'] < sizes['feng'], (name, sizes)
        assert sizes['lines'] < 0.5 * seconds(today) and sizes['dots'] < 0.8 * seconds(today)
        assert sizes['feng'] > 0.9 * seconds(today)  # at fax resolution their fine screen saves almost nothing
    assert screens.DEFAULT_PATTERN == 'lines'


# The renderer on small synthetic drawings ------------------------------------------------------------------------

def flat_page(width=200, height=120, gray_value=230):
    """A gray page with one uniform shaded rectangle and today's halftone of it (every fourth pixel black)."""
    gray = Image.new('L', (width, height), 255)
    ImageDraw.Draw(gray).rectangle((20, 20, width - 21, height - 21), fill=gray_value)
    today = Image.new('1', (width, height), 255)
    for y in range(20, height - 20):
        for x in range(20, width - 20):
            if x % 4 == 0 and y % 4 == 0:
                today.putpixel((x, y), 0)
    today.info['dpi'] = (204.0, 196.0)
    return gray, today


def test_only_gray_the_pattern_can_draw_is_screened():
    lightest, darkest = screens.drawable()
    assert (lightest, darkest) == (16, 238)
    for value, screened in ((230, True), (240, False), (250, False), (10, False), (128, True)):
        gray, today = flat_page(gray_value=value)
        shipped = screens.screen_page(gray, today)
        assert (shipped.screened > 0) == screened, value
        if not screened:
            assert shipped.changed == 0  # a 4% tint never turns white
    gray, today = flat_page(gray_value=230)
    page = screens.screen_page(gray, today).page
    # One black row in eight across the interior: a 10% tint drawn at 12.5%.
    rows = [y for y in range(28, 92) if friendly.bits(page).getpixel((100, y)) == 0]
    assert rows and all((y % screens.PERIOD) == screens.PERIOD - 1 for y in rows)


def test_drawings_that_do_not_line_up_are_not_screened():
    gray, today = flat_page()
    gray.putpixel((5, 5), 0)  # pure black in gray, white in today's image
    assert screens.screen_page(gray, today) is None


def test_the_morphology_matches_a_direct_window():
    rng = random.Random(7)
    mask = Image.new('L', (40, 30), 0)
    for _ in range(25):
        mask.putpixel((rng.randrange(40), rng.randrange(30)), 255)
    grown = screens.dilate(mask, 2, 3, 1, 4)
    for y in range(30):
        for x in range(40):
            expected = any(mask.getpixel((x + dx, y + dy)) for dx in range(-3, 3) for dy in range(-4, 2)
                           if 0 <= x + dx < 40 and 0 <= y + dy < 30)
            assert bool(grown.getpixel((x, y))) == expected, (x, y)


# One synthetic page for each kind of content -----------------------------------------------------------------------

def _page(path, draw, size=(288, 216)):
    pdf = canvas(path, size)
    draw(pdf)
    pdf.showPage()
    pdf.save()
    return path


def _shaded_box(pdf, x, y, width, height, gray=0.9):
    pdf.setFillGray(gray)
    pdf.rect(x, y, width, height, fill=1, stroke=0)
    pdf.setFillGray(0)


def small_type(pdf):
    _shaded_box(pdf, 20, 120, 248, 60)
    pdf.setFont('Helvetica', 5)
    for line in range(6):
        pdf.drawString(26, 170 - line * 8, 'Dosage 2.5 mg twice daily; refills 0; lot 77-1024-B (small print)')


def faint_handwriting(pdf):
    _shaded_box(pdf, 20, 100, 248, 90)
    for shade, top in ((0.8, 170), (0.75, 140), (0.85, 60)):  # 20%, 25% and 15% gray pencil, on shading and off
        pdf.setStrokeGray(shade)
        pdf.setLineWidth(0.6)
        path = pdf.beginPath()
        path.moveTo(30, top)
        for step in range(1, 24):
            path.curveTo(30 + step * 10 - 6, top + 6, 30 + step * 10 - 3, top - 6, 30 + step * 10, top)
        pdf.drawPath(path, stroke=1, fill=0)


def decimal_points(pdf):
    _shaded_box(pdf, 20, 110, 248, 70)
    pdf.setFont('Helvetica', 9)
    for row, (text, shade) in enumerate((('Total due 1.50', 0), ('Rate 0.25 per page', 0.75), ('Dose 7.5', 0.8))):
        pdf.setFillGray(shade)
        pdf.drawString(30, 160 - row * 18, text)
        pdf.drawString(30, 60 - row * 18, text)


def checkboxes(pdf):
    pdf.setFont('Helvetica', 9)
    pdf.setLineWidth(0.5)
    for index, checked in enumerate((False, True, False)):
        x = 40 + index * 80
        _shaded_box(pdf, x, 120, 9, 9)  # a 9-point shaded checkbox, as forms draw them
        pdf.rect(x, 120, 9, 9, fill=0, stroke=1)
        if checked:
            pdf.line(x + 1.5, 121.5, x + 7.5, 127.5)
            pdf.line(x + 1.5, 127.5, x + 7.5, 121.5)
        pdf.drawString(x + 13, 121, ('Routine', 'Urgent', 'Emergency')[index])


def barcode(pdf):
    from reportlab.graphics.barcode import code128
    _shaded_box(pdf, 20, 80, 248, 100)
    code128.Code128('FAXBOT-2041-77', barHeight=40, barWidth=0.9).drawOn(pdf, 30, 110)


def stamp(pdf):
    _shaded_box(pdf, 20, 60, 248, 120)
    pdf.setStrokeColorRGB(0.8, 0.1, 0.1)
    pdf.setFillColorRGB(0.8, 0.1, 0.1)
    pdf.setLineWidth(2)
    pdf.rect(60, 100, 160, 40, fill=0, stroke=1)
    pdf.setFont('Helvetica-Bold', 18)
    pdf.drawString(72, 113, 'RECEIVED')
    pdf.setFillColorRGB(0.43, 0.66, 0.86)  # a light blue date line
    pdf.setFont('Helvetica', 9)
    pdf.drawString(72, 88, '2026-10-08')


def highlight(pdf):
    # Yellow highlighter draws as 3% gray on a fax (too light to screen: it stays as today); pink as 18%.
    for top, colour in ((150, (1, 1, 0)), (100, (1, 0.75, 0.8))):
        pdf.setFillColorRGB(*colour)
        pdf.rect(20, top, 248, 26, fill=1, stroke=0)
        pdf.setFillGray(0)
        pdf.setFont('Helvetica', 11)
        pdf.drawString(24, top + 8, 'Allergy: penicillin.')
    pdf.drawString(24, 40, 'Plain text below the highlights.')


def thin_strip(pdf):
    _shaded_box(pdf, 20, 150, 248, 5)  # a shaded rule 14 rows tall: 6 rows inside its edges, under one period
    _shaded_box(pdf, 20, 60, 248, 60)  # and a roomy shaded panel for contrast


KINDS = {'small_type': small_type, 'faint_handwriting': faint_handwriting, 'decimal_points': decimal_points,
         'checkboxes': checkboxes, 'barcode': barcode, 'stamp': stamp, 'highlight': highlight,
         'thin_strip': thin_strip}


@pytest.fixture(scope='module')
def kinds(tmp_path_factory):
    if shutil.which('gs') is None:
        pytest.skip('Ghostscript draws the PDF pages')
    folder = tmp_path_factory.mktemp('kinds')
    return {name: render(_page(folder / f'{name}.pdf', draw), folder) for name, draw in KINDS.items()}


@pytest.mark.parametrize('name', sorted(KINDS))
def test_every_kind_of_content_keeps_every_mark_when_screened(kinds, name):
    gray, today = kinds[name]
    shipped = screens.screen_page(gray, today)
    found = fidelity.assess(gray, today, shipped.page, frozen=shipped.frozen)
    assert found.kept, (name, found.losses)
    lost = ImageChops.darker(fidelity.marks(gray), ImageChops.logical_xor(shipped.page, friendly.bits(today))
                             .convert('L'))
    assert lost.getbbox() is None, name
    if name not in ('thin_strip', 'checkboxes'):
        assert shipped.screened > 0, name  # each page has room to screen, and the shading still goes faster
        assert seconds(shipped.page) < seconds(today)


@pytest.mark.parametrize('name, loss', [('faint_handwriting', 'pale_marks'), ('decimal_points', 'pale_marks'),
                                        ('highlight', 'shading'), ('small_type', 'shading')])
def test_whitening_is_caught_where_it_erases_something(kinds, name, loss):
    gray, today = kinds[name]
    whitened = friendly.friendly_page(gray, today, despeckle_page=False).page
    found = fidelity.assess(gray, today, whitened)
    assert not found.kept and loss in found.losses, found.losses


def test_a_missing_decimal_point_is_caught_though_it_is_a_few_pixels(kinds):
    gray, today = kinds['decimal_points']
    # "Total due 1.50" in black on white paper: find its decimal point (the smallest mark on that line) and erase it.
    marks = fidelity.marks(gray).convert('1')
    row = (0, round((216 - 66) * 196 / 72), today.width, round((216 - 56) * 196 / 72))
    erased = friendly.bits(today).copy()
    point = None
    for x in range(row[0] + 60, row[2]):
        column = [y for y in range(row[1], row[3]) if friendly.bits(today).getpixel((x, y)) == 0]
        if column and len(column) <= 4 and point is None and all(
                friendly.bits(today).getpixel((x - 3, y)) == 255 and friendly.bits(today).getpixel((x + 4, y)) == 255
                for y in column):
            point = (x, column)
    assert point is not None
    x, column = point
    for dx in range(0, 4):
        for y in column:
            erased.putpixel((x + dx, y), 255)
    changed = screens.count(ImageChops.logical_xor(erased, friendly.bits(today)))
    assert 0 < changed <= 16 and screens.count(ImageChops.logical_and(marks, ImageChops.logical_xor(
        erased, friendly.bits(today)))) == changed
    found = fidelity.assess(gray, today, erased)
    assert not found.kept and 'marks' in found.losses


def test_a_highlight_stays_visibly_distinct(kinds):
    gray, today = kinds['highlight']
    shipped = screens.screen_page(gray, today).page

    def band(top):  # the right half of a highlight, beyond its text
        return (round(160 * 204 / 72), round((216 - top - 22) * 196 / 72), round(260 * 204 / 72),
                round((216 - top - 4) * 196 / 72))
    yellow, pink = band(150), band(100)
    assert changed_in(yellow, today, shipped) == 0  # today's fax already shows it as nearly white paper
    area = (pink[2] - pink[0]) * (pink[3] - pink[1])
    assert changed_in(pink, today, shipped) > 0
    assert 0.6 * ink(today, pink) <= ink(shipped, pink) <= area * 0.35  # still pink-gray, not white, not black
    whitened = friendly.friendly_page(gray, today, despeckle_page=False).page
    assert ink(whitened, yellow) == ink(whitened, pink) == 0
    assert 'shading' in fidelity.assess(gray, today, whitened).losses


def test_why_a_shaded_checkbox_gets_no_pattern(kinds):
    # Without the width rule, two stripes land inside each shaded 9-point box and an empty box reads as marked.
    gray, today = kinds['checkboxes']
    loose = screens.screen_page(gray, today, **UNPROTECTED)
    shipped = screens.screen_page(gray, today)
    assert loose.changed > 0 and shipped.changed == 0
    found = fidelity.assess(gray, today, loose.page)
    assert not found.kept and 'small_areas' in found.losses


def test_why_a_thin_shaded_strip_gets_no_pattern(kinds):
    # A strip shorter than one stripe period cannot show its tone: it comes out white or as one underline.
    gray, today = kinds['thin_strip']
    strip = (0, round((216 - 155) * 196 / 72) - 2, today.width, round((216 - 150) * 196 / 72) + 2)
    loose = screens.screen_page(gray, today, **UNPROTECTED)
    shipped = screens.screen_page(gray, today)
    assert changed_in(strip, today, shipped.page) == 0 and changed_in(strip, today, loose.page) > 0
    assert 'small_areas' in fidelity.assess(gray, today, loose.page).losses
    assert shipped.screened > 0  # the roomy panel below is still screened


# The PDF's own text and annotation objects ---------------------------------------------------------------------

@needs_gs
def test_text_objects_are_frozen_whole(tmp_path):
    def big_pale_word(pdf):
        pdf.setFillGray(0.85)
        pdf.setFont('Helvetica-Bold', 72)
        pdf.drawString(20, 80, 'COPY')
    pdf = _page(tmp_path / 'word.pdf', big_pale_word, (288, 216))
    gray, today = render(pdf, tmp_path)
    masks = screens.object_masks(pdf, [gray], shutil.which('gs'), tmp_path)
    assert screens.count(masks[0]) > 2000
    plain = screens.screen_page(gray, today)
    kept = screens.screen_page(gray, today, objects=masks[0])
    assert plain.screened > 0 and kept.changed == 0  # the strokes' interiors stay as today with the text mask
    assert not list(tmp_path.glob('.faxbot-screens-*'))


@needs_gs
def test_annotations_are_frozen_whole(tmp_path):
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject
    pdf = _page(tmp_path / 'plain.pdf', lambda pdf: _shaded_box(pdf, 20, 40, 248, 140), (288, 216))
    writer = PdfWriter(clone_from=PdfReader(str(pdf)))
    # A square annotation with its own appearance (as a reviewer's markup tool writes it): filled 20% gray.
    look = DecodedStreamObject()
    look.set_data(b'0.8 g 0 0 160 60 re f')
    look.update({NameObject('/Type'): NameObject('/XObject'), NameObject('/Subtype'): NameObject('/Form'),
                 NameObject('/BBox'): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(160),
                                                   NumberObject(60)])})
    note = DictionaryObject({
        NameObject('/Type'): NameObject('/Annot'), NameObject('/Subtype'): NameObject('/Square'),
        NameObject('/Rect'): ArrayObject([NumberObject(60), NumberObject(80), NumberObject(220), NumberObject(140)]),
        NameObject('/F'): NumberObject(4),
        NameObject('/AP'): DictionaryObject({NameObject('/N'): writer._add_object(look)})})
    writer.add_annotation(page_number=0, annotation=note)
    with open(tmp_path / 'annotated.pdf', 'wb') as handle:
        writer.write(handle)
    assert screens.has_annotations(tmp_path / 'annotated.pdf') and not screens.has_annotations(pdf)
    gray, today = render(tmp_path / 'annotated.pdf', tmp_path)
    masks = screens.object_masks(tmp_path / 'annotated.pdf', [gray], shutil.which('gs'), tmp_path)
    box = (round(60 * 204 / 72), round((216 - 140) * 196 / 72), round(220 * 204 / 72), round((216 - 80) * 196 / 72))
    assert screens.count(masks[0].crop(box)) > 0.5 * (box[2] - box[0]) * (box[3] - box[1])
    kept = screens.screen_page(gray, today, objects=masks[0])
    assert changed_in(box, today, kept.page) == 0 and kept.screened > 0


def test_a_failed_drawing_means_no_masks(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise subprocess.CalledProcessError(1, 'gs')
    monkeypatch.setattr(subprocess, 'run', broken)
    gray, _ = flat_page()
    with pytest.raises(screens.MasksUnavailable):
        screens.object_masks(tmp_path / 'missing.pdf', [gray], '/usr/bin/false', tmp_path)
    assert not list(tmp_path.glob('.faxbot-screens-*'))


# The measure itself, on tiny drawings ------------------------------------------------------------------------------

def test_marks_are_thin_dark_structures_and_shading_is_not():
    gray = Image.new('L', (80, 60), 255)
    draw = ImageDraw.Draw(gray)
    draw.rectangle((5, 5, 74, 54), fill=230)  # shading
    draw.rectangle((20, 20, 22, 22), fill=0)  # a decimal point on it
    draw.line((30, 40, 60, 40), fill=200)  # a pale stroke
    found = fidelity.marks(gray)
    assert found.getpixel((21, 21)) == 255 and found.getpixel((45, 40)) == 255
    assert found.getpixel((40, 30)) == 0 and found.getpixel((10, 10)) == 0


def test_an_unchanged_page_is_kept_and_ranks_first():
    gray, today = flat_page()
    assert fidelity.assess(gray, today, today) == fidelity.UNCHANGED
    shipped = screens.screen_page(gray, today)
    found = fidelity.assess(gray, today, shipped.page, frozen=shipped.frozen)
    assert found.kept and found.difference > 0
    whitened = friendly.friendly_page(gray, today, despeckle_page=False).page
    lost = fidelity.assess(gray, today, whitened)
    assert not lost.kept
    assert sorted([lost, found, fidelity.UNCHANGED], key=lambda item: item.rank) == [fidelity.UNCHANGED, found, lost]


def test_a_change_inside_the_frozen_mask_is_a_loss():
    gray, today = flat_page()
    shipped = screens.screen_page(gray, today)
    broken = shipped.page.copy()
    broken.putpixel((20, 20), 255 - broken.getpixel((20, 20)))
    assert 'frozen' in fidelity.assess(gray, today, broken, frozen=shipped.frozen).losses
