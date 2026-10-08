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
    A_NUMBER, ADMIN, B_NUMBER, accept, b_client, b_items, direct_databases, pair, stored_document, transport)


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


def repairs(pair, *, delivery=True):
    """A's repair step, with A's outbound store (``delivery`` False: as if Faxbot stopped before updating the fax)."""
    return CallRepair(pair['a'], delivery=(lambda: pair['delivery']) if delivery else None)


def attempts(pair, job):
    table = pair['delivery'].attempts
    with pair['configuration'].engine.connect() as connection:
        return {row['id']: dict(row) for row in connection.execute(
            sa.select(table).where(table.c.job_id == job)).mappings()}


def events(pair, job):
    return [event['kind'] for event in pair['delivery'].history(job)]


@pytest.mark.asyncio
async def test_a_broken_call_is_completed_with_only_the_missing_pages(pair, tmp_path):
    job, attempt, call_fax, _ = await broken_call(pair, tmp_path, held=6)
    to_b = pair['to_b']
    posts_before = to_b.posts
    assert await repairs(pair).step() is False
    (row,) = [r for r in RepairStore(pair['a'].store.engine).recent() if r['role'] == 'sender']
    assert row['state'] == 'completed' and row['pages_held'] == 6 and row['total_pages'] == 10
    assert row['attempt_id'] == attempt and row['message_id']
    # The fax shows delivered, completed by the repair's own attempt; the broken attempt is kept as it was.
    fax = pair['delivery'].get(job)
    assert (fax['state'], fax['attempt_id']) == ('success', row['repair_id'])
    tried = attempts(pair, job)
    assert (tried[attempt]['phase'], tried[attempt]['error_category']) == ('failed', 'partly_sent')
    repaired = tried[row['repair_id']]
    assert repaired['phase'] == 'success' and repaired['sequence'] == tried[attempt]['sequence'] + 1
    assert repaired['submitted_at'] is not None and repaired['error_category'] is None
    with pair['configuration'].engine.connect() as connection:
        status = connection.execute(sa.text('SELECT status, error FROM fax_jobs WHERE id = :id'), {'id': job}).one()
        route = connection.execute(sa.text('SELECT route FROM delivery_attempt_costs WHERE id = :id'),
                                   {'id': row['repair_id']}).scalar()
    assert tuple(status) == ('success', None)
    assert route == 'direct'  # the pages that went directly are a direct delivery, never priced as a call
    assert events(pair, job)[-2:] == ['repair_started', 'repair_completed']
    # A late result for the broken call can no longer move the fax.
    from api.app.outbound_store import DeliveryConflict
    _, profile = pair['delivery'].attempt_context(job, attempt)
    with pytest.raises(DeliveryConflict):
        pair['delivery'].observe(job, attempt_id=attempt, profile_id=profile.id, provider_sid=None, status='failed',
                                 event_key='late-broken-call', error='The call broke after page 6.')
    assert pair['delivery'].get(job)['state'] == 'success'
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
    assert sent['attempt_id'] == row['repair_id'] != attempt  # its own attempt, never the broken one
    from api.app.direct.http import delivery_text
    assert delivery_text({**sent, 'organization': 'Valley Hospital'}) == (
        'Completed directly by Valley Hospital after the call broke: only the missing pages went, directly, and '
        'Valley Hospital now holds the whole fax.')
    listed = pair['b_client'].get('/direct/repairs', headers=ADMIN).json()['repairs']
    assert listed[0]['status'] == ('Completed: the missing pages went directly and Valley Hospital holds the whole '
                                   'fax. The call brought the first 6 of 10 pages; pages 7 to 10 came directly.')
    # Asked once: a later step does not ask about the same call again.
    await repairs(pair).step()
    assert to_b.posts - posts_before == 2


@pytest.mark.asyncio
async def test_the_certainty_question_asks_the_partner_through_the_real_repair_sender(pair, tmp_path):
    """AV's PartnerQuestion, without a stand-in: its question is this module's signed call query, answered by B."""
    from api.app.work.certainty import PartnerQuestion
    job, attempt, _, _ = await broken_call(pair, tmp_path, held=6)
    ask = PartnerQuestion(None, pair['a'])._ask_function()
    peer = pair['a'].store.get_peer(pair['b_on_a']['id'])
    # The call record as AV's checks read it (reflected, so its times are datetimes on both databases).
    records = sa.Table('sip_call_records', sa.MetaData(), autoload_with=pair['configuration'].engine)
    with pair['configuration'].engine.connect() as connection:
        call = dict(connection.execute(sa.select(
            records.c.caller, records.c.called, records.c.started_at, records.c.answered_at, records.c.ended_at,
            records.c.pages.label('pages_sent')).where(records.c.attempt_id == attempt)).mappings().one())
    answer = await ask(peer, call, 10)
    assert answer['type'] == 'call_pages' and answer['pages_held'] == 6 and answer['envelope']
    # Asking changes nothing: the fax and its broken attempt stay as they were.
    assert pair['delivery'].get(job)['state'] == 'failed'


@pytest.mark.asyncio
async def test_damaged_pages_without_error_correction_count_as_missing(pair, tmp_path):
    await broken_call(pair, tmp_path, held=6, damaged=5)
    await repairs(pair).step()
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
    await repairs(pair).step()
    (row,) = [r for r in RepairStore(pair['a'].store.engine).recent() if r['role'] == 'sender']
    assert row['state'] == 'expired' and row['pages_held'] == 0 and row['message_id'] is None
    assert pair['to_b'].posts - posts_before == 1  # the question only
    assert stored_document(pair['b_client']) == [] and pair['delivery'].get(job)['state'] == 'failed'
    assert len(attempts(pair, job)) == 1  # no attempt of its own: nothing was sent


@pytest.mark.asyncio
async def test_when_the_partner_holds_every_page_nothing_is_sent_again(pair, tmp_path):
    job, attempt, _, _ = await broken_call(pair, tmp_path, held=10)
    posts_before = pair['to_b'].posts
    # Faxbot stops after the partner's answer, before the fax is updated: the fax still reads failed.
    await repairs(pair, delivery=False).step()
    (row,) = [r for r in RepairStore(pair['a'].store.engine).recent() if r['role'] == 'sender']
    assert row['state'] == 'completed' and row['pages_held'] == 10
    assert pair['to_b'].posts - posts_before == 1 and stored_document(pair['b_client']) == []
    assert pair['delivery'].get(job)['state'] == 'failed'
    # The next step finishes it: delivered, with an attempt of its own that sent nothing and is never priced.
    await repairs(pair).step()
    fax = pair['delivery'].get(job)
    assert (fax['state'], fax['attempt_id']) == ('success', row['repair_id'])
    repaired = attempts(pair, job)[row['repair_id']]
    assert repaired['phase'] == 'success' and repaired['submitted_at'] is None
    assert attempts(pair, job)[attempt]['phase'] == 'failed'
    with pair['configuration'].engine.connect() as connection:
        assert connection.execute(sa.text('SELECT COUNT(*) FROM delivery_attempt_costs WHERE id = :id'),
                                  {'id': row['repair_id']}).scalar() == 0
    assert pair['to_b'].posts - posts_before == 1
    await repairs(pair).step()  # once only
    assert events(pair, job).count('repair_completed') == 1
    assert 'repair_started' not in events(pair, job)  # nothing went again


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


@pytest.mark.asyncio
async def test_a_partner_cannot_ask_about_or_add_pages_to_another_senders_fax(pair, tmp_path):
    from api.app.direct.crypto import canonical, timestamp
    await broken_call(pair, tmp_path, held=6)
    # B also holds a partial fax from a third party's number at the same time.
    other = call_image(ten_pages(), tmp_path, 3)
    inbound = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=b_engine())
    moment = datetime.utcnow() - timedelta(minutes=1)
    with b_engine().begin() as connection:
        connection.execute(inbound.insert().values(
            id=uuid4().hex, from_number='+15550109999', to_number=B_NUMBER, status='failed', backend='sip', pages=3,
            tiff_path=str(other), created_at=moment, received_at=moment, updated_at=moment))
    identity = pair['a'].identity()
    statement = canonical({'type': 'call_query', 'repair_id': 'c' * 32, 'message_id': 'd' * 32,
                           'caller': '+15550109999', 'called': B_NUMBER,
                           'started_at': timestamp(), 'ended_at': timestamp(), 'pages_sent': 3, 'total_pages': 10,
                           'signer': identity.signing_key, 'recipient': pair['b_on_a']['signing_key'],
                           'created_at': timestamp()}).decode('ascii')
    answered = pair['b_client'].post('/direct/calls/pages',
                                     json={'statement': statement, 'signature': identity.sign(statement.encode())})
    assert answered.status_code == 200 and '"status":"not_found"' in answered.json()['statement']
    assert RepairStore(b_engine()).find('receiver', 'c' * 32) is None


@pytest.mark.asyncio
async def test_an_unreachable_partner_is_asked_again_only_after_a_pause(pair, tmp_path):
    from api.app.direct import repair as repair_module
    _, attempt, _, reach = await broken_call(pair, tmp_path, held=6)
    reach.down = True
    posts_before = pair['to_b'].posts
    await repairs(pair).step()
    await repairs(pair).step()
    assert RepairStore(pair['a'].store.engine).for_attempt(attempt) is None
    assert attempt in repair_module._BACKOFF and repair_module._BACKOFF[attempt][1] == 1  # asked once, then paused
    reach.down = False
    repair_module._BACKOFF.pop(attempt)
    await repairs(pair).step()
    assert RepairStore(pair['a'].store.engine).for_attempt(attempt)['state'] == 'completed'
    assert pair['to_b'].posts - posts_before == 2
