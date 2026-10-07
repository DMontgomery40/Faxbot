"""What a route needs and charges to call a number of each class, toll-free above all.

US and Canadian toll-free numbers share the North American codes 800, 833,
844, 855, 866, 877 and 888 (822 and 880 to 887 are reserved, not in service).
The toll-free subscriber pays for the call, so calling one usually costs the
caller less, sometimes nothing: Telnyx lists "Toll Free calls | Free" against
$0.005 a minute for local calls (read 2026-10-07).

``config/rate_cards.json`` lists, under ``toll_free``, what each route
publishes about such calls: whether it reaches them, its price (its own,
the same as its sending card, or not published) and the caller ID it needs.
Nothing here places a call or changes a setting:

- ``number_class``: ``toll_free`` or ``standard``.
- ``reaches``: whether a route may call a number. A route that publishes that
  it refuses toll-free numbers, or whose caller-ID rule this installation
  cannot meet, may not; a route that publishes nothing may (useful options
  default on), and a definite refusal moves the fax back to the recipient's
  own number (``alternates.attempt_number``).
- ``class_card``: the rate card that prices a call to a number on a route;
  None when that price is not published, so unknown stays unknown.
"""
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re

from .costs import InvalidRateCard, RateCard, parse_amount
from .seed import _date, _increment, default_path


TOLL_FREE = 'toll_free'
STANDARD = 'standard'
# NANP toll-free codes in service; an exchange code never starts with 0 or 1.
_NANP_TOLL_FREE = re.compile(r'\+18(?:00|33|44|55|66|77|88)[2-9][0-9]{6}')
REACHES = ('yes', 'no', 'not_published')
PRICING = ('own', 'same_as_card', 'not_published')
CALLER_ID = ('account_number', 'local_number', 'set')


def number_class(number):
    """``toll_free`` for a North American toll-free number in E.164, else ``standard``."""
    return TOLL_FREE if isinstance(number, str) and _NANP_TOLL_FREE.fullmatch(number) else STANDARD


def is_toll_free(number):
    return number_class(number) == TOLL_FREE


def display_number(number):
    """A North American number as people write it ("1-800-555-0100"); any other number as stored."""
    if isinstance(number, str) and re.fullmatch(r'\+1[2-9][0-9]{9}', number):
        return f'1-{number[2:5]}-{number[5:8]}-{number[8:]}'
    return number


@dataclass(frozen=True)
class TollFreeTerms:
    route: str
    label: str
    reaches: str
    pricing: str
    card: object  # a RateCard when pricing is ``own``
    caller_id: str | None
    t38: str | None
    source_url: str | None
    advertised_on: str | None
    notes: str | None


def _terms(entry):
    route = str(entry.get('route') or '').strip().lower()
    if not route or re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', route) is None:
        return None
    reaches = entry.get('reaches') if entry.get('reaches') in REACHES else 'not_published'
    pricing = entry.get('pricing') if entry.get('pricing') in PRICING else 'not_published'
    card = None
    if pricing == 'own':
        try:
            card = RateCard(None, route, 'outbound', str(entry.get('label') or route)[:100],
                            str(entry.get('currency') or 'USD').upper(),
                            parse_amount(str(entry.get('per_minute', '0'))), parse_amount(str(entry.get('per_page', '0'))),
                            parse_amount(str(entry.get('per_call', '0'))), _increment(entry),
                            int(entry.get('minimum_seconds', 0)), entry.get('source_url') or None,
                            _date(entry.get('advertised_on')))
        except (InvalidRateCard, ValueError, TypeError):
            pricing, card = 'not_published', None
    caller_id = entry.get('caller_id') if entry.get('caller_id') in CALLER_ID else None
    return TollFreeTerms(route, str(entry.get('label') or route), reaches, pricing, card, caller_id,
                         entry.get('t38') or None, entry.get('source_url') or None,
                         entry.get('advertised_on') or None, entry.get('notes') or None)


def load_terms(path=None):
    """``{route identity: TollFreeTerms}`` from the shipped file; an unreadable file gives none."""
    path = Path(path) if path is not None else default_path()
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    listed = document.get('toll_free') if isinstance(document, dict) else None
    found = {}
    for entry in listed if isinstance(listed, list) else []:
        terms = _terms(entry) if isinstance(entry, dict) else None
        if terms is not None:
            found.setdefault(terms.route, terms)
    return found


@lru_cache(maxsize=1)
def _shipped():
    return load_terms()


def route_identity(provider_id, sip_preset=''):
    """The identity a route's prices are listed under: the SIP trunk is its carrier (``sip-<preset>``)."""
    if provider_id == 'sip':
        return f'sip-{sip_preset}' if sip_preset else 'sip'
    return provider_id


def terms_for(provider_id, sip_preset='', terms=None):
    terms = _shipped() if terms is None else terms
    return terms.get(route_identity(provider_id, sip_preset))


def _caller_id(values, provider_id):
    """The caller ID a toll-free call on this route would carry, when Faxbot sets it; None when the route does."""
    if provider_id == 'sip':
        return getattr(values, 'sip_trunk_caller_id', '') or ''
    if provider_id == 'freeswitch':
        return getattr(values, 'fs_caller_id_number', '') or ''
    return None


def caller_id_problem(values, provider_id, terms=None):
    """Why this route cannot call a toll-free number with this installation's caller ID, or None.

    Toll-free subscribers pay for the call and many refuse calls with no caller
    ID; Telnyx never hides one on such a call. So a route whose caller ID Faxbot
    sets needs a real one, and the route's own rule must hold.
    """
    caller = _caller_id(values, provider_id)
    if caller is None:
        return None  # a cloud fax service sends its own number
    if re.fullmatch(r'\+[1-9][0-9]{6,14}', caller) is None:
        return 'no_caller_id'
    rule = getattr(terms, 'caller_id', None)
    if rule == 'local_number' and (is_toll_free(caller) or not caller.startswith('+1')):
        return 'caller_id_not_local'
    if rule == 'account_number':
        own = set(getattr(values, 'sip_trunk_did_list', ()) or ())
        # Only the trunk's own numbers can be checked here; a carrier-verified number is not listed in Faxbot.
        if own and caller not in own:
            return 'caller_id_not_on_account'
    return None


def reaches(provider_id, number, values, *, sip_preset=None, terms=None):
    """Whether a route may call ``number``: every route calls standard numbers."""
    if not is_toll_free(number):
        return True
    preset = getattr(values, 'sip_trunk_preset', '') if sip_preset is None else sip_preset
    found = terms_for(provider_id, preset or '', terms)
    if found is not None and found.reaches == 'no':
        return False
    return caller_id_problem(values, provider_id, found) is None


REACH_TEXT = {
    'yes': 'Calls toll-free numbers.',
    'no': 'Cannot call toll-free numbers, so faxes go to the number entered.',
    'not_published': ('Not published; Faxbot tries the toll-free number and calls the number entered '
                      'if that call fails.'),
}
CALLER_ID_TEXT = {
    'account_number': 'A number on your account, or one the carrier verified.',
    'local_number': 'A local US or Canadian number, never a toll-free one.',
    'set': 'Any real number of yours.',
}
PROBLEM_TEXT = {
    'no_caller_id': ('Set a caller ID under Providers → Carrier trunk: toll-free numbers often refuse calls '
                     'without one, so until then faxes go to the number entered.'),
    'caller_id_not_local': ("This carrier needs a local caller ID, and your trunk's caller ID is not one, so "
                            'faxes go to the number entered. Change it under Providers → Carrier trunk.'),
    'caller_id_not_on_account': ("This carrier needs a caller ID on your account, and your trunk's caller ID is "
                                 "not one of the trunk's numbers, so faxes go to the number entered. Change it "
                                 'under Providers → Carrier trunk.'),
}


def price_text(terms):
    """The price of a call to a toll-free number on this route, in words."""
    from .costs import rate_text
    if terms.pricing == 'same_as_card':
        return 'The same as its sending price.'
    if terms.pricing != 'own' or terms.card is None:
        return 'Not published.'
    card = terms.card
    if not (card.per_minute_micros or card.per_page_micros or card.per_call_micros):
        return 'Free.'
    return rate_text(card)[0].upper() + rate_text(card)[1:] + '.'


def terms_view(values, terms=None):
    """What each route this installation sends with publishes about calling toll-free numbers.

    For Costs → Prices & plans and ``faxbot costs rate-cards``; routes with no entry are left out.
    """
    from ..provider_labels import PROVIDER_LABELS, trunk_name
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    routes = [route for route in dict.fromkeys([values.effective_outbound, *values.outbound_route_providers]) if route]
    items = []
    for provider_id in routes:
        found = terms_for(provider_id, preset, terms)
        if found is None:
            continue
        problem = caller_id_problem(values, provider_id, found)
        items.append({
            'provider_id': provider_id, 'route': found.route,
            'provider_name': trunk_name(preset) if provider_id == 'sip' else PROVIDER_LABELS.get(provider_id, provider_id),
            'label': found.label, 'reaches': 'no' if problem else found.reaches,
            'reach_text': PROBLEM_TEXT[problem] if problem else REACH_TEXT[found.reaches],
            'price_text': price_text(found), 'pricing': found.pricing,
            'caller_id_text': CALLER_ID_TEXT.get(found.caller_id) if found.caller_id else None,
            'advertised_on': found.advertised_on, 'source_url': found.source_url})
    return items


def class_card(card, provider_id, number, *, sip_preset='', terms=None):
    """The card that prices a call to ``number`` on this route; None when that price is unknown."""
    if not is_toll_free(number):
        return card
    found = terms_for(provider_id, sip_preset or '', terms)
    if found is None or found.pricing == 'not_published':
        return None
    if found.pricing == 'same_as_card':
        return card
    return found.card
