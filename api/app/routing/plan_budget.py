"""A monthly budget for each flat or allowance plan, how much of it is used, and what one more fax adds (M3, B11).

HumbleFax sells unlimited faxing for $10 a month, but its terms keep that for
"normal, individual use" and name no number. So Faxbot gives every plan a
monthly normal-use budget in pages and faxes that the administrator sets,
starts it at a cautious default (``config/rate_cards.json``), and counts what
the plan carried since its billing day. While budget remains, a fax on the
plan adds nothing to the bill: its marginal cost is $0. Past it:

- an allowance plan (eFax: 200 pages a month, then $0.10 a page) charges its
  overage price for each page past the allowance;
- a pure flat plan is "over your normal-use budget": the fax still costs
  nothing extra, and route choice should prefer the next route.

Any route may also have a monthly minute allowance (a trunk bundle; minutes
past it cost the route's per-minute price) or a committed monthly spend (what
you spend below it is already paid for).

The budgets are one configuration value, ``plan_budgets``
(``FAX_PLAN_BUDGETS``): entries separated by ";", each
``<route>:<key>=<value>,...``, for example
``humblefax:pages=200,faxes=50,day=1; efax:included_pages=200,page_overage=0.10,day=15``.

- ``pages``, ``faxes``: the normal-use budget a month; ``none`` sets no limit on that count.
- ``day``: the billing day, when the counts start again (1 to 31; a shorter month starts again on its last day).
- ``included_pages`` and ``page_overage``: a page allowance and the price of each page past it.
- ``included_minutes``: a minute allowance; minutes past it cost the route's per-minute price.
- ``commitment``: a committed monthly spend, in the route's currency.

An entry overrides the shipped default key by key. Routes are the keys
delivery records use: ``sip`` for the carrier trunk, else the provider
(``humblefax``, ``efax``).

What counts: faxes sent through the route that were delivered or whose outcome
is uncertain (they may have gone), and faxes received through it, since the
billing day in the installation's time zone. A page the record does not count
is counted as one.

Contract for callers (the predictor, and the rules' "cheapest marginal" method)
---------------------------------------------------------------------------------
- ``budget_left(plan, now=None, *, engine=None, values=None) -> BudgetLeft | None``:
  ``plan`` is a route key or a ``RateCard``; None when the route has no plan,
  allowance or commitment.
- ``marginal(left, pages, prediction=None) -> Marginal``: what one fax of
  ``pages`` pages adds on that route. ``prediction`` (``predict.predict``) gives
  the route's ordinary cost and time on the line, needed for a commitment or a
  minute allowance and for a route without a plan (``left`` None).
- ``order_key(marginal, position)``: sort routes by it. A route over its
  normal-use budget goes after every route within budget; then known cost
  before unknown, cheaper first; between equal costs, a route that uses no
  plan budget (a free toll-free call, a commitment already paid for) goes
  before one that does.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import weakref
import re


KEYS = ('pages', 'faxes', 'day', 'included_pages', 'page_overage', 'included_minutes', 'commitment')
COUNTS = ('pages', 'faxes', 'included_pages', 'included_minutes')
MONEY = ('page_overage', 'commitment')
MAX_ENTRIES = 20
MAX_COUNT = 1_000_000
# A pure flat plan with no shipped budget starts here: about what individual fax plans include (eFax
# Personal: 200 pages a month, its pricing page read 2026-10-04).
GENERIC_PAGES, GENERIC_FAXES = 200, 50
SENT_OUTCOMES = ('success', 'uncertain')
_ROUTE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')


class InvalidBudget(ValueError):
    pass


# The configuration value ---------------------------------------------------------------

def _route_key(route):
    route = route.strip().lower()
    return 'sip' if route == 'sip' or route.startswith('sip-') else route


def _count(value):
    if value == 'none':
        return None
    if not re.fullmatch(r'[0-9]{1,7}', value) or not 0 < int(value) <= MAX_COUNT:
        raise InvalidBudget('Budgets and allowances are whole numbers from 1 to 1,000,000, or none.')
    return int(value)


def _money_value(value):
    from .costs import InvalidRateCard, parse_amount
    try:
        return parse_amount(value, whole_digits=5)
    except InvalidRateCard:
        raise InvalidBudget('Enter amounts as numbers, such as 0.10 or 50.') from None


def parse_budgets(text):
    """``{route: {key: value}}`` from the configuration value; counts are ints (None: no limit), money is micros."""
    found = {}
    for part in (text or '').split(';'):
        part = part.strip()
        if not part:
            continue
        route, colon, rest = part.partition(':')
        route = _route_key(route)
        if not colon or not _ROUTE.fullmatch(route):
            raise InvalidBudget('Write each plan budget as the route, a colon and its values, such as '
                                'humblefax:pages=200,faxes=50,day=1.')
        if route in found:
            raise InvalidBudget(f'The budget for {route} is given twice.')
        entry = {}
        for item in rest.split(','):
            item = item.strip()
            if not item:
                continue
            key, equals, value = (piece.strip().lower() for piece in item.partition('='))
            if not equals or key not in KEYS:
                raise InvalidBudget(f'Use only {", ".join(KEYS)} in a plan budget.')
            if key in entry:
                raise InvalidBudget(f'{key} is given twice for {route}.')
            if key == 'day':
                if not re.fullmatch(r'[0-9]{1,2}', value) or not 1 <= int(value) <= 31:
                    raise InvalidBudget('The billing day is a day of the month from 1 to 31.')
                entry[key] = int(value)
            elif key in MONEY:
                entry[key] = _money_value(value)
            else:
                entry[key] = _count(value)
        found[route] = entry
        if len(found) > MAX_ENTRIES:
            raise InvalidBudget(f'Faxbot keeps budgets for at most {MAX_ENTRIES} routes.')
    return found


def format_budgets(budgets):
    """The configuration value for ``{route: {key: value}}``, in one order, so equal budgets read the same."""
    from .costs import format_amount
    parts = []
    for route in sorted(budgets):
        items = []
        for key in KEYS:
            if key not in budgets[route]:
                continue
            value = budgets[route][key]
            if key in MONEY:
                items.append(f'{key}={format_amount(value)}')
            else:
                items.append(f"{key}={'none' if value is None else value}")
        parts.append(f"{route}:{','.join(items)}")
    return '; '.join(parts)


def normalize_budgets(text):
    """The checked, ordered configuration value; raises ``InvalidBudget`` with a sentence for a person."""
    return format_budgets(parse_budgets(text))


def with_entry(text, route, entry):
    """The configuration value with ``route``'s entry replaced (``entry`` None: back to the default)."""
    budgets = parse_budgets(text)
    route = _route_key(route)
    if entry is None:
        budgets.pop(route, None)
    else:
        budgets[route] = dict(entry)
    return format_budgets(budgets)


# Shipped defaults ------------------------------------------------------------------------

def _document(path=None):
    from .seed import default_path
    try:
        document = json.loads((path or default_path()).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def shipped_budgets(path=None):
    """``{route: default}`` from the plan cards' ``budget`` objects in ``config/rate_cards.json``."""
    found = {}
    for plan in _document(path).get('plans') or ():
        budget = plan.get('budget') if isinstance(plan, dict) else None
        if isinstance(budget, dict) and plan.get('provider_id'):
            found[_route_key(plan['provider_id'])] = budget
    return found


def published_allowance(card, path=None):
    """``(included pages, overage micros, billing day)`` of the published plan a saved plan card matches.

    A saved card matches a published plan (``reference_plans``) by provider and
    monthly fee, as the predictor matches it; (None, None, None) otherwise.
    """
    from .costs import InvalidRateCard, parse_amount
    if card is None or not card.monthly_fee_micros:
        return None, None, None
    for plan in _document(path).get('reference_plans') or ():
        if not isinstance(plan, dict) or plan.get('provider_id') != card.provider_id or not plan.get('included_pages'):
            continue
        try:
            fee = parse_amount(str(plan.get('monthly_fee')), whole_digits=4) if plan.get('monthly_fee') else None
            overage = parse_amount(str(plan['overage_per_page'])) if plan.get('overage_per_page') else None
        except InvalidRateCard:
            continue
        if fee == card.monthly_fee_micros:
            day = plan.get('billing_day') if type(plan.get('billing_day')) is int else None
            return int(plan['included_pages']), overage, day
    return None, None, None


# The budget for one route ---------------------------------------------------------------------

@dataclass(frozen=True)
class Budget:
    route: str
    label: str
    pages: int | None                      # normal-use pages a month; None: no limit
    faxes: int | None                      # normal-use faxes a month; None: no limit
    day: int                               # the billing day, 1 to 31
    included_pages: int | None = None      # a page allowance
    page_overage_micros: int | None = None
    included_minutes: int | None = None    # a minute allowance; minutes past it at ``per_minute_micros``
    commitment_micros: int | None = None   # a committed monthly spend
    currency: str = 'USD'
    monthly_fee_micros: int | None = None
    per_minute_micros: int = 0
    flat: bool = False                     # a monthly fee with nothing charged per fax
    source: str = 'default'                # 'set' (by you), 'default' (Faxbot's start) or 'published'
    sentence: str = ''                     # where the budget comes from, in one sentence

    @property
    def limited(self):
        return self.pages is not None or self.faxes is not None

    def entry(self):
        """The budget as a configuration entry."""
        found = {'pages': self.pages, 'faxes': self.faxes, 'day': self.day}
        for key, value in (('included_pages', self.included_pages), ('page_overage', self.page_overage_micros),
                           ('included_minutes', self.included_minutes), ('commitment', self.commitment_micros)):
            if value is not None:
                found[key] = value
        return found


def _money(micros, currency):
    """An exact price for a sentence: "$0.10", "$0.005"."""
    from .costs import money_text
    return money_text(micros, currency)


def _about(micros, currency):
    """An estimate for a sentence, rounded as the console shows money: "$49.99", "$0.0052"."""
    from .delivered import short_money_text
    return short_money_text(micros, currency)


def _fee(micros, currency):
    """A monthly amount for a sentence, without cents when whole: "$50", "$18.99"."""
    from .costs import plan_fee_text
    return plan_fee_text(micros, currency)


def _number(count):
    return f'{count:,}'


def _plural(count, one, many=None):
    return f"{_number(count)} {one if count == 1 else (many or one + 's')}"


def _label(route, values=None):
    if route == 'sip':
        from ..provider_labels import trunk_name
        return trunk_name(getattr(values, 'sip_trunk_preset', '') or None)
    from .plan import route_label
    return route_label(route)


def _default_sentence(route, label, budget, default):
    """Why Faxbot starts where it does, in one sentence; never says the plan's fair-use terms are safe."""
    if default.get('sentence'):
        return default['sentence']
    if budget.flat and budget.limited:
        return (f'Faxbot starts {label} at {_budget_text(budget)} a month, about what individual fax plans include, '
                f"because an unlimited plan's fair use is for {label} to judge; this is a cautious start, not a "
                f'limit {label} has promised to accept.')
    if budget.included_pages:
        overage = (f' and charges {_money(budget.page_overage_micros, budget.currency)} for each page past them'
                   if budget.page_overage_micros is not None else '')
        return f"{label}'s published plan includes {_plural(budget.included_pages, 'page')} a month{overage}."
    if not budget.flat and budget.monthly_fee_micros:
        return f'{label} charges for each fax on top of its monthly fee, so Faxbot sets no normal-use budget for it.'
    return f'Faxbot sets no normal-use budget for {label}.'


def metered(budget):
    """A route whose every fax is charged at its own price: no plan room, allowance or commitment to use first."""
    return (not budget.flat and not budget.included_pages and not budget.included_minutes
            and budget.commitment_micros is None)


def _budget_text(budget):
    parts = []
    if budget.pages is not None:
        parts.append(_plural(budget.pages, 'page'))
    if budget.faxes is not None:
        parts.append(_plural(budget.faxes, 'fax', 'faxes'))
    return ' and '.join(parts) or 'no limit'


def budget_for(route, card, values=None, *, path=None, inbound=None):
    """The budget for ``route`` with its rate card, or None when the route has no plan, allowance or commitment.

    ``inbound``: the route's receiving card, when it has one, for a plan priced only on receiving.
    """
    route = _route_key(route)
    try:
        entry = parse_budgets(getattr(values, 'plan_budgets', '') or '').get(route)
    except InvalidBudget:
        entry = None
    shipped = shipped_budgets(path).get(route) or {}
    plan_card = card if card is not None and card.monthly_fee_micros else (
        inbound if inbound is not None and inbound.monthly_fee_micros else card)
    included, overage, published_day = published_allowance(plan_card, path)
    flat = bool(plan_card is not None and plan_card.flat_plan)
    if entry is None and plan_card is None:
        return None
    defaults = {'day': shipped.get('billing_day') or published_day or 1}
    source = 'default'
    # A flat plan gets a normal-use budget unless it has a page allowance (published or yours): past an
    # allowance it charges by the page, so there is no fair use to guard.
    if flat and not included and not (entry or {}).get('included_pages'):
        defaults['pages'] = shipped['pages'] if 'pages' in shipped else GENERIC_PAGES
        defaults['faxes'] = shipped['faxes'] if 'faxes' in shipped else GENERIC_FAXES
    if included:
        defaults['included_pages'], defaults['page_overage'] = included, overage
        source = 'published'
    if entry is None and not flat and not included and not (plan_card is not None and plan_card.monthly_fee_micros):
        return None  # a per-fax route with nothing set and no monthly fee: no budget
    merged = {**defaults, **(entry or {})}
    label = _label(route, values)
    currency = plan_card.currency if plan_card is not None else 'USD'
    budget = Budget(route=route, label=label, pages=merged.get('pages'), faxes=merged.get('faxes'),
                    day=merged.get('day') or 1, included_pages=merged.get('included_pages'),
                    page_overage_micros=merged.get('page_overage'), included_minutes=merged.get('included_minutes'),
                    commitment_micros=merged.get('commitment'), currency=currency,
                    monthly_fee_micros=plan_card.monthly_fee_micros if plan_card is not None else None,
                    per_minute_micros=plan_card.per_minute_micros if plan_card is not None else 0, flat=flat,
                    source='set' if entry is not None else source)
    if entry is not None:
        sentence = f'You set this budget for {label}.'
    else:
        sentence = _default_sentence(route, label, budget, shipped if flat else {})
    return replace(budget, sentence=sentence)


# The billing period -------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    start: datetime          # naive UTC, inclusive
    end: datetime            # naive UTC, exclusive: when the counts start again
    first_day: date          # the billing day this period started, in the installation's time zone
    next_day: date           # the day the counts start again


def _anchor(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _month_after(year, month):
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _month_before(year, month):
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _zone(name):
    from ..people_time import zone
    return zone(name)


def _utc(day, tz):
    return datetime.combine(day, time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def billing_period(now, day, zone_name=''):
    """The billing period holding ``now`` (naive UTC) for a plan billed on ``day``, by the installation's clock.

    A billing day past a month's end falls on that month's last day: billed on
    the 31st, February's period starts on the 28th (or 29th).
    """
    tz = _zone(zone_name)
    today = now.replace(tzinfo=timezone.utc).astimezone(tz).date()
    first = _anchor(today.year, today.month, day)
    if first > today:
        first = _anchor(*_month_before(today.year, today.month), day)
    following = _anchor(*_month_after(first.year, first.month), day)
    return Period(_utc(first, tz), _utc(following, tz), first, following)


def day_text(day):
    """'1 November'"""
    return f'{day.day} {day:%B}'


def _ordinal(day):
    suffix = 'th' if 11 <= day % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
    return f'{day}{suffix}'


def billing_day_text(day):
    """'the 1st', 'the 31st (or the month's last day)'"""
    return f"the {_ordinal(day)}" + (" (or the month's last day)" if day > 28 else '')


# What the route carried --------------------------------------------------------------------------

@dataclass(frozen=True)
class Usage:
    sent_faxes: int = 0
    sent_pages: int = 0
    received_faxes: int = 0
    received_pages: int = 0
    minutes: int | None = None          # counted only with a minute allowance
    spend_micros: int | None = None     # counted only with a commitment
    unpriced: int = 0                   # faxes and calls with no known cost (with a commitment)

    @property
    def faxes(self):
        return self.sent_faxes + self.received_faxes

    @property
    def pages(self):
        return self.sent_pages + self.received_pages


_REFLECTED = weakref.WeakKeyDictionary()
_TABLE_NAMES = ('delivery_attempt_costs', 'fax_jobs', 'inbound_faxes', 'sip_call_records', 'provider_rate_cards')


def _tables(engine):
    """The tables read here, reflected once per database (the schema only changes at startup), as the predictor does."""
    found = _REFLECTED.get(engine)
    if found is None:
        from .database import reflect
        found = reflect(engine, _TABLE_NAMES)
        _REFLECTED[engine] = found
    return found


def records(engine, route, start, end):
    """``[(when, direction, pages)]`` for every fax ``route`` carried in ``[start, end)``, oldest first."""
    from .database import read_connection
    import sqlalchemy as sa
    tables = _tables(engine)
    costs, jobs, faxes = tables['delivery_attempt_costs'], tables['fax_jobs'], tables['inbound_faxes']
    received_at = sa.func.coalesce(faxes.c.received_at, faxes.c.created_at)
    with read_connection(engine) as connection:
        sent = connection.execute(sa.select(costs.c.created_at, jobs.c.pages, costs.c.billed_pages).select_from(
            costs.outerjoin(jobs, jobs.c.id == costs.c.job_id)).where(
            costs.c.route == route, costs.c.outcome.in_(SENT_OUTCOMES), costs.c.created_at >= start,
            costs.c.created_at < end)).all()
        received = connection.execute(sa.select(received_at.label('at'), faxes.c.pages).where(
            faxes.c.backend == route, received_at >= start, received_at < end)).all()
    found = [(row.created_at, 'sent', max(1, int(row.pages or row.billed_pages or 0))) for row in sent]
    found += [(row.at, 'received', max(1, int(row.pages or 0))) for row in received]
    return sorted(found, key=lambda item: item[0])


def _minutes(engine, route, start, end, inbound_card):
    """Billed minutes on the route: sent calls as the carrier billed them, received trunk calls per started minute."""
    from .costs import billed_seconds
    from .database import read_connection
    import sqlalchemy as sa
    tables = _tables(engine)
    costs, calls = tables['delivery_attempt_costs'], tables['sip_call_records']
    with read_connection(engine) as connection:
        sent = connection.scalar(sa.select(sa.func.coalesce(sa.func.sum(costs.c.billed_seconds), 0)).where(
            costs.c.route == route, costs.c.created_at >= start, costs.c.created_at < end)) or 0
        received = 0
        if route == 'sip':
            for (seconds,) in connection.execute(sa.select(calls.c.connected_seconds).where(
                    calls.c.direction == 'inbound', calls.c.started_at >= start, calls.c.started_at < end,
                    calls.c.connected_seconds.is_not(None))):
                received += (billed_seconds(inbound_card, seconds) if inbound_card is not None
                             else 60 * math.ceil(seconds / 60))
    return math.ceil((int(sent) + received) / 60)


def _spend(engine, route, start, end, currency, inbound_card):
    """(micros spent on the route in ``currency``, how many faxes and calls have no known cost)."""
    from .costs import attempt_cost
    from .database import read_connection
    import sqlalchemy as sa
    tables = _tables(engine)
    costs, calls, faxes = tables['delivery_attempt_costs'], tables['sip_call_records'], tables['inbound_faxes']
    total, unpriced = 0, 0
    with read_connection(engine) as connection:
        for row in connection.execute(sa.select(costs.c.reported_cost_micros, costs.c.reported_currency,
                                                costs.c.estimated_cost_micros, costs.c.currency).where(
                costs.c.route == route, costs.c.outcome != 'pending', costs.c.created_at >= start,
                costs.c.created_at < end)):
            if row.reported_cost_micros is not None and row.reported_currency == currency:
                total += int(row.reported_cost_micros)
            elif row.estimated_cost_micros is not None and row.currency == currency:
                total += int(row.estimated_cost_micros)
            else:
                unpriced += 1
        if inbound_card is None or inbound_card.currency != currency:
            return total, unpriced
        if route == 'sip':
            rows = connection.execute(sa.select(calls.c.connected_seconds, calls.c.pages).where(
                calls.c.direction == 'inbound', calls.c.started_at >= start, calls.c.started_at < end)).all()
        else:
            when = sa.func.coalesce(faxes.c.received_at, faxes.c.created_at)
            rows = [(None, row.pages) for row in connection.execute(sa.select(faxes.c.pages).where(
                faxes.c.backend == route, when >= start, when < end))]
    for seconds, pages in rows:
        cost = attempt_cost(inbound_card, seconds=seconds, pages=pages, delivered=True)
        if cost is None:
            unpriced += 1
        else:
            total += cost
    return total, unpriced


def usage(engine, budget, period, *, now=None, inbound_card=None):
    """What the route carried from the period's start until ``now`` (or the period's end)."""
    # Up to and including ``now``: a fax recorded this second has been carried.
    end = min(period.end, now + timedelta(seconds=1)) if now is not None else period.end
    found = records(engine, budget.route, period.start, end)
    sent = [pages for _, direction, pages in found if direction == 'sent']
    received = [pages for _, direction, pages in found if direction == 'received']
    minutes = _minutes(engine, budget.route, period.start, end, inbound_card) if budget.included_minutes else None
    spend, unpriced = (_spend(engine, budget.route, period.start, end, budget.currency, inbound_card)
                       if budget.commitment_micros is not None else (None, 0))
    return Usage(len(sent), sum(sent), len(received), sum(received), minutes, spend, unpriced)


# What is left ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BudgetLeft:
    budget: Budget
    period: Period
    used: Usage
    pages_left: int | None             # of the normal-use budget; None: no page limit
    faxes_left: int | None
    allowance_left: int | None         # of the page allowance; None: no allowance
    overage_pages: int = 0
    overage_micros: int | None = 0     # None: the price of extra pages is not known
    minutes_left: int | None = None
    overage_minutes: int = 0
    commitment_left_micros: int | None = None
    over: bool = False                 # past the normal-use budget
    state: str = 'within'              # 'within', 'over_budget', 'past_allowance', 'no_limit'
    sentence: str = ''

    @property
    def overage_minute_micros(self):
        return self.overage_minutes * self.budget.per_minute_micros


def _left(budget, period, used):
    """The arithmetic of what is left, with no database: tests and the burn-down use it directly."""
    pages_left = None if budget.pages is None else budget.pages - used.pages
    faxes_left = None if budget.faxes is None else budget.faxes - used.faxes
    over = (pages_left is not None and pages_left <= 0) or (faxes_left is not None and faxes_left <= 0)
    allowance_left = overage_pages = None
    overage_micros = 0
    if budget.included_pages:
        allowance_left = budget.included_pages - used.pages
        overage_pages = max(0, -allowance_left)
        overage_micros = (None if overage_pages and budget.page_overage_micros is None
                          else overage_pages * (budget.page_overage_micros or 0))
    minutes_left = overage_minutes = None
    if budget.included_minutes and used.minutes is not None:
        minutes_left = budget.included_minutes - used.minutes
        overage_minutes = max(0, -minutes_left)
    commitment_left = None
    if budget.commitment_micros is not None and used.spend_micros is not None:
        commitment_left = max(0, budget.commitment_micros - used.spend_micros)
    if over:
        state = 'over_budget'
    elif overage_pages or overage_minutes:
        state = 'past_allowance'
    elif not budget.limited and not budget.included_pages and not budget.included_minutes:
        state = 'no_limit'
    else:
        state = 'within'
    left = BudgetLeft(budget, period, used, pages_left, faxes_left, allowance_left, overage_pages or 0,
                      overage_micros, minutes_left, overage_minutes or 0, commitment_left, over, state)
    return replace(left, sentence=left_sentence(left))


def _carried(used):
    return f"{_plural(used.pages, 'page')} and {_plural(used.faxes, 'fax', 'faxes')}"


def left_sentence(left):
    """Where the plan stands this period, in one sentence."""
    budget, used, period = left.budget, left.used, left.period
    name, since, again = budget.label, day_text(period.first_day), day_text(period.next_day)
    if budget.included_pages:
        if left.overage_pages:
            extra = ('the price of extra pages is not known' if left.overage_micros is None else
                     f'about {_about(left.overage_micros, budget.currency)} in extra pages so far (estimate)')
            return (f"{name} has used {_plural(used.pages, 'page')} since {since}, {_number(left.overage_pages)} past "
                    f'the {_number(budget.included_pages)} your plan includes, {extra}; the allowance starts again on '
                    f'{again}.')
        return (f'{name} has used {_number(used.pages)} of the {_plural(budget.included_pages, "page")} your plan '
                f'includes since {since}; {_number(left.allowance_left)} are left until {again}.')
    if budget.included_minutes and left.minutes_left is not None:
        if left.overage_minutes:
            return (f"{name} has used {_plural(used.minutes, 'minute')} since {since}, {_number(left.overage_minutes)} "
                    f'past the {_number(budget.included_minutes)} your plan includes, about '
                    f'{_about(left.overage_minute_micros, budget.currency)} extra so far (estimate); the minutes start '
                    f'again on {again}.')
        return (f'{name} has used {_number(used.minutes)} of the {_plural(budget.included_minutes, "minute")} your '
                f'plan includes since {since}; {_number(left.minutes_left)} are left until {again}.')
    if left.over:
        limit = _budget_text(budget)
        return (f'{name} has carried {_carried(used)} since {since}, past your normal-use budget of {limit}; '
                f'the budget starts again on {again}.')
    if budget.limited:
        parts = []
        if left.pages_left is not None:
            parts.append(_plural(left.pages_left, 'page'))
        if left.faxes_left is not None:
            parts.append(_plural(left.faxes_left, 'fax', 'faxes'))
        return (f"{name} has carried {_carried(used)} since {since}; {' and '.join(parts)} of your normal-use budget "
                f'are left until {again}.')
    if budget.commitment_micros is not None and left.commitment_left_micros is not None:
        spent = _about(used.spend_micros, budget.currency)
        unknown = (f", and {_plural(used.unpriced, 'fax or call', 'faxes and calls')} with no known cost"
                   if used.unpriced else '')
        return (f'You have spent about {spent} with {name} since {since}{unknown}; '
                f'{_about(left.commitment_left_micros, budget.currency)} of your '
                f'{_fee(budget.commitment_micros, budget.currency)} monthly commitment is left until {again} '
                '(estimate).')
    if not budget.flat:
        return f'{name} has carried {_carried(used)} since {since}; the counts start again on {again}.'
    return f'{name} has carried {_carried(used)} since {since}; you set no normal-use budget for it.'


def _engine():
    from .predict_facts import _engine as engine
    return engine()


def _values():
    from .predict_facts import _values as values
    return values()


def _cards(engine, route, values):
    """(sending card, receiving card) for the route: the saved cards, else the shipped ones without a database."""
    if engine is not None:
        # Found as ``RouteStore.card_for`` finds it (the trunk by its carrier's card, then a plain "sip" card),
        # from the cached reflection rather than a new store on every prediction.
        import sqlalchemy as sa
        from .database import read_connection
        from .store import RouteStore
        preset = getattr(values, 'sip_trunk_preset', '') or ''
        identities = ((f'sip-{preset}', 'sip') if preset else ('sip',)) if route == 'sip' else (route,)
        cards = _tables(engine)['provider_rate_cards']
        with read_connection(engine) as connection:
            rows = connection.execute(sa.select(cards).where(
                cards.c.superseded_at.is_(None), cards.c.provider_id.in_(identities))).mappings().all()
        found = {(row['provider_id'], row['direction']): RouteStore._card(row) for row in rows}
        pick = lambda direction: next((found[(identity, direction)] for identity in identities  # noqa: E731
                                       if (identity, direction) in found), None)
        return pick('outbound'), pick('inbound')
    from .seed import load_cards
    cards = load_cards()
    identity = f"sip-{getattr(values, 'sip_trunk_preset', '') or ''}" if route == 'sip' else route
    pick = lambda direction: next((card for card in cards if card.provider_id == identity
                                   and card.direction == direction), None)
    return pick('outbound'), pick('inbound')


def budget_left(plan, now=None, *, engine=None, values=None, path=None):
    """How much of ``plan``'s budget is left at ``now``; None when the route has no plan, allowance or commitment.

    ``plan`` is a route key (``humblefax``, ``efax``, ``sip``) or a ``RateCard``.
    Without a database (no engine) nothing has been carried yet.
    """
    from .database import utcnow
    values = _values() if values is None else values
    engine = _engine() if engine is None else engine
    route = _route_key(plan if isinstance(plan, str) else plan.provider_id)
    card, inbound = _cards(engine, route, values)
    if not isinstance(plan, str):
        card = plan if plan.direction == 'outbound' else card
    budget = budget_for(route, card, values, path=path, inbound=inbound)
    if budget is None:
        return None
    now = (now or utcnow()).replace(tzinfo=None, microsecond=0)
    period = billing_period(now, budget.day, getattr(values, 'time_zone', '') or '')
    used = usage(engine, budget, period, now=now, inbound_card=inbound) if engine is not None else Usage(
        minutes=0 if budget.included_minutes else None, spend_micros=0 if budget.commitment_micros is not None else None)
    return _left(budget, period, used)


def plan_use(engine, route_key, *, now, values=None):
    """The predictor's ``PlanUse`` for a route: what it carried this billing period, and its page budget."""
    from .predict import PlanUse
    left = budget_left(route_key, now, engine=engine, values=values)
    if left is None:
        return None
    return PlanUse(pages=left.used.pages, faxes=left.used.faxes, page_budget=left.budget.pages)


# One more fax ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Marginal:
    cost: object                       # costs.Money, or None when unknown (never 0)
    over_budget: bool                  # the route is past its normal-use budget: prefer another route
    uses_budget: bool                  # the fax uses up part of a plan's budget or allowance
    sentence: str


def order_key(marginal, position=0):
    """Sort key for the "cheapest marginal" order; ``position`` keeps the configured order between equals."""
    cost = marginal.cost
    return (marginal.over_budget, cost is None, cost.micros if cost is not None else 0, marginal.uses_budget, position)


def marginal(left, pages, prediction=None):
    """What one fax of ``pages`` pages adds to the bill on a route (``left``: its ``budget_left``, or None)."""
    from .costs import Money
    pages = pages if isinstance(pages, int) and pages > 0 else 1
    if left is None or metered(left.budget):
        if prediction is None:
            raise ValueError('Give the prediction for a route without a plan.')
        return Marginal(prediction.cost, False, False, prediction.basis)
    budget = left.budget
    name, again, currency = budget.label, day_text(left.period.next_day), budget.currency
    over = left.over or (left.pages_left is not None and pages > left.pages_left) or (
        left.faxes_left is not None and left.faxes_left < 1)
    if budget.included_pages:
        room = max(0, left.allowance_left)
        extra = max(0, pages - room)
        if not extra:
            cost, sentence = Money(0, currency), (
                f'Included in the {_number(budget.included_pages)} pages your {name} plan includes; '
                f'{_number(room - pages)} will be left until {again}.')
        elif budget.page_overage_micros is None:
            cost, sentence = None, (f"{_plural(extra, 'page')} of this fax would go past what your {name} plan "
                                    'includes, and the price of extra pages is not known.')
        else:
            cost, sentence = Money(extra * budget.page_overage_micros, currency), (
                f"{_plural(extra, 'page')} past what your {name} plan includes, at "
                f'{_money(budget.page_overage_micros, currency)} a page.')
        if over and budget.limited:
            sentence += f' It would also go past your normal-use budget for {name} until {again}.'
        return Marginal(cost, over and budget.limited, True, sentence)
    if budget.included_minutes:
        if prediction is None or prediction.seconds is None or left.minutes_left is None:
            return Marginal(None, False, True, f'Whether this fax fits in the minutes your {name} plan includes is '
                                               'unknown, because its time on the line is unknown.')
        minutes = math.ceil(prediction.seconds / 60)
        extra = max(0, minutes - max(0, left.minutes_left))
        if not extra:
            return Marginal(Money(0, currency), False, True,
                            f'Included in the {_number(budget.included_minutes)} minutes your {name} plan includes; '
                            f'about {_number(max(0, left.minutes_left) - minutes)} will be left until {again}.')
        return Marginal(Money(extra * budget.per_minute_micros, currency), False, True,
                        f"About {_plural(extra, 'minute')} past what your {name} plan includes, at "
                        f'{_money(budget.per_minute_micros, currency)} a minute.')
    if budget.commitment_micros is not None and not budget.flat:
        if prediction is None or prediction.cost is None or left.commitment_left_micros is None:
            return Marginal(None, False, False, f'Whether your monthly commitment with {name} covers this fax is '
                                                'unknown, because its cost is unknown.')
        cost = prediction.cost.micros
        extra = max(0, cost - left.commitment_left_micros)
        if not extra:
            return Marginal(Money(0, prediction.cost.currency), False, False,
                            f'Covered by your {_fee(budget.commitment_micros, currency)} monthly commitment with '
                            f'{name}, already paid for until {again}.')
        return Marginal(Money(extra, prediction.cost.currency), False, False,
                        f'About {_about(extra, currency)} past your {_fee(budget.commitment_micros, currency)} '
                        f'monthly commitment with {name}.')
    if not budget.limited:
        return Marginal(Money(0, currency), False, False,
                        f'Included in your {name} plan, with no normal-use budget set.')
    if over:
        return Marginal(Money(0, currency), True, True,
                        f'Over your normal-use budget for {name} until {again}; the fax itself costs nothing extra.')
    parts = []
    if left.pages_left is not None:
        parts.append(_plural(left.pages_left - pages, 'page'))
    if left.faxes_left is not None:
        parts.append(_plural(left.faxes_left - 1, 'fax', 'faxes'))
    return Marginal(Money(0, currency), False, True,
                    f"Included in your {name} plan; {' and '.join(parts)} of your normal-use budget will be left "
                    f'until {again}.')


# The burn-down -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BurnDay:
    day: date
    pages: int          # pages carried from the period's start to the end of this day
    faxes: int


def burn_down(engine, left, *, now, zone_name=''):
    """One row per day from the period's first day to today: what the plan had carried by the end of that day."""
    tz = _zone(zone_name)
    period = left.period
    end = min(period.end, now + timedelta(seconds=1))
    found = records(engine, left.budget.route, period.start, end) if engine is not None else []
    today = now.replace(tzinfo=timezone.utc).astimezone(tz).date()
    totals = {}
    for when, _, pages in found:
        day = when.replace(tzinfo=timezone.utc).astimezone(tz).date()
        pages_so_far, faxes_so_far = totals.get(day, (0, 0))
        totals[day] = (pages_so_far + pages, faxes_so_far + 1)
    rows, pages, faxes = [], 0, 0
    day = period.first_day
    while day <= min(today, period.next_day - timedelta(days=1)):
        added = totals.get(day, (0, 0))
        pages, faxes = pages + added[0], faxes + added[1]
        rows.append(BurnDay(day, pages, faxes))
        day += timedelta(days=1)
    return rows


def pace_sentence(left, rows):
    """At this pace, where the plan ends the period, in one sentence; None before a full day of records."""
    budget = left.budget
    limit = budget.included_pages or budget.pages
    if not rows or limit is None or len(rows) < 2:
        return None
    days = (left.period.next_day - left.period.first_day).days
    projected = math.ceil(rows[-1].pages * days / len(rows))
    what = 'allowance' if budget.included_pages else 'normal-use budget'
    if projected <= limit:
        return (f'At this pace {budget.label} will carry about {_plural(projected, "page")} by '
                f'{day_text(left.period.next_day)}, within your {what} of {_number(limit)}.')
    return (f'At this pace {budget.label} will carry about {_plural(projected, "page")} by '
            f'{day_text(left.period.next_day)}, past your {what} of {_number(limit)}.')


