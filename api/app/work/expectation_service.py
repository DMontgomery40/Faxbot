"""Access-checked expected-fax operations for people (CE3) and outage recovery (CE6).

Each operation runs in one access transaction, so the visibility check, the
permission check and the change are one decision. An expectation is visible
when the actor holds ``work:read`` for a fax that would be filed in its mailbox
(``authorize_child_on``); lists, counts, the report, reconciliation lists and
exports apply that before filters and limits. An invisible expectation is "not
found"; a visible one with a denied action is "not permitted". A linked fax's
details are shown only to someone who may read that fax. Every change needs the
version the person last saw.

Permissions: reading is ``work:read``; adding, confirming, rejecting,
cancelling and recording completion elsewhere is ``work:manage``; exporting is
``work:export``, each for the expectation's mailbox. Import sources and imports
need ``work:import`` at the installation and ``work:manage`` for every mailbox a
row lands in. Outage mode needs ``work:import`` or ``settings:write`` at the
installation.
"""
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import zipfile

import sqlalchemy as sa

from .. import people_time
from ..access.receiving_rules import normalize_subaddress
from ..access.types import ResourceRef
from ..routing.database import utcnow
from ..routing.numbers import stored_number
from . import expectation_imports as importing
from . import expectation_text as text
from .expectations import (CODE_LENGTH, CODE_LETTERS, ENTERED, OPEN_STATES, ExpectationChanged, loads,
                           mailbox_coverage, may_back_up_mailbox, reference_key)
from .imports import ImportInputError


NOT_FOUND = 'This expected fax was not found.'
CHANGED = 'This expected fax changed; reload and try again.'
FORBIDDEN = 'You do not have permission to do this.'
VIEWS = ('waiting', 'overdue', 'proposed', 'missing', 'conflicts', 'closed', 'all')
OUTAGE_PERMISSIONS = ('work:import', 'settings:write')
CHANNELS = ('fax', 'email', 'phone', 'other')
FORMAT = 'faxbot-expected-fax-evidence-1'
LIMITS = ('A match by subaddress, registered form, message ID or email subject shows what the sender stated; it '
          'does not prove who sent the document.',
          'The due time is an operational target, not a legal deadline.')


class ExpectedError(Exception):
    status = 400

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class ExpectedInputError(ExpectedError):
    status = 400


class ExpectedForbidden(ExpectedError):
    status = 403


class ExpectedNotFound(ExpectedError):
    status = 404


class ExpectedConflict(ExpectedError):
    status = 409


def _plain(value, what, limit, *, required=False):
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ExpectedInputError(f'Add {what}.')
        return None
    if not isinstance(value, str):
        raise ExpectedInputError(f'{what.capitalize()} must be text.')
    value = ' '.join(value.split())
    if len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ExpectedInputError(f'{what.capitalize()} can be up to {limit} characters of plain text.')
    return value


def _version(value):
    if type(value) is not int or value < 1:
        raise ExpectedInputError('Send the version of the expected fax you are changing.')
    return value


def _note(value, what='a short note'):
    note = value.strip() if isinstance(value, str) else ''
    if not 0 < len(note) <= 300 or any(ord(c) < 32 for c in note):
        raise ExpectedInputError(f'Add {what}, up to 300 characters.')
    return note


def _utc(value):
    """An aware time as naive UTC; a naive one is taken as UTC already."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _iso(value):
    return value.isoformat(timespec='seconds') + 'Z' if value is not None else None


class ExpectationService:
    def __init__(self, store, access, *, values, clock=None):
        """``access`` carries the AccessStore (``store``) and AccessControl (``control``)."""
        self.store = store
        self.access_store, self.control = access.store, access.control
        self.values = values
        self.clock = clock or utcnow

    # -- shared ----------------------------------------------------------------------
    def _country(self):
        return getattr(self.values(), 'fax_default_country', 'US') or 'US'

    def _zone(self):
        return people_time.zone(getattr(self.values(), 'time_zone', '') or '')

    def _hours(self):
        return getattr(self.values(), 'work_acknowledge_hours', 0) or 0

    def _covered(self, connection, actor, permission, now):
        return mailbox_coverage(self.control, connection, actor, permission, now, self.store)

    def _installation(self, connection, actor, permissions, now):
        if not any(self.control.authorize_on(connection, actor, permission, ResourceRef('installation'),
                                             now=now).allowed for permission in permissions):
            raise ExpectedForbidden(FORBIDDEN)

    def _may(self, connection, actor, permissions, now):
        return any(self.control.authorize_on(connection, actor, permission, ResourceRef('installation'),
                                             now=now).allowed for permission in permissions)

    def _find(self, connection, ident):
        if not isinstance(ident, str) or not 0 < len(ident) <= 40 or any(not 33 <= ord(c) < 127 for c in ident):
            return None
        table = self.store.expectations
        code = ident.upper()
        if len(code) == CODE_LENGTH and set(code) <= set(CODE_LETTERS):
            row = connection.execute(sa.select(table).where(table.c.code == code)).mappings().one_or_none()
            if row is not None:
                return dict(row)
        row = connection.execute(sa.select(table).where(table.c.id == ident)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def _require(self, connection, actor, ident, permission, now):
        """The visible expectation; not found unless the actor may read it, forbidden if the action is denied."""
        self.control._current_source_on(connection, actor, now)
        row = self._find(connection, ident)
        if row is None or row['mailbox_id'] not in self._covered(connection, actor, 'work:read', now):
            raise ExpectedNotFound(NOT_FOUND)
        if permission != 'work:read' and row['mailbox_id'] not in self._covered(connection, actor, permission, now):
            raise ExpectedForbidden(FORBIDDEN)
        return row

    @staticmethod
    def _expect(row, version):
        if _version(version) != row['version']:
            raise ExpectedConflict(CHANGED)

    def _name(self, connection, actor):
        return self.store.names_on(connection, [actor.principal_id]).get(actor.principal_id)

    def _labels(self, connection):
        mailboxes = self.store.mailboxes
        return dict(connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label)).all())

    def _visible_faxes(self, connection, actor, inbound_ids, now):
        """The received faxes among ``inbound_ids`` this actor may read (``work:read`` on the fax)."""
        ids = sorted({value for value in inbound_ids if value})
        if not ids:
            return set()
        resources = self.store.resources
        visible = self.control.visible_resource_ids_on(connection, actor, 'work:read', 'inbound', now=now).subquery()
        return set(connection.execute(sa.select(resources.c.inbound_fax_id).where(
            resources.c.kind == 'inbound', resources.c.inbound_fax_id.in_(ids),
            resources.c.id.in_(sa.select(visible.c.id)))).scalars())

    def _fax_summaries(self, connection, inbound_ids):
        ids = sorted(inbound_ids)
        if not ids:
            return {}
        inbound, items, mailboxes = self.store.inbound, self.store.items, self.store.mailboxes
        rows = connection.execute(
            sa.select(inbound.c.id, inbound.c.from_number, inbound.c.to_number, inbound.c.pages,
                      inbound.c.received_at, items.c.available_at, mailboxes.c.label)
            .select_from(inbound.outerjoin(items, items.c.inbound_fax_id == inbound.c.id)
                         .outerjoin(mailboxes, mailboxes.c.id == items.c.mailbox_id))
            .where(inbound.c.id.in_(ids))).all()
        return {row.id: {'inbound_fax_id': row.id, 'from_number': row.from_number, 'to_number': row.to_number,
                         'pages': row.pages, 'received_at': row.available_at or row.received_at,
                         'mailbox': row.label} for row in rows}

    def _source_names(self, connection, keys):
        keys = sorted({key for key in keys if key and key != ENTERED})
        if not keys:
            return {}
        sources = self.store.sources
        return dict(connection.execute(sa.select(sources.c.id, sources.c.name).where(sources.c.id.in_(keys))).all())

    def _views(self, connection, actor, rows, now, *, detail=False):
        rows = [dict(row) for row in rows]
        if not rows:
            return []
        store = self.store
        names = store.names_on(connection, [row[field] for row in rows
                                            for field in ('owner_principal_id', 'matched_by', 'closed_by')])
        labels = self._labels(connection)
        sources = self._source_names(connection, [row['source_key'] for row in rows])
        manage = self._covered(connection, actor, 'work:manage', now)
        export = self._covered(connection, actor, 'work:export', now)
        links = {}
        for link in connection.execute(sa.select(store.links).where(
                store.links.c.expectation_id.in_([row['id'] for row in rows]),
                store.links.c.state.in_(('proposed', 'automatic', 'confirmed', 'also_arrived')))
                .order_by(store.links.c.created_at, store.links.c.id)).mappings():
            links.setdefault(link['expectation_id'], []).append(dict(link))
        faxes = {link['inbound_fax_id'] for found in links.values() for link in found}
        visible = self._visible_faxes(connection, actor, faxes, now)
        summaries = self._fax_summaries(connection, visible)
        replaced = {}
        replaces = sorted({row['replaces_id'] for row in rows if row['replaces_id']})
        if replaces:
            replaced = dict(connection.execute(sa.select(store.expectations.c.id, store.expectations.c.code).where(
                store.expectations.c.id.in_(replaces))).all())
        views = []
        for row in rows:
            found = links.get(row['id'], [])
            matched = next((link for link in found if link['id'] == row['matched_link_id']), None)
            waiting = row['state'] in OPEN_STATES
            can_manage = row['mailbox_id'] in manage
            proposals = [{
                'id': link['id'], 'signal': link['signal'], 'strength': link['strength'],
                'text': text.proposal_text(link, row), 'version': link['version'],
                'fax': summaries.get(link['inbound_fax_id']),
                'can_decide': waiting and can_manage and link['inbound_fax_id'] in visible,
            } for link in found if link['state'] == 'proposed']
            actions = []
            if waiting and can_manage:
                actions += ['cancel', 'completed_elsewhere', 'match']
            if row['conflict_at'] is not None and can_manage:
                actions.append('resolve_conflict')
            if row['mailbox_id'] in export:
                actions.append('export')
            person = (lambda identity: {'id': identity, 'name': names.get(identity)} if identity else None)
            view = {
                'id': row['id'], 'code': row['code'], 'reference': row['reference'], 'kind': row['kind'],
                'description': row['description'], 'required_parts': loads(row['required_parts'], []) or [],
                'required_revision': row['required_revision'], 'counterparty': row['counterparty'],
                'fax_numbers': loads(row['counterparty_numbers'], []) or [], 'direct_address': row['direct_address'],
                'mailbox': labels.get(row['mailbox_id']), 'mailbox_id': row['mailbox_id'],
                'owner': person(row['owner_principal_id']),
                'state': row['state'], 'state_key': text.state_key(row, now),
                'state_text': text.state_text(row, names, now, match_signal=matched['signal'] if matched else None),
                'due_at': row['due_at'], 'due_text': text.due_text(row),
                'overdue': waiting and row['due_at'] is not None and now > row['due_at'],
                'source': sources.get(row['source_key']), 'operation_id': row['operation_id']
                if row['source_key'] != ENTERED else None,
                'revision': row['revision'] or None, 'replaces': replaced.get(row['replaces_id']),
                'missing_from_export': row['missing_since'] is not None and waiting,
                'conflict': row['conflict_at'] is not None,
                'matched_at': row['matched_at'],
                'match': {'signal': matched['signal'], 'by': names.get(row['matched_by']),
                          'fax': summaries.get(matched['inbound_fax_id'])} if matched else None,
                'also_arrived': sum(1 for link in found if link['state'] == 'also_arrived'),
                'proposals': proposals,
                'keys': {'subaddress': row['subaddress_key'], 'email_subject': row['subject_key'],
                         'message_id': row['message_key'], 'form_field': row['form_field']},
                'created_at': row['created_at'], 'version': row['version'], 'actions': actions,
            }
            views.append(view)
        return views

    # -- reads -----------------------------------------------------------------------
    def list(self, actor, *, view='waiting', mailbox=None, search=None, limit=100):
        if view not in VIEWS or type(limit) is not int or not 1 <= limit <= 500 \
                or (mailbox is not None and (type(mailbox) is not str or len(mailbox) > 100)) \
                or (search is not None and (type(search) is not str or len(search) > 200)):
            raise ExpectedInputError('Choose a valid filter.')
        table = self.store.expectations
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            readable = self._covered(connection, actor, 'work:read', now)
            query = sa.select(table).where(table.c.mailbox_id.in_(sorted(readable)))
            query = query.where(*self._view_filter(view, now))
            if mailbox:
                found = next((key for key, label in self._labels(connection).items()
                              if reference_key(label) == reference_key(mailbox)), None)
                if found is None:
                    return []
                query = query.where(table.c.mailbox_id == found)
            if search:
                query = query.where(sa.or_(table.c.reference_key.contains(reference_key(search), autoescape=True),
                                           table.c.code == search.strip().upper()))
            order = sa.case((table.c.state == 'proposed_match', 0), (table.c.state == 'overdue', 1),
                            (table.c.state == 'open', 2), else_=3)
            query = query.order_by(order, table.c.due_at.is_(None), table.c.due_at, table.c.created_at.desc(),
                                   table.c.id).limit(limit)
            return self._views(connection, actor, connection.execute(query).mappings().all(), now)

    def _view_filter(self, view, now):
        table = self.store.expectations
        waiting = table.c.state.in_(OPEN_STATES)
        overdue = sa.and_(waiting, table.c.due_at.is_not(None), table.c.due_at < now)
        return {
            'waiting': [waiting], 'overdue': [overdue], 'proposed': [table.c.state == 'proposed_match'],
            'missing': [waiting, table.c.missing_since.is_not(None)], 'conflicts': [table.c.conflict_at.is_not(None)],
            'closed': [~waiting], 'all': [],
        }[view]

    def counts(self, actor):
        table = self.store.expectations
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            readable = self._covered(connection, actor, 'work:read', now)
            base = sa.select(sa.func.count()).select_from(table).where(table.c.mailbox_id.in_(sorted(readable)))
            result = {view: connection.execute(base.where(*self._view_filter(view, now))).scalar_one()
                      for view in ('waiting', 'overdue', 'proposed', 'missing', 'conflicts')}
            result['matched'] = connection.execute(base.where(table.c.state == 'matched')).scalar_one()
            return result

    def detail(self, actor, ident):
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:read', now)
            return self._views(connection, actor, [row], now, detail=True)[0]

    def history(self, actor, ident):
        events = self.store.events
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:read', now)
            rows = connection.execute(sa.select(events).where(events.c.expectation_id == row['id'])
                                      .order_by(events.c.occurred_at, events.c.created_at, events.c.id)).mappings().all()
        return [self.event_view(event) for event in rows]

    @staticmethod
    def event_view(row):
        details = loads(row['details'], {}) or {}
        details.setdefault('actor_name', row['actor_name'])
        return {'kind': row['kind'], 'occurred_at': row['occurred_at'], 'actor': row['actor_name'],
                'source': row['source'], 'text': text.event_text(row['kind'], details)}

    def mailboxes(self, actor):
        """Mailboxes where this person may add expected faxes, by name."""
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            manage = self._covered(connection, actor, 'work:manage', now)
            labels = self._labels(connection)
            return sorted(({'id': key, 'label': labels.get(key)} for key in manage if key in labels),
                          key=lambda entry: (entry['label'] or '').casefold())

    # -- adding and deciding -----------------------------------------------------------
    def add(self, actor, entry):
        """Expect a fax: one expectation a person adds by hand."""
        if not isinstance(entry, dict):
            raise ExpectedInputError('Describe the fax you expect.')
        reference = _plain(entry.get('reference'), 'the business reference, such as PO 483', 200, required=True)
        kind = _plain(entry.get('kind'), 'what you expect, such as a signed acknowledgement', 100, required=True)
        description = _plain(entry.get('description'), 'the description', 500)
        counterparty = _plain(entry.get('counterparty'), 'who will send it', 200)
        revision = _plain(entry.get('required_revision'), 'the revision', 40)
        direct = _plain(entry.get('direct_address'), 'the Direct address', 320)
        subject = _plain(entry.get('email_subject'), 'the email subject', 200)
        message = _plain(entry.get('message_id'), 'the message ID', 512)
        form_field = _plain(entry.get('form_field'), 'the form field', 100)
        revision_field = _plain(entry.get('revision_field'), 'the revision field', 100)
        parts = entry.get('required_parts') or []
        if not isinstance(parts, list) or len(parts) > 10:
            raise ExpectedInputError('List up to 10 parts it must include.')
        parts = [_plain(part, 'each part', 100) for part in parts]
        parts = [part for part in parts if part]
        numbers = entry.get('fax_numbers') or []
        if not isinstance(numbers, list) or len(numbers) > 20:
            raise ExpectedInputError('List up to 20 fax numbers it may come from.')
        numbers = sorted({stored_number(_plain(number, 'each fax number', 40), country=self._country())
                          for number in numbers if _plain(number, 'each fax number', 40)})
        sub = _plain(entry.get('subaddress'), 'the subaddress', 40)
        if sub:
            sub = normalize_subaddress(sub)
            if sub is None:
                raise ExpectedInputError('A subaddress is up to 20 digits, such as 4831; it may also use +, # and *.')
        else:
            sub = normalize_subaddress(reference)
        due_at, hours = entry.get('due_at'), entry.get('due_hours')
        if due_at is not None and not isinstance(due_at, datetime):
            raise ExpectedInputError('Choose when it is due.')
        if hours is not None and (type(hours) is not int or not 1 <= hours <= 8760):
            raise ExpectedInputError('Enter a due time from 1 to 8760 hours.')
        mailbox_id, owner = entry.get('mailbox_id'), entry.get('owner_principal_id')
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            manage = self._covered(connection, actor, 'work:manage', now)
            if not isinstance(mailbox_id, str) or mailbox_id not in manage:
                raise ExpectedInputError('Choose a mailbox where you may add expected faxes.')
            due = _utc(due_at)
            if due is not None and due <= now:
                raise ExpectedInputError('Choose a due time in the future.')
            if owner is not None and not (isinstance(owner, str) and may_back_up_mailbox(
                    self.control, connection, owner, manage[mailbox_id])):
                raise ExpectedInputError('That person cannot see every fax in this mailbox, so they cannot own it.')
            table = self.store.expectations
            already = connection.execute(sa.select(table.c.code).where(
                table.c.mailbox_id == mailbox_id, table.c.reference_key == reference_key(reference),
                table.c.state.in_(OPEN_STATES))).scalar()
            if already:
                raise ExpectedConflict(f'{reference} is already expected in this mailbox (code {already}).')
            values = {
                'reference': reference, 'kind': kind, 'description': description,
                'required_parts': json.dumps(parts) if parts else None, 'required_revision': revision,
                'counterparty': counterparty, 'counterparty_numbers': json.dumps(numbers) if numbers else None,
                'direct_address': direct, 'partner_id': _plain(entry.get('partner_id'), 'the partner', 40),
                'mailbox_id': mailbox_id, 'owner_principal_id': owner, 'subaddress_key': sub,
                'subject_key': subject, 'message_key': message, 'form_field': form_field,
                'revision_field': revision_field, 'source_key': ENTERED, 'operation_id': uuid4().hex,
                'revision': revision or '', 'window_start': now,
            }
            values['due_at'], values['due_hours'], values['due_source'] = self.store.due_for(
                connection, mailbox_id, start=now, due_at=due, due_hours=hours,
                due_source='entered', installation_hours=self._hours())
            name = self._name(connection, actor)
            row = self.store.create_on(connection, values, source='person', now=now, actor_id=actor.principal_id,
                                       actor_name=name, details={'actor_name': name})
            return self._views(connection, actor, [row], now, detail=True)[0]

    def _change(self, connection, actor, row, values, *, kind, details, now, evidence=None):
        name = self._name(connection, actor)
        try:
            self.store.change_on(connection, row, values, kind=kind, source='person', now=now,
                                 actor_id=actor.principal_id, actor_name=name,
                                 details={'actor_name': name, **details}, evidence=evidence)
        except ExpectationChanged:
            raise ExpectedConflict(CHANGED) from None
        return self._views(connection, actor, [self.store.expectation_on(connection, row['id'])], now, detail=True)[0]

    def _link(self, connection, row, link_id):
        links = self.store.links
        link = connection.execute(sa.select(links).where(links.c.id == link_id, links.c.expectation_id == row['id'])
                                  ).mappings().one_or_none() if isinstance(link_id, str) and len(link_id) <= 40 else None
        if link is None:
            raise ExpectedNotFound('That proposed fax was not found.')
        if link['state'] != 'proposed':
            raise ExpectedConflict('This proposal was already decided.')
        return dict(link)

    def _decide_link(self, connection, link, state, actor, name, now, note=None):
        links = self.store.links
        result = connection.execute(links.update().where(links.c.id == link['id'], links.c.version == link['version'])
                                    .values(state=state, decided_by=actor.principal_id, decided_by_name=name,
                                            decided_at=now, note=note, version=link['version'] + 1, updated_at=now))
        if result.rowcount != 1:
            raise ExpectedConflict(CHANGED)

    def confirm(self, actor, ident, link_id, *, version):
        """A person confirms that a proposed received fax answers this expectation."""
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:manage', now)
            self._expect(row, version)
            if row['state'] not in OPEN_STATES:
                raise ExpectedConflict('This expected fax is already closed.')
            link = self._link(connection, row, link_id)
            if link['inbound_fax_id'] not in self._visible_faxes(connection, actor, [link['inbound_fax_id']], now):
                raise ExpectedForbidden('You cannot open this fax, so you cannot confirm it.')
            name = self._name(connection, actor)
            self._decide_link(connection, link, 'confirmed', actor, name, now)
            view = self._change(connection, actor, row, {
                'state': 'matched', 'matched_at': now, 'matched_inbound_fax_id': link['inbound_fax_id'],
                'matched_link_id': link['id'], 'matched_by': actor.principal_id, 'closed_at': now},
                kind='confirmed', details={'signal': link['signal']}, now=now,
                evidence={'inbound_fax_id': link['inbound_fax_id'], 'work_item_id': link['work_item_id'],
                          'link_id': link['id']})
            self.store.settle_proposals_on(connection, row['id'], now=now, keep=link['id'])
            return view

    def reject(self, actor, ident, link_id, *, version, note=None):
        """A person says a proposed received fax does not answer this expectation."""
        note = _note(note, 'a short reason') if note else None
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:manage', now)
            self._expect(row, version)
            link = self._link(connection, row, link_id)
            if link['inbound_fax_id'] not in self._visible_faxes(connection, actor, [link['inbound_fax_id']], now):
                raise ExpectedForbidden('You cannot open this fax, so you cannot reject it.')
            name = self._name(connection, actor)
            self._decide_link(connection, link, 'rejected', actor, name, now, note)
            links = self.store.links
            pending = connection.execute(sa.select(sa.func.count()).select_from(links).where(
                links.c.expectation_id == row['id'], links.c.state == 'proposed')).scalar_one()
            values = {}
            if row['state'] == 'proposed_match' and not pending:
                values['state'] = 'overdue' if row['escalated_at'] is not None else 'open'
            return self._change(connection, actor, row, values, kind='proposal_rejected',
                                details={'note': note, 'signal': link['signal']}, now=now,
                                evidence={'inbound_fax_id': link['inbound_fax_id'], 'link_id': link['id']})

    def match(self, actor, ident, inbound_fax_id, *, version, note=None):
        """A person links a received fax to this expectation by hand, which closes it."""
        note = _note(note, 'a short note') if note else None
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:manage', now)
            self._expect(row, version)
            if row['state'] not in OPEN_STATES:
                raise ExpectedConflict('This expected fax is already closed.')
            if not isinstance(inbound_fax_id, str) or inbound_fax_id not in self._visible_faxes(
                    connection, actor, [inbound_fax_id], now):
                raise ExpectedNotFound('That received fax was not found.')
            store, name = self.store, self._name(connection, actor)
            existing = connection.execute(sa.select(store.links).where(
                store.links.c.expectation_id == row['id'], store.links.c.inbound_fax_id == inbound_fax_id)
            ).mappings().one_or_none()
            if existing is not None and existing['state'] == 'proposed':
                link_id = existing['id']
                self._decide_link(connection, dict(existing), 'confirmed', actor, name, now, note)
                signal = existing['signal']
            elif existing is not None:
                raise ExpectedConflict('This fax was already decided for this expected fax.')
            else:
                link_id, signal = uuid4().hex, 'person'
                work_item = connection.execute(sa.select(store.items.c.id).where(
                    store.items.c.inbound_fax_id == inbound_fax_id)).scalar_one_or_none()
                connection.execute(store.links.insert().values(
                    id=link_id, expectation_id=row['id'], inbound_fax_id=inbound_fax_id, work_item_id=work_item,
                    strength='strong', signal='person', state='confirmed', evidence='{}', decided_by=actor.principal_id,
                    decided_by_name=name, decided_at=now, note=note, version=1, created_at=now, updated_at=now))
            view = self._change(connection, actor, row, {
                'state': 'matched', 'matched_at': now, 'matched_inbound_fax_id': inbound_fax_id,
                'matched_link_id': link_id, 'matched_by': actor.principal_id, 'closed_at': now},
                kind='confirmed', details={'signal': signal, 'note': note}, now=now,
                evidence={'inbound_fax_id': inbound_fax_id, 'link_id': link_id})
            self.store.settle_proposals_on(connection, row['id'], now=now, keep=link_id)
            return view

    def close(self, actor, ident, *, outcome, note, version):
        """Cancel it, or record that it was completed another way (by phone, a portal, in person)."""
        if outcome not in ('cancelled', 'completed_elsewhere'):
            raise ExpectedInputError('Choose cancel or completed another way.')
        note = _note(note, 'a short note saying why' if outcome == 'cancelled' else 'a short note saying how')
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:manage', now)
            self._expect(row, version)
            if row['state'] not in OPEN_STATES:
                raise ExpectedConflict('This expected fax is already closed.')
            view = self._change(connection, actor, row, {
                'state': outcome, 'closed_at': now, 'closed_by': actor.principal_id, 'closed_note': note},
                kind=outcome, details={'note': note}, now=now)
            self.store.settle_proposals_on(connection, row['id'], now=now, keep=None)
            return view

    def resolve_conflict(self, actor, ident, *, choice, version):
        """Keep the first version of an imported row, or use the import's changed version."""
        if choice not in ('keep', 'apply'):
            raise ExpectedInputError('Choose keep or apply.')
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:manage', now)
            self._expect(row, version)
            if row['conflict_at'] is None:
                raise ExpectedConflict('There is no change waiting for a decision.')
            if choice == 'keep':
                return self._change(connection, actor, row, {'conflict_at': None}, kind='conflict_kept',
                                    details={}, now=now)
            events = self.store.events
            latest = connection.execute(sa.select(events.c.details).where(
                events.c.expectation_id == row['id'], events.c.kind == 'conflict')
                .order_by(events.c.created_at.desc(), events.c.id.desc())).scalar()
            details = loads(latest, {}) or {}
            changed, digest = details.get('row'), details.get('digest')
            if not isinstance(changed, dict) or not digest:
                raise ExpectedConflict('The changed version is no longer available; import the file again.')
            changed = {**{field: None for field in ('description', 'counterparty', 'direct_address',
                                                    'email_subject', 'message_id', 'required_revision', 'due',
                                                    'window_start', 'subaddress')},
                       'fax_numbers': [], 'required_parts': [], **changed}
            source = connection.execute(sa.select(self.store.sources).where(
                self.store.sources.c.id == row['source_key'])).mappings().one_or_none()
            if source is None:
                raise ExpectedConflict('The import source is no longer saved.')
            values = importing.expectation_values(changed, dict(source), run_id=row['last_import_id'],
                                                  digest=digest, now=row['window_start'])
            for name in ('source_key', 'operation_id', 'revision', 'last_import_id', 'window_start'):
                values.pop(name)
            if changed.get('due'):
                values['due_at'] = datetime.fromisoformat(changed['due'])
                values['due_hours'], values['due_source'] = None, 'import'
            values.update(conflict_at=None, reference_key=reference_key(values['reference']))
            return self._change(connection, actor, row, values, kind='conflict_applied', details={}, now=now)

    # -- the report: what is missing, and what arrived unmatched ------------------------
    def report(self, actor, *, days=30):
        if type(days) is not int or not 1 <= days <= 365:
            raise ExpectedInputError('Choose a period from 1 to 365 days.')
        store = self.store
        table, arrivals, links, inbound = store.expectations, store.arrivals, store.links, store.inbound
        with self.access_store.transaction() as connection:
            now = self.clock()
            since = now - timedelta(days=days)
            self.control._current_source_on(connection, actor, now)
            readable = sorted(self._covered(connection, actor, 'work:read', now))
            base = sa.select(sa.func.count()).select_from(table).where(table.c.mailbox_id.in_(readable),
                                                                       table.c.created_at >= since)
            count = lambda *conditions: connection.execute(base.where(*conditions)).scalar_one()
            matched_auto = connection.execute(
                sa.select(sa.func.count()).select_from(table.join(links, links.c.id == table.c.matched_link_id))
                .where(table.c.mailbox_id.in_(readable), table.c.created_at >= since, table.c.state == 'matched',
                       links.c.state == 'automatic')).scalar_one()
            totals = {
                'expected': count(), 'matched': count(table.c.state == 'matched'), 'matched_automatically': matched_auto,
                'waiting': count(table.c.state.in_(OPEN_STATES)),
                'overdue': count(table.c.state.in_(OPEN_STATES), table.c.due_at.is_not(None), table.c.due_at < now),
                'closed_otherwise': count(table.c.state.in_(('cancelled', 'completed_elsewhere', 'replaced'))),
            }
            unmatched = connection.execute(sa.select(table).where(
                table.c.mailbox_id.in_(readable), table.c.state.in_(OPEN_STATES), table.c.created_at >= since)
                .order_by(table.c.due_at.is_(None), table.c.due_at, table.c.created_at).limit(50)).mappings().all()
            # Arrivals: every received fax this person may read in the period, and which closed or proposed nothing.
            visible = self.control.visible_resource_ids_on(connection, actor, 'work:read', 'inbound', now=now).subquery()
            resources = store.resources
            seen = (sa.select(arrivals, inbound.c.from_number)
                    .select_from(arrivals.join(inbound, inbound.c.id == arrivals.c.id)
                                 .join(resources, sa.and_(resources.c.kind == 'inbound',
                                                          resources.c.inbound_fax_id == arrivals.c.id)))
                    .where(arrivals.c.available_at >= since, resources.c.id.in_(sa.select(visible.c.id))))
            linked = sa.exists(sa.select(1).where(links.c.inbound_fax_id == arrivals.c.id,
                                                  links.c.state.in_(('automatic', 'confirmed', 'proposed',
                                                                     'also_arrived'))))
            arrived = connection.execute(seen.with_only_columns(sa.func.count())).scalar_one()
            unlinked = connection.execute(seen.with_only_columns(sa.func.count()).where(~linked)).scalar_one()
            numbers = set()
            for value in connection.execute(sa.select(table.c.counterparty_numbers).where(
                    table.c.mailbox_id.in_(readable), table.c.counterparty_numbers.is_not(None))).scalars():
                numbers.update(loads(value, []) or [])
            candidates = connection.execute(seen.where(~linked, sa.or_(
                arrivals.c.referenced == 1, inbound.c.from_number.in_(sorted(numbers))))
                .order_by(arrivals.c.available_at.desc()).limit(50)).mappings().all()
            # Received faxes that never became a stored document: a missing acquisition is not a match.
            not_stored = connection.execute(
                sa.select(sa.func.count()).select_from(inbound.join(resources, sa.and_(
                    resources.c.kind == 'inbound', resources.c.inbound_fax_id == inbound.c.id)))
                .where(resources.c.id.in_(sa.select(visible.c.id)), inbound.c.received_at >= since,
                       sa.func.lower(inbound.c.status).in_(('waiting', 'failed')))).scalar_one()
            labels = self._labels(connection)
            entries = []
            for row in candidates:
                signals = loads(row['signals'], {}) or {}
                if signals.get('subaddress'):
                    why = f"It carried subaddress {signals['subaddress']}, which no expected fax uses."
                elif signals.get('form_values'):
                    why = 'It came as a registered form that no expected fax names.'
                else:
                    why = 'It came from a fax number listed on an expected fax, but nothing in it named one.'
                entries.append({'inbound_fax_id': row['id'], 'from_number': row['from_number'],
                                'received_at': row['available_at'], 'mailbox': labels.get(row['mailbox_id']),
                                'why': why})
            views = self._views(connection, actor, unmatched, now)
        rate = (f"{totals['matched']} of {totals['expected']} expected faxes arrived and were matched"
                if totals['expected'] else 'No faxes were expected')
        summary = (f"{rate} in the last {days} days; {totals['waiting']} are still waiting, {totals['overdue']} "
                   f"overdue. {unlinked} of {arrived} received faxes answered no expected fax"
                   + (f", and {not_stored} received faxes could not be stored; they are listed with the reason in "
                      'Faxes → Received (faxbot received list).' if not_stored else '.'))
        return {'days': days, 'since': since, **totals, 'arrived': arrived, 'arrived_unmatched': unlinked,
                'not_stored': not_stored, 'unmatched_expected': views, 'unmatched_arrivals': entries,
                'summary': summary}

    # -- import sources and imports ----------------------------------------------------
    def _source_view(self, row, labels):
        return {'id': row['id'], 'name': row['name'], 'format': row['format'], 'mapping': loads(row['mapping'], {}),
                'mailbox_id': row['mailbox_id'], 'mailbox': labels.get(row['mailbox_id']),
                'due_hours': row['due_hours'], 'subject_template': row['subject_template'],
                'subaddress_template': row['subaddress_template'], 'form_field': row['form_field'],
                'revision_field': row['revision_field'], 'version': row['version'], 'updated_at': row['updated_at']}

    def sources(self, actor):
        table = self.store.sources
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, ('work:import',) + OUTAGE_PERMISSIONS[1:], now)
            labels = self._labels(connection)
            rows = connection.execute(sa.select(table).order_by(table.c.name, table.c.id)).mappings().all()
            return [self._source_view(dict(row), labels) for row in rows]

    def _source_row(self, connection, ident):
        table = self.store.sources
        if not isinstance(ident, str) or not 0 < len(ident) <= 100:
            return None
        row = connection.execute(sa.select(table).where(sa.or_(
            table.c.id == ident, table.c.normalized_name == reference_key(ident)))).mappings().first()
        return dict(row) if row is not None else None

    def save_source(self, actor, entry):
        """Create or change an import source: its name, format and the column mapping, saved once."""
        if not isinstance(entry, dict):
            raise ExpectedInputError('Describe the import source.')
        name = _plain(entry.get('name'), 'a name for the import source', 100, required=True)
        fmt = entry.get('format')
        if fmt not in importing.FORMATS:
            raise ExpectedInputError('Choose CSV or JSON.')
        try:
            mapping = importing.clean_mapping(entry.get('mapping'))
            subject = importing.clean_template(entry.get('subject_template'), 'The email subject pattern')
            sub = importing.clean_template(entry.get('subaddress_template'), 'The subaddress pattern')
        except ImportInputError as error:
            raise ExpectedInputError(str(error)) from None
        hours = entry.get('due_hours')
        if hours is not None and (type(hours) is not int or not 0 <= hours <= 8760):
            raise ExpectedInputError('Enter a due time from 0 to 8760 hours, or leave it empty.')
        form_field = _plain(entry.get('form_field'), 'the form field', 100)
        revision_field = _plain(entry.get('revision_field'), 'the revision field', 100)
        table = self.store.sources
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, ('work:import',), now)
            mailbox = entry.get('mailbox_id')
            if mailbox is not None and (not isinstance(mailbox, str)
                                        or mailbox not in self._covered(connection, actor, 'work:manage', now)):
                raise ExpectedInputError('Choose a mailbox where you may add expected faxes.')
            values = dict(name=name, normalized_name=reference_key(name), format=fmt, mapping=json.dumps(mapping),
                          mailbox_id=mailbox, due_hours=hours or None, subject_template=subject,
                          subaddress_template=sub, form_field=form_field, revision_field=revision_field,
                          updated_at=now)
            other = connection.execute(sa.select(table.c.id).where(
                table.c.normalized_name == values['normalized_name'])).scalar()
            identity = entry.get('id')
            if identity is None:
                if other is not None:
                    raise ExpectedConflict(f'An import source called {name} already exists.')
                identity = uuid4().hex
                name_of = self._name(connection, actor)
                connection.execute(table.insert().values(id=identity, version=1, created_at=now,
                                                         created_by=actor.principal_id, created_by_name=name_of,
                                                         **values))
            else:
                current = self._source_row(connection, identity)
                if current is None:
                    raise ExpectedNotFound('That import source was not found.')
                if other is not None and other != current['id']:
                    raise ExpectedConflict(f'An import source called {name} already exists.')
                version = _version(entry.get('version'))
                result = connection.execute(table.update().where(table.c.id == current['id'],
                                                                 table.c.version == version)
                                            .values(version=version + 1, **values))
                if result.rowcount != 1:
                    raise ExpectedConflict('This import source changed; reload and try again.')
                identity = current['id']
            return self._source_view(self._source_row(connection, identity), self._labels(connection))

    def import_file(self, actor, source_ident, data, *, file_name=None, full=False):
        """Import an open-work export through a saved source; returns what happened, row by row in summary."""
        file_name = (_plain(file_name, 'the file name', 200) or None) if file_name else None
        if type(full) is not bool:
            raise ExpectedInputError('Say whether this file is a full export.')
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, ('work:import',), now)
            source = self._source_row(connection, source_ident)
            if source is None:
                raise ExpectedNotFound('That import source was not found.')
        try:
            raw_rows = importing.parse_rows(data, source['format'])
        except ImportInputError as error:
            raise ExpectedInputError(str(error)) from None
        mapping = loads(source['mapping'], {}) or {}
        digest = hashlib.sha256(data).hexdigest()
        runs, store = self.store.imports, self.store
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, ('work:import',), now)
            name = self._name(connection, actor)
            run = connection.execute(sa.select(runs).where(
                runs.c.source_id == source['id'], runs.c.file_digest == digest,
                runs.c.full_export == (1 if full else 0))).mappings().one_or_none()
            replay = run is not None
            if run is None:
                identity = uuid4().hex
                connection.execute(runs.insert().values(
                    id=identity, source_id=source['id'], file_digest=digest, file_name=file_name,
                    full_export=1 if full else 0, state='running', rows_total=len(raw_rows), created_count=0,
                    unchanged_count=0, revised_count=0, conflict_count=0, missing_count=0, problem_count=0,
                    created_by=actor.principal_id, created_by_name=name, created_at=now))
                run = connection.execute(sa.select(runs).where(runs.c.id == identity)).mappings().one()
            run = dict(run)
            labels = self._labels(connection)
        mailboxes = {reference_key(label): key for key, label in labels.items() if label}
        counts = {'created': 0, 'unchanged': 0, 'revised': 0, 'conflict': 0}
        problems, identities, seen = [], [], set()
        normalized = []
        for number, raw in enumerate(raw_rows, start=2 if source['format'] == 'csv' else 1):
            try:
                row = importing.normalize_row(raw, mapping, source, country=self._country(), zone=self._zone(),
                                              mailboxes=mailboxes)
            except importing.RowProblem as error:
                problems.append({'row': number, 'problem': str(error)})
                continue
            key = (row['operation_id'], row['revision'])
            if key in seen:
                problems.append({'row': number, 'problem': f"{row['reference']} appears twice in this file; the "
                                                           'first row was used.'})
                continue
            seen.add(key)
            normalized.append((number, row, importing.row_digest(row)))
        for start in range(0, len(normalized), importing.BATCH):
            with self.access_store.transaction() as connection:
                now = self.clock()
                self._installation(connection, actor, ('work:import',), now)
                manage = self._covered(connection, actor, 'work:manage', now)
                for number, row, row_hash in normalized[start:start + importing.BATCH]:
                    if row['mailbox_id'] not in manage:
                        problems.append({'row': number, 'problem': f"You cannot add expected faxes to "
                                                                   f"{labels.get(row['mailbox_id'], 'that mailbox')}."})
                        continue
                    try:
                        outcome = importing.apply_row(connection, store, row, row_hash, run=run, source=source,
                                                      now=now, actor_id=actor.principal_id, actor_name=name,
                                                      installation_hours=self._hours())
                    except ExpectationChanged:
                        # A received fax changed it at the same moment; importing the file again finishes the row.
                        problems.append({'row': number, 'problem': 'It changed while importing; import the file '
                                                                   'again to finish it.'})
                        continue
                    counts[outcome] += 1
                    identities.append([row['operation_id'], row['revision'], row_hash, row['mailbox_id'],
                                       row['reference']])
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, ('work:import',), now)
            missing = []
            if full and not problems:
                listed = {(entry[0], entry[1]) for entry in identities}
                missing = importing.missing_on(connection, store, source, run, listed, now=now)
            note = None
            if full and problems:
                note = 'Rows had problems, so this export was not used to find expected faxes it no longer lists.'
            connection.execute(runs.update().where(runs.c.id == run['id']).values(
                state='complete', rows_total=len(raw_rows), created_count=counts['created'],
                unchanged_count=counts['unchanged'], revised_count=counts['revised'],
                conflict_count=counts['conflict'], missing_count=len(missing), problem_count=len(problems),
                summary=importing.run_summary(problems, missing, note), rows=json.dumps(identities),
                completed_at=now))
            run = dict(connection.execute(sa.select(runs).where(runs.c.id == run['id'])).mappings().one())
            reconciled = self._reconcile_after_import(connection, actor, source, run, now)
            readable = self._covered(connection, actor, 'work:read', now)
        result = self._run_view(run, source, labels, readable)
        result.update(replay=replay, reconciled_outage=reconciled)
        return result

    def _run_view(self, run, source, labels, readable):
        summary = loads(run['summary'], {}) or {}
        missing = [dict(entry, mailbox=labels.get(entry['mailbox_id'])) for entry in summary.get('missing', [])
                   if entry.get('mailbox_id') in readable]
        sentence = import_sentence(run)
        if run['full_export']:
            sentence += (f" {len(missing)} expected faxes are no longer in this export; they stay open until you "
                         'decide.' if missing else ' Every expected fax still waiting is in this export.')
        return {'id': run['id'], 'source': source['name'], 'file_name': run['file_name'],
                'full_export': bool(run['full_export']), 'rows_total': run['rows_total'],
                'created': run['created_count'], 'unchanged': run['unchanged_count'],
                'revised': run['revised_count'], 'conflicts': run['conflict_count'],
                'problems': summary.get('problems', []), 'problem_count': run['problem_count'], 'missing': missing,
                'note': summary.get('note'), 'summary': sentence, 'created_at': run['created_at'],
                'completed_at': run['completed_at'], 'imported_by': run['created_by_name']}

    def imports(self, actor, *, limit=20):
        runs, sources = self.store.imports, self.store.sources
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, ('work:import',), now)
            labels, readable = self._labels(connection), self._covered(connection, actor, 'work:read', now)
            names = {row.id: dict(row._mapping) for row in connection.execute(sa.select(sources))}
            rows = connection.execute(sa.select(runs).order_by(runs.c.created_at.desc(), runs.c.id).limit(limit)
                                      ).mappings().all()
            return [self._run_view(dict(row), names.get(row['source_id'], {'name': None}), labels, readable)
                    for row in rows]

    # -- outage mode -------------------------------------------------------------------
    def _outage_row(self, connection, ident):
        table = self.store.outages
        if not isinstance(ident, str) or not 0 < len(ident) <= 40:
            return None
        row = connection.execute(sa.select(table).where(sa.or_(table.c.id == ident, table.c.code == ident.upper()))
                                 ).mappings().first()
        return dict(row) if row is not None else None

    def _outage_view(self, connection, actor, row, now, *, detail=False):
        sources = self._source_names(connection, [row['source_id']])
        view = {'id': row['id'], 'code': row['code'], 'source': sources.get(row['source_id']),
                'source_id': row['source_id'], 'started_at': row['started_at'], 'ended_at': row['ended_at'],
                'note': row['note'], 'declared_by': row['declared_by_name'], 'ended_by': row['ended_by_name'],
                'open': row['ended_at'] is None, 'version': row['version']}
        view['text'] = (f"Down since {text.time_text(row['started_at'])}." if row['ended_at'] is None else
                        f"Down from {text.time_text(row['started_at'])} to {text.time_text(row['ended_at'])}.")
        if not detail:
            return view
        actions = connection.execute(sa.select(self.store.actions).where(
            self.store.actions.c.outage_id == row['id']).order_by(self.store.actions.c.occurred_at,
                                                                  self.store.actions.c.id)).mappings().all()
        view['actions'] = [importing.action_view(dict(action)) for action in actions]
        latest = connection.execute(sa.select(self.store.reconciliations).where(
            self.store.reconciliations.c.outage_id == row['id']).order_by(
            self.store.reconciliations.c.created_at.desc(), self.store.reconciliations.c.id.desc())
        ).mappings().first()
        view['reconciliation'] = (self._reconciliation_view(connection, actor, dict(latest), now)
                                  if latest is not None else None)
        return view

    def _reconciliation_view(self, connection, actor, row, now):
        readable = self._covered(connection, actor, 'work:read', now)
        labels = self._labels(connection)

        def entries(value):
            kept = []
            for entry in loads(value, []) or []:
                # Only items in mailboxes this person can see; an action with no mailbox is the outage's own record.
                if entry.get('mailbox_id') is not None and entry['mailbox_id'] not in readable:
                    continue
                line = text.RECONCILE_TEXT[entry['reason']].format(recorded=entry.get('recorded_revision'),
                                                                   revision=entry.get('revision'))
                kept.append(dict(entry, mailbox=labels.get(entry.get('mailbox_id')), text=line))
            return kept
        done, new, unresolved = entries(row['already_done']), entries(row['new_items']), entries(row['unresolved'])
        return {'id': row['id'], 'created_at': row['created_at'], 'created_by': row['created_by_name'],
                'already_done': done, 'new': new, 'unresolved': unresolved,
                'summary': (f'{len(done)} already done (record them; do not submit them again), {len(new)} new '
                            f'(submit them normally), {len(unresolved)} held for you to decide.')}

    def outages(self, actor):
        table = self.store.outages
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, OUTAGE_PERMISSIONS, now)
            rows = connection.execute(sa.select(table).order_by(table.c.started_at.desc(), table.c.id).limit(50)
                                      ).mappings().all()
            return [self._outage_view(connection, actor, dict(row), now) for row in rows]

    def outage(self, actor, ident):
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, OUTAGE_PERMISSIONS, now)
            row = self._outage_row(connection, ident)
            if row is None:
                raise ExpectedNotFound('That outage was not found.')
            return self._outage_view(connection, actor, row, now, detail=True)

    def start_outage(self, actor, source_ident, *, started_at=None, note=None):
        note = _plain(note, 'the note', 300)
        table = self.store.outages
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, OUTAGE_PERMISSIONS, now)
            source = self._source_row(connection, source_ident)
            if source is None:
                raise ExpectedNotFound('That import source was not found.')
            started = _utc(started_at) or now
            if started > now:
                raise ExpectedInputError('The outage cannot start in the future.')
            if connection.execute(sa.select(table.c.id).where(table.c.source_id == source['id'],
                                                              table.c.ended_at.is_(None))).first():
                raise ExpectedConflict(f"{source['name']} is already marked down; end that outage first.")
            identity, name = uuid4().hex, self._name(connection, actor)
            connection.execute(table.insert().values(
                id=identity, code=self.store.unique_code_on(connection, table), source_id=source['id'],
                started_at=started, note=note, declared_by=actor.principal_id, declared_by_name=name, version=1,
                created_at=now, updated_at=now))
            return self._outage_view(connection, actor, self._outage_row(connection, identity), now, detail=True)

    def end_outage(self, actor, ident, *, version, ended_at=None):
        table = self.store.outages
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, OUTAGE_PERMISSIONS, now)
            row = self._outage_row(connection, ident)
            if row is None:
                raise ExpectedNotFound('That outage was not found.')
            if _version(version) != row['version']:
                raise ExpectedConflict('This outage changed; reload and try again.')
            if row['ended_at'] is not None:
                raise ExpectedConflict('This outage has already ended.')
            ended = _utc(ended_at) or now
            if not row['started_at'] <= ended <= now:
                raise ExpectedInputError('The end must be after the start and not in the future.')
            result = connection.execute(table.update().where(table.c.id == row['id'], table.c.version == version)
                                        .values(ended_at=ended, ended_by=actor.principal_id,
                                                ended_by_name=self._name(connection, actor),
                                                version=version + 1, updated_at=now))
            if result.rowcount != 1:
                raise ExpectedConflict('This outage changed; reload and try again.')
            return self._outage_view(connection, actor, self._outage_row(connection, row['id']), now, detail=True)

    def record_action(self, actor, ident, entry):
        """Record one action done by fax, email or phone during the outage, against the item's original ID."""
        if not isinstance(entry, dict):
            raise ExpectedInputError('Describe what was done.')
        operation = _plain(entry.get('operation_id'), "the item's ID in the source system", 100, required=True)
        revision = _plain(entry.get('revision'), 'the revision', 40) or ''
        reference = _plain(entry.get('reference'), 'the reference', 200)
        action = _plain(entry.get('action'), 'what was done, such as "Order sent to the supplier by fax"', 300,
                        required=True)
        channel = entry.get('channel')
        if channel not in CHANNELS:
            raise ExpectedInputError('Choose how it was done: fax, email, phone or other.')
        outcome = entry.get('outcome') or 'done'
        if outcome not in ('done', 'uncertain'):
            raise ExpectedInputError('Choose whether it is done or may not have gone through.')
        job = _plain(entry.get('fax_job_id'), 'the sent fax ID', 40)
        evidence = _plain(entry.get('evidence_note'), 'the evidence note', 300)
        actions = self.store.actions
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, OUTAGE_PERMISSIONS, now)
            outage = self._outage_row(connection, ident)
            if outage is None:
                raise ExpectedNotFound('That outage was not found.')
            occurred = _utc(entry.get('occurred_at')) or now
            if not outage['started_at'] <= occurred <= (outage['ended_at'] or now):
                raise ExpectedInputError('The action must fall within the outage.')
            if job is not None:
                # The sent fax must exist; whether it was delivered is checked when reconciling.
                from ..routing.database import reflect
                fax_jobs = reflect(self.store.engine, ('fax_jobs',))['fax_jobs']
                if connection.execute(sa.select(fax_jobs.c.id).where(fax_jobs.c.id == job)).first() is None:
                    raise ExpectedInputError('There is no sent fax with that ID.')
            name = self._name(connection, actor)
            connection.execute(actions.insert().values(
                id=uuid4().hex, outage_id=outage['id'], operation_id=operation, revision=revision,
                reference=reference, action=action, channel=channel, outcome=outcome, fax_job_id=job,
                evidence_note=evidence, occurred_at=occurred, recorded_by=actor.principal_id,
                recorded_by_name=name, created_at=now))
            table = self.store.expectations
            for row in connection.execute(sa.select(table).where(table.c.source_key == outage['source_id'],
                                                                 table.c.operation_id == operation)).mappings():
                self.store.event_on(connection, row['id'], 'outage_action', source='person', now=now,
                                    actor_id=actor.principal_id, actor_name=name,
                                    details={'action': action, 'channel': channel, 'outcome': outcome},
                                    evidence={'outage_id': outage['id'], 'fax_job_id': job}, occurred_at=occurred)
            return self._outage_view(connection, actor, outage, now, detail=True)

    def _reconcile_after_import(self, connection, actor, source, run, now):
        """The first import after an outage ends sorts its rows into the three lists; returns the outage's code."""
        outages, reconciliations = self.store.outages, self.store.reconciliations
        candidate = connection.execute(sa.select(outages).where(
            outages.c.source_id == source['id'], outages.c.ended_at.is_not(None), outages.c.ended_at <= now,
            ~sa.exists(sa.select(1).where(reconciliations.c.outage_id == outages.c.id)))
            .order_by(outages.c.ended_at.desc())).mappings().first()
        if candidate is None:
            return None
        importing.reconcile_on(connection, self.store, dict(candidate), run, source, now=now,
                               actor_id=actor.principal_id, actor_name=self._name(connection, actor))
        return candidate['code']

    def reconcile(self, actor, ident):
        """Sort the latest import made after the outage ended into the three lists again; never submits anything."""
        runs = self.store.imports
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._installation(connection, actor, OUTAGE_PERMISSIONS, now)
            outage = self._outage_row(connection, ident)
            if outage is None:
                raise ExpectedNotFound('That outage was not found.')
            if outage['ended_at'] is None:
                raise ExpectedConflict('End the outage first; Faxbot sorts the first export imported after the '
                                       'system is back.')
            source = self._source_row(connection, outage['source_id'])
            run = connection.execute(sa.select(runs).where(
                runs.c.source_id == outage['source_id'], runs.c.state == 'complete',
                runs.c.completed_at >= outage['ended_at']).order_by(runs.c.completed_at.desc())).mappings().first()
            if run is None or source is None:
                raise ExpectedConflict(f"First import an export from {source['name'] if source else 'the system'} "
                                       'made after the outage ended.')
            importing.reconcile_on(connection, self.store, outage, dict(run), source, now=now,
                                   actor_id=actor.principal_id, actor_name=self._name(connection, actor))
            return self._outage_view(connection, actor, outage, now, detail=True)

    # -- evidence ----------------------------------------------------------------------
    def export(self, actor, ident):
        """Return (zip bytes, file name): the expectation, its history, its match and what is missing."""
        store = self.store
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, ident, 'work:export', now)
            view = self._views(connection, actor, [row], now, detail=True)[0]
            events = connection.execute(sa.select(store.events).where(
                store.events.c.expectation_id == row['id'], store.events.c.kind != 'exported')
                .order_by(store.events.c.occurred_at, store.events.c.created_at, store.events.c.id)).mappings().all()
            links = [dict(link) for link in connection.execute(sa.select(store.links).where(
                store.links.c.expectation_id == row['id']).order_by(store.links.c.created_at, store.links.c.id))
                .mappings()]
            visible = self._visible_faxes(connection, actor, [link['inbound_fax_id'] for link in links], now)
            summaries = self._fax_summaries(connection, visible)
            actions = []
            if row['source_key'] != ENTERED:
                actions = [importing.action_view(dict(action)) for action in connection.execute(
                    sa.select(store.actions).select_from(store.actions.join(
                        store.outages, store.outages.c.id == store.actions.c.outage_id))
                    .where(store.outages.c.source_id == row['source_key'],
                           store.actions.c.operation_id == row['operation_id'])
                    .order_by(store.actions.c.occurred_at)).mappings()]
            names = store.names_on(connection, [actor.principal_id])
        missing = []
        if row['state'] in OPEN_STATES:
            missing.append('Nothing that closes it has arrived.')
        if not links:
            missing.append('No received fax has been linked to it.')
        if any(link['inbound_fax_id'] not in visible for link in links):
            missing.append('A linked fax was withheld because you do not have permission to read it.')
        if row['state'] == 'matched' and row['matched_by']:
            missing.append('The match rests on a person\'s decision; no reference the sender stated confirms it.')
        if row['due_at'] is None:
            missing.append('No due time was set.')
        if row['missing_since'] is not None:
            missing.append(f"The source's full export of {text.time_text(row['missing_since'])} no longer lists it.")
        if row['conflict_at'] is not None:
            missing.append('The import changed this row without a new revision, and no one has decided which '
                           'version stands.')
        history = [{'occurred_at': _iso(event['occurred_at']), 'kind': event['kind'], 'source': event['source'],
                    'actor': event['actor_name'], 'evidence': loads(event['evidence']),
                    'text': self.event_view(event)['text']} for event in events]
        export_id = uuid4().hex
        manifest = {
            'export': {'id': export_id, 'created_at': _iso(now), 'exported_by': names.get(actor.principal_id),
                       'format': FORMAT, 'snapshot_version': row['version']},
            'expected': {
                'code': row['code'], 'reference': row['reference'], 'kind': row['kind'],
                'description': row['description'], 'required_parts': view['required_parts'],
                'required_revision': row['required_revision'], 'counterparty': row['counterparty'],
                'fax_numbers': view['fax_numbers'], 'direct_address': row['direct_address'],
                'mailbox': view['mailbox'], 'owner': (view['owner'] or {}).get('name'), 'state': row['state'],
                'state_text': view['state_text'], 'window_start': _iso(row['window_start']),
                'due_at': _iso(row['due_at']), 'due_rule': view['due_text'], 'created_at': _iso(row['created_at']),
                'created_by': row['created_by_name'], 'escalated_at': _iso(row['escalated_at']),
                'matched_at': _iso(row['matched_at']), 'closed_at': _iso(row['closed_at']),
                'closed_note': row['closed_note'], 'missing_since': _iso(row['missing_since']),
                'source': None if row['source_key'] == ENTERED else {
                    'name': view['source'], 'operation_id': row['operation_id'], 'revision': row['revision'] or None,
                    'row_sha256': row['row_digest'], 'replaces': view['replaces']},
                'match_keys': view['keys'],
            },
            'links': [{
                'state': link['state'], 'strength': link['strength'], 'signal': link['signal'],
                'reason': text.proposal_text(link, row) if link['state'] in ('proposed', 'rejected') and link['reason']
                else None, 'evidence': loads(link['evidence'], {}), 'decided_by': link['decided_by_name'],
                'decided_at': _iso(link['decided_at']), 'note': link['note'], 'created_at': _iso(link['created_at']),
                'document': ({**summaries[link['inbound_fax_id']],
                              'received_at': _iso(summaries[link['inbound_fax_id']]['received_at'])}
                             if link['inbound_fax_id'] in summaries else None),
            } for link in links],
            'outage_actions': actions,
            'history': history,
            'missing': missing,
            'limits': list(LIMITS),
        }
        document = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False).encode('utf-8') + b'\n'
        lines = [f"Evidence for the expected fax {row['reference']} ({row['code']}).",
                 f"Exported by {names.get(actor.principal_id) or 'an unnamed account'} on {text.time_text(now)}.",
                 view['state_text'], view['due_text'], '']
        lines += [f"{text.time_text(event['occurred_at'])}  {self.event_view(event)['text']}" for event in events]
        if missing:
            lines += ['', 'Not included or not recorded:'] + [f'- {item}' for item in missing]
        lines += ['', *LIMITS]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', document)
            archive.writestr('history.txt', '\n'.join(lines) + '\n')
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, row['id'], 'work:export', now)
            name = self._name(connection, actor)
            self.store.event_on(connection, row['id'], 'exported', source='person', now=now,
                                actor_id=actor.principal_id, actor_name=name,
                                details={'actor_name': name, 'export_id': export_id,
                                         'manifest_sha256': hashlib.sha256(document).hexdigest()})
        return buffer.getvalue(), f"faxbot-expected-{row['code']}.zip"


def import_sentence(run):
    """What an import did, naming only the counts that are not zero, such as "5 rows: 3 unchanged, 1 new revision"."""
    rows = run['rows_total']
    if not rows:
        return 'The file has no rows.'
    counts = ((run['created_count'], 'new', 'new'), (run['unchanged_count'], 'unchanged', 'unchanged'),
              (run['revised_count'], 'new revision', 'new revisions'),
              (run['conflict_count'], 'changed without a new revision', 'changed without a new revision'),
              (run['problem_count'], 'with a problem', 'with problems'))
    parts = [f'{count} {one if count == 1 else many}' for count, one, many in counts if count]
    return f"{rows} {'row' if rows == 1 else 'rows'}: " + ', '.join(parts) + '.'


def expected_for_document(store, control, connection, actor, inbound_id, now):
    """The expected faxes a received document is linked to, as the exporter may see them (work export)."""
    links, table = store.links, store.expectations
    readable = mailbox_coverage(control, connection, actor, 'work:read', now, store)
    rows = connection.execute(sa.select(table.c.code, table.c.reference, table.c.kind, table.c.state,
                                        table.c.version, table.c.mailbox_id, links.c.state.label('link_state'),
                                        links.c.signal)
                              .select_from(links.join(table, table.c.id == links.c.expectation_id))
                              .where(links.c.inbound_fax_id == inbound_id)
                              .order_by(table.c.code)).mappings().all()
    return [{'code': row['code'], 'reference': row['reference'], 'kind': row['kind'], 'state': row['state'],
             'version': row['version'], 'link': row['link_state'], 'signal': row['signal']}
            for row in rows if row['mailbox_id'] in readable]
