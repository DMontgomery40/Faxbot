"""Dated exchange rates, so route prices in different currencies can be compared (brief 99, item 1).

Stored amounts never change currency: a GBP rate card stays in GBP, a fax's estimate stays in the currency it was
priced in. Only the comparison converts: route choice (``RoutePlanner.plan``) asks ``for_ranking`` for the rates
between the currencies its routes charge in, ranks with them (``RoutePolicy.order(exchange=...)``), and every
place that shows a converted amount shows the original amount and currency and how it was compared ("0.70 GBP,
about 0.88 USD at the ECB rate of 9 October 2026"). Prices are compared in the currency of the first priced
route, which is the fax's own account unless a rule or preference put another first.

Where the rates come from:

- **The ECB's euro reference rates, on by default.** The European Central Bank publishes about 30 currencies
  against the euro at ``ECB_DAILY``, "usually updated at around 16:00 CET every working day, except on TARGET
  closing days" (``ECB_PAGE``, read 10 October 2026). They are "published for information purposes only" and the
  ECB discourages using them for transactions; Faxbot uses them only to compare prices, never to bill. The ECB's
  terms (``ECB_TERMS``, read 10 October 2026) let anyone "make free use of the information" if it "appear[s]
  accurately and the ECB [is] cited as the source": every sentence that uses one names the ECB and its date. The
  routing background task reads them every ``FETCH_HOURS`` hours (``refresh_step``), an hour after a failure, and
  keeps each rate with its date. A rate between two other currencies goes through the euro (USD to GBP is GBP per
  euro over USD per euro), dated the day both legs are from.
- **Your own rate**, such as the rate on your invoice: one dated rate per currency pair, either direction. It
  overrides the reference while it is current; clearing it adds a row, so the history stays.

How old a rate may be: ``REFERENCE_MAX_AGE_DAYS`` (7) for the reference. The ECB publishes nothing on weekends and
TARGET holidays, so its newest rate is normally up to three days old, and at Easter (Good Friday and Easter
Monday) Thursday's rate is the newest until Tuesday afternoon, five days later; seven days covers that with two
days to spare for a missed fetch, and reference rates between major currencies rarely move more than a few
percent in a week, well inside the gaps that decide between fax routes. ``OWN_MAX_AGE_DAYS`` (35) for your own
rate: invoices come monthly, so the rate on the latest one is up to a 31-day billing cycle old, plus four days for
it to arrive. With no current rate between two currencies, ranking falls back to comparing money only within each
currency, with your order between currencies, and says why in one sentence (``for_ranking``'s ``note``).

Only route ranking converts. Cost caps, a scarce plan's held pages and the observed cost per delivered fax still
compare money only within one currency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
import re
import uuid
import xml.etree.ElementTree as ET

import sqlalchemy as sa


ECB_DAILY = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
ECB_PAGE = ('https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/'
            'index.en.html')
ECB_TERMS = 'https://www.ecb.europa.eu/services/disclaimer/html/index.en.html'
READ_ON = '2026-10-10'
EURO = 'EUR'
REFERENCE_MAX_AGE_DAYS = 7
OWN_MAX_AGE_DAYS = 35
FETCH_HOURS = 6
RETRY_HOURS = 1
FETCH_TIMEOUT = 15.0
MAX_BODY = 256 * 1024
_CURRENCY = re.compile(r'[A-Z]{3}')
_ECB_NS = {'gesmes': 'http://www.gesmes.org/xml/2002-08-01',
           'ecb': 'http://www.ecb.int/vocabulary/2002-08-01/eurofxref'}
SOURCE_NAMES = {'ecb': 'the ECB', 'own': 'your'}


class RateError(ValueError):
    """A rate cannot be stored as asked; one plain sentence."""


class FetchError(RuntimeError):
    """The reference rates could not be read; one plain sentence."""


def utcnow():
    # Microseconds kept: the newest row of a setting or a pair counts, so two changes in one second keep their order.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def long_date(day) -> str:
    """'9 October 2026'."""
    day = day.date() if isinstance(day, datetime) else day
    return f'{day.day} {day:%B %Y}'


def amount_text(micros, currency) -> str:
    """'0.84 USD': two decimal places, or up to four under ten cents, always with its currency code (amounts in
    several currencies are shown side by side here, so a dollar sign alone would be ambiguous)."""
    from .costs import MICROS
    amount = Decimal(abs(int(micros))) / MICROS
    places = 4 if amount and amount < Decimal('0.1') else 2
    shown = f"{amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP):f}"
    while places > 2 and shown.endswith('0'):
        shown, places = shown[:-1], places - 1
    return f"{'-' if micros < 0 else ''}{shown} {currency}"


def rate_text(rate: Decimal) -> str:
    """A rate as people read it: at most six significant decimals, no trailing zeros ('1.1206', '0.0063')."""
    if rate >= 1:
        shown = f'{rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP):f}'
    else:
        shown = f'{rate:.6g}' if rate else '0'
    return shown.rstrip('0').rstrip('.') if '.' in shown else shown


@dataclass(frozen=True)
class Conversion:
    """``rate`` units of ``quote`` for one ``base``, as of ``day``, from ``source`` ('ecb' or 'own')."""
    base: str
    quote: str
    rate: Decimal
    day: date
    source: str

    def phrase(self) -> str:
        """'the ECB rate of 9 October 2026' or 'your rate of 30 September 2026'."""
        return f'{SOURCE_NAMES.get(self.source, "the")} rate of {long_date(self.day)}'

    def convert(self, micros) -> int:
        """``micros`` of ``base`` in ``quote`` micros (for comparing only; never stored as a price)."""
        return int((Decimal(int(micros)) * self.rate).to_integral_value(rounding=ROUND_HALF_UP))

    def inverse(self) -> 'Conversion':
        return Conversion(self.quote, self.base, Decimal(1) / self.rate, self.day, self.source)


@dataclass(frozen=True)
class Ranking:
    """What route choice needs: ``rates`` {(from, to): float} for ``RoutePolicy.order``, the ``conversions`` that
    made them, and ``note`` (one clause) when some currency could not be compared."""
    rates: dict = field(default_factory=dict)
    conversions: dict = field(default_factory=dict)
    note: str | None = None


# -- reading -----------------------------------------------------------------------------------------------------

def _table(engine, name):
    return sa.Table(name, sa.MetaData(), autoload_with=engine)


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    return value


def reference_enabled(engine) -> bool:
    """Whether Faxbot reads the reference rates: on unless you turned it off (newest setting counts)."""
    table = _table(engine, 'exchange_rate_settings')
    with engine.connect() as connection:
        value = connection.execute(sa.select(table.c.reference_enabled).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).scalar()
    return True if value is None else bool(value)


def reference_rates(engine):
    """``(day, {currency: Decimal per euro})`` of the newest reference rates stored, or ``(None, {})``."""
    table = _table(engine, 'exchange_rates')
    with engine.connect() as connection:
        newest = connection.execute(sa.select(sa.func.max(table.c.rate_date)).where(
            table.c.source == 'ecb', table.c.state == 'active')).scalar()
        if newest is None:
            return None, {}
        rows = connection.execute(sa.select(table.c.quote, table.c.rate).where(
            table.c.source == 'ecb', table.c.state == 'active', table.c.rate_date == newest)
            .order_by(table.c.created_at, table.c.id)).all()
    return _day(newest), {quote: Decimal(rate) for quote, rate in rows if rate}


def own_rates(engine) -> dict:
    """{(base, quote): row} for the newest own rate of each pair still set (a cleared pair is left out)."""
    table = _table(engine, 'exchange_rates')
    with engine.connect() as connection:
        rows = connection.execute(sa.select(table).where(table.c.source == 'own').order_by(
            table.c.created_at, table.c.id)).mappings().all()
    found = {}
    for row in rows:
        key = tuple(sorted((row['base'], row['quote'])))
        found[key] = dict(row)
    return {(row['base'], row['quote']): row for row in found.values() if row['state'] == 'active'}


def _own_conversion(own, base, quote):
    row = own.get((base, quote))
    if row is not None:
        return Conversion(base, quote, Decimal(row['rate']), _day(row['rate_date']), 'own')
    row = own.get((quote, base))
    if row is not None:
        return Conversion(quote, base, Decimal(row['rate']), _day(row['rate_date']), 'own').inverse()
    return None


def _reference_conversion(day, rates, base, quote):
    if day is None:
        return None
    per_euro = {**rates, EURO: Decimal(1)}
    if base not in per_euro or quote not in per_euro or not per_euro[base]:
        return None
    return Conversion(base, quote, per_euro[quote] / per_euro[base], day, 'ecb')


def conversion(engine, base, quote, *, today=None, own=None, reference=None):
    """``(Conversion or None, clause)``: the rate to compare ``base`` amounts in ``quote``.

    Your own rate while it is no older than ``OWN_MAX_AGE_DAYS``, else the reference rate while it is no older than
    ``REFERENCE_MAX_AGE_DAYS``. Without one, the clause says why ("no exchange rate between GBP and USD is set",
    "the ECB rate between GBP and USD is from 1 October 2026, more than 7 days old")."""
    today = today or utcnow().date()
    if base == quote:
        return Conversion(base, quote, Decimal(1), today, 'own'), None
    own = own_rates(engine) if own is None else own
    day, rates = reference_rates(engine) if reference is None else reference
    mine = _own_conversion(own, base, quote)
    if mine is not None and (today - mine.day).days <= OWN_MAX_AGE_DAYS:
        return mine, None
    ecb = _reference_conversion(day, rates, base, quote)
    if ecb is not None and (today - ecb.day).days <= REFERENCE_MAX_AGE_DAYS:
        return ecb, None
    pair = f'{base} and {quote}'
    if mine is not None and ecb is not None:
        newest = max(mine.day, ecb.day)
        return None, (f'the newest exchange rate between {pair} is from {long_date(newest)}, too old to compare '
                      'prices with')
    if mine is not None:
        return None, (f'your exchange rate between {pair} is from {long_date(mine.day)}, more than '
                      f'{OWN_MAX_AGE_DAYS} days old')
    if ecb is not None:
        return None, (f'the ECB rate between {pair} is from {long_date(ecb.day)}, more than '
                      f'{REFERENCE_MAX_AGE_DAYS} days old')
    return None, f'no exchange rate between {pair} is set'


def for_ranking(engine, currencies, *, today=None) -> Ranking:
    """The rates route choice may rank ``currencies`` with: every ordered pair that has a current rate."""
    currencies = sorted({currency for currency in currencies if currency})
    if len(currencies) < 2:
        return Ranking()
    own, reference = own_rates(engine), reference_rates(engine)
    rates, conversions, notes = {}, {}, []
    for index, base in enumerate(currencies):
        for quote in currencies[index + 1:]:
            found, clause = conversion(engine, base, quote, today=today, own=own, reference=reference)
            if found is None:
                notes.append(clause)
                continue
            conversions[(base, quote)], conversions[(quote, base)] = found, found.inverse()
            rates[(base, quote)], rates[(quote, base)] = float(found.rate), float(found.inverse().rate)
    return Ranking(rates, conversions, notes[0] if notes else None)


# -- your own rates ------------------------------------------------------------------------------------------------

def _currency(value, what):
    value = (value or '').strip().upper()
    if not _CURRENCY.fullmatch(value):
        raise RateError(f'Enter the {what} currency as its three-letter code, such as USD or GBP.')
    return value


def set_own(engine, base, quote, rate, *, rate_date=None, note=None, actor_id=None, actor_name=None, now=None):
    """Store your rate: ``rate`` units of ``quote`` for one ``base``, as of ``rate_date`` (today when not given)."""
    base, quote = _currency(base, 'first'), _currency(quote, 'second')
    if base == quote:
        raise RateError('Choose two different currencies.')
    try:
        value = Decimal(str(rate).strip())
    except (InvalidOperation, ValueError):
        raise RateError('Enter the rate as a number, such as 1.27.') from None
    if not value.is_finite() or value <= 0 or value > Decimal('1000000'):
        raise RateError('Enter a rate above zero and at most 1,000,000.')
    now = now or utcnow()
    day = rate_date or now.date()
    if isinstance(day, str):
        try:
            day = date.fromisoformat(day.strip())
        except ValueError:
            raise RateError('Enter the date the rate is for as year-month-day, such as 2026-10-09.') from None
    day = _day(day)
    if day > now.date() + timedelta(days=1):
        raise RateError('The rate\'s date cannot be in the future.')
    row = {'id': uuid.uuid4().hex, 'base': base, 'quote': quote, 'rate': format(value.normalize(), 'f')[:32],
           'rate_date': datetime(day.year, day.month, day.day), 'source': 'own', 'state': 'active',
           'source_url': None, 'note': (note or '').strip()[:500] or None, 'created_at': now,
           'created_by': (str(actor_id)[:40] if actor_id else None),
           'created_by_name': (str(actor_name)[:200] if actor_name else None)}
    with engine.begin() as connection:
        connection.execute(_table(engine, 'exchange_rates').insert().values(**row))
    return row


def clear_own(engine, base, quote, *, actor_id=None, actor_name=None, now=None) -> bool:
    """Withdraw your rate for a pair (either direction); a new row keeps the history. False when none was set."""
    base, quote = _currency(base, 'first'), _currency(quote, 'second')
    own = own_rates(engine)
    current = own.get((base, quote)) or own.get((quote, base))
    if current is None:
        return False
    now = now or utcnow()
    with engine.begin() as connection:
        connection.execute(_table(engine, 'exchange_rates').insert().values(
            id=uuid.uuid4().hex, base=current['base'], quote=current['quote'], rate=None, rate_date=None,
            source='own', state='cleared', source_url=None, note=None, created_at=now,
            created_by=(str(actor_id)[:40] if actor_id else None),
            created_by_name=(str(actor_name)[:200] if actor_name else None)))
    return True


def set_reference(engine, enabled, *, actor_id=None, actor_name=None, now=None):
    with engine.begin() as connection:
        connection.execute(_table(engine, 'exchange_rate_settings').insert().values(
            id=uuid.uuid4().hex, reference_enabled=int(bool(enabled)), created_at=now or utcnow(),
            created_by=(str(actor_id)[:40] if actor_id else None),
            created_by_name=(str(actor_name)[:200] if actor_name else None)))


# -- the reference rates -------------------------------------------------------------------------------------------

def parse_ecb(body: bytes):
    """``(day, {currency: Decimal per euro})`` from the ECB's daily XML. Raises FetchError."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise FetchError('The ECB answered with something that is not its rate file.') from None
    dated = root.find('ecb:Cube/ecb:Cube[@time]', _ECB_NS)
    if dated is None:
        raise FetchError('The ECB\'s rate file has no dated rates.')
    try:
        day = date.fromisoformat(dated.get('time'))
    except (TypeError, ValueError):
        raise FetchError('The ECB\'s rate file has a date Faxbot does not read.') from None
    rates = {}
    for item in dated.findall('ecb:Cube', _ECB_NS):
        currency, value = item.get('currency') or '', item.get('rate') or ''
        try:
            rate = Decimal(value)
        except InvalidOperation:
            continue
        if _CURRENCY.fullmatch(currency) and rate.is_finite() and rate > 0:
            rates[currency] = rate
    if not rates:
        raise FetchError('The ECB\'s rate file lists no rates.')
    return day, rates


def fetch_ecb(timeout=FETCH_TIMEOUT) -> bytes:
    """The ECB's daily rate file. Raises FetchError with one sentence."""
    import httpx
    try:
        response = httpx.get(ECB_DAILY, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.TimeoutException:
        raise FetchError('The ECB did not answer in time.') from None
    except httpx.HTTPStatusError as error:
        raise FetchError(f'The ECB answered with an error ({error.response.status_code}).') from None
    except httpx.HTTPError:
        raise FetchError('Faxbot could not reach the ECB from this server.') from None
    if len(response.content) > MAX_BODY:
        raise FetchError('The ECB\'s rate file was larger than expected, so Faxbot did not read it.')
    return response.content


def _record_fetch(engine, outcome, *, now, day=None, count=None, error=None):
    with engine.begin() as connection:
        connection.execute(_table(engine, 'exchange_rate_fetches').insert().values(
            id=uuid.uuid4().hex, source='ecb', attempted_at=now, outcome=outcome,
            rate_date=datetime(day.year, day.month, day.day) if day else None, rates=count,
            error=(error or '')[:500] or None))


def refresh(engine, *, fetch=None, now=None) -> dict:
    """Read the reference rates now and store any new day of them. ``{'outcome', 'day', 'rates', 'error'}``; a
    failure is recorded with its reason and the rates already stored stay in use."""
    now = now or utcnow()
    try:
        day, rates = parse_ecb((fetch or fetch_ecb)())
    except FetchError as error:
        _record_fetch(engine, 'failed', now=now, error=str(error))
        logging.getLogger(__name__).warning('Exchange rates were not refreshed: %s', error)
        return {'outcome': 'failed', 'day': None, 'rates': 0, 'error': str(error)}
    stored_day, _ = reference_rates(engine)
    if stored_day is not None and stored_day >= day:
        _record_fetch(engine, 'unchanged', now=now, day=day, count=len(rates))
        return {'outcome': 'unchanged', 'day': day, 'rates': len(rates), 'error': None}
    table = _table(engine, 'exchange_rates')
    with engine.begin() as connection:
        for currency, rate in sorted(rates.items()):
            connection.execute(table.insert().values(
                id=uuid.uuid4().hex, base=EURO, quote=currency, rate=format(rate, 'f')[:32],
                rate_date=datetime(day.year, day.month, day.day), source='ecb', state='active', source_url=ECB_DAILY,
                note=None, created_at=now, created_by=None, created_by_name=None))
    _record_fetch(engine, 'stored', now=now, day=day, count=len(rates))
    return {'outcome': 'stored', 'day': day, 'rates': len(rates), 'error': None}


def last_fetch(engine):
    table = _table(engine, 'exchange_rate_fetches')
    with engine.connect() as connection:
        row = connection.execute(sa.select(table).order_by(table.c.attempted_at.desc(), table.c.id.desc())
                                 .limit(1)).mappings().one_or_none()
    return dict(row) if row else None


def due(engine, now=None) -> bool:
    """Whether the background task should read the reference rates now: on, and the last read is older than
    ``FETCH_HOURS`` (``RETRY_HOURS`` after a failure)."""
    if not reference_enabled(engine):
        return False
    now = now or utcnow()
    last = last_fetch(engine)
    if last is None:
        return True
    wait = RETRY_HOURS if last['outcome'] == 'failed' else FETCH_HOURS
    return now - last['attempted_at'] >= timedelta(hours=wait)


def refresh_step(engine, *, fetch=None, now=None) -> bool:
    """The routing background task's step: read the reference rates when ``due``. Never busy-loops."""
    if due(engine, now):
        refresh(engine, fetch=fetch, now=now)
    return False


# -- what an attempt compared, for Sent ----------------------------------------------------------------------------

def _decimal_text(micros):
    from .costs import format_amount
    return format_amount(int(micros))


def record_uses(engine, *, attempt_id, job_id, choices, now=None) -> int:
    """Keep how an attempt's routes were compared across currencies (``exchange_rate_uses``): each route's own
    amount and the amount it was compared as, with the rate. Once per attempt; returns the rows written."""
    rows = [choice for choice in choices if choice.compared_in is not None and choice.estimated_cost_micros
            is not None and choice.currency and choice.compared_as is not None]
    if not rows:
        return 0
    table = _table(engine, 'exchange_rate_uses')
    now = now or utcnow()
    with engine.begin() as connection:
        if connection.execute(sa.select(table.c.id).where(table.c.attempt_id == attempt_id).limit(1)).first():
            return 0
        for choice in rows:
            converted = choice.currency != choice.compared_in
            rate = choice.conversion if converted else None
            connection.execute(table.insert().values(
                id=uuid.uuid4().hex, attempt_id=attempt_id, job_id=job_id, route_key=str(choice.route.key)[:64],
                amount=_decimal_text(choice.estimated_cost_micros), currency=choice.currency,
                compared_amount=_decimal_text(choice.compared_as), compared_in=choice.compared_in,
                rate=format(rate.rate, 'f')[:32] if rate is not None else None,
                rate_date=datetime(rate.day.year, rate.day.month, rate.day.day) if rate is not None else None,
                source=rate.source if rate is not None else None, created_at=now))
    return len(rows)


def sent_sentence(engine, job_id, route) -> str | None:
    """Sent details for a fax whose route was the cheapest once prices in different currencies were converted:
    "The cheapest route then, with prices in different currencies compared: 0.21 USD, about 0.16 GBP at the ECB
    rate of 9 October 2026, against 0.24 GBP by Phaxio." From the newest attempt that compared currencies; None
    when none did."""
    from .costs import parse_amount
    from .plan import ledger_key, route_label
    try:
        table = _table(engine, 'exchange_rate_uses')
    except sa.exc.NoSuchTableError:
        return None
    with engine.connect() as connection:
        newest = connection.execute(sa.select(table.c.attempt_id).where(table.c.job_id == job_id).order_by(
            table.c.created_at.desc(), table.c.id.desc()).limit(1)).scalar()
        if newest is None:
            return None
        rows = [dict(row) for row in connection.execute(sa.select(table).where(table.c.attempt_id == newest)
                                                         .order_by(table.c.id)).mappings()]
    chosen = next((row for row in rows if ledger_key(row['route_key']) == route or row['route_key'] == route), None)
    if chosen is None:
        return None

    def described(row):
        own = amount_text(parse_amount(row['amount']), row['currency'])
        if row['currency'] == row['compared_in'] or row['rate'] is None:
            return own
        about = amount_text(parse_amount(row['compared_amount']), row['compared_in'])
        rate = Conversion(row['currency'], row['compared_in'], Decimal(row['rate']), _day(row['rate_date']),
                          row['source'])
        return f'{own}, about {about} at {rate.phrase()}'
    others = sorted((row for row in rows if row is not chosen),
                    key=lambda row: parse_amount(row['compared_amount']))
    sentence = f'The cheapest route then, with prices in different currencies compared: {described(chosen)}'
    if others:
        sentence += f', against {described(others[0])} by {route_label(others[0]["route_key"])}'
    return sentence + '.'


# -- the view ----------------------------------------------------------------------------------------------------

def reference_sentence(engine, *, now=None, zone_name=None) -> str:
    """One sentence beside the switch: what the reference rates are and when they were last read."""
    from ..people_time import short
    now = now or utcnow()
    enabled = reference_enabled(engine)
    day, rates = reference_rates(engine)
    last = last_fetch(engine)
    if not enabled:
        if day is None:
            return 'Faxbot does not read the ECB reference rates. Set your own rates to compare prices in different ' \
                   'currencies.'
        return (f'Faxbot does not read the ECB reference rates; the last ones it read, of {long_date(day)}, are used '
                f'until they are more than {REFERENCE_MAX_AGE_DAYS} days old.')
    if last is not None and last['outcome'] == 'failed':
        kept = (f'It uses the rates of {long_date(day)} until it can' if day is not None
                else 'Prices in different currencies are not compared until it can')
        return (f'Faxbot could not read the ECB reference rates at {short(last["attempted_at"], zone_name)}: '
                f'{last["error"]} {kept}, and tries again every hour.')
    if day is None:
        return 'Faxbot reads the ECB reference rates in the background; none are stored yet.'
    old = (now.date() - day).days > REFERENCE_MAX_AGE_DAYS
    tail = (f' They are more than {REFERENCE_MAX_AGE_DAYS} days old, so they are not used to compare prices.'
            if old else '')
    return f'The ECB reference rates of {long_date(day)} for {len(rates)} currencies against the euro.{tail}'


def view(engine, *, now=None, zone_name=None) -> dict:
    """Prices & plans → Exchange rates: the reference rates, your own rates, and how old each may be."""
    now = now or utcnow()
    today = now.date()
    day, rates = reference_rates(engine)
    own = own_rates(engine)
    reference_current = day is not None and (today - day).days <= REFERENCE_MAX_AGE_DAYS
    return {
        'reference': {'enabled': reference_enabled(engine), 'sentence': reference_sentence(engine, now=now,
                                                                                         zone_name=zone_name),
                      'date': day.isoformat() if day else None, 'date_text': long_date(day) if day else None,
                      'current': reference_current, 'source_url': ECB_PAGE, 'read_on': READ_ON,
                      'rates': [{'currency': currency, 'per_euro': rate_text(rate)}
                                for currency, rate in sorted(rates.items())]},
        'own': [{'base': row['base'], 'quote': row['quote'], 'rate': rate_text(Decimal(row['rate'])),
                 'date': _day(row['rate_date']).isoformat(), 'date_text': long_date(row['rate_date']),
                 'current': (today - _day(row['rate_date'])).days <= OWN_MAX_AGE_DAYS,
                 'sentence': _own_sentence(row, today), 'note': row['note']}
                for _, row in sorted(own.items())],
        'reference_max_age_days': REFERENCE_MAX_AGE_DAYS, 'own_max_age_days': OWN_MAX_AGE_DAYS,
    }


def _own_sentence(row, today):
    rate = Decimal(row['rate'])
    head = f'1 {row["base"]} = {rate_text(rate)} {row["quote"]}, your rate of {long_date(row["rate_date"])}.'
    if (today - _day(row['rate_date'])).days > OWN_MAX_AGE_DAYS:
        return head + (f' It is more than {OWN_MAX_AGE_DAYS} days old, so the ECB rate is used instead when it is '
                       'current.')
    return head + ' It is used instead of the ECB rate for this pair.'
