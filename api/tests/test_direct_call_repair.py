"""A fax call to an enrolled partner breaks part way: only the missing pages go directly, and the partner files one fax.

Installation A's fax failed after a call that confirmed some pages (``partly_sent``),
with the call record a trunk call leaves. Installation B (the running application)
holds that call's received fax: a synthetic multi-page G4 TIFF made from the same
document, as the engine would hand it over.
"""
from datetime import datetime, timedelta
import io
import subprocess
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app import main
from api.app.direct.repair import CallRepair, RepairStore, held_pages
from api.app.outbound_worker import OutboundWorker
from api.tests.test_direct_delivery import (  # noqa: F401 - fixtures
    A_NUMBER, ADMIN, B_NUMBER, accept, b_client, b_items, pair, stored_document, transport)


def ten_pages():
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    for page in range(1, 11):
        pdf.setFont('Helvetica-Bold', 40)
        pdf.drawString(72, 700, f'Referral page {page} of 10')
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def call_image(pdf, folder, pages, *, damaged=None):
    """The received fax of the broken call: the first ``pages`` pages, as the engine stored them."""
    from PIL import Image
    source, made = folder / f'{uuid4().hex}.pdf', folder / f'{uuid4().hex}.tif'
    source.write_bytes(pdf)
    subprocess.run(['gs', '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-sDEVICE=tiffg4', '-r204x196',
                    f'-dLastPage={pages}', f'-sOutputFile={made}', str(source)], check=True)
    if damaged:
        from PIL.TiffImagePlugin import ImageFileDirectory_v2
        frames = []
        with Image.open(made) as image:
            for index in range(image.n_frames):
                image.seek(index)
                frames.append(image.copy())
        directories = []
        for index in range(len(frames)):
            directory = ImageFileDirectory_v2()
            directory[326] = 12 if index + 1 == damaged else 0  # BadFaxLines
            directory.tagtype[326] = 4
            directories.append(directory)
        # One page at a time, so each keeps its own bad-line count.
        from PIL.TiffImagePlugin import AppendingTiffWriter
        with AppendingTiffWriter(str(made), new=True) as writer:
            for frame, directory in zip(frames, directories):
                frame.save(writer, 'TIFF', compression='group4', dpi=(204, 196), tiffinfo=directory)
                writer.newFrame()
        with Image.open(made) as check:
            stored = []
            for index in range(check.n_frames):
                check.seek(index)
                stored.append(check.tag_v2.get(326))
        if stored[damaged - 1] in (None, 0):
            pytest.skip('This Pillow cannot write a per-page bad-line count.')
    return made


class Unreachable:
    """A's transport while B cannot be reached: the first send goes by fax."""

    def __init__(self, inner):
        self.inner, self.down = inner, True

    async def request(self, method, url, **kwargs):
        from api.app.direct.service import PartnerUnreachable
        if self.down:
            raise PartnerUnreachable()
        return await self.inner.request(method, url, **kwargs)


def b_engine():
    return main.app.state.configuration_runtime.manager.store.engine


async def broken_call(pair, tmp_path, *, held, damaged=None):
    """A fax of ten pages to B that went by call and broke after ``held`` confirmed pages."""
    a = pair['a']
    a.store.note_capabilities(pair['b_on_a']['id'], fax_images=True, peer_calls=False, said_at=datetime.utcnow())
    reach = Unreachable(pair['to_b'])
    a.http = reach
    document = ten_pages()
    job = accept(pair, document)
    with pair['configuration'].engine.begin() as connection:
        connection.execute(sa.text('UPDATE fax_jobs SET pages = 10 WHERE id = :id'), {'id': job})
    routed, conventional = transport(pair)
    await OutboundWorker(pair['delivery'], routed).step()
    attempt = pair['delivery'].get(job)['attempt_id']
    assert conventional.submissions == 1
    _, profile = pair['delivery'].attempt_context(job, attempt)
    pair['delivery'].observe(job, attempt_id=attempt, profile_id=profile.id, provider_sid=None, status='failed',
                             event_key='call-broke', error='The call broke after page 6.',
                             error_category='partly_sent')
    assert pair['delivery'].get(job)['state'] == 'failed'
    started = datetime.utcnow() - timedelta(minutes=3)
    calls = sa.Table('sip_call_records', sa.MetaData(), autoload_with=pair['configuration'].engine)
    with pair['configuration'].engine.begin() as connection:
        connection.execute(calls.insert().values(
            id=uuid4().hex, direction='outbound', call_id=attempt, job_id=job, attempt_id=attempt, caller=A_NUMBER,
            called=B_NUMBER, started_at=started, answered_at=started + timedelta(seconds=5),
            ended_at=started + timedelta(minutes=2), disposition='answered', t38='yes', pages=held,
            fax_status='FAILED', fax_preference=0, created_at=started, updated_at=started))
    # B's engine kept the call's pages as a received fax (only the confirmed ones are whole).
    image = call_image(document, tmp_path, held, damaged=damaged)
    inbound = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=b_engine())
    fax_id = uuid4().hex
    moment = started + timedelta(minutes=2)
    with b_engine().begin() as connection:
        connection.execute(inbound.insert().values(
            id=fax_id, from_number=A_NUMBER, to_number=B_NUMBER, status='failed', backend='sip', pages=held,
            tiff_path=str(image), created_at=moment, received_at=moment, updated_at=moment))
    reach.down = False
    return job, attempt, fax_id, reach


def requests(reach):
    return reach.inner


@pytest.mark.asyncio
async def test_a_broken_call_is_completed_with_only_the_missing_pages(pair, tmp_path):
    job, attempt, call_fax, _ = await broken_call(pair, tmp_path, held=6)
    to_b = pair['to_b']
    posts_before = to_b.posts
    assert await CallRepair(pair['a']).step() is False
    (row,) = [r for r in RepairStore(pair['a'].store.engine).recent() if r['role'] == 'sender']
    assert row['state'] == 'completed' and row['pages_held'] == 6 and row['total_pages'] == 10
    assert row['attempt_id'] == attempt and row['message_id']
    # One question and one delivery of the four missing pages; nothing else was sent.
    assert to_b.posts - posts_before == 2
    (delivered,) = stored_document(pair['b_client'])
    from PIL import Image
    with Image.open(io.BytesIO(delivered)) as image:
        assert image.n_frames == 4
    # B files one received fax: the call's six pages and the four delivered directly.
    (item,) = b_items(pair['b_client'])
    assert item['source'] == 'direct'
    received = RepairStore(b_engine()).for_message('receiver', row['message_id'])
    assert received['state'] == 'completed' and received['inbound_id'] == call_fax
    with Image.open(received['assembled_path']) as whole:
        assert whole.n_frames == 10
    sent = pair['a'].store.find('outbound', row['message_id'])
    assert sent['state'] == 'accepted' and sent['kind'] == 'repair'
    listed = pair['b_client'].get('/direct/repairs', headers=ADMIN).json()['repairs']
    assert listed[0]['status'] == ('Completed: the missing pages went directly and Valley Hospital holds the whole '
                                   'fax. The call brought the first 6 of 10 pages; pages 7 to 10 came directly.')
    # Asked once: a later step does not ask about the same call again.
    await CallRepair(pair['a']).step()
    assert to_b.posts - posts_before == 2


@pytest.mark.asyncio
async def test_damaged_pages_without_error_correction_count_as_missing(pair, tmp_path):
    await broken_call(pair, tmp_path, held=6, damaged=5)
    await CallRepair(pair['a']).step()
    (row,) = [r for r in RepairStore(pair['a'].store.engine).recent() if r['role'] == 'sender']
    assert row['pages_held'] == 4 and row['state'] == 'completed'
    (delivered,) = stored_document(pair['b_client'])
    from PIL import Image
    with Image.open(io.BytesIO(delivered)) as image:
        assert image.n_frames == 6


@pytest.mark.asyncio
async def test_when_the_partner_cannot_find_the_call_nothing_is_sent(pair, tmp_path):
    job, _, call_fax, _ = await broken_call(pair, tmp_path, held=6)
    with b_engine().begin() as connection:
        connection.execute(sa.text('UPDATE inbound_faxes SET from_number = :other WHERE id = :id'),
                           {'other': '+15550109999', 'id': call_fax})
    posts_before = pair['to_b'].posts
    await CallRepair(pair['a']).step()
    (row,) = [r for r in RepairStore(pair['a'].store.engine).recent() if r['role'] == 'sender']
    assert row['state'] == 'expired' and row['pages_held'] == 0 and row['message_id'] is None
    assert pair['to_b'].posts - posts_before == 1  # the question only
    assert stored_document(pair['b_client']) == [] and pair['delivery'].get(job)['state'] == 'failed'


@pytest.mark.asyncio
async def test_when_the_partner_holds_every_page_nothing_is_sent_again(pair, tmp_path):
    await broken_call(pair, tmp_path, held=10)
    posts_before = pair['to_b'].posts
    await CallRepair(pair['a']).step()
    (row,) = [r for r in RepairStore(pair['a'].store.engine).recent() if r['role'] == 'sender']
    assert row['state'] == 'completed' and row['pages_held'] == 10
    assert pair['to_b'].posts - posts_before == 1 and stored_document(pair['b_client']) == []


def test_a_question_about_a_call_must_be_signed_by_a_verified_partner(pair):
    from api.app.direct.crypto import canonical, timestamp
    identity = pair['a'].identity()
    statement = canonical({'type': 'call_query', 'repair_id': 'a' * 32, 'message_id': 'b' * 32, 'caller': A_NUMBER,
                           'called': B_NUMBER, 'started_at': timestamp(), 'ended_at': timestamp(), 'pages_sent': 3,
                           'total_pages': 5, 'signer': identity.signing_key,
                           'recipient': pair['b_on_a']['signing_key'], 'created_at': timestamp()}).decode('ascii')
    forged = pair['b_client'].post('/direct/calls/pages', json={'statement': statement, 'signature': 'A' * 86})
    assert forged.status_code == 400
    answered = pair['b_client'].post('/direct/calls/pages',
                                     json={'statement': statement, 'signature': identity.sign(statement.encode())})
    assert answered.status_code == 200 and '"status":"not_found"' in answered.json()['statement']


def test_held_pages_reads_the_engine_count_and_damaged_lines(tmp_path):
    image = call_image(ten_pages(), tmp_path, 6)
    data = image.read_bytes()
    assert held_pages(data) == (6, [])
    assert held_pages(data, reported=5) == (5, [])
