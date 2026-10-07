"""The class of a fax number, for pricing: local, toll-free, international or premium (M4).

Carriers price a call by what kind of number it reaches. A call to a US
toll-free number is free on Telnyx (its SIP price page, read 2026-10-07)
while a local call costs $0.005 a minute, and a call abroad is priced by the
country it reaches. ``classify`` is the one place Faxbot decides a number's
class; every other module that prices or reports a number by class reads it
from here.

- ``toll_free``: a toll-free number in any country. In North America only the
  codes in service count (800, 833, 844, 855, 866, 877 and 888); 822 and 880
  to 887 are reserved and never valid.
- ``premium``: a premium-rate number (in North America, 900). No fax route
  publishes a price for these, so their cost is unknown.
- ``international``: a number outside the installation's country. ``region``
  names its country and ``prefix`` its country calling code ("+44").
- ``local``: any other valid number in the installation's country.
- ``unknown``: not a valid number; nothing is priced for it.

Pure: no database, no network, no configuration reads.
"""
from dataclasses import dataclass

import phonenumbers
from phonenumbers import PhoneNumberType


LOCAL = 'local'
TOLL_FREE = 'toll_free'
INTERNATIONAL = 'international'
PREMIUM = 'premium'
UNKNOWN = 'unknown'
CLASSES = (LOCAL, TOLL_FREE, INTERNATIONAL, PREMIUM, UNKNOWN)

# How each class reads in a sentence ("calls to toll-free numbers").
CLASS_TEXT = {LOCAL: 'local numbers', TOLL_FREE: 'toll-free numbers', INTERNATIONAL: 'numbers abroad',
              PREMIUM: 'premium-rate numbers', UNKNOWN: 'this number'}


@dataclass(frozen=True)
class DestinationClass:
    kind: str
    region: str | None = None   # ISO country code of the number ("US", "GB"), when valid
    prefix: str | None = None   # its country calling code in E.164 form ("+1", "+44"), when valid
    number: str | None = None   # the number in E.164 form, when valid

    def __post_init__(self):
        if self.kind not in CLASSES:
            raise ValueError('Unknown destination class.')

    def matches(self, prefixes):
        """The longest of ``prefixes`` ("+44", "+4420") this number starts with, or None."""
        if not self.number:
            return None
        found = [prefix for prefix in prefixes if isinstance(prefix, str) and self.number.startswith(prefix)]
        return max(found, key=len) if found else None


def classify(number, home_country='US'):
    """The class of ``number`` (E.164, or national for ``home_country``) for an installation in ``home_country``."""
    if not isinstance(number, str) or not number.strip():
        return DestinationClass(UNKNOWN)
    home = (home_country or 'US').upper()
    try:
        parsed = phonenumbers.parse(number.strip(), None if number.strip().startswith('+') else home)
    except phonenumbers.NumberParseException:
        return DestinationClass(UNKNOWN)
    if not phonenumbers.is_valid_number(parsed):
        return DestinationClass(UNKNOWN)
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    region = phonenumbers.region_code_for_number(parsed)
    prefix = f'+{parsed.country_code}'
    kind = phonenumbers.number_type(parsed)
    if kind == PhoneNumberType.TOLL_FREE:
        # North American toll-free codes are shared by the US and Canada: they are toll-free from either.
        shared = parsed.country_code == 1 and home in phonenumbers.region_codes_for_country_code(1)
        if region == home or shared:
            return DestinationClass(TOLL_FREE, region, prefix, e164)
        return DestinationClass(INTERNATIONAL, region, prefix, e164)
    if kind == PhoneNumberType.PREMIUM_RATE:
        return DestinationClass(PREMIUM, region, prefix, e164)
    if region != home:
        return DestinationClass(INTERNATIONAL, region, prefix, e164)
    return DestinationClass(LOCAL, region, prefix, e164)


def country_name(region):
    """A country's English name for a sentence ("the United Kingdom"), or its code when unknown."""
    names = {'US': 'the United States', 'CA': 'Canada', 'GB': 'the United Kingdom', 'AU': 'Australia',
             'MX': 'Mexico', 'DE': 'Germany', 'FR': 'France', 'IE': 'Ireland', 'NZ': 'New Zealand',
             'IN': 'India', 'PH': 'the Philippines', 'JP': 'Japan'}
    return names.get(region, region or 'that country')
