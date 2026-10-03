"""Rate cards, destination profiles and the per-attempt route and cost ledger.

The ledger is evidence about attempts the delivery worker already owns. Nothing
here authorizes, repeats or cancels a send.
"""
from dataclasses import dataclass, replace
from datetime import timedelta
import re
from uuid import uuid4

import sqlalchemy as sa

from .costs import RateCard, attempt_cost, billed_seconds, estimate_cost
from .database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction
from .numbers import InvalidNumber, normalize_number
from .policy import REASONS, RouteStats


WINDOW_DAYS = 30
OUTCOMES = {'success': 'success', 'failed': 'failed', 'cancelled': 'cancelled', 'uncertain': 'uncertain'}
_ROUTE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')


class RoutingConflict(RuntimeError):
    """The record changed; reload before saving again."""


class RoutingInputError(ValueError):
    """Plain-sentence validation failure for an operator."""


def destination_key(value):
    """Normalized E.164 where possible; otherwise a bounded literal key."""
    try:
        return normalize_number(value)
    except InvalidNumber:
        text = (value or '').strip() if isinstance(value, str) else ''
        return text[:32] or 'unknown'


@dataclass(frozen=True)
class CaptureTarget:
    attempt_id: str
    job_id: str
    destination: str
    provider_id: str
    provider_sid: str | None
    phase: str
    pages: int | None
    submitted_at: object
    completed_at: object
    has_decision: bool


class RouteStore:
    TABLES = ('provider_rate_cards', 'delivery_destinations', 'delivery_attempt_costs', 'delivery_charges',
              'direct_peers', 'outbound_attempts', 'fax_jobs')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.cards = tables['provider_rate_cards']
        self.destinations = tables['delivery_destinations']
        self.costs = tables['delivery_attempt_costs']
        self.charges = tables['delivery_charges']
        self.peers = tables['direct_peers']
        self.attempts = tables['outbound_attempts']
        self.jobs = tables['fax_jobs']

    # Rate cards -----------------------------------------------------------
    @staticmethod
    def _card(row):
        return RateCard(row['id'], row['provider_id'], row['direction'], row['label'], row['currency'],
                        row['per_minute_micros'], row['per_page_micros'], row['per_call_micros'],
                        row['billing_increment_seconds'], row['minimum_seconds'], row['source_url'],
                        row['captured_on'])

    def current_cards(self, connection=None):
        def read(conn):
            rows = conn.execute(sa.select(self.cards).where(self.cards.c.superseded_at.is_(None))
                                .order_by(self.cards.c.direction, self.cards.c.provider_id)).mappings()
            return [self._card(row) for row in rows]
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def card_for(self, provider_id, direction='outbound', connection=None):
        return next((card for card in self.current_cards(connection)
                     if card.provider_id == provider_id and card.direction == direction), None)

    def replace_cards(self, cards):
        """Make ``cards`` the complete current set; changed cards get a new version."""
        keys = [(card.provider_id, card.direction) for card in cards]
        if len(set(keys)) != len(keys):
            raise RoutingInputError('Each provider can have one sending and one receiving rate card.')
        if len(cards) > 100:
            raise RoutingInputError('Keep at most 100 rate cards.')
        now = utcnow()
        with write_transaction(self.engine) as connection:
            current = {(card.provider_id, card.direction): card for card in self.current_cards(connection)}
            wanted = {}
            for card in cards:
                existing = current.get((card.provider_id, card.direction))
                if existing is not None and replace(existing, id=None) == replace(card, id=None):
                    wanted[(card.provider_id, card.direction)] = existing.id
                    continue
                identity = uuid4().hex
                connection.execute(self.cards.insert().values(
                    id=identity, provider_id=card.provider_id, direction=card.direction,
                    label=card.label.strip(), currency=card.currency,
                    per_minute_micros=card.per_minute_micros, per_page_micros=card.per_page_micros,
                    per_call_micros=card.per_call_micros,
                    billing_increment_seconds=card.billing_increment_seconds,
                    minimum_seconds=card.minimum_seconds, source_url=card.source_url,
                    captured_on=card.captured_on, created_at=now))
                wanted[(card.provider_id, card.direction)] = identity
            retired = [card.id for key, card in current.items() if wanted.get(key) != card.id]
            if retired:
                connection.execute(self.cards.update().where(self.cards.c.id.in_(retired)).values(superseded_at=now))
            return self.current_cards(connection)

    def seed_cards(self, cards):
        """Load starting rate cards once, only into an empty table; the table stays authoritative."""
        if not cards:
            return False
        with read_connection(self.engine) as connection:
            if connection.scalar(sa.select(sa.func.count()).select_from(self.cards)):
                return False
        unique = {}
        for card in cards:
            unique.setdefault((card.provider_id, card.direction), card)
        self.replace_cards(list(unique.values()))
        return bool(unique)

    # Destinations ---------------------------------------------------------
    def get_destination(self, number, connection=None):
        def read(conn):
            row = conn.execute(sa.select(self.destinations).where(
                self.destinations.c.phone_number == number)).mappings().one_or_none()
            return dict(row) if row is not None else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    def update_destination(self, number, *, expected_version=None, **changes):
        allowed = {'display_name', 'notes', 'preferred_route', 'accepts_references'}
        if set(changes) - allowed:
            raise RoutingInputError('Unknown destination setting.')
        if 'display_name' in changes and changes['display_name'] is not None:
            name = changes['display_name'].strip() if isinstance(changes['display_name'], str) else None
            if name is None or len(name) > 200:
                raise RoutingInputError('Names can be up to 200 characters.')
            changes['display_name'] = name or None
        if 'notes' in changes and changes['notes'] is not None:
            if not isinstance(changes['notes'], str) or len(changes['notes']) > 2000:
                raise RoutingInputError('Notes can be up to 2,000 characters.')
            changes['notes'] = changes['notes'].strip() or None
        if 'preferred_route' in changes and changes['preferred_route'] is not None:
            route = changes['preferred_route']
            if not isinstance(route, str) or _ROUTE.fullmatch(route) is None:
                raise RoutingInputError('Choose one of the available routes.')
        if 'accepts_references' in changes:
            if type(changes['accepts_references']) is not bool:
                raise RoutingInputError('Choose whether this recipient accepts references to earlier documents.')
            changes['accepts_references'] = int(changes['accepts_references'])
        now = utcnow()
        with write_transaction(self.engine) as connection:
            row = self.get_destination(number, connection)
            if row is None:
                if expected_version not in (None, 0):
                    raise RoutingConflict('This number changed; reload and try again.')
                values = {'display_name': None, 'notes': None, 'preferred_route': None, 'accepts_references': 0}
                values.update(changes)
                connection.execute(self.destinations.insert().values(
                    id=uuid4().hex, phone_number=number, direct_peer_id=None, version=1,
                    created_at=now, updated_at=now, **values))
            else:
                if expected_version is not None and expected_version != row['version']:
                    raise RoutingConflict('This number changed; reload and try again.')
                connection.execute(self.destinations.update().where(
                    self.destinations.c.id == row['id'], self.destinations.c.version == row['version']).values(
                        **changes, version=row['version'] + 1, updated_at=now))
            return self.get_destination(number, connection)

    def verified_peer(self, number, connection=None, *, now=None):
        """The verified, unexpired direct peer for a number, if exactly one exists."""
        def read(conn):
            moment = now or utcnow()
            rows = conn.execute(sa.select(self.peers).where(
                self.peers.c.phone_number == number, self.peers.c.state == 'verified',
                sa.or_(self.peers.c.expires_at.is_(None), self.peers.c.expires_at > moment))).mappings().all()
            return dict(rows[0]) if len(rows) == 1 else None
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    # Route decisions and costs ------------------------------------------------
    def record_decision(self, *, attempt_id, job_id, destination, route, reason, provider_id):
        """Record the chosen route before submission; a repeated call keeps the first record."""
        if reason not in REASONS or _ROUTE.fullmatch(route) is None or _ROUTE.fullmatch(provider_id) is None:
            raise ValueError('Invalid route decision.')
        now = utcnow()
        with write_transaction(self.engine) as connection:
            if connection.execute(sa.select(self.costs.c.id).where(self.costs.c.id == attempt_id)).first():
                return False
            connection.execute(self.costs.insert().values(
                id=attempt_id, job_id=job_id, destination=destination, route=route, route_reason=reason,
                provider_id=provider_id, outcome='pending', billing_checks=0, created_at=now, updated_at=now))
            return True

    def reroute_decision(self, attempt_id, *, route, provider_id, reason):
        """The attempt fell back before submission left Faxbot; record the route it used."""
        if reason not in REASONS or _ROUTE.fullmatch(route) is None or _ROUTE.fullmatch(provider_id) is None:
            raise ValueError('Invalid route decision.')
        with write_transaction(self.engine) as connection:
            connection.execute(self.costs.update().where(self.costs.c.id == attempt_id).values(
                route=route, provider_id=provider_id, route_reason=reason, updated_at=utcnow()))

    def decision(self, attempt_id):
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.costs).where(self.costs.c.id == attempt_id)).mappings().one_or_none()
            return dict(row) if row is not None else None

    def pending_captures(self, *, limit=100):
        a, j, c = self.attempts, self.jobs, self.costs
        finished = sa.or_(a.c.completed_at.is_not(None), a.c.phase == 'uncertain')
        stale = sa.or_(c.c.id.is_(None), c.c.outcome == 'pending',
                       sa.and_(c.c.outcome == 'uncertain', a.c.phase != 'uncertain'))
        query = (sa.select(a.c.id, a.c.job_id, a.c.phase, a.c.provider_sid, a.c.submitted_at, a.c.completed_at,
                           j.c.to_number, j.c.pages, j.c.backend, c.c.id.label('decision'),
                           c.c.provider_id.label('decided_provider'), c.c.provider_sid.label('decided_sid'))
                 .select_from(a.join(j, j.c.id == a.c.job_id).outerjoin(c, c.c.id == a.c.id))
                 .where(a.c.submitted_at.is_not(None), a.c.phase.in_(tuple(OUTCOMES)), finished, stale)
                 .order_by(a.c.created_at, a.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            rows = connection.execute(query).mappings().all()
        return [CaptureTarget(row['id'], row['job_id'], destination_key(row['to_number']),
                              row['decided_provider'] or row['backend'],
                              row['provider_sid'] or row['decided_sid'], row['phase'], row['pages'],
                              row['submitted_at'], row['completed_at'], row['decision'] is not None)
                for row in rows]

    def capture(self, target, *, observed_seconds=None, now=None):
        """Estimate one finished attempt from the provider's current rate card.

        ``observed_seconds`` is a measured connected duration (for example a SIP
        call record) and is preferred over Faxbot's submit-to-finish time, which
        includes queueing and ringing. Rounding applies to this call alone under
        the card's rule, never to an average. Provider-reported and settled
        amounts are separate observations.
        """
        now = now or utcnow()
        outcome = OUTCOMES[target.phase]
        card = self.card_for(target.provider_id)
        seconds = observed_seconds
        if seconds is None and target.completed_at is not None and target.submitted_at is not None:
            seconds = max(0, int((target.completed_at - target.submitted_at).total_seconds()))
        cost = basis = billed = None
        if card is not None:
            if outcome == 'uncertain':
                # The fax may have been sent; estimate what a successful send would cost.
                cost, basis = estimate_cost(card, target.pages), 'estimated'
            else:
                billed = billed_seconds(card, seconds)
                cost = attempt_cost(card, seconds=seconds, pages=target.pages, delivered=outcome == 'success')
                basis = 'measured' if observed_seconds is not None else 'estimated'
        values = dict(provider_sid=target.provider_sid, rate_card_id=card.id if card is not None else None,
                      started_at=target.submitted_at, ended_at=target.completed_at,
                      billed_seconds=billed, billed_pages=target.pages if outcome == 'success' else 0,
                      estimated_cost_micros=cost, currency=card.currency if card is not None else None,
                      cost_basis=basis, outcome=outcome, updated_at=now)
        with write_transaction(self.engine) as connection:
            exists = connection.execute(sa.select(self.costs.c.id).where(self.costs.c.id == target.attempt_id)).first()
            if exists:
                connection.execute(self.costs.update().where(self.costs.c.id == target.attempt_id).values(**values))
            else:
                connection.execute(self.costs.insert().values(
                    id=target.attempt_id, job_id=target.job_id, destination=target.destination,
                    route=target.provider_id, route_reason='configured', provider_id=target.provider_id,
                    billing_checks=0, created_at=now, **values))
        return values

    # Provider charges ---------------------------------------------------------
    def billing_due(self, providers, *, now=None, retry=timedelta(minutes=10), give_up=timedelta(days=7), limit=50):
        """Finished attempts whose provider charge is unknown or not yet settled."""
        now = now or utcnow()
        c = self.costs
        query = (sa.select(c).where(
            c.c.provider_id.in_(tuple(providers)), c.c.outcome.in_(('success', 'failed')),
            c.c.provider_sid.is_not(None), c.c.settled_at.is_(None), c.c.created_at >= now - give_up,
            sa.or_(c.c.billing_checked_at.is_(None), c.c.billing_checked_at <= now - retry))
            .order_by(sa.func.coalesce(c.c.billing_checked_at, c.c.created_at), c.c.id).limit(limit))
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(query).mappings()]

    def mark_billing_checked(self, attempt_id, *, now=None):
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            connection.execute(self.costs.update().where(self.costs.c.id == attempt_id).values(
                billing_checked_at=now, billing_checks=self.costs.c.billing_checks + 1))

    def ingest_charge(self, attempt_id, *, provider_id, charge_id, amount_micros, currency, billed_seconds=None,
                      final=False, now=None):
        """Record one provider charge; returns ``new``, ``duplicate``, ``corrected`` or ``pending``.

        Idempotent on the provider's charge identity. Only billing evidence
        changes; the delivery itself is never reopened or moved.
        """
        if (not isinstance(charge_id, str) or not 0 < len(charge_id) <= 100 or type(amount_micros) is not int
                or not isinstance(currency, str) or re.fullmatch(r'[A-Z]{3}', currency) is None
                or _ROUTE.fullmatch(provider_id) is None or type(final) is not bool):
            raise ValueError('Invalid provider charge.')
        now = now or utcnow()
        charges = self.charges
        with write_transaction(self.engine) as connection:
            if connection.execute(sa.select(self.costs.c.id).where(self.costs.c.id == attempt_id)).first() is None:
                return 'pending'
            row = connection.execute(sa.select(charges).where(
                charges.c.attempt_id == attempt_id, charges.c.charge_id == charge_id)).mappings().one_or_none()
            values = dict(amount_micros=amount_micros, currency=currency, billed_seconds=billed_seconds,
                          is_final=int(final))
            if row is None:
                connection.execute(charges.insert().values(id=uuid4().hex, attempt_id=attempt_id,
                    provider_id=provider_id, charge_id=charge_id, version=1, observed_at=now, updated_at=now,
                    **values))
                result = 'new'
            elif all(row[key] == value for key, value in values.items()):
                return 'duplicate'
            else:
                connection.execute(charges.update().where(charges.c.id == row['id']).values(
                    version=row['version'] + 1, updated_at=now, **values))
                result = 'corrected'
            observed = connection.execute(sa.select(charges).where(charges.c.attempt_id == attempt_id)
                                          .order_by(charges.c.observed_at, charges.c.id)).mappings().all()
            unit = observed[0]['currency']
            same = [charge for charge in observed if charge['currency'] == unit]
            total = sum(charge['amount_micros'] for charge in same)
            settled = all(charge['is_final'] for charge in observed) and len(same) == len(observed)
            connection.execute(self.costs.update().where(self.costs.c.id == attempt_id).values(
                reported_cost_micros=total, reported_currency=unit, reported_at=now,
                settled_cost_micros=total if settled else None, settled_at=now if settled else None,
                updated_at=now))
            return result

    # Evidence ---------------------------------------------------------------
    def route_stats(self, destination, *, now=None, connection=None):
        since = (now or utcnow()) - timedelta(days=WINDOW_DAYS)
        c = self.costs

        def read(conn):
            rows = conn.execute(sa.select(c.c.route, c.c.outcome, sa.func.count()).where(
                c.c.destination == destination, c.c.created_at >= since,
                c.c.outcome.in_(('success', 'failed'))).group_by(c.c.route, c.c.outcome)).all()
            totals = {}
            for route, outcome, count in rows:
                attempts, successes = totals.get(route, (0, 0))
                totals[route] = (attempts + count, successes + (count if outcome == 'success' else 0))
            return {route: RouteStats(attempts, successes) for route, (attempts, successes) in totals.items()}
        if connection is not None:
            return read(connection)
        with read_connection(self.engine) as conn:
            return read(conn)

    @staticmethod
    def _add(bucket, currency, micros):
        if currency and micros is not None:
            bucket[currency] = bucket.get(currency, 0) + int(micros)

    def destination_evidence(self, *, now=None, number=None):
        """Per destination and route: attempts, outcomes and 30-day estimated cost."""
        since = (now or utcnow()) - timedelta(days=WINDOW_DAYS)
        c = self.costs
        query = sa.select(c.c.destination, c.c.route, c.c.provider_id, c.c.outcome, c.c.currency,
                          c.c.estimated_cost_micros, c.c.reported_currency, c.c.reported_cost_micros,
                          c.c.created_at).where(c.c.created_at >= since, c.c.outcome != 'pending')
        if number is not None:
            query = query.where(c.c.destination == number)
        with read_connection(self.engine) as connection:
            rows = connection.execute(query).mappings().all()
            destinations = {row['phone_number']: dict(row) for row in connection.execute(
                sa.select(self.destinations) if number is None else
                sa.select(self.destinations).where(self.destinations.c.phone_number == number)).mappings()}
        evidence = {}
        for row in rows:
            routes = evidence.setdefault(row['destination'], {})
            entry = routes.setdefault(row['route'], {'route': row['route'], 'provider_id': row['provider_id'],
                'attempts': 0, 'successes': 0, 'failures': 0, 'uncertain': 0, 'cost_micros': {},
                'reported_cost_micros': {}, 'last_attempt_at': None})
            entry['attempts'] += 1
            entry['successes'] += row['outcome'] == 'success'
            entry['failures'] += row['outcome'] == 'failed'
            entry['uncertain'] += row['outcome'] == 'uncertain'
            self._add(entry['cost_micros'], row['currency'], row['estimated_cost_micros'])
            self._add(entry['reported_cost_micros'], row['reported_currency'], row['reported_cost_micros'])
            if entry['last_attempt_at'] is None or row['created_at'] > entry['last_attempt_at']:
                entry['last_attempt_at'] = row['created_at']
        return destinations, evidence

    def cost_totals(self, since):
        c = self.costs
        query = sa.select(c.c.provider_id, c.c.outcome, c.c.currency, c.c.estimated_cost_micros,
                          c.c.reported_currency, c.c.reported_cost_micros, c.c.settled_cost_micros,
                          c.c.billed_seconds, c.c.billed_pages).where(c.c.created_at >= since, c.c.outcome != 'pending')
        with read_connection(self.engine) as connection:
            rows = connection.execute(query).mappings().all()
        totals = {}
        for row in rows:
            entry = totals.setdefault(row['provider_id'], {'provider_id': row['provider_id'], 'attempts': 0,
                'successes': 0, 'failures': 0, 'uncertain': 0, 'billed_seconds': 0, 'billed_pages': 0,
                'cost_micros': {}, 'reported_cost_micros': {}, 'settled_cost_micros': {}, 'unreported': 0})
            entry['attempts'] += 1
            entry['successes'] += row['outcome'] == 'success'
            entry['failures'] += row['outcome'] == 'failed'
            entry['uncertain'] += row['outcome'] == 'uncertain'
            entry['billed_seconds'] += int(row['billed_seconds'] or 0)
            entry['billed_pages'] += int(row['billed_pages'] or 0)
            self._add(entry['cost_micros'], row['currency'], row['estimated_cost_micros'])
            self._add(entry['reported_cost_micros'], row['reported_currency'], row['reported_cost_micros'])
            self._add(entry['settled_cost_micros'], row['reported_currency'], row['settled_cost_micros'])
            entry['unreported'] += row['reported_cost_micros'] is None
        return sorted(totals.values(), key=lambda entry: entry['provider_id'])
