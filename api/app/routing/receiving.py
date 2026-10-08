"""Receiving-side savings from real call history: a shared channel pool, quiet numbers and connections.

Telnyx lets standard local numbers on one account share inbound channels
instead of paying for each received minute; ``config/rate_cards.json`` lists
the channel prices under ``receiving_prices`` with their source and the date
they were read. A call that arrives while every channel is in use hears busy;
it is not billed by the minute instead. Toll-free and international numbers
stay billed by the minute.

The channel-pool advice is a replay of the received calls Faxbot recorded, not
a forecast:

- each received call holds one channel from when it arrived until it ended;
- a channel freed at the same instant another call arrives is free for it;
- a call that finds every channel in use is counted as turned away and never
  retried (the replay adds no new retries).

Numbers join the pool busiest first, by what their calls cost billed by the
minute, and the pool is sized so no call in the first window is turned away.
That choice is judged on the later window only, so the advice is never
fitted to the month it is judged on. Money is integer micros in the price
currency; every figure is an estimate. Faxbot recommends only and never
changes a carrier account.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import heapq
import json
from pathlib import Path

import phonenumbers
from phonenumbers import PhoneNumberType
import sqlalchemy as sa

from .carriers import CarrierChargeStore, carrier_label
from .costs import RateCard, attempt_cost, format_amount, money_text, parse_amount
from .database import read_connection, utcnow
from .numbers import stored_number
from .seed import default_path


WINDOW_DAYS = 30
# A number with this many calls or fewer, received and sent, in the later window is quiet.
QUIET_CALLS = 2
BUSY_WINDOWS_SHOWN = 20
HISTORY_GRACE = timedelta(days=1)
_MONTH_SECONDS = 30 * 86_400
_COUNTRY_NAMES = {'US': 'the US', 'CA': 'Canada', 'GB': 'the UK', 'AU': 'Australia'}


# Shipped prices -------------------------------------------------------------------

def load_receiving_prices(path=None):
    """Every ``receiving_prices`` entry in the shipped price file; an unreadable file gives none."""
    path = Path(path) if path is not None else default_path()
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return [], []
    if not isinstance(document, dict):
        return [], []
    prices = document.get('receiving_prices')
    cards = document.get('cards')
    return ([entry for entry in prices if isinstance(entry, dict)] if isinstance(prices, list) else [],
            [entry for entry in cards if isinstance(entry, dict)] if isinstance(cards, list) else [])


def _date(value):
    try:
        return datetime.strptime(str(value or '')[:10], '%Y-%m-%d')
    except ValueError:
        return None


def _micros(value, whole_digits=4):
    if value in (None, ''):
        return None
    try:
        return parse_amount(str(value), whole_digits=whole_digits)
    except ValueError:
        return None


@dataclass
class CarrierPrices:
    """What one carrier publishes for receiving, read from the shipped price file."""
    carrier: str
    currency: str | None = None
    country: str | None = None
    tiers: tuple = ()  # ((channels in the tier or None for the rest, monthly micros per channel), ...)
    per_minute: dict = field(default_factory=dict)  # number type -> RateCard
    rental: dict = field(default_factory=dict)  # number type -> monthly micros
    trunk_fee: int | None = None
    sources: list = field(default_factory=list)  # [{'label', 'text', 'source_url', 'read_on'}]

    @property
    def channels_priced(self):
        return bool(self.tiers) and self.currency is not None


def carrier_prices(carrier, path=None):
    """The shipped receiving prices for one carrier (a SIP trunk preset such as ``telnyx``)."""
    entries, cards = load_receiving_prices(path)
    prices = CarrierPrices(carrier)
    for entry in entries:
        if entry.get('carrier') != carrier:
            continue
        kind, currency, read_on = entry.get('kind'), str(entry.get('currency') or 'USD').upper(), _date(
            entry.get('advertised_on'))
        source = {'label': entry.get('label'), 'source_url': entry.get('source_url'),
                  'read_on': read_on.date().isoformat() if read_on else None}
        if kind == 'inbound_channel':
            tiers = []
            for tier in entry.get('tiers') or []:
                fee = _micros(tier.get('monthly_fee')) if isinstance(tier, dict) else None
                size = tier.get('channels') if isinstance(tier, dict) else None
                if fee is None or (size is not None and (type(size) is not int or size < 1)):
                    tiers = []
                    break
                tiers.append((size, fee))
            if tiers and tiers[-1][0] is None:
                prices.tiers, prices.currency, prices.country = tuple(tiers), currency, entry.get('country')
                source['text'] = _tier_text(prices.tiers, currency)
                prices.sources.append(source)
        elif kind == 'inbound_per_minute':
            rate = _micros(entry.get('per_minute'), whole_digits=3)
            if rate is None or read_on is None:
                continue
            try:
                card = RateCard(None, f'sip-{carrier}', 'inbound', str(entry.get('label') or carrier)[:100], currency,
                                rate, 0, 0, int(entry.get('billing_increment_seconds') or 60),
                                int(entry.get('minimum_seconds') or 0), entry.get('source_url'), read_on)
            except ValueError:
                continue
            prices.per_minute[entry.get('number_type') or 'local'] = card
            source['text'] = f'{money_text(rate, currency)} a minute'
            prices.sources.append(source)
        elif kind == 'number_rental':
            fee = _micros(entry.get('monthly_fee'))
            if fee is not None:
                prices.rental[entry.get('number_type') or 'local'] = fee
                source['text'] = f'{money_text(fee, currency)} a month'
                prices.sources.append(source)
        elif kind == 'trunk':
            fee = _micros(entry.get('monthly_fee'))
            if fee is not None:
                prices.trunk_fee = fee
                source['text'] = f'{money_text(fee, currency)} a month'
                prices.sources.append(source)
    if 'local' not in prices.rental:
        # Another carrier's local number rental, from its shipped trunk card.
        for card in cards:
            fee = _micros(card.get('number_rental_monthly'))
            if card.get('provider_id') == f'sip-{carrier}' and card.get('direction') == 'inbound' and fee is not None:
                prices.rental['local'] = fee
                prices.currency = prices.currency or str(card.get('currency') or 'USD').upper()
                read_on = _date(card.get('advertised_on'))
                prices.sources.append({'label': f"{carrier_label(carrier)} number", 'source_url': card.get('source_url'),
                                       'read_on': read_on.date().isoformat() if read_on else None,
                                       'text': f"{money_text(fee, prices.currency)} a month"})
                break
    return prices


def _tier_text(tiers, currency):
    """"$12.00 a month each for the first 10, $11.00 for the next 40, ... and $8.00 after 250"."""
    parts, counted = [], 0
    for index, (size, fee) in enumerate(tiers):
        amount = money_text(fee, currency) + (' a month each' if index == 0 else '')
        if size is None:
            parts.append(f'{amount} after {counted}' if counted else amount)
        else:
            parts.append(f"{amount} for the {'first' if index == 0 else 'next'} {size}")
            counted += size
    return ', '.join(parts[:-1]) + ' and ' + parts[-1] if len(parts) > 1 else parts[0]


# The replay (pure) ----------------------------------------------------------------

@dataclass(frozen=True)
class Call:
    """One received call, as the replay sees it: the number it came in on and when it held a channel."""
    id: str
    number: str
    start: datetime
    end: datetime
    metered_micros: int | None = None  # what it cost billed by the minute; None when no price is known


@dataclass
class Replay:
    channels: int | None
    calls: int
    turned_away: list
    peak: int
    busy: list  # [{'start', 'end', 'turned_away', 'numbers'}]: each stretch when every channel was in use


def replay(calls, channels=None):
    """Replay ``calls`` in arrival order through ``channels`` shared channels (None: as many as needed).

    A channel freed at a call's end is released before a call arriving at that
    same instant is admitted; calls arriving together are taken in a fixed order
    (earlier end first, then id). A call that finds every channel in use is
    turned away and not retried.
    """
    ordered = sorted(calls, key=lambda call: (call.start, call.end, call.id))
    ends, turned, busy, current, peak = [], [], [], None, 0
    for call in ordered:
        while ends and ends[0] <= call.start:
            ended = heapq.heappop(ends)
            if current is not None and len(ends) < channels:
                current['end'] = ended
                busy.append(current)
                current = None
        if channels is not None and len(ends) >= channels:
            turned.append(call)
            if current is not None:
                current['turned_away'] += 1
                current['numbers'].add(call.number)
            continue
        heapq.heappush(ends, call.end)
        peak = max(peak, len(ends))
        if channels is not None and current is None and len(ends) >= channels:
            current = {'start': call.start, 'end': None, 'turned_away': 0, 'numbers': set()}
    if current is not None:
        current['end'] = ends[0]
        busy.append(current)
    for window in busy:
        window['numbers'] = sorted(window['numbers'])
    return Replay(channels, len(ordered), turned, peak, busy)


def channel_fee(tiers, channels):
    """Monthly micros for ``channels`` channels, each tier at its own price (the first 10 at the first price...)."""
    total, left = 0, max(0, channels)
    for size, fee in tiers:
        take = left if size is None else min(left, size)
        total += take * fee
        left -= take
        if not left:
            break
    return total


def prorate(monthly_micros, seconds):
    """A monthly amount for a period of ``seconds``: 30 days count one month, rounded up."""
    if not monthly_micros or seconds <= 0:
        return 0
    return -(-monthly_micros * int(seconds) // _MONTH_SECONDS)


def spend(calls):
    """What ``calls`` cost billed by the minute, or None when any of them has no price: unknown is never zero."""
    total = 0
    for call in calls:
        if call.metered_micros is None:
            return None
        total += call.metered_micros
    return total


def unpriced(calls):
    return sum(1 for call in calls if call.metered_micros is None)


def _seconds(calls):
    return sum(int((call.end - call.start).total_seconds()) for call in calls)


def choose_pool(calls_by_number, eligible, tiers, seconds):
    """``(numbers, channels, cost)`` that cost least over a period of ``seconds`` with no call turned away.

    Eligible numbers join busiest first (by what their calls cost billed by the
    minute, then by how long they held a line); the pool has as many channels as
    its calls ever needed at once. ``cost`` is the channels plus what the
    eligible numbers left out still pay by the minute. No pool (every number
    billed by the minute) wins a tie. When any eligible number has a call with
    no price, nothing can be compared: no pool, and ``cost`` is None.
    """
    by_number = {number: spend(calls_by_number.get(number, ())) for number in eligible}
    if any(value is None for value in by_number.values()):
        return (), 0, None
    order = sorted(eligible, key=lambda number: (-by_number[number],
                                                 -_seconds(calls_by_number.get(number, ())), number))
    remaining = sum(by_number.values())
    best, pooled = ((), 0, remaining), []
    for index, number in enumerate(order):
        if by_number[number] <= 0:
            break
        pooled += calls_by_number.get(number, [])
        remaining -= by_number[number]
        channels = replay(pooled).peak
        cost = prorate(channel_fee(tiers, channels), seconds) + remaining
        if cost < best[2]:
            best = (tuple(order[:index + 1]), channels, cost)
    return best


def evaluate(calls_by_number, pool, channels, tiers, seconds):
    """What a chosen pool would have done over a period: turned-away calls, peak, busy stretches and cost."""
    pooled = [call for number in pool for call in calls_by_number.get(number, ())]
    everything = [call for calls in calls_by_number.values() for call in calls]
    result = replay(pooled, channels) if pool else Replay(0, 0, [], 0, [])
    pooled_spend, metered = spend(pooled), spend(everything)
    # Unknown stays unknown: with a call that has no price there is no total to compare.
    still = None if metered is None or pooled_spend is None else metered - pooled_spend
    return {'replay': result, 'needed': replay(pooled).peak if pool else 0,
            'metered': metered, 'still_metered': still,
            'channel_cost': prorate(channel_fee(tiers, channels), seconds) if pool else 0}


# Numbers ---------------------------------------------------------------------------

def number_kind(number, country):
    """``local``, ``toll_free``, ``international`` (outside ``country``) or ``other`` for a stored number."""
    try:
        parsed = phonenumbers.parse(number, None)
    except phonenumbers.NumberParseException:
        return 'other'
    if not phonenumbers.is_valid_number(parsed):
        return 'other'
    kind = phonenumbers.number_type(parsed)
    if kind == PhoneNumberType.TOLL_FREE:
        return 'toll_free'
    if phonenumbers.region_code_for_number(parsed) != country:
        return 'international'
    if kind in (PhoneNumberType.FIXED_LINE, PhoneNumberType.FIXED_LINE_OR_MOBILE):
        return 'local'
    return 'other'


def _reason(kind, prices):
    if kind == 'local':
        return None
    if kind == 'toll_free':
        return 'Toll-free numbers stay billed by the minute.'
    if kind == 'international':
        where = _COUNTRY_NAMES.get(prices.country, prices.country)
        return f"Numbers outside {where} stay billed by the minute, because the shared-line price is for {where}."
    return 'This may not be an ordinary local number, so it stays billed by the minute.'


# History from the database ----------------------------------------------------------

@dataclass
class History:
    first_at: datetime | None
    inbound: list  # [(Call, kind)] in the whole period
    sent: dict  # number -> [start times]
    numbers_seen: set
    no_end: int = 0
    other_carrier: int = 0
    unpriced: int = 0


class ReceivingHistory:
    """Read received and sent trunk calls for one carrier, with what each received call cost."""

    def __init__(self, engine, routes):
        self.engine, self.routes = engine, routes
        self.carriers = CarrierChargeStore(engine)

    def first_call(self, preset, until):
        """When the first call on this carrier's trunk that Faxbot knows of started, before ``until``."""
        calls, records = self.carriers.calls, self.carriers.records
        with read_connection(self.engine) as connection:
            first = connection.scalar(sa.select(sa.func.min(calls.c.started_at)).where(
                calls.c.trunk_preset == preset, calls.c.started_at < until))
            kept = connection.scalar(sa.select(sa.func.min(records.c.started_at)).where(
                records.c.provider_id == preset, records.c.started_at < until))
        found = [moment for moment in (first, kept) if moment is not None]
        return min(found) if found else None

    def _card(self, preset):
        cards = {(card.provider_id, card.direction): card for card in self.routes.current_cards()}
        return cards.get((f'sip-{preset}', 'inbound')) or cards.get(('sip', 'inbound'))

    def read(self, preset, since, until, *, country, prices):
        calls = self.carriers.calls
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(calls).where(
                calls.c.started_at >= since, calls.c.started_at < until)).mappings().all()
            effective = self.carriers.in_effect([row['id'] for row in rows if row['direction'] == 'inbound'],
                                                connection)
            kept = [row for row in self.carriers.unrecorded_in_effect(since=since, connection=connection)
                    if row['started_at'] < until]
        history = History(self.first_call(preset, until), [], {}, set())
        card = self._card(preset)
        currency = prices.currency or (card.currency if card else None)

        def price_card(kind):
            if kind == 'toll_free' and 'toll_free' in prices.per_minute:
                return prices.per_minute['toll_free']
            return card or prices.per_minute.get('local')

        def add_inbound(identity, number, start, end, reported, estimate):
            if not number:
                return
            if end is None:
                history.no_end += 1
                return
            kind = number_kind(number, prices.country or country)
            metered = reported if reported is not None else estimate
            if metered is None:
                history.unpriced += 1
            history.inbound.append((Call(identity, number, start, max(start, end), metered), kind))
            history.numbers_seen.add(number)

        for row in rows:
            if row['trunk_preset'] != preset:
                history.other_carrier += 1
                continue
            if row['direction'] == 'outbound':
                number = stored_number(row['did'] or row['caller'] or '', country=country) or None
                if number:
                    history.sent.setdefault(number, []).append(row['started_at'])
                    history.numbers_seen.add(number)
                continue
            number = stored_number(row['did'] or row['called'] or '', country=country) or None
            end = row['ended_at']
            if end is None and row['connected_seconds'] is not None:
                end = (row['answered_at'] or row['started_at']) + timedelta(seconds=row['connected_seconds'])
            charges = effective.get(row['id'], [])
            reported = None
            if charges and all(charge['currency'] == currency for charge in charges):
                reported = sum(charge['amount_micros'] for charge in charges)
            chosen = price_card(number_kind(number, prices.country or country)) if number else None
            estimate = None
            seconds = row['connected_seconds']
            if seconds is None and row['answered_at'] is not None and row['ended_at'] is not None:
                seconds = int((row['ended_at'] - row['answered_at']).total_seconds())
            if (seconds is None and row['ended_at'] is not None and row['answered_at'] is None
                    and row['disposition'] not in ('answered', 'ambiguous', None)):
                seconds = 0  # ended without being answered: nothing billed by the minute
            # An answered call with no times (the fax engine reports a received fax after its session, with no
            # answer time or length) has an unknown length: unpriced, never zero minutes.
            if chosen is not None and chosen.currency == currency and seconds is not None:
                estimate = attempt_cost(chosen, seconds=seconds, pages=row['pages'],
                                        delivered=row['job_id'] is not None)
            add_inbound(row['id'], number, row['started_at'], end, reported, estimate)
        for row in kept:
            if row['provider_id'] != preset:
                history.other_carrier += 1
                continue
            if row['direction'] == 'outbound':
                number = stored_number(row['calling'] or '', country=country) or None
                if number:
                    history.sent.setdefault(number, []).append(row['started_at'])
                    history.numbers_seen.add(number)
                continue
            end = row['finished_at']
            if end is None and row['call_seconds'] is not None:
                end = (row['answered_at'] or row['started_at']) + timedelta(seconds=row['call_seconds'])
            reported = row['amount_micros'] if row['currency'] == currency else None
            add_inbound(f"kept-{row['id']}", stored_number(row['called'] or '', country=country) or None,
                        row['started_at'], end, reported, None)
        return history


# The report ---------------------------------------------------------------------------

def _money(micros, currency):
    return [] if micros is None or currency is None else [{'currency': currency, 'amount': format_amount(micros)}]


def about(micros, currency):
    """An estimated total for a sentence, to the cent once it reaches a cent: "$0.42", "$12.00", "$0.0032"."""
    if abs(micros) < 10_000:
        return money_text(micros, currency)
    cents = (abs(micros) + 5_000) // 10_000 * 10_000
    return money_text(cents if micros >= 0 else -cents, currency)


def _plural(count, one, many=None):
    return f'{count} {one if count == 1 else (many or one + "s")}'


def _days_text(days):
    return f'the last {days} days'


def covers(first_at, start):
    """True when call history reaches back to ``start``, with a day's grace for the first day's first call."""
    return first_at is not None and first_at <= start + HISTORY_GRACE


def _have_text(first_at, now):
    if first_at is None:
        return 'it has none yet'
    days = max(0, (now - first_at).days)
    return f"it has {'less than a day' if days < 1 else _plural(days, 'day')} so far"


def _channels_text(channels):
    # Telnyx calls a shared line an inbound channel; people read "line".
    return 'one shared line' if channels == 1 else f'{channels} shared lines'


def _sentence_start(text):
    return text[:1].upper() + text[1:]


def _numbers_text(count, carrier):
    return f'one of your {carrier} numbers' if count == 1 else f'{count} of your {carrier} numbers'


def _window(start, end):
    return {'start': start, 'end': end, 'days': round((end - start).total_seconds() / 86_400)}


def _cost_view(evaluation, rental, currency):
    """Money for one window: billed by the minute today against the pool, number rental on both sides."""
    baseline = evaluation['metered'] + rental
    pooled = evaluation['channel_cost'] + evaluation['still_metered'] + rental
    return {'billed_by_the_minute': _money(evaluation['metered'], currency),
            'channels': _money(evaluation['channel_cost'], currency),
            'still_billed_by_the_minute': _money(evaluation['still_metered'], currency),
            'number_rental': _money(rental, currency),
            'total_today': _money(baseline, currency), 'total_with_pool': _money(pooled, currency),
            'difference': _money(baseline - pooled, currency)}


def _rental(numbers, kinds, prices, seconds):
    """Number rental over a period for ``numbers``, and how many have no published price."""
    total, missing = 0, 0
    for number in numbers:
        fee = prices.rental.get(kinds[number])
        if fee is None:
            missing += 1
        else:
            total += prorate(fee, seconds)
    return total, missing


def receiving_report(engine, routes, values, *, now=None, days=WINDOW_DAYS, path=None):
    """Receiving recommendations for the configured carrier trunk; always estimates, never applied."""
    now = (now or utcnow()).replace(microsecond=0)
    check_start, choose_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    seconds = days * 86_400
    preset = (getattr(values, 'sip_trunk_preset', '') or '').strip()
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    prices = carrier_prices(preset, path) if preset else CarrierPrices('')
    carrier = carrier_label(preset) if preset else None
    windows = {'choose': _window(choose_start, check_start), 'check': _window(check_start, now)}
    result = {'days': days, 'estimate': True, 'carrier': carrier, 'windows': windows, 'prices': prices.sources,
              'connections': connections(values, routes, prices, engine, choose_start, now),
              'provider_numbers': provider_numbers(engine, routes, values, now=now, days=days)}
    configured = _configured_numbers(values, country)
    if not preset:
        sentence = 'Faxbot has no phone line from a carrier set up, so there are no received calls to compare.'
        result.update(sentence=sentence, history={'enough': False, 'first_call_at': None, 'days': 0},
                      pool={'state': 'no_trunk', 'sentence': sentence, 'numbers': []},
                      quiet_numbers={'state': 'no_trunk', 'sentence': sentence, 'numbers': [],
                                     'monthly_total': []})
        return result
    history = ReceivingHistory(engine, routes).read(preset, choose_start, now, country=country, prices=prices)
    have = 0 if history.first_at is None else max(0, (now - history.first_at).days)
    result['history'] = {'enough': covers(history.first_at, choose_start),
                         'first_call_at': history.first_at, 'days': have}
    kinds = {number: number_kind(number, prices.country or country)
             for number in set(configured) | history.numbers_seen}
    result['quiet_numbers'] = quiet_numbers(history, configured, kinds, prices, carrier, check_start, now, days)
    result['pool'] = pool_advice(history, kinds, prices, carrier, choose_start, check_start, now, days)
    result['sentence'] = result['pool']['sentence']
    return result


def _configured_numbers(values, country):
    found = [*getattr(values, 'sip_trunk_did_list', ()), getattr(values, 'sip_trunk_caller_id', '') or '']
    return list(dict.fromkeys(number for number in (stored_number(item, country=country) for item in found if item)
                              if number))


def _assumptions(history, carrier, days, prices):
    where = _COUNTRY_NAMES.get(prices.country, prices.country)
    lines = [f'{carrier} calls a shared line an inbound channel: it takes one call at a time, with no charge per '
             'minute.',
             'These figures assume the same calls come in again: each call uses one line from when it arrived until '
             'it ended.',
             'A caller who would have heard a busy signal is counted once, not as calling back.',
             'Only received calls count; sent calls on these numbers stay billed by the minute.',
             f'Numbers outside {where}, Canadian numbers included, stay billed by the minute, because the shared-line '
             f'price is for {where}.',
             f'Faxbot picks the lines from older calls and checks them on {_days_text(days)}, so one unusual month '
             'does not decide the advice.',
             'Callers who never reached Faxbot are not in its history.',
             f'Faxbot only recommends; it never changes your {carrier} account.']
    if history.no_end:
        lines.append(f"{_plural(history.no_end, 'call')} {'is' if history.no_end == 1 else 'are'} not counted "
                     f"because {'its' if history.no_end == 1 else 'their'} end time is missing.")
    if history.other_carrier:
        lines.append(f"{_plural(history.other_carrier, 'call')} that came in through another carrier "
                     f"{'is' if history.other_carrier == 1 else 'are'} not counted.")
    return lines


def pool_advice(history, kinds, prices, carrier, choose_start, check_start, now, days):
    seconds = days * 86_400
    currency = prices.currency
    if not prices.channels_priced:
        sentence = 'Shared lines are a Telnyx option, so Faxbot suggests them only for your Telnyx numbers.'
        return {'state': 'no_channel_price', 'sentence': sentence, 'numbers': []}
    if not covers(history.first_at, choose_start):
        sentence = (f'Faxbot needs {2 * days} days of call history to advise on shared lines; '
                    f'{_have_text(history.first_at, now)}.')
        return {'state': 'too_little_history', 'sentence': sentence, 'numbers': []}
    first, later = {}, {}
    for call, _ in history.inbound:
        (first if call.start < check_start else later).setdefault(call.number, []).append(call)
    numbers = sorted(set(first) | set(later) | set(kinds))
    eligible = [number for number in numbers if kinds.get(number) == 'local']

    def number_rows(pool=()):
        return [{'number': number, 'kind': kinds.get(number, 'other'), 'eligible': kinds.get(number) == 'local',
                 'reason': _reason(kinds.get(number, 'other'), prices), 'in_pool': number in pool,
                 'calls_before': len(first.get(number, [])), 'calls': len(later.get(number, [])),
                 # Empty when a call has no price: unknown, never $0.
                 'billed_by_the_minute': _money(spend(later.get(number, [])), currency),
                 'unpriced_calls': unpriced(first.get(number, [])) + unpriced(later.get(number, []))}
                for number in numbers]
    without_price = [number for number in numbers if unpriced(first.get(number, []) + later.get(number, []))]
    if without_price:
        # Every comparison rests on what each call cost; with calls that have no price there is no saving to state.
        named = (', '.join(without_price[:-1]) + ' and ' + without_price[-1] if 1 < len(without_price) <= 3
                 else without_price[0] if len(without_price) == 1
                 else _numbers_text(len(without_price), carrier))
        return {'state': 'unpriced', 'numbers': number_rows(), 'unpriced_numbers': without_price,
                'sentence': (f'Faxbot has no price for some calls received on {named}, so it cannot compare shared '
                             'lines yet.'),
                'action': f"Add {carrier}'s price for receiving faxes in Costs → Prices & plans.",
                'assumptions': _assumptions(history, carrier, days, prices)}
    pool, channels, _ = choose_pool(first, eligible, prices.tiers, seconds)
    checked = evaluate({number: later.get(number, []) for number in numbers}, pool, channels, prices.tiers, seconds)
    chosen = evaluate({number: first.get(number, []) for number in numbers}, pool, channels, prices.tiers, seconds)
    rental, unpriced_numbers = _rental(numbers, kinds, prices, seconds)
    turned = checked['replay'].turned_away
    later_pool, later_channels, _ = choose_pool(later, eligible, prices.tiers, seconds)
    baseline = checked['metered'] + rental
    pooled_total = checked['channel_cost'] + checked['still_metered'] + rental
    window = _days_text(days)
    note = None
    if not pool:
        state = 'keep_metered'
        local_spend = sum(spend(later.get(number, [])) for number in eligible)
        if not any(later.get(number) for number in eligible):
            sentence = (f'No calls came in on your {carrier} local numbers in {window}, so keep them billed by '
                        'the minute.')
        else:
            sentence = (f'Keep your {carrier} numbers billed by the minute: in {window} their received calls cost '
                        f'about {about(local_spend, currency)}, and one shared line costs '
                        f'{about(channel_fee(prices.tiers, 1), currency)} a month (estimate).')
        if later_pool:
            note = (f'{_sentence_start(window)} alone suggest shared lines would pay off; Faxbot will recommend them '
                    f'if the next {days} days agree.')
    elif turned:
        state = 'turned_away'
        sentence = (f"Don't switch yet: with {_channels_text(channels)} for {_numbers_text(len(pool), carrier)}, "
                    f"{_plural(len(turned), 'caller')} would have heard a busy signal in {window} (estimate).")
    elif pooled_total < baseline:
        state = 'share'
        sentence = (f'Put {_numbers_text(len(pool), carrier)} on {_channels_text(channels)}: in {window} that would '
                    f'have cost about {about(pooled_total, currency)} instead of {about(baseline, currency)}, and '
                    'no caller would have heard busy (estimate).')
    else:
        state = 'not_saving'
        sentence = (f'Keep your {carrier} numbers billed by the minute: the shared lines chosen from the {days} '
                    f'days before would have cost about {about(pooled_total, currency)} in {window}, compared with '
                    f'{about(baseline, currency)} billed by the minute (estimate).')
    rows = number_rows(pool)
    rate = prices.per_minute.get('local')
    break_even = None
    if rate is not None and rate.per_minute_micros:
        fee = channel_fee(prices.tiers, 1)
        minutes = -(-fee // rate.per_minute_micros)
        break_even = (f'One shared line at {about(fee, currency)} a month costs as much as {minutes:,} received '
                      f'minutes at {money_text(rate.per_minute_micros, currency)} a minute.')
    busy = [{'start': stretch['start'], 'end': stretch['end'], 'turned_away': stretch['turned_away'],
             'numbers': stretch['numbers']} for stretch in checked['replay'].busy if stretch['turned_away']]
    return {'state': state, 'sentence': sentence, 'note': note, 'numbers': rows, 'pool_numbers': list(pool),
            'channels': channels, 'calls': sum(len(calls) for calls in later.values()),
            'turned_away': len(turned), 'peak': checked['replay'].peak, 'needed': checked['needed'],
            'busy_windows': busy[:BUSY_WINDOWS_SHOWN], 'busy_windows_total': len(busy),
            'check': _cost_view(checked, rental, currency), 'choose': {
                **_cost_view(chosen, rental, currency), 'calls': sum(len(calls) for calls in first.values()),
                'peak': chosen['replay'].peak, 'turned_away': len(chosen['replay'].turned_away)},
            'later_window_alone': {'pool_numbers': list(later_pool), 'channels': later_channels},
            'numbers_without_rental_price': unpriced_numbers, 'break_even': break_even,
            'assumptions': _assumptions(history, carrier, days, prices)}


def quiet_numbers(history, configured, kinds, prices, carrier, check_start, now, days):
    """Numbers with few or no calls in the later window, with what each costs to keep a month."""
    currency = prices.currency
    window = _days_text(days)
    if not covers(history.first_at, check_start):
        sentence = (f'Faxbot needs {days} days of call history to tell which numbers are quiet; '
                    f'{_have_text(history.first_at, now)}.')
        return {'state': 'too_little_history', 'sentence': sentence, 'numbers': [], 'monthly_total': [],
                'numbers_without_price': 0, 'most_calls': QUIET_CALLS}
    received, sent = {}, {}
    for call, _ in history.inbound:
        if call.start >= check_start:
            received[call.number] = received.get(call.number, 0) + 1
    for number, starts in history.sent.items():
        sent[number] = sum(1 for start in starts if start >= check_start)
    numbers = sorted(set(configured) | history.numbers_seen)
    rows, total, missing = [], 0, 0
    for number in numbers:
        calls = received.get(number, 0) + sent.get(number, 0)
        if calls > QUIET_CALLS:
            continue
        fee = prices.rental.get(kinds.get(number))
        if fee is None:
            missing += 1
        else:
            total += fee
        rows.append({'number': number, 'received': received.get(number, 0), 'sent': sent.get(number, 0),
                     'monthly_rental': _money(fee, currency), 'question': still_published(number)})
    if not rows:
        sentence = (f"Every one of your {carrier} numbers had more than {_plural(QUIET_CALLS, 'call')} in {window}, "
                    'so none is quiet.')
    else:
        many = len(rows) > 1
        sentence = (f"{_sentence_start(_numbers_text(len(rows), carrier))} had {_plural(QUIET_CALLS, 'call')} or fewer "
                    f'in {window}')
        if total:
            sentence += (f"; {'together they cost' if many else 'it costs'} about {about(total, currency)} a month "
                         'to keep (estimate)')
        sentence += '. Check that no one still faxes a number before you give it up.'
        if missing:
            sentence += (f" Faxbot has no rental price for {_plural(missing, 'of them', 'of them')}, so "
                         f"{'it is' if missing == 1 else 'they are'} not in that total." if total else '')
    return {'state': 'quiet' if rows else 'none_quiet', 'sentence': sentence, 'numbers': rows,
            'monthly_total': _money(total, currency) if rows else [], 'numbers_without_price': missing,
            'most_calls': QUIET_CALLS}


def connections(values, routes, prices, engine, since, now):
    """The fax connections in use with their monthly fees, and what keeping one would cost."""
    preset = (getattr(values, 'sip_trunk_preset', '') or '').strip()
    sending = [getattr(values, 'effective_outbound', '') or '', *getattr(values, 'outbound_route_providers', ())]
    # Every provider with an account that receives (accounts.receiving_accounts), the receiving provider first.
    receiving = [getattr(values, 'effective_inbound', '') or ''] if getattr(values, 'inbound_enabled', False) else []
    if receiving:
        from ..accounts import receiving_accounts
        try:
            receiving += [account.provider for account in receiving_accounts(values)]
        except Exception:
            pass  # values without provider accounts (a fixture): the receiving provider alone, as before
    in_use = [provider for provider in dict.fromkeys(sending + receiving) if provider]
    # A provider's monthly fee is the one on its rate cards; a provider with cards but no fee charges none.
    fees = {}
    for card in routes.current_cards():
        fee, _ = fees.get(card.provider_id, (0, card.currency))
        fees[card.provider_id] = (max(fee, card.monthly_fee_micros or 0), card.currency)
    from .plan import route_label

    def trunk(carrier, published):
        """A carrier trunk's monthly fee: the published one, else its rate cards' (none on them is no fee)."""
        if published.trunk_fee is not None:
            return published.trunk_fee, published.currency
        return fees.get(f'sip-{carrier}') or (fees.get('sip') if carrier == preset else None) or (None, None)

    items = []
    if 'sip' in in_use and preset:
        fee, currency = trunk(preset, prices)
        items.append({'name': carrier_label(preset), 'kind': 'trunk', 'monthly_fee': _money(fee, currency),
                      '_fee': fee, '_currency': currency})
    for provider in in_use:
        if provider == 'sip':
            continue
        fee, currency = fees.get(provider, (None, None))
        items.append({'name': route_label(provider), 'kind': 'provider', 'monthly_fee': _money(fee, currency),
                      '_fee': fee, '_currency': currency})
    calls = CarrierChargeStore(engine).calls
    with read_connection(engine) as connection:
        seen = connection.execute(sa.select(calls.c.trunk_preset).where(
            calls.c.started_at >= since, calls.c.started_at < now, calls.c.trunk_preset.is_not(None),
            calls.c.trunk_preset != preset).distinct()).scalars().all()
    for other in sorted(seen):
        fee, currency = trunk(other, carrier_prices(other))
        items.append({'name': carrier_label(other), 'kind': 'trunk', 'monthly_fee': _money(fee, currency),
                      '_fee': fee, '_currency': currency})
    sentence = _connections_sentence(items)
    return {'sentence': sentence, 'items': [{key: value for key, value in item.items() if not key.startswith('_')}
                                            for item in items]}


def _connections_sentence(items):
    if not items:
        return 'No fax service is set up yet.'
    if len(items) == 1:
        return f"{items[0]['name']} is your only fax service, so there is no second monthly fee to save."
    known = [item for item in items if item['_fee'] is not None]
    unknown = [item['name'] for item in items if item['_fee'] is None]
    currencies = {item['_currency'] for item in known if item['_fee']}
    if len(unknown) == 1:
        missing = f" Faxbot does not know {unknown[0]}'s monthly fee yet; enter it in Costs → Prices & plans."
    else:
        missing = (f" Faxbot does not know the monthly fees of {' and '.join(unknown)} yet; enter them in "
                   'Costs → Prices & plans.' if unknown else '')
    if len(currencies) > 1:
        return f'Your {len(items)} fax services charge monthly fees in different currencies.' + missing
    total = sum(item['_fee'] for item in known)
    if not total and unknown:
        return (f"{' and '.join(item['name'] for item in known)} "
                f"{'has' if len(known) == 1 else 'have'} no monthly fee." + missing) if known else missing.strip()
    if not total:
        return f'None of your {len(items)} fax services has a monthly fee, so combining them would save no fixed fees.'
    currency = currencies.pop()
    cheapest = min(known, key=lambda item: item['_fee'])
    return (f"Your {len(items)} fax services cost {about(total, currency)} a month in fixed fees (estimate). "
            f"Keeping only {cheapest['name']} would cost {about(cheapest['_fee'], currency)} a month, "
            f"{about(total - cheapest['_fee'], currency)} less, if it can carry all your numbers and calls; keep "
            'a second service if you need a backup.' + missing)


# Numbers at other fax services ----------------------------------------------------------

# The fax services whose own number Faxbot knows from its settings: (provider, setting holding the number).
PROVIDER_NUMBERS = (('humblefax', 'humblefax_from_number'), ('efax', 'efax_caller_id'))


def shown_number(number):
    """+13035550100 as +1 303-555-0100, as people read it."""
    try:
        return phonenumbers.format_number(phonenumbers.parse(number, None),
                                          phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return number


def still_published(number):
    """The question to answer before giving up a number: people who still have it on file keep faxing it."""
    return (f'Is {shown_number(number)} still printed on your letterhead, forms or website, or listed anywhere? '
            'If it is, keep it.')


def provider_numbers(engine, routes, values, *, now=None, days=WINDOW_DAYS):
    """Each fax service number Faxbot knows of (HumbleFax, eFax), with its faxes in the last ``days``.

    Received faxes are counted from Faxbot's received faxes by the service that
    brought them in, and sent faxes from the attempts that went by it. A number is
    quiet with ``QUIET_CALLS`` faxes or fewer. Its monthly cost is the service's
    plan fee from its rate card: a plan fee, never called number rental. Advice
    only: Faxbot never cancels a plan or releases a number.
    """
    from .database import reflect
    from .plan import route_label
    now = (now or utcnow()).replace(microsecond=0)
    since = now - timedelta(days=days)
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    wanted = []
    for provider, setting in PROVIDER_NUMBERS:
        number = stored_number(getattr(values, setting, '') or '', country=country)
        if number:
            wanted.append((provider, number))
    if not wanted:
        return {'state': 'none', 'numbers': [], 'most_faxes': QUIET_CALLS,
                'sentence': 'Faxbot knows no fax service number besides your carrier line.'}
    tables = reflect(engine, ('inbound_faxes', 'delivery_attempt_costs'))
    inbound, costs = tables['inbound_faxes'], tables['delivery_attempt_costs']
    fees = {card.provider_id: card for card in routes.current_cards() if card.monthly_fee_micros}
    rows = []
    with read_connection(engine) as connection:
        for provider, number in wanted:
            received = connection.scalar(sa.select(sa.func.count()).select_from(inbound).where(
                inbound.c.backend == provider, inbound.c.created_at >= since, inbound.c.created_at < now))
            sent = connection.scalar(sa.select(sa.func.count(sa.distinct(costs.c.job_id))).where(
                costs.c.provider_id == provider, costs.c.created_at >= since, costs.c.created_at < now))
            first = [moment for moment in (
                connection.scalar(sa.select(sa.func.min(inbound.c.created_at)).where(inbound.c.backend == provider)),
                connection.scalar(sa.select(sa.func.min(costs.c.created_at)).where(costs.c.provider_id == provider)))
                if moment is not None]
            rows.append(_provider_row(provider, route_label(provider), number, received, sent,
                                      min(first) if first else None, fees.get(provider), since, now, days))
    quiet = [row for row in rows if row['quiet']]
    if quiet:
        state = 'quiet'
        sentence = (f"{_sentence_start(_plural(len(quiet), 'fax service number'))} had "
                    f"{_plural(QUIET_CALLS, 'fax', 'faxes')} or fewer in {_days_text(days)}. Before you give one up, "
                    'check that it is not still printed or published anywhere.')
    elif all(row['enough_history'] for row in rows):
        state = 'none_quiet'
        sentence = (f"Every fax service number had more than {_plural(QUIET_CALLS, 'fax', 'faxes')} in "
                    f'{_days_text(days)}, so none is quiet.')
    else:
        state = 'too_little_history'
        sentence = rows[0]['sentence'] if len(rows) == 1 else (
            f'Faxbot needs {days} days of history at each fax service to tell which numbers are quiet.')
    return {'state': state, 'sentence': sentence, 'numbers': rows, 'most_faxes': QUIET_CALLS}


def _provider_row(provider, name, number, received, sent, first, card, since, now, days):
    enough = first is not None and first <= since + HISTORY_GRACE
    quiet = enough and received + sent <= QUIET_CALLS
    total = received + sent
    faxes = f"{total} {'fax' if total == 1 else 'faxes'}"
    if not enough:
        sentence = (f'Faxbot needs {days} days of {name} history to tell whether {shown_number(number)} is quiet; '
                    f'{_have_text(first, now)}.')
    elif quiet:
        fee = (f' Your {name} plan costs {about(card.monthly_fee_micros, card.currency)} a month; that is what giving '
               'it up would save, if nothing else uses the plan (estimate).' if card else
               f' Faxbot has no price for your {name} plan; enter it in Costs → Prices & plans.')
        sentence = (f'{name} number {shown_number(number)} had {faxes} in the last {days} days, {received} received '
                    f'and {sent} sent.{fee}')
    else:
        sentence = f'{name} number {shown_number(number)} had {faxes} in the last {days} days, so it is in use.'
    return {'provider': provider, 'name': name, 'number': number, 'received': received, 'sent': sent,
            'enough_history': enough, 'quiet': quiet,
            'plan_fee': _money(card.monthly_fee_micros, card.currency) if card else [],
            'question': still_published(number) if quiet else None, 'sentence': sentence}
