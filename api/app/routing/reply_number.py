"""Your reply number: the number printed on the faxes you send, so replies reach you cheaply.

A recipient who answers a fax dials the number printed on it: the header line
at the top of each page and the station identifier (TSI) its fax machine
shows. 47 CFR 68.318(d) asks for "the telephone number of the sending machine
or of such business": any of the business's own numbers is lawful there. So
Faxbot prints the number replies should come to, which need not be the line
the fax leaves on.

Which number a fax shows, first match wins:

1. the sending mailbox's own reply number (``fax_reply_numbers``), when the
   send names a mailbox;
2. the organization's reply number (``fax_reply_number``);
3. the station ID setting (``fax_station_id``), as before this feature;
4. automatically, the cheapest of your numbers to receive on (below);
5. nothing: the line's own number (the trunk's caller ID), as before.

A number qualifies only when all three hold, each with its own refusal:

- one of your accounts gives it to you (``own_numbers.account_numbers``);
- it receives into this Faxbot (the trunk's numbers, the direct-delivery
  number, or the number of the provider that receives for you);
- a rule under Numbers sends its faxes to a mailbox in use: for a mailbox's
  own reply number, that same mailbox.

Cheapest to receive on: a number on a flat plan costs nothing per fax within
the plan; then the lowest estimated cost of a typical received fax
(``TYPICAL_PAGES`` pages) on the number's own price. Toll-free numbers and
numbers billed by the page are never chosen automatically, and a number with
no known price is never called the cheapest.

Caller ID is separate: a carrier decides which numbers it lets you show.
Faxbot shows the reply number as caller ID only when it is one of the same
trunk's own numbers (a number the carrier itself gives you on that account);
otherwise calls keep the line's caller ID and ``caller_id`` says why, citing
each carrier's published rule (``CALLER_ID_RULES``).

Nothing here places a call or changes a provider account.
"""
from dataclasses import dataclass, field
import re

import sqlalchemy as sa

from .numbers import InvalidNumber, normalize_number, stored_number


SETTING = 'fax_reply_number'
MAILBOX_SETTING = 'fax_reply_numbers'
# A typical received fax for comparing numbers (the research workload: five pages).
TYPICAL_PAGES = 5
_PAIR = re.compile(r'([A-Za-z0-9_-]{1,40})=(\+[1-9][0-9]{6,14})')
MAILBOX_ID = re.compile(r'[A-Za-z0-9_-]{1,40}')
# Providers that receive into Faxbot on the number configured for them, while they are the receiving provider.
# HumbleFax is not one of them until its receiving registers in own_numbers.RECEIVING_ACCOUNTS.
_RECEIVES_ON_CONFIGURED = {'efax': 'efax_caller_id', 'signalwire': 'signalwire_fax_from_e164'}

# Why a number cannot be your reply number; one sentence each.
NOT_YOURS = ('{number} is not one of your numbers. Use a number one of your fax accounts gives you '
             '(Providers lists them).')
NOT_RECEIVING = ('Faxes sent to {number} do not reach this Faxbot. Choose a number Faxbot receives on, or turn '
                 'on receiving for the provider that carries it.')
NO_MAILBOX = ('No rule under Numbers sends faxes for {number} to a mailbox. Add one under Numbers, Your numbers, '
              'then choose it again.')
OTHER_MAILBOX = ('Faxes for {number} go to the {other} mailbox, not {mailbox}. Choose a number that reaches '
                 '{mailbox}, or change its rule under Numbers.')
NOT_A_NUMBER = '{number} is not a fax number Faxbot can read. Enter it with its country code, such as +13035550100.'


class ReplyNumberRefused(ValueError):
    """A number that cannot be a reply number; the message is one plain sentence."""


@dataclass(frozen=True)
class Route:
    number: str
    mailbox_id: str
    mailbox: str


@dataclass(frozen=True)
class Candidate:
    number: str
    provider: str  # 'sip', 'humblefax', 'efax', 'signalwire', 'direct'
    provider_name: str
    kind: str  # 'local', 'toll_free', 'international' or 'other'
    receives: bool
    mailbox_id: str | None
    mailbox: str | None
    cost_micros: int | None  # a typical received fax; 0 within a flat plan; None when no price is known
    currency: str | None
    price: str  # how receiving on it is billed, in words
    per_page: bool = False
    flat: bool = False

    @property
    def avoid(self) -> bool:
        """Expensive to receive on: toll-free (billed to you by the minute) or billed by the page."""
        return self.kind == 'toll_free' or self.per_page


@dataclass(frozen=True)
class Choice:
    number: str | None
    source: str  # 'mailbox', 'organization', 'station', 'automatic' or 'line'
    sentence: str
    caller_id: str | None = None  # the caller ID to use instead of the line's, when the carrier allows it
    notes: tuple = field(default_factory=tuple)


# -- settings ----------------------------------------------------------------------------

def mailbox_numbers(values) -> dict:
    """{mailbox ID: reply number} from the saved setting; anything unreadable is left out."""
    text = getattr(values, MAILBOX_SETTING, '') or ''
    return {match.group(1): match.group(2) for match in _PAIR.finditer(text)}


def encode_mailbox_numbers(numbers: dict) -> str:
    """The setting's text for {mailbox ID: number}, sorted so the same choices save the same text."""
    pairs = []
    for mailbox_id, number in sorted(numbers.items()):
        if not _PAIR.fullmatch(f'{mailbox_id}={number}'):
            raise ValueError('Unsupported mailbox reply number')
        pairs.append(f'{mailbox_id}={number}')
    return ';'.join(pairs)


def _country(values):
    return getattr(values, 'fax_default_country', 'US') or 'US'


def read_number(text, values) -> str:
    """A typed number in E.164, or ReplyNumberRefused."""
    try:
        return normalize_number(str(text or '').strip(), country=_country(values))
    except InvalidNumber:
        raise ReplyNumberRefused(NOT_A_NUMBER.format(number=str(text or '').strip()[:40] or 'That')) from None


# -- what your numbers are, where they lead, and what they cost --------------------------

def _provider_numbers(values):
    """[(number, provider)] for every number your accounts give you, the trunk's first."""
    from .own_numbers import PROVIDER_NUMBERS
    country = _country(values)
    found = [(stored_number(number, country=country), 'sip') for number in getattr(values, 'sip_trunk_did_list', ())]
    caller = getattr(values, 'sip_trunk_caller_id', '') or ''
    if caller:
        found.append((stored_number(caller, country=country), 'sip'))
    for provider, fields in PROVIDER_NUMBERS.items():
        if provider == 'freeswitch':
            continue  # kept one release without a page; its caller number is not a receiving number
        for name in fields:
            number = getattr(values, name, '') or ''
            if number:
                found.append((stored_number(number, country=country), provider))
    direct = getattr(values, 'direct_fax_number', '') or ''
    if direct and getattr(values, 'direct_delivery_enabled', False):
        found.append((stored_number(direct, country=country), 'direct'))
    seen, result = set(), []
    for number, provider in found:
        if number.startswith('+') and number not in seen:
            seen.add(number)
            result.append((number, provider))
    return result


def receiving_numbers(values) -> set:
    """Numbers whose faxes reach this Faxbot."""
    from .own_numbers import receiving_numbers as own_receiving
    numbers = set(own_receiving(values))
    if getattr(values, 'inbound_enabled', False):
        provider = getattr(values, 'effective_inbound', '') or ''
        name = _RECEIVES_ON_CONFIGURED.get(provider)
        if name and getattr(values, name, ''):
            numbers.add(stored_number(getattr(values, name), country=_country(values)))
    return numbers


def account_numbers(values) -> set:
    from .own_numbers import account_numbers as own_accounts
    return set(own_accounts(values)) | {number for number, _ in _provider_numbers(values)}


def mailbox_routes(engine, values) -> list:
    """Every Numbers rule whose mailbox is in use, oldest first, as Route(number in E.164, mailbox)."""
    from .database import read_connection, reflect
    names = ('inbound_rules', 'access_mailbox_routes', 'access_resources', 'mailboxes')
    tables = reflect(engine, names)
    rules, routes, resources, mailboxes = (tables[name] for name in names)
    with read_connection(engine) as connection:
        rows = connection.execute(
            sa.select(rules.c.to_number, mailboxes.c.id, mailboxes.c.label)
            .select_from(rules.join(routes, routes.c.id == rules.c.id)
                         .join(mailboxes, mailboxes.c.id == routes.c.mailbox_id)
                         .join(resources, sa.and_(resources.c.kind == 'mailbox',
                                                  resources.c.mailbox_id == mailboxes.c.id)))
            .where(resources.c.enabled == 1)
            .order_by(rules.c.created_at, rules.c.id)).all()
    country = _country(values)
    return [Route(stored_number(str(row.to_number).strip(), country=country), row.id, row.label) for row in rows]


def mailbox_labels(engine) -> dict:
    """{mailbox ID: label} for every mailbox."""
    from .database import read_connection, reflect
    mailboxes = reflect(engine, ('mailboxes',))['mailboxes']
    with read_connection(engine) as connection:
        return {row.id: row.label for row in connection.execute(sa.select(mailboxes.c.id, mailboxes.c.label))}


def _route_for(routes, number):
    """The oldest rule for ``number`` (as Faxbot places a received fax), or None."""
    return next((route for route in routes if route.number == number), None)


def _card(store, provider, direction='inbound'):
    if store is None:
        return None
    try:
        card = store.card_for(provider, direction)
        if card is None and provider == 'humblefax':
            # HumbleFax's plan covers sending and receiving ("unlimited faxes").
            card = store.card_for(provider, 'outbound')
        return card
    except Exception:
        return None


def _price(values, number, provider, kind, store):
    """(cost of a typical received fax or None, currency, words, billed per page, flat plan)."""
    from .costs import estimate_cost, money_text, plan_fee_text, rate_text
    from .receiving import carrier_prices
    if provider in ('sip', 'direct'):
        preset = getattr(values, 'sip_trunk_preset', '') or ''
        card = None
        if preset:
            card = carrier_prices(preset).per_minute.get(kind if kind in ('local', 'toll_free') else 'local')
        if card is None and kind != 'toll_free':
            card = _card(store, 'sip')
        if card is None:
            if kind == 'toll_free':
                return None, None, 'Toll-free: your carrier bills you for every minute received.', False, False
            return None, None, 'No receiving price known for this number.', False, False
    else:
        card = _card(store, provider)
        if card is None:
            return None, None, 'No receiving price known for this number.', False, False
    if card.flat_plan:
        return 0, card.currency, (f'Included in your plan ({plan_fee_text(card.monthly_fee_micros, card.currency)} '
                                  'a month): nothing extra per fax.'), False, True
    cost = estimate_cost(card, TYPICAL_PAGES)
    words = rate_text(card) or 'Nothing charged per fax.'
    if cost is not None:
        words = f'{words[0].upper()}{words[1:]}; about {money_text(cost, card.currency)} for a {TYPICAL_PAGES}-page fax.'
    return cost, card.currency, words, card.per_page_micros > 0, False


def candidates(values, *, engine=None, store=None, routes=None) -> list:
    """Every number your accounts give you, with whether it receives here, its mailbox and its price."""
    from .receiving import number_kind
    from .carriers import carrier_label
    from ..provider_labels import provider_label
    if routes is None:
        routes = mailbox_routes(engine, values) if engine is not None else []
    receiving = receiving_numbers(values)
    result = []
    for number, provider in _provider_numbers(values):
        kind = number_kind(number, _country(values))
        route = _route_for(routes, number)
        cost, currency, words, per_page, flat = _price(values, number, provider, kind, store)
        if provider in ('sip', 'direct'):
            preset = getattr(values, 'sip_trunk_preset', '') or ''
            name = carrier_label(preset) if preset else provider_label('sip')
        else:
            name = provider_label(provider)
        result.append(Candidate(number, provider, name, kind, number in receiving,
                                route.mailbox_id if route else None, route.mailbox if route else None,
                                cost, currency, words, per_page, flat))
    return result


def refusal(values, number, *, routes, mailbox_id=None, labels=None) -> str | None:
    """Why ``number`` cannot be the reply number (for ``mailbox_id``, or the whole organization); None when it can."""
    if number not in account_numbers(values):
        return NOT_YOURS.format(number=number)
    if number not in receiving_numbers(values):
        return NOT_RECEIVING.format(number=number)
    route = _route_for(routes, number)
    if route is None:
        return NO_MAILBOX.format(number=number)
    if mailbox_id is not None and route.mailbox_id != mailbox_id:
        mailbox = (labels or {}).get(mailbox_id, 'this mailbox')
        return OTHER_MAILBOX.format(number=number, other=route.mailbox, mailbox=mailbox)
    return None


def check(values, text, *, engine, mailbox_id=None) -> str:
    """The number to save as a reply number, in E.164; ReplyNumberRefused with one sentence when it can't be."""
    number = read_number(text, values)
    routes = mailbox_routes(engine, values)
    labels = mailbox_labels(engine) if mailbox_id is not None else None
    if mailbox_id is not None and mailbox_id not in labels:
        raise ReplyNumberRefused('That mailbox no longer exists. Choose it again under Numbers, Mailboxes.')
    problem = refusal(values, number, routes=routes, mailbox_id=mailbox_id, labels=labels)
    if problem:
        raise ReplyNumberRefused(problem)
    return number


def _rank(candidate):
    # Flat plans first, then the lowest typical cost; local before other kinds on a tie.
    return (0 if candidate.flat else 1, candidate.cost_micros, 0 if candidate.kind == 'local' else 1, candidate.number)


def cheapest(found) -> Candidate | None:
    """The cheapest number to receive on that reaches a mailbox; never toll-free, per page or unpriced."""
    usable = [candidate for candidate in found if candidate.receives and candidate.mailbox_id
              and candidate.cost_micros is not None and not candidate.avoid]
    return min(usable, key=_rank) if usable else None


def advice(found) -> dict:
    """{'suggest': Candidate or None, 'avoid': [(Candidate, sentence)]}: what to publish for receiving."""
    from .costs import money_text
    avoid = []
    for candidate in found:
        if candidate.kind == 'toll_free':
            avoid.append((candidate, f"Don't publish {candidate.number} for receiving: on a toll-free number you pay "
                                     'for every minute of every fax sent to you.'))
        elif candidate.per_page:
            cost = (f' (about {money_text(candidate.cost_micros, candidate.currency)} for a {TYPICAL_PAGES}-page fax)'
                    if candidate.cost_micros is not None else '')
            avoid.append((candidate, f"Don't publish {candidate.number} for receiving: {candidate.provider_name} "
                                     f'charges for every page you receive{cost}.'))
    return {'suggest': cheapest(found), 'avoid': avoid}


# -- the number each fax shows --------------------------------------------------------------

def choose(values, *, engine=None, store=None, mailbox_id=None, routes=None) -> Choice:
    """The reply number for a fax (sent from ``mailbox_id``, when known), with where it came from.

    A saved number that no longer qualifies (its rule was removed, say) is
    skipped, and the next in the order is used. When the mailbox rules can't be
    read, a saved number is still used: it was checked when it was saved.
    """
    try:
        if routes is None and engine is not None:
            routes = mailbox_routes(engine, values)
    except Exception:
        routes = None
    notes = []

    def usable(number, scope_mailbox):
        if routes is None:
            return number in account_numbers(values)
        problem = refusal(values, number, routes=routes, mailbox_id=scope_mailbox)
        if problem:
            notes.append(problem)
        return problem is None

    if mailbox_id is not None:
        number = mailbox_numbers(values).get(mailbox_id)
        if number and usable(number, mailbox_id):
            return Choice(number, 'mailbox', f"Faxes from this mailbox show {number}, the mailbox's reply number.",
                          notes=tuple(notes))
    number = getattr(values, SETTING, '') or ''
    if number and usable(number, None):
        return Choice(number, 'organization', f'Faxes show {number}, your reply number.', notes=tuple(notes))
    station = (getattr(values, 'fax_station_id', '') or '').strip()
    if station:
        # As entered, as before this feature: the station ID setting is sent exactly as typed.
        return Choice(station, 'station', f'Faxes show {station}, the station ID you set.', notes=tuple(notes))
    if routes is not None:
        try:
            found = candidates(values, store=store, routes=routes)
        except Exception:
            found = []
        best = cheapest(found)
        if best is not None:
            return Choice(best.number, 'automatic',
                          f'Faxes show {best.number}, your cheapest number to receive on that reaches a mailbox.',
                          notes=tuple(notes))
    return Choice(None, 'line', "Faxes show the number of the line they leave on.", notes=tuple(notes))


def caller_id_for(values, number) -> str | None:
    """The reply number as the trunk's caller ID when the carrier gives it to you on that trunk, else None."""
    if not number:
        return None
    country = _country(values)
    trunk = {stored_number(item, country=country) for item in getattr(values, 'sip_trunk_did_list', ())}
    caller = getattr(values, 'sip_trunk_caller_id', '') or ''
    if caller:
        trunk.add(stored_number(caller, country=country))
    return number if number in trunk else None


# Each carrier's published rule for showing a number other than the line's as caller ID, with its source and the
# date it was read. Faxbot confirms only the first kind itself (a number on the same trunk); for any other number it
# keeps the line's caller ID and says why. A carrier with no rule here has not published one Faxbot could find.
CALLER_ID_RULES = {
    'telnyx': {
        'other': ('Telnyx shows another number only when it was bought on your Telnyx account, ported to Telnyx or '
                  'verified in the Telnyx portal (Numbers, Verified Numbers), and rejects calls that show any other '
                  "number; Faxbot can't confirm that for this number"),
        'source_url': 'https://support.telnyx.com/en/articles/6790265-verified-numbers-faq', 'read_on': '2026-10-07'},
    'signalwire': {
        'other': ('SignalWire shows another number only when it was bought on your SignalWire space or verified there '
                  "(set it as Send As on the SIP credential); Faxbot can't confirm that for this number"),
        'source_url': 'https://signalwire.com/docs/platform/voice/how-to-set-caller-id-or-cnam',
        'read_on': '2026-10-07'},
    'flowroute': {
        'other': ('Flowroute accepts only a valid local North American number assigned to you, never a toll-free one, '
                  "and Faxbot can't confirm that this number is one of them"),
        'source_url': 'https://support.bcmone.com/flowroute-support/docs/outbound-ani-requirements',
        'read_on': '2026-10-07'},
}
# Fax services set the sending number themselves: Faxbot's header line and station ID apply to faxes sent over the
# SIP trunk (both engines), never to these.
CLOUD_PROVIDERS = ('humblefax', 'efax', 'signalwire', 'sinch', 'phaxio', 'documo')


def caller_id(values, number) -> list:
    """[{'provider', 'shows', 'sentence', 'source_url', 'read_on'}] for the providers you use, for ``number``."""
    from .carriers import carrier_label
    from ..provider_labels import provider_label
    if not number:
        return []
    result = []
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    if getattr(values, 'sip_trunk_host', '') or preset:
        name = carrier_label(preset) if preset else provider_label('sip')
        rule = CALLER_ID_RULES.get(preset, {})
        line = getattr(values, 'sip_trunk_caller_id', '') or 'its own number'
        if caller_id_for(values, number):
            result.append({'provider': name, 'shows': True, 'source_url': rule.get('source_url'),
                           'read_on': rule.get('read_on'),
                           'sentence': f'{name} calls show {number} as caller ID: {name} gives you that number on '
                                       'this trunk.'})
        else:
            why = rule.get('other') or (f"{name} has not published which other numbers it lets you show, so Faxbot "
                                        "can't confirm it allows this one")
            result.append({'provider': name, 'shows': False, 'source_url': rule.get('source_url'),
                           'read_on': rule.get('read_on'),
                           'sentence': f'{name} calls keep showing {line} as caller ID: {why}.'})
    in_use = {getattr(values, 'effective_outbound', ''), *_outbound_routes(values)}
    for provider in CLOUD_PROVIDERS:
        if provider not in in_use:
            continue
        label = provider_label(provider)
        result.append({'provider': label, 'shows': False, 'source_url': None, 'read_on': None,
                       'sentence': (f'Faxes sent through {label} show the number set in your {label} account; the '
                                    'reply number is printed on faxes sent over your SIP trunk.')})
    return result


def _outbound_routes(values):
    text = getattr(values, 'outbound_routes', '') or ''
    return {part.strip().split(':', 1)[0] for part in str(text).split(',') if part.strip()}


# -- 47 CFR 68.318(d): the header line on every page --------------------------------------

HEADER_MISSING = ('Faxes go out with no header line, which US rules ask for on every fax. Enter your organization '
                  'name as the header text.')


def header_problem(values) -> str | None:
    """One sentence when sent faxes would lack the 68.318(d) header line; None when every page carries it."""
    return HEADER_MISSING if not (getattr(values, 'fax_header', '') or '').strip() else None


def tagline(header: str) -> str:
    """The SSL Fax engine's header line for each page: date and time, your header, the reply number, the page.

    HylaFAX+ passes the format through strftime and then expands its own
    codes (faxd/TagLine.c++): %%l is the station ID the job sends (the reply
    number, with UseJobTSI), %%P and %%T the page and the page count. A % in
    the header text is written %%%% so it prints as itself; | separates fields.
    """
    text = ' '.join(str(header or '').replace('|', ' ').split())[:80]
    text = text.replace('%', '%%%%')
    return f'%d %b %Y %H:%M|{text}|%%l|Page %%P of %%T'
