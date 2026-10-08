"""No duplicate work from the direct path: a notice fax and a repaired call's first pages fold into the document.

The direct path (``direct/notice.py`` and ``direct/repair.py``, revision 0046)
files one received document for what a partner sent, but two received faxes
come with it that must not stand beside it as their own work or email:

- **A notice fax.** The original arrives directly and is held; a one-page
  notice comes by fax so the recipient's intake records a fax event. When the
  matcher pairs them (by the subaddress the notice carried, else its barcode),
  the original is filed in Received. Until the matcher has examined a received
  fax of one or two pages that came in while an original waits, that fax gets
  no Work item and no email (``held``). It is examined at hand-over, so the
  wait is short; after ``HOLD_LONGEST`` it is released whatever happened, so a
  real fax is never kept back. Once paired, the notice's email is not sent
  (``email_note``) and its Work item is closed into the document's.
- **A repaired call.** A fax call from a partner broke after some pages; the
  partner then sent only the missing pages directly, and the whole fax was
  filed as one document. The first pages were already received as a fax of
  their own, usually already emailed. Its Work item is closed into the whole
  document's, and its email is held back when it has not gone yet.

A closed item says why, with the document it went into ("Pages 1–6 of this fax
were completed directly by Lakeside; see the whole document."), and keeps its
history; its owner becomes the document's owner when the document has none and
they can see it. This module only reads the direct path's tables, and only
when they exist: before revision 0046 it does nothing.
"""
from datetime import timedelta

import sqlalchemy as sa

from ..routing.database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction


NOTICE_PAGES = 2  # the matcher examines received faxes of up to two pages (direct/notice.SCAN_PAGES)
SCAN_BACK = timedelta(minutes=10)  # and those that came this long before a held original (direct/notice.SCAN_BACK)
HOLD_LONGEST = timedelta(minutes=15)
FILING_SOURCE, FILING_ACCOUNT = 'local', 'direct:'  # how direct/filing.py files a partner's document
DIRECT_TABLES = ('direct_notices', 'direct_notice_scans', 'direct_call_repairs')


def _tables(engine):
    """The direct path's notice and repair tables, with what they are read against; None before 0046."""
    cached = getattr(engine, '_faxbot_duplicate_tables', None)
    if cached is not None:
        return cached or None
    names = set(sa.inspect(engine).get_table_names())
    if not set(DIRECT_TABLES) <= names:
        return None  # not cached: the tables appear when the installation is upgraded
    tables = reflect(engine, DIRECT_TABLES + ('direct_deliveries', 'direct_peers', 'inbound_imports',
                                              'inbound_faxes', 'work_items', 'intake_items'))
    try:
        engine._faxbot_duplicate_tables = tables
    except AttributeError:
        pass
    return tables


def held(engine, inbound, *, now=None):
    """A predicate over ``inbound`` (the received faxes table) that is true for a fax to keep back for now.

    False everywhere, at the cost of one query, unless an original that arrived directly waits for its notice.
    """
    tables = _tables(engine)
    if tables is None:
        return sa.false()
    now = now or utcnow()
    notices, deliveries, scans = tables['direct_notices'], tables['direct_deliveries'], tables['direct_notice_scans']
    with read_connection(engine) as connection:
        since = connection.execute(sa.select(sa.func.min(notices.c.created_at)).select_from(
            notices.join(deliveries, sa.and_(deliveries.c.message_id == notices.c.message_id,
                                             deliveries.c.direction == 'inbound',
                                             deliveries.c.peer_id == notices.c.peer_id)))
            .where(notices.c.role == 'receiver', notices.c.state == 'waiting', deliveries.c.state == 'accepted')
        ).scalar()
    if since is None:
        return sa.false()
    moment = sa.func.coalesce(inbound.c.received_at, inbound.c.created_at)
    examined = sa.exists(sa.select(1).where(scans.c.inbound_id == inbound.c.id))
    return sa.and_(moment >= since - SCAN_BACK, moment >= now - HOLD_LONGEST, ~examined,
                   sa.or_(inbound.c.pages.is_(None), inbound.c.pages <= NOTICE_PAGES))


def _organization(connection, tables, peer_id):
    peers = tables['direct_peers']
    return connection.execute(sa.select(peers.c.organization).where(peers.c.id == peer_id)).scalar() or 'the partner'


def _filed(connection, tables, peer_id, message_id):
    """The received fax the partner's document was filed as, or None while it is not filed."""
    imports = tables['inbound_imports']
    return connection.execute(sa.select(imports.c.inbound_fax_id).where(
        imports.c.source == FILING_SOURCE, imports.c.account == FILING_ACCOUNT + (peer_id or 'unknown'),
        imports.c.operation_id == message_id, imports.c.inbound_fax_id.is_not(None))).scalar()


def notice_reason(organization):
    return f'This page is the fax notice for a document {organization} delivered directly; see the whole document.'


def _first_pages(pages_held):
    return 'Page 1 of this fax was' if pages_held == 1 else f'Pages 1–{pages_held} of this fax were'


def repair_reason(organization, pages_held):
    return f'{_first_pages(pages_held)} completed directly by {organization}; see the whole document.'


def repair_note(organization, pages_held):
    return f'{_first_pages(pages_held)} completed directly by {organization}; the whole document is emailed instead.'


def folded(connection, tables):
    """[(received fax to fold, the document's received fax, reason, email note)] that are ready to fold."""
    notices, repairs = tables['direct_notices'], tables['direct_call_repairs']
    found = []
    for row in connection.execute(sa.select(notices.c.inbound_id, notices.c.peer_id, notices.c.message_id).where(
            notices.c.role == 'receiver', notices.c.state == 'paired', notices.c.inbound_id.is_not(None))).all():
        document = _filed(connection, tables, row.peer_id, row.message_id)
        if document and document != row.inbound_id:
            organization = _organization(connection, tables, row.peer_id)
            found.append((row.inbound_id, document, notice_reason(organization),
                          'This is the fax notice for a document delivered directly; that document is emailed '
                          'instead.'))
    for row in connection.execute(sa.select(repairs.c.inbound_id, repairs.c.peer_id, repairs.c.message_id,
                                            repairs.c.pages_held).where(
            repairs.c.role == 'receiver', repairs.c.state == 'completed', repairs.c.inbound_id.is_not(None),
            repairs.c.message_id.is_not(None))).all():
        document = _filed(connection, tables, row.peer_id, row.message_id)
        if document and document != row.inbound_id:
            organization = _organization(connection, tables, row.peer_id)
            found.append((row.inbound_id, document, repair_reason(organization, row.pages_held),
                          repair_note(organization, row.pages_held)))
    return found


def email_note(connection, engine, inbound_id):
    """Why the email of a received fax is not sent (a paired notice fax), or None."""
    tables = _tables(engine)
    if tables is None or not inbound_id:
        return None
    notices = tables['direct_notices']
    paired = connection.execute(sa.select(notices.c.id).where(
        notices.c.role == 'receiver', notices.c.state == 'paired', notices.c.inbound_id == inbound_id)).first()
    if paired is None:
        return None
    return 'This is the fax notice for a document delivered directly; that document is emailed instead.'


def fold(store, control, *, now=None):
    """Close the Work item of each notice fax and repaired call's first pages into its document's; how many."""
    tables = _tables(store.engine)
    if tables is None:
        return 0
    now = now or utcnow()
    from .store import WorkChanged, may_own_on
    items, intake = tables['work_items'], tables['intake_items']
    with read_connection(store.engine) as connection:
        pending = [entry for entry in folded(connection, tables)
                   if connection.execute(sa.select(items.c.id).where(
                       items.c.inbound_fax_id == entry[0], items.c.state != 'done')).first() is not None
                   or connection.execute(sa.select(intake.c.id).where(
                       intake.c.inbound_fax_id == entry[0], intake.c.state == 'received',
                       intake.c.next_attempt_at.is_not(None))).first() is not None]
    changed = 0
    for duplicate, document, reason, note in pending:
        try:
            with write_transaction(store.engine) as connection:
                # Its email, when it has not gone yet: kept for a person, never sent beside the document's.
                connection.execute(intake.update().where(
                    intake.c.inbound_fax_id == duplicate, intake.c.state == 'received',
                    intake.c.next_attempt_at.is_not(None)).values(
                    next_attempt_at=None, last_error=note[:200], version=intake.c.version + 1, updated_at=now))
                row = connection.execute(sa.select(items).where(items.c.inbound_fax_id == duplicate)
                                         ).mappings().one_or_none()
                whole = connection.execute(sa.select(items).where(items.c.inbound_fax_id == document)
                                           ).mappings().one_or_none()
                if row is None or row['state'] == 'done' or whole is None:
                    continue  # the document's item comes with the next feed
                item, whole = dict(row), dict(whole)
                store.change_on(connection, item, {'state': 'done', 'done_at': now, 'done_by': None,
                                                   'done_note': reason[:200]},
                                kind='done', actor_id=None, now=now, dedupe_key='folded',
                                details={'note': reason, 'folded_into': whole['id']})
                owner = item['owner_principal_id']
                if whole['owner_principal_id'] is None and whole['state'] == 'open' and owner:
                    place = store.placement_on(connection, document)
                    if may_own_on(control, connection, owner, place.resource_id):
                        names = store.names_on(connection, [owner])
                        store.change_on(connection, whole, {'owner_principal_id': owner, 'assigned_at': now},
                                        kind='assigned', actor_id=None, now=now,
                                        details={'to_name': names.get(owner), 'reason': reason})
                changed += 1
        except (WorkChanged, DeliveryStoreError):
            continue
    return changed
