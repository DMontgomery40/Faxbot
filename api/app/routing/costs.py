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


def parse_amount(value):
    """Decimal text such as "0.0095" to micros; at most six decimal places."""
    if isinstance(value, bool):
        raise InvalidRateCard('Enter prices as numbers, such as 0.0095.')
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{1,6})?', value.strip()) is None:
        raise InvalidRateCard('Enter prices as numbers with up to six decimal places, such as 0.0095.')
    whole, _, fraction = value.strip().partition('.')
    return int(whole) * MICROS + int((fraction + '000000')[:6])


def format_amount(micros):
    """Micros to the exact decimal text an operator entered (trailing zeros trimmed)."""
    if micros is None:
        return None
    sign = '-' if micros < 0 else ''
    whole, fraction = divmod(abs(micros), MICROS)
    return f"{sign}{whole}.{f'{fraction:06d}'.rstrip('0').ljust(2, '0')}"
