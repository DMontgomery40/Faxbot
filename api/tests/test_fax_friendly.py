"""Fax-friendly pages (migration 0042): light shading left out and specks removed before a page goes on the line.

The transform is deterministic; every pixel darker than the light level stays exactly as Faxbot draws it today
(so text stays as readable as it was); a page that is already black and white from a scanner is untouched unless
the setting for your documents says so; pages Faxbot draws itself from text are unchanged; "where it saves time"
by default, decided for each attempt; every sentence. SQLite and PostgreSQL. All synthetic."""
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


def test_a_send_is_recorded_once_per_attempt_and_the_sent_detail_says_so(installation):
    request = done(pages=5, changed=5)
    first = friendly.record_send(installation, job_id=JOB, attempt_id=ATTEMPT, request=request, now=NOW)
    assert friendly.record_send(installation, job_id=JOB, attempt_id=ATTEMPT, request=request, now=NOW) == first
    view = views.sent_view(installation, JOB)
    assert view['sentences'] == ['Shaded areas on all 5 pages were lightened and specks removed before sending: '
                                 'an estimated 50 seconds less on the line at full fax speed.']
    assert view['lightened'] == {'pages_changed': 5, 'seconds_saved': 50, 'at': '2026-10-07T09:00:00Z'}
    assert views.sent_view(installation, 'c' * 32) is None
    with pytest.raises(ValueError):
        friendly.record_send(installation, job_id='not a job!', attempt_id=ATTEMPT, request=request)


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
    show_friendly(out, {'choice': 'never', 'sentence': None, 'action': None})
    show_friendly(out, {'choice': 'where_it_saves', 'sentence': None, 'action': None})
    show_friendly(out, {'choice': 'always', 'sentence': None, 'action': None})
    show_friendly(out, {'choice': 'never', 'sentence': 'Your last 2 faxes would have taken 50 seconds less.',
                        'action': 'Choose "Where it saves time".'})
    assert lines == ['Faxbot has no recent faxes to check yet.',
                     'Nothing to suggest: shaded areas are lightened where it saves time.',
                     'Nothing to suggest: shaded areas are lightened on every document.',
                     'Your last 2 faxes would have taken 50 seconds less.',
                     'Choose "Where it saves time". '
                     'Or run: faxbot system settings set fax_friendly_documents=where_it_saves']


# When: three choices, the recipient's own choice, and each attempt's route ---------------------------------------

def test_where_it_saves_time_is_the_default_and_the_earlier_on_and_off_still_work():
    assert ConfigurationValues.model_fields['fax_friendly_documents'].default == 'where_it_saves'
    for raw, choice in ((True, 'always'), (False, 'never'), ('on', 'always'), ('off', 'never'), ('true', 'always'),
                        ('false', 'never'), ('Always', 'always'), ('where_it_saves', 'where_it_saves')):
        assert ConfigurationValues(FAX_FRIENDLY_DOCUMENTS=raw).fax_friendly_documents == choice
        assert friendly.documents_choice(SimpleNamespace(fax_friendly_documents=raw)) == choice
    with pytest.raises(ValueError):
        ConfigurationValues(FAX_FRIENDLY_DOCUMENTS='sometimes')
    assert friendly.documents_choice(SimpleNamespace()) == 'where_it_saves'
    with pytest.raises(ValueError):
        friendly.Request('everything')


@pytest.mark.parametrize('choice, recipient, by_time, ecm, expected', [
    ('where_it_saves', None, True, True, (True, 'time')),
    ('where_it_saves', None, False, False, (True, 'ecm')),
    ('where_it_saves', None, False, None, (False, None)),
    ('where_it_saves', None, False, True, (False, None)),
    ('always', None, False, True, (True, 'always')),
    ('never', None, True, False, (False, None)),
    ('always', 'never', True, False, (False, None)),
    ('never', 'always', False, True, (True, 'recipient')),
])
def test_each_attempt_is_decided_by_the_setting_the_recipient_the_billing_and_the_machine(choice, recipient, by_time,
                                                                                          ecm, expected):
    assert friendly.decide(choice, recipient, by_time=by_time, ecm=ecm) == expected


def card(provider, *, per_page='0', per_minute='0', monthly=None):
    from app.routing.costs import RateCard, parse_amount
    return RateCard(None, provider, 'outbound', provider, 'USD', parse_amount(per_minute), parse_amount(per_page), 0,
                    60, 0, None, NOW, parse_amount(monthly) if monthly else None)


def test_a_cloud_route_bills_by_time_only_when_its_rate_card_says_so():
    assert friendly.billed_by_time(card('signalwire', per_minute='0.007'), 'signalwire')
    assert not friendly.billed_by_time(card('sinch', per_page='0.03'), 'sinch')
    assert not friendly.billed_by_time(card('humblefax', monthly='19.95'), 'humblefax')
    assert not friendly.billed_by_time(None, 'documo')


def test_a_phone_line_bills_by_time_unless_its_card_has_a_per_page_rate():
    # The Sinch trunk preset ships with no published price: still a phone line billed by the minute.
    assert friendly.billed_by_time(card('sip-sinch'), 'sip')
    assert friendly.billed_by_time(None, 'sip') and friendly.billed_by_time(None, 'freeswitch')
    assert friendly.billed_by_time(card('sip', monthly='20'), 'sip')
    assert not friendly.billed_by_time(card('sip', per_page='0.01'), 'sip')


CARDS = {'sip': card('sip', per_minute='0.007'), 'sinch': card('sinch', per_page='0.03')}
TRUNK = SimpleNamespace(provider_id='sip', manifest=None, traits={'requires_tiff': True})
SINCH = SimpleNamespace(provider_id='sinch', manifest=None, traits={})


@pytest.fixture
def billed(monkeypatch):
    """Rate cards: the trunk bills per minute and Sinch per page."""
    monkeypatch.setattr(sending, '_card', lambda engine, route: CARDS.get(route))


def attempt(database, tmp_path, configuration, *, attempt_id=ATTEMPT, choice='where_it_saves', now=NOW, number=PEER,
            recipient=None):
    """One attempt of the shaded fax JOB by ``configuration``'s route (the fax's own PDF and fax image); ``number``
    is the number dialed, ``recipient`` the one the person chose when an approved alternate is dialed instead."""
    pdf, tiff = tmp_path / f'{JOB}.pdf', tmp_path / f'{JOB}.tiff'
    if not pdf.exists():
        shaded_pdf(pdf)
        conversion.pdf_to_tiff(str(pdf), str(tiff))
    image = configuration.provider_id in ('sip', 'freeswitch')
    return sending.prepare(database, SimpleNamespace(sip_fax_fine=True, fax_friendly_documents=choice),
                           configuration, SimpleNamespace(job_id=JOB, attempt_id=attempt_id, members=()),
                           {'to_number': number, **({'recipient_number': recipient} if recipient else {})}, pdf,
                           tiff if image else None, now=now)


@needs_gs
def test_a_trunk_attempt_is_lightened_and_a_per_page_attempt_is_not(installation, tmp_path, billed):
    assert attempt(installation, tmp_path, SINCH) is None
    assert friendly.run_for(installation, JOB) is None
    before = (tmp_path / f'{JOB}.tiff').read_bytes()
    trunk = attempt(installation, tmp_path, TRUNK, attempt_id='d' * 32)
    assert trunk.pdf is None and trunk.tiff.endswith(f'packed-{JOB}-{"d" * 32}.tiff')
    sent = conversion.read_fax_frames(trunk.tiff)[0]
    own = conversion.read_fax_frames(str(tmp_path / f'{JOB}.tiff'))[0]
    assert sum(conversion.frame_bits([sent])) < sum(conversion.frame_bits([own])) / 2
    run = friendly.run_for(installation, JOB)
    assert run['attempt_id'] == 'd' * 32 and run['pages_changed'] == 1
    # The fax's own image is never changed: the lightened pages are the attempt's own file.
    assert (tmp_path / f'{JOB}.tiff').read_bytes() == before
    assert not list(tmp_path.glob('*.source.tiff'))


@needs_gs
def test_a_trunk_with_no_published_price_is_lightened_by_default(installation, tmp_path, monkeypatch):
    # The Sinch trunk preset's card has no price at all; Phaxio-style per-page cards stay untouched.
    monkeypatch.setattr(sending, '_card', lambda engine, route: card('sip-sinch') if route == 'sip' else None)
    assert attempt(installation, tmp_path, TRUNK) is not None
    assert friendly.run_for(installation, JOB)['attempt_id'] == ATTEMPT
    assert attempt(installation, tmp_path, SINCH, attempt_id='d' * 32) is None  # no card: not billed by time


@needs_gs
def test_a_second_attempt_draws_nothing_again_and_retention_removes_the_kept_pages(installation, tmp_path, billed,
                                                                                    monkeypatch):
    first = attempt(installation, tmp_path, TRUNK)
    kept = sorted(path.name for path in tmp_path.glob(friendly.CACHE + '*'))
    assert kept == [f'{friendly.CACHE}{JOB}.json', f'{friendly.CACHE}{JOB}.tiff']

    def drawn_again(*args, **kwargs):
        raise AssertionError('Ghostscript ran again')
    monkeypatch.setattr(friendly, 'apply', drawn_again)
    monkeypatch.setattr(conversion, 'pdf_to_tiff', drawn_again)
    second = attempt(installation, tmp_path, TRUNK, attempt_id='d' * 32, now=NOW + timedelta(minutes=1))
    assert same_pages(first.tiff, second.tiff)
    assert friendly.run_for(installation, JOB)['attempt_id'] == 'd' * 32
    # The kept pages are attempt files: the retention cleanup removes them with the rest.
    assert sending.cleanup(tmp_path, datetime.utcnow() + timedelta(days=1))
    assert not list(tmp_path.glob(friendly.CACHE + '*'))


@needs_gs
def test_faxes_sent_together_on_the_trunk_are_lightened_and_their_separators_stay(installation, tmp_path):
    from app.batching.image import build_call_image, separator_line
    jobs = [('7' * 32, '8' * 32), ('9' * 32, 'a' * 32)]
    for job_id, _ in jobs:
        shaded_pdf(tmp_path / f'{job_id}.pdf')
        conversion.pdf_to_tiff(str(tmp_path / f'{job_id}.pdf'), str(tmp_path / f'{job_id}.tiff'))
    claim = SimpleNamespace(everyone=tuple(SimpleNamespace(job_id=job, attempt_id=attempt_id)
                                           for job, attempt_id in jobs))
    members = [(job, 1, separator_line(number, 2, 'Faxbot 7f3a9c21', 1, 'Front Desk'))
               for number, (job, _) in enumerate(jobs, start=1)]
    values = SimpleNamespace(fax_friendly_documents='where_it_saves')
    # No rate card for the phone line: still billed by the minute, so the call's faxes are lightened.
    lighten = friendly.call_lightener(installation, values, 'sip', PEER, tmp_path, claim)
    together = conversion.read_fax_frames(str(build_call_image(tmp_path, 'b' * 32, members, lighten=lighten)))
    plain = conversion.read_fax_frames(str(build_call_image(tmp_path, 'c' * 32, members)))
    assert len(together) == len(plain) == 4
    assert pixels(together[0]) == pixels(plain[0]) and pixels(together[2]) == pixels(plain[2])  # separators
    for index in (1, 3):
        assert sum(conversion.frame_bits([together[index]])) < sum(conversion.frame_bits([plain[index]])) / 2
    for job_id, attempt_id in jobs:
        assert friendly.run_for(installation, job_id)['attempt_id'] == attempt_id
    # A recipient's never: the call goes as it is.
    friendly.set_recipient_choice(installation, PEER, 'never')
    assert friendly.call_lightener(installation, values, 'sip', PEER, tmp_path, claim) is None


@needs_gs
def test_a_machine_without_error_correction_is_lightened_on_any_route(installation, tmp_path, billed):
    from app.pages import capability
    capability.records_for(installation).record_observation(
        PEER, source='e' * 32, engine='hylafax', values={'max_length': 'a4', 'ecm': 0, 'fine': 1}, now=NOW)
    changed = attempt(installation, tmp_path, SINCH)
    assert changed.tiff is None and conversion.validate_pdf(changed.pdf) == 1
    assert friendly.run_for(installation, JOB)['attempt_id'] == ATTEMPT


@needs_gs
def test_the_recipients_never_beats_always_and_its_always_beats_never(installation, tmp_path, billed):
    friendly.set_recipient_choice(installation, PEER, 'never', actor='synthetic')
    assert friendly.recipient_choice(installation, PEER) == 'never'
    assert attempt(installation, tmp_path, TRUNK, choice='always') is None
    friendly.set_recipient_choice(installation, PEER, 'always')
    assert attempt(installation, tmp_path, SINCH, choice='never', attempt_id='f' * 32).pdf.endswith('.pdf')
    assert friendly.set_recipient_choice(installation, PEER, None) is None
    with pytest.raises(ValueError):
        friendly.set_recipient_choice(installation, PEER, 'sometimes')
    with pytest.raises(ValueError):
        friendly.set_recipient_choice(installation, 'not a number', 'never')


@needs_gs
def test_never_for_the_chosen_recipient_holds_when_an_approved_toll_free_number_is_dialed(installation, tmp_path,
                                                                                           billed):
    toll_free = '+18005550199'
    friendly.set_recipient_choice(installation, PEER, 'never')
    assert attempt(installation, tmp_path, TRUNK, choice='always', number=toll_free, recipient=PEER) is None
    friendly.set_recipient_choice(installation, PEER, None)
    friendly.set_recipient_choice(installation, toll_free, 'never')
    assert attempt(installation, tmp_path, TRUNK, choice='always', number=toll_free, recipient=PEER) is None
    friendly.set_recipient_choice(installation, toll_free, None)
    assert attempt(installation, tmp_path, TRUNK, choice='always', number=toll_free, recipient=PEER) is not None


@needs_gs
def test_encoded_pages_are_never_lightened(installation, tmp_path, billed):
    """Encoded pages (experimental, codec/send.py) go exactly as made: no lightening either."""
    from app.codec.store import record_send
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=installation)
    with installation.begin() as connection:
        connection.execute(jobs.insert().values(id=JOB, to_number=PEER, status='queued', file_name='a.pdf',
                                                tiff_path='', backend='sip', pages=1, created_at=NOW, updated_at=NOW))
        record_send(connection, installation, JOB, {
            'phone_number': PEER, 'provider_id': 'sip', 'layout': 'grid', 'resolution': 'fine', 'fec': 'medium',
            'pages_original': 1, 'pages_encoded': 1, 'document_sha256': '0' * 64, 'encrypted': 0,
            'format_version': 1}, NOW)
    assert attempt(installation, tmp_path, TRUNK, choice='always') is None
    assert friendly.run_for(installation, JOB) is None and not list(tmp_path.glob('packed-*'))


@needs_gs
@pytest.mark.parametrize('legacy, lightened', [(True, True), (False, False), ('on', True), ('off', False)])
def test_the_earlier_on_and_off_values_still_work_per_attempt(installation, tmp_path, billed, legacy, lightened):
    result = attempt(installation, tmp_path, SINCH, choice=legacy)
    assert (result is not None) is lightened


def _attempts(database, job_id, attempts):
    """fax_jobs and outbound_attempts rows: the attempts in order of their sequence."""
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=database) for name in ('fax_jobs', 'outbound_attempts')}
    with database.begin() as connection:
        for name, values in (('fax_jobs', {'id': job_id, 'to_number': PEER, 'status': 'queued', 'pages': 1,
                                           'file_name': 'a.pdf'}),):
            table = tables[name]
            required = {column.name: ('' if isinstance(column.type, sa.String) else 0) for column in table.columns
                        if not column.nullable and column.server_default is None}
            connection.execute(table.insert().values({**required, **values, 'created_at': NOW, 'updated_at': NOW}))
        for sequence, identity in enumerate(attempts, start=1):
            connection.execute(tables['outbound_attempts'].insert().values(
                id=identity, job_id=job_id, sequence=sequence, phase='prepared', created_at=NOW))


@needs_gs
def test_a_retry_onto_the_trunk_is_lightened_on_that_attempt_only_and_the_sent_detail_follows_it(installation,
                                                                                                 tmp_path, billed):
    first, second, third = '1' * 32, '2' * 32, '3' * 32
    _attempts(installation, JOB, [first])
    assert attempt(installation, tmp_path, SINCH, attempt_id=first) is None
    assert views.sent_view(installation, JOB) is None
    with installation.begin() as connection:
        attempts = sa.Table('outbound_attempts', sa.MetaData(), autoload_with=installation)
        connection.execute(attempts.insert().values(id=second, job_id=JOB, sequence=2, phase='prepared',
                                                    created_at=NOW))
    assert attempt(installation, tmp_path, TRUNK, attempt_id=second) is not None
    assert views.sent_view(installation, JOB)['sentences'][0].startswith('Shaded areas on the page were lightened')
    # A third attempt by Sinch sent the pages as they were: the Sent detail follows it.
    with installation.begin() as connection:
        connection.execute(attempts.insert().values(id=third, job_id=JOB, sequence=3, phase='prepared',
                                                    created_at=NOW))
    assert attempt(installation, tmp_path, SINCH, attempt_id=third) is None
    assert views.sent_view(installation, JOB) is None


@needs_gs
def test_a_case_packet_is_lightened_on_the_trunk_and_its_index_page_stays_as_it_is(installation, tmp_path, billed):
    from datetime import datetime as moment
    from pypdf import PdfReader, PdfWriter
    from app.cases.ledger import index_page
    shaded_pdf(tmp_path / 'document.pdf')
    # The case ledger's packet plan (cases/ledger.py): each entry with its pages, version and source.
    plan = SimpleNamespace(
        referenced=({'title': 'Lab results', 'pages': 2, 'version': '', 'source': '', 'digest': 'f' * 64,
                     'accepted_at': moment(2026, 9, 1)},),
        unique=(SimpleNamespace(title='Referral', pages=1, version='', source=''),))
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(index_page('CASE-1', PEER, plan, 'County Clinic'))).pages:
        writer.add_page(page)
    for page in PdfReader(str(tmp_path / 'document.pdf')).pages:
        writer.add_page(page)
    with open(tmp_path / f'{JOB}.pdf', 'wb') as handle:
        writer.write(handle)
    conversion.pdf_to_tiff(str(tmp_path / f'{JOB}.pdf'), str(tmp_path / f'{JOB}.tiff'))
    sent = conversion.read_fax_frames(attempt(installation, tmp_path, TRUNK).tiff)
    own = conversion.read_fax_frames(str(tmp_path / f'{JOB}.tiff'))
    assert pixels(sent[0]) == pixels(own[0]) and pixels(sent[1]) != pixels(own[1])
    assert views.sent_view(installation, JOB)['sentences'][0].startswith(
        'Shaded areas on 1 of the 2 pages were lightened and specks removed before sending')


# The recommendation: only while the setting is Never ---------------------------------------------------------------

def _jobs(database, tmp_path, count, *, pages=1, backend='sip'):
    jobs = sa.Table('fax_jobs', sa.MetaData(), autoload_with=database)
    identities = [f'{index:032x}' for index in range(1, count + 1)]
    required = {column.name: ('' if isinstance(column.type, sa.String) else 0) for column in jobs.columns
                if not column.nullable and column.server_default is None}
    with database.begin() as connection:
        for index, identity in enumerate(identities):
            connection.execute(jobs.insert().values({
                **required, 'id': identity, 'to_number': PEER, 'status': 'SUCCESS', 'pages': pages,
                'file_name': 'a.pdf', 'backend': backend, 'created_at': NOW - timedelta(hours=index),
                'updated_at': NOW}))
            (tmp_path / f'{identity}.pdf').write_bytes(b'%PDF-1.4 synthetic')
    return identities


def test_with_the_setting_at_never_it_says_what_recent_faxes_billed_by_time_would_have_saved(installation, tmp_path):
    identities = _jobs(installation, tmp_path, 12)
    measured = []

    def measure(path):
        measured.append(path.stem)
        shaded = path.stem in identities[:3]
        return friendly.Result(1, 1 if shaded else 0, 870_000 if shaded else 0, 170_000 if shaded else 0)
    friendly._MEASURED.clear()
    view = friendly.recommendation(installation, tmp_path, choice='never', how_sent='image', now=NOW,
                                   measure=measure, saves=lambda route, number: route == 'sip')
    assert view['recommend'] is True and (view['faxes_checked'], view['faxes_changed']) == (10, 3)
    assert view['seconds_saved'] == 3 * (700_000 // 14400)
    assert view['sentence'] == ('Your last 10 faxes would have taken an estimated 2 minutes less on the line with '
                                'shaded areas lightened and specks removed on calls billed by time; 3 of them have '
                                'shaded areas or specks.')
    assert view['action'] == ('Choose "Where it saves time" for "Lighten shaded areas and remove specks on documents '
                              'you send" under Providers, In use, Delivery routes. Shaded areas then print white on '
                              'those calls, and photographs lose their lightest parts.')
    assert len(measured) == 10
    friendly.recommendation(installation, tmp_path, choice='never', how_sent='image', now=NOW, measure=measure,
                            saves=lambda route, number: True)
    assert len(measured) == 10  # each fax is measured once
    # Faxes by a provider that charges per page would have saved nothing.
    per_page = friendly.recommendation(installation, tmp_path, choice='never', how_sent='pdf_upload', now=NOW,
                                       measure=measure, saves=lambda route, number: False)
    assert per_page['recommend'] is False and per_page['sentence'] == (
        'Your last 10 faxes went by providers that charge per page, to machines with error correction, so lightening '
        'shaded areas would have saved nothing.')


def test_any_other_choice_has_nothing_to_recommend(installation, tmp_path):
    _jobs(installation, tmp_path, 2)
    for choice in ('where_it_saves', 'always'):
        view = friendly.recommendation(installation, tmp_path, choice=choice, how_sent='image', now=NOW,
                                       measure=lambda path: friendly.Result(1, 1, 900_000, 100_000))
        assert view['sentence'] is None and view['recommend'] is False and view['faxes_checked'] == 0


def test_where_it_saves_follows_the_rate_card_and_the_machine(installation):
    from app.pages import capability
    from app.routing import store
    saves = friendly.where_it_saves(installation)
    original = store.RouteStore.card_for
    try:
        store.RouteStore.card_for = lambda self, route: CARDS.get(route)
        assert saves('sip', PEER) is True and saves('sinch', PEER) is False
        capability.records_for(installation).record_observation(
            PEER, source='e' * 32, engine='hylafax', values={'max_length': 'a4', 'ecm': 0}, now=NOW)
        assert friendly.where_it_saves(installation)('sinch', PEER) is True
    finally:
        store.RouteStore.card_for = original


def test_the_recommendation_stays_quiet_when_nothing_would_change_or_the_provider_draws_the_pages(installation,
                                                                                                   tmp_path):
    _jobs(installation, tmp_path, 2, pages=1)
    friendly._MEASURED.clear()
    plain = friendly.recommendation(installation, tmp_path, choice='never', how_sent='image', now=NOW,
                                    measure=lambda path: friendly.Result(1, 0, 0, 0), saves=lambda *_: True)
    assert plain['recommend'] is False
    assert plain['sentence'] == 'Your last 2 faxes have no shaded areas or specks that slow them down.'
    fetched = friendly.recommendation(installation, tmp_path, choice='never', how_sent='pdf_url', now=NOW)
    assert fetched['recommend'] is False and fetched['sentence'] == (
        'Your fax provider fetches each document from Faxbot and draws its pages itself, so Faxbot cannot lighten '
        'them.')
    nothing = friendly.recommendation(installation, tmp_path / 'empty', choice='never', how_sent='image', now=NOW,
                                      saves=lambda *_: True)
    assert nothing['recommend'] is False and nothing['sentence'] is None and nothing['faxes_checked'] == 0
    later = friendly.recommendation(installation, tmp_path, choice='never', how_sent='image',
                                    now=NOW + timedelta(days=60), saves=lambda *_: True,
                                    measure=lambda path: friendly.Result(1, 1, 900_000, 100_000))
    assert later['faxes_checked'] == 0


@pytest.mark.parametrize('shaded, sentence', [
    (True, 'Your last fax would have taken an estimated 48 seconds less on the line with shaded areas lightened and '
           'specks removed on calls billed by time; it has shaded areas or specks.'),
    (False, 'Your last fax has no shaded areas or specks that slow it down.'),
])
def test_one_recent_fax_reads_as_one(installation, tmp_path, shaded, sentence):
    _jobs(installation, tmp_path, 1)
    friendly._MEASURED.clear()
    result = friendly.Result(1, 1, 800_000, 100_000) if shaded else friendly.Result(1, 0, 0, 0)
    view = friendly.recommendation(installation, tmp_path, choice='never', how_sent='image', now=NOW,
                                   measure=lambda path: result, saves=lambda *_: True)
    assert view['sentence'] == sentence and view['recommend'] is shaded


def test_one_recent_fax_by_a_per_page_provider_reads_as_one(installation, tmp_path):
    _jobs(installation, tmp_path, 1, backend='sinch')
    friendly._MEASURED.clear()
    view = friendly.recommendation(installation, tmp_path, choice='never', how_sent='pdf_upload', now=NOW,
                                   measure=lambda path: friendly.Result(1, 1, 800_000, 100_000),
                                   saves=lambda *_: False)
    assert view['sentence'] == ('Your last fax went by a provider that charges per page, to a machine with error '
                                'correction, so lightening shaded areas would have saved nothing.')


def test_a_fax_too_long_for_one_look_is_left_out_rather_than_half_measured(installation, tmp_path):
    _jobs(installation, tmp_path, 1, pages=friendly.PAGE_BUDGET + 1)
    friendly._MEASURED.clear()
    view = friendly.recommendation(installation, tmp_path, choice='never', how_sent='image', now=NOW,
                                   measure=lambda path: friendly.Result(1, 1, 900_000, 100_000),
                                   saves=lambda *_: True)
    assert view['faxes_checked'] == 0 and view['recommend'] is False


@pytest.mark.parametrize('seconds, text', [(1, '1 second'), (45, '45 seconds'), (89, '89 seconds'),
                                           (90, '2 minutes'), (600, '10 minutes')])
def test_durations_read_as_seconds_then_minutes(seconds, text):
    assert friendly.duration(seconds) == text


# Over HTTP and the command line -----------------------------------------------------------------------------------

def test_the_settings_the_recipient_choice_and_the_recommendation_over_http_and_the_command_line(monkeypatch,
                                                                                                  tmp_path):
    from api.tests.test_cli import BOOTSTRAP, Cli, _serve
    for client in _serve(monkeypatch, tmp_path):
        admin = {'X-API-Key': BOOTSTRAP}
        routing = lambda: client.get('/admin/settings', headers=admin).json()['routing']  # noqa: E731
        assert routing()['fax_friendly_documents'] == 'where_it_saves'
        cli = Cli(client)
        for typed, stored in (('never', 'never'), ('on', 'always'), ('off', 'never')):
            result = cli('system', 'settings', 'set', f'fax_friendly_documents={typed}')
            assert result.exit_code == 0, (result.stdout, result.stderr)
            assert routing()['fax_friendly_documents'] == stored
        # A recipient's own choice, in Recipients, Details and on the command line.
        url = '/routing/destinations/' + PEER + '/pages'
        view = client.get(url, headers=admin).json()
        assert (view['shading'], view['shading_default']) == (None, 'never')
        assert client.put(url, headers=admin, json={'shading': 'always'}).json()['shading'] == 'always'
        assert client.put(url, headers=admin, json={'shading': 'sometimes'}).status_code == 422
        result = cli('recipients', 'set', PEER, '--shading', 'off')
        assert result.exit_code == 0, (result.stdout, result.stderr)
        assert 'Lighten shaded areas never' in ' '.join(cli('recipients', 'show', PEER).stdout.split())
        assert cli('recipients', 'set', PEER, '--shading', 'maybe').exit_code != 0
        assert cli('recipients', 'set', PEER, '--shading', 'default').exit_code == 0
        shown = ' '.join(cli('recipients', 'show', PEER).stdout.split())
        assert 'Lighten shaded areas as set for all faxes (never)' in shown
        # The recommendation needs settings permission; this installation's Phaxio fetches each document itself.
        assert client.get('/routing/recommendations/fax-friendly').status_code in (401, 403)
        view = client.get('/routing/recommendations/fax-friendly', headers=admin).json()
        assert view['choice'] == 'never' and view['sentence'].startswith('Your fax provider fetches each document')
        result = cli('costs', 'recommendations')
        assert result.exit_code == 0 and 'Shaded areas and specks' in result.stdout

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


# The migration chain follows merge order: 0042 comes after 0041 (intake connectors).
PRIOR = '0041_intake_connectors'


def test_fax_friendly_pages_follow_intake_connectors():
    assert schema_friendly_pages.REVISION == '0042_fax_friendly_pages'
    assert schema.INTAKE_SOURCES == PRIOR


def test_0042_adds_its_tables_keeps_every_row_and_downgrades(database):
    at_revision(database, PRIOR)
    before = snapshot(database)
    schema.upgrade_schema(database)
    after = snapshot(database)
    assert after['alembic_version'] == [{'version_num': schema.HEAD}]
    for name, rows_before in before.items():
        if name != 'alembic_version':
            # Each earlier table keeps every row on its original columns; later revisions may add columns.
            rows, kept = (without_later_access_changes(name, rows_before),
                          without_later_access_changes(name, after[name]))
            assert ([{key: row[key] for key in rows[0]} for row in kept] == rows) if rows else kept == [], name
    assert after['fax_friendly_pages'] == [] and after['fax_friendly_recipients'] == []
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == schema.HEAD
    friendly.record_send(database, job_id=JOB, attempt_id=ATTEMPT, request=done(), now=NOW)
    friendly.set_recipient_choice(database, PEER, 'never', now=NOW)
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=database) for name in schema_friendly_pages.ORDER}
    with database.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(tables['fax_friendly_pages'].insert().values(
            id='x', job_id=JOB, scope='everything', pages=1, pages_changed=1, bits_before=1, bits_after=1,
            created_at=NOW))
    with database.begin() as connection, pytest.raises(sa.exc.IntegrityError):
        connection.execute(tables['fax_friendly_recipients'].insert().values(
            id='y', number=PEER, shading='sometimes', updated_at=NOW))
    _downgrade(database, PRIOR)
    assert not set(schema_friendly_pages.ORDER) & set(sa.inspect(database).get_table_names())
    with database.connect() as connection:
        assert schema.validate_schema(connection, require_version=True) == PRIOR
    schema.upgrade_schema(database)
    assert set(schema_friendly_pages.ORDER) <= set(sa.inspect(database).get_table_names())
