"""Repair a broken fax call to an enrolled partner: only the missing pages, assembled into one fax there.

A long fax to an enrolled partner breaks at page 7 of 10. Today the fax
fails as "partly sent" and waits for a person, because a full resend would
give the recipient pages 1 to 6 twice. With an enrolled partner Faxbot can
know what arrived instead of guessing:

1. **The question.** Faxbot asks the partner, signed, which pages of that call
   it holds intact, naming the call by its start and end time, the calling
   number and the fax's page count (``POST /direct/calls/pages``).
2. **The answer.** The partner finds the call's received fax (the same calling
   number, within minutes of the call) and counts its leading pages that
   arrived whole: every page the engine confirmed, but on a call without error
   correction (ECM) a page the engine marked with damaged lines (TIFF
   ``BadFaxLines``) counts as missing, and so does every page after it. It
   answers with a signed statement, and keeps the offer.
3. **Only the rest.** When the partner holds some but not all pages, Faxbot
   sends only the remaining pages over the direct path, as a fax image (the
   exact pages a call would carry). The partner files one received fax made of
   the call's pages and those pages, once: the delivery's message ID is bound to
   the offer, and a message is recorded once. If it holds every page, nothing
   is sent at all. When the partner cannot confirm (not found, not reachable,
   not taking fax images), nothing changes: the fax waits for a person, as
   before.

This is never a blind resend (C1): pages go again only after the partner's
signed statement says they are missing, and only to a partner verified by the
challenge fax, and the receiver answers only a partner it enrolled (as it
accepts documents only from one).

What is not done here: the sent fax's own record stays "partly sent", because
changing a finished delivery belongs to the delivery store; the repair, its
pages and the partner's statement are shown beside it (Sent details, Delivery
routes → Partners, ``faxbot recipients partners repairs``).
"""
from datetime import timedelta
import io
import json
import logging
from pathlib import Path
import re
import secrets
from uuid import uuid4

import httpx
import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..routing.database import read_connection, reflect, utcnow, write_transaction
from .crypto import DirectProtocolError, canonical, check_signed, parse_timestamp, signed, timestamp, verify


WINDOW = timedelta(minutes=15)  # a call's received fax is looked for this close to the call (clocks differ)
REPAIR_DAYS = 7  # a broken call older than this is left to a person
QUESTION_FRESHNESS = timedelta(hours=24)
# How long the partner's side keeps an offer open for the missing pages. The sender sends them at once after the
# answer, and a lost answer is settled within minutes, so an older offer will never be completed.
OFFER_LIFETIME = timedelta(hours=24)
BACKOFF_FIRST = timedelta(minutes=2)
BACKOFF_MAX = timedelta(hours=6)
# Broken calls whose partner could not be reached: when to ask again, and how many tries so far (this process).
_BACKOFF = {}
BAD_LINES_TAG = 326  # TIFF BadFaxLines: damaged lines on a received page (written by HylaFAX and spandsp)
KIND = 'repair'  # the sender's delivery record of the missing pages
STATE_TEXT = {
    'confirmed': 'Sending the missing pages directly.',
    'sent': 'Sending the missing pages directly.',
    'completed': 'Completed: the missing pages went directly and {partner} holds the whole fax.',
    'offered': 'Waiting for the missing pages from {partner}.',
    'expired': '{partner} could not confirm which pages arrived, so the fax waits for you.',
}
# The partner's side of an offer that was never completed (the sender sent no pages).
RECEIVER_EXPIRED = '{partner} did not send the missing pages, so the fax stays as the call brought it.'


def _stamp(moment):
    """A stored time (naive UTC, as the database keeps it) as a signed statement writes it."""
    from datetime import timezone
    return timestamp(moment.replace(tzinfo=timezone.utc))


class RepairStore:
    """``direct_call_repairs`` (0046), and the call records, attempts and received faxes a repair reads."""

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, ('direct_call_repairs', 'sip_call_records', 'outbound_attempts',
                                  'outbound_deliveries', 'fax_jobs', 'inbound_faxes', 'inbound_imports'))
        self.repairs, self.calls = tables['direct_call_repairs'], tables['sip_call_records']
        self.attempts, self.deliveries = tables['outbound_attempts'], tables['outbound_deliveries']
        self.jobs, self.inbound, self.imports = tables['fax_jobs'], tables['inbound_faxes'], tables['inbound_imports']

    def find(self, role, repair_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.repairs).where(
                self.repairs.c.role == role, self.repairs.c.repair_id == repair_id)).mappings().first()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def for_message(self, role, message_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.repairs).where(
                self.repairs.c.role == role, self.repairs.c.message_id == message_id)).mappings().first()
        return dict(row) if row is not None else None

    def for_attempt(self, attempt_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.repairs).where(
                self.repairs.c.role == 'sender', self.repairs.c.attempt_id == attempt_id)).mappings().first()
        return dict(row) if row is not None else None

    def record(self, values, *, now=None):
        """A repair on this side, created once per repair ID; returns (row, created)."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            existing = self.find(values['role'], values['repair_id'], connection)
            if existing is not None:
                return existing, False
            connection.execute(self.repairs.insert().values(id=uuid4().hex, created_at=now, updated_at=now, **values))
            return self.find(values['role'], values['repair_id'], connection), True

    def update(self, row_id, **values):
        with write_transaction(self.engine) as connection:
            connection.execute(self.repairs.update().where(self.repairs.c.id == row_id).values(
                **values, updated_at=utcnow()))

    def expire_offers(self, *, now=None, message_id=None, peer_id=None):
        """Close this side's offers for missing pages that will never come; how many were closed.

        With ``message_id`` and ``peer_id``: that partner's offer whose pages it has said, by asking for that
        message, it never sent (the question was answered "not received"); a partner closes only its own offers.
        Without them: every offer older than ``OFFER_LIFETIME``.
        """
        now = now or utcnow()
        r = self.repairs
        which = (sa.and_(r.c.message_id == message_id, r.c.peer_id == peer_id) if message_id is not None
                 else r.c.created_at < now - OFFER_LIFETIME)
        with write_transaction(self.engine) as connection:
            return connection.execute(r.update().where(r.c.role == 'receiver', r.c.state == 'offered', which)
                                      .values(state='expired', updated_at=now)).rowcount

    def broken_calls(self, *, now=None, limit=20):
        """Sent faxes that failed part way through a call (``partly_sent``) recently, not yet asked about.

        Each comes with its call record: the start and end, the calling number and the pages confirmed sent."""
        now = now or utcnow()
        a, d, c, j, r = self.attempts, self.deliveries, self.calls, self.jobs, self.repairs
        asked = sa.exists(sa.select(1).where(r.c.role == 'sender', r.c.attempt_id == a.c.id))
        query = (sa.select(a.c.id.label('attempt_id'), a.c.job_id, j.c.to_number, j.c.pages.label('total_pages'),
                           c.c.caller, c.c.called, c.c.started_at, c.c.answered_at, c.c.ended_at,
                           c.c.pages.label('pages_sent'))
                 .select_from(a.join(d, d.c.attempt_id == a.c.id).join(j, j.c.id == a.c.job_id)
                              .join(c, sa.and_(c.c.attempt_id == a.c.id, c.c.direction == 'outbound')))
                 .where(d.c.state == 'failed', a.c.error_category == 'partly_sent', c.c.pages >= 1,
                        c.c.started_at >= now - timedelta(days=REPAIR_DAYS), ~asked)
                 .order_by(c.c.started_at).limit(limit))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def call_fax(self, caller, started, ended, *, exclude=()):
        """The received fax of a call from ``caller`` between ``started`` and ``ended`` (with the window)."""
        from ..engine_frames import same_number
        f = self.inbound
        moment = sa.func.coalesce(f.c.received_at, f.c.created_at)
        used = sa.exists(sa.select(1).where(self.repairs.c.role == 'receiver', self.repairs.c.inbound_id == f.c.id))
        with read_connection(self.engine) as connection:
            rows = [dict(row) for row in connection.execute(
                sa.select(f).where(moment >= started - WINDOW, moment <= ended + WINDOW, f.c.tiff_path.is_not(None),
                                   ~used).order_by(moment)).mappings()]
        rows = [row for row in rows if same_number(row['from_number'], caller) and row['id'] not in exclude]
        return min(rows, key=lambda row: abs(((row['received_at'] or row['created_at']) - started).total_seconds()),
                   default=None)

    def reported_pages(self, inbound_id):
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(self.imports.c.reported_pages).where(
                self.imports.c.inbound_fax_id == inbound_id)).scalar()

    def call_key(self, inbound_id):
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(self.imports.c.operation_id).where(
                self.imports.c.inbound_fax_id == inbound_id, self.imports.c.source == 'sip')).scalar()

    def recent(self, *, limit=100):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.repairs).order_by(
                self.repairs.c.created_at.desc()).limit(limit)).mappings()]


def _ecm(engine, call_key):
    """Whether the call used error correction, from its last DCS (T.30 bit 27), or None when not known."""
    if not call_key:
        return None
    try:
        from ..engine_frames import FrameStore, _bit, _octets
        frames = FrameStore(engine)
        with frames.engine.connect() as connection:
            row = connection.execute(sa.select(frames.frames.c.dcs_last).where(
                frames.frames.c.id == f'in:{call_key}')).first()
    except Exception:
        return None
    frame = _octets(row.dcs_last) if row is not None else b''
    return _bit(frame[3:], 27) if len(frame) > 3 else None


def held_pages(data, *, reported=None, ecm=None):
    """(leading pages held intact, pages with damaged lines) of a received fax image.

    Without error correction (or when it is not known), the first page with
    damaged lines and every page after it count as missing."""
    from PIL import Image
    damaged = []
    with Image.open(io.BytesIO(data)) as image:
        count = getattr(image, 'n_frames', 1)
        for index in range(count):
            image.seek(index)
            bad = image.tag_v2.get(BAD_LINES_TAG)
            bad = bad[0] if isinstance(bad, tuple) else bad
            if ecm is not True and isinstance(bad, int) and bad > 0:
                damaged.append(index + 1)
    held = count if reported is None else min(count, max(0, int(reported)))
    if damaged:
        held = min(held, damaged[0] - 1)
    return held, damaged


def _fine_page(frame):
    """One page at the fine resolution and the call width, so pages from both sources form one image."""
    from PIL import Image
    from .faximage import _fit_width
    dpi = frame.info.get('dpi') or (204, 196)
    page = frame.convert('1')
    if float(dpi[1]) < 150:
        page = page.resize((page.size[0], page.size[1] * 2), Image.NEAREST)
    return _fit_width(page)


def assemble(call_image, missing_image, held):
    """One fax image: the call's first ``held`` pages, then the pages delivered directly (G4, fine)."""
    from PIL import Image
    pages = []
    for data, limit in ((call_image, held), (missing_image, None)):
        with Image.open(io.BytesIO(data)) as image:
            count = getattr(image, 'n_frames', 1)
            for index in range(count if limit is None else min(limit, count)):
                image.seek(index)
                frame = image.copy()
                frame.info['dpi'] = image.info.get('dpi', (204, 196))
                pages.append(_fine_page(frame))
    output = io.BytesIO()
    pages[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=pages[1:], dpi=(204, 196))
    return output.getvalue(), len(pages)


def slice_pages(data, first):
    """The fax image's pages from ``first`` (0-based) on, with its own resolution and compression."""
    from PIL import Image
    pages, dpi = [], (204, 196)
    with Image.open(io.BytesIO(data)) as image:
        dpi = image.info.get('dpi', dpi)
        for index in range(first, getattr(image, 'n_frames', 1)):
            image.seek(index)
            pages.append(image.convert('1') if image.mode != '1' else image.copy())
    output = io.BytesIO()
    pages[0].save(output, 'TIFF', compression='group4', save_all=True, append_images=pages[1:],
                  dpi=(round(float(dpi[0])), round(float(dpi[1]))))
    return output.getvalue(), len(pages)


class CallRepair:
    def __init__(self, service):
        self.service = service
        self.store = RepairStore(service.store.engine)

    # Receiving: the partner's side ----------------------------------------------------------------
    def answer(self, statement, signature, *, now=None):
        """Which leading pages of a broken call this installation holds intact, signed; the offer is kept."""
        now = now or utcnow()
        service = self.service
        _, identity = service._enabled_identity()
        refused = (400, {'detail': 'This question could not be checked.'})
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
        except (AttributeError, UnicodeEncodeError, ValueError):
            return refused
        keys = {'type', 'repair_id', 'message_id', 'caller', 'called', 'started_at', 'ended_at', 'pages_sent',
                'total_pages', 'signer', 'recipient', 'created_at'}
        if (not isinstance(body, dict) or set(body) != keys or body['type'] != 'call_query'
                or body['recipient'] != identity.signing_key
                or not all(isinstance(body[name], str) and re.fullmatch(r'[a-f0-9]{32}', body[name])
                           for name in ('repair_id', 'message_id'))
                or type(body['total_pages']) is not int or not 0 < body['total_pages'] <= 10000
                or type(body['pages_sent']) is not int or not 0 <= body['pages_sent'] <= body['total_pages']
                or not isinstance(body['caller'], str) or not isinstance(body['called'], str)):
            return refused
        peer = service.store.peer_by_key(body['signer']) if isinstance(body['signer'], str) else None
        if peer is None or peer['state'] == 'revoked':
            return 403, {'detail': 'This installation does not answer this sender.'}
        try:
            verify(peer['signing_key'], encoded, signature)
            started, ended = parse_timestamp(body['started_at']), parse_timestamp(body['ended_at'])
            if abs(parse_timestamp(body['created_at']) - now) > QUESTION_FRESHNESS or ended < started:
                return refused
        except DirectProtocolError:
            return refused
        existing = self.store.find('receiver', body['repair_id'])
        if existing is not None:
            if existing['peer_id'] != peer['id'] or existing['message_id'] != body['message_id']:
                return 409, service._refusal(identity, body['message_id'], 'replay',
                                             'This repair was already asked about for another document.', peer)
            return 200, {'statement': existing['statement'], 'signature': existing['signature']}
        # Only a call from the partner's own enrolled number to this installation's number: a partner can never
        # ask about, or add pages to, a fax someone else sent here. A caller ID other than the enrolled number
        # is "not found", and that fax waits for a person as before.
        from ..engine_frames import same_number
        from ..routing.numbers import InvalidNumber, normalize_number
        values = service.values()
        try:
            own = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except InvalidNumber:
            own = None
        fax = None
        if same_number(body['caller'], peer['phone_number']) and own and same_number(body['called'], own):
            fax = self.store.call_fax(peer['phone_number'], started, ended)
        held, ecm, damaged = 0, None, []
        if fax is not None:
            try:
                data = Path(fax['tiff_path']).read_bytes()
                ecm = _ecm(self.store.engine, self.store.call_key(fax['id']))
                held, damaged = held_pages(data, reported=self.store.reported_pages(fax['id']), ecm=ecm)
            except (OSError, ValueError):
                held = 0
        held = min(held, body['total_pages'])
        found = fax is not None and held > 0
        answer = {'type': 'call_pages', 'repair_id': body['repair_id'], 'message_id': body['message_id'],
                  'status': 'found' if found else 'not_found', 'pages_held': held if found else 0,
                  'total_pages': body['total_pages'], 'ecm': ecm, 'damaged_pages': damaged,
                  'answered_at': _stamp(now)}
        envelope = signed(identity, answer)
        if found:
            self.store.record({'role': 'receiver', 'repair_id': body['repair_id'], 'peer_id': peer['id'],
                               'message_id': body['message_id'], 'inbound_id': fax['id'], 'caller': body['caller'],
                               'call_started_at': started, 'total_pages': body['total_pages'], 'pages_held': held,
                               'ecm': None if ecm is None else int(bool(ecm)),
                               'state': 'completed' if held == body['total_pages'] else 'offered',
                               'statement': envelope['statement'], 'signature': envelope['signature']}, now=now)
        return 200, envelope

    def offered(self, peer, message_id):
        """The open offer a fax image arriving as ``message_id`` completes, or None."""
        row = self.store.for_message('receiver', message_id)
        if row is None or row['peer_id'] != peer['id'] or row['state'] != 'offered':
            return None
        return row

    def complete(self, row, delivered, folder):
        """Assemble the call's pages and the pages delivered directly into one fax image; its path."""
        fax = self.store.inbound
        with read_connection(self.store.engine) as connection:
            path = connection.execute(sa.select(fax.c.tiff_path).where(fax.c.id == row['inbound_id'])).scalar()
        data, pages = assemble(Path(path).read_bytes(), delivered, row['pages_held'])
        target = Path(folder) / f"{row['message_id']}-whole-{secrets.token_hex(4)}.tiff"
        from .transfer import _write_private
        _write_private(target, data)
        self.store.update(row['id'], state='completed', assembled_path=str(target))
        return str(target), pages

    # Sending: our side ----------------------------------------------------------------------------
    async def ask(self, identity, peer, call, *, repair_id, message_id, total_pages):
        """Ask the partner what it holds of a broken call; its signed answer (a dict), or None if it cannot say."""
        from .service import PartnerUnreachable
        moment = lambda value: _stamp(value) if value is not None else timestamp()  # noqa: E731
        started = call['answered_at'] or call['started_at']
        statement = canonical({'type': 'call_query', 'repair_id': repair_id, 'message_id': message_id,
                               'caller': call['caller'] or '', 'called': call['called'] or peer['phone_number'],
                               'started_at': moment(started), 'ended_at': moment(call['ended_at'] or started),
                               'pages_sent': min(int(call['pages_sent'] or 0), total_pages),
                               'total_pages': total_pages, 'signer': identity.signing_key,
                               'recipient': peer['signing_key'], 'created_at': timestamp()})
        try:
            status, body = await self.service.http.request('POST', peer['endpoint_url'] + '/direct/calls/pages', json={
                'statement': statement.decode('ascii'), 'signature': identity.sign(statement)})
            answer = check_signed(body, peer['signing_key'])
        except (PartnerUnreachable, httpx.HTTPError, DirectProtocolError):
            return None
        if (status != 200 or answer.get('type') != 'call_pages' or answer.get('repair_id') != repair_id
                or answer.get('message_id') != message_id or type(answer.get('pages_held')) is not int
                or not 0 <= answer['pages_held'] <= total_pages):
            return None
        return {**answer, 'envelope': body}

    def _image(self, values, job_id):
        from . import faximage
        return faximage.build(values, job_id, encoded=faximage.encoded_send(self.store.engine, job_id))

    async def repair(self, call):
        """Ask about one broken call and send only the missing pages; the repair's state."""
        from .service import _DirectSubmission
        from .crypto import seal
        from .faximage import FaxImageUnavailable
        service = self.service
        values = await run_lifecycle_step(service.values)
        if not values.direct_delivery_enabled:
            return None
        peer = await run_lifecycle_step(lambda: service.store.verified_peer_for(call['to_number']))
        if peer is None or not peer.get('partner_receives_fax_images'):
            return None  # Not an enrolled partner that takes fax images: the fax waits for a person, as before.
        moment = utcnow()
        if _BACKOFF.get(call['attempt_id'], (moment, 0))[0] > moment:
            return None  # The partner could not be reached lately; asked again after a pause.
        identity = await run_lifecycle_step(service.identity)
        image = None
        total = call.get('total_pages')
        if not isinstance(total, int) or total < 1:
            # The fax's page count is not recorded: count the pages of the image that would go.
            try:
                image = await run_lifecycle_step(lambda: self._image(values, call['job_id']))
            except FaxImageUnavailable:
                return None
            total = image.pages
        repair_id, message_id = uuid4().hex, uuid4().hex
        answer = await self.ask(identity, peer, call, repair_id=repair_id, message_id=message_id, total_pages=total)
        if answer is None:
            # Not reachable or no answer: asked again later, with a growing pause (2 minutes up to 6 hours).
            _, tries = _BACKOFF.get(call['attempt_id'], (moment, 0))
            _BACKOFF[call['attempt_id']] = (moment + min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** tries), tries + 1)
            return None
        _BACKOFF.pop(call['attempt_id'], None)
        held = answer['pages_held'] if answer.get('status') == 'found' else 0
        if 0 < held < total and image is None:
            # Built only now, once the partner confirmed it holds part of the call.
            try:
                image = await run_lifecycle_step(lambda: self._image(values, call['job_id']))
            except FaxImageUnavailable:
                image = None
            if image is None or image.pages != total:
                held = 0  # The pages would not line up with the call's: nothing is sent; the fax waits for you.
        state = 'completed' if held == total else 'confirmed' if held > 0 else 'expired'
        envelope = answer['envelope']
        row, created = await run_lifecycle_step(lambda: self.store.record({
            'role': 'sender', 'repair_id': repair_id, 'peer_id': peer['id'], 'job_id': call['job_id'],
            'attempt_id': call['attempt_id'], 'message_id': message_id if 0 < held < total else None,
            'caller': call['caller'], 'call_started_at': call['answered_at'] or call['started_at'],
            'total_pages': total, 'pages_held': held,
            'ecm': None if answer.get('ecm') is None else int(bool(answer['ecm'])), 'state': state,
            'statement': envelope['statement'], 'signature': envelope['signature']}))
        if not created or state != 'confirmed':
            return state
        data, pages = slice_pages(image.data, held)
        from .faximage import check
        check(data, image.facts, pages)
        from ..routing.numbers import normalize_number
        try:
            sender_number = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except ValueError:
            sender_number = None
        manifest, signature, ciphertext = seal(
            identity, message_id=message_id, organization=values.direct_organization.strip() or 'Faxbot',
            fax_number=sender_number, recipient_number=peer['phone_number'], recipient_signing_key=peer['signing_key'],
            recipient_exchange_key=peer['exchange_key'], document=data, pages=pages, fax=image.facts)
        import hashlib
        digest = hashlib.sha256(data).hexdigest()
        await run_lifecycle_step(lambda: service.store.record_outbound(
            message_id=message_id, peer_id=peer['id'], job_id=call['job_id'], attempt_id=call['attempt_id'],
            recipient_number=peer['phone_number'], digest=digest, size=len(data), manifest=manifest.decode('ascii'),
            kind=KIND))
        await run_lifecycle_step(lambda: self.store.update(row['id'], state='sent'))
        submission = _DirectSubmission(service, peer, message_id, manifest, signature, ciphertext, digest)
        try:
            await submission.submit()
        except Exception:
            # Refused (nothing accepted) or not known yet: a later step asks the partner, never resends blindly.
            return 'sent'
        await run_lifecycle_step(lambda: self.store.update(row['id'], state='completed'))
        return 'completed'

    async def settle(self, row):
        """A repair whose pages were sent but whose answer was lost: ask the partner (this fences a no)."""
        service = self.service
        delivery = await run_lifecycle_step(lambda: service.store.find('outbound', row['message_id']))
        if delivery is None:
            return None
        if delivery['state'] == 'accepted':
            await run_lifecycle_step(lambda: self.store.update(row['id'], state='completed'))
            return 'completed'
        if delivery['state'] == 'refused':
            await run_lifecycle_step(lambda: self.store.update(row['id'], state='expired'))
            return 'expired'
        from .service import DirectReconciler

        class _NoDelivery:
            """The repaired fax keeps its own record; only the direct delivery is settled here."""
            def attempt_context(self, job_id, attempt_id):
                return None, type('P', (), {'id': None})()

            def observe(self, *args, **kwargs):
                return None

            def requeue_after_failure(self, *args, **kwargs):
                return None
        outcome = await DirectReconciler(service, _NoDelivery()).reconcile(delivery)
        if outcome == 'accepted':
            await run_lifecycle_step(lambda: self.store.update(row['id'], state='completed'))
        elif outcome == 'not_received':
            await run_lifecycle_step(lambda: self.store.update(row['id'], state='expired'))
        return outcome

    async def step(self):
        """Repair recent broken calls to enrolled partners, settle repairs whose answer was lost, and close the
        partner's side of offers whose pages never came."""
        await run_lifecycle_step(self.store.expire_offers)
        for call in await run_lifecycle_step(self.store.broken_calls):
            try:
                await self.repair(call)
            except Exception:
                logging.getLogger(__name__).warning('A broken fax call to a partner could not be repaired yet.')
        for row in await run_lifecycle_step(lambda: [r for r in self.store.recent(limit=50)
                                                     if r['role'] == 'sender' and r['state'] == 'sent']):
            try:
                await self.settle(row)
            except Exception:
                logging.getLogger(__name__).warning('A repaired fax call is confirmed with the partner later.')
        return False


def repair_view(row, organization=None):
    """One repaired call for the console and the command line, in plain words."""
    partner = organization or 'the partner'
    status = STATE_TEXT[row['state']].format(partner=partner)
    if row['role'] == 'receiver' and row['state'] == 'expired':
        status = RECEIVER_EXPIRED.format(partner=partner)
    if row['state'] in ('confirmed', 'sent', 'completed', 'offered') and row['pages_held'] < row['total_pages']:
        first = row['pages_held'] + 1
        pages = (f"page {first}" if first == row['total_pages'] else f"pages {first} to {row['total_pages']}")
        if row['role'] == 'sender':
            status = (f'{status} {partner} held the first {row["pages_held"]} of {row["total_pages"]} pages from the '
                      f'call; only {pages} went directly.')
        else:
            status = (f'{status} The call brought the first {row["pages_held"]} of {row["total_pages"]} pages; '
                      f'{pages} came directly.')
    elif row['state'] == 'completed':
        status = f'{partner} held every page from the call; nothing was sent again.'
    return {'id': row['id'], 'direction': 'outbound' if row['role'] == 'sender' else 'inbound',
            'partner': organization, 'state': row['state'], 'status': status, 'fax_id': row['job_id'] or row['inbound_id'],
            'pages_held': row['pages_held'], 'total_pages': row['total_pages'],
            'error_correction': None if row['ecm'] is None else bool(row['ecm']), 'created_at': row['created_at']}


def received_repair_text(report):
    """The Received sentence for a fax completed by a repair (filing.py)."""
    repair = report.get('call_repair') if isinstance(report.get('call_repair'), dict) else {}
    partner = report.get('partner') or 'a partner'
    held, total = repair.get('pages_from_call'), repair.get('total_pages')
    if isinstance(held, int) and isinstance(total, int):
        return (f'Pages 1 to {held} came by a fax call from {partner} that broke; pages {held + 1} to {total} were '
                'delivered directly as a fax image, and this is the whole fax.')
    return f'Completed directly by {partner} after a fax call broke.'
