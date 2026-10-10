"""Exact integer cost arithmetic in millionths of a currency unit (micros).

A rate card is what a provider advertises; a computed cost applies it to what
Faxbot observed. Rounding always favours the provider (ceil), like an invoice.
"""
from dataclasses import dataclass
from datetime import datetime
import re


MICROS = 1_000_000
# The 2026-10-03 research example's call time: about 30 seconds of setup plus 30 seconds per page. Only the stand-in
# page predictor (``pages.decision``) and the carrier comparison's what-if (``carrier_compare``) still read it;
# ``estimate_cost``, and with it every route ranking, uses the shared predictor's time model instead.
ESTIMATE_SETUP_SECONDS = 30
ESTIMATE_SECONDS_PER_PAGE = 30
MAX_RATE_MICROS = 100 * MICROS
# A monthly plan fee fits a 32-bit integer column: at most 2,000 per month.
MAX_MONTHLY_MICROS = 2000 * MICROS


class InvalidRateCard(ValueError):
    pass


def _ceil_div(numerator, denominator):
    return -(-numerator // denominator)


@dataclass(frozen=True)
class RateCard:
    id: str | None
    provider_id: str
    direction: str
    label: str
    currency: str
    per_minute_micros: int
    per_page_micros: int
    per_call_micros: int
    billing_increment_seconds: int
    minimum_seconds: int
    source_url: str | None
    captured_on: datetime
    # A flat monthly plan fee, if any: shared by every fax, never added to one fax's cost.
    monthly_fee_micros: int | None = None

    @property
    def flat_plan(self):
        """A monthly fee with nothing charged per minute, page or call: faxes are included in the plan."""
        return (bool(self.monthly_fee_micros) and self.per_minute_micros == 0 and self.per_page_micros == 0
                and self.per_call_micros == 0)

    def __post_init__(self):
        if (not isinstance(self.provider_id, str)
                or re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', self.provider_id) is None):
            raise InvalidRateCard('Choose a provider for this rate card.')
        if self.direction not in {'outbound', 'inbound'}:
            raise InvalidRateCard('A rate card is either for sending or for receiving.')
        if not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 100:
            raise InvalidRateCard('Give the rate card a name of up to 100 characters.')
        if not isinstance(self.currency, str) or re.fullmatch(r'[A-Z]{3}', self.currency) is None:
            raise InvalidRateCard('Use a three-letter currency code, such as USD.')
        for name in ('per_minute_micros', 'per_page_micros', 'per_call_micros'):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= MAX_RATE_MICROS:
                raise InvalidRateCard('Rates must be zero or more and at most 100 per unit.')
        if type(self.billing_increment_seconds) is not int or not 1 <= self.billing_increment_seconds <= 3600:
            raise InvalidRateCard('Billing increments are between 1 second and 1 hour.')
        if type(self.minimum_seconds) is not int or not 0 <= self.minimum_seconds <= 3600:
            raise InvalidRateCard('A minimum charge is between 0 seconds and 1 hour.')
        if self.source_url is not None and (not isinstance(self.source_url, str) or len(self.source_url) > 512
                                            or re.fullmatch(r'https?://[^\s]+', self.source_url) is None):
            raise InvalidRateCard('The source must be a web address.')
        if not isinstance(self.captured_on, datetime):
            raise InvalidRateCard('Record the date these prices were advertised.')
        if self.monthly_fee_micros is not None and (type(self.monthly_fee_micros) is not int
                                                    or not 0 <= self.monthly_fee_micros <= MAX_MONTHLY_MICROS):
            raise InvalidRateCard('A monthly fee is zero or more and at most 2,000.')


@dataclass(frozen=True)
class TimeBand:
    """A per-minute price that applies on some days between two times, on the carrier's own clock (a peak,
    off-peak or weekend rate). Pure data, as the carrier publishes it."""
    days: tuple                 # 0 Monday to 6 Sunday
    start_minute: int           # minutes after local midnight, included
    end_minute: int             # minutes after local midnight, excluded (1440: midnight)
    per_minute_micros: int
    label: str = ''             # the carrier's own name for it: 'peak', 'off-peak', 'weekend'

    def __post_init__(self):
        if (not isinstance(self.days, tuple) or not self.days
                or any(type(day) is not int or not 0 <= day <= 6 for day in self.days)):
            raise InvalidRateCard('A time band names days from Monday (0) to Sunday (6).')
        if (type(self.start_minute) is not int or type(self.end_minute) is not int
                or not 0 <= self.start_minute < self.end_minute <= 1440):
            raise InvalidRateCard('A time band starts before it ends, within one day.')
        if type(self.per_minute_micros) is not int or not 0 <= self.per_minute_micros <= MAX_RATE_MICROS:
            raise InvalidRateCard('Rates must be zero or more and at most 100 per unit.')

    def covers(self, local):
        minute = local.hour * 60 + local.minute
        return local.weekday() in self.days and self.start_minute <= minute < self.end_minute


@dataclass(frozen=True)
class RateTerms:
    """A rate card and the terms a stored card does not hold: the numbers it prices and how it counts pages.

    Stored cards price calls to local numbers (``destination_class`` 'local').
    Prices for other classes, and terms such as Fax.Plus's page-or-time rule,
    come from ``config/rate_cards.json`` with their source and date, so a card
    saved in the console never loses them. Pure data; ``terms_cost`` prices it.

    - ``destination_class``: 'local', 'toll_free', 'international' or 'premium' (``destinations.CLASSES``).
    - ``prefixes``: for an international price, the E.164 prefixes it covers ("+44"); the longest match wins.
    - ``page_time_seconds``: the greater-of rule. A page is also counted for each started period of this
      many seconds on the line, and the bill counts the greater of the two (Fax.Plus: 60).
    - ``included_pages`` and ``overage_page_micros``: a plan's monthly page allowance and the price of
      each page past it.
    - ``included_minutes``: a monthly minute allowance (a trunk bundle); minutes past it cost the card's
      per-minute price.
    - ``max_pages_per_fax``: the most pages the route takes in one fax, when it publishes a limit.
    - ``published``: True for a shipped published price, False for a rate card saved in Faxbot.
    """
    card: RateCard
    destination_class: str = 'local'
    prefixes: tuple = ()
    page_time_seconds: int | None = None
    included_pages: int | None = None
    overage_page_micros: int | None = None
    max_pages_per_fax: int | None = None
    published: bool = False
    included_minutes: int | None = None
    # A carrier's prices by time of day (``TimeBand``), on its own clock (``time_zone``), with the version they came
    # from ('aaisp read 2026-10-10'). Empty by default: the card's own per-minute price at every hour.
    time_bands: tuple = ()
    time_zone: str | None = None
    bands_version: str | None = None

    def at(self, moment):
        """These terms for a call starting at ``moment`` (naive UTC): the card's per-minute price replaced by the
        band covering that local time, when the terms have bands; unchanged otherwise or when no band covers it."""
        if not self.time_bands or moment is None:
            return self
        from dataclasses import replace as replaced
        from datetime import timezone
        from zoneinfo import ZoneInfo
        local = moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(self.time_zone or 'UTC'))
        band = next((band for band in self.time_bands if band.covers(local)), None)
        if band is None:
            return self
        return replaced(self, card=replaced(self.card, per_minute_micros=band.per_minute_micros))

    def band_at(self, moment):
        """The ``TimeBand`` covering ``moment`` (naive UTC), or None."""
        if not self.time_bands or moment is None:
            return None
        from datetime import timezone
        from zoneinfo import ZoneInfo
        local = moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(self.time_zone or 'UTC'))
        return next((band for band in self.time_bands if band.covers(local)), None)

    def __post_init__(self):
        from .destinations import CLASSES, UNKNOWN
        if not isinstance(self.time_bands, tuple) or any(not isinstance(band, TimeBand) for band in self.time_bands):
            raise InvalidRateCard('Time bands are a list of bands.')
        if self.time_bands:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(self.time_zone or '')
            except (ZoneInfoNotFoundError, ValueError):
                raise InvalidRateCard('Time bands need the carrier time zone they are read in.') from None
        if not isinstance(self.card, RateCard):
            raise InvalidRateCard('Rate terms need a rate card.')
        if self.destination_class not in CLASSES or self.destination_class == UNKNOWN:
            raise InvalidRateCard('Choose local, toll-free, international or premium-rate numbers.')
        if not isinstance(self.prefixes, tuple) or any(
                not isinstance(prefix, str) or re.fullmatch(r'\+[1-9][0-9]{0,6}', prefix) is None
                for prefix in self.prefixes):
            raise InvalidRateCard('Write each number prefix with its country code, such as +44.')
        for name, highest in (('page_time_seconds', 3600), ('included_pages', 1_000_000), ('max_pages_per_fax', 10_000),
                              ('included_minutes', 1_000_000)):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1 <= value <= highest):
                raise InvalidRateCard('Page and time limits are whole numbers above zero.')
        if self.overage_page_micros is not None and (type(self.overage_page_micros) is not int
                                                     or not 0 <= self.overage_page_micros <= MAX_RATE_MICROS):
            raise InvalidRateCard('Rates must be zero or more and at most 100 per unit.')


def greater_of_pages(pages, seconds, unit_seconds):
    """The greater of the pages sent and each started ``unit_seconds`` on the line; None when ``seconds`` is unknown.

    Fax.Plus counts "the greater of physical pages or full/partial 60-second
    transmission or connection intervals", so one page that takes 66 seconds
    is billed as two pages.
    """
    if seconds is None:
        return None
    pages = pages if isinstance(pages, int) and pages > 0 else 0
    if seconds <= 0:
        return pages
    started = _ceil_div(_ceil_div(int(seconds * 1000), 1000), unit_seconds)
    return max(pages, started)


def terms_cost(terms, *, seconds, pages):
    """(pages billed at a page price, cost in micros) for one delivered fax under ``terms``.

    Pages billed are the pages sent on a per-page card, the greater-of count
    under a page-or-time rule, and 0 on a card without a page price. The cost
    is None when it depends on a call length nobody knows: unknown is never
    priced as zero.
    """
    card = terms.card
    pages = pages if isinstance(pages, int) and pages > 0 else 0
    if terms.page_time_seconds:
        billed_pages = greater_of_pages(pages, seconds, terms.page_time_seconds)
        if billed_pages is None:
            return None, None
    else:
        billed_pages = pages if card.per_page_micros else 0
    billed = billed_seconds(card, seconds)
    if billed is None:
        if card.per_minute_micros:
            return billed_pages, None
        billed = 0
    return billed_pages, (card.per_call_micros + _ceil_div(billed * card.per_minute_micros, 60)
                          + billed_pages * card.per_page_micros)


class UnknownAmount(TypeError):
    """An unknown amount met a known one in arithmetic. Count it as not priced (``Tally``) instead."""


@dataclass(frozen=True)
class Money:
    """A known amount: whole micros in one currency. An unknown amount is ``None``, never ``Money(0, ...)``.

    Money adds to and subtracts from Money of the same currency only. Adding
    ``None`` raises ``UnknownAmount``, and plain numbers (``sum()``'s starting
    0 included) raise ``TypeError``, so an unknown cost can never quietly
    become a number. To total amounts that may be unknown, use ``Tally``, which
    keeps the known sum per currency and counts the unknown ones.
    """
    micros: int
    currency: str

    def __post_init__(self):
        if type(self.micros) is not int:
            raise TypeError('Money is a whole number of micros.')
        if not isinstance(self.currency, str) or re.fullmatch(r'[A-Z]{3}', self.currency) is None:
            raise ValueError('Money needs a three-letter currency code.')

    @classmethod
    def of(cls, micros, currency):
        """The amount, or None when either part is unknown."""
        if micros is None or not currency:
            return None
        return cls(int(micros), currency)

    def _other(self, other):
        if other is None:
            raise UnknownAmount('An unknown amount cannot be added to a known one; count it as not priced.')
        if not isinstance(other, Money):
            return None
        if other.currency != self.currency:
            raise ValueError(f'Cannot combine {self.currency} with {other.currency}.')
        return other

    def __add__(self, other):
        found = self._other(other)
        return NotImplemented if found is None else Money(self.micros + found.micros, self.currency)

    __radd__ = __add__

    def __sub__(self, other):
        found = self._other(other)
        return NotImplemented if found is None else Money(self.micros - found.micros, self.currency)

    def __rsub__(self, other):
        found = self._other(other)
        return NotImplemented if found is None else Money(found.micros - self.micros, self.currency)

    def __neg__(self):
        return Money(-self.micros, self.currency)

    def text(self):
        return money_text(self.micros, self.currency)


class Tally:
    """Known amounts summed per currency (``known``), and how many amounts were unknown (``unknown``).

    An unknown amount is counted, never added as 0. With nothing known,
    ``known`` is empty: no total, rather than a total of 0.
    """

    def __init__(self):
        self.known, self.unknown = {}, 0

    def add(self, amount):
        if amount is None:
            self.unknown += 1
        elif isinstance(amount, Money):
            self.known[amount.currency] = self.known.get(amount.currency, 0) + amount.micros
        else:
            raise TypeError('A Tally adds Money or None (unknown).')
        return self


# A call that ended without being answered is billed nothing by the minute.
NOT_ANSWERED = ('busy', 'congestion', 'failed', 'no_answer')


def call_seconds(connected_seconds, answered_at, ended_at, disposition=None):
    """Seconds to price a call by, or None when nobody measured them.

    The measured connected time; else the time from answer to end; else 0 for
    a call that ended without being answered. An answered call whose length was
    never reported (the SSL Fax engine records the answer before Asterisk
    reports the times) is unknown, never 0.
    """
    if connected_seconds is not None:
        return connected_seconds
    if answered_at is not None and ended_at is not None:
        return max(0, int((ended_at - answered_at).total_seconds()))
    if ended_at is not None and answered_at is None and disposition in NOT_ANSWERED:
        return 0
    return None


def billed_seconds(card, seconds):
    """Connected seconds rounded up to the card's increment, after its minimum; None when ``seconds`` is unknown."""
    if seconds is None:
        return None
    if seconds <= 0:
        return 0
    seconds = max(int(_ceil_div(int(seconds * 1000), 1000)), card.minimum_seconds)
    return _ceil_div(seconds, card.billing_increment_seconds) * card.billing_increment_seconds


def attempt_cost(card, *, seconds, pages, delivered):
    """Cost of one placed attempt: setup fee, billed minutes, and pages only if delivered.

    None when the card charges by the minute and the call's length is unknown:
    unknown is never priced as zero minutes.
    """
    billed = billed_seconds(card, seconds)
    if billed is None:
        if card.per_minute_micros:
            return None
        billed = 0
    minutes = _ceil_div(billed * card.per_minute_micros, 60)
    page_count = pages if delivered and isinstance(pages, int) and pages > 0 else 0
    return card.per_call_micros + minutes + page_count * card.per_page_micros


def split_by_weight(total, weights):
    """Split integer ``total`` micros in proportion to ``weights``; the parts always sum exactly to ``total``.

    Largest remainder: each part is first rounded down, then the micros left over go one each to the
    parts with the largest remainders. Between equal remainders the earlier part comes first, so
    callers pass the weights in a stated order (for a shared call: call order, then fax ID). A negative
    total is split by its size and keeps its sign.
    """
    weights = [int(weight) for weight in weights]
    whole = sum(weights)
    if not weights or whole <= 0 or any(weight < 0 for weight in weights):
        raise ValueError('Weights must be whole numbers with a positive sum.')
    size, sign = abs(int(total)), -1 if total < 0 else 1
    parts = [size * weight // whole for weight in weights]
    remainders = [size * weight % whole for weight in weights]
    for index in sorted(range(len(weights)), key=lambda index: (-remainders[index], index))[:size - sum(parts)]:
        parts[index] += 1
    return [sign * part for part in parts]


def estimate_cost(card, pages):
    """Expected cost of one delivered fax of ``pages`` typical pages on ``card``, in micros.

    The shared pre-dial predictor's figure (``predict``): its setup time, typical page size at standard resolution
    and handshake per page, billed over its default spread of call times with the card's increment and minimum,
    exactly as route ranking prices a fax (``pricing.price``) when nothing is known about the number. It replaces
    the older estimate of 30 seconds plus 30 seconds a page. A flat plan's fax adds nothing (0).
    """
    from .destinations import LOCAL, DestinationClass
    from .predict import RouteFacts, Shape, predict_from
    pages = pages if isinstance(pages, int) and pages > 0 else 1
    facts = RouteFacts(card.provider_id, card.label, DestinationClass(LOCAL), RateTerms(card))
    found = predict_from(facts, Shape(min(pages, 10_000), None, 'standard', 'normal'))
    if found.cost is None:
        # A card always prices a fax of known time; this is only reached for an impossible shape.
        return attempt_cost(card, seconds=None, pages=pages, delivered=True)
    return found.cost.micros


def rate_text(card):
    """A card's price in its own units: "$0.005 a minute, at least 1 minute", "$0.07 a page".

    None for a flat plan or a card that charges nothing per fax.
    """
    if card is None or card.flat_plan:
        return None
    parts = []
    if card.per_call_micros:
        parts.append(f'{money_text(card.per_call_micros, card.currency)} a call')
    if card.per_minute_micros:
        least = billed_seconds(card, 1)
        floor = (f'{least // 60} minute' + ('' if least == 60 else 's')) if least % 60 == 0 else f'{least} second' + ('' if least == 1 else 's')
        parts.append(f'{money_text(card.per_minute_micros, card.currency)} a minute, at least {floor}')
    if card.per_page_micros:
        parts.append(f'{money_text(card.per_page_micros, card.currency)} a page')
    return ' plus '.join(parts) or None


def parse_amount(value, *, whole_digits=3):
    """Decimal text such as "0.0095" to micros; at most six decimal places."""
    if isinstance(value, bool):
        raise InvalidRateCard('Enter prices as numbers, such as 0.0095.')
    if isinstance(value, int):
        value = str(value)
    if (not isinstance(value, str)
            or re.fullmatch(r'[0-9]{1,%d}(?:\.[0-9]{1,6})?' % whole_digits, value.strip()) is None):
        raise InvalidRateCard('Enter prices as numbers with up to six decimal places, such as 0.0095.')
    whole, _, fraction = value.strip().partition('.')
    return int(whole) * MICROS + int((fraction + '000000')[:6])


def money_text(micros, currency):
    """Money for a sentence: "$0.005" for US dollars, "0.005 EUR" otherwise."""
    amount = format_amount(abs(micros))
    sign = '-' if micros < 0 else ''
    return f'{sign}${amount}' if currency == 'USD' else f'{sign}{amount} {currency}'


def plan_fee_text(micros, currency):
    """A monthly fee for a sentence, without cents when it is whole: "$10", "$9.99"."""
    if micros % MICROS == 0:
        whole = micros // MICROS
        return f'${whole}' if currency == 'USD' else f'{whole} {currency}'
    return money_text(micros, currency)


def plan_fee_for_days(card, days):
    """A flat plan's fee for a period, pro-rated by days: a 30-day period counts one monthly fee."""
    if not card.monthly_fee_micros or days <= 0:
        return 0
    return _ceil_div(card.monthly_fee_micros * days, 30)


def money_list_text(amounts):
    """{currency: micros} as one phrase, such as "$0.015" or "$0.01 + 0.02 EUR"."""
    return ' + '.join(money_text(micros, currency) for currency, micros in sorted(amounts.items()))


def format_amount(micros):
    """Micros to the exact decimal text an operator entered (trailing zeros trimmed)."""
    if micros is None:
        return None
    sign = '-' if micros < 0 else ''
    whole, fraction = divmod(abs(micros), MICROS)
    return f"{sign}{whole}.{f'{fraction:06d}'.rstrip('0').ljust(2, '0')}"
