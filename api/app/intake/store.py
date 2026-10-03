"""Intake items, email connectors and the delivery lease that prevents duplicates.

An item is delivered at most once automatically. A send whose outcome is
unknown (the mail server stopped answering after the document was sent) is
marked failed with a plain explanation and is never resent without a person.
"""
from dataclasses import dataclass
from datetime import timedelta
import json
import re
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction
from ..routing.numbers import InvalidNumber, normalize_number


LEASE = timedelta(minutes=2)
MAX_AUTOMATIC_ATTEMPTS = 6
SECURITY = ('starttls', 'tls', 'none')
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+")
_HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\]')
_FIELDS = re.compile(r'\{(\w+)\}')
SUBJECT_FIELDS = frozenset({'from_number', 'to_number', 'pages', 'received_at'})


class IntakeInputError(ValueError):
    """Plain-sentence validation failure."""


class IntakeConflict(RuntimeError):
    """The record changed or is in a state that does not allow this action."""


def number_key(value):
    try:
        return normalize_number(value)
    except InvalidNumber:
        return None


@dataclass(frozen=True)
class EmailSettings:
    host: str
    port: int
    security: str
    username: str
    from_address: str
    recipients: tuple
    subject_template: str

    @classmethod
    def validate(cls, data):
        if not isinstance(data, dict):
            raise IntakeInputError('Enter the email server details.')
        host = str(data.get('host') or '').strip()
        if not _HOST.fullmatch(host):
            raise IntakeInputError('Enter the email server name, such as smtp.example.org.')
        port = data.get('port', 587)
        if type(port) is not int or not 1 <= port <= 65535:
            raise IntakeInputError('Enter an email server port between 1 and 65535.')
        security = data.get('security', 'starttls')
        if security not in SECURITY:
            raise IntakeInputError('Choose how to secure the connection: STARTTLS, TLS, or none.')
        username = str(data.get('username') or '').strip()
        if len(username) > 200:
            raise IntakeInputError('The email user name can be up to 200 characters.')
        sender = str(data.get('from_address') or '').strip()
        if not _EMAIL.fullmatch(sender):
            raise IntakeInputError('Enter the address faxes are sent from, such as fax@example.org.')
        recipients = data.get('recipients') or []
        if isinstance(recipients, str):
            recipients = [part for part in re.split(r'[,;\s]+', recipients) if part]
        if (not isinstance(recipients, (list, tuple)) or not 1 <= len(recipients) <= 20
                or any(not isinstance(item, str) or not _EMAIL.fullmatch(item.strip()) for item in recipients)):
            raise IntakeInputError('Enter one to twenty recipient email addresses.')
        subject = str(data.get('subject_template') or 'Fax from {from_number}').strip()
        if (not subject or len(subject) > 200 or any(ord(c) < 32 for c in subject)
                or set(_FIELDS.findall(subject)) - SUBJECT_FIELDS or subject.count('{') != len(_FIELDS.findall(subject))):
            raise IntakeInputError('The subject can use {from_number}, {to_number}, {pages} and {received_at}.')
        return cls(host, port, security, username, sender, tuple(item.strip() for item in recipients), subject)

    def as_dict(self):
        return {'host': self.host, 'port': self.port, 'security': self.security, 'username': self.username,
                'from_address': self.from_address, 'recipients': list(self.recipients),
                'subject_template': self.subject_template}


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    enabled: bool
    match_number: str | None
    settings: EmailSettings
    has_password: bool
    managed: bool
    version: int
    created_at: object


class IntakeStore:
    TABLES = ('intake_items', 'intake_connectors', 'inbound_faxes', 'direct_deliveries')

    def __init__(self, engine, secrets):
        """``secrets`` seals and opens connector passwords bound to their record."""
        self.engine = engine
        self.secrets = secrets
        tables = reflect(engine, self.TABLES)
        self.items = tables['intake_items']
        self.connectors = tables['intake_connectors']
        self.inbound = tables['inbound_faxes']
        self.direct = tables['direct_deliveries']

    # Connectors ---------------------------------------------------------
    def _connector(self, row):
        data = json.loads(row['settings'])
        return Connector(row['id'], row['name'], bool(row['enabled']), row['match_number'],
                         EmailSettings.validate(data), row['secret_envelope'] is not None, bool(data.get('managed')),
                         row['version'], row['created_at'])

    def list_connectors(self, connection=None):
        def read(conn):
            rows = conn.execute(sa.select(self.connectors).order_by(self.connectors.c.created_at,
                                                                   self.connectors.c.id)).mappings()
            return [self._connector(row) for row in rows]
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def get_connector(self, identity, connection=None):
        return next((c for c in self.list_connectors(connection) if c.id == identity), None)

    def password(self, identity):
        with read_connection(self.engine) as connection:
            envelope = connection.scalar(sa.select(self.connectors.c.secret_envelope).where(
                self.connectors.c.id == identity))
        return self.secrets.open(envelope, identity) if envelope else ''

    @staticmethod
    def _name(name):
        name = (name or '').strip() if isinstance(name, str) else ''
        if not name or len(name) > 100:
            raise IntakeInputError('Give the connector a name of up to 100 characters.')
        return name

    @staticmethod
    def _match(value):
        if value in (None, ''):
            return None
        key = number_key(value)
        if key is None:
            raise IntakeInputError('Enter the fax number this connector handles, with its country code.')
        return key

    def create_connector(self, *, name, settings, password=None, enabled=True, match_number=None, managed=False):
        settings = EmailSettings.validate(settings)
        identity, now = uuid4().hex, utcnow()
        document = {**settings.as_dict(), **({'managed': True} if managed else {})}
        with write_transaction(self.engine) as connection:
            connection.execute(self.connectors.insert().values(
                id=identity, kind='email', name=self._name(name), enabled=int(bool(enabled)),
                match_number=self._match(match_number), settings=json.dumps(document, sort_keys=True),
                secret_envelope=self.secrets.seal(password, identity) if password else None,
                version=1, created_at=now, updated_at=now))
            return self.get_connector(identity, connection)

    def update_connector(self, identity, *, version, name=None, settings=None, password=None, enabled=None,
                         match_number=..., allow_managed=False):
        """``password=None`` keeps the stored password; an empty string removes it."""
        now = utcnow()
        with write_transaction(self.engine) as connection:
            current = self.get_connector(identity, connection)
            if current is None:
                raise IntakeConflict('This connector no longer exists.')
            if current.managed and not allow_managed:
                raise IntakeConflict('This connector is set in the installation settings.')
            if version is not None and version != current.version:
                raise IntakeConflict('This connector changed; reload and try again.')
            values = {'version': current.version + 1, 'updated_at': now}
            if name is not None:
                values['name'] = self._name(name)
            if settings is not None:
                document = {**EmailSettings.validate(settings).as_dict(), **({'managed': True} if current.managed else {})}
                values['settings'] = json.dumps(document, sort_keys=True)
            if password is not None:
                values['secret_envelope'] = self.secrets.seal(password, identity) if password else None
            if enabled is not None:
                values['enabled'] = int(bool(enabled))
            if match_number is not ...:
                values['match_number'] = self._match(match_number)
            connection.execute(self.connectors.update().where(self.connectors.c.id == identity).values(**values))
            return self.get_connector(identity, connection)

    def delete_connector(self, identity):
        with write_transaction(self.engine) as connection:
            current = self.get_connector(identity, connection)
            if current is None:
                return False
            if current.managed:
                raise IntakeConflict('This connector is set in the installation settings.')
            connection.execute(self.connectors.delete().where(self.connectors.c.id == identity))
            return True

    def sync_managed(self, values):
        """Keep the connector defined by INTAKE_* settings in step with them."""
        managed = next((c for c in self.list_connectors() if c.managed), None)
        wanted = values.intake_email_enabled and values.intake_smtp_host and values.intake_email_to
        if not wanted:
            if managed is not None and managed.enabled:
                self.update_connector(managed.id, version=managed.version, enabled=False, allow_managed=True)
            return None
        settings = {'host': values.intake_smtp_host, 'port': values.intake_smtp_port,
                    'security': values.intake_smtp_security, 'username': values.intake_smtp_username,
                    'from_address': values.intake_email_from, 'recipients': values.intake_email_to,
                    'subject_template': values.intake_email_subject}
        if managed is None:
            return self.create_connector(name='Email from installation settings', settings=settings,
                                         password=values.intake_smtp_password or None, managed=True)
        current_password = self.password(managed.id)
        if (managed.settings == EmailSettings.validate(settings) and managed.enabled
                and current_password == values.intake_smtp_password):
            return managed
        return self.update_connector(managed.id, version=managed.version, settings=settings, enabled=True,
                                     password=values.intake_smtp_password, allow_managed=True)

    def connector_for(self, to_number, connection=None):
        """The enabled connector for a fax number: an exact number match, else a catch-all."""
        key = number_key(to_number) if to_number else None
        enabled = [c for c in self.list_connectors(connection) if c.enabled]
        exact = [c for c in enabled if key is not None and c.match_number == key]
        return (exact or [c for c in enabled if c.match_number is None] or [None])[0]

    # Items --------------------------------------------------------------
    def _schedule(self, connection, to_number, received_at, now):
        connector = self.connector_for(to_number, connection)
        # A connector delivers what arrives after it was set up; earlier faxes wait for a person.
        if connector is None:
            return None, 'No email delivery is set up for this number yet.'
        if received_at < connector.created_at:
            return None, 'This fax arrived before email delivery was set up; send it when you are ready.'
        return now, None

    def feed_inbound(self, *, limit=100, now=None):
        """Create one item per received inbound fax that has a document."""
        now = now or utcnow()
        inbound, items = self.inbound, self.items
        query = (sa.select(inbound.c.id, inbound.c.from_number, inbound.c.to_number, inbound.c.pages,
                           inbound.c.received_at)
                 .select_from(inbound.outerjoin(items, items.c.inbound_fax_id == inbound.c.id))
                 .where(items.c.id.is_(None), inbound.c.pdf_path.is_not(None), inbound.c.pdf_path != '')
                 .order_by(inbound.c.received_at, inbound.c.id).limit(limit))
        created = 0
        with read_connection(self.engine) as connection:
            rows = connection.execute(query).all()
        for row in rows:
            try:
                with write_transaction(self.engine) as connection:
                    if connection.execute(sa.select(items.c.id).where(items.c.inbound_fax_id == row.id)).first():
                        continue
                    when, note = self._schedule(connection, row.to_number, row.received_at, now)
                    connection.execute(items.insert().values(
                        id=uuid4().hex, source='fax', inbound_fax_id=row.id, received_at=row.received_at,
                        pages=row.pages, from_number=row.from_number, to_number=row.to_number, state='received',
                        attempts=0, next_attempt_at=when, last_error=note, version=1, created_at=now, updated_at=now))
                    created += 1
            except DeliveryStoreError:
                continue  # A concurrent feeder created it; the unique index decides.
        return created

    def add_direct(self, connection, *, direct_delivery_id, received_at, pages, from_number, to_number, now):
        """Called inside the direct delivery acceptance transaction."""
        identity = uuid4().hex
        when, note = self._schedule(connection, to_number, received_at, now)
        connection.execute(self.items.insert().values(
            id=identity, source='direct', direct_delivery_id=direct_delivery_id, received_at=received_at,
            pages=pages, from_number=from_number, to_number=to_number, state='received', attempts=0,
            next_attempt_at=when, last_error=note, version=1, created_at=now, updated_at=now))
        return identity

    def get_item(self, identity, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.items).where(self.items.c.id == identity)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def list_items(self, *, state=None, limit=100):
        query = sa.select(self.items).order_by(self.items.c.received_at.desc(), self.items.c.id).limit(limit)
        if state is not None:
            query = query.where(self.items.c.state == state)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def counts(self):
        with read_connection(self.engine) as connection:
            return dict(connection.execute(sa.select(self.items.c.state, sa.func.count()).group_by(
                self.items.c.state)).all())

    def schedule_retry(self, identity, *, now=None):
        """A person asks to send a waiting or failed item now."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = self.get_item(identity, connection)
            if row is None:
                raise IntakeConflict('This item no longer exists.')
            if row['state'] not in {'received', 'failed'}:
                raise IntakeConflict('This item is already delivered or being sent.')
            connection.execute(self.items.update().where(self.items.c.id == identity,
                                                         self.items.c.version == row['version']).values(
                state='received', next_attempt_at=now, last_error=None, version=row['version'] + 1, updated_at=now))
            return self.get_item(identity, connection)

    def _due(self, now):
        return sa.select(self.items.c.id).where(
            self.items.c.state == 'received', self.items.c.next_attempt_at.is_not(None),
            self.items.c.next_attempt_at <= now).limit(1)

    def claim(self, *, now=None):
        """Lease the oldest item due for delivery; a delivered item is never claimed."""
        now = now or utcnow()
        with read_connection(self.engine) as connection:
            if connection.execute(self._due(now)).first() is None:
                return None  # Nothing due; avoid taking the write lock.
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.items).where(
                self.items.c.state == 'received', self.items.c.next_attempt_at.is_not(None),
                self.items.c.next_attempt_at <= now).order_by(self.items.c.next_attempt_at, self.items.c.id)
                .limit(1)).mappings().one_or_none()
            if row is None:
                return None
            token = uuid4().hex
            connection.execute(self.items.update().where(self.items.c.id == row['id']).values(
                state='sending', claim_token=token, claim_expires_at=now + LEASE, attempts=row['attempts'] + 1,
                version=row['version'] + 1, updated_at=now))
            return {**dict(row), 'claim_token': token, 'attempts': row['attempts'] + 1}

    def _finish(self, item, now, **values):
        with write_transaction(self.engine) as connection:
            current = self.get_item(item['id'], connection)
            if current is None or current['state'] != 'sending' or current['claim_token'] != item['claim_token']:
                raise IntakeConflict('This delivery lease is no longer current.')
            connection.execute(self.items.update().where(self.items.c.id == item['id']).values(
                claim_token=None, claim_expires_at=None, version=current['version'] + 1, updated_at=now, **values))

    def record_delivered(self, item, *, connector_id, reference, now=None):
        self._finish(item, now or utcnow(), state='delivered', delivered_at=now or utcnow(),
                     delivery_reference=reference[:255], connector_id=connector_id, last_error=None,
                     next_attempt_at=None)

    def record_retry(self, item, *, message, connector_id=None, now=None):
        """A definite failure that may succeed later; back off, then stop for a person."""
        now = now or utcnow()
        if item['attempts'] >= MAX_AUTOMATIC_ATTEMPTS:
            self.record_failed(item, message=message + ' Faxbot stopped retrying.', connector_id=connector_id, now=now)
            return False
        delay = timedelta(minutes=min(60, 2 ** (item['attempts'] - 1)))
        self._finish(item, now, state='received', next_attempt_at=now + delay, last_error=message[:200],
                     connector_id=connector_id)
        return True

    def record_failed(self, item, *, message, connector_id=None, now=None):
        self._finish(item, now or utcnow(), state='failed', next_attempt_at=None, last_error=message[:200],
                     connector_id=connector_id)

    def recover_expired(self, *, now=None):
        """A worker stopped mid-send; the email may have gone out, so a person decides."""
        now = now or utcnow()
        expired = sa.select(self.items.c.id).where(self.items.c.state == 'sending',
                                                   self.items.c.claim_expires_at <= now).limit(1)
        with read_connection(self.engine) as connection:
            if connection.execute(expired).first() is None:
                return 0
        with write_transaction(self.engine) as connection:
            result = connection.execute(self.items.update().where(
                self.items.c.state == 'sending', self.items.c.claim_expires_at <= now).values(
                state='failed', claim_token=None, claim_expires_at=None, next_attempt_at=None,
                last_error='Faxbot stopped while sending this email; check the inbox before sending it again.',
                version=self.items.c.version + 1, updated_at=now))
            return result.rowcount

    def document(self, item):
        """Where the item's original document is: a local path or a storage URI."""
        with read_connection(self.engine) as connection:
            if item['inbound_fax_id']:
                return connection.scalar(sa.select(self.inbound.c.pdf_path).where(
                    self.inbound.c.id == item['inbound_fax_id']))
            if item['direct_delivery_id']:
                return connection.scalar(sa.select(self.direct.c.document_path).where(
                    self.direct.c.id == item['direct_delivery_id']))
        return None
