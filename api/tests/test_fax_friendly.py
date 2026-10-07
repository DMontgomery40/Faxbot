"""Fax-friendly pages (migration 0042): light shading left out and specks removed before a page goes on the line.

The transform is deterministic; every pixel darker than the light level stays exactly as Faxbot draws it today
(so text stays as readable as it was); a page that is already black and white from a scanner is untouched unless
the setting for your documents is on; pages Faxbot draws itself from text are unchanged; the setting is off by
default; every sentence. SQLite and PostgreSQL. All synthetic."""
from datetime import datetime, timedelta
import io
import shutil
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
from PIL import Image, ImageDraw
import pytest
import sqlalchemy as sa

from api.app import schema, schema_friendly_pages
from app import conversion
from app.config_values import ConfigurationValues
from app.pages import friendly, sending, views
from api.tests.test_schema import database, snapshot  # noqa: F401 - fixture
from api.tests.test_access_schema import at_revision
from api.tests.test_work_schema import without_later_access_changes

JOB, ATTEMPT = 'a' * 32, 'b' * 32
NOW = datetime(2026, 10, 7, 9, 0, 0)
PEER = '+15555550199'
needs_gs = pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript draws the PDF pages')


def drawings(width=60, height=40):
    """A gray page and the halftone Faxbot sends today for it: a 50% block drawn as a checkerboard, a 15% block
    as sparse dots, black strokes, and one dark speck on white paper."""
    gray = Image.new('L', (width, height), 255)
    draw = ImageDraw.Draw(gray)
    draw.rectangle((2, 2, 17, 17), fill=127)
    draw.rectangle((22, 2, 37, 17), fill=217)
    draw.rectangle((42, 2, 44, 30), fill=0)
    draw.rectangle((2, 25, 30, 26), fill=0)
    gray.putpixel((50, 35), 30)
    halftone = Image.new('1', (width, height), 255)
    for y in range(height):
        for x in range(width):
            value = gray.getpixel((x, y))
            if (value == 0 or value == 30 or (value == 127 and (x + y) % 2 == 0)
                    or (value == 217 and x % 4 == 0 and y % 4 == 0)):
                halftone.putpixel((x, y), 0)
    halftone.info['dpi'] = (204.0, 196.0)
    return gray, halftone


def pixels(image):
    return friendly.bits(image).tobytes()


def same_pages(first, second):
    """The same pages pixel for pixel (Ghostscript stamps each file with the time it was made)."""
    a, b = conversion.read_fax_frames(str(first)), conversion.read_fax_frames(str(second))
    return len(a) == len(b) and all(x.size == y.size and pixels(x) == pixels(y) for x, y in zip(a, b))


# The transform --------------------------------------------------------------------------------------------------

def test_the_transform_is_deterministic():
    gray, halftone = drawings()
    first, second = friendly.friendly_page(gray, halftone), friendly.friendly_page(gray.copy(), halftone.copy())
    assert first.changed and pixels(first.page) == pixels(second.page)
    assert (first.lightened, first.specks) == (second.lightened, second.specks) == (16, 1)
    assert first.page.info['dpi'] == (204.0, 196.0)


def test_every_pixel_darker_than_the_light_level_stays_as_today_and_light_shading_goes_white():
    gray, halftone = drawings()
    change = friendly.friendly_page(gray, halftone)
    page, today = friendly.bits(change.page), friendly.bits(halftone)
    for y in range(gray.height):
        for x in range(gray.width):
            value = gray.getpixel((x, y))
            if (x, y) == (50, 35):
                assert page.getpixel((x, y)) == 255  # the speck
            elif value < friendly.LIGHT_LEVEL:
                assert page.getpixel((x, y)) == today.getpixel((x, y)), (x, y)
            else:
                assert page.getpixel((x, y)) == 255, (x, y)


def test_specks_are_only_lone_dots_on_light_paper():
    gray = Image.new('L', (20, 12), 255)
    halftone = Image.new('1', (20, 12), 255)
    for x, y in ((5, 5), (0, 3)):  # a lone dot inside the page, and one on its edge
        gray.putpixel((x, y), 20)
        halftone.putpixel((x, y), 0)
    for x, y in ((10, 5), (11, 5), (10, 6), (11, 6)):  # a 2 x 2 speck: a full stop in small print
        gray.putpixel((x, y), 20)
        halftone.putpixel((x, y), 0)
    found, removed = friendly.despeckle(halftone, gray)
    assert removed == 1 and found.getpixel((5, 5)) == 255
    assert found.getpixel((0, 3)) == 0 and all(found.getpixel(point) == 0 for point in ((10, 5), (11, 6)))
    # The dots of a 50% halftone are lone dots too, but the gray around them is dark: they stay.
    gray, halftone = drawings()
    assert friendly.specks(halftone, gray).crop((2, 2, 18, 18)).histogram()[255] == 0


def test_drawings_that_do_not_line_up_change_nothing():
    gray, halftone = drawings()
    gray.putpixel((55, 5), 0)  # pure black in gray, white in today's image
    assert friendly.friendly_page(gray, halftone) is None
    assert friendly.aligned(*drawings())


def test_a_gray_page_narrower_than_the_fax_width_is_padded_with_white_on_the_right():
    gray = Image.new('L', (1687, 4), 200)
    fitted = friendly.fit(gray, (1728, 4))
    assert fitted.size == (1728, 4) and fitted.getpixel((1686, 0)) == 200 and fitted.getpixel((1700, 0)) == 255
    assert friendly.fit(Image.new('L', (1734, 4), 0), (1728, 4)).size == (1728, 4)


def test_a_black_and_white_scan_is_untouched_unless_the_setting_for_your_documents_is_on():
    # A scanner's black-and-white page: its gray drawing is pure black and white, so nothing is light shading.
    scan = Image.new('1', (40, 30), 255)
    for point in ((5, 5), (20, 10), (30, 20)):
        scan.putpixel(point, 0)
    ImageDraw.Draw(scan).rectangle((10, 15, 25, 18), fill=0)
    gray = scan.convert('L')
    drawn = friendly.Request('drawn')
    assert drawn.despeckle is False and friendly.Request('documents').despeckle is True
    kept = friendly.friendly_page(gray, scan, despeckle_page=drawn.despeckle)
    assert not kept.changed and pixels(kept.page) == pixels(scan)
    cleaned = friendly.friendly_page(gray, scan, despeckle_page=True)
    assert cleaned.changed and cleaned.specks == 3 and cleaned.lightened == 0


def test_the_setting_for_your_documents_is_off_by_default():
    assert ConfigurationValues.model_fields['fax_friendly_documents'].default is False
    assert friendly.documents_on(SimpleNamespace()) is False
    assert friendly.documents_on(SimpleNamespace(fax_friendly_documents=True)) is True
    with pytest.raises(ValueError):
        friendly.Request('everything')


# Through Ghostscript (the hook in conversion.pdf_to_tiff) ------------------------------------------------------

def shaded_pdf(path, *, header_gray=0.75):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(path), pagesize=letter)
    for index in range(12):
        y = 700 - index * 22
        if index == 0 or index % 2 == 0:
            pdf.setFillGray(header_gray if index == 0 else 0.9)
            pdf.rect(72, y - 6, 468, 22, fill=1, stroke=0)
        pdf.setFillGray(0)
        pdf.setFont('Helvetica', 10)
        pdf.drawString(78, y, f'Row {index}  Service visit  INV-{1000 + index}  {index * 37}.00')
    pdf.showPage()
    pdf.save()


@needs_gs
def test_a_shaded_table_goes_lighter_and_its_text_is_kept_pixel_for_pixel(tmp_path):
    shaded_pdf(tmp_path / 'table.pdf')
    conversion.pdf_to_tiff(str(tmp_path / 'table.pdf'), str(tmp_path / 'today.tiff'))
    request = friendly.Request('documents')
    conversion.pdf_to_tiff(str(tmp_path / 'table.pdf'), str(tmp_path / 'friendly.tiff'), friendly=request)
    result = request.result
    assert result.pages == result.pages_changed == 1
    assert result.bits_after < result.bits_before / 2 and friendly.seconds_saved(result) >= 10
    today = conversion.read_fax_frames(str(tmp_path / 'today.tiff'))[0]
    after = conversion.read_fax_frames(str(tmp_path / 'friendly.tiff'))[0]
    assert result.bits_before == sum(conversion.frame_bits([today]))
    assert result.bits_after == sum(conversion.frame_bits([after]))
    # Text is black in the gray drawing: every pixel of it is exactly as today.
    import subprocess
    subprocess.run([shutil.which('gs'), '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-sDEVICE=tiffgray', '-r204x196',
                    f'-sOutputFile={tmp_path / "gray.tiff"}', str(tmp_path / 'table.pdf')], check=True)
    with Image.open(tmp_path / 'gray.tiff') as gray:
        gray = friendly.fit(gray.copy(), today.size)
    dark = gray.point(lambda value: 255 if value < friendly.LIGHT_LEVEL else 0).convert('1')
    from PIL import ImageChops
    differ = ImageChops.logical_xor(friendly.bits(today), friendly.bits(after))
    assert ImageChops.logical_and(differ, dark).histogram()[255] == 0
    assert differ.histogram()[255] > 10000
    assert oct((tmp_path / 'friendly.tiff').stat().st_mode & 0o777) == oct(conversion.FAX_IMAGE_MODE)


@needs_gs
def test_pages_faxbot_draws_from_text_come_out_byte_for_byte_as_today(tmp_path):
    from app.batching.image import _separator_pdf, separator_line
    _separator_pdf([separator_line(1, 2, 'Faxbot 7f3a9c21', 3, 'Front Desk')], tmp_path / 'separator.pdf')
    (tmp_path / 'note.txt').write_text('Please call me back about the referral.\n')
    conversion.txt_to_pdf(str(tmp_path / 'note.txt'), str(tmp_path / 'note.pdf'))
    for name in ('separator', 'note'):
        conversion.pdf_to_tiff(str(tmp_path / f'{name}.pdf'), str(tmp_path / f'{name}.today.tiff'))
        request = friendly.Request('drawn')
        conversion.pdf_to_tiff(str(tmp_path / f'{name}.pdf'), str(tmp_path / f'{name}.drawn.tiff'), friendly=request)
        assert request.result is not None and request.result.pages_changed == 0
        assert same_pages(tmp_path / f'{name}.today.tiff', tmp_path / f'{name}.drawn.tiff')
    assert friendly.acceptance_step(JOB, request) is None


@needs_gs
def test_anything_going_wrong_leaves_the_pages_exactly_as_today(tmp_path, monkeypatch):
    shaded_pdf(tmp_path / 'table.pdf')
    conversion.pdf_to_tiff(str(tmp_path / 'table.pdf'), str(tmp_path / 'today.tiff'))

    def broken(*args, **kwargs):
        raise OSError('synthetic failure')
    monkeypatch.setattr(friendly, '_render_gray', broken)
    request = friendly.Request('documents')
    pages, _ = conversion.pdf_to_tiff(str(tmp_path / 'table.pdf'), str(tmp_path / 'after.tiff'), friendly=request)
    assert pages == 1 and request.result is None
    assert same_pages(tmp_path / 'today.tiff', tmp_path / 'after.tiff')
    assert not list(tmp_path.glob('.faxbot-friendly-*'))


# The record (migration 0042) and the Sent detail --------------------------------------------------------------

@pytest.fixture
def installation(database):
    schema.upgrade_schema(database)
    return database


def done(pages=3, changed=2, before=900_000, after=180_000):
    return SimpleNamespace(scope='documents', result=friendly.Result(pages, changed, before, after))


def test_the_acceptance_step_records_once_after_the_step_it_wraps(installation):
    calls = []
    step = friendly.acceptance_step(JOB, done(), lambda connection, now: calls.append(now))
    with installation.begin() as connection:
        step(connection, NOW)
    assert calls == [NOW]
    run = friendly.run_for(installation, JOB)
    assert (run['pages'], run['pages_changed'], run['bits_before'], run['bits_after'], run['seconds_saved'],
            run['attempt_id'], run['scope'], run['created_at']) == (3, 2, 900_000, 180_000, 50, None, 'documents', NOW)
    # Nothing changed: the wrapped step is returned as it is, and nothing is recorded.
    other = lambda connection, now: None  # noqa: E731
    assert friendly.acceptance_step(JOB, done(changed=0), other) is other
    assert friendly.acceptance_step(JOB, None) is None
    # A record that cannot be made never refuses the fax: the step it wraps runs on its own.
    assert friendly.acceptance_step('not a job id!', done(), other) is other


def test_a_send_is_recorded_once_per_attempt_and_the_sent_detail_says_so(installation):
    request = done(pages=5, changed=5)
    first = friendly.record_send(installation, job_id=JOB, attempt_id=ATTEMPT, request=request, now=NOW)
    assert friendly.record_send(installation, job_id=JOB, attempt_id=ATTEMPT, request=request, now=NOW) == first
    view = views.sent_view(installation, JOB)
    assert view['sentences'] == ['Shaded areas on all 5 pages were lightened and specks removed before sending: '
                                 'an estimated 50 seconds less on the line at full fax speed.']
    assert view['lightened'] == {'pages_changed': 5, 'seconds_saved': 50, 'at': '2026-10-07T09:00:00Z'}
    assert views.sent_view(installation, 'c' * 32) is None


@pytest.mark.parametrize('run, sentence', [
    ({'scope': 'documents', 'pages': 1, 'pages_changed': 1, 'seconds_saved': 49},
     'Shaded areas on the page were lightened and specks removed before sending: an estimated 49 seconds less on '
     'the line at full fax speed.'),
    ({'scope': 'documents', 'pages': 4, 'pages_changed': 2, 'seconds_saved': 200},
     'Shaded areas on 2 of the 4 pages were lightened and specks removed before sending: an estimated 3 minutes '
     'less on the line at full fax speed.'),
    ({'scope': 'documents', 'pages': 2, 'pages_changed': 1, 'seconds_saved': 0},
     'Shaded areas on 1 of the 2 pages were lightened and specks removed before sending.'),
    ({'scope': 'drawn', 'pages': 1, 'pages_changed': 1, 'seconds_saved': 1},
     'Shaded areas on the page Faxbot drew were lightened before sending: an estimated 1 second less on the line '
     'at full fax speed.'),
    (None, None),
])
def test_every_sent_sentence(run, sentence):
    assert friendly.sent_sentence(run) == sentence


def test_the_estimate_uses_the_calls_own_speed_when_its_engine_reported_one(installation, monkeypatch):
    friendly.record_send(installation, job_id=JOB, attempt_id=ATTEMPT, request=done(), now=NOW)
    from app import hylafax_records
    negotiation = {'rate_first': 9600, 'rate_lowest': 7200, 'rate_last_page': 7200}
    monkeypatch.setattr(hylafax_records, 'records_for', lambda engine: SimpleNamespace(
        sent_detail=lambda job_id: {'negotiation': negotiation}))
    view = views.sent_view(installation, JOB)
    assert view['sentences'] == ["Shaded areas on 2 of the 3 pages were lightened and specks removed before sending: "
                                 "an estimated 75 seconds less on the line at this call's speed of 9,600 bit/s."]
    assert view['lightened']['seconds_saved'] == 720_000 // 9600
    # The built-in engine reports only its last page's speed.
    negotiation.update(rate_first=None, rate_last_page=14400)
    assert friendly.call_rate(installation, JOB) == 14400
    # Anything that is not a fax speed is ignored, and the estimate falls back to full fax speed.
    negotiation.update(rate_first=True, rate_last_page=100)
    assert friendly.call_rate(installation, JOB) is None
    assert views.sent_view(installation, JOB)['sentences'][0].endswith('at full fax speed.')


def test_the_command_line_section_says_one_thing_at_a_time():
    from app.cli.commands.delivery import show_friendly
    lines = []
    out = SimpleNamespace(line=lines.append)
    show_friendly(out, {'sentence': None, 'action': None})
    show_friendly(out, {'sentence': 'Your last 2 faxes would have taken an estimated 50 seconds less on the line.',
                        'action': 'Turn on "Lighten shaded areas and remove specks on documents you send".'})
    assert lines == ['Faxbot has no recent faxes to check yet.',
                     'Your last 2 faxes would have taken an estimated 50 seconds less on the line.',
                     'Turn on "Lighten shaded areas and remove specks on documents you send". '
                     'Or run: faxbot system settings set fax_friendly_documents=on']


# The recommendation -------------------------------------------------------------------------------------------

def _jobs(database, tmp_path, count, *, pages=1):
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=database)
    identities = [f'{index:032x}' for index in range(1, count + 1)]
    required = {column.name: ('' if isinstance(column.type, sa.String) else 0) for column in jobs.columns
                if not column.nullable and column.server_default is None}
    with database.begin() as connection:
        for index, identity in enumerate(identities):
            connection.execute(jobs.insert().values({
                **required, 'id': identity, 'to_number': PEER, 'status': 'SUCCESS', 'pages': pages,
                'file_name': 'a.pdf', 'created_at': NOW - timedelta(hours=index), 'updated_at': NOW}))
            (tmp_path / f'{identity}.pdf').write_bytes(b'%PDF-1.4 synthetic')
    return identities


def test_the_recommendation_measures_recent_faxes_once_and_says_what_they_would_have_saved(installation, tmp_path):
    identities = _jobs(installation, tmp_path, 12)
    measured = []

    def measure(path):
        measured.append(path.stem)
        shaded = path.stem in identities[:3]
        return friendly.Result(1, 1 if shaded else 0, 870_000 if shaded else 0, 170_000 if shaded else 0)
    friendly._MEASURED.clear()
    view = friendly.recommendation(installation, tmp_path, enabled=False, how_sent='image', now=NOW, measure=measure)
    assert view['recommend'] is True and (view['faxes_checked'], view['faxes_changed']) == (10, 3)
    assert view['seconds_saved'] == 3 * (700_000 // 14400)
    assert view['sentence'] == ('Your last 10 faxes would have taken an estimated 2 minutes less on the line with '
                                'shaded areas lightened and specks removed; 3 of them have shaded areas or specks.')
    assert view['action'] == ('Turn on "Lighten shaded areas and remove specks on documents you send" under '
                              'Providers, In use, Delivery routes. Shaded areas then print white and photographs '
                              'lose their lightest parts.')
    assert len(measured) == 10
    friendly.recommendation(installation, tmp_path, enabled=False, how_sent='image', now=NOW, measure=measure)
    assert len(measured) == 10  # each fax is measured once


def test_the_recommendation_stays_quiet_when_nothing_would_change_or_the_provider_draws_the_pages(installation,
                                                                                                   tmp_path):
    _jobs(installation, tmp_path, 2, pages=1)
    friendly._MEASURED.clear()
    plain = friendly.recommendation(installation, tmp_path, enabled=False, how_sent='pdf_upload', now=NOW,
                                    measure=lambda path: friendly.Result(1, 0, 0, 0))
    assert plain['recommend'] is False
    assert plain['sentence'] == 'Your last 2 faxes have no shaded areas or specks that slow them down.'
    fetched = friendly.recommendation(installation, tmp_path, enabled=False, how_sent='pdf_url', now=NOW)
    assert fetched['recommend'] is False and fetched['sentence'] == (
        'Your fax provider fetches each document from Faxbot and draws its pages itself, so Faxbot cannot lighten '
        'them.')
    nothing = friendly.recommendation(installation, tmp_path / 'empty', enabled=False, how_sent='image', now=NOW)
    assert nothing['recommend'] is False and nothing['sentence'] is None and nothing['faxes_checked'] == 0
    # Faxes older than 30 days are not looked at.
    later = friendly.recommendation(installation, tmp_path, enabled=False, how_sent='image', now=NOW + timedelta(days=60),
                                    measure=lambda path: friendly.Result(1, 1, 900_000, 100_000))
    assert later['faxes_checked'] == 0


@pytest.mark.parametrize('shaded, sentence', [
    (True, 'Your last fax would have taken an estimated 48 seconds less on the line with shaded areas lightened and '
           'specks removed; it has shaded areas or specks.'),
    (False, 'Your last fax has no shaded areas or specks that slow it down.'),
])
def test_one_recent_fax_reads_as_one(installation, tmp_path, shaded, sentence):
    _jobs(installation, tmp_path, 1)
    friendly._MEASURED.clear()
    result = friendly.Result(1, 1, 800_000, 100_000) if shaded else friendly.Result(1, 0, 0, 0)
    view = friendly.recommendation(installation, tmp_path, enabled=False, how_sent='image', now=NOW,
                                   measure=lambda path: result)
    assert view['sentence'] == sentence and view['recommend'] is shaded


def test_a_fax_too_long_for_one_look_is_left_out_rather_than_half_measured(installation, tmp_path):
    _jobs(installation, tmp_path, 1, pages=friendly.PAGE_BUDGET + 1)
    friendly._MEASURED.clear()
    view = friendly.recommendation(installation, tmp_path, enabled=False, how_sent='image', now=NOW,
                                   measure=lambda path: friendly.Result(1, 1, 900_000, 100_000))
    assert view['faxes_checked'] == 0 and view['recommend'] is False


def test_with_the_setting_on_the_recommendation_says_what_it_saved(installation):
    assert friendly.recommendation(installation, '/nowhere', enabled=True, how_sent='image', now=NOW)['sentence'] == (
        'Shaded areas are lightened and specks removed on documents you send; none of your faxes in the last 30 days '
        'had any.')
    friendly.record_send(installation, job_id=JOB, attempt_id=ATTEMPT, request=done(), now=NOW)
    view = friendly.recommendation(installation, '/nowhere', enabled=True, how_sent='image', now=NOW)
    assert view['recommend'] is False and (view['faxes_changed'], view['seconds_saved']) == (1, 50)
    assert view['sentence'] == ('Shaded areas were lightened and specks removed on 1 fax in the last 30 days: an '
                                'estimated 50 seconds less on the line.')


@pytest.mark.parametrize('seconds, text', [(1, '1 second'), (45, '45 seconds'), (89, '89 seconds'),
                                           (90, '2 minutes'), (600, '10 minutes')])
def test_durations_read_as_seconds_then_minutes(seconds, text):
    assert friendly.duration(seconds) == text


# The send-time hook: a cloud provider that takes a PDF gets the lightened pages ----------------------------------

@needs_gs
def test_a_cloud_route_gets_the_lightened_pages_only_with_the_setting_on(installation, tmp_path):
    shaded_pdf(tmp_path / f'{JOB}.pdf')
    configuration = SimpleNamespace(provider_id='sinch', manifest=None, traits={})
    claim = SimpleNamespace(job_id=JOB, attempt_id=ATTEMPT, members=())

    def send(on):
        return sending.prepare(installation, SimpleNamespace(sip_fax_fine=True, fax_friendly_documents=on),
                               configuration, claim, {'to_number': PEER}, tmp_path / f'{JOB}.pdf', None, now=NOW)
    assert send(False) is None
    changed = send(True)
    assert changed.tiff is None and changed.pdf.endswith('.pdf') and changed.sent_pages == 1
    assert conversion.validate_pdf(changed.pdf) == 1
    view = views.sent_view(installation, JOB)
    assert view['layout'] == 'normal' and view['sentences'][0].startswith(
        'Shaded areas on the page were lightened and specks removed before sending: an estimated ')
    # Phaxio fetches the fax's own PDF: nothing Faxbot can change.
    assert sending.prepare(installation, SimpleNamespace(fax_friendly_documents=True),
                           SimpleNamespace(provider_id='phaxio', manifest=None, traits={}), claim,
                           {'to_number': PEER}, tmp_path / f'{JOB}.pdf', None, now=NOW) is None


# Every other way a fax image is made from your documents follows the same setting ---------------------------------

@needs_gs
def test_a_route_that_needs_a_fax_image_later_gets_the_lightened_one_and_it_is_recorded(installation, tmp_path):
    from app.routing.routes import ensure_route_artifact
    trunk = SimpleNamespace(manifest=None, provider_id='sip', traits={})
    for identity, on in ((JOB, False), ('c' * 32, True)):
        shaded_pdf(tmp_path / f'{identity}.pdf')
        revision = SimpleNamespace(values=SimpleNamespace(fax_data_dir=str(tmp_path), fax_friendly_documents=on))
        tiff = ensure_route_artifact(revision, trunk, identity, engine=installation)
        assert tiff == tmp_path / f'{identity}.tiff'
    plain, lightened = (conversion.read_fax_frames(str(tmp_path / f'{identity}.tiff'))[0] for identity in (JOB, 'c' * 32))
    assert sum(conversion.frame_bits([lightened])) < sum(conversion.frame_bits([plain])) / 2
    assert friendly.run_for(installation, JOB) is None
    run = friendly.run_for(installation, 'c' * 32)
    assert run['attempt_id'] is None and run['pages_changed'] == 1 and run['scope'] == 'documents'
    # Without an engine to record in, the image is still lightened and the route still works.
    shaded_pdf(tmp_path / f'{"d" * 32}.pdf')
    revision = SimpleNamespace(values=SimpleNamespace(fax_data_dir=str(tmp_path), fax_friendly_documents=True))
    assert ensure_route_artifact(revision, trunk, 'd' * 32).is_file()


@needs_gs
@pytest.mark.parametrize('on', [False, True])
def test_a_case_packet_or_other_generated_fax_follows_the_setting_for_your_documents(installation, tmp_path, on):
    from datetime import datetime as moment
    from pypdf import PdfReader, PdfWriter
    from app.cases.ledger import index_page
    from app.routing.submit import accept_generated_fax
    # A case packet: Faxbot's own index page, then your document with a shaded table.
    shaded_pdf(tmp_path / 'document.pdf')
    plan = SimpleNamespace(
        referenced=({'title': 'Lab results', 'page_count': 2, 'digest': 'f' * 64, 'accepted_at': moment(2026, 9, 1)},),
        included=(SimpleNamespace(title='Referral', pages=1),))
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(index_page('CASE-1', PEER, plan, 'County Clinic'))).pages:
        writer.add_page(page)
    for page in PdfReader(str(tmp_path / 'document.pdf')).pages:
        writer.add_page(page)
    with open(tmp_path / 'packet.pdf', 'wb') as handle:
        writer.write(handle)
    conversion.pdf_to_tiff(str(tmp_path / 'packet.pdf'), str(tmp_path / 'today.tiff'))
    accepted = []

    def accept(actor, revision, job, *, also=None):
        accepted.append(job)
        if also is not None:
            with installation.begin() as connection:
                also(connection, NOW)
    trunk = SimpleNamespace(manifest=None, provider_id='sip', traits={})
    runtime = SimpleNamespace(manager=SimpleNamespace(store=SimpleNamespace(
        read_profile=lambda profile_id: SimpleNamespace(configuration=trunk))))
    revision = SimpleNamespace(profile_id=lambda kind: 'p' * 32,
                               values=SimpleNamespace(fax_data_dir=str(tmp_path), fax_friendly_documents=on))
    job_id = accept_generated_fax(runtime, SimpleNamespace(outbound=SimpleNamespace(accept=accept)), 'actor', revision,
                                  to_number=PEER, document=(tmp_path / 'packet.pdf').read_bytes(),
                                  file_name='packet.pdf', pages=2)
    assert accepted[0]['tiff_path'].endswith(f'{job_id}.tiff')
    today, sent = (conversion.read_fax_frames(path) for path in (str(tmp_path / 'today.tiff'), accepted[0]['tiff_path']))
    # Faxbot's own index page goes exactly as before either way; only your document's page is lightened.
    assert pixels(sent[0]) == pixels(today[0])
    assert (pixels(sent[1]) == pixels(today[1])) is not on
    run = friendly.run_for(installation, job_id)
    if on:
        assert (run['pages'], run['pages_changed'], run['created_at']) == (2, 1, NOW)
        assert views.sent_view(installation, job_id)['sentences'][0].startswith(
            'Shaded areas on 1 of the 2 pages were lightened and specks removed before sending')
    else:
        assert run is None


# Over HTTP and the command line, with the trunk (Faxbot makes the fax image) --------------------------------------

@needs_gs
def test_an_upload_with_the_setting_on_is_lightened_and_the_sent_detail_and_cli_say_so(monkeypatch, tmp_path):
    from api.tests.test_cli import BOOTSTRAP, Cli, _serve
    shaded_pdf(tmp_path / 'table.pdf')
    for client in _serve(monkeypatch, tmp_path, FAX_BACKEND='sip', FAX_OUTBOUND_BACKEND='sip',
                         FAX_FRIENDLY_DOCUMENTS='true'):
        admin = {'X-API-Key': BOOTSTRAP}
        sent = client.post('/fax', headers=admin, data={'to': PEER},
                           files={'file': ('table.pdf', (tmp_path / 'table.pdf').read_bytes(), 'application/pdf')})
        assert sent.status_code in (200, 202), sent.text
        job_id = sent.json()['id']
        detail = client.get(f'/admin/fax-jobs/{job_id}', headers=admin).json()
        assert detail['page_layout']['sentences'][0].startswith(
            'Shaded areas on the page were lightened and specks removed before sending: an estimated ')
        settings = client.get('/admin/settings', headers=admin).json()
        assert settings['routing']['fax_friendly_documents'] is True
        recommendation = client.get('/routing/recommendations/fax-friendly', headers=admin)
        assert recommendation.status_code == 200, recommendation.text
        assert recommendation.json()['enabled'] is True and recommendation.json()['faxes_changed'] == 1
        cli = Cli(client)
        shown = ' '.join(cli('sent', 'show', job_id).stdout.split())
        assert 'Shaded areas on the page were lightened' in shown
        result = cli('costs', 'recommendations')
        assert result.exit_code == 0, (result.stdout, result.stderr)
        assert 'Shaded areas and specks' in result.stdout and 'were lightened and specks removed on 1 fax' in ' '.join(
            result.stdout.split())


def test_the_recommendation_needs_settings_read_and_the_setting_defaults_off(monkeypatch, tmp_path):
    from api.tests.test_cli import BOOTSTRAP, Cli, _serve
    for client in _serve(monkeypatch, tmp_path):
        admin = {'X-API-Key': BOOTSTRAP}
        assert client.get('/admin/settings', headers=admin).json()['routing']['fax_friendly_documents'] is False
        assert client.get('/routing/recommendations/fax-friendly').status_code in (401, 403)
        view = client.get('/routing/recommendations/fax-friendly', headers=admin).json()
        # This installation sends through Phaxio, which fetches each document itself.
        assert view['recommend'] is False and view['sentence'].startswith('Your fax provider fetches each document')
        result = Cli(client)('system', 'settings', 'set', 'fax_friendly_documents=on')
        assert result.exit_code == 0, (result.stdout, result.stderr)
        assert client.get('/admin/settings', headers=admin).json()['routing']['fax_friendly_documents'] is True

        def unavailable(*args, **kwargs):
            raise sa.exc.OperationalError('SELECT', {}, Exception('synthetic'))
        monkeypatch.setattr(friendly, 'recommendation', unavailable)
        failed = client.get('/routing/recommendations/fax-friendly', headers=admin)
        assert failed.status_code == 503 and failed.json()['detail'] == 'Recommendations are unavailable. Try again.'


# Migration 0042 ------------------------------------------------------------------------------------------------

def _downgrade(engine, revision):
    config = Config(str(schema.API_DIRECTORY / 'alembic.ini'))
    config.set_main_option('script_location', str(schema.API_DIRECTORY / 'alembic'))
    with engine.connect() as connection:
        config.attributes['connection'] = connection
        command.downgrade(config, revision)


def test_fax_friendly_pages_is_head_after_dense_pages():
    assert schema.HEAD == schema_friendly_pages.REVISION == '0042_fax_friendly_pages'
    assert schema.DENSE_PAGES == '0028_dense_pages'


def test_0042_adds_its_table_keeps_every_row_and_downgrades(database):
    at_revision(database, '0028_dense_pages')
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows_before in before.items():
        if name != 'alembic_version':
            assert without_later_access_changes(name, after[name]) == without_later_access_changes(
                name, rows_before), name
    assert after['fax_friendly_pages'] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    friendly.record_send(database, job_id=JOB, attempt_id=ATTEMPT, request=done(), now=NOW)
    table = sa.Table('fax_friendly_pages', sa.MetaData(), autoload_with=database)
    with database.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(table.insert().values(id='x', job_id=JOB, scope='everything', pages=1, pages_changed=1,
                                                 bits_before=1, bits_after=1, created_at=NOW))
    _downgrade(database, '0028_dense_pages')
    assert 'fax_friendly_pages' not in sa.inspect(database).get_table_names()
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == '0028_dense_pages'
    schema.upgrade_schema(database)
    assert 'fax_friendly_pages' in sa.inspect(database).get_table_names()
