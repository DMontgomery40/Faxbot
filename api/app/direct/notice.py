"""Notice fax (D3, M17b): the original goes directly, and one opaque page goes by fax for intakes that need a fax event.

Some intakes count a fax event (a fax-server log, a regulator's expectation,
an EHR fax queue). For a partner marked for it (``direct_peers.notice_fax``),
every original document still goes by encrypted direct delivery, and one page
goes by fax to the partner's number so its intake records the event. The
original is never labelled as faxed (R08 §5): only the notice page was.

**Sending.**

1. Before the document, the sender signs a notice link (``POST
   /direct/notices``): the message ID, the document's SHA-256 and a random
   20-digit notice ID. A Faxbot that cannot pair notices answers 404, and the
   fax goes by fax instead, because this partner's intake needs a fax event.
2. The original is delivered directly (staged): the partner stores it and
   answers with its signed receipt, but holds it out of Received until the
   notice fax arrives.
3. Then the notice fax is queued, under the same owner as the original fax,
   with a fax ID derived from the message (so it is never queued twice). It
   always goes by telephone (``DirectRoute`` refuses it). The page carries the
   notice ID as a Code 128 barcode and as digits, and the SSL Fax engine also
   sends it as the T.33 subaddress (SUB), which is recorded as requested, not
   as carried: HylaFAX sends SUB only when the far end says it takes one.

**Receiving.** A matcher looks at received faxes of one or two pages that
arrived after a held original, each once (``direct_notice_scans``): the SUB
first (the received TIFF's FaxSubAddress tag, or the built-in engine's SUB
frame), then the barcode. A match files the original in Received (its email,
mailbox rules and Work) and tells the sender, signed. When neither can be read,
the administrator pairs it under Delivery routes → Partners → Notices by typing
the code from the page or picking the received fax; a held original never
turns into a fax (C1).

**What the page reveals.** Only an opaque random number. It is not a bearer
secret (R05 §6.2): the original was pushed to the receiver beforehand, so
knowing the ID retrieves nothing, and the page carries no part of the document,
not even its file name.

Three records are kept on each side: the notice fax (the sender's fax job, the
receiver's received fax), the original (``direct_deliveries``) and the signed
link (``direct_notices``, with both signed statements).
"""
from datetime import timedelta, timezone
import hashlib
import io
import json
import logging
import re
import secrets
from uuid import uuid4

import httpx
import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..routing.database import read_connection, reflect, utcnow, write_transaction
from .crypto import DirectProtocolError, canonical, check_signed, parse_timestamp, signed, timestamp, verify


DIGITS = 20  # the T.33 subaddress holds up to 20 digits
SCAN_PAGES = 2  # a notice is one page; a received fax of more pages is not examined
SCAN_BACK = timedelta(minutes=10)  # received faxes this long before a held original are still examined
SCAN_LIMIT = 50
LINK_FRESHNESS = timedelta(hours=24)
WAITING_LONG = timedelta(hours=24)
FILE_NAME = 'direct-delivery-notice.pdf'
SUB_TAG = 34909  # TIFFTAG_FAXSUBADDRESS (libtiff tiff.h): HylaFAX and spandsp write the received SUB into the image
MATCHED_TEXT = {'sub': 'by its subaddress', 'barcode': 'by its barcode', 'person': 'by hand'}


class NoticeConflict(RuntimeError):
    """One plain sentence for the administrator."""


def new_notice_id():
    return f'{secrets.randbelow(10 ** DIGITS):0{DIGITS}d}'


def code_text(notice_id):
    """The notice ID as people read and type it: five groups of four digits."""
    return ' '.join(notice_id[at:at + 4] for at in range(0, len(notice_id), 4))


def normalize(text):
    digits = ''.join(character for character in str(text or '') if character.isdigit())
    return digits if len(digits) == DIGITS else None


def notice_job_id(message_id):
    """The notice fax's ID, derived from its original's message: queued once, whatever happens in between."""
    return hashlib.sha256(f'faxbot-notice|{message_id}'.encode('ascii')).hexdigest()[:32]


# -- Code 128 (set C), drawn and read here -----------------------------------------------------------

def _widths(value):
    """The bar and space widths (in modules) of one Code 128 symbol, from reportlab's table."""
    from reportlab.graphics.barcode.code128 import _patterns
    return [ord(letter.lower()) - 96 for letter in _patterns[value]]


START_C, STOP = 105, 106


def code128c(digits):
    """The symbol values for an even run of digits in set C: start, pairs, check, stop."""
    if not digits or len(digits) % 2 or not digits.isdigit():
        raise ValueError('Set C carries an even number of digits.')
    data = [int(digits[at:at + 2]) for at in range(0, len(digits), 2)]
    check = (START_C + sum(position * value for position, value in enumerate(data, start=1))) % 103
    return [START_C, *data, check, STOP]


def barcode_runs(digits):
    """(is_bar, modules) runs of the whole symbol, quiet zones excluded."""
    runs = []
    for value in code128c(digits):
        for index, width in enumerate(_widths(value)):
            runs.append((index % 2 == 0, width))
    return runs


_LOOKUP = None


def _lookup():
    global _LOOKUP
    if _LOOKUP is None:
        _LOOKUP = {tuple(_widths(value)): value for value in range(106)}
    return _LOOKUP


def _symbol(runs):
    total = sum(runs)
    if total <= 0:
        return None
    unit = total / 11.0
    widths = tuple(max(1, min(4, int(round(run / unit)))) for run in runs)
    if sum(widths) != 11:
        return None
    return _lookup().get(widths)


_DARK = bytes(1 if value < 128 else 0 for value in range(256))
_RUNS = re.compile(rb'\x01+|\x00+')


def _row_runs(row):
    """Alternating runs of one 8-bit image row: [(dark, length), ...] (the regular expression does the work)."""
    marked = bytes(row).translate(_DARK)
    return [(match.group()[0] == 1, match.end() - match.start()) for match in _RUNS.finditer(marked)]


def _is_stop(widths):
    unit = sum(widths) / 13.0
    return unit > 0 and tuple(max(1, min(4, int(round(run / unit)))) for run in widths) == tuple(_widths(STOP))


def _decode_row(runs):
    """Every set C barcode found in one row's runs (digit strings whose check symbol is right)."""
    found = []
    widths = [length for _, length in runs]
    for start in range(len(runs) - 6):
        if not runs[start][0] or _symbol(widths[start:start + 6]) != START_C:
            continue
        values, at, stopped = [], start + 6, False
        while at + 6 <= len(runs):
            if at + 7 <= len(runs) and _is_stop(widths[at:at + 7]):
                stopped = True
                break
            value = _symbol(widths[at:at + 6])
            if value is None or value > 102:
                break
            values.append(value)
            at += 6
        if not stopped or len(values) < 2:
            continue
        *data, check = values
        if (START_C + sum(position * value for position, value in enumerate(data, start=1))) % 103 != check:
            continue
        if all(value < 100 for value in data):
            found.append(''.join(f'{value:02d}' for value in data))
    return found


def read_barcode(image, *, digits=DIGITS):
    """The set C barcode of ``digits`` digits on a page image, read across many rows and voted, or None."""
    from collections import Counter
    gray = image.convert('L')
    width, height = gray.size
    data = gray.tobytes()
    votes = Counter()
    step = 2 if height < 1500 else 3
    for y in range(0, height, step):
        row = data[y * width:(y + 1) * width]
        if min(row) >= 128:
            continue
        for found in _decode_row(_row_runs(row)):
            if len(found) == digits:
                votes[found] += 1
    if not votes:
        return None
    best, count = votes.most_common(1)[0]
    return best if count >= 2 else None


# -- the notice page ----------------------------------------------------------------------------------

def notice_page(*, notice_id, sender, sender_number, recipient, recipient_number):
    """The one-page notice PDF: who to whom, a barcode and the code, and nothing of the document."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    pdf.setTitle('Notice of a document delivered directly')
    y = 720
    pdf.setFont('Helvetica-Bold', 18)
    pdf.drawString(72, y, 'Notice of a document delivered directly')
    y -= 34
    lines = [
        f'From: {sender}{f" ({sender_number})" if sender_number else ""}',
        f'To: {recipient} ({recipient_number})',
        '',
        'The document this page refers to was delivered to your Faxbot directly, over an',
        'encrypted connection. It was not faxed. This page is faxed only so that your fax',
        'intake records the delivery, and it contains no part of the document.',
    ]
    pdf.setFont('Helvetica', 12)
    for line in lines:
        pdf.drawString(72, y, line)
        y -= 18
    # The barcode: 1.5 points a module (about 4 dots across at fax resolution), 0.75 inch tall.
    module, bar_height, x = 1.5, 54, 72 + 15
    y -= 70
    for is_bar, width in barcode_runs(notice_id):
        if is_bar:
            pdf.rect(x, y, width * module, bar_height, stroke=0, fill=1)
        x += width * module
    y -= 34
    pdf.setFont('Helvetica-Bold', 22)
    pdf.drawString(72, y, f'Notice code: {code_text(notice_id)}')
    y -= 34
    pdf.setFont('Helvetica', 12)
    for line in ('Your Faxbot pairs this page with the document by itself. If it does not,',
                 'enter this code in Faxbot under Delivery routes, Partners, Notices.'):
        pdf.drawString(72, y, line)
        y -= 18
    pdf.showPage()
    pdf.save()
    return output.getvalue()


# -- storage -----------------------------------------------------------------------------------------

class NoticeStore:
    """``direct_notices`` and ``direct_notice_scans`` (0046), and the received faxes a notice may be."""

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, ('direct_notices', 'direct_notice_scans', 'inbound_faxes', 'inbound_imports',
                                  'direct_deliveries'))
        self.notices, self.scans = tables['direct_notices'], tables['direct_notice_scans']
        self.inbound, self.imports = tables['inbound_faxes'], tables['inbound_imports']
        self.deliveries = tables['direct_deliveries']

    def get(self, notice_row_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.notices).where(self.notices.c.id == notice_row_id)).mappings().first()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def find(self, role, message_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.notices).where(
                self.notices.c.role == role, self.notices.c.message_id == message_id)).mappings().first()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def for_job(self, job_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.notices).where(
                self.notices.c.role == 'sender', self.notices.c.notice_job_id == job_id)).mappings().first()
        return dict(row) if row is not None else None

    def for_fax(self, inbound_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.notices).where(
                self.notices.c.role == 'receiver', self.notices.c.inbound_id == inbound_id)).mappings().first()
        return dict(row) if row is not None else None

    def record(self, *, role, notice_id, message_id, peer_id, digest, state, statement, signature, job_id=None,
               now=None):
        """The notice for ``message_id`` on this side, created once; returns (row, created)."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            existing = self.find(role, message_id, connection)
            if existing is not None:
                return existing, False
            connection.execute(self.notices.insert().values(
                id=uuid4().hex, role=role, notice_id=notice_id, message_id=message_id, peer_id=peer_id,
                document_sha256=digest, state=state, link_statement=statement, link_signature=signature,
                job_id=job_id, created_at=now, updated_at=now))
            return self.find(role, message_id, connection), True

    def update(self, notice_row_id, connection=None, **values):
        values = {**values, 'updated_at': values.get('updated_at') or utcnow()}
        if connection is not None:
            connection.execute(self.notices.update().where(self.notices.c.id == notice_row_id).values(**values))
            return
        with write_transaction(self.engine) as conn:
            conn.execute(self.notices.update().where(self.notices.c.id == notice_row_id).values(**values))

    def waiting(self):
        """Held originals that arrived and wait for their notice fax (receiver)."""
        d, n = self.deliveries, self.notices
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                sa.select(n).select_from(n.join(d, sa.and_(d.c.message_id == n.c.message_id,
                                                           d.c.direction == 'inbound')))
                .where(n.c.role == 'receiver', n.c.state == 'waiting', d.c.state == 'accepted',
                       d.c.peer_id == n.c.peer_id)
                .order_by(n.c.created_at)).mappings()]

    def candidates(self, since, *, limit=SCAN_LIMIT):
        """Received faxes of one or two pages since ``since`` not examined yet."""
        f, s = self.inbound, self.scans
        examined = sa.exists(sa.select(1).where(s.c.inbound_id == f.c.id))
        moment = sa.func.coalesce(f.c.received_at, f.c.created_at)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                sa.select(f).where(moment >= since, ~examined,
                                   sa.or_(f.c.pages.is_(None), f.c.pages <= SCAN_PAGES))
                .order_by(moment).limit(limit)).mappings()]

    def recent_faxes(self, since, *, limit=20):
        """Received faxes of one or two pages since ``since``: what a person may pair a notice with."""
        f = self.inbound
        moment = sa.func.coalesce(f.c.received_at, f.c.created_at)
        paired = sa.exists(sa.select(1).where(self.notices.c.inbound_id == f.c.id))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                sa.select(f).where(moment >= since, ~paired, sa.or_(f.c.pages.is_(None), f.c.pages <= SCAN_PAGES))
                .order_by(moment.desc()).limit(limit)).mappings()]

    def fax(self, inbound_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.inbound).where(self.inbound.c.id == inbound_id)).mappings().first()
        return dict(row) if row is not None else None

    def scanned(self, inbound_id, *, found=None, method=None, notice_row_id=None, now=None):
        try:
            with write_transaction(self.engine) as connection:
                connection.execute(self.scans.insert().values(
                    id=uuid4().hex, inbound_id=inbound_id, found=found, method=method, notice_row_id=notice_row_id,
                    scanned_at=now or utcnow()))
        except sa.exc.IntegrityError:
            pass

    def call_key(self, inbound_id):
        """The engine's call key of a fax received over the SIP trunk (its import operation), or None."""
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(self.imports.c.operation_id).where(
                self.imports.c.inbound_fax_id == inbound_id, self.imports.c.source == 'sip')).scalar()

    def filed_original(self, row):
        """The received fax a paired original was filed as (its id), or None."""
        from .store import FILING_ACCOUNT, FILING_SOURCE
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(self.imports.c.inbound_fax_id).where(
                self.imports.c.source == FILING_SOURCE, self.imports.c.account == FILING_ACCOUNT + row['peer_id'],
                self.imports.c.operation_id == row['message_id'])).scalar()

    def untold(self, *, limit=20):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.notices).where(
                self.notices.c.role == 'receiver', self.notices.c.state == 'paired',
                self.notices.c.pairing_statement.is_not(None), self.notices.c.told_at.is_(None))
                .order_by(self.notices.c.paired_at).limit(limit)).mappings()]

    def to_queue(self, *, limit=20):
        """Sender notices whose original the partner accepted and whose notice fax is not queued yet."""
        d, n = self.deliveries, self.notices
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                sa.select(n).select_from(n.join(d, sa.and_(d.c.message_id == n.c.message_id,
                                                           d.c.direction == 'outbound')))
                .where(n.c.role == 'sender', n.c.state == 'announced', d.c.state == 'accepted')
                .order_by(n.c.created_at).limit(limit)).mappings()]

    def recent(self, *, limit=100):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.notices).order_by(
                self.notices.c.created_at.desc()).limit(limit)).mappings()]


def subaddress_for(engine, job_id):
    """The 20-digit notice ID to send as the T.33 subaddress for a notice fax, or None for any other fax."""
    try:
        row = NoticeStore(engine).for_job(job_id)
    except Exception:
        return None
    return row['notice_id'] if row is not None else None


def is_notice_job(engine, job_id):
    return subaddress_for(engine, job_id) is not None


# -- sending ------------------------------------------------------------------------------------------

def wants_notice(peer):
    value = peer.get('notice_fax')
    return value is not None and int(value) == 1


class NoticeSender:
    def __init__(self, service):
        self.service = service
        self.store = NoticeStore(service.store.engine)

    def link(self, identity, peer, *, message_id, digest, job_id):
        """This side's signed link for a message (created once): the notice ID it carries stays the same."""
        existing = self.store.find('sender', message_id)
        if existing is not None:
            return existing
        statement = canonical({'type': 'notice', 'message_id': message_id, 'notice_id': new_notice_id(),
                               'document_sha256': digest, 'signer': identity.signing_key,
                               'recipient': peer['signing_key'], 'created_at': timestamp()})
        row, _ = self.store.record(role='sender', notice_id=json.loads(statement)['notice_id'], message_id=message_id,
                                   peer_id=peer['id'], digest=digest, state='announced',
                                   statement=statement.decode('ascii'), signature=identity.sign(statement),
                                   job_id=job_id)
        return row

    async def announce(self, identity, peer, *, message_id, digest, job_id):
        """Tell the partner, signed, which notice ID goes with this document, before the document.

        Returns None when the partner recorded it; otherwise the sentence why the fax goes by fax instead
        (nothing of the document was sent)."""
        from .service import PartnerUnreachable
        row = await run_lifecycle_step(lambda: self.link(identity, peer, message_id=message_id, digest=digest,
                                                         job_id=job_id))
        name = peer['organization']
        try:
            status, body = await self.service.http.request('POST', peer['endpoint_url'] + '/direct/notices', json={
                'statement': row['link_statement'], 'signature': row['link_signature']})
        except PartnerUnreachable as error:
            return str(error) if str(error) else f'Faxbot could not reach {name}; the fax goes by fax.'
        except httpx.HTTPError:
            return f'{name} did not answer about the notice fax; the fax goes by fax.'
        if status in (404, 405):
            return f"{name}'s Faxbot cannot pair notice faxes yet, so the fax goes by fax."
        try:
            statement = check_signed(body, peer['signing_key'])
        except DirectProtocolError:
            statement = None
        if (status == 200 and statement is not None and statement.get('type') == 'notice'
                and statement.get('message_id') == message_id and statement.get('status') == 'recorded'):
            return None
        detail = statement.get('detail') if statement is not None else None
        return detail or f'{name} did not take the notice fax; the fax goes by fax.'

    def original_accepted(self, message_id):
        """The partner holds the original: queue its notice fax (once)."""
        row = self.store.find('sender', message_id)
        if row is None or row['state'] != 'announced':
            return None
        return self.queue(row)

    def cancel(self, message_id):
        """The original did not go directly (it goes by fax): no notice fax is needed."""
        row = self.store.find('sender', message_id)
        if row is not None and row['state'] == 'announced':
            self.store.update(row['id'], state='cancelled')

    def queue(self, row):
        """Accept the notice fax as a fax of the original's owner, in one transaction with its record."""
        access = self.service.access()
        if access is None:
            return None  # Faxbot is still starting; the background step queues it.
        peer = self.service.store.get_peer(row['peer_id'])
        if peer is None:
            return None
        configuration = access.outbound.configuration
        revision = configuration.read().active
        profile_id = revision.profile_id('outbound')
        if profile_id is None:
            return None
        profile = configuration.read_profile(profile_id)
        values = revision.values
        from ..routing.numbers import InvalidNumber, normalize_number
        try:
            own = normalize_number(values.direct_fax_number, country=values.fax_default_country)
        except InvalidNumber:
            own = None
        document = notice_page(notice_id=row['notice_id'], sender=values.direct_organization.strip() or 'Faxbot',
                               sender_number=own, recipient=peer['organization'], recipient_number=peer['phone_number'])
        job_id = notice_job_id(row['message_id'])
        from pathlib import Path
        import os
        folder = Path(values.fax_data_dir)
        pdf, tiff = folder / (job_id + '.pdf'), None
        needs_tiff = ((profile.configuration.manifest is None and profile.configuration.provider_id in {'sip',
                                                                                                         'freeswitch'})
                      or profile.configuration.traits.get('requires_tiff') is True)
        descriptor = os.open(pdf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(document)
        if needs_tiff:
            from ..conversion import pdf_to_tiff
            tiff = folder / (job_id + '.tiff')
            pdf_to_tiff(str(pdf), str(tiff))
        now = utcnow()
        job = {'id': job_id, 'to_number': peer['phone_number'], 'file_name': FILE_NAME,
               'tiff_path': str(tiff) if tiff else '', 'status': 'queued', 'pages': 1, 'send_by_call': 1,
               'created_at': now, 'updated_at': now}

        def also(connection, moment):
            current = self.store.get(row['id'], connection)
            if current is None or current['state'] != 'announced':
                raise _AlreadyQueued()
            self.store.update(row['id'], connection, state='queued', notice_job_id=job_id, updated_at=moment)
        try:
            _accept_as_original(access, revision, job, original_job_id=row['job_id'], also=also)
        except _AlreadyQueued:
            return job_id
        except Exception:
            current = self.store.get(row['id'])
            if current is not None and current['notice_job_id'] == job_id:
                return job_id
            raise
        return job_id

    def step(self):
        for row in self.store.to_queue():
            try:
                self.queue(row)
            except Exception:
                logging.getLogger(__name__).warning('A notice fax could not be queued yet; Faxbot tries again.')
        return False

    def note_paired(self, statement, signature, *, now=None):
        """The partner tells us, signed, that it paired our notice fax with the original."""
        now = now or utcnow()
        _, identity = self.service._enabled_identity()
        refused = (400, {'recorded': False, 'detail': 'This statement could not be checked.'})
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
        except (AttributeError, UnicodeEncodeError, ValueError):
            return refused
        if (not isinstance(body, dict) or body.get('type') != 'notice_paired' or body.get('recipient') != identity.signing_key
                or not isinstance(body.get('message_id'), str)):
            return refused
        row = self.store.find('sender', body['message_id'])
        peer = self.service.store.get_peer(row['peer_id']) if row is not None else None
        if peer is None or peer['signing_key'] != body.get('signer'):
            return refused
        try:
            verify(peer['signing_key'], encoded, signature)
        except DirectProtocolError:
            return refused
        if body.get('notice_id') != row['notice_id'] or body.get('document_sha256') != row['document_sha256']:
            return refused
        if row['state'] != 'paired':
            matched = body.get('matched_by') if body.get('matched_by') in MATCHED_TEXT else None
            self.store.update(row['id'], state='paired', matched_by=matched,
                              paired_at=_moment(body.get('paired_at')) or now, pairing_statement=statement,
                              pairing_signature=signature)
        return 200, {'recorded': True}


class _AlreadyQueued(RuntimeError):
    pass


def _moment(text):
    try:
        return parse_timestamp(text)
    except DirectProtocolError:
        return None


def _accept_as_original(access, revision, job, *, original_job_id, also):
    """Accept ``job`` under the same owner as the original fax (its outbound resource's parent), audited
    ``source: direct_notice``; ``also`` runs in the same transaction."""
    import json as _json
    configuration = access.outbound.configuration
    tables = access.store.tables
    resources, audit = tables['access_resources'], tables['access_audit']
    with configuration._locked() as connection:
        version = access.store.lock_on(connection)
        now = utcnow()
        parent = connection.execute(sa.select(resources.c.parent_id, resources.c.parent_kind).where(
            resources.c.kind == 'outbound', resources.c.fax_job_id == original_job_id)).first() if original_job_id else None
        configuration._accept_outbound_on(connection, revision, job)
        resource_id = None
        if parent is not None:
            resource_id = uuid4().hex
            connection.execute(resources.insert().values(
                id=resource_id, kind='outbound', parent_id=parent.parent_id, parent_kind=parent.parent_kind,
                principal_id=None, mailbox_id=None, fax_job_id=job['id'], inbound_fax_id=None, enabled=1, version=1,
                created_at=now, updated_at=now))
        connection.execute(audit.insert().values(
            id=uuid4().hex, actor_principal_id=None, actor_key_binding_id=None, actor_session_id=None,
            operation='fax.accept', target_kind='resource' if resource_id else 'fax', target_id=resource_id or job['id'],
            policy_version_before=version, policy_version_after=version, outcome='allowed',
            details=_json.dumps({'source': 'direct_notice', 'original': original_job_id}, ensure_ascii=True,
                                separators=(',', ':'), sort_keys=True), created_at=now))
        also(connection, now)


# -- receiving ----------------------------------------------------------------------------------------

class NoticeReceiver:
    def __init__(self, service):
        self.service = service
        self.store = NoticeStore(service.store.engine)

    def link(self, statement, signature, *, now=None):
        """A partner's signed notice link, recorded before its document: the document is then held."""
        now = now or utcnow()
        _, identity = self.service._enabled_identity()
        refused = (400, {'detail': 'This notice could not be checked.'})
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
        except (AttributeError, UnicodeEncodeError, ValueError):
            return refused
        keys = {'type', 'message_id', 'notice_id', 'document_sha256', 'signer', 'recipient', 'created_at'}
        if (not isinstance(body, dict) or set(body) != keys or body['type'] != 'notice'
                or body['recipient'] != identity.signing_key or normalize(body['notice_id']) != body['notice_id']
                or not isinstance(body['message_id'], str) or re.fullmatch(r'[a-f0-9]{32}', body['message_id']) is None
                or not isinstance(body['document_sha256'], str)
                or re.fullmatch(r'[a-f0-9]{64}', body['document_sha256']) is None):
            return refused
        peer = self.service.store.peer_by_key(body['signer']) if isinstance(body['signer'], str) else None
        if peer is None or peer['state'] == 'revoked':
            return 403, {'detail': 'This installation does not accept documents from this sender.'}
        try:
            verify(peer['signing_key'], encoded, signature)
            if abs(parse_timestamp(body['created_at']) - now) > LINK_FRESHNESS:
                return refused
        except DirectProtocolError:
            return refused
        if self.service.store.find('inbound', body['message_id']) is not None:
            return 409, self.service._refusal(identity, body['message_id'], 'too_late',
                                              'This document already arrived; its notice cannot be added now.', peer)
        row, _ = self.store.record(role='receiver', notice_id=body['notice_id'], message_id=body['message_id'],
                                   peer_id=peer['id'], digest=body['document_sha256'], state='waiting',
                                   statement=statement, signature=signature, now=now)
        if row['link_statement'] != statement:
            return 409, self.service._refusal(identity, body['message_id'], 'replay',
                                              'This message already has a different notice.', peer)
        return 200, signed(identity, {'type': 'notice', 'message_id': body['message_id'], 'status': 'recorded'})

    def held(self, peer, message_id, digest):
        """The notice an arriving original waits for (it is then held out of Received), or None."""
        row = self.store.find('receiver', message_id)
        if row is None or row['peer_id'] != peer['id'] or row['document_sha256'] != digest:
            return None
        return row

    # Recognizing the notice page ------------------------------------------------------------------
    def _image(self, fax):
        for name in ('tiff_path', 'pdf_path'):
            path = fax.get(name)
            if path:
                try:
                    with open(path, 'rb') as handle:
                        return handle.read()
                except OSError:
                    continue
        return None

    def subaddress(self, fax, data):
        """The SUB the sender gave: the received image's FaxSubAddress tag, then the built-in engine's frame."""
        if data and data[:4] in (b'II*\x00', b'MM\x00*'):
            try:
                from PIL import Image
                with Image.open(io.BytesIO(data)) as image:
                    found = normalize(image.tag_v2.get(SUB_TAG))
                if found:
                    return found
            except Exception:
                pass
        key = self.store.call_key(fax['id'])
        if key:
            try:
                from ..engine_frames import FrameStore, decode_sub
                frames = FrameStore(self.store.engine)
                with frames.engine.connect() as connection:
                    row = connection.execute(sa.select(frames.frames.c.sub).where(
                        frames.frames.c.id == f'in:{key}')).first()
                if row is not None:
                    return normalize(decode_sub(row.sub))
            except Exception:
                return None
        return None

    def read(self, fax):
        """(notice ID, how it was read) from one received fax, or (None, None)."""
        data = self._image(fax)
        found = self.subaddress(fax, data)
        if found:
            return found, 'sub'
        if data:
            from ..codec import first_page
            page = first_page(data)
            if page is not None:
                found = read_barcode(page)
                if found:
                    return found, 'barcode'
        return None, None

    def step(self, *, now=None):
        """Examine new received faxes for a notice while any original waits for one; pair what matches."""
        now = now or utcnow()
        waiting = self.store.waiting()
        if not waiting:
            return False
        by_id = {row['notice_id']: row for row in waiting}
        since = min(row['created_at'] for row in waiting) - SCAN_BACK
        for fax in self.store.candidates(since):
            try:
                found, method = self.read(fax)
            except Exception:
                found, method = None, None
            row = by_id.get(found) if found else None
            if row is not None:
                self.pair(row, fax['id'], matched_by=method, now=now)
                by_id.pop(found, None)
            self.store.scanned(fax['id'], found=found, method=method if found else None,
                               notice_row_id=row['id'] if row is not None else None, now=now)
            if not by_id:
                break
        return False

    def pair(self, row, inbound_id, *, matched_by, actor=None, actor_name=None, now=None):
        """Pair a held original with its notice fax (or with none, by a person), file it in Received, and sign."""
        now = now or utcnow()
        _, identity = self.service._enabled_identity()
        statement = canonical({'type': 'notice_paired', 'message_id': row['message_id'], 'notice_id': row['notice_id'],
                               'document_sha256': row['document_sha256'], 'matched_by': matched_by,
                               'notice_fax': bool(inbound_id), 'paired_at': timestamp(now.replace(tzinfo=timezone.utc)),
                               'signer': identity.signing_key,
                               'recipient': self.service.store.get_peer(row['peer_id'])['signing_key']})
        with write_transaction(self.store.engine) as connection:
            current = self.store.get(row['id'], connection)
            if current is None or current['state'] != 'waiting':
                raise NoticeConflict('This document is already paired with its notice.')
            if inbound_id and connection.execute(sa.select(self.store.notices.c.id).where(
                    self.store.notices.c.inbound_id == inbound_id)).first():
                raise NoticeConflict('That received fax is already paired with another document.')
            self.store.update(row['id'], connection, state='paired', inbound_id=inbound_id, matched_by=matched_by,
                              paired_by=actor, paired_by_name=(actor_name or None) and str(actor_name)[:200],
                              paired_at=now, pairing_statement=statement.decode('ascii'),
                              pairing_signature=identity.sign(statement), updated_at=now)
        delivery = self.service.store.find('inbound', row['message_id'])
        if delivery is not None and delivery['state'] == 'accepted':
            try:
                self.service.filing.file(delivery)
            except Exception:
                logging.getLogger(__name__).warning('A paired document is filed in Received shortly.')
        return self.store.get(row['id'])

    def pair_by_person(self, notice_row_id, *, code=None, fax_id=None, actor=None, actor_name=None):
        """The administrator pairs a held original: by the code typed from the page, or by the received fax."""
        row = self.store.get(notice_row_id)
        if row is None or row['role'] != 'receiver':
            raise NoticeConflict('There is no such document waiting for its notice.')
        if row['state'] != 'waiting':
            raise NoticeConflict('This document is already paired with its notice.')
        if code is not None and normalize(code) != row['notice_id']:
            raise NoticeConflict('That code is not the one on this notice; check the 20 digits on the page.')
        if fax_id is not None and self.store.fax(fax_id) is None:
            raise NoticeConflict('There is no such received fax.')
        return self.pair(row, fax_id, matched_by='person', actor=actor, actor_name=actor_name)

    async def tell(self, row):
        from .service import PartnerUnreachable
        peer = await run_lifecycle_step(lambda: self.service.store.get_peer(row['peer_id']))
        if peer is None:
            return False
        try:
            status, body = await self.service.http.request('POST', peer['endpoint_url'] + '/direct/notices/paired', json={
                'statement': row['pairing_statement'], 'signature': row['pairing_signature']})
        except (PartnerUnreachable, httpx.HTTPError):
            return False
        if status in (200, 404, 405) and (status != 200 or (isinstance(body, dict) and body.get('recorded') is True)):
            await run_lifecycle_step(lambda: self.store.update(row['id'], told_at=utcnow()))
            return True
        return False

    async def tell_partners(self):
        for row in await run_lifecycle_step(self.store.untold):
            await self.tell(row)
        return False


# -- what people read ----------------------------------------------------------------------------------

def notice_view(row, *, organization=None, fax=None, now=None, original_fax_id=None):
    """One notice for the console and the command line, in plain words."""
    now = now or utcnow()
    partner = organization or 'the partner'
    how = MATCHED_TEXT.get(row['matched_by'] or '', '')
    if row['role'] == 'sender':
        status = {
            'announced': f'The document is on its way to {partner} directly; the notice fax follows.',
            'queued': f'The document was delivered directly to {partner}; a one-page notice was faxed.',
            'paired': f'{partner} paired the faxed notice with the document delivered directly.',
            'cancelled': 'The document went by fax, so no notice was needed.',
        }[row['state']]
    else:
        if row['state'] == 'waiting':
            status = (f'The document from {partner} arrived directly and waits for its notice fax.'
                      if now - row['created_at'] < WAITING_LONG else
                      f'The document from {partner} has waited more than a day for its notice fax. Pair it by hand.')
        elif row['state'] == 'paired' and row['inbound_id']:
            status = f'Paired {how} with the notice fax; the document is in Received.'
        elif row['state'] == 'paired':
            status = 'Filed in Received by hand, without a notice fax.'
        else:
            status = 'No document arrived for this notice.'
    return {'id': row['id'], 'direction': 'outbound' if row['role'] == 'sender' else 'inbound',
            'partner': organization, 'state': row['state'], 'status': status, 'code': code_text(row['notice_id']),
            'matched_by': row['matched_by'], 'fax_id': row['notice_job_id'] if row['role'] == 'sender' else row['inbound_id'],
            'original_fax_id': original_fax_id, 'paired_by': row['paired_by_name'], 'created_at': row['created_at'],
            'paired_at': row['paired_at']}


def received_notice_text(row, *, organization, pages):
    """The line on a received fax that is a paired notice."""
    size = f'{pages}-page ' if pages else ''
    return f"Notice for {organization}'s {size}document: the document came by direct delivery."
