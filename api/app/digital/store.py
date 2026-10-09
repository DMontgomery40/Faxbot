"""Digital route records (migration 0052): recipients' addresses, messages and their evidence, trust bundles.

Every write is append-only except a message's ``state``, which moves forward
by compare-and-set together with the event that moved it. Nothing here
contacts a HISP or FHIR server.
"""
from dataclasses import dataclass
import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction


TABLES = ('digital_addresses', 'digital_address_events', 'digital_messages', 'digital_message_events',
          'digital_trust_bundles')
KINDS = ('direct', 'fhir')
ACTIONS = ('suggested', 'confirmed', 'withdrawn', 'dismissed')
# A message's states, by direction: what each may move to.
FORWARD = {
    'sending': ('submitted', 'delivered', 'failed', 'uncertain', 'refused'),
    'submitted': ('processed', 'dispatched', 'failed', 'uncertain'),
    'processed': ('dispatched', 'failed', 'uncertain'),
    'uncertain': ('processed', 'dispatched', 'delivered', 'failed'),
    'dispatched': (), 'delivered': (), 'failed': (), 'refused': (), 'filed': (), 'not_filed': (),
}
SETTLED = ('dispatched', 'delivered', 'failed', 'refused', 'filed', 'not_filed')


class DigitalInputError(ValueError):
    """One plain sentence about what to fix."""


def message_key(account_key, direction, message_id):
    return hashlib.sha256(f'{account_key}\n{direction}\n{message_id}'.encode('utf-8')).hexdigest()


def _principal(connection, engine, principal_id):
    if not principal_id:
        return None, None
    try:
        principals = reflect(engine, ('access_principals',))['access_principals']
    except DeliveryStoreError:
        # An installation without access tables records who, without a name.
        return principal_id[:40], None
    name = connection.execute(sa.select(principals.c.display_name).where(principals.c.id == principal_id)).scalar()
    return principal_id[:40], (name or None)


@dataclass(frozen=True)
class Message:
    row: dict

    def __getattr__(self, name):
        try:
            return self.row[name]
        except KeyError:
            raise AttributeError(name) from None


class DigitalStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, TABLES)
        self.addresses, self.events = tables['digital_addresses'], tables['digital_address_events']
        self.messages, self.message_events = tables['digital_messages'], tables['digital_message_events']
        self.bundles = tables['digital_trust_bundles']

    # Recipients' addresses ----------------------------------------------------------------------------------------

    def _state_on(self, connection, address_id):
        events = self.events
        return connection.execute(sa.select(events).where(events.c.address_id == address_id)
                                  .order_by(events.c.created_at.desc(), events.c.id.desc())).mappings().all()

    def _view_on(self, connection, row):
        history = [dict(event) for event in self._state_on(connection, row['id'])]
        current = history[0]['action'] if history else 'suggested'
        return {**dict(row), 'state': current, 'history': history}

    def add_address(self, *, number, kind, address, source, account_key=None, organization=None, npi=None,
                    evidence=None, action='suggested', note=None, principal_id=None, now=None):
        """The address's view, adding it once and appending ``action`` (``suggested`` or ``confirmed``)."""
        if kind not in KINDS or action not in ('suggested', 'confirmed'):
            raise DigitalInputError('Choose a Direct address or a FHIR endpoint.')
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.addresses).where(
                self.addresses.c.phone_number == number, self.addresses.c.kind == kind,
                self.addresses.c.address == address)).mappings().one_or_none()
            by, name = _principal(connection, self.engine, principal_id)
            if row is None:
                values = dict(id=uuid4().hex, phone_number=number, kind=kind, address=address,
                              account_key=account_key, organization=(organization or None) and organization[:200],
                              source=source, npi=npi, evidence=evidence, created_by=by, created_by_name=name,
                              created_at=now)
                connection.execute(self.addresses.insert().values(**values))
                row = values
            history = self._state_on(connection, row['id'])
            current = history[0]['action'] if history else None
            if current != action and not (action == 'suggested' and current == 'confirmed'):
                connection.execute(self.events.insert().values(
                    id=uuid4().hex, address_id=row['id'], action=action, note=note, recorded_by=by,
                    recorded_by_name=name, created_at=now))
            return self._view_on(connection, row)

    def record(self, address_id, action, *, note=None, principal_id=None, now=None):
        """Append ``action`` to an address; returns its view. Raises DigitalInputError for a change that makes no
        sense (confirming twice, withdrawing what is not confirmed)."""
        if action not in ACTIONS:
            raise DigitalInputError('Choose confirm, withdraw or dismiss.')
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.addresses).where(self.addresses.c.id == address_id)
                                     ).mappings().one_or_none()
            if row is None:
                raise DigitalInputError('Faxbot has no such address for this recipient.')
            history = self._state_on(connection, address_id)
            current = history[0]['action'] if history else 'suggested'
            if action == current:
                raise DigitalInputError({'confirmed': 'This address is already confirmed.',
                                         'withdrawn': 'This address is already withdrawn.',
                                         'dismissed': 'This suggestion is already dismissed.'}.get(
                    action, 'Nothing changed.'))
            if action == 'withdrawn' and current != 'confirmed':
                raise DigitalInputError('Only a confirmed address can be withdrawn.')
            if action == 'dismissed' and current == 'confirmed':
                raise DigitalInputError('Withdraw a confirmed address instead of dismissing it.')
            by, name = _principal(connection, self.engine, principal_id)
            connection.execute(self.events.insert().values(
                id=uuid4().hex, address_id=address_id, action=action, note=(note or None) and note[:2000],
                recorded_by=by, recorded_by_name=name, created_at=now))
            return self._view_on(connection, row)

    def address(self, address_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.addresses).where(self.addresses.c.id == address_id)
                                     ).mappings().one_or_none()
            return self._view_on(connection, row) if row is not None else None

    def addresses_for(self, number):
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.addresses).where(self.addresses.c.phone_number == number)
                                      .order_by(self.addresses.c.created_at, self.addresses.c.id)).mappings().all()
            return [self._view_on(connection, row) for row in rows]

    def confirmed_for(self, number):
        """The recipient's confirmed addresses, oldest first (the order they were added)."""
        return [view for view in self.addresses_for(number) if view['state'] == 'confirmed']

    def confirmed_ids(self):
        """Every address id whose newest event is a confirmation (for checking rules)."""
        events = self.events
        newest = sa.select(events.c.address_id, sa.func.max(events.c.created_at).label('at')).group_by(
            events.c.address_id).subquery()
        query = sa.select(events.c.address_id, events.c.action, events.c.id).join(
            newest, sa.and_(newest.c.address_id == events.c.address_id, newest.c.at == events.c.created_at))
        latest = {}
        with read_connection(self.engine) as connection:
            for address_id, action, event_id in connection.execute(query).all():
                if address_id not in latest or event_id > latest[address_id][1]:
                    latest[address_id] = (action, event_id)
        return {address_id for address_id, (action, _) in latest.items() if action == 'confirmed'}

    # Messages ---------------------------------------------------------------------------------------------------

    def begin_message(self, *, direction, kind, account_key, message_id, counterpart, state, address_id=None,
                      job_id=None, attempt_id=None, security=None, digest=None, size=None, pages=None,
                      certificate_sha256=None, detail=None, now=None):
        """(message row, created). A message with the same account, direction and ID is returned as it is."""
        now = now or utcnow()
        key = message_key(account_key, direction, message_id)
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.messages).where(self.messages.c.message_key == key)
                                     ).mappings().one_or_none()
            if row is not None:
                return dict(row), False
            values = dict(id=uuid4().hex, direction=direction, kind=kind, account_key=account_key,
                          address_id=address_id, job_id=job_id, attempt_id=attempt_id,
                          message_id=message_id[:512], message_key=key, counterpart=counterpart[:512], state=state,
                          security=security, digest=digest, size=size, pages=pages,
                          certificate_sha256=certificate_sha256, detail=(detail or None) and detail[:300],
                          created_at=now, updated_at=now, settled_at=now if state in SETTLED else None)
            try:
                connection.execute(self.messages.insert().values(**values))
            except sa.exc.IntegrityError:
                raise DigitalInputError('This message was recorded at the same moment by another worker.') from None
            return values, True

    def message(self, row_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.messages).where(self.messages.c.id == row_id)
                                     ).mappings().one_or_none()
            return dict(row) if row is not None else None

    def message_for(self, account_key, direction, message_id):
        key = message_key(account_key, direction, message_id)
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.messages).where(self.messages.c.message_key == key)
                                     ).mappings().one_or_none()
            return dict(row) if row is not None else None

    def message_by_id(self, message_id, *, direction='out'):
        """A message by its Message-ID alone (a delivery notice names only that), or None."""
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.messages).where(
                self.messages.c.message_id == message_id, self.messages.c.direction == direction)
                .order_by(self.messages.c.created_at.desc()).limit(1)).mappings().one_or_none()
            return dict(row) if row is not None else None

    def for_attempt(self, attempt_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.messages).where(
                self.messages.c.attempt_id == attempt_id, self.messages.c.direction == 'out')
                .order_by(self.messages.c.created_at.desc()).limit(1)).mappings().one_or_none()
            return dict(row) if row is not None else None

    def for_job(self, job_id):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.messages).where(
                self.messages.c.job_id == job_id).order_by(self.messages.c.created_at)).mappings()]

    def in_states(self, states, *, direction='out', kind=None, limit=100):
        query = sa.select(self.messages).where(self.messages.c.state.in_(tuple(states)),
                                               self.messages.c.direction == direction)
        if kind is not None:
            query = query.where(self.messages.c.kind == kind)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                query.order_by(self.messages.c.updated_at, self.messages.c.id).limit(limit)).mappings()]

    def recent(self, *, direction=None, kind=None, limit=50):
        query = sa.select(self.messages)
        if direction is not None:
            query = query.where(self.messages.c.direction == direction)
        if kind is not None:
            query = query.where(self.messages.c.kind == kind)
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                query.order_by(self.messages.c.created_at.desc(), self.messages.c.id.desc()).limit(limit)).mappings()]

    def count_since(self, account_key, since, *, direction='out'):
        """Messages an account sent since ``since`` that reached the HISP or server (for a plan's allowance)."""
        query = sa.select(sa.func.count()).select_from(self.messages).where(
            self.messages.c.account_key == account_key, self.messages.c.direction == direction,
            self.messages.c.created_at >= since, self.messages.c.state.not_in(('refused', 'sending')))
        with read_connection(self.engine) as connection:
            return connection.execute(query).scalar() or 0

    def move(self, row_id, state, *, expected, kind, dedupe, details=None, detail=None, remote_id=None,
             submitted=False, now=None):
        """Move a message to ``state`` from one of ``expected`` and append the event, once per ``dedupe``.

        Returns True when it moved; False when the event was already recorded or the message had moved on (the
        event is still recorded as late evidence when it is new).
        """
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            if not self._event_on(connection, row_id, kind, dedupe, details, now):
                return False
            values = {'state': state, 'updated_at': now}
            if detail is not None:
                values['detail'] = detail[:300]
            if remote_id is not None:
                values['remote_id'] = remote_id[:512]
            if submitted:
                values['submitted_at'] = now
            if state in SETTLED:
                values['settled_at'] = now
            moved = connection.execute(self.messages.update().where(
                self.messages.c.id == row_id, self.messages.c.state.in_(tuple(expected))).values(**values)).rowcount
            return bool(moved)

    def event(self, row_id, kind, *, dedupe, details=None, now=None):
        """Append one event; False when ``dedupe`` was already recorded."""
        with write_transaction(self.engine) as connection:
            return self._event_on(connection, row_id, kind, dedupe, details, now or utcnow())

    def _event_on(self, connection, row_id, kind, dedupe, details, now):
        key = hashlib.sha256(dedupe.encode('utf-8')).hexdigest()
        if connection.execute(sa.select(self.message_events.c.id).where(
                self.message_events.c.dedupe_key == key)).first():
            return False
        connection.execute(self.message_events.insert().values(
            id=uuid4().hex, message_row_id=row_id, kind=kind[:24], dedupe_key=key,
            details=json.dumps(details, sort_keys=True, separators=(',', ':'))[:4000] if details else None,
            created_at=now))
        return True

    def events_for(self, row_id):
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.message_events).where(
                self.message_events.c.message_row_id == row_id).order_by(
                self.message_events.c.created_at, self.message_events.c.id)).mappings().all()
        found = []
        for row in rows:
            item = dict(row)
            try:
                item['details'] = json.loads(row['details']) if row['details'] else {}
            except ValueError:
                item['details'] = {}
            found.append(item)
        return found

    # Trust bundles ----------------------------------------------------------------------------------------------

    def add_bundle(self, account_key, content, *, anchors, source_url=None, principal_id=None, now=None):
        now = now or utcnow()
        digest = hashlib.sha256(content.encode('ascii')).hexdigest()
        with write_transaction(self.engine) as connection:
            by, name = _principal(connection, self.engine, principal_id)
            values = dict(id=uuid4().hex, account_key=account_key, source_url=source_url, content=content,
                          sha256=digest, anchors=anchors, created_by=by, created_by_name=name, created_at=now)
            connection.execute(self.bundles.insert().values(**values))
            return values

    def bundle(self, account_key):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.bundles).where(self.bundles.c.account_key == account_key)
                                     .order_by(self.bundles.c.created_at.desc(), self.bundles.c.id.desc()).limit(1)
                                     ).mappings().one_or_none()
            return dict(row) if row is not None else None
