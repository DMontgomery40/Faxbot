"""A document sent to a partner in pieces: preflight, resume after a drop, repair, duplicates and one commit.

Installation B is the running application (TestClient); installation A posts
to it through a transport that can drop a request before it reaches B or lose
B's answer after B has it. Pieces are made small (1 KiB) so a few kilobytes of
synthetic PDF become several pieces.
"""
import asyncio
import hashlib
import os
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from app import main
from api.app.direct.crypto import timestamp
from api.app.direct.service import DirectReconciler
from api.app.direct.transfer import TransferStore
from api.app.outbound_worker import OutboundWorker
from api.tests.test_direct_delivery import (  # noqa: F401 - fixtures
    ADMIN, accept, b_client, b_items, pair, stored_document, transport)


def big_pdf(seed='transfer'):
    """A synthetic PDF of several kilobytes that does not compress away."""
    from io import BytesIO
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output, pageCompression=0)
    y = 760
    for line in range(60):
        pdf.drawString(40, y, hashlib.sha256(f'{seed}:{line}'.encode()).hexdigest())
        y -= 12
    pdf.showPage()
    pdf.save()
    return output.getvalue()


class Partner:
    """A's transport to B with scripted trouble: ``plan[(method, prefix)]`` is a list of actions used in order.

    ``drop`` raises before B sees the request; ``lose`` lets B handle it and then loses B's answer;
    ``missing`` answers 404 as a Faxbot without transfers would; ``down`` drops every request.
    """

    def __init__(self, client):
        self.client = client
        self.plan = {}
        self.calls = []
        self.down = False
        self.before = {}

    def script(self, method, prefix, *actions):
        self.plan.setdefault((method, prefix), []).extend(actions)

    async def request(self, method, url, **kwargs):
        assert url.startswith('https://testserver/')
        path = url[len('https://testserver'):]
        if self.down:
            raise httpx.ConnectTimeout('partner unreachable')
        action = None
        for (want, prefix), actions in self.plan.items():
            if want == method and path.startswith(prefix) and actions:
                action = actions.pop(0)
                break
        if action == 'drop':
            raise httpx.ReadTimeout('request lost before reaching the partner')
        if action == 'missing':
            return 404, {'detail': 'Not found.'}
        hook = self.before.get((method, path))
        if hook is not None:
            hook()
        self.calls.append((method, path))
        response = await asyncio.to_thread(self.client.request, method, path, **kwargs)
        if action == 'lose':
            raise httpx.ReadTimeout('answer lost after the partner received it')
        return response.status_code, response.json()

    def count(self, method, prefix):
        return sum(1 for m, p in self.calls if m == method and p.startswith(prefix))


async def _no_pause(seconds):
    return None


@pytest.fixture
def pieces(pair):
    partner = Partner(pair['b_client'])
    a = pair['a']
    a.http = partner
    a.transfer_threshold = 0
    a.transfer_piece_size = 1024
    a.transfer_pause = _no_pause
    return {**pair, 'partner': partner}


def b_store():
    return TransferStore(main.app.state.configuration_runtime.manager.store.engine)


def _sender_row(pieces, job):
    attempt = pieces['delivery'].get(job)['attempt_id']
    return attempt, TransferStore(pieces['a'].store.engine).find('sender', attempt)


@pytest.mark.asyncio
async def test_a_large_document_goes_in_pieces_and_is_committed_once(pieces):
    original = big_pdf()
    job = accept(pieces, original)
    routed, conventional = transport(pieces)
    assert await OutboundWorker(pieces['delivery'], routed).step() is True
    assert pieces['delivery'].get(job)['state'] == 'success' and conventional.submissions == 0
    attempt, sent = _sender_row(pieces, job)
    partner = pieces['partner']
    assert sent['state'] == 'committed' and sent['pieces'] > 3
    assert partner.count('POST', '/direct/transfers') == 2  # the preflight and the commit
    assert partner.count('PUT', f'/direct/transfers/{attempt}/pieces/') == sent['pieces']
    assert partner.count('POST', '/direct/deliveries') == 0
    assert not Path(sent['folder']).exists()  # the sealed copy is gone once the partner has it
    received = b_store().find('receiver', attempt)
    assert received['state'] == 'committed' and not Path(received['folder']).exists()
    assert stored_document(pieces['b_client']) == [original] and len(b_items(pieces['b_client'])) == 1
    assert pieces['a'].store.find('outbound', attempt)['state'] == 'accepted'


@pytest.mark.asyncio
async def test_a_preflight_refusal_comes_before_any_document_byte(pieces, monkeypatch):
    from app import conversion  # installation B runs the application's own modules
    job = accept(pieces, big_pdf('too long'))
    # This installation takes fewer pages than the document says it has.
    monkeypatch.setattr(conversion, 'MAX_DOCUMENT_PAGES', 0)
    routed, conventional = transport(pieces)
    await OutboundWorker(pieces['delivery'], routed).step()
    row = pieces['delivery'].get(job)
    # Refused at the preflight: nothing was accepted, so the fax route sends it in the same attempt.
    assert row['state'] == 'in_progress' and conventional.submissions == 1
    assert pieces['routes'].decision(row['attempt_id'])['route'] == 'phaxio'
    assert pieces['partner'].count('PUT', '/direct/transfers/') == 0
    _, sent = _sender_row(pieces, job)
    assert sent['state'] == 'refused' and 'more than 0 pages' in sent['detail']
    assert b_items(pieces['b_client']) == []


@pytest.mark.asyncio
async def test_after_a_drop_only_the_missing_pieces_go_again(pieces):
    original = big_pdf('drop')
    job = accept(pieces, original)
    partner = pieces['partner']
    # The third piece never arrives; B keeps the fourth but its answer is lost.
    partner.script('PUT', '/direct/transfers/', None, None, 'drop', 'lose')
    routed, conventional = transport(pieces)
    assert await OutboundWorker(pieces['delivery'], routed).step() is True
    assert pieces['delivery'].get(job)['state'] == 'success' and conventional.submissions == 0
    attempt, sent = _sender_row(pieces, job)
    reached = [path for method, path in partner.calls if method == 'PUT']
    # Every piece reached B exactly once: the lost answer's piece was not sent again.
    assert sorted(reached) == sorted(set(reached)) and len(reached) == sent['pieces']
    assert partner.count('GET', f'/direct/transfers/{attempt}') >= 1  # it asked what B holds
    assert stored_document(pieces['b_client']) == [original] and len(b_items(pieces['b_client'])) == 1


@pytest.mark.asyncio
async def test_a_transfer_cut_off_is_resumed_by_the_reconciler_never_resent(pieces):
    original = big_pdf('resume')
    job = accept(pieces, original)
    partner = pieces['partner']
    partner.script('PUT', '/direct/transfers/', None, None, 'drop', 'drop', 'drop', 'drop', 'drop', 'drop',
                   'drop', 'drop')
    partner.script('GET', '/direct/transfers/', 'drop', 'drop', 'drop')
    routed, conventional = transport(pieces)
    await OutboundWorker(pieces['delivery'], routed).step()
    assert pieces['delivery'].get(job)['state'] == 'reconciliation_required' and conventional.submissions == 0
    attempt, sent = _sender_row(pieces, job)
    assert sent['state'] == 'open' and Path(sent['folder']).is_file()
    assert b_items(pieces['b_client']) == []
    partner.plan.clear()
    before = [path for method, path in partner.calls if method == 'PUT']
    assert await DirectReconciler(pieces['a'], pieces['delivery']).reconcile(
        pieces['a'].store.awaiting_partner()[0]) == 'accepted'
    assert pieces['delivery'].get(job)['state'] == 'success'
    after = [path for method, path in partner.calls if method == 'PUT'][len(before):]
    assert not set(before) & set(after)  # only the pieces B did not hold went again
    assert partner.count('POST', '/direct/deliveries') == 0
    assert stored_document(pieces['b_client']) == [original] and len(b_items(pieces['b_client'])) == 1


@pytest.mark.asyncio
async def test_a_commit_happens_only_when_every_hash_matches(pieces):
    original = big_pdf('corrupt')
    job = accept(pieces, original)
    partner = pieces['partner']
    corrupted = []

    def corrupt():
        # A stored piece goes bad on B's disk before the commit; B forgets it and asks for it again.
        if corrupted:
            return
        row = b_store().recent()[0]
        piece = Path(row['folder']) / '000001.piece'
        data = bytearray(piece.read_bytes())
        data[0] ^= 0xFF
        piece.write_bytes(bytes(data))
        corrupted.append(row['message_id'])
    routed, _ = transport(pieces)
    attempt_hook = {}

    original_request = partner.request

    async def watching(method, url, **kwargs):
        path = url[len('https://testserver'):]
        if method == 'POST' and path.endswith('/commit') and not attempt_hook:
            attempt_hook['done'] = True
            corrupt()
        return await original_request(method, url, **kwargs)
    partner.request = watching
    assert await OutboundWorker(pieces['delivery'], routed).step() is True
    assert pieces['delivery'].get(job)['state'] == 'success'
    attempt = pieces['delivery'].get(job)['attempt_id']
    puts = [path for method, path in partner.calls if method == 'PUT']
    assert puts.count(f'/direct/transfers/{attempt}/pieces/1') == 2  # asked for again by sequence and hash
    assert partner.count('POST', f'/direct/transfers/{attempt}/commit') == 2
    assert stored_document(pieces['b_client']) == [original]


def _signed(identity, text):
    moment = timestamp()
    return moment, {'X-Faxbot-Direct-Key': identity.signing_key, 'X-Faxbot-Direct-Time': moment,
                    'X-Faxbot-Direct-Signature': identity.sign(text.replace('{time}', moment).encode('ascii'))}


@pytest.mark.asyncio
async def test_duplicate_pieces_and_commits_are_suppressed_and_a_wrong_piece_is_refused(pieces):
    from api.app.direct.crypto import canonical
    from api.app.direct.transfer import TransferSender
    a, client = pieces['a'], pieces['b_client']
    identity, peer = a.identity(), pieces['b_on_a']
    from api.app.direct.crypto import seal
    from api.tests.test_direct_delivery import A_NUMBER, B_NUMBER
    message = 'd' * 32
    manifest, signature, ciphertext = seal(
        identity, message_id=message, organization='Valley Hospital', fax_number=A_NUMBER, recipient_number=B_NUMBER,
        recipient_signing_key=peer['signing_key'], recipient_exchange_key=peer['exchange_key'],
        document=big_pdf('duplicates'), pages=1)
    row = TransferSender(a).stage(identity, peer, message, manifest, signature, ciphertext)
    opened = client.post('/direct/transfers', json={'manifest': row['manifest'], 'signature': row['signature'],
                                                    'offer': row['offer'], 'offer_signature': row['offer_signature']})
    assert opened.status_code == 200
    import json
    hashes = json.loads(row['offer'])['pieces']

    def put(sequence, data, digest=None):
        digest = digest or hashlib.sha256(data).hexdigest()
        offset = sequence * 1024
        _, headers = _signed(identity, f'PUT /direct/transfers/{message}/pieces/{sequence} {digest} {offset} {{time}}')
        return client.put(f'/direct/transfers/{message}/pieces/{sequence}', content=data,
                          headers={**headers, 'Upload-Offset': str(offset), 'X-Faxbot-Piece-Sha256': digest})
    first = ciphertext[:1024]
    assert '"status":"stored"' in put(0, first).json()['statement']
    assert '"status":"duplicate"' in put(0, first).json()['statement']
    wrong = put(1, b'x' * 1024, digest=hashes[1])
    assert wrong.status_code == 422 and '"reason":"piece_mismatch"' in wrong.json()['statement']
    assert set(b_store().held(b_store().find('receiver', message)['id'])) == {0}
    for sequence in range(1, len(hashes)):
        assert put(sequence, ciphertext[sequence * 1024:(sequence + 1) * 1024]).status_code == 200
    statement = canonical({'type': 'commit', 'message_id': message,
                           'ciphertext_sha256': json.loads(row['manifest'])['encryption']['ciphertext_sha256'],
                           'signer': identity.signing_key, 'recipient': peer['signing_key']})
    body = {'statement': statement.decode('ascii'), 'signature': identity.sign(statement)}
    committed = client.post(f'/direct/transfers/{message}/commit', json=body)
    again = client.post(f'/direct/transfers/{message}/commit', json=body)
    assert committed.status_code == again.status_code == 200 and committed.json() == again.json()
    assert len(b_items(client)) == 1
    # The preflight of a message already delivered answers with its receipt: nothing is sent again.
    reopened = client.post('/direct/transfers', json={'manifest': row['manifest'], 'signature': row['signature'],
                                                      'offer': row['offer'], 'offer_signature': row['offer_signature']})
    assert reopened.status_code == 200 and reopened.json() == committed.json()


@pytest.mark.asyncio
async def test_asking_about_pieces_never_fences_the_document(pieces):
    a, client = pieces['a'], pieces['b_client']
    identity = a.identity()
    message = 'e' * 32
    _, headers = _signed(identity, f'GET /direct/transfers/{message} {{time}}')
    answer = client.get(f'/direct/transfers/{message}', headers=headers)
    assert answer.status_code == 200 and '"status":"unknown"' in answer.json()['statement']
    stranger = client.get(f'/direct/transfers/{message}', headers={**headers, 'X-Faxbot-Direct-Signature': 'A' * 86})
    assert stranger.status_code == 403
    engine = main.app.state.configuration_runtime.manager.store.engine
    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM direct_deliveries WHERE message_id = :m"),
                                  {'m': message}).scalar() == 0


@pytest.mark.asyncio
async def test_a_partner_without_transfers_gets_the_whole_document_in_one_request(pieces):
    original = big_pdf('older partner')
    job = accept(pieces, original)
    pieces['partner'].script('POST', '/direct/transfers', 'missing')
    routed, conventional = transport(pieces)
    assert await OutboundWorker(pieces['delivery'], routed).step() is True
    assert pieces['delivery'].get(job)['state'] == 'success' and conventional.submissions == 0
    assert pieces['partner'].count('POST', '/direct/deliveries') == 1
    assert pieces['partner'].count('PUT', '/direct/transfers/') == 0
    assert stored_document(pieces['b_client']) == [original]


def test_unfinished_transfers_are_stopped_and_fenced(pieces):
    from datetime import timedelta
    from api.app.direct.service import DirectService
    from api.app.direct.transfer import TransferReceiver
    from api.app.routing.database import utcnow
    import json as _json
    from api.app.direct.crypto import seal
    from api.app.direct.transfer import TransferSender
    from api.tests.test_direct_delivery import A_NUMBER, B_NUMBER
    a, client = pieces['a'], pieces['b_client']
    identity, peer = a.identity(), pieces['b_on_a']
    message = 'f' * 32
    manifest, signature, ciphertext = seal(
        identity, message_id=message, organization='Valley Hospital', fax_number=A_NUMBER, recipient_number=B_NUMBER,
        recipient_signing_key=peer['signing_key'], recipient_exchange_key=peer['exchange_key'],
        document=big_pdf('left open'), pages=1)
    row = TransferSender(a).stage(identity, peer, message, manifest, signature, ciphertext)
    assert client.post('/direct/transfers', json={'manifest': row['manifest'], 'signature': row['signature'],
                                                  'offer': row['offer'],
                                                  'offer_signature': row['offer_signature']}).status_code == 200
    from api.app.direct.http import service_for
    service = service_for(main.app)
    TransferReceiver(service).expire(now=utcnow() + timedelta(days=1))
    assert b_store().find('receiver', message)['state'] == 'abandoned'
    # A late single delivery of the same message is refused: the stopped transfer left a fence.
    late = client.post('/direct/deliveries', files={'manifest': (None, manifest), 'signature': (None, signature),
                                                    'document': ('d', ciphertext, 'application/octet-stream')})
    assert late.status_code == 409 and '"reason":"withdrawn"' in late.json()['statement']
    assert _json.loads(row['offer'])['message_id'] == message
