"""Which documents of a case each recipient has, what it acknowledged, and the packet to send next.

A case keeps its original documents unchanged (``originals``). A ledger entry
is one document for one recipient, identified by its exact bytes (SHA-256),
its source, its version and the purpose it was sent for: the same bytes sent
for another purpose, or labelled as a newer version, are another entry.

A fax that carried an entry and finished successfully makes it **sent**, never
accepted. An entry is **accepted** only on the recipient's acknowledgement:
a partner's signed receipt for the packet (direct delivery), the receiving
team acknowledging a packet delivered to one of this installation's own
numbers (its work item), a received fax a person recorded as the recipient's
acknowledgement, or a person recording that the recipient confirmed it, with
a note. Acceptance is trusted for the recipient's reuse period (90 days unless
set) and then **expires**; the recipient saying it cannot find a document
**invalidates** it. Either way the next packet sends that document in full.

A packet may leave out an accepted document, and list it on a one-page index
instead, only when the recipient's destination profile says it accepts
references. A full-packet repair sends every kept original again as a new
fax; only a person starts one, and the reason is recorded.

History is append-only: carriages, acknowledgements and invalidations are
rows that are never changed. ``case_documents`` and ``case_packet_sends`` are
still written as before, for the sending-together separator page and the
savings count; ``case_documents.accepted_at`` is no longer written.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import read_connection, reflect, utcnow, write_transaction


CASE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,99}')
DEFAULT_REUSE_DAYS = 90
MAX_REUSE_DAYS = 3650
TERMINAL = frozenset({'success', 'failed', 'cancelled'})
LOCAL_SOURCE, LOCAL_ACCOUNT = 'local', 'local:installation'
LIMITS = {'title': 200, 'source': 120, 'version': 64, 'purpose': 120, 'document_type': 100}
MAX_NOTE = 500


class CaseInputError(ValueError):
    pass


class CaseConflict(RuntimeError):
    """A plain sentence: the change does not fit the case's current state."""


def clean(value, limit):
    """One line of text: whitespace collapsed, cut to the column's length."""
    return ' '.join(str(value or '').split())[:limit]


def check_case_id(value):
    if not isinstance(value, str) or CASE_ID.fullmatch(value) is None:
        raise CaseInputError('Use a case reference of letters, numbers, dots, dashes or colons, up to 100 characters.')
    return value


def check_note(value, *, required):
    note = str(value or '').strip()
    if required and len(note) < 3:
        raise CaseInputError('Add a short note: who confirmed it, and how.')
    if len(note) > MAX_NOTE:
        raise CaseInputError(f'Keep the note under {MAX_NOTE} characters.')
    return note or None


@dataclass(frozen=True)
class CaseDocument:
    title: str
    data: bytes
    pages: int
    source: str = ''
    version: str = ''
    document_type: str = ''
    document_date: datetime | None = None
    purpose: str = ''
    original_id: str | None = None

    @property
    def digest(self):
        return hashlib.sha256(self.data).hexdigest()

    @property
    def key(self):
        """The ledger identity: exact bytes, source, version and purpose."""
        return (self.digest, self.source, self.version, self.purpose)


def document(title, data, pages, **context):
    """A case document with its text fields cleaned to their stored lengths."""
    values = {name: clean(context.get(name), LIMITS[name]) for name in ('source', 'version', 'purpose',
                                                                       'document_type')}
    return CaseDocument(clean(title, LIMITS['title']) or 'Document', data, pages,
                        document_date=context.get('document_date'), original_id=context.get('original_id'), **values)


@dataclass(frozen=True)
class PacketPlan:
    included: tuple
    referenced: tuple
    references_allowed: bool
    # Why each included document goes in full, in the same order: new, sent (not yet
    # acknowledged), waiting, not_sent, expired, invalidated or references_off.
    why: tuple = field(default=())

    @property
    def unique(self):
        """Included documents with their bytes once: the same file for two entries is faxed once."""
        seen, result = set(), []
        for item in self.included:
            if item.digest not in seen:
                seen.add(item.digest)
                result.append(item)
        return result

    @property
    def pages(self):
        index = 1 if self.referenced else 0
        return index + sum(document.pages for document in self.unique)

    @property
    def pages_saved(self):
        return max(0, sum(entry['pages'] for entry in self.referenced) - (1 if self.referenced else 0))


def _state(view, sends, events, now, reuse_days):
    """The entry's state from its carriages, events and the recipient's reuse period."""
    def carried(since=None):
        later = [send for send in sends if since is None or send['created_at'] >= since]
        if any(send['delivery'] == 'success' for send in later):
            return 'sent'
        if any(send['delivery'] not in TERMINAL for send in later):
            return 'waiting'
        return 'not_sent' if later or since is None else None
    latest = events[-1] if events else None
    accepted = next((event for event in reversed(events) if event['kind'] == 'accepted'), None)
    view.update(accepted_at=None, accepted_how=None, accepted_by=None, accepted_note=None, expires_at=None,
                invalidated_at=None, invalidated_note=None)
    if accepted is not None:
        view.update(accepted_at=accepted['occurred_at'], accepted_how=accepted['source'],
                    accepted_by=accepted['principal_name'], accepted_note=accepted['note'])
    if latest is not None and latest['kind'] == 'accepted':
        expires = latest['occurred_at'] + timedelta(days=reuse_days) if reuse_days else None
        view['expires_at'] = expires
        if expires is None or now < expires:
            return 'accepted'
        return carried(expires) or 'expired'
    if latest is not None:
        view.update(invalidated_at=latest['occurred_at'], invalidated_note=latest['note'])
        return carried(latest['occurred_at']) or 'invalidated'
    return carried()


class CaseLedger:
    TABLES = ('case_documents', 'case_packet_sends', 'case_originals', 'case_entries', 'case_entry_sends',
              'case_entry_events', 'case_recipients', 'case_packets', 'delivery_destinations', 'outbound_deliveries',
              'direct_deliveries', 'inbound_imports', 'work_items', 'access_principals')

    def __init__(self, engine, data_dir=None):
        self.engine = engine
        self.data_dir = data_dir
        self.t = reflect(engine, self.TABLES)
        self.documents = self.t['case_documents']
        self.destinations = self.t['delivery_destinations']
        self.deliveries = self.t['outbound_deliveries']

    # Recipient settings ---------------------------------------------------------------------------------
    def recipient_on(self, connection, recipient):
        r = self.t['case_recipients']
        row = connection.execute(sa.select(r).where(r.c.phone_number == recipient)).mappings().one_or_none()
        days = row['reuse_days'] if row is not None else None
        return {'to': recipient, 'reuse_days': DEFAULT_REUSE_DAYS if days is None else days,
                'reuse_days_default': DEFAULT_REUSE_DAYS, 'reuse_days_set': days is not None,
                'version': row['version'] if row is not None else 0}

    def recipient(self, recipient):
        with read_connection(self.engine) as connection:
            return self.recipient_on(connection, recipient)

    def set_reuse_days(self, recipient, days, *, expected_version=None):
        """Days a reference to an accepted document is trusted: None for Faxbot's default, 0 for no limit."""
        if days is not None and (isinstance(days, bool) or not isinstance(days, int)
                                 or not 0 <= days <= MAX_REUSE_DAYS):
            raise CaseInputError(f'Use a number of days from 0 (no limit) to {MAX_REUSE_DAYS}.')
        r, now = self.t['case_recipients'], utcnow()
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(r).where(r.c.phone_number == recipient)).mappings().one_or_none()
            current = row['version'] if row is not None else 0
            if expected_version is not None and expected_version != current:
                raise CaseConflict('Someone else changed this recipient; reload and try again.')
            if row is None:
                connection.execute(r.insert().values(id=uuid4().hex, phone_number=recipient, reuse_days=days,
                                                     version=1, created_at=now, updated_at=now))
            else:
                connection.execute(r.update().where(r.c.id == row['id']).values(
                    reuse_days=days, version=current + 1, updated_at=now))
            return self.recipient_on(connection, recipient)

    def references_allowed(self, recipient):
        with read_connection(self.engine) as connection:
            value = connection.scalar(sa.select(self.destinations.c.accepts_references).where(
                self.destinations.c.phone_number == recipient))
        return value == 1

    # Originals ------------------------------------------------------------------------------------------
    def _path(self, digest):
        if self.data_dir is None or re.fullmatch('[a-f0-9]{64}', digest) is None:
            raise CaseConflict('Case documents are not available on this installation.')
        return Path(self.data_dir) / 'cases' / f'{digest}.pdf'

    def _store(self, item):
        """Keep the exact bytes once, named by their SHA-256; an existing copy must match."""
        path = self._path(item.digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != item.digest:
                raise CaseConflict('A kept case document does not match its record; restore it from a backup.')
            return
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(item.data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _actor_on(self, connection, principal_id):
        if principal_id is None:
            return {'principal_id': None, 'principal_name': None}
        principals = self.t['access_principals']
        name = connection.scalar(sa.select(principals.c.display_name).where(principals.c.id == principal_id))
        return {'principal_id': principal_id,
                'principal_name': name[:200] if isinstance(name, str) and name else None}

    def keep(self, case_id, documents, *, principal_id=None):
        """Add documents to the case's originals; returns them with their original's id."""
        o, now = self.t['case_originals'], utcnow()
        for item in documents:
            if item.original_id is None:
                self._store(item)
        kept = []
        with write_transaction(self.engine) as connection:
            who = self._actor_on(connection, principal_id)
            for item in documents:
                if item.original_id is not None:
                    kept.append(item)
                    continue
                row = connection.execute(sa.select(o.c.id).where(
                    o.c.case_id == case_id, o.c.digest == item.digest, o.c.source == item.source,
                    o.c.version == item.version)).first()
                if row is None:
                    identity = uuid4().hex
                    connection.execute(o.insert().values(
                        id=identity, case_id=case_id, digest=item.digest, title=item.title,
                        document_type=item.document_type, document_date=item.document_date, source=item.source,
                        version=item.version, page_count=item.pages, size_bytes=len(item.data), created_at=now,
                        **who))
                else:
                    identity = row.id
                kept.append(replace(item, original_id=identity))
        return kept

    def originals(self, case_id):
        o = self.t['case_originals']
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(o).where(o.c.case_id == case_id).order_by(
                o.c.created_at, o.c.title, o.c.digest)).mappings()]

    def load(self, row, *, purpose=''):
        """The kept original as a document, its bytes checked against the record."""
        path = self._path(row['digest'])
        if path.is_symlink() or not path.is_file():
            raise CaseConflict(f"The kept copy of '{row['title']}' is missing; add it to the case again.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != row['digest']:
            raise CaseConflict(f"The kept copy of '{row['title']}' has changed; add it to the case again.")
        return CaseDocument(row['title'], data, row['page_count'], source=row['source'], version=row['version'],
                            document_type=row['document_type'], document_date=row['document_date'],
                            purpose=purpose, original_id=row['id'])

    # Acknowledgements -----------------------------------------------------------------------------------
    def _settle_on(self, connection, entry_ids, now):
        """Record acknowledgements that arrived as evidence: partner receipts and acknowledged work items.

        Writers share one serialized write transaction (``write_transaction``),
        so checking for the dedupe key and inserting cannot race.
        """
        if not entry_ids:
            return
        t = self.t
        s, e, dd = t['case_entry_sends'], t['case_entry_events'], t['direct_deliveries']
        i, w = t['inbound_imports'], t['work_items']
        found = []
        for row in connection.execute(sa.select(s.c.entry_id, s.c.job_id, dd.c.id, dd.c.accepted_at).join(
                dd, dd.c.job_id == s.c.job_id).where(
                s.c.entry_id.in_(entry_ids), dd.c.direction == 'outbound', dd.c.state == 'accepted',
                dd.c.accepted_at.is_not(None))):
            found.append((row.entry_id, row.job_id, 'partner_receipt', row.id, row.accepted_at, None))
        when = sa.func.coalesce(w.c.acknowledged_at, w.c.done_at)
        for row in connection.execute(sa.select(
                s.c.entry_id, s.c.job_id, w.c.id, when.label('at'),
                sa.func.coalesce(w.c.acknowledged_by, w.c.done_by).label('by')).join(
                i, sa.and_(i.c.operation_id == s.c.job_id, i.c.source == LOCAL_SOURCE, i.c.account == LOCAL_ACCOUNT,
                           i.c.revision == '')).join(w, w.c.inbound_fax_id == i.c.inbound_fax_id).where(
                s.c.entry_id.in_(entry_ids), when.is_not(None))):
            found.append((row.entry_id, row.job_id, 'work_acknowledged', row.id, row.at, row.by))
        if not found:
            return
        recorded = {(row.entry_id, row.dedupe_key) for row in connection.execute(sa.select(
            e.c.entry_id, e.c.dedupe_key).where(e.c.entry_id.in_(entry_ids), e.c.dedupe_key.is_not(None)))}
        for entry_id, job_id, source, evidence_id, at, by in found:
            key = f'{source}:{evidence_id}'
            if (entry_id, key) in recorded:
                continue
            recorded.add((entry_id, key))
            connection.execute(e.insert().values(
                id=uuid4().hex, entry_id=entry_id, kind='accepted', source=source, job_id=job_id,
                evidence_id=evidence_id, dedupe_key=key, occurred_at=at, created_at=now,
                **self._actor_on(connection, by)))

    def _views_on(self, connection, rows, now):
        """Entry views with their state, oldest first; ``rows`` share one recipient's reuse period per row."""
        if not rows:
            return []
        t = self.t
        s, e, o = t['case_entry_sends'], t['case_entry_events'], self.deliveries
        ids = [row['id'] for row in rows]
        sends, events = {}, {}
        for row in connection.execute(sa.select(s, o.c.state.label('delivery'), o.c.updated_at.label('delivered_at'))
                                      .select_from(s.outerjoin(o, o.c.id == s.c.job_id))
                                      .where(s.c.entry_id.in_(ids)).order_by(s.c.created_at, s.c.id)).mappings():
            sends.setdefault(row['entry_id'], []).append(dict(row))
        for row in connection.execute(sa.select(e).where(e.c.entry_id.in_(ids)).order_by(
                e.c.occurred_at, e.c.created_at, e.c.id)).mappings():
            events.setdefault(row['entry_id'], []).append(dict(row))
        settings = {}
        views = []
        for row in rows:
            if row['recipient'] not in settings:
                settings[row['recipient']] = self.recipient_on(connection, row['recipient'])['reuse_days']
            carried = sends.get(row['id'], [])
            delivered = [send for send in carried if send['delivery'] == 'success']
            view = {**row, 'pages': row['page_count'], 'sends': len(carried),
                    'fax_id': carried[-1]['job_id'] if carried else None,
                    'first_page': carried[0]['first_page'] if carried else None,
                    'sent_at': delivered[-1]['delivered_at'] if delivered else None}
            view['state'] = _state(view, carried, events.get(row['id'], []), now, settings[row['recipient']])
            views.append(view)
        views.sort(key=lambda view: (view['created_at'], view['first_page'] or 0, view['title'], view['id']))
        return views

    def entries_on(self, connection, case_id, recipient, now):
        c = self.t['case_entries']
        rows = [dict(row) for row in connection.execute(sa.select(c).where(
            c.c.case_id == case_id, c.c.recipient == recipient)).mappings()]
        self._settle_on(connection, [row['id'] for row in rows], now)
        return self._views_on(connection, rows, now)

    def entries(self, case_id, recipient, *, now=None):
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            return self.entries_on(connection, case_id, recipient, now)

    def _chosen_on(self, connection, case_id, recipient, entry_ids, now):
        views = self.entries_on(connection, case_id, recipient, now)
        if not entry_ids:
            raise CaseInputError('Choose at least one document.')
        by_id = {view['id']: view for view in views}
        missing = [identity for identity in entry_ids if identity not in by_id]
        if missing:
            raise CaseInputError('A chosen document was not sent to this recipient for this case.')
        return [by_id[identity] for identity in dict.fromkeys(entry_ids)]

    def confirm(self, case_id, recipient, entry_ids, *, note, principal_id=None, received_fax_id=None, now=None):
        """A person records that the recipient confirmed it has these documents (or sent this fax saying so)."""
        note = check_note(note, required=received_fax_id is None)
        now = now or utcnow()
        e = self.t['case_entry_events']
        with write_transaction(self.engine) as connection:
            chosen = self._chosen_on(connection, case_id, recipient, entry_ids, now)
            unsent = [view['title'] for view in chosen if view['sent_at'] is None]
            if unsent:
                raise CaseConflict(f"'{unsent[0]}' has not been delivered yet, so the recipient cannot have "
                                   'acknowledged it. Wait until its fax is delivered.')
            who = self._actor_on(connection, principal_id)
            for view in chosen:
                connection.execute(e.insert().values(
                    id=uuid4().hex, entry_id=view['id'], kind='accepted',
                    source='received_fax' if received_fax_id else 'person', evidence_id=received_fax_id,
                    note=note, occurred_at=now, created_at=now, **who))
            return self.entries_on(connection, case_id, recipient, now)

    def invalidate(self, case_id, recipient, entry_ids, *, note=None, principal_id=None, now=None):
        """The recipient could not find these documents: the next packet sends them in full."""
        note = check_note(note, required=False)
        now = now or utcnow()
        e = self.t['case_entry_events']
        with write_transaction(self.engine) as connection:
            chosen = self._chosen_on(connection, case_id, recipient, entry_ids, now)
            who = self._actor_on(connection, principal_id)
            for view in chosen:
                connection.execute(e.insert().values(
                    id=uuid4().hex, entry_id=view['id'], kind='invalidated', source='cache_miss', note=note,
                    occurred_at=now, created_at=now, **who))
            return self.entries_on(connection, case_id, recipient, now)

    # Cases -------------------------------------------------------------------------------------------------
    def recent(self, limit=50, *, now=None):
        """The newest cases sent packets, one row per case and recipient, newest first."""
        now = now or utcnow()
        c, s = self.t['case_entries'], self.t['case_entry_sends']
        last_send = sa.select(s.c.entry_id, sa.func.max(s.c.created_at).label('at')).group_by(
            s.c.entry_id).subquery()
        last = sa.func.max(sa.func.coalesce(last_send.c.at, c.c.created_at))
        query = (sa.select(c.c.case_id, c.c.recipient, last.label('last_sent_at'))
                 .select_from(c.outerjoin(last_send, last_send.c.entry_id == c.c.id))
                 .group_by(c.c.case_id, c.c.recipient)
                 .order_by(last.desc(), c.c.case_id, c.c.recipient).limit(limit))
        with write_transaction(self.engine) as connection:
            groups = connection.execute(query).mappings().all()
            result = []
            for group in groups:
                views = self.entries_on(connection, group['case_id'], group['recipient'], now)
                states = [view['state'] for view in views]
                result.append({'case_id': group['case_id'], 'recipient': group['recipient'],
                               'documents': len(views), 'accepted': states.count('accepted'),
                               'sent': sum(1 for view in views if view['sent_at'] is not None),
                               'needs_attention': sum(state in {'expired', 'invalidated', 'not_sent'}
                                                      for state in states),
                               'pages': sum(view['page_count'] for view in views),
                               'last_sent_at': group['last_sent_at']})
            numbers = sorted({row['recipient'] for row in result})
            allowed = set(connection.execute(sa.select(self.destinations.c.phone_number).where(
                self.destinations.c.phone_number.in_(numbers), self.destinations.c.accepts_references == 1)).scalars()
            ) if numbers else set()
        for row in result:
            row['accepts_references'] = row['recipient'] in allowed
        return result

    def in_flight(self, case_id, recipient):
        """Case packet faxes to this recipient that have not finished, newest first."""
        p, o = self.t['case_packet_sends'], self.deliveries
        with read_connection(self.engine) as connection:
            return [row.id for row in connection.execute(sa.select(p.c.id).join(o, o.c.id == p.c.id).where(
                p.c.case_id == case_id, p.c.recipient == recipient, o.c.state.not_in(tuple(TERMINAL))).order_by(
                p.c.created_at.desc()))]

    # Packets -----------------------------------------------------------------------------------------------
    def plan(self, case_id, recipient, documents, *, now=None):
        """Split the submission into documents to send and accepted ones to reference."""
        now = now or utcnow()
        unique, seen = [], set()
        for item in documents:
            if item.key not in seen:
                seen.add(item.key)
                unique.append(item)
        allowed = self.references_allowed(recipient)
        known = {(view['digest'], view['source'], view['version'], view['purpose']): view
                 for view in self.entries(case_id, recipient, now=now)}
        included, referenced, why = [], [], []
        for item in unique:
            view = known.get(item.key)
            if allowed and view is not None and view['state'] == 'accepted':
                referenced.append(view)
                continue
            included.append(item)
            if view is None:
                why.append('new')
            elif not allowed:
                why.append('references_off')
            else:
                why.append(view['state'])
        return PacketPlan(tuple(included), tuple(referenced), allowed, tuple(why))

    def full_packet(self, case_id, recipient, *, now=None):
        """Every document of the case sent to this recipient, from kept originals, for a repair.

        Returns (documents, titles without a kept original). Entries sharing bytes keep
        their own purpose, so each one's carriage is recorded; the fax carries the bytes once.
        """
        o = self.t['case_originals']
        views = self.entries(case_id, recipient, now=now)
        with read_connection(self.engine) as connection:
            rows = {row['id']: dict(row) for row in connection.execute(sa.select(o).where(
                o.c.case_id == case_id)).mappings()}
        by_digest = {}
        for row in sorted(rows.values(), key=lambda row: (row['created_at'], row['id'])):
            by_digest.setdefault(row['digest'], row)
        documents, missing = [], []
        for view in views:
            row = rows.get(view['original_id']) or by_digest.get(view['digest'])
            if row is None:
                missing.append(view['title'])
                continue
            item = self.load(row, purpose=view['purpose'])
            documents.append(replace(item, title=view['title'], source=view['source'], version=view['version']))
        return documents, missing

    def record(self, case_id, recipient, plan, job_id, *, kind='update', purpose='', reason=None,
               checklist_id=None, principal_id=None):
        """Remember what this fax carries; acknowledgement comes later, from the recipient."""
        t, now = self.t, utcnow()
        c, s = t['case_entries'], t['case_entry_sends']
        with write_transaction(self.engine) as connection:
            # What this packet left out, so its savings can be counted later.
            connection.execute(t['case_packet_sends'].insert().values(
                id=job_id, case_id=case_id, recipient=recipient, pages_sent=plan.pages,
                pages_left_out=sum(entry['pages'] for entry in plan.referenced),
                documents_left_out=len(plan.referenced), created_at=now))
            who = self._actor_on(connection, principal_id)
            connection.execute(t['case_packets'].insert().values(
                id=job_id, case_id=case_id, recipient=recipient, kind=kind, purpose=clean(purpose, LIMITS['purpose']),
                reason=reason, checklist_id=checklist_id, created_at=now, **who))
            pages, page = {}, 2 if plan.referenced else 1
            for item in plan.unique:
                pages[item.digest] = (page, page + item.pages - 1)
                page += item.pages
            for item in plan.included:
                first, last = pages[item.digest]
                entry = connection.execute(sa.select(c.c.id).where(
                    c.c.case_id == case_id, c.c.recipient == recipient, c.c.digest == item.digest,
                    c.c.source == item.source, c.c.version == item.version,
                    c.c.purpose == item.purpose)).scalar_one_or_none()
                if entry is None:
                    entry = uuid4().hex
                    connection.execute(c.insert().values(
                        id=entry, case_id=case_id, recipient=recipient, digest=item.digest, source=item.source,
                        version=item.version, purpose=item.purpose, title=item.title, page_count=item.pages,
                        original_id=item.original_id, created_at=now))
                connection.execute(s.insert().values(id=uuid4().hex, entry_id=entry, job_id=job_id, first_page=first,
                                                     last_page=last, created_at=now))
            for item in plan.unique:
                self._legacy_on(connection, case_id, recipient, item, job_id, pages[item.digest], now)

    def _legacy_on(self, connection, case_id, recipient, item, job_id, pages, now):
        """Keep ``case_documents`` as before: the sending-together separator page reads it."""
        d = self.documents
        existing = connection.execute(sa.select(d).where(
            d.c.case_id == case_id, d.c.recipient == recipient, d.c.digest == item.digest)).mappings().one_or_none()
        if existing is None:
            connection.execute(d.insert().values(
                id=uuid4().hex, case_id=case_id, recipient=recipient, digest=item.digest, title=item.title,
                page_count=item.pages, first_page=pages[0], last_page=pages[1], source_job_id=job_id, created_at=now))
        elif existing['accepted_at'] is None:
            # The newest fax carries it now.
            connection.execute(d.update().where(d.c.id == existing['id']).values(
                source_job_id=job_id, first_page=pages[0], last_page=pages[1]))


def _day(moment):
    from ..people_time import installation_zone_name, zone
    from datetime import timezone
    local = moment.replace(tzinfo=timezone.utc).astimezone(zone(installation_zone_name()))
    return f'{local.day} {local:%B %Y}'


def index_page(case_id, recipient, plan, organization, purpose=''):
    """The one-page index that stands in for documents the recipient already accepted."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    y = 740

    def line(text, font='Helvetica', size=11, gap=16):
        nonlocal y
        pdf.setFont(font, size)
        pdf.drawString(54, y, text[:110])
        y -= gap

    def label(item_title, version, source):
        extra = ', '.join(part for part in (f'version {version}' if version else '', f'from {source}' if source else '')
                          if part)
        return f'{item_title} ({extra})' if extra else item_title
    line(f'Case {case_id}: documents for {recipient}', 'Helvetica-Bold', 14, 24)
    if purpose:
        line(f'Purpose: {purpose}')
    line(f'From {organization}. This fax carries only new or revised documents.')
    line('The documents listed below were already acknowledged by you for this case and are not repeated.')
    line(f'To receive every document again in full, contact {organization}.', gap=24)
    line('Already acknowledged', 'Helvetica-Bold', 12, 18)
    for entry in plan.referenced:
        pages = '1 page' if entry['pages'] == 1 else f"{entry['pages']} pages"
        line(f"{label(entry['title'], entry['version'], entry['source'])}, {pages}, acknowledged "
             f"{_day(entry['accepted_at'])}, reference {entry['digest'][:12]}")
        if y < 160:
            line('More acknowledged documents are listed in the case record.')
            break
    y -= 8
    line('Included in this fax', 'Helvetica-Bold', 12, 18)
    page = 2
    for item in plan.unique:
        pages = '1 page' if item.pages == 1 else f'{item.pages} pages'
        line(f'{label(item.title, item.version, item.source)} ({pages}), starting on page {page}')
        page += item.pages
        if y < 72:
            break
    pdf.showPage()
    pdf.save()
    return output.getvalue()


def compose(case_id, recipient, plan, organization, purpose=''):
    """The packet PDF: an index page when documents are referenced, then each included document once."""
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    if plan.referenced:
        for page in PdfReader(BytesIO(index_page(case_id, recipient, plan, organization, purpose))).pages:
            writer.add_page(page)
    for item in plan.unique:
        for page in PdfReader(BytesIO(item.data)).pages:
            writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
