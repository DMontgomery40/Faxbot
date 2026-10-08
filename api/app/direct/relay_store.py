"""The partner relay ledger: agreements, the signed statements behind them, and every relayed fax.

Statements are append-only: each one is kept exactly as it was signed, with
its signature and SHA-256, and never updated. An agreement row holds the
current state on this installation (offered, accepting, active, withdrawn)
with a version; a relayed fax row moves once from sending or accepted to its
outcome (an uncertain outcome may still settle). Usage against an agreement's
limits is always counted from the relayed faxes, never kept as a counter.
"""
import hashlib
import json
from uuid import uuid4

import sqlalchemy as sa

from ..routing.database import read_connection, reflect, utcnow, write_transaction


ROLES = ('relay', 'sender')
STATES = ('offered', 'accepting', 'active', 'withdrawn')
OUTCOMES = ('delivered', 'failed_before_data', 'uncertain')
# A relayed fax counts against an agreement's limits unless the relay refused it.
COUNTED = ('sending', 'accepted', 'delivered', 'failed_before_data', 'uncertain')


class RelayConflict(RuntimeError):
    """One plain sentence for the administrator."""


def digest(text):
    return hashlib.sha256(text.encode('ascii')).hexdigest()


class RelayStore:
    TABLES = ('relay_agreements', 'relay_statements', 'relay_faxes', 'direct_peers', 'direct_deliveries')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.agreements = tables['relay_agreements']
        self.statements = tables['relay_statements']
        self.faxes = tables['relay_faxes']
        self.peers = tables['direct_peers']
        self.deliveries = tables['direct_deliveries']

    # Statements ------------------------------------------------------------------------------
    def keep_on(self, connection, *, peer_id, direction, kind, envelope, agreement_id=None, message_id=None,
                now=None):
        """Keep one signed statement as signed; the same statement kept twice is kept once. Returns its ID."""
        text = envelope['statement']
        found = digest(text)
        existing = connection.execute(sa.select(self.statements.c.id).where(
            self.statements.c.direction == direction, self.statements.c.digest == found)).scalar_one_or_none()
        if existing is not None:
            return existing
        identity = uuid4().hex
        connection.execute(self.statements.insert().values(
            id=identity, agreement_id=agreement_id, peer_id=peer_id, direction=direction, kind=kind,
            message_id=message_id, statement=text, signature=envelope['signature'], digest=found,
            created_at=now or utcnow()))
        return identity

    def keep(self, **values):
        with write_transaction(self.engine) as connection:
            return self.keep_on(connection, **values)

    def statement(self, statement_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.statements).where(
                self.statements.c.id == statement_id)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    @staticmethod
    def envelope(row):
        return {'statement': row['statement'], 'signature': row['signature']}

    def latest(self, *, peer_id, kind, direction, agreement_id=None):
        query = sa.select(self.statements).where(self.statements.c.peer_id == peer_id,
                                                 self.statements.c.kind == kind,
                                                 self.statements.c.direction == direction)
        if agreement_id is not None:
            query = query.where(self.statements.c.agreement_id == agreement_id)
        with read_connection(self.engine) as connection:
            row = connection.execute(query.order_by(self.statements.c.created_at.desc(),
                                                    self.statements.c.id.desc()).limit(1)).mappings().one_or_none()
            return dict(row) if row is not None else None

    def history(self, agreement_id):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.statements).where(
                self.statements.c.agreement_id == agreement_id).order_by(
                self.statements.c.created_at, self.statements.c.id)).mappings()]

    # Agreements ------------------------------------------------------------------------------
    def agreement(self, agreement_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.agreements).where(
                self.agreements.c.id == agreement_id)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def agreements_for(self, *, peer_id=None, role=None, states=None):
        query = sa.select(self.agreements, self.peers.c.organization, self.peers.c.phone_number,
                          self.peers.c.state.label('peer_state'), self.peers.c.expires_at.label('peer_expires_at')
                          ).select_from(self.agreements.join(self.peers, self.peers.c.id == self.agreements.c.peer_id))
        if peer_id is not None:
            query = query.where(self.agreements.c.peer_id == peer_id)
        if role is not None:
            query = query.where(self.agreements.c.role == role)
        if states is not None:
            query = query.where(self.agreements.c.state.in_(list(states)))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query.order_by(
                self.peers.c.organization, self.agreements.c.created_at)).mappings()]

    def create(self, *, agreement_id, peer_id, role, state, terms, statement=None, direction=None,
               principal_id=None, send_together=False, actor_name=None, price_statement=None, now=None):
        """A new agreement row and the statement that made it, in one transaction."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            if self.agreement(agreement_id, connection) is not None:
                raise RelayConflict('This relay agreement already exists.')
            statement_id = None
            if statement is not None:
                statement_id = self.keep_on(connection, peer_id=peer_id, direction=direction, kind='offer',
                                            envelope=statement, agreement_id=agreement_id, now=now)
            price_id = None
            if price_statement is not None:
                price_id = self.keep_on(connection, peer_id=peer_id, direction=direction, kind='price',
                                        envelope=price_statement, agreement_id=agreement_id, now=now)
            connection.execute(self.agreements.insert().values(
                id=agreement_id, peer_id=peer_id, role=role, state=state,
                terms=json.dumps(terms, sort_keys=True, separators=(',', ':')), send_together=int(bool(send_together)),
                principal_id=principal_id, price_statement_id=price_id or statement_id,
                pending_statement_id=statement_id if direction == 'sent' else None,
                actor_name=(actor_name or '')[:200] or None, version=1, created_at=now, updated_at=now))
            return self.agreement(agreement_id, connection)

    def change(self, agreement_id, *, expected_state=None, statement=None, kind=None, direction=None, now=None,
               **values):
        """Move an agreement on, keeping the statement that moved it; None when it was not in ``expected_state``."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = self.agreement(agreement_id, connection)
            if row is None or (expected_state is not None and row['state'] not in expected_state):
                return None
            if statement is not None:
                statement_id = self.keep_on(connection, peer_id=row['peer_id'], direction=direction, kind=kind,
                                            envelope=statement, agreement_id=agreement_id, now=now)
                if direction == 'sent':
                    values.setdefault('pending_statement_id', statement_id)
                    values.setdefault('told_at', None)
                if kind == 'price':
                    values['price_statement_id'] = statement_id
            connection.execute(self.agreements.update().where(self.agreements.c.id == agreement_id).values(
                version=row['version'] + 1, updated_at=now, **values))
            return self.agreement(agreement_id, connection)

    def told(self, agreement_id, statement_id):
        """The partner has our latest statement; kept only while it is still the latest."""
        with write_transaction(self.engine) as connection:
            connection.execute(self.agreements.update().where(
                self.agreements.c.id == agreement_id,
                self.agreements.c.pending_statement_id == statement_id).values(
                pending_statement_id=None, told_at=utcnow()))

    def untold(self, *, limit=20):
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(sa.select(self.agreements).where(
                self.agreements.c.pending_statement_id.is_not(None)).order_by(
                self.agreements.c.updated_at).limit(limit)).mappings()]

    # Relayed faxes ---------------------------------------------------------------------------
    def fax(self, *, role, message_id, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.faxes).where(
                self.faxes.c.role == role, self.faxes.c.message_id == message_id)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def fax_for_job(self, job_id, *, role):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.faxes).where(
                self.faxes.c.job_id == job_id, self.faxes.c.role == role).order_by(
                self.faxes.c.created_at.desc()).limit(1)).mappings().one_or_none()
            return dict(row) if row is not None else None

    def add_fax_on(self, connection, *, role, message_id, agreement_id, peer_id, job_id, destination, pages,
                   state, attempt_id=None, shared=False, cost=None, own_route=None, detail=None, now=None):
        now = now or utcnow()
        identity = uuid4().hex
        connection.execute(self.faxes.insert().values(
            id=identity, role=role, message_id=message_id, agreement_id=agreement_id, peer_id=peer_id,
            job_id=job_id, attempt_id=attempt_id, destination=destination, pages=pages, state=state, detail=detail,
            shared=int(bool(shared)), cost_micros=cost[0] if cost else None, cost_currency=cost[1] if cost else None,
            own_route_micros=own_route[0] if own_route else None,
            own_route_currency=own_route[1] if own_route else None, created_at=now, updated_at=now))
        return identity

    def add_fax(self, **values):
        with write_transaction(self.engine) as connection:
            existing = self.fax(role=values['role'], message_id=values['message_id'], connection=connection)
            if existing is not None:
                return existing
            self.add_fax_on(connection, **values)
            return self.fax(role=values['role'], message_id=values['message_id'], connection=connection)

    def move_fax(self, fax_id, state, *, expected, statement=None, peer_id=None, direction=None, now=None,
                 **values):
        """Move one relayed fax to ``state`` (keeping its outcome statement); None when it was not ``expected``."""
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = connection.execute(sa.select(self.faxes).where(self.faxes.c.id == fax_id)).mappings().one_or_none()
            if row is None or row['state'] not in expected:
                return None
            if statement is not None:
                values['outcome_statement_id'] = self.keep_on(
                    connection, peer_id=peer_id or row['peer_id'], direction=direction, kind='outcome',
                    envelope=statement, agreement_id=row['agreement_id'], message_id=row['message_id'], now=now)
                values.setdefault('outcome_at', now)
            connection.execute(self.faxes.update().where(self.faxes.c.id == fax_id).values(
                state=state, updated_at=now, **values))
            return dict(connection.execute(sa.select(self.faxes).where(
                self.faxes.c.id == fax_id)).mappings().one())

    def mark_fax_told(self, fax_id, statement_id):
        with write_transaction(self.engine) as connection:
            connection.execute(self.faxes.update().where(
                self.faxes.c.id == fax_id, self.faxes.c.outcome_statement_id == statement_id).values(
                told_at=utcnow()))

    def faxes_in(self, *, role, states, limit=50, untold=False):
        query = sa.select(self.faxes).where(self.faxes.c.role == role, self.faxes.c.state.in_(list(states)))
        if untold:
            query = query.where(self.faxes.c.outcome_statement_id.is_not(None), self.faxes.c.told_at.is_(None))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                query.order_by(self.faxes.c.updated_at, self.faxes.c.id).limit(limit)).mappings()]

    def usage_on(self, connection, agreement_id, since):
        """(pages, {currency: micros}) relayed under an agreement since ``since``; refused faxes do not count."""
        rows = connection.execute(sa.select(
            self.faxes.c.pages, self.faxes.c.cost_micros, self.faxes.c.cost_currency, self.faxes.c.charge_micros,
            self.faxes.c.charge_currency).where(
            self.faxes.c.agreement_id == agreement_id, self.faxes.c.created_at >= since,
            self.faxes.c.state.in_(COUNTED))).all()
        pages, money = 0, {}
        for row in rows:
            pages += row.pages or 0
            # The charge once known, else the estimate made when the fax was accepted.
            micros, currency = ((row.charge_micros, row.charge_currency) if row.charge_micros is not None
                                else (row.cost_micros, row.cost_currency))
            if micros is not None and currency:
                money[currency] = money.get(currency, 0) + int(micros)
        return pages, money

    def usage(self, agreement_id, since):
        with read_connection(self.engine) as connection:
            return self.usage_on(connection, agreement_id, since)

    def faxes_since(self, since, *, role):
        query = (sa.select(self.faxes, self.peers.c.organization).select_from(
            self.faxes.join(self.peers, self.peers.c.id == self.faxes.c.peer_id)).where(
            self.faxes.c.role == role, self.faxes.c.created_at >= since).order_by(self.faxes.c.created_at))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]
