"""A fax image's unchanged body, reused for an enrolled partner (Codex's fifth idea, extending D9).

A fax image differs on every send only in what Faxbot itself draws: the header band with the date, time and page
number. Its page body is written in strips of its own, addressed by a digest, so a partner that holds an earlier
image with the same body rebuilds the exact new image from Faxbot's new header bands alone, and files it only when
the whole image matches the signed manifest's SHA-256. Everything else is an explicit miss, and the whole image
goes at once under the same message ID.

The first part checks the fax image layout on its own; the second runs two synthetic installations over the real
HTTP stack on SQLite and PostgreSQL (``peer_pair``). Every page, number and partner is synthetic.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from api.app.direct import faximage, reuse
from api.app.direct.crypto import timestamp
from api.app.direct.distribute import sent_texts
from api.app.outbound_worker import OutboundWorker
from api.tests.test_cli import cli, server  # noqa: F401 - fixtures
from api.tests.test_direct_delivery import B_NUMBER, pdf_bytes
from api.tests.test_peer_fax import IMAGE_LABEL, opt_in, peer_pair, received, rows, transport  # noqa: F401 - fixture


A_NUMBER = '+15550100001'
MOMENT = datetime(2026, 10, 7, 20, 5)
LATER = datetime(2026, 10, 8, 15, 31)
BODY_LABEL = ('Delivered directly as a fax image by Valley Hospital, rebuilt from pages it sent you before with only '
              'its new header lines, and checked against the whole image; no telephone call.')
OTHER_NUMBER = '+15550100099'


def engine_image(pages=2, *, width=1734, y_dpi=196, rows=400, dot=None):
    """A synthetic engine image as ``pdf_to_tiff`` makes one; ``dot`` = (page, x, y) blackens one more pixel."""
    from PIL import Image, ImageDraw
    frames = []
    for number in range(pages):
        frame = Image.new('1', (width, rows), 1)
        draw = ImageDraw.Draw(frame)
        for line in range(rows // 60):
            draw.text((120, 40 + 60 * line), f'Synthetic referral, page {number + 1}, line {line + 1}', fill=0)
        if dot is not None and dot[0] == number:
            frame.putpixel(dot[1:], 0)
        frames.append(frame)
    output = io.BytesIO()
    frames[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=frames[1:], dpi=(204, y_dpi))
    return output.getvalue()


def stamped(tiff, moment=MOMENT):
    return faximage.stamp(tiff, header='Valley Hospital', station=A_NUMBER, moment=moment)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def pixels(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        found = []
        for page in range(image.n_frames):
            image.seek(page)
            found.append((image.size, image.tobytes()))
        return found


# -- the fax image's own layout ----------------------------------------------------------------------------------

def test_identical_bodies_with_changing_headers_share_one_body_digest():
    tiff = engine_image(3)
    first, _ = stamped(tiff)
    second, facts = stamped(tiff, LATER)
    assert sha(first) != sha(second) and facts['header_line'].startswith(' 8-Oct-2026')
    one, two = faximage.layout(first), faximage.layout(second)
    assert one is not None and two is not None
    assert one.body_sha256 == two.body_sha256 and one.pages == 3 and one.band_rows == faximage.BAND_ROWS_FINE
    # Only the header bands differ: the body strips are the same bytes, wherever they sit in each file.
    assert faximage.body_strips(first, one) == faximage.body_strips(second, two)
    remaining, holes = faximage.cut(second, two)
    assert len(remaining) + sum(length for _, length in holes) == len(second)
    # The earlier image's body put into the new image's holes is the new image, byte for byte.
    rebuilt = faximage.splice(remaining, holes, faximage.body_strips(first, one), size=len(second))
    assert rebuilt == second and sha(rebuilt) == sha(second)
    assert faximage.check(rebuilt, facts, 3) == 3


def test_a_one_pixel_body_change_changes_the_body_digest_and_cannot_be_rebuilt_from_the_old_body():
    first, _ = stamped(engine_image(3))
    changed, _ = stamped(engine_image(3, dot=(1, 900, 200)), LATER)
    old, new = faximage.layout(first), faximage.layout(changed)
    assert old.body_sha256 != new.body_sha256
    remaining, holes = faximage.cut(changed, new)
    try:
        rebuilt = faximage.splice(remaining, holes, faximage.body_strips(first, old), size=len(changed))
    except faximage.FaxImageInvalid:
        rebuilt = None
    # Either the old strips do not fit, or they fit and give another image: never the declared one.
    assert rebuilt is None or sha(rebuilt) != sha(changed)


@pytest.mark.parametrize('y_dpi,rows', [(98, 300), (196, 400), (391, 800)])
@pytest.mark.parametrize('width', [1728, 2048, 2432])
def test_every_resolution_and_width_has_its_band_as_the_first_strip(y_dpi, rows, width):
    from PIL import Image
    data, facts = stamped(engine_image(2, width=width, y_dpi=y_dpi, rows=rows))
    found = faximage.layout(data)
    band = faximage.band_rows(y_dpi)
    assert found is not None and found.band_rows == band and facts['width'] == width
    with Image.open(io.BytesIO(data)) as image:
        for page in range(2):
            image.seek(page)
            assert image.tag_v2[278] == band and len(image.tag_v2[273]) == -(-image.size[1] // band)
    assert len(found.body) == 2 * (-(-(band + rows) // band) - 1)


def test_the_strip_layout_changes_no_pixel_and_still_becomes_the_pdf_people_read(tmp_path):
    from PIL import Image
    data, facts = stamped(engine_image(2))
    with Image.open(io.BytesIO(data)) as image:
        pages = []
        for page in range(image.n_frames):
            image.seek(page)
            pages.append(image.copy())
    whole = io.BytesIO()
    pages[0].save(whole, 'TIFF', compression='group4', save_all=True, append_images=pages[1:], dpi=(204, 196),
                  strip_size=10 ** 9)
    assert pixels(whole.getvalue()) == pixels(data)
    assert faximage.layout(whole.getvalue()) is None  # one strip per page: no body of its own, so it goes whole
    assert faximage.check(data, facts, 2) == 2
    assert faximage.readable_copy(data, tmp_path) == 2


def test_only_a_fax_image_of_this_layout_has_a_body():
    from PIL import Image
    assert faximage.layout(b'%PDF-1.7 not an image') is None
    assert faximage.layout(b'II*\x00' + b'\x00' * 64) is None
    data, _ = stamped(engine_image(1))
    assert faximage.layout(data[:len(data) // 2]) is None  # cut short: strips run past the end
    gray = io.BytesIO()
    Image.new('L', (1728, 400), 255).save(gray, 'TIFF', dpi=(204, 196))
    assert faximage.layout(gray.getvalue()) is None


def test_splice_refuses_strips_that_do_not_fill_their_holes_exactly():
    data, _ = stamped(engine_image(2))
    found = faximage.layout(data)
    remaining, holes = faximage.cut(data, found)
    strips = faximage.body_strips(data, found)
    with pytest.raises(faximage.FaxImageInvalid):
        faximage.splice(remaining, holes, strips[:-1], size=len(data))
    with pytest.raises(faximage.FaxImageInvalid):
        faximage.splice(remaining, holes, [strips[0] + b'\x00'] + strips[1:], size=len(data))
    with pytest.raises(faximage.FaxImageInvalid):
        faximage.splice(remaining, holes, strips, size=len(data) + 1)
    with pytest.raises(faximage.FaxImageInvalid):
        faximage.splice(remaining, [(len(data) * 2, holes[0][1])] + holes[1:], strips, size=len(data))
    assert faximage.splice(remaining, holes, strips, size=len(data)) == data


# -- two installations: A (Valley Hospital) sends fax images to B (County Clinic) --------------------------------

@pytest.fixture
def images(peer_pair, monkeypatch):
    """The pair with fax images on, A's requests to B logged, and every fax's header time a few minutes later."""
    from api.app.direct import service as a_service
    pair = peer_pair
    opt_in(pair)
    posts = []
    real = pair['to_b'].request

    async def logged(method, url, **kwargs):
        posts.append(url[len('https://testserver'):])
        return await real(method, url, **kwargs)
    pair['to_b'].request = logged
    start, step = datetime.now(timezone.utc) - timedelta(hours=1), {'minutes': 0}

    def later(moment=None):
        if moment is not None:
            return timestamp(moment)
        step['minutes'] += 7  # a new header line each time; the page body stays the same
        return timestamp(start + timedelta(minutes=step['minutes']))
    monkeypatch.setattr(a_service, 'timestamp', later)
    return {**pair, 'posts': posts}


def accept_image(pair, tiff):
    job = uuid4().hex
    (pair['data'] / (job + '.pdf')).write_bytes(pdf_bytes('Synthetic referral'))
    (pair['data'] / (job + '.tiff')).write_bytes(tiff)
    now = datetime.utcnow()
    pair['configuration'].accept_outbound(pair['snapshot'].active, {
        'id': job, 'to_number': B_NUMBER, 'file_name': 'referral.pdf', 'tiff_path': '', 'status': 'queued',
        'pages': 3, 'created_at': now, 'updated_at': now})
    return job


async def send(pair, tiff):
    """One fax of ``tiff`` through A's real delivery worker: (A's outbound record, the paths A posted to B)."""
    job = accept_image(pair, tiff)
    before = len(pair['posts'])
    routed, conventional = transport(pair)
    assert await OutboundWorker(pair['delivery'], routed).step() is True
    row = pair['delivery'].get(job)
    # Never by telephone: every miss is a signed refusal, and the whole image then goes directly.
    assert row['state'] == 'success' and conventional.submissions == 0
    return pair['a'].store.find('outbound', row['attempt_id']), pair['posts'][before:]


def arrival(pair, message_id):
    """B's record of one arrival, checked against the decisive rule: what B keeps is exactly the signed image."""
    (row,) = rows(pair['b_engine'], "SELECT * FROM direct_deliveries WHERE direction = 'inbound' "
                                    'AND message_id = :id', id=message_id)
    manifest = json.loads(row['manifest'])
    kept = Path(row['document_path']).read_bytes()
    assert sha(kept) == row['digest'] == manifest['document']['sha256'] and len(kept) == manifest['document']['size']
    return {**row, 'said': reuse._statement(row['receipt']), 'data': kept}


def lose(pair, message_id):
    """B loses its kept copy of one arrival (a disk cleanup, a restore from an older backup)."""
    path = Path(arrival(pair, message_id)['document_path'])
    path.write_bytes(b'')
    path.chmod(0)


def b_reuse():
    import app.direct.reuse as module  # B runs as the application package
    return module


@pytest.mark.asyncio
async def test_identical_bodies_with_changing_headers_send_only_the_header_regions(images):
    pair, tiff = images, engine_image(3, rows=1200)
    first, posts = await send(pair, tiff)
    assert posts == ['/direct/deliveries']  # nothing is held yet, so nothing is asked
    second, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions']
    third, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions']

    sent = [first, second, third]
    # Three different fax images (each header line has its own time) with one body.
    assert len({row['digest'] for row in sent}) == 3
    kept = [arrival(pair, row['message_id']) for row in sent]
    assert [row['digest'] for row in kept] == [row['digest'] for row in sent]
    body = faximage.layout(kept[0]['data']).body_sha256
    assert [faximage.layout(row['data']).body_sha256 for row in kept] == [body] * 3
    assert [row['said'].get('body_sha256') for row in kept] == [body] * 3
    assert [row['said'].get('carriage') for row in kept] == [None, 'body', 'body']
    # The base is a fax image B held, and what crossed is a small part of the image.
    assert kept[1]['said']['base_sha256'] == first['digest']
    assert kept[2]['said']['base_sha256'] in {first['digest'], second['digest']}
    for row in kept[1:]:
        assert 0 < row['said']['delta_size'] < row['size_bytes'] // 4

    # Received says how each arrived; Sent and Costs -> Savings count bytes, never money.
    assert sorted(fax['status_text'] for fax in received(pair)) == sorted([IMAGE_LABEL, BODY_LABEL, BODY_LABEL])
    store = reuse.ReuseStore(pair['a'].store.engine)
    saving = store.saving(second['message_id'])
    assert (saving['carriage'], saving['kind'], saving['base_sha256']) == ('patch', 'fax_image', first['digest'])
    assert saving['sent_bytes'] == kept[1]['said']['delta_size'] and saving['full_bytes'] == second['size_bytes']
    assert store.saving(first['message_id']) is None
    texts = sent_texts(pair['a'], pair['a'].store.recent())
    assert first['message_id'] not in texts
    assert texts[second['message_id']] == (
        'Delivered directly as a fax image to County Clinic, which already held these pages; only the new header '
        f"lines went, {reuse.size_text(saving['sent_bytes'])} instead of {reuse.size_text(saving['full_bytes'])}.")
    view = reuse.savings_view(pair['a'].store.engine, since=datetime(2000, 1, 1), days=30)
    assert (view['fax_images'], view['documents'], view['references'], view['patches']) == (2, 2, 0, 0)
    assert view['bytes_saved'] == sum(row['size_bytes'] - row['said']['delta_size'] for row in kept[1:])
    assert view['sentence'] == (
        f"{reuse.size_text(view['bytes_saved'])} not sent in the last 30 days: 2 fax images sent as new header lines "
        'only, over pages the partner already held. These are bytes over the internet, not money; the calls were '
        'already saved.')


@pytest.mark.asyncio
async def test_a_one_pixel_body_change_goes_whole_and_a_claim_to_the_old_body_never_arrives(images, monkeypatch):
    pair = images
    first, _ = await send(pair, engine_image(3, rows=1200))
    second, posts = await send(pair, engine_image(3, rows=1200, dot=(2, 700, 900)))
    assert posts == ['/direct/deliveries']  # A never sent this body, so it asks nothing
    assert arrival(pair, second['message_id'])['said'].get('carriage') is None

    # Even when a sender claims the old body for a changed image, B rebuilds, compares and refuses: it never keeps
    # an image other than the signed one, and the whole image follows under the same message ID.
    old_body = faximage.layout(arrival(pair, first['message_id'])['data']).body_sha256
    real = faximage.layout

    def claimed(data):
        found = real(data)
        return faximage.Layout(old_body, found.body, found.pages, found.band_rows)
    monkeypatch.setattr(reuse.faximage, 'layout', claimed)
    third, posts = await send(pair, engine_image(3, rows=1200, dot=(0, 300, 1000)))
    assert posts == ['/direct/holdings', '/direct/regions', '/direct/deliveries']
    kept = arrival(pair, third['message_id'])
    assert kept['said'].get('carriage') is None and kept['digest'] == third['digest']
    assert reuse.ReuseStore(pair['a'].store.engine).saving(third['message_id']) is None


@pytest.mark.asyncio
async def test_a_missing_base_is_an_explicit_miss_and_the_whole_image_follows(images, monkeypatch):
    pair, tiff = images, engine_image(3, rows=1200)
    first, _ = await send(pair, tiff)
    lose(pair, first['message_id'])

    # B answered that it holds the body, then lost it before the regions came: not held, nothing accepted.
    real = b_reuse().ReuseStore.held_body
    calls = []

    def held_while_asked(store, peer_id, digest, recipient):
        calls.append(digest)
        return None if len(calls) == 1 else real(store, peer_id, digest, recipient)
    monkeypatch.setattr(b_reuse().ReuseStore, 'held_body', held_while_asked)
    second, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions', '/direct/deliveries'] and len(calls) == 2
    assert arrival(pair, second['message_id'])['said'].get('carriage') is None
    monkeypatch.setattr(b_reuse().ReuseStore, 'held_body', real)

    # With no kept copy left, B says so when asked, and the whole image goes at once.
    lose(pair, second['message_id'])
    third, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/deliveries']
    assert arrival(pair, third['message_id'])['digest'] == third['digest']
    store = reuse.ReuseStore(pair['a'].store.engine)
    assert store.saving(second['message_id']) is None and store.saving(third['message_id']) is None


@pytest.mark.asyncio
async def test_an_expired_authorization_is_an_explicit_miss_and_a_fresh_one_works(images, monkeypatch):
    pair, tiff = images, engine_image(3, rows=1200)
    await send(pair, tiff)
    monkeypatch.setattr(b_reuse(), 'AUTHORIZATION_SECONDS', -60)  # B's answer has expired when the regions come
    second, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions', '/direct/deliveries']
    assert arrival(pair, second['message_id'])['said'].get('carriage') is None
    monkeypatch.setattr(b_reuse(), 'AUTHORIZATION_SECONDS', 600)
    third, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions']
    assert arrival(pair, third['message_id'])['said']['carriage'] == 'body'


@pytest.mark.asyncio
async def test_a_changed_recipient_is_an_explicit_miss(images, monkeypatch):
    pair, tiff = images, engine_image(3, rows=1200)
    first, _ = await send(pair, tiff)
    real = reuse.regions_request

    def for_another_number(identity, submission, recipient, found, authorization):
        return real(identity, submission, {**recipient, 'fax_number': OTHER_NUMBER}, found, authorization)
    monkeypatch.setattr(reuse, 'regions_request', for_another_number)
    second, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions', '/direct/deliveries']
    assert arrival(pair, second['message_id'])['said'].get('carriage') is None

    # A body B holds for one recipient is never used for another: B's own records say where each arrival went.
    kept = arrival(pair, first['message_id'])
    store = b_reuse().ReuseStore(pair['b_engine'])
    body = faximage.layout(kept['data']).body_sha256
    recipient = json.loads(kept['manifest'])['recipient']
    assert store.held_body(kept['peer_id'], body, recipient)[2] in {first['digest'], second['digest']}
    with pytest.raises(b_reuse().CarriageMiss) as missed:
        store.held_body(kept['peer_id'], body, {**recipient, 'fax_number': OTHER_NUMBER})
    assert missed.value.reason == 'recipient_changed'
    with pytest.raises(b_reuse().CarriageMiss) as missed:
        store.held_body(kept['peer_id'], 'f' * 64, recipient)
    assert missed.value.reason == 'not_held'


@pytest.mark.asyncio
async def test_an_answer_given_for_another_fax_is_no_authorization(images, monkeypatch):
    pair, tiff = images, engine_image(3, rows=1200)
    await send(pair, tiff)
    real, answers = reuse.regions_request, []

    def keeping(identity, submission, recipient, found, authorization):
        answers.append(authorization)
        return real(identity, submission, recipient, found, answers[0])  # always B's first answer
    monkeypatch.setattr(reuse, 'regions_request', keeping)
    second, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions']
    # B's answer named the fax it was asked about; replayed for the next fax it authorizes nothing.
    third, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/regions', '/direct/deliveries']
    assert arrival(pair, second['message_id'])['said']['carriage'] == 'body'
    assert arrival(pair, third['message_id'])['said'].get('carriage') is None


@pytest.mark.asyncio
async def test_a_partner_that_does_not_know_fax_image_bodies_gets_the_whole_image(images, monkeypatch):
    pair, tiff = images, engine_image(3, rows=1200)
    await send(pair, tiff)
    # An earlier Faxbot refuses the question it does not know, unsigned; nothing else changes.
    monkeypatch.setattr(b_reuse(), '_answer_bodies',
                        lambda *args, **kwargs: (400, {'detail': 'This question could not be checked.'}))
    second, posts = await send(pair, tiff)
    assert posts == ['/direct/holdings', '/direct/deliveries']
    assert arrival(pair, second['message_id'])['digest'] == second['digest']


# -- what the console and the command line show -------------------------------------------------------------------

def test_costs_savings_and_sent_say_what_a_reused_body_saved(cli, tmp_path):  # noqa: F811
    """``faxbot costs savings`` and the deliveries Sent and the console read carry the bytes, never money."""
    import sqlalchemy as sa
    from app import main
    from app.direct.crypto import Identity, card
    partner = tmp_path / 'clinic.json'
    partner.write_text(json.dumps(card(Identity.generate(), organization='Valley Hospital',
                                       fax_number='+15550007777', endpoint='https://valley.example')))
    peer = cli.json('recipients', 'partners', 'add', partner)
    engine = main.app.state.configuration_runtime.manager.store.engine
    now, message = datetime.utcnow(), uuid4().hex
    receipt = json.dumps({'statement': json.dumps({'type': 'receipt', 'carriage': 'body', 'delta_size': 2048}),
                          'signature': 'synthetic'})
    with engine.begin() as connection:
        connection.execute(sa.text(
            'INSERT INTO direct_deliveries (id, direction, message_id, peer_id, recipient_number, digest, size_bytes, '
            'manifest, state, receipt, accepted_at, created_at, updated_at, kind) VALUES (:id, :direction, :message, '
            ':peer, :number, :digest, :size, :manifest, :state, :receipt, :now, :now, :now, :kind)'),
            {'id': uuid4().hex, 'direction': 'outbound', 'message': message, 'peer': peer['id'],
             'number': '+15550007777', 'digest': 'a' * 64, 'size': 61440, 'manifest': '{}', 'state': 'accepted',
             'receipt': receipt, 'now': now, 'kind': 'fax_image'})
        connection.execute(sa.text(
            'INSERT INTO direct_byte_savings (id, message_id, peer_id, carriage, document_sha256, base_sha256, '
            'full_bytes, sent_bytes, created_at) VALUES (:id, :message, :peer, :carriage, :digest, :base, :full, '
            ':sent, :now)'),
            {'id': uuid4().hex, 'message': message, 'peer': peer['id'], 'carriage': 'patch', 'digest': 'a' * 64,
             'base': 'b' * 64, 'full': 61440, 'sent': 2048, 'now': now})
    savings = cli('costs', 'savings')
    assert savings.exit_code == 0, savings.stderr
    assert ('Bytes saved by reuse and patches: 58 KB not sent in the last 30 days: 1 fax image sent as new header '
            'lines only, over pages the partner already held. These are bytes over the internet, not money; the calls '
            'were already saved.') in ' '.join(savings.stdout.split())  # the terminal wraps long lines
    assert cli.json('costs', 'savings')['direct_bytes']['fax_images'] == 1
    from api.tests.test_cli import BOOTSTRAP
    deliveries = cli.client.get('/direct/deliveries', headers={'X-API-Key': BOOTSTRAP}).json()['deliveries']
    (record,) = [item for item in deliveries if item['message_id'] == message]
    assert record['send_once'] == ('Delivered directly as a fax image to Valley Hospital, which already held these '
                                   'pages; only the new header lines went, 2.0 KB instead of 60 KB.')
