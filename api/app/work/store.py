"""Work item records, the feeder, escalation and who may own a document.

Rules this module keeps:

- One work item per received document. A document is received when its inbound
  row's status is ``received``; rows written before that vocabulary count as
  received when their document is stored. Waiting or failed rows have no item.
- The clock starts when the document became available: the earliest
  ``inbound_imports.acquired_at`` for the document when Faxbot acquired it
  through an import record, else ``inbound_faxes.received_at``.
- ``due_at`` is computed once, when the item is created, from the mailbox's
  target, else the installation target. Changing a target later does not move
  existing deadlines; restarts, replayed notifications and duplicates never
  reset it.
- An open item past ``due_at`` is escalated once: one ``escalated`` event with
  a dedupe key, and the backup person becomes the owner when they can see the
  document. Owner history stays in events.
- Every change is a compare-and-set on ``version`` together with its event.
- Assignment never confers access: an owner or backup must already hold
  ``work:read`` and ``inbound:read`` on the document.
- Every document a partner delivers directly is filed as a received fax
  (``direct/filing.py``), so it gets an item like any fax; its ``received``
  event says it came directly, with no telephone call.
"""
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import sqlalchemy as sa

from ..access.policy import _Source
from ..routing.database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction


TABLES = ('work_items', 'work_events', 'work_mailbox_settings', 'inbound_faxes', 'inbound_imports',
          'access_resources', 'mailboxes', 'access_principals', 'access_users', 'intake_items',
          'intake_connectors')
# What a person needs on a document before it can be theirs.
OWNER_PERMISSIONS = ('work:read', 'inbound:read')
MAX_HOURS = 8760
MAX_DETAILS = 4000


class WorkChanged(RuntimeError):
    """The item changed between reading and writing; nothing was written."""


def details_json(details):
    """Bounded JSON of plain values for one event; never secrets or document content."""
    text = json.dumps({key: value for key, value in details.items() if value not in (None, '')},
                      ensure_ascii=True, separators=(',', ':'), sort_keys=True, default=str)
    if len(text) > MAX_DETAILS:
        raise ValueError('event details are too large')
    return text


def _target(control, principal_id):
    """A policy subject for another enabled user, with no credential or key ceiling."""
    principals, users = control.tables['access_principals'], control.tables['access_users']
    predicate = sa.exists(sa.select(1).select_from(principals.join(users, users.c.id == principals.c.id)).where(
        principals.c.id == principal_id, principals.c.kind == 'user', principals.c.enabled == 1))
    return SimpleNamespace(principal_id=principal_id), _Source(predicate, None, False, False)


def may_own_on(control, connection, principal_id, resource_id):
    """Whether this user can see this received document now (work:read and inbound:read)."""
    if not principal_id or not resource_id:
        return False
    context, source = _target(control, principal_id)
    return all(connection.execute(control._allowed_query(context, source, permission, resource_id=resource_id))
               .first() is not None for permission in OWNER_PERMISSIONS)


def may_back_up_on(control, connection, principal_id, mailbox_resource_id):
    """Whether this user can see every document in the mailbox (both permissions at it or above)."""
    if not principal_id or not mailbox_resource_id:
        return False
    context, source = _target(control, principal_id)
    return all(connection.execute(control._coverage_query(context, source, permission, mailbox_resource_id))
               .first() is not None for permission in OWNER_PERMISSIONS)


def people_on(control, connection, check, resource_id, *, limit=500):
    """Enabled users for whom ``check`` passes, by name."""
    principals, users = control.tables['access_principals'], control.tables['access_users']
    rows = connection.execute(sa.select(principals.c.id, principals.c.display_name, users.c.login)
                              .select_from(principals.join(users, users.c.id == principals.c.id))
                              .where(principals.c.kind == 'user', principals.c.enabled == 1)
                              .order_by(principals.c.display_name, principals.c.id).limit(limit)).all()
    return [{'id': row.id, 'name': row.display_name, 'login': row.login} for row in rows
            if check(control, connection, row.id, resource_id)]


class WorkStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, TABLES)
        self.items, self.events = tables['work_items'], tables['work_events']
        self.settings = tables['work_mailbox_settings']
        self.inbound, self.imports = tables['inbound_faxes'], tables['inbound_imports']
        self.resources, self.mailboxes = tables['access_resources'], tables['mailboxes']
        self.principals, self.users = tables['access_principals'], tables['access_users']
        self.intake, self.connectors = tables['intake_items'], tables['intake_connectors']
        # How each received fax was placed (0030): a receiving rule may mark its item urgent. None before then.
        from ..access.receiving_rules import tables as receiving_tables
        receiving = receiving_tables(engine)
        self.routing = receiving['routing'] if receiving is not None else None

    # -- reading ---------------------------------------------------------------------
    def received(self):
        status = sa.func.lower(self.inbound.c.status)
        stored = sa.and_(self.inbound.c.pdf_path.is_not(None), self.inbound.c.pdf_path != '')
        return sa.or_(status == 'received', sa.and_(status.not_in(['waiting', 'failed']), stored))

    def available_at(self):
        acquired = (sa.select(sa.func.min(self.imports.c.acquired_at))
                    .where(self.imports.c.inbound_fax_id == self.inbound.c.id).scalar_subquery())
        return sa.func.coalesce(acquired, self.inbound.c.received_at)

    def placement_on(self, connection, inbound_id):
        """The document's access resource and, when it is in one, its mailbox."""
        resource, parent = self.resources, self.resources.alias('parent')
        row = connection.execute(
            sa.select(resource.c.id, parent.c.mailbox_id, self.mailboxes.c.label)
            .select_from(resource.outerjoin(parent, sa.and_(parent.c.id == resource.c.parent_id,
                                                            parent.c.kind == 'mailbox'))
                         .outerjoin(self.mailboxes, self.mailboxes.c.id == parent.c.mailbox_id))
            .where(resource.c.kind == 'inbound', resource.c.inbound_fax_id == inbound_id)).first()
        if row is None:
            return SimpleNamespace(resource_id=None, mailbox_id=None, label=None)
        return SimpleNamespace(resource_id=row.id, mailbox_id=row.mailbox_id, label=row.label)

    def mailbox_resource_on(self, connection, mailbox_id):
        return connection.execute(sa.select(self.resources.c.id).where(
            self.resources.c.kind == 'mailbox', self.resources.c.mailbox_id == mailbox_id)).scalar_one_or_none()

    def setting_on(self, connection, mailbox_id):
        if mailbox_id is None:
            return None
        row = connection.execute(sa.select(self.settings).where(self.settings.c.mailbox_id == mailbox_id)
                                 ).mappings().one_or_none()
        return dict(row) if row is not None else None

    def item_on(self, connection, item_id):
        row = connection.execute(sa.select(self.items).where(self.items.c.id == item_id)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def names_on(self, connection, ids):
        ids = sorted({value for value in ids if value})
        if not ids:
            return {}
        return dict(connection.execute(sa.select(self.principals.c.id, self.principals.c.display_name)
                                       .where(self.principals.c.id.in_(ids))).all())

    def arrived_on(self, connection, inbound_id):
        """How a document a partner delivered directly arrived, in one sentence; None for any other document."""
        record = connection.execute(sa.select(self.imports.c.report).where(
            self.imports.c.inbound_fax_id == inbound_id, self.imports.c.source == 'local',
            self.imports.c.account.like('direct:%'))).mappings().first()
        if record is None:
            return None
        from ..direct.filing import received_text
        return received_text(dict(record))

    @staticmethod
    def target(setting, installation_hours):
        """(hours, source) for a new item: the mailbox target, else the installation target."""
        if setting is not None and setting['acknowledge_hours'] is not None:
            hours = setting['acknowledge_hours']
            return (hours, 'mailbox') if hours > 0 else (None, None)
        if installation_hours and installation_hours > 0:
            return min(int(installation_hours), MAX_HOURS), 'installation'
        return None, None

    # -- writing ---------------------------------------------------------------------
    def change_on(self, connection, item, values, *, kind, actor_id, details, now, dedupe_key=None):
        """Compare-and-set one item and append its event; raise WorkChanged when it moved."""
        result = connection.execute(self.items.update().where(
            self.items.c.id == item['id'], self.items.c.version == item['version']).values(
            version=item['version'] + 1, updated_at=now, **values))
        if result.rowcount != 1:
            raise WorkChanged()
        self.event_on(connection, item['id'], kind, actor_id=actor_id, details=details, now=now,
                      dedupe_key=dedupe_key)

    def event_on(self, connection, item_id, kind, *, actor_id, details, now, occurred_at=None, dedupe_key=None):
        connection.execute(self.events.insert().values(
            id=uuid4().hex, work_item_id=item_id, kind=kind, actor_principal_id=actor_id,
            occurred_at=occurred_at or now, details=details_json(details), dedupe_key=dedupe_key, created_at=now))

    def feed(self, *, installation_hours=0, now=None, limit=100):
        """Create one open item per received document that has none; return how many."""
        now = now or utcnow()
        inbound, items = self.inbound, self.items
        query = (sa.select(inbound.c.id, inbound.c.sha256, self.available_at().label('available_at'))
                 .select_from(inbound.outerjoin(items, items.c.inbound_fax_id == inbound.c.id))
                 .where(items.c.id.is_(None), self.received())
                 .order_by(inbound.c.received_at, inbound.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            rows = connection.execute(query).all()
        created = 0
        for row in rows:
            try:
                with write_transaction(self.engine) as connection:
                    if connection.execute(sa.select(items.c.id).where(items.c.inbound_fax_id == row.id)).first():
                        continue
                    place = self.placement_on(connection, row.id)
                    hours, source = self.target(self.setting_on(connection, place.mailbox_id), installation_hours)
                    identity = uuid4().hex
                    connection.execute(items.insert().values(
                        id=identity, inbound_fax_id=row.id, mailbox_id=place.mailbox_id, state='open',
                        available_at=row.available_at, due_at=row.available_at + timedelta(hours=hours) if hours else None,
                        due_hours=hours, due_source=source, content_digest=(row.sha256 or None),
                        version=1, created_at=now, updated_at=now))
                    self.event_on(connection, identity, 'received', actor_id=None, now=now,
                                  occurred_at=row.available_at,
                                  details={'mailbox': place.label, 'due_hours': hours, 'due_source': source,
                                           'arrived': self.arrived_on(connection, row.id)})
                    created += 1
            except DeliveryStoreError:
                continue  # A concurrent feeder created it; the unique index decides.
        return created

    def escalate(self, control, *, now=None, limit=100):
        """Escalate open items past their target once each; return how many changed."""
        now = now or utcnow()
        items = self.items
        due = (sa.select(items.c.id).where(items.c.state == 'open', items.c.due_at.is_not(None),
                                           items.c.due_at <= now, items.c.escalated_at.is_(None))
               .order_by(items.c.due_at, items.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            identities = connection.execute(due).scalars().all()
        changed = 0
        for identity in identities:
            try:
                with write_transaction(self.engine) as connection:
                    item = self.item_on(connection, identity)
                    if (item is None or item['state'] != 'open' or item['escalated_at'] is not None
                            or item['due_at'] is None or item['due_at'] > now):
                        continue
                    setting = self.setting_on(connection, item['mailbox_id'])
                    backup = item['backup_principal_id'] or (setting or {}).get('backup_principal_id')
                    owner = item['owner_principal_id']
                    place = self.placement_on(connection, item['inbound_fax_id'])
                    names = self.names_on(connection, (owner, backup))
                    values = {'escalated_at': now, 'backup_principal_id': backup}
                    details = {'from_name': names.get(owner), 'due_at': item['due_at'].isoformat(timespec='seconds')}
                    if backup and backup == owner:
                        details['to_name'] = names.get(backup)
                    elif backup and may_own_on(control, connection, backup, place.resource_id):
                        values.update(owner_principal_id=backup, assigned_at=now)
                        details['to_name'] = names.get(backup)
                    elif backup:
                        details['backup_name'] = names.get(backup)
                    self.change_on(connection, item, values, kind='escalated', actor_id=None, details=details,
                                   now=now, dedupe_key='escalated:' + item['due_at'].isoformat(timespec='seconds'))
                    changed += 1
            except (WorkChanged, DeliveryStoreError):
                continue  # A person changed it first, or another worker escalated it.
        return changed
