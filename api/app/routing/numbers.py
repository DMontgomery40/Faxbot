"""Normalize fax numbers to E.164 so evidence for one destination is never split."""
import re


class InvalidNumber(ValueError):
    def __init__(self):
        super().__init__('Enter a fax number with its country code, such as +1 555 010 0001.')


def normalize_number(value):
    """Return ``+<digits>``; ten-digit and 1-prefixed eleven-digit numbers are North American."""
    if not isinstance(value, str) or len(value) > 64:
        raise InvalidNumber()
    text = value.strip()
    if not text or re.fullmatch(r'[0-9+().\-\s]+', text) is None or '+' in text[1:]:
        raise InvalidNumber()
    digits = re.sub(r'[^0-9]', '', text)
    if text.startswith('+'):
        candidate = digits
    elif len(digits) == 10:
        candidate = '1' + digits
    elif len(digits) == 11 and digits.startswith('1'):
        candidate = digits
    else:
        raise InvalidNumber()
    if not 8 <= len(candidate) <= 15 or candidate.startswith('0'):
        raise InvalidNumber()
    return '+' + candidate
