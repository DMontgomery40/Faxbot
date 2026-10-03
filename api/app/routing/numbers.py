"""One canonical form for fax numbers: E.164, a plus sign and up to 15 digits.

A number written with a leading ``+`` keeps its country code. A number without
one is read for the installation's country (ISO 3166 alpha-2), the way a person
in that country would dial it: in the UK ``01782 684953`` is ``+441782684953``
and in the US ``303 555 0123`` is ``+13035550123``. Input that is not a complete
number in that reading is refused; nothing is guessed. Provider adapters receive
the canonical number and only change its format.
"""
import re

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType, ValidationResult


DEFAULT_COUNTRY = 'US'
SUPPORTED_COUNTRIES = tuple(sorted(phonenumbers.SUPPORTED_REGIONS))
_ENTERED = re.compile(r'[0-9+().\-\s]+')
_CANONICAL = re.compile(r'\+[1-9][0-9]{1,14}', re.ASCII)


class InvalidNumber(ValueError):
    def __init__(self, message='Enter a complete fax number, or one starting with + and its country code.'):
        super().__init__(message)


class AmbiguousNumber(InvalidNumber):
    """A number without its area code, which cannot be dialed from elsewhere."""

    def __init__(self):
        super().__init__('Enter the full fax number with its area code, or with its country code starting with +.')


def normalize_number(value, *, country=DEFAULT_COUNTRY):
    """Return the canonical ``+<digits>`` form of a fax number.

    ``country`` is the installation's country for numbers entered without a
    country code; international dialing prefixes such as 00 and 011 count as
    a country code.
    """
    if country not in phonenumbers.SUPPORTED_REGIONS:
        raise ValueError('Unsupported installation country.')
    if not isinstance(value, str) or len(value) > 64:
        raise InvalidNumber()
    text = value.strip()
    if not text or _ENTERED.fullmatch(text) is None or '+' in text[1:]:
        raise InvalidNumber()
    try:
        number = phonenumbers.parse(text, country)
    except phonenumbers.NumberParseException:
        raise InvalidNumber() from None
    reason = phonenumbers.is_possible_number_with_reason(number)
    if reason == ValidationResult.IS_POSSIBLE_LOCAL_ONLY:
        raise AmbiguousNumber()
    if reason != ValidationResult.IS_POSSIBLE:
        raise InvalidNumber()
    canonical = phonenumbers.format_number(number, PhoneNumberFormat.E164)
    if canonical.startswith('+1') and re.fullmatch(r'\+1[2-9][0-9]{9}', canonical) is None:
        raise InvalidNumber()  # North American area codes never start with 0 or 1
    return canonical


def canonical_number(value):
    """Return ``value`` when it is already canonical; never reinterpret it."""
    if not isinstance(value, str) or _CANONICAL.fullmatch(value) is None:
        raise InvalidNumber()
    if normalize_number(value) != value:
        raise InvalidNumber()
    return value


def is_canonical(value):
    try:
        canonical_number(value)
    except InvalidNumber:
        return False
    return True


def accepted_destination(stored, *, country):
    """The canonical destination of an accepted fax job.

    Jobs are stored with their canonical number. Jobs accepted before that kept
    the number as entered; it is resolved once, with the country captured in
    the job's own configuration, so later settings cannot redirect it.
    """
    if is_canonical(stored):
        return stored
    return normalize_number(stored, country=country)


def stored_number(value, *, country):
    """Canonical form of a number received or saved elsewhere, else the value as given.

    Inbound provider callbacks and older records can carry numbers that are not
    complete; they are kept, never dropped, and simply match no canonical rule.
    """
    try:
        return normalize_number(value, country=country)
    except InvalidNumber:
        return value


def number_example(country):
    """A sample number for the country, written nationally and internationally."""
    if country not in phonenumbers.SUPPORTED_REGIONS:
        raise ValueError('Unsupported installation country.')
    sample = (phonenumbers.example_number_for_type(country, PhoneNumberType.FIXED_LINE)
              or phonenumbers.example_number(country))
    if sample is None:
        return {'national': '', 'international': ''}
    return {'national': phonenumbers.format_number(sample, PhoneNumberFormat.NATIONAL),
            'international': phonenumbers.format_number(sample, PhoneNumberFormat.INTERNATIONAL)}
