"""Exact integer cost arithmetic in millionths of a currency unit (micros).

A rate card is what a provider advertises; a computed cost applies it to what
Faxbot observed. Rounding always favours the provider (ceil), like an invoice.
"""
from dataclasses import dataclass
from datetime import datetime
import re


MICROS = 1_000_000
# The research example: about 30 seconds of setup plus 30 seconds per page.
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


def billed_seconds(card, seconds):
    """Connected seconds rounded up to the card's increment, after its minimum."""
    if seconds is None or seconds <= 0:
        return 0
    seconds = max(int(_ceil_div(int(seconds * 1000), 1000)), card.minimum_seconds)
    return _ceil_div(seconds, card.billing_increment_seconds) * card.billing_increment_seconds


def attempt_cost(card, *, seconds, pages, delivered):
    """Cost of one placed attempt: setup fee, billed minutes, and pages only if delivered."""
    billed = billed_seconds(card, seconds)
    minutes = _ceil_div(billed * card.per_minute_micros, 60)
    page_count = pages if delivered and isinstance(pages, int) and pages > 0 else 0
    return card.per_call_micros + minutes + page_count * card.per_page_micros


def estimate_cost(card, pages):
    """Expected cost of a successful send, used only to rank routes."""
    pages = pages if isinstance(pages, int) and pages > 0 else 1
    seconds = ESTIMATE_SETUP_SECONDS + ESTIMATE_SECONDS_PER_PAGE * pages
    return attempt_cost(card, seconds=seconds, pages=pages, delivered=True)


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
        floor = (f'{least // 60} minute' + ('' if least == 60 else 's')) if least % 60 == 0 else f'{least} seconds'
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
