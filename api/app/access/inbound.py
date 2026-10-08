"""Inbound fax resources: provider placement and human visibility.

Provider ingest has no principal. It inserts the inbound row, its resource and a
provider audit row in one access transaction. Placement follows the persisted
rule-to-mailbox binding; anything unrouted enters the unassigned legacy
container. Human reads apply visibility before filters and limits, and an
individual fax is hidden (404) unless the actor may independently read it.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
import hmac
import json
import uuid

import sqlalchemy as sa

from ..routing.numbers import DEFAULT_COUNTRY, stored_number
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

    def receiving_tables(self):
        """The receiving-rule tables (0030), or None before that migration."""
        from .receiving_rules import tables
        return tables(self.store.engine)

    def _rule_on(self, connection, facts, country=DEFAULT_COUNTRY):
        """The number rule that places a fax with these facts, or None (``receiving_rules.choose``).

        With no rule options this is exactly the old match: the oldest rule for the number whose mailbox is
        enabled, in E.164 (a rule saved as "01782 684953" in the UK routes "+441782684953"), then on digits for
        rows saved before numbers were stored in E.164.
        """
        from .receiving_rules import choose, ordered_rules
        return choose(ordered_rules(connection, self.tables, self.receiving_tables()), facts, country)

    def _route_on(self, connection, to_number, country=DEFAULT_COUNTRY):
        """The place a fax to this number is filed in, knowing only the number: (resource id, label) or None."""
        from .receiving_rules import ReceivedFacts
        rule = self._rule_on(connection, ReceivedFacts(to_number=to_number), country)
        if rule is None:
            return None
        return SimpleNamespace(id=rule['resource_id'], label=rule['mailbox_label'], to_number=rule['to_number'])

    def _mailbox_on(self, connection, mailbox_id, actor, now):
        """A mailbox an import names directly: (resource id, label).

        A person importing (``actor``) must be able to read faxes in it. Without an actor the caller is Faxbot
        itself, such as an intake connector an administrator set up for that mailbox.
        """
        resources, mailboxes = self.tables['access_resources'], self.tables['mailboxes']
        row = connection.execute(sa.select(resources.c.id, resources.c.enabled, mailboxes.c.label)
            .select_from(mailboxes.join(resources, sa.and_(resources.c.kind == 'mailbox',
                                                           resources.c.mailbox_id == mailboxes.c.id)))
            .where(mailboxes.c.id == mailbox_id)).first() if _identity(mailbox_id) else None
        if row is None or row.enabled != 1:
            raise FaxAccessError('invalid_target')
        if actor is not None and not self.control.authorize_child_on(connection, actor, 'inbound:read',
                                                                     ResourceRef(row.id), now=now):
            raise FaxAccessError('forbidden')
        return SimpleNamespace(id=row.id, label=row.label)

    def _audit_on(self, connection, operation, target_kind, target_id, details, now):
        version = self.store.require_lock_on(connection)
        connection.execute(self.tables['access_audit'].insert().values(
            id=uuid.uuid4().hex, actor_principal_id=None, actor_key_binding_id=None, actor_session_id=None,
            operation=operation, target_kind=target_kind, target_id=target_id,
            policy_version_before=version, policy_version_after=version, outcome='allowed',
            details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
            created_at=now))

    def record_inbound_on(self, connection, inbound_id, to_number, now, *, country=DEFAULT_COUNTRY, facts=None,
                          mailbox_id=None, actor=None):
        """Place a just-inserted inbound row; the caller owns and rolls back the transaction.

        ``facts`` (``receiving_rules.ReceivedFacts``) are what the receiving rules read; without them only the
        number is known. ``mailbox_id`` files the fax straight into that mailbox, for an import that names one
        (a document with no fax number); ``actor``, the importer, must be able to read that mailbox. How the fax
        was placed is recorded once (``inbound_fax_routing``). A rule chooses where a fax is filed; it never
        grants anyone access.
        """
        from .receiving_rules import ReceivedFacts, record_on
        self.store.require_lock_on(connection)
        if not _identity(inbound_id) or type(now) is not datetime or now.tzinfo is not None:
            raise FaxAccessError('invalid_input')
        resources, faxes = self.tables['access_resources'], self.tables['inbound_faxes']
        if (connection.execute(sa.select(faxes.c.id).where(faxes.c.id == inbound_id)).first() is None
                or connection.execute(sa.select(resources.c.id).where(resources.c.inbound_fax_id == inbound_id)).first() is not None):
            raise FaxAccessError('invalid_target')
        facts = facts or ReceivedFacts(to_number=to_number, received_at=now)
        rule = None
        if mailbox_id is not None:
            route = self._mailbox_on(connection, mailbox_id, actor, now)
        else:
            rule = self._rule_on(connection, facts, country)
            route = None if rule is None else SimpleNamespace(id=rule['resource_id'], label=rule['mailbox_label'])
        parent_id, parent_kind = (route.id, 'mailbox') if route is not None else ('legacy', 'legacy')
        if route is not None:
            connection.execute(faxes.update().where(faxes.c.id == inbound_id).values(mailbox_label=route.label))
        identity = uuid.uuid4().hex
        connection.execute(resources.insert().values(id=identity, kind='inbound', parent_id=parent_id,
            parent_kind=parent_kind, principal_id=None, mailbox_id=None, fax_job_id=None,
            inbound_fax_id=inbound_id, enabled=1, version=1, created_at=now, updated_at=now))
        details = {'source': 'provider', 'placement': 'mailbox' if route is not None else 'unassigned'}
        if rule is not None and rule['options']:
            details['rule'] = rule['id']
        self._audit_on(connection, 'inbound.receive', 'resource', identity, details, now)
        record_on(connection, self.receiving_tables(), inbound_id, rule, facts, now,
                  mailbox_id=mailbox_id)
        return ResourceRef(identity)

    def insert_on(self, connection, values, now, *, country=DEFAULT_COUNTRY, facts=None, mailbox_id=None,
                  actor=None):
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
        from dataclasses import replace
        from .receiving_rules import ReceivedFacts
        if facts is None:
            from ..people_time import installation_zone_name
            facts = ReceivedFacts(to_number=None, received_at=now, time_zone=installation_zone_name())
        facts = replace(facts, to_number=values.get('to_number'), from_number=values.get('from_number'))
        return self.record_inbound_on(connection, values['id'], values.get('to_number'), now, country=country,
                                      facts=facts, mailbox_id=mailbox_id, actor=actor)

    def accept(self, values, *, now=None, country=DEFAULT_COUNTRY, facts=None, mailbox_id=None, actor=None):
        """Insert one provider inbound row with its resource and audit, atomically."""
        with self.store.transaction() as connection:
            return self.insert_on(connection, values, now or _utcnow(), country=country, facts=facts,
                                  mailbox_id=mailbox_id, actor=actor)

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

    def received_access_on(self, connection, actor, *, inbound_id=None, to_number=None, country=DEFAULT_COUNTRY,
                           now):
        """(may read, may open the document) for one received fax.

        For a document a partner delivered that is not filed as a received fax
        (yet), the answer is the one for the place a fax to ``to_number`` is
        filed in: its mailbox, or the unassigned received faxes.
        """
        permissions = ('inbound:read', 'inbound:document')
        if inbound_id is not None:
            resources = self.tables['access_resources']
            identity = connection.execute(sa.select(resources.c.id).where(
                resources.c.kind == 'inbound', resources.c.inbound_fax_id == inbound_id)).scalar_one_or_none()
            if identity is None:
                return False, False
            read, document = (self.control.authorize_on(connection, actor, permission, ResourceRef(identity),
                                                        now=now).allowed for permission in permissions)
            return read, document
        route = self._route_on(connection, to_number, country)
        place = ResourceRef(route.id if route is not None else 'legacy')
        read, document = (self.control.authorize_child_on(connection, actor, permission, place, now=now)
                          for permission in permissions)
        return read, document

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


def _received_on(record, placed):
    """The account a fax arrived on, by key and name, the subaddress its sender stated and whether a receiving
    rule marked it urgent. Faxes from before accounts existed name no account."""
    key = (record or {}).get('account_key') or (placed or {}).get('account_key')
    label = None
    if key:
        try:
            from ..accounts import account_named
            from ..config import configuration_values
            account = account_named(configuration_values(), key)
            label = account.label if account is not None else None
        except Exception:
            label = None
    return {'account_key': key, 'account_label': label, 'subaddress': (placed or {}).get('subaddress'),
            'urgent': bool((placed or {}).get('urgent'))}


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
        from ..inbound.acquisition import describe, failures_on, provider_copies_on
        imports = self.resources.imports_table()
        found = {}
        identities = [row['id'] for row in rows]
        if imports is not None and identities:
            for record in connection.execute(sa.select(imports).where(imports.c.inbound_fax_id.in_(identities))
                                             .order_by(imports.c.created_at, imports.c.id)).mappings():
                found.setdefault(record['inbound_fax_id'], dict(record))
        failures = failures_on(connection, self.store.engine, identities) if imports is not None else {}
        copies = provider_copies_on(connection, self.store.engine, identities) if imports is not None else {}
        placements = self._placements_on(connection, identities)
        now = self._clock()
        result = []
        for row in rows:
            row = dict(row)
            record = found.get(row['id'])
            row.update(describe(row, record, now=now, failures=failures.get(row['id'], ()),
                                provider_copy=copies.get(row['id'])))
            row.update(_received_on(record, placements.get(row['id'])))
            for private in ('pdf_path', 'provider_sid'):
                row.pop(private, None)
            result.append(row)
        return result

    def _placements_on(self, connection, identities):
        """{fax id: how the receiving rules placed it} in one query."""
        receiving = self.resources.receiving_tables()
        if receiving is None or not identities:
            return {}
        routing = receiving['routing']
        return {row['id']: dict(row) for row in connection.execute(
            sa.select(routing).where(routing.c.id.in_(identities))).mappings()}

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
