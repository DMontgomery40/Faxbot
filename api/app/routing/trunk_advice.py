"""Trunk advice (B6): whether one trunk's traffic fits on another, and what moving it would save.

With several trunks, each has its own monthly fee, its own lines and its own
prices. For every trunk this reads, over the last ``WINDOW_DAYS`` days and from
records Faxbot already keeps:

- what it costs a month: its rate card's monthly fee (a card you saved for the
  trunk account or its carrier), else its carrier's published trunk fee
  (``config/rate_cards.json``, with source and date); unknown stays unknown;
- its peak lines in use: the most calls at once, sent and received
  (``sip_call_records.trunk_key``; none is the first trunk);
- the faxes it carried, and what each delivered sent fax cost (settled, else
  reported, else Faxbot's estimate);

and for each pair it asks: had the other trunk's calls and this trunk's calls
all gone over the other trunk, would they have fitted on its lines at every
moment, and what would this trunk's sent faxes have cost there (``pricing``,
origin-rated rows included)? When they fit and moving them saves money, it
says so in one sentence, with what to do about the numbers this trunk receives
on. It is advice only: Faxbot never cancels or changes anything at a carrier.
It can draft the sending rule that moves the traffic; a draft never publishes
itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import sqlalchemy as sa


WINDOW_DAYS = 30
# A call with no recorded end held its line this long at most (capacity.HOLD's reasoning: a long fax call).
LONGEST_CALL = timedelta(minutes=30)
NO_TRUNKS = 'Trunk advice needs two or more trunks; with one, there is nothing to move.'


@dataclass(frozen=True)
class TrunkUse:
    """One trunk's month: fee, lines, faxes and what its delivered sent faxes cost."""
    key: str
    label: str
    lines: int
    peak_lines: int
    sent: int
    received: int
    sent_cost_micros: int | None           # what its delivered sent faxes cost together; None when any is unknown
    currency: str | None
    monthly_micros: int | None             # what the trunk costs a month by itself; None when unknown
    monthly_source: str | None = None      # where that came from: 'card' or the published page's address
    numbers: tuple = ()
    intervals: tuple = field(default=(), repr=False, compare=False)
    faxes: tuple = field(default=(), repr=False, compare=False)   # (destination, pages) of each delivered sent fax


def _tables(engine):
    try:
        from .database import reflect
        return reflect(engine, ('sip_call_records', 'delivery_attempt_costs', 'fax_jobs'))
    except Exception:
        return None


def _monthly(values, trunk, routes):
    """(micros, currency, source) of what a trunk costs a month by itself, or (None, None, None)."""
    preset = getattr(trunk.values, 'sip_trunk_preset', '') or ''
    if routes is not None:
        for identity in dict.fromkeys([trunk.key, f'sip-{preset}' if preset else None]):
            if not identity:
                continue
            try:
                card = next((card for card in routes.current_cards() if card.provider_id == identity
                             and card.direction == 'outbound' and card.monthly_fee_micros is not None), None)
            except Exception:
                card = None
            if card is not None:
                return card.monthly_fee_micros, card.currency, 'card'
    from .predict_facts import _document
    from .costs import InvalidRateCard, parse_amount
    for entry in _document().get('receiving_prices') or ():
        if isinstance(entry, dict) and entry.get('kind') == 'trunk' and entry.get('carrier') == preset:
            try:
                return parse_amount(str(entry.get('monthly_fee', '0')), whole_digits=4), entry.get('currency'), \
                    entry.get('source_url')
            except InvalidRateCard:
                break
    return None, None, None


def _peak(intervals):
    """The most intervals open at once (an interval ends before another starting at the same moment)."""
    events = sorted([(start, 1) for start, _ in intervals] + [(end, -1) for _, end in intervals],
                    key=lambda item: (item[0], item[1]))
    current = peak = 0
    for _, step in events:
        current += step
        peak = max(peak, current)
    return peak


def trunk_uses(values, engine, *, now=None, routes=None) -> list:
    """A ``TrunkUse`` for every trunk account, the first trunk first."""
    from .. import sip_trunk
    from ..capacity import trunk_calls_at_once
    now = now or datetime.utcnow()
    since = now - timedelta(days=WINDOW_DAYS)
    tables = _tables(engine) if engine is not None else None
    found = []
    for trunk in sip_trunk.trunk_accounts(values):
        intervals, sent, received, faxes = [], 0, 0, []
        cost, currency, known = 0, None, True
        if tables is not None:
            records, costs = tables['sip_call_records'], tables['delivery_attempt_costs']
            jobs = tables['fax_jobs']
            on_trunk = (sa.func.coalesce(records.c.trunk_key, 'sip') == trunk.key) if 'trunk_key' in records.c \
                else sa.true()
            with engine.connect() as connection:
                for row in connection.execute(sa.select(
                        records.c.direction, records.c.started_at, records.c.answered_at, records.c.ended_at,
                        records.c.fax_status).where(on_trunk, records.c.started_at >= since)):
                    start = row.answered_at or row.started_at
                    end = row.ended_at or min(start + LONGEST_CALL, now)
                    intervals.append((start, max(end, start)))
                    if row.direction == 'inbound' and row.fax_status == 'SUCCESS':
                        received += 1
                amount = sa.func.coalesce(costs.c.settled_cost_micros, costs.c.reported_cost_micros,
                                          costs.c.estimated_cost_micros)
                for row in connection.execute(sa.select(
                        costs.c.destination, amount.label('amount'),
                        sa.func.coalesce(costs.c.reported_currency, costs.c.currency).label('currency'),
                        jobs.c.pages).select_from(costs.outerjoin(jobs, jobs.c.id == costs.c.job_id))
                        .where(costs.c.route == trunk.key, costs.c.outcome == 'success',
                               costs.c.created_at >= since)):
                    sent += 1
                    faxes.append((row.destination, row.pages or 1))
                    if row.amount is None or (currency and row.currency and row.currency != currency):
                        known = False
                    else:
                        cost += row.amount
                        currency = currency or row.currency
        monthly, monthly_currency, source = _monthly(values, trunk, routes)
        found.append(TrunkUse(trunk.key, trunk.label, trunk_calls_at_once(trunk.values), _peak(intervals), sent,
                              received, cost if known else None, currency or monthly_currency, monthly,
                              source, tuple(trunk.values.sip_trunk_did_list), tuple(intervals), tuple(faxes)))
    return found


def _money(micros, currency):
    from .costs import money_text
    return money_text(micros, currency or 'USD')


def _moved_cost(routes, values, into, faxes):
    """What ``faxes`` would have cost over trunk ``into`` (micros, currency), or (None, None) when any is unknown."""
    from .pricing import price
    total, currency = 0, None
    for destination, pages in faxes:
        try:
            found = price(routes, values, into, destination, pages, provider='sip')
        except Exception:
            return None, None
        if found.micros is None or (currency and found.currency != currency):
            return None, None
        total += found.micros
        currency = found.currency
    return total, currency


def advice(values, engine, *, now=None, routes=None) -> dict:
    """{'trunks': [one row per trunk], 'items': [advice], 'sentence': one sentence when there is nothing to say}."""
    uses = trunk_uses(values, engine, now=now, routes=routes)
    rows = [{'key': use.key, 'label': use.label, 'lines': use.lines, 'peak_lines': use.peak_lines,
             'sent': use.sent, 'received': use.received,
             'monthly': _money(use.monthly_micros, use.currency) if use.monthly_micros is not None else None,
             'monthly_source': use.monthly_source,
             'cost_per_delivered': (_money(use.sent_cost_micros // use.sent, use.currency)
                                    if use.sent and use.sent_cost_micros is not None else None)}
            for use in uses]
    if len(uses) < 2:
        return {'trunks': rows, 'items': [], 'sentence': NO_TRUNKS}
    items = []
    for moving in uses:
        for into in uses:
            if into.key == moving.key or not (moving.sent or moving.received):
                continue
            item = _consider(values, routes, moving, into)
            if item is not None:
                items.append(item)
    # The biggest saving first; one suggestion per trunk to move.
    items.sort(key=lambda item: -(item['_saving'] or 0))
    chosen, seen = [], set()
    for item in items:
        if item['trunk'] not in seen:
            seen.add(item['trunk'])
            chosen.append({key: value for key, value in item.items() if not key.startswith('_')})
    return {'trunks': rows, 'items': chosen,
            'sentence': None if chosen else 'Each trunk carries traffic the others could not take as cheaply.'}


def _consider(values, routes, moving, into):
    """One suggestion to move ``moving``'s faxes to ``into``, or None when they would not fit or save nothing."""
    if _peak(list(into.intervals) + list(moving.intervals)) > into.lines:
        return None
    fee = moving.monthly_micros
    moved, moved_currency = _moved_cost(routes, values, into.key, moving.faxes) if moving.faxes else (0, None)
    if moving.faxes and (moved is None or moving.sent_cost_micros is None):
        per_fax = None
    else:
        per_fax = (moving.sent_cost_micros or 0) - (moved or 0)
    currency = moving.currency or moved_currency
    if fee is None and per_fax is None:
        return None
    if moved_currency and moving.currency and moved_currency != moving.currency:
        return None
    saving = (fee or 0) + (per_fax or 0)
    if saving <= 0:
        return None
    parts = [f'{moving.label} carried {moving.sent + moving.received} '
             f'{"fax" if moving.sent + moving.received == 1 else "faxes"} in the last {WINDOW_DAYS} days']
    if fee is not None:
        parts[0] += f' and costs {_money(fee, currency)} a month by itself'
    sentence = parts[0] + '. '
    sentence += (f'{into.label} had free lines at those times'
                 + (' and charges the same or less for those numbers' if per_fax is not None and per_fax >= 0
                    else '') + '. ')
    sentence += (f'Sending those faxes over {into.label} and cancelling {moving.label} would save about '
                 f'{_money(saving, currency)} a month.')
    if moving.received:
        sentence += (f' Its numbers received {moving.received} '
                     f'{"fax" if moving.received == 1 else "faxes"}: move them to {into.label} at your carrier first.')
    if fee is None:
        sentence += f' Add {moving.label}\'s monthly fee to its rate card to see the whole saving.'
    return {'trunk': moving.key, 'trunk_label': moving.label, 'into': into.key, 'into_label': into.label,
            'faxes': moving.sent + moving.received, 'received': moving.received,
            'saving_per_month': {'micros': saving, 'currency': currency, 'text': _money(saving, currency)},
            'fee_known': fee is not None, 'sentence': sentence,
            # A draft sending limit that keeps faxes off this trunk, so the automatic choice and every rule use the
            # others; it is a draft until you publish it, and Faxbot never cancels the trunk itself.
            'limit_suggestion': {'name': f'No faxes over {moving.label}', 'when': {},
                                 'then': {'never': [moving.key]}},
            '_saving': saving}
