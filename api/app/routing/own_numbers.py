"""One rule for "your own numbers", with its two uses.

``receiving_numbers(values)`` (receives into this Faxbot): the numbers on which
a fax reaches this installation. A fax to one of them is delivered inside
Faxbot with no call (``routing.local``). They are only numbers Faxbot knows are
routed to it, never guessed from formatting:

- the SIP trunk's numbers (its DIDs), when Faxbot receives over the trunk;
- the direct-delivery card's own number, while direct delivery is on;
- numbers read from the receiving provider's own account, only when that
  provider receives into Faxbot (``RECEIVING_ACCOUNTS``). HumbleFax reports its
  account numbers but cannot receive into Faxbot, so a fax to a HumbleFax
  number still places a real call and lands in the HumbleFax inbox.

``account_numbers(values, accounts)`` (an account number of yours): every
number one of your accounts gives you, whether or not it receives into Faxbot.
That is the receiving numbers, the trunk's numbers and caller ID, the number
configured for each provider, and the numbers a provider's account reports
(``accounts``, such as HumbleFax's). A fax to one of them is a test between your
own numbers. A test to a number that does not receive into this Faxbot still
places a paid call, so it is still a cost.
"""
from .numbers import InvalidNumber, normalize_number, stored_number
from .store import destination_key


# Receiving providers that report their own fax numbers to Faxbot and receive into it: none today.
RECEIVING_ACCOUNTS = {}
# Config fields that hold a number a provider gives you; the trunk's are its DIDs and caller ID.
PROVIDER_NUMBERS = {'humblefax': ('humblefax_from_number',), 'efax': ('efax_caller_id',),
                    'signalwire': ('signalwire_fax_from_e164',), 'freeswitch': ('fs_caller_id_number',)}


def receiving_numbers(values):
    """The installation's own receiving numbers, in E.164: a fax to one needs no call."""
    country = getattr(values, 'fax_default_country', 'US')
    numbers = set()
    if not getattr(values, 'inbound_enabled', False):
        return numbers  # this installation does not receive faxes, so no number is its own receiving number
    from ..inbound.sip_handover import receives_over_trunk
    if receives_over_trunk(values):
        numbers |= {destination_key(number, country) for number in getattr(values, 'sip_trunk_did_list', ())}
    if getattr(values, 'direct_delivery_enabled', False) and getattr(values, 'direct_fax_number', ''):
        try:
            numbers.add(normalize_number(values.direct_fax_number, country=country))
        except InvalidNumber:
            pass
    reader = RECEIVING_ACCOUNTS.get(getattr(values, 'effective_inbound', ''))
    if reader is not None:
        for number in reader(values) or ():
            numbers.add(destination_key(number, country))
    return {number for number in numbers if number.startswith('+')}


def account_numbers(values, accounts=None):
    """Every number your accounts give you (``accounts``: {provider: numbers} a provider's account reports)."""
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    found = [*getattr(values, 'sip_trunk_did_list', ()), getattr(values, 'sip_trunk_caller_id', '')]
    for fields in PROVIDER_NUMBERS.values():
        found += [getattr(values, field, '') or '' for field in fields]
    found += [number for numbers in (accounts or {}).values() for number in numbers]
    return {stored_number(number, country=country) for number in found if number} | receiving_numbers(values)
