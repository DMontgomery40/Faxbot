"""Closed aggregate queries. No documents, identifiers, free text, or credentials leave here."""
from datetime import timedelta
import re
import sqlalchemy as sa
from ..routing.database import reflect, read_connection, utcnow

LABELS = {'delivery_outcomes': 'Delivery outcomes', 'spending': 'Attempt spending', 'calls_avoided': 'Calls avoided'}
TOOLS = [{'type': 'function', 'function': {'name': name, 'description': description,
          'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
         for name, description in (
             ('delivery_outcomes', 'Read counts of recorded outbound delivery attempts by outcome over the last 30 days.'),
             ('spending', 'Read separate outbound attempt estimated, reported and settled amounts by currency, and unknown-cost counts. Excludes subscriptions and unmatched carrier bills.'),
             ('calls_avoided', 'Read successful local and direct deliveries that avoided fax calls. These counts are not measured monetary savings.'))]


class OperationalEvidence:
    def __init__(self, engine, *, now=None):
        self.engine = engine
        self.now = now or utcnow()
        self.costs = reflect(engine, ('delivery_attempt_costs',))['delivery_attempt_costs']

    def read(self, name):
        if name not in LABELS:
            raise ValueError('Unsupported analysis tool.')
        table = self.costs
        window = sa.and_(table.c.created_at >= self.now - timedelta(days=30), table.c.created_at <= self.now)
        result = {'period_days': 30, 'through': self.now.isoformat() + 'Z', 'scope': 'Recorded outbound delivery attempts'}
        with read_connection(self.engine) as connection:
            if name == 'delivery_outcomes':
                counts = {}
                for outcome in ('pending', 'success', 'failed', 'uncertain', 'cancelled'):
                    counts[outcome] = connection.execute(sa.select(sa.func.count()).select_from(table).where(
                        window, table.c.outcome == outcome)).scalar_one()
                result.update(attempts=sum(counts.values()), outcomes=counts)
            elif name == 'calls_avoided':
                result['successful_deliveries'] = {provider: connection.execute(sa.select(sa.func.count()).select_from(table).where(
                    window, table.c.provider_id == provider, table.c.outcome == 'success')).scalar_one()
                    for provider in ('local', 'direct')}
                result['limitation'] = 'Counts of recorded delivery attempts, not measured monetary savings.'
            else:
                observations = {}
                for kind, amount_name, currency_name in (
                    ('estimated', 'estimated_cost_micros', 'currency'),
                    ('reported', 'reported_cost_micros', 'reported_currency'),
                    ('settled', 'settled_cost_micros', 'reported_currency')):
                    amount, currency = table.c[amount_name], table.c[currency_name]
                    rows = connection.execute(sa.select(currency, sa.func.count(), sa.func.sum(amount)).where(
                        window, amount.is_not(None)).group_by(currency).limit(200)).all()
                    observations[kind] = [{'currency': code, 'attempts': int(count), 'amount_micros': int(total)}
                                          for code, count, total in rows if isinstance(code, str) and re.fullmatch(r'[A-Z]{3}', code)]
                result['observations'] = observations
                result['unpriced_attempts'] = connection.execute(sa.select(sa.func.count()).select_from(table).where(
                    window, table.c.estimated_cost_micros.is_(None), table.c.reported_cost_micros.is_(None))).scalar_one()
                result['limitation'] = ('Estimated, reported and settled observations overlap; never add them together. '
                    'Unknown cost is not zero. Excludes subscriptions, inbound costs and unmatched carrier bills; this is not a total invoice.')
        return result
