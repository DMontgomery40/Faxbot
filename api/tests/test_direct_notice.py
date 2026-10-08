"""The notice fax: the original goes directly and one opaque page by fax; the receiver pairs them.

Installation B is the running application; A sends through the real delivery
worker. B's received notice fax is a synthetic received-fax row whose image is
the page A actually made (as an engine would hand it over), so pairing is
proven by SUB, by the printed barcode, and by a person when neither is there.
"""
import asyncio
from datetime import datetime
import io
import json
import random
from uuid import uuid4

import pytest
import sqlalchemy as sa

from app import main
from api.app.direct import notice
from api.app.outbound_worker import OutboundWorker
from api.tests.test_direct_delivery import (  # noqa: F401 - fixtures
    A_NUMBER, ADMIN, B_NUMBER, accept, b_client, b_items, direct_databases, pair, pdf_bytes, stored_document,
    transport)


class ToA:
    """B's transport to A: code confirmations and the signed statement that a notice was paired."""

    def __init__(self, service):
        self.service = service
        self.paired = []

    async def request(self, method, url, **kwargs):
        from api.app.direct.notice import NoticeSender
        body = kwargs['json']
        if url == 'https://a.example/direct/verifications':
            return await asyncio.to_thread(self.service.confirm, body['statement'], body['signature'])
        assert method == 'POST' and url == 'https://a.example/direct/notices/paired'
        self.paired.append(body)
        return await asyncio.to_thread(NoticeSender(self.service).note_paired, body['statement'], body['signature'])


@pytest.fixture
def noticed(pair):
    from api.app.access.runtime import AccessRuntime
    a = pair['a']
    runtime = AccessRuntime(pair['configuration'])
    a.access = lambda: runtime
    a.store.set_notice_fax(pair['b_on_a']['id'], True)
    to_a = ToA(a)
    main.app.state.direct_http = to_a
    return {**pair, 'to_a': to_a}


def b_service():
    from api.app.direct.http import service_for
    return service_for(main.app)


def b_engine():
    return main.app.state.configuration_runtime.manager.store.engine


def received_fax(path, *, pages=1, field='tiff_path', at=None):
    """A fax B received (as an engine hands one over), whose image is ``path``."""
    identity = uuid4().hex
    now = at or datetime.utcnow()
    inbound = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=b_engine())
    with b_engine().begin() as connection:
        connection.execute(inbound.insert().values(
            id=identity, from_number=A_NUMBER, to_number=B_NUMBER, status='received', backend='sip', pages=pages,
            created_at=now, received_at=now, updated_at=now, **{field: str(path)}))
    return identity


def fax_image(pdf, folder, *, resolution='204x196', subaddress=None, noise=0.0):
    """The page as a fax engine stores it: one-bit G4 TIFF, optionally with the SUB tag and line damage."""
    import subprocess
    from PIL import Image
    source, made = folder / f'{uuid4().hex}.pdf', folder / f'{uuid4().hex}.tif'
    source.write_bytes(pdf)
    subprocess.run(['gs', '-q', '-dSAFER', '-dNOPAUSE', '-dBATCH', '-sDEVICE=tiffg4', f'-r{resolution}',
                    f'-sOutputFile={made}', str(source)], check=True)
    with Image.open(made) as image:
        page = image.convert('1')
        dpi = image.info.get('dpi', (204, 196))
    if noise:
        rng = random.Random(7)
        pixels = page.load()
        width, height = page.size
        for _ in range(int(width * height * noise)):
            x, y = rng.randrange(width), rng.randrange(height)
            pixels[x, y] = 255 - pixels[x, y]
        for y in rng.sample(range(height), height // 50):  # whole lines lost on the line, as white rows
            for x in range(width):
                pixels[x, y] = 255
    extra = {}
    if subaddress:
        from PIL.TiffImagePlugin import ImageFileDirectory_v2
        directory = ImageFileDirectory_v2()
        directory[notice.SUB_TAG] = subaddress
        directory.tagtype[notice.SUB_TAG] = 2  # ASCII
        extra['tiffinfo'] = directory
    page.save(made, 'TIFF', compression='group4', dpi=dpi, **extra)
    return made


async def send_with_notice(noticed, text='Referral with a notice'):
    """A sends one document to B: delivered directly, held at B, and the notice fax queued at A."""
    original = pdf_bytes(text)
    job = accept(noticed, original)
    routed, conventional = transport(noticed)
    assert await OutboundWorker(noticed['delivery'], routed).step() is True
    assert noticed['delivery'].get(job)['state'] == 'success' and conventional.submissions == 0
    attempt = noticed['delivery'].get(job)['attempt_id']
    sent = notice.NoticeStore(noticed['a'].store.engine).find('sender', attempt)
    return original, job, attempt, sent, routed, conventional


@pytest.mark.parametrize('resolution', ['204x196', '204x98'])
def test_the_printed_code_reads_back_after_fax_conversion_and_line_damage(tmp_path, resolution):
    from PIL import Image
    identity = notice.new_notice_id()
    page = notice.notice_page(notice_id=identity, sender='Valley Hospital', sender_number=A_NUMBER,
                              recipient='County Clinic', recipient_number=B_NUMBER)
    for noise in (0.0, 0.004):
        with Image.open(fax_image(page, tmp_path, resolution=resolution, noise=noise)) as image:
            assert notice.read_barcode(image) == identity
    # The page carries the opaque code and nothing of the document, not even a file name.
    from pypdf import PdfReader
    text = PdfReader(io.BytesIO(page)).pages[0].extract_text()
    assert notice.code_text(identity) in text and 'referral' not in text.lower() and '.pdf' not in text
    assert notice.code128c('1234')[0] == notice.START_C and notice.normalize('1234 5678') is None


@pytest.mark.asyncio
async def test_the_original_goes_directly_held_and_paired_by_its_subaddress(noticed, tmp_path):
    original, job, attempt, sent, routed, conventional = await send_with_notice(noticed)
    client = noticed['b_client']
    # B holds the original out of Received until its notice fax arrives.
    assert stored_document(client) == [original] and b_items(client) == []
    assert sent['state'] == 'queued' and sent['notice_job_id'] == notice.notice_job_id(attempt)
    receipt = json.loads(json.loads(noticed['a'].store.find('outbound', attempt)['receipt'])['statement'])
    assert receipt['held_for_notice'] is True
    notice_job = noticed['delivery'].get(sent['notice_job_id'])
    assert notice_job is not None
    # The notice fax goes by telephone, never directly, even though B is a verified partner.
    assert await OutboundWorker(noticed['delivery'], routed).step() is True
    after = noticed['delivery'].get(sent['notice_job_id'])
    assert conventional.submissions == 1 and noticed['routes'].decision(after['attempt_id'])['route'] == 'phaxio'
    with noticed['configuration'].engine.connect() as connection:
        row = connection.execute(sa.text('SELECT to_number, pages, file_name FROM fax_jobs WHERE id = :id'),
                                 {'id': sent['notice_job_id']}).one()
        audit = connection.execute(sa.text("SELECT details FROM access_audit WHERE operation = 'fax.accept' "
                                           "ORDER BY created_at DESC")).scalars().first()
    assert tuple(row) == (B_NUMBER, 1, 'direct-delivery-notice.pdf')
    assert json.loads(audit) == {'original': job, 'source': 'direct_notice'}
    # The page A made arrives at B over the trunk, with the notice ID as its subaddress.
    page = (noticed['data'] / f"{sent['notice_job_id']}.pdf").read_bytes()
    fax_id = received_fax(fax_image(page, tmp_path, subaddress=sent['notice_id']))
    assert notice.NoticeReceiver(b_service()).step() is False
    (item,) = b_items(client)
    assert item['source'] == 'direct'
    listed = client.get('/direct/notices', headers=ADMIN).json()['notices']
    (received,) = [view for view in listed if view['direction'] == 'inbound']
    assert received['state'] == 'paired' and received['matched_by'] == 'sub' and received['fax_id'] == fax_id
    assert received['status'] == 'Paired by its subaddress with the notice fax; the document is in Received.'
    # The original is never labelled as faxed; only the notice page was.
    text = client.get('/direct/notices', headers=ADMIN, params={'fax': fax_id}).json()['notice_text']
    assert text == "Notice for Valley Hospital's 1-page document: the document came by direct delivery."
    # B told A, signed; A's record says what happened, also without calling the original faxed.
    assert await notice.NoticeReceiver(b_service()).tell_partners() is False
    paired = notice.NoticeStore(noticed['a'].store.engine).find('sender', attempt)
    assert paired['state'] == 'paired' and paired['matched_by'] == 'sub' and paired['pairing_statement']
    view = notice.notice_view(paired, organization='County Clinic')
    assert view['status'] == 'County Clinic paired the faxed notice with the document delivered directly.'


@pytest.mark.asyncio
async def test_without_a_subaddress_the_printed_barcode_pairs_it(noticed, tmp_path):
    original, _, attempt, sent, _, _ = await send_with_notice(noticed, 'Second referral')
    page = (noticed['data'] / f"{sent['notice_job_id']}.pdf").read_bytes()
    # Received by a provider as a PDF (no subaddress), at standard resolution with line damage.
    from PIL import Image
    with Image.open(fax_image(page, tmp_path, resolution='204x98', noise=0.003)) as image:
        image.save(tmp_path / 'notice.pdf', 'PDF', resolution=98)
    fax_id = received_fax(tmp_path / 'notice.pdf', field='pdf_path')
    notice.NoticeReceiver(b_service()).step()
    row = notice.NoticeStore(b_engine()).find('receiver', attempt)
    assert row['state'] == 'paired' and row['matched_by'] == 'barcode' and row['inbound_id'] == fax_id
    assert len(b_items(noticed['b_client'])) == 1


@pytest.mark.asyncio
async def test_with_neither_a_person_pairs_it_by_the_code(noticed, tmp_path):
    original, _, attempt, sent, _, _ = await send_with_notice(noticed, 'Third referral')
    client = noticed['b_client']
    # A one-page fax arrives with no subaddress and no readable code (say, printed and rescanned badly).
    blank = fax_image(pdf_bytes(' '), tmp_path)
    fax_id = received_fax(blank)
    service = b_service()
    notice.NoticeReceiver(service).step()
    notice.NoticeReceiver(service).step()  # each received fax is examined once
    with b_engine().connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM direct_notice_scans WHERE inbound_id = :id'),
                                  {'id': fax_id}).scalar() == 1
    row = notice.NoticeStore(b_engine()).find('receiver', attempt)
    assert row['state'] == 'waiting' and b_items(client) == []
    candidates = client.get(f"/direct/notices/{row['id']}/faxes", headers=ADMIN).json()['faxes']
    assert [fax['id'] for fax in candidates] == [fax_id]
    wrong = client.post(f"/direct/notices/{row['id']}/pair", headers=ADMIN, json={'code': '1' * 20, 'fax_id': fax_id})
    assert wrong.status_code == 409 and 'not the one on this notice' in wrong.json()['detail']
    paired = client.post(f"/direct/notices/{row['id']}/pair", headers=ADMIN,
                         json={'code': notice.code_text(sent['notice_id']), 'fax_id': fax_id})
    assert paired.status_code == 200, paired.text
    assert paired.json()['state'] == 'paired' and paired.json()['matched_by'] == 'person'
    assert paired.json()['partner_told'] is True and len(b_items(client)) == 1
    again = client.post(f"/direct/notices/{row['id']}/pair", headers=ADMIN, json={'fax_id': fax_id})
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_a_partner_that_cannot_pair_notices_gets_the_fax_by_fax(noticed):
    to_b = noticed['to_b']
    real = to_b.request

    async def older(method, url, **kwargs):
        if url.endswith('/direct/notices'):
            return 404, {'detail': 'Not found.'}
        return await real(method, url, **kwargs)
    to_b.request = older
    job = accept(noticed, pdf_bytes('Needs a fax event'))
    routed, conventional = transport(noticed)
    await OutboundWorker(noticed['delivery'], routed).step()
    row = noticed['delivery'].get(job)
    # Its intake needs a fax event, so the whole fax goes by fax; nothing went directly.
    assert row['state'] == 'in_progress' and conventional.submissions == 1
    assert b_items(noticed['b_client']) == [] and stored_document(noticed['b_client']) == []
    sent = notice.NoticeStore(noticed['a'].store.engine).find('sender', row['attempt_id'])
    assert sent['state'] == 'cancelled' and sent['notice_job_id'] is None


@pytest.mark.asyncio
async def test_only_a_waiting_notice_holds_the_original(noticed):
    """A notice that ends any other way (cancelled) no longer keeps its original out of Received."""
    original, _, attempt, _, _, _ = await send_with_notice(noticed, 'Held then released')
    client = noticed['b_client']
    service = b_service()
    assert b_items(client) == [] and [row['message_id'] for row in service.store.unfiled()] == []
    store = notice.NoticeStore(b_engine())
    held = store.find('receiver', attempt)
    assert held['state'] == 'waiting'
    store.update(held['id'], state='cancelled')
    assert [row['message_id'] for row in service.store.unfiled()] == [attempt]
    service.filing.step()
    (item,) = b_items(client)
    assert item['source'] == 'direct' and stored_document(client) == [original]


def test_a_notice_link_needs_the_sender_signature_and_comes_before_its_document(noticed):
    from api.app.direct.crypto import canonical, timestamp
    a, client = noticed['a'], noticed['b_client']
    identity = a.identity()
    body = {'type': 'notice', 'message_id': 'a' * 32, 'notice_id': '1' * 20, 'document_sha256': 'b' * 64,
            'signer': identity.signing_key, 'recipient': noticed['b_on_a']['signing_key'], 'created_at': timestamp()}
    statement = canonical(body).decode('ascii')
    forged = client.post('/direct/notices', json={'statement': statement, 'signature': 'A' * 86})
    assert forged.status_code == 400
    recorded = client.post('/direct/notices', json={'statement': statement, 'signature': identity.sign(statement.encode())})
    assert recorded.status_code == 200 and '"status":"recorded"' in recorded.json()['statement']


def test_the_partner_switch_and_its_sentence(noticed):
    client = noticed['b_client']
    peer = noticed['a_on_b']
    on = client.post(f"/direct/peers/{peer['id']}/notice-fax", headers=ADMIN, json={'on': True})
    assert on.status_code == 200 and on.json()['notice_fax'] is True
    assert on.json()['detail'] == 'Each document to Valley Hospital now goes directly, with a one-page notice by fax.'
    off = client.post(f"/direct/peers/{peer['id']}/notice-fax", headers=ADMIN, json={'on': False})
    assert off.json()['notice_fax'] is False and off.json()['notice_fax_text'] is None


def test_the_ssl_fax_engine_requests_the_notice_id_as_the_subaddress(tmp_path):
    from api.app import hylafax_engine
    from api.tests.test_hylafax_engine import ATTEMPT, JOB, FakeEngine, trunk_values
    import threading
    values = trunk_values(tmp_path)
    server = FakeEngine(hylafax_engine.engine_secrets(values)['submit_password'])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        image = tmp_path / 'fax.tiff'
        image.write_bytes(b'II*\x00synthetic')
        hylafax_engine.create_job(values, tag=hylafax_engine.new_tag(), job_id=JOB, attempt_id=ATTEMPT,
                                  tiff_path=str(image), subaddress='1234 5678 9012 3456 7890',
                                  host='127.0.0.1', port=server.server_address[1]).discard()
        assert 'JPARM SUBADDR "12345678901234567890"' in server.commands
        server.commands.clear()
        hylafax_engine.create_job(values, tag=hylafax_engine.new_tag(), job_id=JOB, attempt_id=ATTEMPT,
                                  tiff_path=str(image), host='127.0.0.1', port=server.server_address[1]).discard()
        assert not any(command.startswith('JPARM SUBADDR') for command in server.commands)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_the_built_in_engines_sub_frame_pairs_it(noticed, tmp_path):
    """A fax over the SIP trunk on the built-in engine: the SUB comes from its call's frames (patch 0004)."""
    _, _, attempt, sent, _, _ = await send_with_notice(noticed, 'Fourth referral')
    blank = fax_image(pdf_bytes(' '), tmp_path)  # no barcode on the image: only the frame can pair it
    fax_id = received_fax(blank)
    key = '1760000000.42'
    now = datetime.utcnow()
    imports = sa.Table('inbound_imports', sa.MetaData(), autoload_with=b_engine())
    frames = sa.Table('fax_call_frames', sa.MetaData(), autoload_with=b_engine())
    # T.30 SUB: address, control, FCF 0xC2, then the 20 digits sent last first.
    sub = 'ff13c2' + ''.join(f'{ord(digit):02x}' for digit in reversed(sent['notice_id']))
    with b_engine().begin() as connection:
        connection.execute(imports.insert().values(
            id=uuid4().hex, source='sip', account='sip:trunk', operation_id=key, revision='r1', state='pending',
            attempts=1, imported_at=now, inbound_fax_id=fax_id, created_at=now, updated_at=now))
        connection.execute(frames.insert().values(id=f'in:{key}', direction='in', call_key=key, sub=sub,
                                                  created_at=now))
    notice.NoticeReceiver(b_service()).step()
    row = notice.NoticeStore(b_engine()).find('receiver', attempt)
    assert row['state'] == 'paired' and row['matched_by'] == 'sub' and row['inbound_id'] == fax_id


@pytest.mark.asyncio
async def test_the_notice_fax_is_queued_by_the_background_step_once(noticed):
    """The delivery worker's own service has no access runtime: the background step queues the notice fax."""
    runtime = noticed['a'].access()
    noticed['a'].access = lambda: None
    original = pdf_bytes('Queued later')
    job = accept(noticed, original)
    routed, _ = transport(noticed)
    assert await OutboundWorker(noticed['delivery'], routed).step() is True
    attempt = noticed['delivery'].get(job)['attempt_id']
    store = notice.NoticeStore(noticed['a'].store.engine)
    assert store.find('sender', attempt)['state'] == 'announced'
    noticed['a'].access = lambda: runtime
    assert notice.NoticeSender(noticed['a']).step() is False
    queued = store.find('sender', attempt)
    assert queued['state'] == 'queued' and queued['notice_job_id'] == notice.notice_job_id(attempt)
    notice.NoticeSender(noticed['a']).step()
    with noticed['configuration'].engine.connect() as connection:
        assert connection.execute(sa.text('SELECT count(*) FROM fax_jobs WHERE file_name = :name'),
                                  {'name': notice.FILE_NAME}).scalar() == 1
