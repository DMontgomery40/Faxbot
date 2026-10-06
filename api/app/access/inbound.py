"""Inbound fax resources: provider placement and human visibility.

Provider ingest has no principal. It inserts the inbound row, its resource and a
provider audit row in one access transaction. Placement follows the persisted
rule-to-mailbox binding; anything unrouted enters the unassigned legacy
container. Human reads apply visibility before filters and limits, and an
individual fax is hidden (404) unless the actor may independently read it.
"""
from datetime import datetime, timezone
import hmac
import json
import uuid

import sqlalchemy as sa

from ..routing.numbers import DEFAULT_COUNTRY, is_canonical, stored_number
from .catalog import INBOUND_PERMISSIONS
from .fax_resources import FaxAccessError
from .types import AccessUnavailableError, ResourceRef


def _identity(value):
    return type(value) is str and 0 < len(value) <= 40 and all(32 <= ord(c) < 127 for c in value)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _digits(value):
    return ''.join(c for c in value if '0' <= c <= '9')


def _number(value):
    if value is None:
        return None
    text = str(value).strip()
    return text[:64] or None


class InboundResources:
    def __init__(self, control):
        self.control, self.store, self.tables = control, control.store, control.store.tables

    def _permission(self, permission):
        if type(permission) is not str or permission not in INBOUND_PERMISSIONS:
            raise FaxAccessError('invalid_input')

    def _route_on(self, connection, to_number, country=DEFAULT_COUNTRY):
        """The oldest rule for this number whose bound mailbox is enabled, else None.

        Rules and received numbers match in E.164, reading numbers without a
        country code for the installation country, so a rule saved as
        "01782 684953" in the UK routes "+441782684953". Rows saved before
        numbers were stored in E.164 also match on their digits.
        """
        number = _number(to_number)
        if number is None:
            return None
        canonical = stored_number(number, country=country)
        digits = _digits(number) or number
        rules, routes = self.tables['inbound_rules'], self.tables['access_mailbox_routes']
        resources, mailboxes = self.tables['access_resources'], self.tables['mailboxes']
        rows = connection.execute(sa.select(rules.c.to_number, resources.c.id, mailboxes.c.label)
            .select_from(rules.join(routes, routes.c.id == rules.c.id)
                .join(mailboxes, mailboxes.c.id == routes.c.mailbox_id)
                .join(resources, sa.and_(resources.c.kind == 'mailbox', resources.c.mailbox_id == mailboxes.c.id)))
            .where(resources.c.enabled == 1)
            .order_by(rules.c.created_at, rules.c.id)).all()
        if is_canonical(canonical):
            match = next((row for row in rows
                          if stored_number(row.to_number.strip(), country=country) == canonical), None)
            if match is not None:
                return match
        return next((row for row in rows if (_digits(row.to_number) or row.to_number.strip()) == digits), None)

    def _audit_on(self, connection, operation, target_kind, target_id, details, now):
        version = self.store.require_lock_on(connection)
        connection.execute(self.tables['access_audit'].insert().values(
            id=uuid.uuid4().hex, actor_principal_id=None, actor_key_binding_id=None, actor_session_id=None,
            operation=operation, target_kind=target_kind, target_id=target_id,
            policy_version_before=version, policy_version_after=version, outcome='allowed',
            details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
            created_at=now))

    def record_inbound_on(self, connection, inbound_id, to_number, now, *, country=DEFAULT_COUNTRY):
        """Place a just-inserted inbound row; the caller owns and rolls back the transaction."""
        self.store.require_lock_on(connection)
        if not _identity(inbound_id) or type(now) is not datetime or now.tzinfo is not None:
            raise FaxAccessError('invalid_input')
        resources, faxes = self.tables['access_resources'], self.tables['inbound_faxes']
        if (connection.execute(sa.select(faxes.c.id).where(faxes.c.id == inbound_id)).first() is None
                or connection.execute(sa.select(resources.c.id).where(resources.c.inbound_fax_id == inbound_id)).first() is not None):
            raise FaxAccessError('invalid_target')
        route = self._route_on(connection, to_number, country)
        parent_id, parent_kind = (route.id, 'mailbox') if route is not None else ('legacy', 'legacy')
        if route is not None:
            connection.execute(faxes.update().where(faxes.c.id == inbound_id).values(mailbox_label=route.label))
        identity = uuid.uuid4().hex
        connection.execute(resources.insert().values(id=identity, kind='inbound', parent_id=parent_id,
            parent_kind=parent_kind, principal_id=None, mailbox_id=None, fax_job_id=None,
            inbound_fax_id=inbound_id, enabled=1, version=1, created_at=now, updated_at=now))
        self._audit_on(connection, 'inbound.receive', 'resource', identity,
            {'source': 'provider', 'placement': 'mailbox' if route is not None else 'unassigned'}, now)
        return ResourceRef(identity)

    def insert_on(self, connection, values, now, *, country=DEFAULT_COUNTRY):
        """Insert one inbound row and place it; the caller owns the access transaction.

        Received numbers are stored in E.164 when they can be read for the
        installation country, and as received otherwise; no fax is dropped.
        """
        faxes = self.tables['inbound_faxes']
        if type(values) is not dict or not set(values) <= set(faxes.c.keys()) or not _identity(values.get('id')):
            raise FaxAccessError('invalid_input')
        values = dict(values)
        for field in ('to_number', 'from_number'):
            if isinstance(values.get(field), str) and values[field].strip():
                values[field] = stored_number(values[field].strip(), country=country)
        self.store.require_lock_on(connection)
        connection.execute(faxes.insert().values(**values))
        return self.record_inbound_on(connection, values['id'], values.get('to_number'), now, country=country)

    def accept(self, values, *, now=None, country=DEFAULT_COUNTRY):
        """Insert one provider inbound row with its resource and audit, atomically."""
        with self.store.transaction() as connection:
            return self.insert_on(connection, values, now or _utcnow(), country=country)

    def imports_table(self):
        """The 0010 acquisition records, reflected once; None before that migration."""
        if not hasattr(self, '_imports'):
            try:
                self._imports = sa.Table('inbound_imports', sa.MetaData(), autoload_with=self.store.engine)
            except sa.exc.NoSuchTableError:
                return None
            except sa.exc.SQLAlchemyError:
                raise AccessUnavailableError() from None
        return self._imports

    def backfill_on(self, connection, now):
        """Place every resource-less inbound row under legacy; repeated runs change nothing."""
        self.store.require_lock_on(connection)
        resources, faxes = self.tables['access_resources'], self.tables['inbound_faxes']
        missing = connection.execute(sa.select(faxes.c.id).where(~sa.exists(
            sa.select(1).where(resources.c.inbound_fax_id == faxes.c.id))).order_by(faxes.c.id)).scalars().all()
        for inbound_id in missing:
            connection.execute(resources.insert().values(id=uuid.uuid4().hex, kind='inbound',
                parent_id='legacy', parent_kind='legacy', principal_id=None, mailbox_id=None,
                fax_job_id=None, inbound_fax_id=inbound_id, enabled=1, version=1, created_at=now, updated_at=now))
        if missing:
            self._audit_on(connection, 'inbound.backfill', 'resource', 'legacy',
                {'source': 'startup', 'placed': len(missing)}, now)
        return len(missing)

    def backfill(self, *, now=None):
        with self.store.transaction() as connection:
            return self.backfill_on(connection, now or _utcnow())

    def visible_inbound_ids_on(self, connection, actor, permission, *, now):
        self.store.require_lock_on(connection)
        self._permission(permission)
        visible = self.control.visible_resource_ids_on(connection, actor, permission, 'inbound', now=now)
        resources = self.tables['access_resources']
        return sa.select(resources.c.inbound_fax_id).where(resources.c.id.in_(visible),
            resources.c.kind == 'inbound', resources.c.inbound_fax_id.is_not(None))

    def require_inbound_on(self, connection, actor, inbound_id, permission, *, now):
        """Permit the exact action; a denied action reveals existence only with inbound:read."""
        source = self.control._current_source_on(connection, actor, now)
        self._permission(permission)
        if source.reset_required:
            raise FaxAccessError('reset_required')
        if not _identity(inbound_id):
            raise FaxAccessError('not_found')
        resources = self.tables['access_resources']
        identity = connection.execute(sa.select(resources.c.id).where(
            resources.c.kind == 'inbound', resources.c.inbound_fax_id == inbound_id)).scalar_one_or_none()
        if identity is None:
            raise FaxAccessError('not_found')
        resource = ResourceRef(identity)
        if self.control.authorize_on(connection, actor, permission, resource, now=now).allowed:
            return resource
        visible = self.control.authorize_on(connection, actor, 'inbound:read', resource, now=now).allowed
        raise FaxAccessError('forbidden' if visible else 'not_found')


class AuthorizedInboundQueries:
    """Inbound projections in the shape of the existing /inbound API (InboundFaxOut)."""

    def __init__(self, resources, *, clock=None):
        self.resources, self.store, self.tables = resources, resources.store, resources.tables
        self._clock = clock or _utcnow

    def _selection(self):
        faxes, resources, mailboxes = self.tables['inbound_faxes'], self.tables['access_resources'], self.tables['mailboxes']
        parent = resources.alias('mailbox_resource')
        query = sa.select(faxes.c.id, faxes.c.from_number.label('fr'), faxes.c.to_number.label('to'),
            faxes.c.status, faxes.c.backend, faxes.c.pages, faxes.c.size_bytes, faxes.c.created_at,
            faxes.c.received_at, faxes.c.updated_at, faxes.c.sha256, faxes.c.provider_sid, faxes.c.pdf_path,
            sa.func.coalesce(mailboxes.c.label, faxes.c.mailbox_label).label('mailbox')).select_from(
                faxes.outerjoin(resources, sa.and_(resources.c.inbound_fax_id == faxes.c.id, resources.c.kind == 'inbound'))
                .outerjoin(parent, sa.and_(parent.c.id == resources.c.parent_id, parent.c.kind == 'mailbox'))
                .outerjoin(mailboxes, mailboxes.c.id == parent.c.mailbox_id))
        return query, parent

    def page(self, actor, *, to_number=None, status=None, mailbox=None, limit=100, country=None):
        if (type(limit) is not int or not 1 <= limit <= 100
                or any(value is not None and (type(value) is not str or len(value) > 100)
                       for value in (to_number, status, mailbox))):
            raise FaxAccessError('invalid_input')
        faxes, mailboxes = self.tables['inbound_faxes'], self.tables['mailboxes']
        with self.store.transaction() as connection:
            now = self._clock()
            source = self.resources.control._current_source_on(connection, actor, now)
            if source.reset_required:
                raise FaxAccessError('reset_required')
            visible = self.resources.visible_inbound_ids_on(connection, actor, 'inbound:list', now=now)
            query, parent = self._selection()
            query = query.where(faxes.c.id.in_(visible))
            if to_number:
                # Find a number however it is typed; older rows keep the text received.
                wanted = {to_number}
                if country is not None:
                    wanted.add(stored_number(to_number.strip(), country=country))
                query = query.where(faxes.c.to_number.in_(sorted(wanted)))
            if status:
                query = query.where(faxes.c.status == status)
            if mailbox:
                mailbox_id = connection.execute(sa.select(mailboxes.c.id).where(mailboxes.c.label == mailbox)).scalar_one_or_none()
                if mailbox_id is None:
                    return []
                query = query.where(parent.c.mailbox_id == mailbox_id)
            rows = connection.execute(query.order_by(faxes.c.received_at.desc(), faxes.c.id.desc()).limit(limit)).mappings().all()
            return self._with_acquisition(connection, rows)

    def item(self, actor, inbound_id):
        with self.store.transaction() as connection:
            self.resources.require_inbound_on(connection, actor, inbound_id, 'inbound:read', now=self._clock())
            query, _ = self._selection()
            row = connection.execute(query.where(self.tables['inbound_faxes'].c.id == inbound_id)).mappings().one_or_none()
            if row is None:
                raise FaxAccessError('not_found')
            return self._with_acquisition(connection, [row])[0]

    def _with_acquisition(self, connection, rows):
        """Add each fax's acquisition state, read in one query; never duplicates a fax."""
        from ..inbound.acquisition import describe
        imports = self.resources.imports_table()
        found = {}
        identities = [row['id'] for row in rows]
        if imports is not None and identities:
            for record in connection.execute(sa.select(imports).where(imports.c.inbound_fax_id.in_(identities))
                                             .order_by(imports.c.created_at, imports.c.id)).mappings():
                found.setdefault(record['inbound_fax_id'], dict(record))
        now = self._clock()
        result = []
        for row in rows:
            row = dict(row)
            record = found.get(row['id'])
            row.update(describe(row, record, now=now))
            for private in ('pdf_path', 'provider_sid'):
                row.pop(private, None)
            result.append(row)
        return result

    def document(self, actor, inbound_id):
        faxes = self.tables['inbound_faxes']
        with self.store.transaction() as connection:
            self.resources.require_inbound_on(connection, actor, inbound_id, 'inbound:document', now=self._clock())
            row = connection.execute(sa.select(faxes.c.pdf_path, faxes.c.status, faxes.c.sha256).where(
                faxes.c.id == inbound_id)).first()
            return {'id': inbound_id, 'pdf_path': row.pdf_path if row else None,
                    'status': row.status if row else None, 'sha256': row.sha256 if row else None}

    def require_fetchable(self, actor, inbound_id):
        """A person may ask to fetch again only a fax they can read."""
        with self.store.transaction() as connection:
            self.resources.require_inbound_on(connection, actor, inbound_id, 'inbound:read', now=self._clock())

    def shared_document(self, inbound_id, token):
        """The per-fax download link token issued at ingest; it expires and is never a session."""
        if not _identity(inbound_id) or type(token) is not str or not 0 < len(token) <= 256:
            raise FaxAccessError('forbidden')
        faxes = self.tables['inbound_faxes']
        try:
            with self.store.engine.connect() as connection:
                row = connection.execute(sa.select(faxes.c.pdf_path, faxes.c.pdf_token, faxes.c.pdf_token_expires_at,
                                                   faxes.c.status, faxes.c.sha256)
                    .where(faxes.c.id == inbound_id)).first()
        except sa.exc.SQLAlchemyError:
            raise AccessUnavailableError() from None
        if row is None:
            raise FaxAccessError('not_found')
        stored = row.pdf_token
        if (type(stored) is not str or not stored
                or not hmac.compare_digest(token.encode('utf-8', 'replace'), stored.encode('utf-8', 'replace'))):
            raise FaxAccessError('forbidden')
        if row.pdf_token_expires_at is None or self._clock() > row.pdf_token_expires_at:
            raise FaxAccessError('forbidden')
        return {'id': inbound_id, 'pdf_path': row.pdf_path, 'status': row.status, 'sha256': row.sha256}
