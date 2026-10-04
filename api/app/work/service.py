"""Access-checked work operations for people.

Each operation runs in one access transaction, so the visibility check, the
permission check and the change are one decision. An item is visible when the
actor may ``work:read`` its document's inbound resource; lists, counts,
details, history and exports all apply that before filters and limits. An
invisible item is "not found"; a visible item with a denied action is
"not permitted". Every change needs the version the person last saw.
"""
import json
from uuid import uuid4

import sqlalchemy as sa

from ..access.types import ResourceRef
from ..routing.database import utcnow
from . import text
from .store import MAX_HOURS, WorkChanged, may_back_up_on, may_own_on, people_on


NOT_FOUND = 'This work item was not found.'
CHANGED = 'This item changed; reload and try again.'
FORBIDDEN = 'You do not have permission to do this.'
VIEWS = ('all', 'mine', 'unassigned', 'overdue')
STATES = ('open', 'acknowledged', 'done')


class WorkError(Exception):
    status = 400

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class WorkInputError(WorkError):
    status = 400


class WorkForbidden(WorkError):
    status = 403


class WorkNotFound(WorkError):
    status = 404


class WorkConflict(WorkError):
    status = 409


def _identifier(value):
    return type(value) is str and 0 < len(value) <= 40 and all(32 <= ord(c) < 127 for c in value)


def _version(value):
    if type(value) is not int or value < 1:
        raise WorkInputError('Send the version of the item you are changing.')
    return value


class WorkService:
    def __init__(self, store, access, *, values, clock=None):
        """``access`` carries the AccessStore (``store``) and AccessControl (``control``)."""
        self.store = store
        self.access_store, self.control = access.store, access.control
        self.values = values
        self.clock = clock or utcnow

    # -- shared ----------------------------------------------------------------------
    def _rows(self):
        items, inbound, resources, mailboxes = (self.store.items, self.store.inbound, self.store.resources,
                                                self.store.mailboxes)
        imports = self.store.imports
        test = sa.exists(sa.select(1).where(imports.c.inbound_fax_id == items.c.inbound_fax_id,
                                            imports.c.source == 'test'))
        return (sa.select(*items.c, inbound.c.from_number, inbound.c.to_number, inbound.c.pages, test.label('is_test'),
                          inbound.c.status.label('document_status'), inbound.c.sha256,
                          inbound.c.received_at.label('document_received_at'),
                          mailboxes.c.label.label('mailbox'), resources.c.id.label('resource_id'))
                .select_from(items.join(inbound, inbound.c.id == items.c.inbound_fax_id)
                             .join(resources, sa.and_(resources.c.kind == 'inbound',
                                                      resources.c.inbound_fax_id == items.c.inbound_fax_id))
                             .outerjoin(mailboxes, mailboxes.c.id == items.c.mailbox_id)))

    def _visible(self, connection, actor, permission, now):
        return self.control.visible_resource_ids_on(connection, actor, permission, 'inbound', now=now)

    def _among(self, connection, actor, permission, resource_ids, now):
        if not resource_ids:
            return set()
        visible = self._visible(connection, actor, permission, now).subquery()
        return set(connection.execute(sa.select(visible.c.id).where(visible.c.id.in_(sorted(resource_ids)))).scalars())

    def _require(self, connection, actor, item_id, permission, now):
        """The visible item; raises not found unless the actor may read it, forbidden if the action is denied."""
        self.control._current_source_on(connection, actor, now)
        if not _identifier(item_id):
            raise WorkNotFound(NOT_FOUND)
        row = connection.execute(self._rows().where(self.store.items.c.id == item_id)).mappings().one_or_none()
        if row is None:
            raise WorkNotFound(NOT_FOUND)
        resource = ResourceRef(row['resource_id'])
        if not self.control.authorize_on(connection, actor, 'work:read', resource, now=now).allowed:
            raise WorkNotFound(NOT_FOUND)
        if permission != 'work:read' and not self.control.authorize_on(connection, actor, permission, resource,
                                                                       now=now).allowed:
            raise WorkForbidden(FORBIDDEN)
        return dict(row)

    def _allowed(self, connection, actor, permission, resource_id, now):
        return self.control.authorize_on(connection, actor, permission, ResourceRef(resource_id), now=now).allowed

    def _views(self, connection, actor, rows, now, *, detail=False):
        rows = [dict(row) for row in rows]
        names = self.store.names_on(connection, [row[field] for row in rows for field in
                                                 ('owner_principal_id', 'backup_principal_id', 'acknowledged_by',
                                                  'done_by')])
        resource_ids = {row['resource_id'] for row in rows}
        manage = self._among(connection, actor, 'work:manage', resource_ids, now)
        export = self._among(connection, actor, 'work:export', resource_ids, now)
        document = self._among(connection, actor, 'inbound:document', resource_ids, now)
        duplicates = self._duplicates(connection, actor, rows, now)
        views = []
        for row in rows:
            mine = row['owner_principal_id'] == actor.principal_id
            actions = []
            if row['state'] != 'done' and row['resource_id'] in manage:
                actions.append('assign')
            if row['state'] == 'open' and mine:
                actions.append('acknowledge')
            if row['state'] != 'done' and (mine or row['resource_id'] in manage):
                actions.append('done')
            if row['state'] == 'done' and row['resource_id'] in manage:
                actions.append('reopen')
            if row['resource_id'] in export:
                actions.append('export')
            if row['resource_id'] in document and row['document_status'] != 'waiting':
                actions.append('document')
            person = lambda identity: {'id': identity, 'name': names.get(identity)} if identity else None
            view = {
                'id': row['id'], 'inbound_fax_id': row['inbound_fax_id'], 'state': row['state'],
                'state_key': text.state_key(row, now), 'state_text': text.state_text(row, names, now),
                'due_text': text.due_text(row), 'due_at': row['due_at'], 'due_hours': row['due_hours'],
                'due_source': row['due_source'], 'available_at': row['available_at'],
                'from_number': row['from_number'], 'to_number': row['to_number'], 'pages': row['pages'],
                'mailbox': row['mailbox'], 'owner': person(row['owner_principal_id']),
                'backup': person(row['backup_principal_id']), 'assigned_at': row['assigned_at'],
                'acknowledged_by': names.get(row['acknowledged_by']), 'acknowledged_at': row['acknowledged_at'],
                'escalated_at': row['escalated_at'], 'done_at': row['done_at'], 'done_by': names.get(row['done_by']),
                'done_note': row['done_note'], 'duplicate_of': duplicates.get(row['id']), 'is_mine': mine,
                'overdue': row['state'] == 'open' and row['due_at'] is not None and now > row['due_at'],
                'is_test': bool(row['is_test']),
                'version': row['version'], 'actions': actions,
            }
            if detail:
                owner = row['owner_principal_id']
                view['owner_can_see'] = (None if owner is None else
                                         may_own_on(self.control, connection, owner, row['resource_id']))
            views.append(view)
        return views

    def _duplicates(self, connection, actor, rows, now):
        """For each item, the earliest other visible item with the same document bytes."""
        digests = sorted({row['content_digest'] for row in rows if row['content_digest']})
        if not digests:
            return {}
        items, resources = self.store.items, self.store.resources
        visible = self._visible(connection, actor, 'work:read', now)
        others = connection.execute(
            sa.select(items.c.id, items.c.content_digest, items.c.available_at)
            .select_from(items.join(resources, sa.and_(resources.c.kind == 'inbound',
                                                       resources.c.inbound_fax_id == items.c.inbound_fax_id)))
            .where(items.c.content_digest.in_(digests), resources.c.id.in_(visible))
            .order_by(items.c.available_at, items.c.id)).all()
        result = {}
        for row in rows:
            match = next((other for other in others if other.content_digest == row['content_digest']
                          and other.id != row['id']), None)
            if match is not None:
                result[row['id']] = {'id': match.id, 'available_at': match.available_at}
        return result

    def _names(self, connection, *ids):
        return self.store.names_on(connection, ids)

    # -- reads -----------------------------------------------------------------------
    def list(self, actor, *, view='all', state=None, mailbox=None, limit=100):
        if view not in VIEWS or (state is not None and state not in STATES) or type(limit) is not int \
                or not 1 <= limit <= 200 or (mailbox is not None and (type(mailbox) is not str or len(mailbox) > 100)):
            raise WorkInputError('Choose a valid filter.')
        items, mailboxes = self.store.items, self.store.mailboxes
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            query = self._rows().where(self.store.resources.c.id.in_(self._visible(connection, actor, 'work:read', now)))
            if view == 'mine':
                query = query.where(items.c.owner_principal_id == actor.principal_id, items.c.state != 'done')
            elif view == 'unassigned':
                query = query.where(items.c.owner_principal_id.is_(None), items.c.state == 'open')
            elif view == 'overdue':
                query = query.where(items.c.state == 'open', items.c.due_at.is_not(None), items.c.due_at < now)
            if state is not None:
                query = query.where(items.c.state == state)
            if mailbox:
                found = connection.execute(sa.select(mailboxes.c.id).where(mailboxes.c.label == mailbox)).scalar_one_or_none()
                if found is None:
                    return []
                query = query.where(items.c.mailbox_id == found)
            order = sa.case((items.c.state == 'open', 0), (items.c.state == 'acknowledged', 1), else_=2)
            query = query.order_by(order, items.c.due_at.is_(None), items.c.due_at, items.c.available_at.desc(),
                                   items.c.id).limit(limit)
            return self._views(connection, actor, connection.execute(query).mappings().all(), now)

    def counts(self, actor):
        items = self.store.items
        with self.access_store.transaction() as connection:
            now = self.clock()
            self.control._current_source_on(connection, actor, now)
            visible = self.store.resources.c.id.in_(self._visible(connection, actor, 'work:read', now))
            base = self._rows().with_only_columns(sa.func.count()).where(visible)

            def count(*conditions):
                return connection.execute(base.where(*conditions)).scalar_one()
            return {
                'open': count(items.c.state == 'open'),
                'acknowledged': count(items.c.state == 'acknowledged'),
                'done': count(items.c.state == 'done'),
                'unassigned': count(items.c.state == 'open', items.c.owner_principal_id.is_(None)),
                'mine': count(items.c.state != 'done', items.c.owner_principal_id == actor.principal_id),
                'overdue': count(items.c.state == 'open', items.c.due_at.is_not(None), items.c.due_at < now),
            }

    def _detail_on(self, connection, actor, item_id, now):
        row = self._require(connection, actor, item_id, 'work:read', now)
        return self._views(connection, actor, [row], now, detail=True)[0]

    def detail(self, actor, item_id):
        with self.access_store.transaction() as connection:
            return self._detail_on(connection, actor, item_id, self.clock())

    def history(self, actor, item_id):
        events = self.store.events
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._require(connection, actor, item_id, 'work:read', now)
            rows = connection.execute(sa.select(events).where(events.c.work_item_id == item_id)
                                      .order_by(events.c.occurred_at, events.c.created_at, events.c.id)).mappings().all()
        return [self.event_view(row) for row in rows]

    @staticmethod
    def event_view(row):
        details = json.loads(row['details'] or '{}')
        return {'kind': row['kind'], 'occurred_at': row['occurred_at'], 'actor': details.get('actor_name'),
                'text': text.event_text(row['kind'], details)}

    def assignees(self, actor, item_id):
        with self.access_store.transaction() as connection:
            row = self._require(connection, actor, item_id, 'work:manage', self.clock())
            return people_on(self.control, connection, may_own_on, row['resource_id'])

    # -- changes ---------------------------------------------------------------------
    def _change(self, connection, actor, row, values, *, kind, details, now):
        try:
            self.store.change_on(connection, row, values, kind=kind, actor_id=actor.principal_id,
                                 details={'actor_name': self._names(connection, actor.principal_id)
                                          .get(actor.principal_id), **details}, now=now)
        except WorkChanged:
            raise WorkConflict(CHANGED) from None
        return self._detail_on(connection, actor, row['id'], now)

    @staticmethod
    def _expect(row, version):
        if _version(version) != row['version']:
            raise WorkConflict(CHANGED)

    def assign(self, actor, item_id, principal_id, *, version):
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, 'work:manage', now)
            self._expect(row, version)
            if row['state'] == 'done':
                raise WorkConflict('This item is done; reopen it before assigning it.')
            if not _identifier(principal_id):
                raise WorkInputError('Choose a person to assign.')
            names = self._names(connection, principal_id, row['owner_principal_id'])
            if not may_own_on(self.control, connection, principal_id, row['resource_id']):
                who = names.get(principal_id) or 'That person'
                raise WorkInputError(f'{who} cannot see this document, so it cannot be assigned to them.')
            previous = row['owner_principal_id']
            if previous == principal_id:
                return self._detail_on(connection, actor, item_id, now)
            return self._change(connection, actor, row, {
                'owner_principal_id': principal_id, 'assigned_at': now, 'state': 'open',
                'acknowledged_at': None, 'acknowledged_by': None},
                kind='assigned' if previous is None else 'reassigned',
                details={'from_name': names.get(previous), 'to_name': names.get(principal_id)}, now=now)

    def acknowledge(self, actor, item_id, *, version):
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, 'work:read', now)
            self._expect(row, version)
            if row['owner_principal_id'] != actor.principal_id:
                raise WorkForbidden('Only the owner can acknowledge this item.')
            if row['state'] != 'open':
                raise WorkConflict('This item is already acknowledged.' if row['state'] == 'acknowledged'
                                   else 'This item is already done.')
            return self._change(connection, actor, row, {
                'state': 'acknowledged', 'acknowledged_at': now, 'acknowledged_by': actor.principal_id},
                kind='acknowledged', details={}, now=now)

    def done(self, actor, item_id, *, note, version):
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, 'work:read', now)
            self._expect(row, version)
            if (row['owner_principal_id'] != actor.principal_id
                    and not self._allowed(connection, actor, 'work:manage', row['resource_id'], now)):
                raise WorkForbidden('Only the owner or someone who manages this work can mark it done.')
            note = note.strip() if isinstance(note, str) else ''
            if not 0 < len(note) <= 200 or any(ord(c) < 32 for c in note):
                raise WorkInputError('Add a short note, up to 200 characters, about what was done.')
            if row['state'] == 'done':
                raise WorkConflict('This item is already done.')
            return self._change(connection, actor, row, {
                'state': 'done', 'done_at': now, 'done_by': actor.principal_id, 'done_note': note},
                kind='done', details={'note': note}, now=now)

    def reopen(self, actor, item_id, *, version):
        with self.access_store.transaction() as connection:
            now = self.clock()
            row = self._require(connection, actor, item_id, 'work:manage', now)
            self._expect(row, version)
            if row['state'] != 'done':
                raise WorkConflict('This item is not done.')
            return self._change(connection, actor, row, {
                'state': 'open', 'done_at': None, 'done_by': None, 'done_note': None,
                'acknowledged_at': None, 'acknowledged_by': None}, kind='reopened', details={}, now=now)

    # -- settings --------------------------------------------------------------------
    def _settings_on(self, connection):
        mailboxes, resources, settings = self.store.mailboxes, self.store.resources, self.store.settings
        rows = connection.execute(
            sa.select(mailboxes.c.id, mailboxes.c.label, resources.c.id.label('resource_id'),
                      resources.c.enabled, settings.c.acknowledge_hours, settings.c.backup_principal_id,
                      settings.c.version)
            .select_from(mailboxes.join(resources, sa.and_(resources.c.kind == 'mailbox',
                                                           resources.c.mailbox_id == mailboxes.c.id))
                         .outerjoin(settings, settings.c.mailbox_id == mailboxes.c.id))
            .order_by(mailboxes.c.label, mailboxes.c.id)).all()
        names = self.store.names_on(connection, [row.backup_principal_id for row in rows])
        return {
            'acknowledge_hours': getattr(self.values(), 'work_acknowledge_hours', 0),
            'mailboxes': [{
                'mailbox_id': row.id, 'label': row.label, 'enabled': bool(row.enabled),
                'acknowledge_hours': row.acknowledge_hours,
                'backup': {'id': row.backup_principal_id, 'name': names.get(row.backup_principal_id)}
                if row.backup_principal_id else None,
                'version': row.version or 0,
                'people': people_on(self.control, connection, may_back_up_on, row.resource_id),
            } for row in rows],
        }

    def _require_installation(self, connection, actor, permission, now):
        if not self.control.authorize_on(connection, actor, permission, ResourceRef('installation'), now=now).allowed:
            raise WorkForbidden(FORBIDDEN)

    def settings(self, actor):
        with self.access_store.transaction() as connection:
            self._require_installation(connection, actor, 'settings:read', self.clock())
            return self._settings_on(connection)

    def update_settings(self, actor, entries):
        """Each entry: mailbox_id, acknowledge_hours (None uses the installation target), backup_principal_id, version."""
        if type(entries) is not list or not 1 <= len(entries) <= 200:
            raise WorkInputError('Send at least one mailbox to change.')
        settings = self.store.settings
        with self.access_store.transaction() as connection:
            now = self.clock()
            self._require_installation(connection, actor, 'settings:write', now)
            for entry in entries:
                mailbox_id, hours = entry.get('mailbox_id'), entry.get('acknowledge_hours')
                backup, version = entry.get('backup_principal_id'), entry.get('version') or 0
                resource_id = self.store.mailbox_resource_on(connection, mailbox_id) if _identifier(mailbox_id) else None
                if resource_id is None:
                    raise WorkInputError('That mailbox no longer exists.')
                label = connection.execute(sa.select(self.store.mailboxes.c.label)
                                           .where(self.store.mailboxes.c.id == mailbox_id)).scalar_one()
                if hours is not None and (type(hours) is not int or not 0 <= hours <= MAX_HOURS):
                    raise WorkInputError('Enter a target from 0 to 8760 hours, or leave it empty to use the '
                                         'installation target.')
                if backup is not None and not (_identifier(backup) and may_back_up_on(
                        self.control, connection, backup, resource_id)):
                    who = self._names(connection, backup).get(backup) if _identifier(backup) else None
                    raise WorkInputError(f'{who or "That person"} cannot see every document in {label}, '
                                         'so they cannot be its backup.')
                current = self.store.setting_on(connection, mailbox_id)
                if (current['version'] if current else 0) != version:
                    raise WorkConflict('These settings changed; reload and try again.')
                if current is None:
                    connection.execute(settings.insert().values(
                        id=uuid4().hex, mailbox_id=mailbox_id, acknowledge_hours=hours, backup_principal_id=backup,
                        version=1, created_at=now, updated_at=now))
                else:
                    result = connection.execute(settings.update().where(
                        settings.c.id == current['id'], settings.c.version == current['version']).values(
                        acknowledge_hours=hours, backup_principal_id=backup, version=current['version'] + 1,
                        updated_at=now))
                    if result.rowcount != 1:
                        raise WorkConflict('These settings changed; reload and try again.')
            return self._settings_on(connection)

