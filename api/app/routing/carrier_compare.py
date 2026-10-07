"""What your last 30 days of faxing would have cost at each carrier's published prices (B13). Advice only.

Faxbot ships dated, sourced prices for many carriers and fax services
(``config/rate_cards.json``). This module prices the faxes you actually sent
and received in the window at each of them, the same way for every carrier
and for the services you use now, so the figures compare like with like:

- a sent fax is priced by the class of the number it went to (local,
  toll-free, abroad) with the carrier's own price for that class, billing
  increment and minimum (``predict_facts.terms_for``); its time on the line is
  the call's measured connected time when the fax went over your trunk, else
  the usual estimate (30 seconds plus 30 a page);
- a received fax is priced with the carrier's receiving price for a local
  number; a fax received on a toll-free or foreign number has no comparable
  price, so it stays unknown;
- each carrier also costs its monthly fees: a plan's fee, and the published
  monthly price of one number for each number that received faxes.

Unknown stays unknown: a carrier that publishes no price is left out and named
in one sentence, and a carrier missing a price for some of your faxes shows
what it does price, counts the rest, and is never named the cheapest.
Changing carriers takes porting your numbers and a new account or contract;
Faxbot compares only and never switches anything. Every figure is an estimate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json

import sqlalchemy as sa

from .costs import ESTIMATE_SECONDS_PER_PAGE, ESTIMATE_SETUP_SECONDS, InvalidRateCard, parse_amount, terms_cost
from .database import read_connection, reflect, utcnow
from .delivered import short_money_text
from .destinations import LOCAL, classify


WINDOW_DAYS = 30
SWITCHING = ('Changing carriers means moving (porting) your fax numbers to the new carrier and opening an account '
             'there, often under a contract; Faxbot only compares published prices and never switches anything.')
NOTHING = 'You sent and received no faxes in the last {days} days, so there is nothing to compare yet.'


@dataclass(frozen=True)
class Fax:
    direction: str            # 'sent' or 'received'
    number: str               # the number called (sent) or the number that received it
    pages: int
    seconds: float | None     # measured time on the line, when known
    route: str                # the route it actually took: 'sip', 'humblefax', 'phaxio'...


@dataclass(frozen=True)
class Carrier:
    id: str                   # 'sip-telnyx', 'phaxio', 'humblefax'
    name: str
    kind: str                 # 'trunk' (your own fax engine over its SIP trunk), 'service' or 'plan'
    sending: object           # RateCard
    receiving: object | None  # RateCard, or None when it publishes no receiving price
    rental_micros: int | None  # one number a month; None when not published
    includes_number: bool = False
    source_url: str | None = None
    advertised_on: str | None = None


def _document(path=None):
    from .seed import default_path
    try:
        document = json.loads((path or default_path()).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def _rentals(document):
    """{provider id: monthly micros or None} for every shipped card that states its number price."""
    found = {}
    for key in ('cards', 'providers', 'plans'):
        for entry in document.get(key) or ():
            if not isinstance(entry, dict) or 'number_rental_monthly' not in entry:
                continue
            value = entry.get('number_rental_monthly')
            try:
                found.setdefault(entry.get('provider_id'), None if value in (None, '') else parse_amount(str(value)))
            except InvalidRateCard:
                found.setdefault(entry.get('provider_id'), None)
    return found


def _name(identity):
    """'Telnyx trunk' for a carrier's trunk (Faxbot's own fax engine places the calls), 'Phaxio' for a fax service."""
    if identity.startswith('sip-'):
        from .carriers import carrier_label
        return f"{carrier_label(identity[len('sip-'):])} trunk"
    from .plan import route_label
    return route_label(identity)


def carriers(path=None):
    """(carriers with a published sending price, names of those that publish none)."""
    from .seed import load_cards
    document = _document(path)
    cards = load_cards(path)
    rentals = _rentals(document)
    plans = {entry.get('provider_id'): entry for entry in document.get('plans') or () if isinstance(entry, dict)}
    priced = {}
    for card in cards:
        priced.setdefault(card.provider_id, {})[card.direction] = card
    found = []
    for identity, sides in sorted(priced.items()):
        sending = sides.get('outbound')
        if sending is None:
            continue
        plan = plans.get(identity) or {}
        found.append(Carrier(identity, _name(identity), 'trunk' if identity.startswith('sip-') else (
            'plan' if sending.flat_plan else 'service'), sending, sides.get('inbound') or (
            sending if sending.flat_plan else None), rentals.get(identity), bool(plan.get('includes_number')),
            sending.source_url, sending.captured_on.date().isoformat()))
    listed = {carrier.id for carrier in found}
    missing = []
    for key in ('cards', 'providers'):
        for entry in document.get(key) or ():
            identity = entry.get('provider_id') if isinstance(entry, dict) else None
            if identity and identity not in listed and _name(identity) not in missing:
                missing.append(_name(identity))
    for entry in document.get('reference_plans') or ():
        # A published plan the provider does not sell for the service Faxbot uses (eFax prices its API by quote).
        if isinstance(entry, dict) and entry.get('provider_id') and _name(entry['provider_id']) not in missing \
                and entry.get('provider_id') not in listed:
            missing.append(_name(entry['provider_id']))
    return found, missing


# Your faxes ---------------------------------------------------------------------------------------

def recorded_faxes(engine, values, start, end):
    """Every fax delivered or received in ``[start, end)``, with its pages and measured time when known."""
    tables = reflect(engine, ('delivery_attempt_costs', 'fax_jobs', 'inbound_faxes', 'sip_call_records'))
    costs, jobs, faxes, calls = (tables['delivery_attempt_costs'], tables['fax_jobs'], tables['inbound_faxes'],
                                 tables['sip_call_records'])
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    received_at = sa.func.coalesce(faxes.c.received_at, faxes.c.created_at)
    with read_connection(engine) as connection:
        sent = connection.execute(sa.select(
            costs.c.route, costs.c.destination, jobs.c.pages, costs.c.billed_pages, calls.c.connected_seconds).select_from(
            costs.outerjoin(jobs, jobs.c.id == costs.c.job_id).outerjoin(
                calls, sa.and_(calls.c.attempt_id == costs.c.id, calls.c.direction == 'outbound'))).where(
            costs.c.outcome == 'success', costs.c.created_at >= start, costs.c.created_at < end,
            costs.c.route.notin_(('local', 'direct')))).all()
        received = connection.execute(sa.select(faxes.c.backend, faxes.c.to_number, faxes.c.pages).where(
            received_at >= start, received_at < end, faxes.c.backend.notin_(('local', 'direct')))).all()
    from .numbers import stored_number
    found = [Fax('sent', row.destination, max(1, int(row.pages or row.billed_pages or 0)),
                 float(row.connected_seconds) if row.route == 'sip' and row.connected_seconds is not None else None,
                 row.route) for row in sent]
    found += [Fax('received', stored_number(row.to_number or '', country=country) if row.to_number else '',
                  max(1, int(row.pages or 0)), None, row.backend or '') for row in received]
    return found


def _seconds(fax):
    if fax.seconds is not None:
        return fax.seconds
    return ESTIMATE_SETUP_SECONDS + ESTIMATE_SECONDS_PER_PAGE * fax.pages


# Pricing ------------------------------------------------------------------------------------------

@dataclass
class Bill:
    sent: int = 0
    received: int = 0
    monthly: int = 0
    not_priced: int = 0           # faxes with no published price at this carrier
    numbers_unpriced: int = 0     # numbers whose monthly price is not published
    over_budget: bool = False     # a flat plan carrying more than its normal-use budget

    @property
    def total(self):
        return self.sent + self.received + self.monthly

    @property
    def complete(self):
        return not self.not_priced and not self.numbers_unpriced


def _terms(carrier, fax, country, data):
    """RateTerms for one fax at this carrier, or None when it publishes no price for it."""
    from .costs import RateTerms
    from .predict_facts import terms_for
    where = classify(fax.number, country) if fax.number else None
    if fax.direction == 'received':
        if carrier.receiving is None or (where is not None and where.kind != LOCAL):
            return None
        return RateTerms(carrier.receiving, LOCAL, published=True)
    if where is None:
        return None
    terms, _ = terms_for(carrier.id, where, carrier.sending, data, label=carrier.name)
    return terms


def price(carrier, faxes, *, country='US', days=WINDOW_DAYS, numbers=0, data=None, budget_pages=None):
    """What ``faxes`` would have cost at ``carrier``, with ``numbers`` receiving numbers kept for ``days`` days."""
    from .costs import plan_fee_for_days
    from .predict_facts import shipped
    data = shipped() if data is None else data
    bill = Bill()
    for fax in faxes:
        terms = _terms(carrier, fax, country, data)
        if terms is None:
            bill.not_priced += 1
            continue
        _, micros = terms_cost(terms, seconds=_seconds(fax), pages=fax.pages)
        if micros is None:
            bill.not_priced += 1
        elif fax.direction == 'sent':
            bill.sent += micros
        else:
            bill.received += micros
    bill.monthly += plan_fee_for_days(carrier.sending, days)
    rented = max(0, numbers - (1 if carrier.includes_number else 0))
    if rented:
        if carrier.rental_micros is None:
            bill.numbers_unpriced += rented
        else:
            bill.monthly += -(-carrier.rental_micros * rented * days // 30)
    if carrier.sending.flat_plan and budget_pages is not None:
        bill.over_budget = sum(fax.pages for fax in faxes) > budget_pages
    return bill


def _money(micros, currency):
    from .costs import format_amount
    return [{'currency': currency, 'amount': format_amount(micros)}]


def _about(micros, currency):
    return short_money_text(micros, currency)


def _count(count, one, many=None):
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _carrier_sentence(carrier, bill, sent, received, numbers, currency):
    what = []
    if sent:
        what.append(f"{_count(sent, 'sent fax', 'sent faxes')}")
    if received:
        what.append(f"{_count(received, 'received fax', 'received faxes')}")
    head = f"About {_about(bill.total, currency)} for your {' and '.join(what)}"
    parts = []
    if carrier.sending.flat_plan:
        parts.append('its plan fee included')
    elif bill.monthly and numbers:
        parts.append(f"{_count(numbers, 'number')} included at its published monthly price")
    if bill.not_priced:
        parts.append(f"{_count(bill.not_priced, 'fax', 'faxes')} it publishes no price for left out")
    if bill.numbers_unpriced:
        parts.append(f"{_count(bill.numbers_unpriced, 'number')} it publishes no monthly price for left out")
    sentence = head + (f" ({', '.join(parts)})" if parts else '') + ' (estimate).'
    if bill.over_budget:
        sentence += (f' That is more than the normal-use budget Faxbot uses for {carrier.name}, so its plan may not '
                     'take this much.')
    return sentence


def compare(engine, values, *, now=None, days=WINDOW_DAYS, path=None):
    """The comparison for the last ``days`` days: each carrier's bill, the cheapest, and the difference."""
    from .plan_budget import budget_for
    from .predict_facts import shipped
    now = (now or utcnow()).replace(microsecond=0)
    start = now - timedelta(days=days)
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    found, missing = carriers(path)
    data = shipped(path) if path is not None else shipped()
    faxes = recorded_faxes(engine, values, start, now)
    sent = sum(1 for fax in faxes if fax.direction == 'sent')
    received = len(faxes) - sent
    numbers = len({fax.number for fax in faxes if fax.direction == 'received'}) or (1 if faxes else 0)
    currency = 'USD'
    base = {'days': days, 'estimate': True, 'advice_only': True, 'sent': sent, 'received': received,
            'switching_sentence': SWITCHING,
            'unpublished_sentence': (f"{_join(missing)} {'publishes' if len(missing) == 1 else 'publish'} no price "
                                     'Faxbot can use, so ' + ('it is' if len(missing) == 1 else 'they are')
                                     + ' left out.') if missing else None}
    if not faxes:
        return {**base, 'sentence': NOTHING.format(days=days), 'carriers': [], 'current': None, 'cheapest': None}
    rows = []
    for carrier in found:
        if carrier.sending.currency != currency:
            continue
        budget = budget_for(carrier.id, carrier.sending, values, path=path) if carrier.sending.flat_plan else None
        bill = price(carrier, faxes, country=country, days=days, numbers=numbers, data=data,
                     budget_pages=budget.pages if budget is not None else None)
        yours = carrier.id in _yours(faxes, preset)
        rows.append((carrier, bill, yours))
    current = _current(found, faxes, preset, country, days, data, currency)
    complete = [(carrier, bill) for carrier, bill, _ in rows if bill.complete and not bill.over_budget]
    cheapest = min(complete, key=lambda item: (item[1].total, item[0].id)) if complete else None
    views = []
    for carrier, bill, yours in sorted(rows, key=lambda item: (not item[1].complete, item[1].total, item[0].id)):
        difference = (current['micros'] - bill.total if current['complete'] and bill.complete else None)
        views.append({'id': carrier.id, 'name': carrier.name, 'kind': carrier.kind, 'yours': yours,
                      'total': _money(bill.total, currency), 'complete': bill.complete,
                      'sending': _money(bill.sent, currency), 'receiving': _money(bill.received, currency),
                      'monthly': _money(bill.monthly, currency), 'not_priced': bill.not_priced,
                      'numbers_not_priced': bill.numbers_unpriced, 'over_budget': bill.over_budget,
                      'cheapest': cheapest is not None and carrier.id == cheapest[0].id,
                      'difference': [] if difference is None else _money(difference, currency),
                      'source_url': carrier.source_url, 'advertised_on': carrier.advertised_on,
                      'sentence': _carrier_sentence(carrier, bill, sent, received, numbers, currency)})
    return {**base, 'carriers': views, 'cheapest': cheapest[0].id if cheapest else None,
            'current': {'total': _money(current['micros'], currency), 'complete': current['complete'],
                        'not_priced': current['not_priced'], 'routes': current['routes']},
            'sentence': _summary(cheapest, current, days, currency)}


def _join(names):
    names = list(names)
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]


def _identity(route, preset):
    return (f'sip-{preset}' if preset else 'sip') if route == 'sip' else route


def _yours(faxes, preset):
    return {_identity(fax.route, preset) for fax in faxes}


def _current(found, faxes, preset, country, days, data, currency):
    """The same faxes with the services you use now, priced the same way: each fax at the route it took.

    The numbers are counted as for every other carrier: each route keeps the
    numbers that received on it, and with nothing received the one number
    needed to send is kept on the route that sent the most.
    """
    by_id = {carrier.id: carrier for carrier in found}
    total, unpriced, routes = 0, 0, []
    groups = {}
    for fax in faxes:
        groups.setdefault(_identity(fax.route, preset), []).append(fax)
    numbers = {identity: len({fax.number for fax in items if fax.direction == 'received'})
               for identity, items in groups.items()}
    if groups and not any(numbers.values()):
        busiest = max(sorted(groups), key=lambda identity: sum(1 for fax in groups[identity] if fax.direction == 'sent'))
        numbers[busiest] = 1
    for identity, items in sorted(groups.items()):
        carrier = by_id.get(identity)
        routes.append(carrier.name if carrier is not None else _name(identity))
        if carrier is None or carrier.sending.currency != currency:
            unpriced += len(items)
            continue
        bill = price(carrier, items, country=country, days=days, numbers=numbers[identity], data=data)
        total += bill.total
        unpriced += bill.not_priced + bill.numbers_unpriced
    return {'micros': total, 'complete': not unpriced, 'not_priced': unpriced, 'routes': routes}


def _summary(cheapest, current, days, currency):
    period = f'your last {days} days of faxing'
    if cheapest is None:
        return (f'No carrier with published prices covers all of {period}, so Faxbot cannot name the cheapest; each '
                'figure below leaves out what that carrier does not price.')
    carrier, bill = cheapest
    if not current['complete']:
        return (f'At published prices, {carrier.name} would have cost least for {period}: about '
                f'{_about(bill.total, currency)} (estimate). Faxbot cannot say how much that saves, because some of '
                'what your current services charge is not published.')
    saving = current['micros'] - bill.total
    if saving <= 0:
        return (f'At published prices, no carrier Faxbot knows would have cost less than your current services for '
                f"{period}: about {_about(current['micros'], currency)} (estimate).")
    return (f'At published prices, {carrier.name} would have cost least for {period}: about '
            f"{_about(bill.total, currency)}, {_about(saving, currency)} less than your current services' "
            f"{_about(current['micros'], currency)} (estimate).")
