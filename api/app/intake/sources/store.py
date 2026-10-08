"""Connector records: settings, sealed secrets, the check lease, items and replies (tables from 0041).

One item per source identity. ``record`` inserts it once; a later copy of the
same identity only counts as a duplicate (``seen_again``). An email-to-fax
item is written inside the fax's own acceptance transaction (``link_on``), so
the fax and the record that it was sent commit together or not at all.
"""
from dataclasses import dataclass
from datetime import timedelta
import json
import secrets as token_source
from uuid import uuid4

import sqlalchemy as sa

from ...routing.database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction
from . import text
from .settings import SourceInputError


TABLES = ('intake_sources', 'intake_source_senders', 'intake_source_items', 'access_principals',
          'access_users', 'mailboxes', 'inbound_rules', 'access_mailbox_routes', 'fax_jobs',
          'outbound_deliveries')
LEASE = timedelta(minutes=5)
REPLY_RETRY_MINUTES = (1, 5, 15, 60, 240)


class SourceConflict(RuntimeError):
    """One plain sentence (409)."""


class SourceSecrets:
    """Seal connector secrets with the installation configuration key, bound to the connector."""

    KIND = 'intake_source'

    def __init__(self, configuration):
        self.configuration = configuration

    def _context(self):
        with self.configuration.engine.connect() as connection:
            head = self.configuration._head(connection)
        if head is None:
            raise RuntimeError('Installation configuration is not ready.')
        return self.configuration._cipher(), head['installation_id']

    def seal(self, payload, record_id):
        cipher, installation = self._context()
        return cipher.seal(dict(payload), installation_id=installation, kind=self.KIND, record_id=record_id)

    def open(self, envelope, record_id):
        cipher, installation = self._context()
        return dict(cipher.open(envelope, installation_id=installation, kind=self.KIND, record_id=record_id))


@dataclass(frozen=True)
class Source:
    id: str
    kind: str
    direction: str
    name: str
    enabled: bool
    settings: dict
    has_secret: bool
    key_id: str | None
    key_binding_id: str | None
    key_principal_id: str | None
    paused_reason: str | None
    last_checked_at: object
    last_ok: bool | None
    last_result: str | None
    next_check_at: object
    removed: bool
    version: int
    created_at: object


def normalized(name):
    return ' '.join(name.split()).casefold()


class SourceStore:
    def __init__(self, engine, secrets, *, clock=utcnow):
        self.engine, self.secrets, self.clock = engine, secrets, clock
        tables = reflect(engine, TABLES)
        self.sources, self.senders = tables['intake_sources'], tables['intake_source_senders']
        self.items, self.principals = tables['intake_source_items'], tables['access_principals']
        self.users, self.mailboxes = tables['access_users'], tables['mailboxes']
        self.rules, self.routes = tables['inbound_rules'], tables['access_mailbox_routes']
        self.jobs, self.deliveries = tables['fax_jobs'], tables['outbound_deliveries']

    # -- connectors -------------------------------------------------------------------
    @staticmethod
    def _source(row):
        return Source(row['id'], row['kind'], row['direction'], row['name'], bool(row['enabled']),
                      json.loads(row['settings']), row['secret_envelope'] is not None, row['key_id'],
                      row['key_binding_id'], row['key_principal_id'], row['paused_reason'], row['last_checked_at'],
                      None if row['last_ok'] is None else bool(row['last_ok']), row['last_result'],
                      row['next_check_at'], row['removed_at'] is not None, row['version'], row['created_at'])

    def list(self, *, include_removed=False):
        query = sa.select(self.sources).order_by(self.sources.c.created_at, self.sources.c.id)
        if not include_removed:
            query = query.where(self.sources.c.removed_at.is_(None))
        with read_connection(self.engine) as connection:
            return [self._source(row) for row in connection.execute(query).mappings()]

    def get(self, source_id, connection=None):
        query = sa.select(self.sources).where(self.sources.c.id == source_id)
        if connection is not None:
            row = connection.execute(query).mappings().first()
        else:
            with read_connection(self.engine) as conn:
                row = conn.execute(query).mappings().first()
        return self._source(row) if row is not None else None

    def find(self, name_or_id):
        matches = [source for source in self.list() if source.id == name_or_id
                   or normalized(source.name) == normalized(name_or_id or '')]
        return matches[0] if len(matches) == 1 else None

    def secret(self, source):
        if not source.has_secret:
            return {}
        with read_connection(self.engine) as connection:
            envelope = connection.scalar(sa.select(self.sources.c.secret_envelope).where(
                self.sources.c.id == source.id))
        return self.secrets.open(envelope, source.id) if envelope else {}

    @staticmethod
    def _name(name):
        name = ' '.join(name.split()) if isinstance(name, str) else ''
        if not name or len(name) > 100:
            raise SourceInputError('Give the connector a name of up to 100 characters.')
        return name

    def create(self, *, kind, direction, name, settings, secret=None, senders=(), key=None, source_id=None):
        """``key`` is (public id, binding id, principal id) of the connector's own sending key."""
        source_id, now = source_id or uuid4().hex, self.clock()
        name = self._name(name)
        envelope = self.secrets.seal(secret, source_id) if secret else None
        key = key or (None, None, None)
        with write_transaction(self.engine) as connection:
            if connection.execute(sa.select(self.sources.c.id).where(
                    self.sources.c.normalized_name == normalized(name))).first() is not None:
                raise SourceConflict('Another connector already has this name.')
            connection.execute(self.sources.insert().values(
                id=source_id, kind=kind, direction=direction, name=name, normalized_name=normalized(name),
                enabled=1, settings=json.dumps(settings, sort_keys=True), secret_envelope=envelope,
                key_id=key[0], key_binding_id=key[1], key_principal_id=key[2], next_check_at=now,
                version=1, created_at=now, updated_at=now))
            self._write_senders(connection, source_id, senders, now)
            return self.get(source_id, connection)

    def update(self, source_id, *, version, name=None, settings=None, secret=None, senders=None):
        now = self.clock()
        with write_transaction(self.engine) as connection:
            current = self._current(connection, source_id, version)
            values = {'version': current.version + 1, 'updated_at': now, 'next_check_at': now}
            if name is not None:
                name = self._name(name)
                clash = connection.execute(sa.select(self.sources.c.id).where(
                    self.sources.c.normalized_name == normalized(name), self.sources.c.id != source_id)).first()
                if clash is not None:
                    raise SourceConflict('Another connector already has this name.')
                values.update(name=name, normalized_name=normalized(name))
            if settings is not None:
                values['settings'] = json.dumps(settings, sort_keys=True)
            if secret is not None:
                values['secret_envelope'] = self.secrets.seal(secret, source_id) if secret else None
            connection.execute(self.sources.update().where(self.sources.c.id == source_id).values(**values))
            if senders is not None:
                connection.execute(self.senders.delete().where(self.senders.c.source_id == source_id))
                self._write_senders(connection, source_id, senders, now)
            return self.get(source_id, connection)

    def _current(self, connection, source_id, version=None):
        current = self.get(source_id, connection)
        if current is None or current.removed:
            raise SourceConflict('This connector no longer exists.')
        if version is not None and version != current.version:
            raise SourceConflict('This connector changed; reload and try again.')
        return current

    def set_paused(self, source_id, *, paused, reason=None, version=None, key=None, clear_key=False):
        """Pause (with the reason shown beside it) or resume; ``key`` replaces the sending key on resume."""
        now = self.clock()
        with write_transaction(self.engine) as connection:
            current = self._current(connection, source_id, version)
            values = {'enabled': 0 if paused else 1, 'paused_reason': (reason or text.PAUSED)[:300] if paused else None,
                      'version': current.version + 1, 'updated_at': now, 'claim_token': None,
                      'claim_expires_at': None, 'next_check_at': None if paused else now}
            if key is not None:
                values.update(key_id=key[0], key_binding_id=key[1], key_principal_id=key[2])
            elif clear_key:
                values.update(key_id=None, key_binding_id=None)
            connection.execute(self.sources.update().where(self.sources.c.id == source_id).values(**values))
            return self.get(source_id, connection)

    def replace_secret_fields(self, source, fields):
        """Store new sealed values (the sending key's token) beside the existing ones."""
        secret = {**self.secret(source), **fields}
        with write_transaction(self.engine) as connection:
            connection.execute(self.sources.update().where(self.sources.c.id == source.id).values(
                secret_envelope=self.secrets.seal(secret, source.id), updated_at=self.clock()))

    def remove(self, source_id):
        """Mark removed; the connector's items stay as the record of what it filed and sent."""
        now = self.clock()
        with write_transaction(self.engine) as connection:
            current = self._current(connection, source_id)
            connection.execute(self.sources.update().where(self.sources.c.id == source_id).values(
                enabled=0, removed_at=now, normalized_name=('removed:' + source_id)[:100], secret_envelope=None,
                claim_token=None, claim_expires_at=None, next_check_at=None, key_id=None, key_binding_id=None,
                version=current.version + 1, updated_at=now))
        return current

    # -- email senders ------------------------------------------------------------------
    def _write_senders(self, connection, source_id, senders, now):
        seen = set()
        for entry in senders:
            address, principal_id = entry['address'], entry['principal_id']
            key = address.casefold()
            if key in seen:
                raise SourceInputError(f'{address} is listed twice.')
            seen.add(key)
            connection.execute(self.senders.insert().values(
                id=uuid4().hex, source_id=source_id, address=address, normalized_address=key,
                principal_id=principal_id, created_at=now))

    def senders_of(self, source_id):
        query = (sa.select(self.senders.c.address, self.senders.c.principal_id, self.principals.c.display_name,
                           self.principals.c.enabled)
                 .select_from(self.senders.outerjoin(self.principals,
                                                     self.principals.c.id == self.senders.c.principal_id))
                 .where(self.senders.c.source_id == source_id).order_by(self.senders.c.normalized_address))
        with read_connection(self.engine) as connection:
            return [{'address': row.address, 'principal_id': row.principal_id, 'name': row.display_name,
                     'enabled': bool(row.enabled) if row.enabled is not None else False}
                    for row in connection.execute(query)]

    def person_for(self, source_id, address):
        """(principal id, display name) mapped to this address, or (None, None)."""
        query = (sa.select(self.senders.c.principal_id, self.principals.c.display_name)
                 .select_from(self.senders.outerjoin(self.principals,
                                                     self.principals.c.id == self.senders.c.principal_id))
                 .where(self.senders.c.source_id == source_id,
                        self.senders.c.normalized_address == (address or '').casefold()))
        with read_connection(self.engine) as connection:
            row = connection.execute(query).first()
        return (row.principal_id, row.display_name) if row is not None else (None, None)

    def people(self, ids):
        if not ids:
            return {}
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.principals.c.id, self.principals.c.display_name,
                                                self.principals.c.kind).where(self.principals.c.id.in_(list(ids))))
            return {row.id: {'name': row.display_name, 'kind': row.kind} for row in rows}

    # -- mailboxes (where received documents are filed) ---------------------------------------
    def mailbox_choices(self):
        """Every mailbox with the number Faxbot files a connector's documents under (its oldest number)."""
        with read_connection(self.engine) as connection:
            boxes = connection.execute(sa.select(self.mailboxes.c.id, self.mailboxes.c.label)
                                       .order_by(self.mailboxes.c.label, self.mailboxes.c.id)).all()
            numbers = connection.execute(
                sa.select(self.routes.c.mailbox_id, self.rules.c.to_number)
                .select_from(self.rules.join(self.routes, self.routes.c.id == self.rules.c.id))
                .where(self.rules.c.to_number != '')
                .order_by(self.rules.c.created_at, self.rules.c.id)).all()
        first = {}
        for mailbox_id, number in numbers:
            first.setdefault(mailbox_id, number)
        return [{'id': row.id, 'label': row.label, 'number': first.get(row.id)} for row in boxes]

    def mailbox(self, mailbox_id):
        return next((box for box in self.mailbox_choices() if box['id'] == mailbox_id), None)

    # -- the check lease -----------------------------------------------------------------
    def claim(self, source_id=None):
        """Lease one due connector (or the named one) for a check; returns (Source, token) or None."""
        now = self.clock()
        token = token_source.token_hex(16)
        query = sa.select(self.sources.c.id).where(
            self.sources.c.removed_at.is_(None),
            sa.or_(self.sources.c.claim_token.is_(None), self.sources.c.claim_expires_at <= now))
        if source_id is None:
            query = query.where(self.sources.c.enabled == 1, sa.or_(
                self.sources.c.next_check_at.is_(None), self.sources.c.next_check_at <= now))
        else:
            query = query.where(self.sources.c.id == source_id)
        query = query.order_by(self.sources.c.next_check_at, self.sources.c.id).limit(1)
        # A read first: most steps find nothing due and take no write lock.
        with read_connection(self.engine) as connection:
            if connection.execute(query).first() is None:
                return None
        with write_transaction(self.engine) as connection:
            found = connection.execute(query).scalar_one_or_none()
            if found is None:
                return None
            connection.execute(self.sources.update().where(self.sources.c.id == found).values(
                claim_token=token, claim_expires_at=now + LEASE))
            return self.get(found, connection), token

    def finish(self, source, token, *, ok, result):
        now = self.clock()
        with write_transaction(self.engine) as connection:
            connection.execute(self.sources.update().where(
                self.sources.c.id == source.id, self.sources.c.claim_token == token).values(
                claim_token=None, claim_expires_at=None, last_checked_at=now, last_ok=int(bool(ok)),
                last_result=(result or '')[:300] or None,
                next_check_at=now + timedelta(seconds=int(source.settings.get('check_seconds', 60)))))

    def release(self, source, token):
        with write_transaction(self.engine) as connection:
            connection.execute(self.sources.update().where(
                self.sources.c.id == source.id, self.sources.c.claim_token == token).values(
                claim_token=None, claim_expires_at=None))

    # -- items ---------------------------------------------------------------------------
    def item(self, source_id, operation_id, part=''):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.items).where(
                self.items.c.source_id == source_id, self.items.c.operation_id == operation_id,
                self.items.c.part == part)).mappings().first()
            return dict(row) if row is not None else None

    def _values(self, source_id, direction, operation_id, part, fields, now):
        values = dict(id=uuid4().hex, source_id=source_id, direction=direction, operation_id=operation_id,
                      part=part, duplicates=0, reply_state='none', reply_attempts=0, version=1, created_at=now,
                      updated_at=now)
        for name, value in fields.items():
            if isinstance(value, str):
                limit = self.items.c[name].type.length
                value = value[:limit] if limit else value
            values[name] = value
        return values

    def record(self, source_id, direction, operation_id, part='', **fields):
        """Insert the item for this identity once; returns (item, created)."""
        now = self.clock()
        try:
            with write_transaction(self.engine) as connection:
                existing = connection.execute(sa.select(self.items).where(
                    self.items.c.source_id == source_id, self.items.c.operation_id == operation_id,
                    self.items.c.part == part)).mappings().first()
                if existing is not None:
                    return dict(existing), False
                values = self._values(source_id, direction, operation_id, part, fields, now)
                connection.execute(self.items.insert().values(**values))
                return values, True
        except DeliveryStoreError:
            found = self.item(source_id, operation_id, part)
            if found is None:
                raise
            return found, False

    def link_on(self, connection, source_id, operation_id, part='', **fields):
        """Write the email-to-fax item inside the fax's acceptance transaction."""
        values = self._values(source_id, 'send', operation_id, part, fields, self.clock())
        connection.execute(self.items.insert().values(**values))
        return values

    def seen_again(self, item):
        now = self.clock()
        with write_transaction(self.engine) as connection:
            connection.execute(self.items.update().where(self.items.c.id == item['id']).values(
                duplicates=self.items.c.duplicates + 1, last_duplicate_at=now, updated_at=now))

    def mark_conflict(self, item, reason):
        now = self.clock()
        with write_transaction(self.engine) as connection:
            connection.execute(self.items.update().where(self.items.c.id == item['id'],
                                                         self.items.c.state != 'conflict').values(
                state='conflict', reason=reason[:300], duplicates=self.items.c.duplicates + 1,
                last_duplicate_at=now, version=self.items.c.version + 1, updated_at=now))

    def list_items(self, *, source_id=None, limit=100):
        query = sa.select(self.items).order_by(self.items.c.created_at.desc(), self.items.c.id).limit(limit)
        if source_id is not None:
            query = query.where(self.items.c.source_id == source_id)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def counts(self):
        """{source id: {'items', 'duplicates', 'refused', 'failed'}}."""
        refused = sa.func.sum(sa.case((self.items.c.state == 'refused', 1), else_=0))
        failed = sa.func.sum(sa.case((self.items.c.state.in_(['failed', 'conflict']), 1), else_=0))
        query = sa.select(self.items.c.source_id, sa.func.count(), sa.func.sum(self.items.c.duplicates), refused,
                          failed).group_by(self.items.c.source_id)
        with read_connection(self.engine) as connection:
            return {row[0]: {'items': int(row[1] or 0), 'duplicates': int(row[2] or 0), 'refused': int(row[3] or 0),
                             'failed': int(row[4] or 0)} for row in connection.execute(query)}

    def for_fax(self, job_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.items, self.sources.c.name.label('source_name'))
                                     .select_from(self.items.join(self.sources,
                                                                  self.sources.c.id == self.items.c.source_id))
                                     .where(self.items.c.fax_job_id == job_id)).mappings().first()
            return dict(row) if row is not None else None

    # -- replies -------------------------------------------------------------------------
    def due_replies(self, limit=10):
        now = self.clock()
        query = (sa.select(self.items).where(
            sa.or_(self.items.c.reply_state == 'due',
                   sa.and_(self.items.c.reply_state == 'waiting', self.items.c.fax_job_id.is_not(None))),
            sa.or_(self.items.c.reply_next_at.is_(None), self.items.c.reply_next_at <= now))
            .order_by(self.items.c.reply_next_at, self.items.c.created_at, self.items.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def fax_outcome(self, job_id):
        """(delivery state, pages, error) of a sent fax, or None when it no longer exists."""
        with read_connection(self.engine) as connection:
            row = connection.execute(
                sa.select(self.deliveries.c.state, self.jobs.c.pages, self.jobs.c.error, self.jobs.c.status)
                .select_from(self.jobs.outerjoin(self.deliveries, self.deliveries.c.id == self.jobs.c.id))
                .where(self.jobs.c.id == job_id)).first()
        if row is None:
            return None
        return {'state': row.state, 'pages': row.pages, 'error': row.error, 'status': row.status}

    def reply_wait(self, item, minutes=1):
        with write_transaction(self.engine) as connection:
            connection.execute(self.items.update().where(self.items.c.id == item['id']).values(
                reply_next_at=self.clock() + timedelta(minutes=minutes)))

    def reply_done(self, item, *, state, note=None, kind=None):
        """Record the one reply's outcome: sent, failed (definite) or uncertain (never resent)."""
        now = self.clock()
        attempts = item['reply_attempts'] + 1
        values = {'reply_attempts': attempts, 'reply_note': (note or '')[:300] or None, 'updated_at': now}
        if kind:
            values['reply_kind'] = kind
        if state == 'retry' and attempts <= len(REPLY_RETRY_MINUTES):
            values.update(reply_state='due', reply_next_at=now + timedelta(minutes=REPLY_RETRY_MINUTES[attempts - 1]))
        else:
            values.update(reply_state='failed' if state == 'retry' else state, reply_next_at=None,
                          replied_at=now if state == 'sent' else None)
        with write_transaction(self.engine) as connection:
            connection.execute(self.items.update().where(self.items.c.id == item['id'],
                                                         self.items.c.reply_state.in_(['due', 'waiting'])).values(**values))
