"""Send only the pages a broken call did not confirm, as a new fax a person chooses (T15).

A long fax to an ordinary recipient breaks at page 7 of 20. Before this, the
choices were to send all 20 pages again or nothing. A *continuation* is a new
fax of only pages k+1 to n, with one line on its first page that names the
fax it continues. It is linked both ways with the original in Sent. Partners
already get the missing pages through ``direct/repair.py``; this covers every
other recipient.

What the call record proves
---------------------------
Only the receiving machine's own answers count, as each engine or fax service
reported them. Unknown means unknown: no continuation is offered, only the
whole fax again ("send again").

- **The built-in engine (Asterisk with spandsp 0.0.6).** ``FAXPAGES`` (the call
  record's ``pages``) is spandsp's ``pages_tx``. In ``t30.c`` it grows only in
  ``tx_end_page``, which runs on the receiver's MCF ("received fine") or RTP
  ("received, but poor"; spandsp's own receiver answers RTP for 5 to 15 per
  cent damaged rows, ``copy_quality``). On RTN ("not received well") the page
  is not counted, and ``t4_tx_start_page`` sends the same page again, because
  only ``t4_tx_end_page`` moves to the next one. So the count is always the
  first pages in order. With error correction (ECM; the call's last DCS, bit
  27, kept in ``fax_call_frames``), a confirmed page arrived whole. Without
  it, an RTP page is a page with reported damaged lines; the engine does not
  say which page that was, but every RTP or RTN is followed by a new training
  (a new DCS). So a call that trained exactly once had no damaged page, and
  any other call without error correction is unknown. (Read from spandsp's
  source on 2026-10-08: github.com/freeswitch/spandsp, ``src/t30.c`` and
  ``src/t4_tx.c``.)
- **The SSL Fax engine (HylaFAX+ 7.0.11).** ``npages`` from the job file. Its
  default ``RTNHandlingMethod`` is ``Retransmit-Ignore``: after the third RTN
  for one page it goes on to the next page and counts the RTN page as sent
  (hylafax-config(5)). So without error correction Faxbot reads the call's
  session log (``hylafax/bin/negotiation``: ``clean_pages``, the pages the
  receiver answered MCF before any other answer) and counts only those; a
  session log without page answers is unknown. With error correction on for
  the whole call (``fax_engine_calls.ecm``), ``npages`` stands; on for some
  pages only, the answers count blocks rather than pages: unknown.
- **Cloud fax services**, from their own reports, kept when the result
  arrives (``fax_page_reports``):

  - Sinch: ``pagesSentSuccessfully``, "The number of pages successfully sent
    to the receiving side in the fax", and ``numberOfPages`` (Fax API v3
    OpenAPI description, developers.sinch.com/_bundle/docs/fax/api-reference/
    fax.yaml, read 2026-10-08).
  - Documo: ``pagesComplete`` ("all successful pages sent/received") and
    ``pagesCount``. From search-result text of Documo's help pages (Reports,
    Webhooks); the pages themselves answered 403 on 2026-10-08, so this is not
    yet checked against a real Documo account.
  - HumbleFax: each attempt's ``numPagesSent`` (api.humblefax.com, as
    ``routing/predata.py`` reads it). Each attempt sends from the first page,
    so the most any one attempt sent is the count; attempts are never added up.
  - Phaxio and SignalWire publish no pages-sent count for a failed fax: Phaxio
    v2.1's ``num_pages`` and SignalWire's ``num_pages`` are the document's
    pages (phaxio.com/docs/api/v2.1/faxes/get_fax; signalwire.com/docs/
    compatibility-api/rest/faxes/retrieve-fax.md, read 2026-10-08). Unknown.

Pages are counted as the call sent them. When an attempt sent packed or
encoded pages (``fax_page_changes``: a dense or codec layout, or a different
page count), or the service counted a different total, the count does not
name the document's own pages: unknown.

The continuation
----------------
- **A person chooses it.** Nothing here runs by itself; the recipient may have
  missed a page its machine confirmed. The first page sent again may already
  have arrived, so the recipient may get it twice, and the console says so.
- **One fax per broken call.** Its fax ID comes from the broken attempt
  (``continuation_id``), so a click in Sent and on the uncertain item, or a
  repeated click, give the same fax.
- **Through the sending rules** like any fax: the caller passes ``send``,
  ``routing/submit.accept_generated_fax`` bound to the person (their own
  ``fax:send``) and the active configuration, so the rules decide its route,
  which may differ from the first call's, and may hold it.
- **The note** is drawn by Faxbot in a band at the top of the first page; the
  page's own content is scaled down a little to make room, so nothing of the
  document is covered. The band starts below the strip where a fax engine
  prints its header line.
- **Cost:** the shared predictor prices the pages left and the whole fax on
  the account the rules would choose (``pricing.price``), so the action can
  read "About $0.02, against $0.05 for the whole fax."
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, replace
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa

from .database import DeliveryStoreError, reflect, utcnow, write_transaction


REQUIRED = ('fax_continuations', 'fax_page_reports', 'fax_jobs', 'outbound_deliveries', 'outbound_attempts',
            'outbound_events', 'access_resources', 'access_principals')
OPTIONAL = ('sip_call_records', 'fax_engine_calls', 'fax_call_frames', 'fax_page_changes', 'direct_call_repairs',
            'direct_peers', 'certainty_items', 'provider_profiles')
REPORT_SOURCES = ('hylafax', 'sinch', 'documo', 'humblefax')
PROVIDER_NAMES = {'sinch': 'Sinch', 'documo': 'Documo', 'humblefax': 'HumbleFax', 'phaxio': 'Phaxio',
                  'signalwire': 'SignalWire'}
NO_PAGE_COUNT = ('phaxio', 'signalwire')
DCS_ECM_BIT = 27  # T.30 Table 2: error correction mode
# The band Faxbot adds at the top of the first page, in PDF points (1/72 inch): the top 12 points stay clear for
# the header line a fax engine prints there (HylaFAX images about 24 rows of it at 196 lines an inch, 9 points),
# then the note.
BAND_POINTS = 30.0
CLEAR_POINTS = 12.0
NOTE_POINTS = 9.0
DASH = '–'


class ContinuationError(Exception):
    status = 400

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ContinuationNotFound(ContinuationError):
    status = 404


class ContinuationForbidden(ContinuationError):
    status = 403


class ContinuationConflict(ContinuationError):
    status = 409


NOT_FOUND = 'This sent fax was not found.'
FORBIDDEN = 'Only the person who sent this fax, or someone who may confirm receipt of it, can send its pages again.'
CHANGED = 'The pages to send changed; reload and check them again.'


def continuation_id(attempt_id):
    """The continuation's fax ID for a broken attempt: the same from Sent, the uncertain item and every click."""
    return hashlib.sha256(f'faxbot-continuation|{attempt_id}'.encode('ascii')).hexdigest()[:32]


def _count(value, highest=10_000):
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and 0 <= value <= highest else None


def pages_text(first, last):
    """'pages 8–20', or 'page 20' for one page."""
    return f'page {first}' if first == last else f'pages {first}{DASH}{last}'


def _pages(number):
    return f"{number} page{'' if number == 1 else 's'}"


# -- What the call record proves ----------------------------------------------------------------------

@dataclass(frozen=True)
class Confirmed:
    """The first ``pages`` pages the receiving machine confirmed (None: unknown), whose report says so, and how.

    ``reported`` is the raw page count the engine or service gave (the call record's, or the service's pages
    sent), which says the call broke part way even when the pages cannot be trusted."""
    pages: int | None
    source: str | None
    sentence: str
    total: int | None = None
    reported: int | None = None


def dcs_ecm(dcs_hex):
    """Whether a DCS frame (hex, as ``fax_call_frames`` keeps it) asks for error correction; None when unreadable."""
    from ..engine_frames import _bit, _octets
    frame = _octets(dcs_hex) if dcs_hex else b''
    return _bit(frame[3:], DCS_ECM_BIT) if len(frame) > 3 else None


def builtin_confirmed(pages, *, ecm, trainings):
    """The built-in engine: ``pages`` (FAXPAGES), ECM from the call's last DCS, and how many trainings (DCS)."""
    if type(pages) is not int or pages < 0:
        return Confirmed(None, 'builtin', "Faxbot's fax line did not report how many pages the receiving machine "
                                          'confirmed.')
    if ecm is True:
        return Confirmed(pages, 'builtin', f'The call used error correction, and the receiving machine confirmed '
                                           f'the first {_pages(pages)} whole.')
    if ecm is None:
        return Confirmed(None, 'builtin', 'Faxbot has no record of whether this call used error correction, so it '
                                          'cannot tell whether a page arrived damaged.')
    if trainings == 1:
        return Confirmed(pages, 'builtin', f'The receiving machine confirmed the first {_pages(pages)}, and it '
                                           'reported no damaged page.')
    return Confirmed(None, 'builtin', 'The call ran without error correction and had to start again part way, so '
                                      'Faxbot cannot tell which page the receiving machine reported damaged.')


def hylafax_confirmed(pages, *, ecm, clean_pages, flagged_page):
    """The SSL Fax engine: ``pages`` (npages), ECM over the whole call, and the session log's page answers."""
    if type(pages) is not int or pages < 0:
        return Confirmed(None, 'hylafax', 'The fax engine did not report how many pages the receiving machine '
                                          'confirmed.')
    if ecm == 'on':
        return Confirmed(pages, 'hylafax', f'The call used error correction, and the receiving machine confirmed '
                                           f'the first {_pages(pages)} whole.')
    if ecm == 'mixed':
        # With error correction the machine answers each block, not each page, so the answers cannot be counted.
        return Confirmed(None, 'hylafax', 'The call used error correction for some pages only, so Faxbot cannot tell '
                                          'which page the receiving machine reported damaged.')
    if type(clean_pages) is not int:
        return Confirmed(None, 'hylafax', "Faxbot could not read the receiving machine's answer to each page of "
                                          'this call, so it cannot tell which pages arrived well.')
    confirmed = min(pages, clean_pages)
    if type(flagged_page) is int and flagged_page <= pages:
        return Confirmed(confirmed, 'hylafax', f'The receiving machine reported damaged lines on page {flagged_page}, '
                                               f'so only the first {_pages(confirmed)} count as confirmed.')
    return Confirmed(confirmed, 'hylafax', f'The receiving machine confirmed the first {_pages(confirmed)}, and it '
                                           'reported no damaged page.')


def provider_confirmed(provider, report):
    """A cloud fax service's own count of pages sent, from ``fax_page_reports``; unknown where it gives none."""
    name = PROVIDER_NAMES.get(provider, 'This fax service')
    if provider in NO_PAGE_COUNT or provider not in REPORT_SOURCES:
        return Confirmed(None, None, f'{name} does not report how many pages it sent before a fax failed, so Faxbot '
                                     'cannot tell which pages arrived.')
    sent = (report or {}).get('pages_sent')
    total = (report or {}).get('total_pages')
    if type(sent) is not int:
        return Confirmed(None, provider, f'{name} did not report how many pages it sent before this fax failed.')
    verb = {'documo': 'completed', 'humblefax': 'sent'}.get(provider, 'sent successfully')
    of = f' of {total}' if type(total) is int else ''
    return Confirmed(sent, provider, f'{name} reported {sent}{of} pages {verb}.', total if type(total) is int else None)


# -- Reports kept when the result arrives --------------------------------------------------------------

def engine_report(encoded):
    """(clean pages, first flagged page) from hylafax/bin/negotiation's object; (None, None) when not reported."""
    if not isinstance(encoded, str) or not encoded or len(encoded) > 1024:
        return None, None
    try:
        data = json.loads(base64.b64decode(encoded, validate=True).decode('ascii'))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    clean, flagged = _count(data.get('clean_pages')), _count(data.get('flagged_page'))
    return clean, (flagged if flagged else None)


def sinch_pages(fax):
    """(pages sent, pages in the fax) from Sinch's fax object."""
    fax = fax.get('data', fax) if isinstance(fax, dict) and isinstance(fax.get('data'), dict) else fax
    if not isinstance(fax, dict):
        return None, None
    return _count(fax.get('pagesSentSuccessfully')), _count(fax.get('numberOfPages'))


def documo_pages(payload):
    """(pages complete, pages in the fax) from Documo's fax object."""
    if not isinstance(payload, dict):
        return None, None
    return _count(payload.get('pagesComplete')), _count(payload.get('pagesCount'))


def humblefax_pages(fax):
    """(the most pages any one attempt sent, None) from HumbleFax's sent fax; None when an attempt did not say."""
    if not isinstance(fax, dict):
        return None, None
    found = None
    for recipient in fax.get('recipients') or ():
        if not isinstance(recipient, dict):
            continue
        for attempt in recipient.get('attempts') or ():
            if not isinstance(attempt, dict):
                continue
            sent = _count(attempt.get('numPagesSent'))
            if sent is None:
                return None, None
            found = sent if found is None else max(found, sent)
    return found, None


def provider_report(provider, result):
    """(pages sent, pages in the fax) a status result carries; adapters put Documo's and HumbleFax's in
    ``pages_sent``/``pages_total``, Sinch's result is its own fax object."""
    if not isinstance(result, dict):
        return None, None
    if provider == 'sinch':
        return sinch_pages(result)
    if provider in ('documo', 'humblefax'):
        return _count(result.get('pages_sent')), _count(result.get('pages_total'))
    return None, None


def _insert_report(engine, values):
    """Keep one report per attempt and source, once; a repeated result changes nothing. Never raises."""
    try:
        tables = reflect(engine, ('fax_page_reports',))
        table = tables['fax_page_reports']
        with write_transaction(engine) as connection:
            if connection.execute(sa.select(table.c.id).where(table.c.attempt_id == values['attempt_id'],
                                                               table.c.source == values['source'])).first():
                return False
            connection.execute(table.insert().values(id=uuid4().hex, created_at=utcnow(), **values))
            return True
    except (DeliveryStoreError, sa.exc.SQLAlchemyError, KeyError):
        return False


def record_engine_report(engine, *, job_id, attempt_id, negotiation):
    """The SSL Fax engine's page answers for one sent call (hylafax_http); evidence only."""
    clean, flagged = engine_report(negotiation)
    if clean is None or not job_id or not attempt_id:
        return False
    return _insert_report(engine, {'job_id': job_id, 'attempt_id': attempt_id, 'source': 'hylafax',
                                   'clean_pages': clean, 'flagged_page': flagged})


def record_provider_report(engine, *, job_id, attempt_id, provider, result):
    """A cloud fax service's count of pages sent for a failed fax (outbound_polling); evidence only."""
    if provider not in REPORT_SOURCES or provider == 'hylafax' or not job_id or not attempt_id:
        return False
    sent, total = provider_report(provider, result)
    if sent is None:
        return False
    return _insert_report(engine, {'job_id': job_id, 'attempt_id': attempt_id, 'source': provider,
                                   'pages_sent': sent, 'total_pages': total})


# -- The continuation's pages -------------------------------------------------------------------------

def note_text(sent_at, total, first, *, zone_name=None):
    """'Continuation of our fax of 7 October 2026 at 9:41 AM MDT, 20 pages: pages 8–20. Pages 1–7 arrived on the
    first call.'"""
    from .. import people_time
    when = people_time.date_and_time(sent_at, zone_name)
    before = 'Page 1 arrived' if first == 2 else f'Pages 1{DASH}{first - 1} arrived'
    return (f'Continuation of our fax of {when}, {_pages(total)}: {pages_text(first, total)}. '
            f'{before} on the first call.')


def _note_font():
    """(font name, whether it has the en dash): DejaVu Sans as shipped for forms, else Helvetica."""
    from ..forms.renderer import FONT_PATH
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        if 'FaxbotNote' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('FaxbotNote', str(FONT_PATH)))
        return 'FaxbotNote', True
    except Exception:
        return 'Helvetica', False


def _band_page(width, height, note):
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader
    font, dash = _note_font()
    text = note if dash else note.replace(DASH, '-')
    output = io.BytesIO()
    pdf = canvas.Canvas(output, pagesize=(width, height))
    size = NOTE_POINTS
    while size > 5 and pdf.stringWidth(text, font, size) > width - 36:
        size -= 0.5
    pdf.setFont(font, size)
    pdf.drawString(18, height - CLEAR_POINTS - size, text)
    pdf.showPage()
    pdf.save()
    return PdfReader(io.BytesIO(output.getvalue())).pages[0]


def continuation_pdf(document, *, first_page, last_page, note):
    """A PDF of ``document``'s pages ``first_page`` to ``last_page`` (1-based), the note in a band on the first.

    The first page keeps its size; its content is scaled into the space below the band, so nothing is covered.
    Every other page is the original page unchanged."""
    from pypdf import PageObject, PdfReader, PdfWriter, Transformation
    reader = PdfReader(io.BytesIO(document))
    if not 1 <= first_page <= last_page <= len(reader.pages):
        raise ValueError('The document does not have these pages.')
    writer = PdfWriter()
    first = reader.pages[first_page - 1]
    if first.rotation % 360:
        first.transfer_rotation_to_content()
    box = first.mediabox
    left, bottom = float(box.left), float(box.bottom)
    width, height = float(box.width), float(box.height)
    scale = max(0.5, (height - BAND_POINTS) / height)
    page = PageObject.create_blank_page(width=width, height=height)
    page.merge_transformed_page(first, Transformation().translate(-left, -bottom).scale(scale, scale)
                                .translate(width * (1 - scale) / 2, 0))
    page.merge_page(_band_page(width, height, note))
    writer.add_page(page)
    for index in range(first_page, last_page):
        writer.add_page(reader.pages[index])
    writer.add_metadata({'/Title': f'Continuation: {pages_text(first_page, last_page)}', '/Subject': note})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def document_pages(path):
    """The page count of a kept document, or None when it cannot be read."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


# -- The store ----------------------------------------------------------------------------------------

class ContinuationStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, REQUIRED)
        names = set(sa.inspect(engine).get_table_names())
        present = tuple(name for name in OPTIONAL if name in names)
        tables.update(reflect(engine, present) if present else {})
        self.tables = tables
        self.links, self.reports = tables['fax_continuations'], tables['fax_page_reports']
        self.jobs, self.deliveries = tables['fax_jobs'], tables['outbound_deliveries']
        self.attempts, self.events, self.resources = (tables['outbound_attempts'], tables['outbound_events'],
                                                      tables['access_resources'])
        self.principals = tables['access_principals']

    def table(self, name):
        return self.tables.get(name)

    def _one(self, connection, query):
        row = connection.execute(query).mappings().first()
        return dict(row) if row is not None else None

    def job_on(self, connection, job_id):
        return self._one(connection, sa.select(self.jobs).where(self.jobs.c.id == job_id))

    def resource_of(self, connection, job_id):
        return connection.execute(sa.select(self.resources.c.id).where(
            self.resources.c.kind == 'outbound', self.resources.c.fax_job_id == job_id)).scalar()

    def link_for_original(self, connection, job_id):
        return self._one(connection, sa.select(self.links).where(self.links.c.job_id == job_id)
                         .order_by(self.links.c.created_at.desc()).limit(1))

    def link_for_continuation(self, connection, job_id):
        return self._one(connection, sa.select(self.links).where(self.links.c.continuation_job_id == job_id))

    def links_for(self, connection, continuation_ids):
        ids = sorted({value for value in continuation_ids if value})
        if not ids:
            return {}
        rows = connection.execute(sa.select(self.links).where(self.links.c.continuation_job_id.in_(ids))).mappings()
        return {row['continuation_job_id']: dict(row) for row in rows}

    def report_on(self, connection, attempt_id, source):
        return self._one(connection, sa.select(self.reports).where(self.reports.c.attempt_id == attempt_id,
                                                                   self.reports.c.source == source))

    def provider_of(self, connection, attempt, job):
        profiles = self.table('provider_profiles')
        if profiles is not None and attempt.get('profile_id'):
            found = connection.execute(sa.select(profiles.c.provider_id).where(
                profiles.c.id == attempt['profile_id'])).scalar()
            if found:
                return found
        backend = job.get('outbound_backend') if 'outbound_backend' in job else None
        return backend or job.get('backend') or None

    def name_on(self, connection, principal_id):
        if not principal_id:
            return None
        return connection.execute(sa.select(self.principals.c.display_name).where(
            self.principals.c.id == principal_id)).scalar()

    def record_on(self, connection, prepared, *, actor_id, actor_name, item_id=None, reason=None, now=None):
        """Keep the link once; a repeated request finds it already kept. True when this call wrote it."""
        if self.link_for_continuation(connection, prepared.job_id) is not None:
            return False
        connection.execute(self.links.insert().values(
            id=uuid4().hex, job_id=prepared.original_job_id, attempt_id=prepared.attempt_id,
            continuation_job_id=prepared.job_id, first_page=prepared.first_page, last_page=prepared.last_page,
            confirmed_by=prepared.confirmed_by, item_id=item_id, requested_by=actor_id,
            requested_by_name=(actor_name or '')[:200] or None, reason=(reason or '')[:400] or None,
            created_at=now or utcnow()))
        return True


# -- The offer ----------------------------------------------------------------------------------------

def _attempt_on(store, connection, attempt_id):
    return store._one(connection, sa.select(store.attempts).where(store.attempts.c.id == attempt_id))


def _call_record(store, connection, attempt_id):
    calls = store.table('sip_call_records')
    if calls is None:
        return None
    return store._one(connection, sa.select(calls).where(calls.c.attempt_id == attempt_id,
                                                         calls.c.direction == 'outbound')
                      .order_by(calls.c.started_at.desc(), calls.c.id).limit(1))


def _engine_call(store, connection, attempt_id):
    calls = store.table('fax_engine_calls')
    if calls is None:
        return None
    return store._one(connection, sa.select(calls).where(calls.c.direction == 'outbound',
                                                         calls.c.call_key == attempt_id,
                                                         calls.c.engine == 'hylafax').limit(1))


def _frames(store, connection, attempt_id):
    frames = store.table('fax_call_frames')
    if frames is None:
        return None
    return store._one(connection, sa.select(frames).where(frames.c.id == f'out:{attempt_id}'))


def confirmed_on(store, connection, attempt, job):
    """What the broken attempt's own record proves about its pages (``Confirmed``)."""
    provider = store.provider_of(connection, attempt, job)
    if provider in ('sip', 'freeswitch'):
        call = _call_record(store, connection, attempt['id'])
        pages = call.get('pages') if call is not None else None
        reported = pages if type(pages) is int else None
        engine_call = _engine_call(store, connection, attempt['id'])
        if engine_call is not None:
            report = store.report_on(connection, attempt['id'], 'hylafax') or {}
            return replace(hylafax_confirmed(pages, ecm=engine_call.get('ecm'), clean_pages=report.get('clean_pages'),
                                             flagged_page=report.get('flagged_page')), reported=reported)
        if provider == 'freeswitch':
            return Confirmed(None, None, 'This fax line does not report which pages the receiving machine '
                                         'confirmed.', reported=reported)
        frames = _frames(store, connection, attempt['id']) or {}
        return replace(builtin_confirmed(pages, ecm=dcs_ecm(frames.get('dcs_last')), trainings=frames.get('trainings')),
                       reported=reported)
    report = store.report_on(connection, attempt['id'], provider) if provider in REPORT_SOURCES else None
    found = provider_confirmed(provider, report)
    return replace(found, reported=(report or {}).get('pages_sent') if type((report or {}).get('pages_sent')) is int
                   else None)


def _shared_call(store, connection, attempt):
    return connection.execute(sa.select(store.events.c.id).where(
        store.events.c.job_id == attempt['job_id'], store.events.c.attempt_id == attempt['id'],
        store.events.c.kind == 'sent_together').limit(1)).first() is not None


def _packed(store, connection, attempt):
    changes = store.table('fax_page_changes')
    if changes is None:
        return False
    row = store._one(connection, sa.select(changes).where(changes.c.attempt_id == attempt['id']).limit(1))
    return row is not None and (row.get('layout') in ('dense', 'codec')
                                or row.get('sent_pages') != row.get('original_pages'))


def _repair(store, connection, attempt):
    repairs = store.table('direct_call_repairs')
    if repairs is None:
        return None
    return store._one(connection, sa.select(repairs).where(repairs.c.role == 'sender',
                                                           repairs.c.attempt_id == attempt['id'])
                      .order_by(repairs.c.created_at.desc()).limit(1))


def _partner(store, connection, peer_id):
    peers = store.table('direct_peers')
    if peers is None or not peer_id:
        return 'the partner'
    return connection.execute(sa.select(peers.c.organization).where(peers.c.id == peer_id)).scalar() or 'the partner'


def _item(store, connection, attempt_id):
    items = store.table('certainty_items')
    if items is None:
        return None
    return store._one(connection, sa.select(items).where(items.c.attempt_id == attempt_id))


@dataclass(frozen=True)
class Offer:
    """What a person may send for one broken call, or why not (``available`` False with ``reason``)."""
    available: bool
    reason: str | None
    job_id: str
    attempt_id: str | None = None
    first_page: int | None = None
    last_page: int | None = None
    confirmed: Confirmed | None = None
    sent_at: object = None
    to_number: str | None = None
    item: dict | None = None


def offer_on(store, connection, job_id, *, data_dir, resume=False):
    """The continuation a person may send for this fax, why it cannot be offered, or None when the fax did not
    break part way through a call (nothing to say: an ordinary fax, a delivered one, an uncertain one).

    ``resume``: a person's send, which may finish a continuation already queued under its fax ID whose link was
    not kept (the request stopped between the two); a view never offers it again."""
    job = store.job_on(connection, job_id)
    delivery = store._one(connection, sa.select(store.deliveries).where(store.deliveries.c.id == job_id))
    if job is None or delivery is None or delivery['state'] != 'failed' or not delivery['attempt_id']:
        return None
    attempt = _attempt_on(store, connection, delivery['attempt_id'])
    if attempt is None or attempt['job_id'] != job_id or attempt['submitted_at'] is None:
        return None
    confirmed = confirmed_on(store, connection, attempt, job)
    total = job.get('pages')
    # The call broke part way: the engine or service said so, or its own page count stops short of the fax.
    partly = attempt.get('error_category') == 'partly_sent' or (
        type(confirmed.reported) is int and confirmed.reported > 0 and type(total) is int
        and confirmed.reported < total)
    if not partly or delivery.get('dispatch_mode') not in (None, 'normal'):
        return None
    item = _item(store, connection, attempt['id'])
    common = dict(job_id=job_id, attempt_id=attempt['id'], confirmed=confirmed, to_number=job.get('to_number'),
                  sent_at=attempt.get('submitted_at') or job.get('created_at'), item=item)

    def no(reason):
        return Offer(False, reason, **common)
    if type(total) is not int or total < 2:
        return no('Faxbot does not know how many pages this fax has, so it cannot send only the rest.')
    if _shared_call(store, connection, attempt):
        return no('This fax went in one call with other faxes, so its remaining pages cannot be sent on their own '
                  'from here.')
    if _packed(store, connection, attempt):
        return no('Faxbot packed the pages of this fax onto longer pages for the call, so the pages the receiving '
                  'machine confirmed are not your document\'s pages.')
    repair = _repair(store, connection, attempt)
    if repair is not None and repair.get('state') != 'expired':
        return no(f"Faxbot is completing this fax directly with {_partner(store, connection, repair.get('peer_id'))}.")
    if item is not None and item.get('resend_job_id'):
        return no('This fax was already sent again in full.' if store.link_for_continuation(
            connection, item['resend_job_id']) is None else 'The remaining pages of this fax were already sent.')
    if item is not None and item.get('state') == 'settled' and item.get('outcome') == 'delivered':
        return no('This fax was settled as delivered.')
    if not resume and store.job_on(connection, continuation_id(attempt['id'])) is not None:
        return no('The remaining pages of this fax are already on their way as a new fax.')
    if confirmed.pages is None:
        return no(confirmed.sentence)
    if confirmed.total is not None and confirmed.total != total:
        name = PROVIDER_NAMES.get(confirmed.source, 'The fax service')
        return no(f'{name} counted {_pages(confirmed.total)} in this fax, not {total}, so Faxbot cannot tell which '
                  'of your pages arrived.')
    if confirmed.pages < 1:
        return no('The receiving machine confirmed no page, so there is nothing to continue; send the whole fax '
                  'again instead.')
    if confirmed.pages >= total:
        return no('The receiving machine confirmed every page of this fax.')
    source = Path(data_dir or '.') / f'{job_id}.pdf'
    if source.is_symlink() or not source.is_file():
        return no('The document of this fax is no longer kept, so its remaining pages cannot be sent from here.')
    kept = document_pages(source)
    if kept != total:
        return no(f'The document kept for this fax does not have the {total} pages that were sent, so its remaining '
                  'pages cannot be sent from here.')
    return Offer(True, None, first_page=confirmed.pages + 1, last_page=total, **common)


@dataclass(frozen=True)
class Prepared:
    """A continuation ready to send: its fax ID, document and pages, and what it continues."""
    job_id: str
    original_job_id: str
    attempt_id: str
    to_number: str
    first_page: int
    last_page: int
    confirmed_by: str
    document: bytes
    file_name: str

    @property
    def pages(self):
        return self.last_page - self.first_page + 1


def prepare(offer, *, data_dir, zone_name=None):
    """The continuation's document for an available offer."""
    if not offer.available:
        raise ContinuationConflict(offer.reason)
    source = Path(data_dir or '.') / f'{offer.job_id}.pdf'
    try:
        document = continuation_pdf(source.read_bytes(), first_page=offer.first_page, last_page=offer.last_page,
                                    note=note_text(offer.sent_at, offer.last_page, offer.first_page,
                                                   zone_name=zone_name))
    except Exception:  # an unreadable or damaged document: nothing is sent
        raise ContinuationConflict('The remaining pages of this fax could not be prepared from its document.') \
            from None
    return Prepared(continuation_id(offer.attempt_id), offer.job_id, offer.attempt_id, offer.to_number,
                    offer.first_page, offer.last_page, offer.confirmed.source or 'builtin', document,
                    f'continuation-pages-{offer.first_page}-{offer.last_page}.pdf')


# -- Cost ---------------------------------------------------------------------------------------------

def _price_words(found):
    if found is None:
        return None
    if found.in_plan:
        return 'in your plan'
    if found.micros is None:
        return None
    from .costs import money_text
    return 'about ' + money_text(found.micros, found.currency)


def cost_sentence(part, whole):
    """'About $0.02, against $0.05 for the whole fax.'; unknown stays unknown, never $0."""
    left, all_pages = _price_words(part), _price_words(whole)
    if left is None:
        return 'Faxbot cannot price these pages, so their cost is unknown.'
    if left == 'in your plan':
        return 'In your plan, as the whole fax would be.' if all_pages == 'in your plan' else 'In your plan.'
    if all_pages is None:
        return f'{left[0].upper()}{left[1:]}; the whole fax cannot be priced.'
    if all_pages == 'in your plan':
        return f'{left[0].upper()}{left[1:]}; the whole fax would be in your plan.'
    return f'{left[0].upper()}{left[1:]}, against {all_pages.removeprefix("about ")} for the whole fax.'


def cost(engine, values, actor, *, to_number, pages, total):
    """(sentence, account key or None, {'part', 'whole'} money or None) on the account the rules would choose."""
    from ..accounts import all_accounts, sending_accounts
    from ..rules.evaluate import decide
    from ..rules.explain import FactsReader
    from ..rules.store import RuleStore
    from .pricing import price
    from .rules_acceptance import alternate_lookup, sender_of
    from .store import RouteStore
    try:
        routes = RouteStore(engine)
        accounts = sending_accounts(values)
        principal, kind, key_id = sender_of(actor)
        facts = FactsReader(engine, values, routes, alternates=alternate_lookup(engine)).read(
            to_number=to_number, accounts=accounts, pages=pages, principal_id=principal, sender_kind=kind,
            key_id=key_id)
        envelope = decide(RuleStore(engine).compiled_active(), facts, accounts).envelope
        if not envelope.accounts:
            return 'No account your rules allow can send to this number, so these pages cannot be priced.', None, None
        key = envelope.accounts[0]
        account = {item.key: item for item in all_accounts(values)}.get(key)
        provider = account.provider if account is not None else key
        part = price(routes, values, key, to_number, pages, provider=provider)
        whole = price(routes, values, key, to_number, total, provider=provider)
        return cost_sentence(part, whole), key, {'part': part.money(), 'whole': whole.money()}
    except Exception:
        return 'Faxbot cannot price these pages, so their cost is unknown.', None, None


# -- Views and sending, access-checked ----------------------------------------------------------------

def _link_view(link):
    return {'fax_id': link['continuation_job_id'], 'first_page': link['first_page'], 'last_page': link['last_page'],
            'pages_text': pages_text(link['first_page'], link['last_page']), 'sent_at': link['created_at'],
            'requested_by': link.get('requested_by_name'), 'from_item': bool(link.get('item_id'))}


class ContinuationService:
    """Read and send continuations for one person; ``access`` carries AccessStore (``store``) and AccessControl."""

    def __init__(self, store, access, *, values, clock=None):
        self.store = store
        self.access_store, self.control = access.store, access.control
        self.values = values
        self.clock = clock or utcnow

    def _data_dir(self):
        return getattr(self.values(), 'fax_data_dir', '') or '.'

    def _allowed(self, connection, actor, permission, resource_id, now):
        from ..access.types import ResourceRef
        return self.control.authorize_on(connection, actor, permission, ResourceRef(resource_id), now=now).allowed

    def _visible(self, connection, actor, job_id, now):
        self.control._current_source_on(connection, actor, now)
        if not isinstance(job_id, str) or not 0 < len(job_id) <= 40:
            raise ContinuationNotFound(NOT_FOUND)
        resource = self.store.resource_of(connection, job_id)
        if resource is None or not self._allowed(connection, actor, 'fax:read', resource, now):
            raise ContinuationNotFound(NOT_FOUND)
        return resource

    def _sender(self, connection, job_id):
        """The person who sent the fax (its personal container's user), or None."""
        r, parent = self.store.resources, self.store.resources.alias('parent')
        principals = self.store.principals
        return connection.execute(
            sa.select(principals.c.id).select_from(r.join(parent, parent.c.id == r.c.parent_id)
                                                   .join(principals, principals.c.id == parent.c.principal_id))
            .where(r.c.kind == 'outbound', r.c.fax_job_id == job_id, parent.c.kind == 'personal',
                   principals.c.kind == 'user')).scalar()

    def _may_act(self, connection, actor, job_id, resource, item, now):
        mine = actor.principal_id is not None and actor.principal_id in (
            (item or {}).get('owner_principal_id'), self._sender(connection, job_id))
        return mine or self._allowed(connection, actor, 'fax:reconcile', resource, now)

    def _view_on(self, connection, actor, job_id, now):
        resource = self._visible(connection, actor, job_id, now)
        view = {'fax_id': job_id, 'offer': None, 'continued_by': None, 'continues': None}
        link = self.store.link_for_original(connection, job_id)
        if link is not None:
            view['continued_by'] = _link_view(link)
        origin = self.store.link_for_continuation(connection, job_id)
        if origin is not None:
            original = self.store.resource_of(connection, origin['job_id'])
            if original is not None and self._allowed(connection, actor, 'fax:read', original, now):
                view['continues'] = {'fax_id': origin['job_id'], 'first_page': origin['first_page'],
                                     'last_page': origin['last_page'],
                                     'pages_text': pages_text(origin['first_page'], origin['last_page'])}
        if link is not None:
            return view
        offer = offer_on(self.store, connection, job_id, data_dir=self._data_dir())
        if offer is None:
            return view
        item = offer.item if offer.item is not None and offer.item.get('state') == 'open' else None
        found = {'available': offer.available, 'reason': offer.reason,
                 'basis': offer.confirmed.sentence if offer.confirmed else None,
                 'confirmed_pages': offer.confirmed.pages if offer.confirmed else None,
                 'open_item_id': item['id'] if item is not None else None,
                 'may_send': self._may_act(connection, actor, job_id, resource, offer.item, now)}
        if offer.available:
            first, last = offer.first_page, offer.last_page
            found.update({'first_page': first, 'last_page': last, 'pages': last - first + 1, 'total_pages': last,
                          'pages_text': pages_text(first, last),
                          'action': f'Send {pages_text(first, last)}',
                          'warning': f'Page {first} may already have arrived, so the recipient may get it twice.'})
        view['offer'] = found
        view['_offer'] = offer
        return view

    def view(self, actor, job_id, *, with_cost=True):
        """Sent's continuation section: the offer (with its cost), or why not, and the links both ways."""
        with self.access_store.transaction() as connection:
            now = self.clock()
            view = self._view_on(connection, actor, job_id, now)
        offer = view.pop('_offer', None)
        if with_cost and offer is not None and offer.available:
            sentence, account, money = cost(self.store.engine, self.values(), actor, to_number=offer.to_number,
                                            pages=offer.last_page - offer.first_page + 1, total=offer.last_page)
            view['offer'].update({'cost_text': sentence, 'cost_account': account, 'cost': money})
        return view

    def offer_for(self, actor, job_id, connection, now):
        """The offer inside the caller's transaction (the uncertain item's detail), without its cost."""
        view = self._view_on(connection, actor, job_id, now)
        view.pop('_offer', None)
        return view

    def prepare_for(self, actor, job_id, *, first_page):
        """Check the person may send this fax's remaining pages and that they are the pages they saw; Prepared."""
        if type(first_page) is not int or first_page < 2:
            raise ContinuationError('Say which page to start from.')
        with self.access_store.transaction() as connection:
            now = self.clock()
            resource = self._visible(connection, actor, job_id, now)
            if self.store.link_for_original(connection, job_id) is not None:
                raise ContinuationConflict('The remaining pages of this fax were already sent.')
            offer = offer_on(self.store, connection, job_id, data_dir=self._data_dir(), resume=True)
            if offer is None:
                raise ContinuationConflict('This fax did not break part way through a call, so there are no '
                                           'remaining pages to send.')
            if not self._may_act(connection, actor, job_id, resource, offer.item, now):
                raise ContinuationForbidden(FORBIDDEN)
            if not offer.available:
                raise ContinuationConflict(offer.reason)
            if offer.first_page != first_page:
                raise ContinuationConflict(CHANGED)
        return offer, prepare(offer, data_dir=self._data_dir())

    def send(self, actor, job_id, *, first_page, reason=None, send):
        """Send pages ``first_page`` to the end as a new fax, at this person's request; their view after.

        A fax with an open uncertain item goes through the item (``CertaintyService.settle``), which settles it
        as not delivered with the continuation as its new fax.
        """
        reason = ' '.join(str(reason or '').split())[:400] or None
        offer, prepared = self.prepare_for(actor, job_id, first_page=first_page)
        if offer.item is not None and offer.item.get('state') == 'open':
            raise ContinuationConflict('This fax is waiting to be settled; send its remaining pages from there.')
        send(to_number=prepared.to_number, document=prepared.document, file_name=prepared.file_name,
             pages=prepared.pages, job_id=prepared.job_id)
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._visible(connection, actor, job_id, now)
            if self.store.job_on(connection, prepared.job_id) is None:
                raise ContinuationConflict('The remaining pages are already being sent; reload in a moment.')
            name = self.store.name_on(connection, actor.principal_id)
            self.store.record_on(connection, prepared, actor_id=actor.principal_id, actor_name=name, reason=reason,
                                 now=now)
        from ..audit import audit_event
        try:
            audit_event('fax_continued', job_id=job_id, continuation_job_id=prepared.job_id,
                        first_page=prepared.first_page, last_page=prepared.last_page)
        except Exception:
            pass
        return self.view(actor, job_id, with_cost=False)
