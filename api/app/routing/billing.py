"""Reconcile provider charges after delivery has finished.

Delivery status stops changing at a final result, but a provider can price a
fax later or correct a price. This task keeps asking, records each charge once
per provider charge identity, and never touches the delivery itself. An
unknown price stays unknown; Faxbot's estimate is never copied into it.

Received faxes are read the same way (``ReceivedChargeReconciler``): Sinch and
Phaxio report what they charged for a fax they received, on the fax itself.
Each report is an append-only observation in ``provider_received_charges``
(migration 0051); a different amount later supersedes the one in effect with
both kept, and an older report is kept without taking effect. A received fax
is asked about until its charge settles (a price seen a day after the fax
arrived) or for a week, then left as never priced. The received fax itself is
never read, fetched again or changed here.
"""
from datetime import timedelta
import logging
from uuid import uuid4

import sqlalchemy as sa

from .database import read_connection, reflect, utcnow, write_transaction


class BillingReconciler:
    def __init__(self, routes, sources, *, settle_after=timedelta(hours=24), retry=timedelta(minutes=10),
                 give_up=timedelta(days=7)):
        self.routes, self.sources = routes, dict(sources)
        self.settle_after, self.retry, self.give_up = settle_after, retry, give_up

    def step(self, *, now=None):
        now = now or utcnow()
        if not self.sources:
            return False
        for row in self.routes.billing_due(set(self.sources), now=now, retry=self.retry, give_up=self.give_up):
            try:
                observations = self.sources[row['provider_id']](row)
            except Exception:
                logging.getLogger(__name__).warning('A provider charge could not be read; Faxbot will ask again later.')
                observations = None
            # A price seen a day after the call ended is treated as the settled amount.
            final = row['ended_at'] is not None and now - row['ended_at'] >= self.settle_after
            for charge in observations or ():
                self.routes.ingest_charge(row['id'], provider_id=row['provider_id'], charge_id=charge['charge_id'],
                                          amount_micros=charge['amount_micros'], currency=charge['currency'],
                                          billed_seconds=charge.get('billed_seconds'), final=final, now=now)
            self.routes.mark_billing_checked(row['id'], now=now)
        return False


# Received faxes ---------------------------------------------------------------------------------------------

FIRST_RETRY = timedelta(minutes=10)
LONGEST_RETRY = timedelta(hours=6)


class ReceivedChargeStore:
    TABLES = ('inbound_imports', 'provider_received_charges', 'provider_received_checks')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.imports = tables['inbound_imports']
        self.charges = tables['provider_received_charges']
        self.checks = tables['provider_received_checks']

    def due(self, providers, *, now=None, give_up=timedelta(days=7), limit=50):
        """Received faxes from these providers whose charge is still open, oldest first."""
        now = now or utcnow()
        imports, checks = self.imports, self.checks
        received = sa.func.coalesce(imports.c.source_received_at, imports.c.imported_at)
        query = (sa.select(imports.c.inbound_fax_id, imports.c.source, imports.c.account, imports.c.account_key,
                           imports.c.operation_id, received.label('received_at'), checks.c.state, checks.c.checks)
                 .select_from(imports.outerjoin(checks, checks.c.id == imports.c.inbound_fax_id))
                 .where(imports.c.source.in_(tuple(providers)), imports.c.state == 'received',
                        imports.c.revision == '', imports.c.imported_at >= now - give_up,
                        sa.or_(checks.c.id.is_(None),
                               sa.and_(checks.c.state == 'waiting', checks.c.next_check_at <= now)))
                 .order_by(imports.c.imported_at, imports.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def mark(self, row, state, *, now=None):
        now = now or utcnow()
        checks = self.checks
        with write_transaction(self.engine) as connection:
            current = connection.execute(sa.select(checks).where(
                checks.c.id == row['inbound_fax_id'])).mappings().one_or_none()
            count = (current['checks'] if current is not None else 0) + 1
            wait = min(FIRST_RETRY * (2 ** min(count - 1, 10)), LONGEST_RETRY)
            values = dict(provider_id=row['source'], account_key=row.get('account_key'), state=state, checks=count,
                          checked_at=now, next_check_at=now + wait, updated_at=now)
            if current is None:
                connection.execute(checks.insert().values(id=row['inbound_fax_id'], created_at=now, **values))
            else:
                connection.execute(checks.update().where(checks.c.id == row['inbound_fax_id']).values(**values))

    def expire(self, *, now=None, give_up=timedelta(days=7)):
        """Stop asking about received faxes the provider never priced within ``give_up``; returns how many."""
        now = now or utcnow()
        checks = self.checks
        with write_transaction(self.engine) as connection:
            return connection.execute(checks.update().where(
                checks.c.state == 'waiting', checks.c.created_at < now - give_up).values(
                state='unreported', updated_at=now)).rowcount or 0

    def ingest(self, row, charge, *, final, now=None):
        """Record one report; returns ``new``, ``duplicate``, ``corrected``, ``settled`` or ``older``."""
        amount, currency = charge['amount_micros'], charge['currency']
        if type(amount) is not int or not isinstance(currency, str) or len(currency) != 3:
            raise ValueError('Invalid provider charge.')
        now = now or utcnow()
        table = self.charges
        with write_transaction(self.engine) as connection:
            rows = connection.execute(sa.select(table).where(
                table.c.inbound_fax_id == row['inbound_fax_id'], table.c.charge_id == charge['charge_id'])
                .order_by(table.c.version)).mappings().all()
            current = next((item for item in reversed(rows) if item['applied']), None)
            values = dict(inbound_fax_id=row['inbound_fax_id'], provider_id=row['source'],
                          account_key=row.get('account_key'), charge_id=charge['charge_id'], version=len(rows) + 1,
                          amount_micros=amount, raw_amount=(charge.get('raw_amount') or '')[:32], currency=currency,
                          effective_at=now, observed_at=now, created_at=now)
            if current is not None and (current['amount_micros'], current['currency']) == (amount, currency):
                if final and not current['is_final']:
                    connection.execute(table.insert().values(id=uuid4().hex, supersedes_id=current['id'], applied=1,
                                                             is_final=1, **values))
                    return 'settled'
                return 'duplicate'
            connection.execute(table.insert().values(
                id=uuid4().hex, supersedes_id=current['id'] if current is not None else None, applied=1,
                is_final=int(final), **values))
            return 'new' if current is None else 'corrected'

    def in_effect(self, inbound_fax_ids, connection=None):
        """{received fax id: [the report in effect for each provider charge]}."""
        table = self.charges

        def read(conn):
            found = {}
            for row in conn.execute(sa.select(table).where(
                    table.c.inbound_fax_id.in_(tuple(inbound_fax_ids) or ('',)), table.c.applied == 1)
                    .order_by(table.c.inbound_fax_id, table.c.charge_id, table.c.version)).mappings():
                found.setdefault(row['inbound_fax_id'], {})[row['charge_id']] = dict(row)
            return {key: list(value.values()) for key, value in found.items()}
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def state(self, inbound_fax_id):
        checks = self.checks
        with read_connection(self.engine) as connection:
            return connection.execute(sa.select(checks.c.state).where(
                checks.c.id == inbound_fax_id)).scalar_one_or_none()

    def summary(self, since):
        """Per provider: received faxes charged, still waiting and never priced since ``since``, with the amounts."""
        imports, checks = self.imports, self.checks
        received = sa.func.coalesce(imports.c.source_received_at, imports.c.imported_at)
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(imports.c.inbound_fax_id, imports.c.source, checks.c.state)
                                      .select_from(imports.outerjoin(checks, checks.c.id == imports.c.inbound_fax_id))
                                      .where(imports.c.source.in_(RECEIVED_PROVIDERS), imports.c.state == 'received',
                                             imports.c.revision == '', received >= since)).all()
            effective = self.in_effect([row.inbound_fax_id for row in rows], connection)
        found = {}
        for row in rows:
            entry = found.setdefault(row.source, {'provider_id': row.source, 'faxes': 0, 'charged': 0, 'waiting': 0,
                                                  'never_priced': 0, 'amounts': {}})
            entry['faxes'] += 1
            reports = effective.get(row.inbound_fax_id) or []
            if reports:
                entry['charged'] += 1
                for report in reports:
                    entry['amounts'][report['currency']] = entry['amounts'].get(report['currency'], 0) + \
                        report['amount_micros']
            elif row.state == 'unreported':
                entry['never_priced'] += 1
            else:
                entry['waiting'] += 1
        return [found[key] for key in sorted(found)]


RECEIVED_PROVIDERS = ('sinch', 'phaxio')


class ReceivedChargeReconciler:
    """Ask Sinch and Phaxio what each received fax cost; never touches the fax."""

    def __init__(self, store, sources, *, settle_after=timedelta(hours=24), give_up=timedelta(days=7)):
        self.store, self.sources = store, dict(sources)
        self.settle_after, self.give_up = settle_after, give_up

    def step(self, *, now=None):
        now = now or utcnow()
        if not self.sources:
            return False
        self.store.expire(now=now, give_up=self.give_up)
        for row in self.store.due(set(self.sources), now=now, give_up=self.give_up):
            try:
                observations = self.sources[row['source']](row)
            except Exception:
                logging.getLogger(__name__).warning('A received fax charge could not be read; Faxbot will ask again '
                                                    'later.')
                observations = None
            final = row['received_at'] is not None and now - row['received_at'] >= self.settle_after
            for charge in observations or ():
                self.store.ingest(row, charge, final=final, now=now)
            # A price seen a day after the fax arrived is the settled amount; until then Faxbot asks again.
            self.store.mark(row, 'settled' if observations and final else 'waiting', now=now)
        return False
