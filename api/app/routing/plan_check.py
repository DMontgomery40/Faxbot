"""Is each monthly plan worth it at your traffic? Advice only: Faxbot never cancels or changes a plan.

A plan is any fax service whose rate card has a monthly fee (HumbleFax's
unlimited plan today; an eFax plan saved as a rate card; a SIP trunk priced as
a fixed monthly cost). For the last ``WINDOW_DAYS`` days and the ones before,
each plan gets the faxes it carried (delivered sends by its route, faxes
received through it), its fee for the days Faxbot has records for, the fee per
fax, and what the same faxes would have cost another way:

- a sent fax costs what the cheapest other route that is reliable for its number
  cost per delivered fax there (``delivered.py``, failed calls included), or
  else that route's rate-card estimate; a flat plan is never the other way,
  so it is never a $0 "cheapest";
- a received fax costs the carrier trunk's received-call estimate, plus the
  carrier's published rental for the number once it is moved there;
- a fax to one of your own numbers is a test (``own_numbers.account_numbers``).
  A test to a number that receives into this Faxbot (delivered inside Faxbot
  with no call) or to the plan's own number (it goes with the plan, or moves to
  the carrier and then receives here) needs no other way; a test to any other
  number of yours, such as another provider's number, still places a paid call
  and is priced like any other fax.

The advice is "keep" (the plan is cheaper, or some of what it carries has no
reliable other way), "review" (the other way would have cost less; the
administrator decides) or "too little history". Money is integer micros and
every figure is an estimate.
"""
from datetime import timedelta
import json

import sqlalchemy as sa

from .carriers import carrier_label
from .costs import estimate_cost, format_amount
from .database import read_connection, reflect, utcnow
from .delivered import WINDOW_DAYS, short_money_text
from .delivered_store import DeliveredEvidence
from .numbers import stored_number
from .own_numbers import PROVIDER_NUMBERS, account_numbers, receiving_numbers
from .plan import MIN_ATTEMPTS, extra_routes, route_label
from .policy import DIRECT, RoutePolicy
from .receiving import carrier_prices, prorate
from .seed import default_path


DAY = 86_400
# With fewer days of records than the window, a plan needs at least this many faxes to be judged.
MIN_FAXES = 3


def _money(micros, currency):
    return [] if micros is None or currency is None else [{'currency': currency, 'amount': format_amount(micros)}]


def _about(micros, currency):
    return short_money_text(micros, currency)


def _fee_text(micros, currency):
    whole = micros % 1_000_000 == 0
    return (f'${micros // 1_000_000}' if whole else short_money_text(micros, currency)) if currency == 'USD' \
        else short_money_text(micros, currency)


def _faxes(count):
    return f"{count} {'fax' if count == 1 else 'faxes'}"


def _days(days):
    return 'less than a day' if days < 1 else f"{days} {'day' if days == 1 else 'days'}"


def shipped_numbers_included(path=None):
    """{provider: True} for shipped plans whose provider says the plan includes its own fax number."""
    try:
        document = json.loads((path or default_path()).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    plans = document.get('plans') if isinstance(document, dict) else None
    return {plan['provider_id']: True for plan in plans or []
            if isinstance(plan, dict) and plan.get('includes_number') is True and plan.get('provider_id')}


def _route_key(provider_id):
    return 'sip' if provider_id == 'sip' or provider_id.startswith('sip-') else provider_id


def _plan_numbers(values, key, country, accounts):
    """The plan's own numbers: the configured one, then those the provider's account reports."""
    if key == 'sip':
        found = [*getattr(values, 'sip_trunk_did_list', ()), getattr(values, 'sip_trunk_caller_id', '')]
    else:
        found = [getattr(values, field, '') or '' for field in PROVIDER_NUMBERS.get(key, ())]
    found += list(accounts.get(key, ()))
    return list(dict.fromkeys(stored_number(number, country=country) for number in found if number))


def known_account_numbers(values):
    """{provider: numbers} each provider's account reports as its own, from Faxbot's cache.

    HumbleFax's come from its GetUser answer, the same cached read the provider
    settings show (``humblefax_service.account_numbers``); never read while sending.
    """
    found = {}
    access, secret = getattr(values, 'humblefax_access_key', ''), getattr(values, 'humblefax_secret_key', '')
    if access and secret:
        from ..humblefax_service import account_numbers
        found['humblefax'] = tuple(account_numbers(access, secret) or ())
    return found


class PlanCheck:
    def __init__(self, routes, values, *, now=None, days=WINDOW_DAYS, bound=None, path=None, accounts=None):
        """``accounts``: {provider: numbers} each provider's account reports as its own (HumbleFax's account numbers)."""
        self.routes, self.values, self.days, self.path = routes, values, days, path
        self.accounts = dict(accounts or {})
        self.now = (now or utcnow()).replace(microsecond=0)
        self.country = getattr(values, 'fax_default_country', 'US') or 'US'
        self.preset = (getattr(values, 'sip_trunk_preset', '') or '').strip()
        tables = reflect(routes.engine, ('inbound_faxes', 'sip_call_records'))
        self.faxes, self.calls = tables['inbound_faxes'], tables['sip_call_records']
        self.policy = RoutePolicy(min_success_percent=getattr(values, 'route_min_success_percent', 80),
                                  min_attempts=MIN_ATTEMPTS)
        self.bound = bound or getattr(values, 'effective_outbound', '') or ''
        self.extras = extra_routes(values, self.bound) if self.bound else []
        # Tests: faxes to any number of yours. Delivered inside Faxbot with no call when the number receives here.
        self.own = account_numbers(values, self.accounts)
        self.free = receiving_numbers(values) if getattr(values, 'local_delivery_enabled', True) else set()
        self._evidence = None
        self._stats = {}

    def label(self, route):
        """A route's plain name; the trunk is named after its carrier ("Telnyx")."""
        if route == 'sip' and self.preset:
            return carrier_label(self.preset)
        return route_label(route)

    # Records ------------------------------------------------------------------------
    def first_record(self):
        """When the installation's records start: its first attempt, received fax or trunk call."""
        c = self.routes.costs
        with read_connection(self.routes.engine) as connection:
            found = [connection.scalar(sa.select(sa.func.min(c.c.created_at))),
                     connection.scalar(sa.select(sa.func.min(sa.func.coalesce(self.faxes.c.received_at,
                                                                               self.faxes.c.created_at)))),
                     connection.scalar(sa.select(sa.func.min(self.calls.c.started_at)))]
        found = [moment for moment in found if moment is not None and moment < self.now]
        return min(found) if found else None

    def sent(self, key, start, end):
        """[(destination, pages)] for faxes the route ``key`` delivered between ``start`` and ``end``."""
        c, j = self.routes.costs, self.routes.jobs
        with read_connection(self.routes.engine) as connection:
            rows = connection.execute(sa.select(c.c.destination, j.c.pages).select_from(
                c.outerjoin(j, j.c.id == c.c.job_id)).where(
                c.c.route == key, c.c.outcome == 'success', c.c.created_at >= start, c.c.created_at < end)).all()
        return [(row.destination, row.pages) for row in rows]

    def received(self, key, start, end):
        """[(to number, pages)] for faxes received through ``key`` between ``start`` and ``end``."""
        when = sa.func.coalesce(self.faxes.c.received_at, self.faxes.c.created_at)
        with read_connection(self.routes.engine) as connection:
            rows = connection.execute(sa.select(self.faxes.c.to_number, self.faxes.c.pages).where(
                self.faxes.c.backend == key, when >= start, when < end)).all()
        return [(stored_number(row.to_number or '', country=self.country), row.pages) for row in rows]

    # The other way ---------------------------------------------------------------------
    def evidence(self):
        if self._evidence is None:
            self._evidence = DeliveredEvidence(self.routes).by_destination(now=self.now, timing=False)
        return self._evidence

    def alternative(self, key, destination, pages, currency):
        """``(micros, route, estimated)`` for one fax to ``destination`` another way, or ``(None, reason, None)``.

        ``reason`` is ``none`` (no other route set up), ``unreliable`` or ``unpriced``.
        """
        options = [route for route in [self.bound, *self.extras] if route and route not in (key, DIRECT)]
        if not options:
            return None, 'none', None
        if destination not in self._stats:
            self._stats[destination] = self.routes.route_stats(destination, now=self.now)
        stats = self._stats[destination]
        figures = self.evidence().get(destination, {})
        priced, unreliable = [], 0
        for route in options:
            card = self.routes.card_for(route)
            if card is not None and card.flat_plan:
                continue  # another plan is never the cheaper way: its faxes are "included", not free
            if self.policy.unreliable(stats.get(route)):
                unreliable += 1
                continue
            figure = figures.get(route)
            if figure is not None and figure.comparable() and figure.currency == currency:
                priced.append((figure.per_delivered_micros, self.label(route), route, figure.estimate))
            elif card is not None and card.currency == currency:
                priced.append((estimate_cost(card, pages), self.label(route), route, True))
        if priced:
            micros, _, route, estimated = min(priced)
            return micros, route, estimated
        return None, ('unreliable' if unreliable else 'unpriced'), None

    # One plan ----------------------------------------------------------------------------
    def window(self, key, card, start, end, first_at):
        covered_from = max(start, first_at) if first_at is not None else end
        seconds = max(0, int((end - covered_from).total_seconds()))
        sent = self.sent(key, start, end)
        received = self.received(key, start, end)
        currency = card.currency
        result = {'start': start, 'end': end, 'days': seconds // DAY, 'seconds': seconds, 'sent': len(sent),
                  'received': len(received), 'own_numbers': 0, 'paid_tests': 0, 'paid_numbers': set(),
                  'fee': prorate(card.monthly_fee_micros, seconds), 'alternative': 0, 'estimated': False,
                  'routes': set(), 'blocked': {}}
        # Tests needing no other way: to a number that receives here (delivered inside Faxbot with no call), or to
        # the plan's own number, which goes with the plan or moves to the carrier and then receives here.
        free = self.free | set(_plan_numbers(self.values, key, self.country, self.accounts))
        for destination, pages in sent:
            if destination in self.own:
                result['own_numbers'] += 1
                if destination in free:
                    continue
                # Any other number of yours (another provider's) still places a paid call: priced like any fax.
                result['paid_tests'] += 1
                result['paid_numbers'].add(destination)
            micros, route, estimated = self.alternative(key, destination, pages, currency)
            if micros is None:
                result['blocked'][route] = result['blocked'].get(route, 0) + 1
                continue
            result['alternative'] += micros
            result['estimated'] |= bool(estimated)
            result['routes'].add(route)
        # A received fax arrives on the carrier trunk once the plan's number is moved there.
        inbound = self.routes.card_for('sip', 'inbound') if self.preset and key != 'sip' else None
        for number, pages in received:
            if inbound is None or inbound.flat_plan or inbound.currency != currency:
                result['blocked']['receive'] = result['blocked'].get('receive', 0) + 1
                continue
            result['alternative'] += estimate_cost(inbound, pages)
            result['estimated'] = True
            result['routes'].add('sip')
        return result

    def plan(self, key, card, first_at, suggested):
        name, currency, fee = self.label(key), card.currency, card.monthly_fee_micros
        windows = [self.window(key, card, self.now - timedelta(days=self.days), self.now, first_at),
                   self.window(key, card, self.now - timedelta(days=2 * self.days),
                               self.now - timedelta(days=self.days), first_at)]
        latest = windows[0]
        carried = latest['sent'] + latest['received']
        full = latest['seconds'] >= self.days * DAY - DAY  # a day's grace for the first day's first record
        numbers = _plan_numbers(self.values, key, self.country, self.accounts)
        includes_number = bool(numbers or shipped_numbers_included(self.path).get(key) or latest['received'])
        carrier = self.label('sip') if self.preset else None
        renting = bool(includes_number and carrier and key != 'sip')
        monthly = carrier_prices(self.preset).rental.get('local') if renting else None
        for view in windows:
            # Keeping the plan's number means renting it from the carrier instead. When the carrier publishes
            # no price for that, the rent is unknown (None), never $0.
            view['rental'] = (None if monthly is None else prorate(monthly, view['seconds'])) if renting else 0
        rental = latest['rental']
        # The other way's full cost; unknown with the rent. Without the rent, the faxes alone still decide "keep".
        total = None if rental is None else latest['alternative'] + rental
        period = (f'in the last {self.days} days' if full
                  else f"over the {_days(latest['days'])} Faxbot has records for")
        other = ' and '.join(sorted(self.label(route) for route in latest['routes'])) or carrier or 'another way'
        if latest['alternative'] and rental:
            other_cost = (f"{other} would have cost about {_about(latest['alternative'], currency)} for the same faxes "
                          f"and {_about(rental, currency)} to keep the number")
        elif rental:
            other_cost = f"keeping the number with {carrier} would have cost about {_about(rental, currency)}"
        else:
            other_cost = f"{other} would have cost about {_about(latest['alternative'], currency)} for the same faxes"
        caveats = self._caveats(key, name, latest, numbers, includes_number, carrier, suggested, fee, currency)
        action = None
        if latest['seconds'] < DAY or (not full and carried < MIN_FAXES):
            state = 'too_little_history'
            sentence = (f"Not enough history yet to judge your {_fee_text(fee, currency)} {name} plan: Faxbot has "
                        f"records for {_days(latest['days'])}, and {name} carried {_faxes(carried)} in that time.")
        elif latest['blocked']:
            state = 'keep'
            sentence = self._blocked_sentence(name, latest['blocked'], period)
        elif latest['fee'] <= (latest['alternative'] if total is None else total):
            # An unknown rent can only add to the other way, so a plan cheaper than the faxes alone is kept.
            state = 'keep'
            per = (f", about {_about(-(-latest['fee'] // carried), currency)} a fax" if carried else '')
            sentence = (f"Keep it: {name} carried {_faxes(carried)} {period} for its "
                        f"{_fee_text(fee, currency)} monthly fee{per}, while {other_cost} (estimate).")
        else:
            state = 'review'
            if carried:
                head = (f"{name} carried {_faxes(carried)} {period}, about "
                        f"{_about(-(-latest['fee'] // carried), currency)} each")
            else:
                head = f'{name} carried no faxes {period}'
            fee_part = ('its ' if full else 'that part of its ') + f'{_fee_text(fee, currency)} monthly fee'
            if total is None:
                # No saving is stated: the number's rent at the carrier is unknown.
                sentence = (f"Worth reviewing: {head} for {fee_part}; {other_cost}, but {carrier} does not publish "
                            f"what it charges to keep your {name} number, so Faxbot can't tell whether dropping the "
                            "plan would save money (estimate).")
            else:
                sentence = (f"Worth reviewing: {head} for {fee_part}; {other_cost}, "
                            f"{_about(latest['fee'] - total, currency)} less (estimate).")
            # Faxes priced another way (not only received ones on the trunk) need that way chosen in Recipients;
            # tests delivered inside Faxbot need none, but paid tests do.
            sends_elsewhere = latest['sent'] - latest['own_numbers'] + latest['paid_tests'] > 0 and latest['routes']
            action = (f'If you decide to drop the plan, fax these numbers with {other} instead, then cancel the plan in '
                      f'your {name} account. Faxbot never cancels anything for you.') if sends_elsewhere else (
                f'If you decide to drop the plan, cancel it in your {name} account. Faxbot never cancels anything '
                'for you.')
        return {'route': key, 'name': name, 'monthly_fee': _money(fee, currency), 'state': state,
                'sentence': sentence, 'action': action, 'caveats': caveats, 'estimate': True,
                'windows': [self._window_view(view, currency) for view in windows]}

    @staticmethod
    def _blocked_sentence(name, blocked, period):
        if blocked.get('receive'):
            count = blocked['receive']
            return (f"Keep it: {name} received {_faxes(count)} {period}, and no other fax service you have can "
                    'receive them.')
        if blocked.get('unreliable'):
            count = blocked['unreliable']
            return f"Keep it: no other way of faxing works reliably for {count} of the faxes {name} sent {period}."
        if blocked.get('unpriced'):
            return ('Keep it for now: enter the prices of your other fax services in Costs → Prices & plans so Faxbot '
                    'can compare this plan.')
        count = sum(blocked.values())
        return f"Keep it: no other way of faxing is set up for {count} of the faxes {name} sent {period}."

    def _caveats(self, key, name, latest, numbers, includes_number, carrier, suggested, fee, currency):
        caveats = []
        if includes_number and key != 'sip':
            target = carrier or 'another carrier'
            # Only faxes received through the plan count: Faxbot's own sends to the number are tests, not callers.
            received = latest['received']
            number = (numbers[0] if len(numbers) == 1 else ', '.join(numbers)) if numbers else None
            if number and received:
                caveats.append(f'Your {name} plan includes the fax number {number}, which received '
                               f'{_faxes(received)} in the last {self.days} days. Move it to {target} before you '
                               'cancel, or faxes sent to it will stop arriving.')
            elif number:
                caveats.append(f'Before you cancel, move your {name} number, {number}, to {target} if anyone still '
                               'faxes it.')
            else:
                caveats.append(f'Before you cancel, move your {name} number to {target} if anyone still faxes it.')
        if latest['own_numbers']:
            tests = (f"{latest['own_numbers']} of these faxes " +
                     ('was a test' if latest['own_numbers'] == 1 else 'were tests') + ' to your own numbers.')
            paid, where = latest['paid_tests'], ', '.join(sorted(latest['paid_numbers']))
            # A test to a number that does not receive into Faxbot still places a paid call: counted as a cost.
            if paid == 1:
                tests += (f' The one sent to {where} still costs a phone call, so it is counted in what the other way '
                          'would cost.')
            elif paid:
                tests += (f' The {paid} sent to {where} still cost a phone call, so they are counted in what the other '
                          'way would cost.')
            caveats.append(tests)
        if key in suggested:
            caveats.append(f'Faxes through the plan cost nothing extra while you pay for it; the question here is '
                           f'whether the {_fee_text(fee, currency)} monthly fee is worth paying.')
        return caveats

    def _window_view(self, view, currency):
        carried = view['sent'] + view['received']
        return {'start': view['start'], 'end': view['end'], 'days': view['days'], 'sent': view['sent'],
                'received': view['received'], 'own_numbers': view['own_numbers'],
                # Of own_numbers, tests that still place a paid call (to a number that does not receive here).
                'paid_tests': view['paid_tests'],
                'fee': _money(view['fee'], currency) if view['seconds'] else [],
                'fee_per_fax': _money(-(-view['fee'] // carried), currency) if carried and view['seconds'] else [],
                # The same faxes another way, and the carrier's rental for the plan's number once moved there.
                'other_way': _money(view['alternative'], currency) if view['seconds'] and not view['blocked'] else [],
                'number_rental': _money(view['rental'], currency) if view['seconds'] and view['rental'] else [],
                # True when the carrier publishes no price for keeping the plan's number: unknown, not free.
                'number_rental_unpublished': view['rental'] is None,
                'other_routes': sorted(self.label(route) for route in view['routes']),
                'without_other_way': sum(view['blocked'].values())}

    # All plans ------------------------------------------------------------------------------
    def plans(self):
        """``{route key: rate card}`` for every plan in use or that carried faxes."""
        in_use = {self.bound, *self.extras}
        if getattr(self.values, 'inbound_enabled', False):
            in_use.add(getattr(self.values, 'effective_inbound', '') or '')
        c = self.routes.costs
        since = self.now - timedelta(days=2 * self.days)
        with read_connection(self.routes.engine) as connection:
            in_use |= set(connection.execute(sa.select(c.c.route).where(c.c.created_at >= since).distinct()).scalars())
            in_use |= set(connection.execute(sa.select(self.faxes.c.backend).where(
                self.faxes.c.created_at >= since).distinct()).scalars())
        found = {}
        for key in sorted(route for route in in_use if route and route != DIRECT):
            for direction in ('outbound', 'inbound'):
                card = self.routes.card_for(_route_key(key), direction)
                if card is not None and card.monthly_fee_micros:
                    found.setdefault(_route_key(key), card)
        return found

    def report(self, suggested=()):
        first_at = self.first_record()
        plans = [self.plan(key, card, first_at, set(suggested)) for key, card in sorted(self.plans().items())]
        return {'days': self.days, 'estimate': True, 'plans': plans,
                'empty_sentence': (None if plans else
                                   'You pay no monthly fee for a fax service, so there is no plan to review.')}


def plan_report(routes, values, *, now=None, days=WINDOW_DAYS, bound=None, suggested=(), path=None, accounts=None):
    """``bound`` is the outbound provider of the active configuration, when known; else the configured one.

    ``accounts`` maps a provider to the numbers its account reports (default: Faxbot's cached reads).
    """
    accounts = known_account_numbers(values) if accounts is None else accounts
    return PlanCheck(routes, values, now=now, days=days, bound=bound, path=path, accounts=accounts).report(suggested)


# The contract view (B11) ----------------------------------------------------------------------------
#
# Per plan, for the current billing period: the budget or allowance used and left, the overage so far, what
# is committed (the plan fee and any monthly commitment), the bill so far, a day-by-day burn-down, and whose
# bill a fax falls on when both sides are your own accounts. Every figure is an estimate.

def _pages(count):
    return f"{count} {'page' if count == 1 else 'pages'}"


def _owners(values, accounts, country):
    """{number: route} for the numbers each of your accounts holds (the trunk's as ``sip``)."""
    owners = {}
    for key in ('sip', *PROVIDER_NUMBERS):
        for number in _plan_numbers(values, key, country, accounts):
            owners.setdefault(number, key)
    return owners


def _sum_money(rows, currency):
    """(micros, how many rows had no known cost in ``currency``) for reported-else-estimated costs."""
    total, unknown = 0, 0
    for row in rows:
        if row.reported_cost_micros is not None and row.reported_currency == currency:
            total += int(row.reported_cost_micros)
        elif row.estimated_cost_micros is not None and row.currency == currency:
            total += int(row.estimated_cost_micros)
        else:
            unknown += 1
    return total, unknown


class ContractView:
    def __init__(self, routes, values, *, now=None, path=None, accounts=None):
        self.routes, self.values, self.path = routes, values, path
        self.now = (now or utcnow()).replace(microsecond=0)
        self.accounts = dict(accounts or {})
        self.country = getattr(values, 'fax_default_country', 'US') or 'US'
        self.zone = getattr(values, 'time_zone', '') or ''
        self.check = PlanCheck(routes, values, now=self.now, path=path, accounts=self.accounts)
        self.owners = _owners(values, self.accounts, self.country)
        self.here = receiving_numbers(values) if getattr(values, 'local_delivery_enabled', True) else set()

    def plan_routes(self):
        """Plans in use or that carried faxes, and every route you gave a budget, allowance or commitment."""
        from .plan_budget import InvalidBudget, parse_budgets
        found = set(self.check.plans())
        try:
            found |= set(parse_budgets(getattr(self.values, 'plan_budgets', '') or ''))
        except InvalidBudget:
            pass
        return sorted(found)

    def _attempts(self, start, end, *, route=None, other_than=None, destinations=None):
        c, j = self.routes.costs, self.routes.jobs
        query = sa.select(c.c.route, c.c.destination, j.c.pages, c.c.billed_pages, c.c.reported_cost_micros,
                          c.c.reported_currency, c.c.estimated_cost_micros, c.c.currency).select_from(
            c.outerjoin(j, j.c.id == c.c.job_id)).where(
            c.c.outcome == 'success', c.c.created_at >= start, c.c.created_at < end)
        if route is not None:
            query = query.where(c.c.route == route)
        if other_than is not None:
            query = query.where(c.c.route != other_than, c.c.route != DIRECT, c.c.route != 'local')
        if destinations is not None:
            if not destinations:
                return []
            query = query.where(c.c.destination.in_(sorted(destinations)))
        with read_connection(self.routes.engine) as connection:
            return connection.execute(query).all()

    def own_accounts(self, key, budget, start, end):
        """Faxes between two of your own accounts this period, and whose bill each side falls on."""
        name = self.check.label(key)
        mine = set(_plan_numbers(self.values, key, self.country, self.accounts))
        rows = []
        # Sent by this plan to a number of yours held elsewhere, or to one that receives into this Faxbot.
        groups = {}
        for row in self._attempts(start, end, route=key):
            if row.destination not in self.owners and row.destination not in self.here:
                continue
            owner = self.owners.get(row.destination) or 'local'
            if owner == key:
                continue  # a fax to the plan's own number is a test on one account, not two
            groups.setdefault(owner, []).append(row)
        uses = 'your allowance' if budget is not None and budget.included_pages else 'your plan'
        for owner, items in sorted(groups.items()):
            pages = sum(max(1, int(row.pages or row.billed_pages or 0)) for row in items)
            them = 'it' if len(items) == 1 else 'them'
            if owner == 'local':
                other, receive = 'this Faxbot', 'nothing was charged for receiving'
                where = 'own number that receives into this Faxbot'
            else:
                other = self.check.label(owner)
                receive, where = f'receiving {them} is on your {other} bill', f'{other} number'
            rows.append({'direction': 'sent', 'other': other, 'faxes': len(items), 'pages': pages,
                         'sending_bill': name, 'receiving_bill': None if owner == 'local' else other,
                         'sending_cost': [],
                         'sentence': f'{_faxes(len(items))} from {name} went to your {where}: sending {them} used '
                                     f'{uses} with {name}, and {receive}.'})
        # Sent to this plan's number by another of your routes.
        incoming = {}
        for row in self._attempts(start, end, other_than=key, destinations=mine):
            incoming.setdefault(row.route, []).append(row)
        for route, items in sorted(incoming.items()):
            other = self.check.label(route)
            card = self.routes.card_for(_route_key(route))
            currency = card.currency if card is not None else (budget.currency if budget else 'USD')
            micros, unknown = _sum_money(items, currency)
            pages = sum(max(1, int(row.pages or row.billed_pages or 0)) for row in items)
            them = 'it' if len(items) == 1 else 'them'
            flat = card is not None and card.flat_plan
            if flat:
                charge = f'sending {them} used your {other} plan'
            elif unknown:
                charge = f'{other} billed the calls, at a cost Faxbot does not know yet'
            else:
                charge = f'{other} billed the calls (about {_about(micros, currency)}, estimate)'
            if budget is not None and budget.included_pages:
                receive = f'their {_pages(pages)} count against your {name} allowance'
            else:
                receive = f'{name} received {them} inside your plan'
            rows.append({'direction': 'received', 'other': other, 'faxes': len(items), 'pages': pages,
                         'sending_bill': other, 'receiving_bill': name,
                         'sending_cost': [] if unknown or flat else _money(micros, currency),
                         'sentence': f'{_faxes(len(items))} from {other} came to your {name} number: {charge}, and '
                                     f'{receive}.'})
        return rows

    def plan(self, key):
        from .plan_budget import (budget_left, burn_down, day_text, metered, pace_sentence, shipped_budgets,
                                  untimed_sentence)
        left = budget_left(key, self.now, engine=self.routes.engine, values=self.values, path=self.path)
        if left is None:
            return None
        budget, used, period = left.budget, left.used, left.period
        currency = budget.currency
        fee = budget.monthly_fee_micros or 0
        overage = left.overage_micros if budget.included_pages else 0
        minutes_extra = left.overage_minute_micros if budget.included_minutes else 0
        committed = fee + (budget.commitment_micros or 0)
        above = 0
        if budget.commitment_micros is not None and used.spend_micros is not None:
            above = max(0, used.spend_micros - budget.commitment_micros)
        bill = None if overage is None else committed + overage + minutes_extra + above
        rows = burn_down(self.routes.engine, left, now=self.now, zone_name=self.zone)
        money = lambda micros: _money(micros, currency) if micros is not None else []  # noqa: E731
        return {'route': key, 'name': budget.label, 'currency': currency, 'estimate': True,
                'monthly_fee': money(fee or None),
                'kind': ('allowance' if budget.included_pages else 'minutes' if budget.included_minutes
                         else 'commitment' if budget.commitment_micros is not None and not budget.flat
                         else 'metered' if metered(budget) else 'flat'),
                'budget': {'pages': budget.pages, 'faxes': budget.faxes, 'day': budget.day,
                           'included_pages': budget.included_pages, 'page_overage': money(budget.page_overage_micros),
                           'included_minutes': budget.included_minutes,
                           'per_minute': money(budget.per_minute_micros if budget.included_minutes else None),
                           'commitment': money(budget.commitment_micros), 'source': budget.source,
                           'sentence': budget.sentence},
                'period': {'start': period.start, 'end': period.end, 'first_day': period.first_day.isoformat(),
                           'next_day': period.next_day.isoformat(), 'next_day_text': day_text(period.next_day)},
                'used': {'sent_faxes': used.sent_faxes, 'sent_pages': used.sent_pages,
                         'received_faxes': used.received_faxes, 'received_pages': used.received_pages,
                         'faxes': used.faxes, 'pages': used.pages, 'minutes': used.minutes,
                         'spend': money(used.spend_micros), 'not_priced': used.unpriced,
                         # Faxes counted by pages alone: the plan also counts time on the line, which was not known.
                         'counted_by_pages_only': used.untimed},
                'left': {'pages': left.pages_left, 'faxes': left.faxes_left, 'allowance': left.allowance_left,
                         'minutes': left.minutes_left, 'commitment': money(left.commitment_left_micros)},
                'overage': {'pages': left.overage_pages, 'minutes': left.overage_minutes,
                            'cost': [] if overage is None else _money(overage + minutes_extra, currency),
                            'cost_unknown': overage is None},
                'committed': money(committed or None),
                'bill_so_far': [] if bill is None or not bill else _money(bill, currency),
                'bill_sentence': self._bill_sentence(budget, overage, minutes_extra + above, used),
                'state': left.state, 'over': left.over, 'sentence': left.sentence,
                'pace_sentence': pace_sentence(left, rows),
                'count_sentence': (shipped_budgets(self.path).get(key) or {}).get('count_sentence'),
                'untimed_sentence': untimed_sentence(left),
                'burn_down': [{'date': row.day.isoformat(), 'pages': row.pages, 'faxes': row.faxes} for row in rows],
                'own_accounts': self.own_accounts(key, budget, period.start,
                                                  min(period.end, self.now + timedelta(seconds=1)))}

    @staticmethod
    def _bill_sentence(budget, overage, extra, used):
        """What is committed this period and anything past it, in one sentence; None with nothing committed."""
        currency, name = budget.currency, budget.label
        parts = []
        if budget.monthly_fee_micros:
            parts.append(f'the {_fee_text(budget.monthly_fee_micros, currency)} plan fee')
        if budget.commitment_micros is not None:
            parts.append(f'your {_fee_text(budget.commitment_micros, currency)} monthly commitment')
        if not parts:
            return None
        head = f"Committed this period with {name}: {' and '.join(parts)}"
        if overage is None:
            return head + ', plus extra pages at a price Faxbot does not know.'
        if overage + extra:
            return head + f', plus about {_about(overage + extra, currency)} past it so far (estimate).'
        if budget.commitment_micros is not None and used.spend_micros is not None:
            return head + f'; about {_about(used.spend_micros, currency)} of the commitment used so far (estimate).'
        return head + '; nothing past it so far.'

    def report(self):
        plans = [view for view in (self.plan(key) for key in self.plan_routes()) if view is not None]
        return {'plans': plans, 'estimate': True, 'plan_budgets': getattr(self.values, 'plan_budgets', '') or '',
                'empty_sentence': None if plans else (
                    'You pay no monthly fee for a fax service and set no allowance or commitment, so there is no plan '
                    'to show.')}


def contract_report(routes, values, *, now=None, path=None, accounts=None):
    """The contract view for every plan; ``accounts`` maps a provider to its account's numbers (default: cached)."""
    accounts = known_account_numbers(values) if accounts is None else accounts
    return ContractView(routes, values, now=now, path=path, accounts=accounts).report()
