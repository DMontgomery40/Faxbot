"""The header notice (N6): one notice line on every page, and a cover sheet sent as that notice instead.

The HTTP tests run the real POST /fax path of an installation with a SIP trunk and sending turned off, so
Faxbot prepares both the PDF a fax service would get and the fax image its own engines send, and nothing is
dialed. Documents are synthetic; numbers use the 555 exchange.
"""
import base64
import io
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from app import ami, header_notice
from app.routing import reply_number
from api.tests.test_reply_number import DID_A, DID_B, client, values  # noqa: F401 - fixture

NOTICE = 'Confidential: for the addressee only. If this reached you in error, call 303-555-0101.'
TO = '+13035550150'


def document(*titles, size=(612, 792)):
    """A PDF with one page per title, each title printed large near the top."""
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=size)
    for title in titles:
        pdf.setFont('Helvetica-Bold', 28)
        pdf.drawString(72, size[1] - 120, title)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def texts(data):
    return [page.extract_text() or '' for page in PdfReader(io.BytesIO(data)).pages]


# -- the notice text --------------------------------------------------------------------------------------------

def test_the_notice_is_one_tidy_printable_line_that_fits_an_a4_page():
    assert header_notice.check_text('  Confidential:\tfor   the addressee  ') == 'Confidential: for the addressee'
    assert header_notice.check_text('') is None and header_notice.check_text(None) is None
    for wrong in ('x' * 121, 'Line one\x07bell'):
        with pytest.raises(header_notice.NoticeRefused):
            header_notice.check_text(wrong)
    with pytest.raises(header_notice.NoticeRefused, match='A4'):
        header_notice.check_text('W' * 120)
    assert header_notice.check_text(NOTICE) == NOTICE


# -- drawing ------------------------------------------------------------------------------------------------------

def test_the_cover_is_left_out_and_every_page_carries_the_notice_without_covering_anything():
    rendered, count = header_notice.render(document('Cover sheet', 'Referral letter', 'Lab results'), NOTICE,
                                           drop_first=True)
    assert count == 2
    pages = texts(rendered)
    assert len(pages) == 2
    assert 'Cover sheet' not in ' '.join(pages)
    assert 'Referral letter' in pages[0] and 'Lab results' in pages[1]
    assert all('Confidential: for the addressee only.' in page for page in pages)
    reader = PdfReader(io.BytesIO(rendered))
    assert all((float(page.mediabox.width), float(page.mediabox.height)) == (612.0, 792.0) for page in reader.pages)
    kept, kept_count = header_notice.render(document('Only page'), NOTICE)
    assert kept_count == 1 and 'Only page' in texts(kept)[0]
    with pytest.raises(header_notice.NoticeRefused):
        header_notice.render(document('Only page'), NOTICE, drop_first=True)


@pytest.mark.parametrize('width', [612.0, 595.0])  # Letter and A4
def test_the_longest_notice_is_never_drawn_smaller_than_the_readable_size(width):
    text = ('Confidential: for the addressee only; if received in error, call us at 303-555-0101 and destroy '
            'it. ') * 2
    longest = header_notice.check_text(text[:header_notice.MAX_NOTICE])
    assert longest is not None and len(longest) >= header_notice.MAX_NOTICE - 1
    font, size, lines = header_notice.band_layout(width, longest)
    assert size >= header_notice.MIN_POINTS and lines == [longest]
    # The continuation band's own note size for a short notice.
    assert header_notice.band_layout(width, 'Confidential.')[1] == 9.0


def test_a_narrow_page_takes_two_lines_and_a_page_too_narrow_is_refused():
    longest = header_notice.check_text(('Confidential: for the addressee only; if received in error, call us at '
                                        '303-555-0101 and destroy it. ' * 2)[:header_notice.MAX_NOTICE])
    font, size, lines = header_notice.band_layout(300.0, longest)  # a page about 4 inches wide
    assert size == header_notice.MIN_POINTS and len(lines) == 2 and ' '.join(lines) == longest
    with pytest.raises(header_notice.NoticeRefused, match='too narrow'):
        header_notice.band_layout(150.0, NOTICE)


def _rows(image, top, bottom):
    """How many black pixels lie in rows ``top`` to ``bottom`` (fax rows, 196 to the inch)."""
    strip = image.convert('1').crop((0, top, image.width, bottom))
    return strip.histogram()[0]


@pytest.mark.parametrize('size', [(612, 792), (595, 842)])  # Letter and A4
def test_on_the_fax_image_the_engine_strip_stays_clear_and_the_notice_sits_under_it(tmp_path, size):
    from app.conversion import pdf_to_tiff
    from app.routing.continuation import BAND_POINTS, CLEAR_POINTS
    rendered, _ = header_notice.render(document('Referral letter', size=size), NOTICE)
    pdf = tmp_path / 'noticed.pdf'
    pdf.write_bytes(rendered)
    pdf_to_tiff(str(pdf), str(tmp_path / 'noticed.tiff'))
    with Image.open(tmp_path / 'noticed.tiff') as image:
        rows_per_point = image.info.get('dpi', (204, 196))[1] / 72
        clear = int(CLEAR_POINTS * rows_per_point) - 2
        band = int(BAND_POINTS * rows_per_point)
        # The top strip, where the SSL Fax engine images its header line, has nothing of ours (the built-in engine
        # adds its own rows above the page instead).
        assert _rows(image, 0, clear) == 0
        # The notice is in the band below it, and the page's own content starts under the band.
        assert _rows(image, clear, band) > 200
        assert _rows(image, band, image.height) > 200


def test_both_engines_header_lines_stay_as_they_were():
    settings = values(FAX_REPLY_NUMBER=DID_A)
    fields = ami.originate_fields_for(settings, 'job1', TO, '/faxdata/job1.tiff',
                                      choice=reply_number.Choice(DID_A, 'organization', ''))
    variables = dict(item.split('=', 1) for item in fields['Variable'].split(',') if '=' in item)
    # The built-in engine's header line keeps the header text alone; the notice is drawn on the page itself.
    assert base64.b64decode(variables['FAXHEADER64']).decode() == 'Example Clinic'
    assert reply_number.tagline('Example Clinic') == '%d %b %Y %H:%M|Example Clinic|%%l|Page %%P of %%T'


# -- the real acceptance path ---------------------------------------------------------------------------------------

def _send(test_client, data, **fields):
    from api.tests.test_access_management_http import B
    return test_client.post('/fax', headers=B, data={'to': TO, **fields},
                            files={'file': ('referral.pdf', data, 'application/pdf')})


def _data_dir():
    import os
    return Path(os.environ['FAX_DATA_DIR'])


def _set(test_client, path, body):
    from api.tests.test_access_management_http import B
    response = test_client.put(path, headers=B, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _view(test_client, job):
    from api.tests.test_access_management_http import B
    response = test_client.get(f'/header-notice/faxes/{job}', headers=B)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_cover_sent_as_the_notice_is_left_out_of_the_pdf_the_image_the_rules_and_the_price(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    saved = _set(client, '/header-notice', {'notice': NOTICE})
    assert saved['organization']['notice'] == NOTICE
    assert saved['sentence'] == 'Saved. Every page of your faxes carries this notice from now on.'
    sent = _send(client, document('Cover sheet', 'Referral letter', 'Lab results'), cover_in_header='true')
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    assert sent.json()['pages'] == 2
    root = _data_dir()
    assert len(PdfReader(str(root / f'{job}.pdf')).pages) == 2
    assert 'Cover sheet' not in ' '.join(texts((root / f'{job}.pdf').read_bytes()))
    with Image.open(root / f'{job}.tiff') as image:
        assert image.n_frames == 2
    # The sender's whole document is kept beside the fax.
    assert len(PdfReader(str(root / f'{job}.upload.pdf')).pages) == 3
    view = _view(client, job)
    assert (view['cover'], view['original_pages'], view['sent_pages']) == ('dropped', 3, 2)
    assert view['sentence'] == ('The first page was a cover sheet. Its notice went in the header of every page '
                                'instead, so it was not sent: 2 pages went instead of 3.')
    # The sending rules, quotes and caps saw the pages that are sent.
    from app.main import app
    from app.routing import envelope
    engine = app.state.configuration_runtime.manager.store.engine
    assert envelope.load(engine, job).facts.pages == 2
    assert client.get(f'/fax/{job}', headers=B).json()['pages'] == 2


def test_a_recipient_that_needs_a_cover_keeps_it_and_sent_details_say_why(client):  # noqa: F811
    _set(client, '/header-notice', {'notice': NOTICE})
    flagged = _set(client, f'/header-notice/recipients/{TO}', {'needs_cover': True})
    assert flagged['sentence'] == 'Saved. Faxes to this number always keep their cover sheet.'
    sent = _send(client, document('Cover sheet', 'Referral letter'), cover_in_header='true')
    assert sent.status_code == 202, sent.text
    assert sent.json()['pages'] == 2
    view = _view(client, sent.json()['id'])
    assert view['cover'] == 'kept' and view['sentence'].startswith('You marked the first page as a cover sheet, but')


def test_the_cover_choice_needs_a_notice_and_a_second_page(client):  # noqa: F811
    refused = _send(client, document('Cover sheet', 'Referral letter'), cover_in_header='true')
    assert refused.status_code == 400
    assert refused.json()['detail'].startswith('There is no header notice to carry the cover sheet’s notice.')
    _set(client, '/header-notice', {'notice': NOTICE})
    single = _send(client, document('Cover sheet'), cover_in_header='true')
    assert single.status_code == 400 and 'only one page' in single.json()['detail']
    # Nothing of a refused fax stays behind.
    assert not list(_data_dir().glob('*.upload.pdf'))


def test_a_notice_alone_is_printed_on_every_page_and_a_mailbox_notice_wins(client):  # noqa: F811
    from api.tests.test_work_http import mailbox
    _set(client, '/header-notice', {'notice': NOTICE})
    plain = _send(client, document('Referral letter', 'Lab results'))
    assert plain.status_code == 202 and plain.json()['pages'] == 2
    view = _view(client, plain.json()['id'])
    assert (view['cover'], view['whose'], view['sentence']) == ('none', 'organization',
                                                                'Every page carried your header notice.')
    billing = mailbox(client, 'Billing', DID_B)
    _set(client, f"/header-notice/mailboxes/{billing['id']}", {'notice': 'Billing office: confidential.'})
    boxed = _send(client, document('Invoice'), mailbox=billing['id'])
    assert boxed.status_code == 202, boxed.text
    view = _view(client, boxed.json()['id'])
    assert (view['notice'], view['whose']) == ('Billing office: confidential.', 'mailbox')
    # Without a notice anywhere, nothing is printed and nothing is recorded.
    _set(client, '/header-notice', {'notice': ''})
    bare = _send(client, document('Referral letter'))
    assert _view(client, bare.json()['id'])['sentence'] is None


def test_a_replay_with_another_cover_choice_is_not_the_same_request(client):  # noqa: F811
    from api.tests.test_access_management_http import B
    _set(client, '/header-notice', {'notice': NOTICE})
    data = document('Cover sheet', 'Referral letter')
    first = client.post('/fax', headers={**B, 'Idempotency-Key': 'synthetic-cover-1'}, data={'to': TO},
                        files={'file': ('a.pdf', data, 'application/pdf')})
    assert first.status_code == 202
    other = client.post('/fax', headers={**B, 'Idempotency-Key': 'synthetic-cover-1'},
                        data={'to': TO, 'cover_in_header': 'true'}, files={'file': ('a.pdf', data, 'application/pdf')})
    assert other.status_code == 409


def test_a_fax_sent_as_encoded_pages_says_the_notice_is_in_the_decoded_document(client):  # noqa: F811
    from datetime import datetime
    import uuid
    import sqlalchemy as sa
    _set(client, '/header-notice', {'notice': NOTICE})
    job = _send(client, document('Referral letter', 'Lab results')).json()['id']
    assert _view(client, job)['encoded'] is None
    from app.main import app
    engine = app.state.configuration_runtime.manager.store.engine
    changes = sa.Table('fax_page_changes', sa.MetaData(), autoload_with=engine)
    attempts = sa.table('outbound_attempts', sa.column('id'), sa.column('job_id'), sa.column('sequence'),
                        sa.column('phase'), sa.column('created_at'))
    attempt, now = uuid.uuid4().hex, datetime(2026, 10, 10, 9, 0)
    required = {column.name for column in changes.columns if not column.nullable}
    row = {'id': uuid.uuid4().hex, 'job_id': job, 'attempt_id': attempt, 'original_pages': 2, 'sent_pages': 1,
           'layout': 'codec', 'reason': 'Encoded pages.', 'created_at': now}
    for name in required - set(row):
        row[name] = 0
    with engine.begin() as connection:
        connection.execute(attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='completed',
                                                    created_at=now))
        connection.execute(changes.insert().values(**row))
    assert _view(client, job)['encoded'] == header_notice.ENCODED_SENTENCE


def test_who_may_read_and_change_notices(client):  # noqa: F811
    assert client.get('/header-notice').status_code in (401, 403)
    assert client.put('/header-notice', json={'notice': NOTICE}).status_code in (401, 403)
    assert client.put(f'/header-notice/recipients/{TO}', json={'needs_cover': True}).status_code in (401, 403)
    bad = client.put('/header-notice/mailboxes/nowhere', headers=__import__(
        'api.tests.test_access_management_http', fromlist=['B']).B, json={'notice': NOTICE})
    assert bad.status_code == 400


def test_the_command_line_sets_the_notice_and_sends_a_cover_as_it(client, tmp_path):  # noqa: F811
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    from api.tests.test_access_management_http import BOOTSTRAP

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    saved = run('numbers', 'reply', 'notice', 'set', NOTICE)
    assert saved.exit_code == 0, saved.stdout
    assert 'Saved. Every page of your faxes carries this notice from now on.' in saved.stdout
    shown = run('numbers', 'reply', 'notice', 'show')
    assert f'Every page carries: {NOTICE}' in ' '.join(shown.stdout.split())
    flagged = run('recipients', 'set', TO, '--needs-cover')
    assert flagged.exit_code == 0, flagged.stdout
    assert 'Faxes to this number always keep their cover sheet.' in flagged.stdout
    cleared = run('recipients', 'set', TO, '--no-cover-needed')
    assert cleared.exit_code == 0, (cleared.stdout, cleared.stderr)
    assert 'Senders may send a cover sheet’s notice in the header to this number.' in cleared.stdout
    pdf = tmp_path / 'referral.pdf'
    pdf.write_bytes(document('Cover sheet', 'Referral letter', 'Lab results'))
    sent = run('--json', 'send', TO, str(pdf), '--cover-in-header')
    assert sent.exit_code == 0, (sent.stdout, sent.stderr)
    import json
    job = json.loads(sent.stdout)
    assert job['pages'] == 2, _view(client, job['id'])
