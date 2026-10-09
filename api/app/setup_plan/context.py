"""What the administrator says about the organization: only the facts suggestions need.

``organization_name`` is the business name for the header line, and
``country`` and ``mailboxes[<id>].country`` are the countries the
organization and each mailbox work in. Nothing is filled in from elsewhere:
a country left empty stays "not stated", and the installation country (which
only says how to read phone numbers) is never taken as a jurisdiction.
"""
import re


class ContextError(ValueError):
    """The description can't be used as given; the message says what to change."""


_COUNTRY = re.compile(r'[A-Z]{2}')
MAX_NAME = 80


def _country(value, where):
    import phonenumbers
    text = str(value or '').strip().upper()
    if not text:
        return ''
    if not _COUNTRY.fullmatch(text) or text not in phonenumbers.SUPPORTED_REGIONS:
        raise ContextError(f'Choose a country for {where} from the list, such as US or GB.')
    return text


def clean_context(raw, mailbox_ids):
    """The description as stored with the plan; unknown mailboxes are refused, empty fields stay empty."""
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ContextError('Describe the organization with its name and countries.')
    name = ' '.join(str(raw.get('organization_name') or '').split())
    if len(name) > MAX_NAME:
        raise ContextError(f'Keep the business name to {MAX_NAME} characters; it is printed at the top of each page.')
    if any(ord(character) < 32 or character in '|%' for character in name):
        raise ContextError('Type the business name as it should be printed, without | or %.')
    mailboxes = {}
    given = raw.get('mailboxes') or {}
    if not isinstance(given, dict):
        raise ContextError('Give each mailbox’s country by mailbox.')
    for key, value in given.items():
        if key not in mailbox_ids:
            raise ContextError('One of those mailboxes no longer exists. Reload and try again.')
        country = _country((value or {}).get('country') if isinstance(value, dict) else value, 'that mailbox')
        if country:
            mailboxes[key] = {'country': country}
    return {'organization_name': name, 'country': _country(raw.get('country'), 'the organization'),
            'mailboxes': dict(sorted(mailboxes.items()))}
