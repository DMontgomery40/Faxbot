"""Where Faxbot may dial: a toll-fraud guard that is also a cost control (N14).

Abuse of a fax line's calling account is the costliest thing that can happen
to it: someone who can submit faxes dials premium-rate, satellite or far-away
numbers, and the carrier bills every minute. Faxbot therefore dials only the
classes of numbers it may dial. Every fax is checked once, when it is
accepted, against the number its calls will dial (an approved alternate
replaces the original). A fax to a class it may not dial is held in Sent with
one sentence and the existing approval flow (owner's decision of 2026-10-07:
no allowed route left means hold in Sent); it is never failed and never
dialed until someone decides.

Classes (``dial_class``), built on ``destinations.classify`` and the
number's type and calling code:

- national geographic, national toll-free and national mobile numbers in the
  installation's country (``fax_default_country``);
- one class per other country (``country:GB``), by the country the number
  belongs to, as the sending rules read it;
- premium-rate, special-service (personal numbers, shared-cost, pager and
  voicemail ranges, international freephone) and satellite or international
  network numbers (+870, +881, +882, +883), wherever they are.

What Faxbot may dial, unless an administrator changed it (the newest row per
class in ``dialing_class_changes`` counts):

- national classes: allowed;
- a country: allowed when this installation had already delivered a fax there
  over a call before it started checking (recorded once, with that reason),
  when an active routing rule names it (its countries, prefixes, numbers,
  lists or regions), when a saved recipient's number is there, or when a
  registered sender (``sender_pins``) names a recipient there;
- premium-rate, special-service and satellite numbers: never, unless an
  administrator allows that exact class. Approving a held fax does not dial
  one of these while its class is not allowed.

A class may also carry a per-minute price ceiling (default none). A fax whose
cheapest allowed calling route costs more a minute than that, or whose price
per minute is unknown, waits for approval. A route that does not bill by the
minute (per page, or included in a plan) counts as within the ceiling.

Limits: the check runs at acceptance only. Dispatch may still choose a pricier
account inside the fax's sending rules; the ceiling compares the cheapest one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import uuid

import phonenumbers
from phonenumbers import PhoneNumberType
import sqlalchemy as sa


GEOGRAPHIC = 'national_geographic'
TOLL_FREE = 'national_toll_free'
MOBILE = 'national_mobile'
PREMIUM = 'premium'
SPECIAL = 'special_service'
SATELLITE = 'satellite'
COUNTRY = 'country:'
NATIONAL = (GEOGRAPHIC, TOLL_FREE, MOBILE)
FENCED = (PREMIUM, SPECIAL, SATELLITE)
FIXED = NATIONAL + FENCED

# Calling codes that belong to no country (ITU-T E.164 assignments for global services).
SATELLITE_CODES = frozenset({870, 881, 882, 883})   # Inmarsat, global mobile satellite, international networks
SPECIAL_CODES = frozenset({800, 808, 878, 888})     # international freephone, shared cost, personal, OCHA
PREMIUM_CODES = frozenset({979})                    # international premium rate
# Number types that are charged and routed like special services, not like an ordinary line. UK 03 numbers
# (UAN) and VoIP ranges are charged like geographic numbers, so they stay national.
SPECIAL_TYPES = frozenset({PhoneNumberType.SHARED_COST, PhoneNumberType.PERSONAL_NUMBER, PhoneNumberType.PAGER,
                           PhoneNumberType.VOICEMAIL})
NO_CALL_ACCOUNTS = ('local', 'direct')

# Where the administrator changes this, named once so a navigation change is one edit.
WHERE = 'Providers → In use → Where Faxbot may dial'

CLASS_LABELS = {GEOGRAPHIC: 'Numbers in your country', TOLL_FREE: 'Toll-free numbers in your country',
                MOBILE: 'Mobile numbers in your country', PREMIUM: 'Premium-rate numbers',
                SPECIAL: 'Special-service numbers', SATELLITE: 'Satellite and international network numbers'}
# The class in a sentence ("calls to premium-rate numbers").
CLASS_TEXT = {GEOGRAPHIC: 'numbers in your country', TOLL_FREE: 'toll-free numbers in your country',
              MOBILE: 'mobile numbers in your country', PREMIUM: 'premium-rate numbers',
              SPECIAL: 'special-service numbers', SATELLITE: 'satellite and international network numbers'}


class GuardInputError(ValueError):
    """The change cannot be made as asked; one plain sentence."""


class GuardConflict(RuntimeError):
    """Someone changed this class since it was read; one plain sentence."""


# Classes ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DialClass:
    key: str                      # GEOGRAPHIC, ..., or 'country:GB'
    region: str | None = None     # the country for a country class

    @property
    def country(self):
        return self.key.startswith(COUNTRY)


def _home_code(home):
    return phonenumbers.country_code_for_region(home) or None


def country_key(region):
    return f'{COUNTRY}{region.upper()}'


def dial_class(number, home_country='US'):
    """The class of ``number`` (E.164) for an installation in ``home_country``; None when it cannot be read."""
    home = (home_country or 'US').upper()
    text = (number or '').strip()
    try:
        parsed = phonenumbers.parse(text, None if text.startswith('+') else home)
    except phonenumbers.NumberParseException:
        return None
    code = parsed.country_code
    if code in SATELLITE_CODES:
        return DialClass(SATELLITE)
    if code in PREMIUM_CODES:
        return DialClass(PREMIUM)
    if code in SPECIAL_CODES:
        return DialClass(SPECIAL)
    if not phonenumbers.is_valid_number(parsed):
        # Possible but not in the numbering plan Faxbot knows (a new range, or a test number such as 555-0100):
        # national with the home calling code, otherwise the calling code's main country.
        if code == _home_code(home):
            return DialClass(GEOGRAPHIC)
        region = phonenumbers.region_code_for_country_code(code)
        if region in (None, 'ZZ', '001'):
            return DialClass(SPECIAL)
        return DialClass(country_key(region), region)
    kind = phonenumbers.number_type(parsed)
    region = phonenumbers.region_code_for_number(parsed)
    if kind == PhoneNumberType.PREMIUM_RATE:
        return DialClass(PREMIUM)
    if kind in SPECIAL_TYPES:
        return DialClass(SPECIAL)
    if region in (None, 'ZZ', '001'):
        return DialClass(SPECIAL)
    # North American toll-free codes are shared by every NANP country: toll-free from any of them.
    shared = kind == PhoneNumberType.TOLL_FREE and code == 1 and home in phonenumbers.region_codes_for_country_code(1)
    if region == home or shared:
        if kind == PhoneNumberType.TOLL_FREE:
            return DialClass(TOLL_FREE)
        if kind == PhoneNumberType.MOBILE:
            return DialClass(MOBILE)
        return DialClass(GEOGRAPHIC)
    return DialClass(country_key(region), region)


def parse_class(text, home_country='US'):
    """A class the administrator typed: a class name ("premium", "national-mobile"), a two-letter country code
    ("GB") or a country calling code ("+44"); the class key, or raises ``GuardInputError``."""
    value = (text or '').strip()
    words = value.lower().replace('-', '_').replace(' ', '_')
    aliases = {'geographic': GEOGRAPHIC, 'national': GEOGRAPHIC, 'toll_free': TOLL_FREE, 'mobile': MOBILE,
               'premium_rate': PREMIUM, 'special': SPECIAL, 'satellite_and_international_network': SATELLITE}
    words = aliases.get(words, words)
    if words in FIXED:
        return words
    if words.startswith(COUNTRY):
        value = value[len(COUNTRY):]
    if value.startswith('+') and value[1:].isdigit():
        region = phonenumbers.region_code_for_country_code(int(value[1:]))
        if region in (None, 'ZZ', '001'):
            raise GuardInputError(f'{value} is not a country calling code. Use premium, special-service or '
                                  'satellite for numbers that belong to no country.')
        if region == (home_country or 'US').upper():
            raise GuardInputError('That is your own country; choose national, national-toll-free or national-mobile.')
        return country_key(region)
    region = value.upper()
    if len(region) == 2 and region.isalpha() and region in phonenumbers.SUPPORTED_REGIONS:
        if region == (home_country or 'US').upper():
            raise GuardInputError('That is your own country; choose national, national-toll-free or national-mobile.')
        return country_key(region)
    raise GuardInputError('Name a class (national, national-toll-free, national-mobile, premium, special-service '
                          'or satellite), a two-letter country code such as GB, or a calling code such as +44.')


def class_label(key):
    if key in CLASS_LABELS:
        return CLASS_LABELS[key]
    from .destinations import country_name
    region = key[len(COUNTRY):] if key.startswith(COUNTRY) else key
    name = country_name(region)
    return name[4].upper() + name[5:] if name.startswith('the ') else name


def class_text(key):
    """The class in a sentence: "premium-rate numbers", "numbers in the United Kingdom"."""
    if key in CLASS_TEXT:
        return CLASS_TEXT[key]
    from .destinations import country_name
    return f'numbers in {country_name(key[len(COUNTRY):])}'


# Tables ---------------------------------------------------------------------------------------------------------

def _changes():
    return sa.table('dialing_class_changes', sa.column('id'), sa.column('class_key'), sa.column('state'),
                    sa.column('reason'), sa.column('ceiling_micros', sa.Integer()), sa.column('currency'),
                    sa.column('first_delivered_at', sa.DateTime()), sa.column('actor_principal_id'),
                    sa.column('actor_name'), sa.column('created_at', sa.DateTime()))


def _state():
    return sa.table('dialing_guard_state', sa.column('id'), sa.column('history_checked_at', sa.DateTime()))


def _guard_holds():
    return sa.table('dialing_guard_holds', sa.column('id'), sa.column('job_id'), sa.column('class_key'),
                    sa.column('dialed_number'), sa.column('why'), sa.column('rate_micros', sa.Integer()),
                    sa.column('ceiling_micros', sa.Integer()), sa.column('currency'),
                    sa.column('created_at', sa.DateTime()))


@dataclass(frozen=True)
class Setting:
    class_key: str
    state: str                    # 'allowed', 'blocked' or 'default'
    reason: str                   # 'administrator' or 'delivered'
    ceiling_micros: int | None = None
    currency: str | None = None
    actor_name: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class Policy:
    settings: dict                # class key -> newest Setting
    delivered: dict               # country class key -> first delivered at, recorded once


def policy_on(connection):
    """The newest setting of each class, and the countries recorded as already delivered to."""
    changes = _changes()
    rows = connection.execute(sa.select(changes).order_by(changes.c.created_at, changes.c.id)).mappings().all()
    settings, delivered = {}, {}
    for row in rows:
        settings[row['class_key']] = Setting(row['class_key'], row['state'], row['reason'], row['ceiling_micros'],
                                             row['currency'], row['actor_name'], row['created_at'])
        if row['reason'] == 'delivered':
            delivered.setdefault(row['class_key'], row['first_delivered_at'])
    return Policy(settings, delivered)


def ensure_history_on(connection, home_country, now):
    """Record once the countries this installation had already delivered a fax to over a call, with that reason.

    Runs on the caller's transaction, under the configuration lock, so two acceptances never record it twice.
    Only placed calls count: a fax delivered inside Faxbot or directly to a partner dialed nothing.
    """
    state = _state()
    if connection.execute(sa.select(state.c.id).where(state.c.id == 'installation')).first() is not None:
        return False
    costs = sa.table('delivery_attempt_costs', sa.column('destination'), sa.column('route'), sa.column('outcome'),
                     sa.column('created_at', sa.DateTime()))
    first_at = sa.func.min(costs.c.created_at, type_=sa.DateTime())
    rows = connection.execute(sa.select(costs.c.destination, first_at).where(
        costs.c.outcome == 'success', costs.c.route.not_in(NO_CALL_ACCOUNTS)).group_by(costs.c.destination)).all()
    first = {}
    for destination, at in rows:
        found = dial_class(destination, home_country)
        if found is None or not found.country:
            continue
        if found.key not in first or (at is not None and (first[found.key] is None or at < first[found.key])):
            first[found.key] = at
    changes = _changes()
    for key in sorted(first):
        connection.execute(changes.insert().values(
            id=uuid.uuid4().hex, class_key=key, state='allowed', reason='delivered', ceiling_micros=None,
            currency=None, first_delivered_at=first[key], actor_principal_id=None, actor_name=None, created_at=now))
    connection.execute(state.insert().values(id='installation', history_checked_at=now))
    return True


# What names a country ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Named:
    countries: dict               # region -> the rule's name
    prefixes: dict                # E.164 prefix -> the rule's name
    recipients: dict              # region -> a saved recipient's number (one of them)


def _active_documents(connection):
    revisions = sa.table('routing_rule_revisions', sa.column('id'), sa.column('document'))
    state = sa.table('routing_rule_state', sa.column('scope_kind'), sa.column('scope_id'),
                     sa.column('active_revision_id'))
    rows = connection.execute(sa.select(state.c.scope_kind, revisions.c.document).join(
        revisions, revisions.c.id == state.c.active_revision_id)).all()
    found = []
    for kind, text in rows:
        try:
            document = json.loads(text) if text else {}
        except ValueError:
            continue
        if isinstance(document, dict):
            found.append((kind, document))
    return found


def _region_of(number):
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(number)) or None
    except phonenumbers.NumberParseException:
        return None


def _strings(value):
    return [item for item in value or () if isinstance(item, str) and item]


def named_by_rules_on(connection):
    """Countries and prefixes an active routing rule's ``when`` names (``unless`` and limits never allow)."""
    documents = _active_documents(connection)
    organization = next((document for kind, document in documents if kind == 'organization'), {})
    lists = organization.get('lists') if isinstance(organization.get('lists'), dict) else {}
    regions = organization.get('regions') if isinstance(organization.get('regions'), dict) else {}
    countries, prefixes = {}, {}
    for _, document in documents:
        for rule in document.get('routes') or ():
            if not isinstance(rule, dict) or rule.get('on') is False:
                continue
            when = rule.get('when') if isinstance(rule.get('when'), dict) else {}
            destination = when.get('destination') if isinstance(when.get('destination'), dict) else {}
            name = str(rule.get('name') or rule.get('id') or 'a routing rule')[:200]
            numbers = list(_strings(destination.get('numbers')))
            for region in _strings(destination.get('countries')):
                countries.setdefault(region.upper(), name)
            for prefix in _strings(destination.get('prefixes')):
                prefixes.setdefault(prefix, name)
            for key in _strings(destination.get('lists')):
                group = lists.get(key) if isinstance(lists.get(key), dict) else {}
                numbers.extend(_strings(group.get('numbers')))
                for prefix in _strings(group.get('prefixes')):
                    prefixes.setdefault(prefix, name)
            for key in _strings(destination.get('regions')):
                area = regions.get(key) if isinstance(regions.get(key), dict) else {}
                for region in _strings(area.get('countries')):
                    countries.setdefault(region.upper(), name)
                for prefix in _strings(area.get('prefixes')):
                    prefixes.setdefault(prefix, name)
            for number in numbers:
                region = _region_of(number)
                if region:
                    countries.setdefault(region, name)
    return countries, prefixes


def recipient_in_on(connection, found, home_country):
    """A saved recipient's number in ``found``'s country (``delivery_destinations``), or None."""
    code = phonenumbers.country_code_for_region(found.region) if found.region else 0
    if not code:
        return None
    destinations = sa.table('delivery_destinations', sa.column('phone_number'))
    rows = connection.execute(sa.select(destinations.c.phone_number).where(
        destinations.c.phone_number.like(f'+{code}%')).limit(500)).scalars().all()
    for number in rows:
        other = dial_class(number, home_country)
        if other is not None and other.key == found.key:
            return number
    return None


def recipients_on(connection, home_country, limit=5000):
    """region -> one saved recipient number there, for the list view."""
    destinations = sa.table('delivery_destinations', sa.column('phone_number'))
    found = {}
    for number in connection.execute(sa.select(destinations.c.phone_number).limit(limit)).scalars():
        other = dial_class(number, home_country)
        if other is not None and other.country:
            found.setdefault(other.region, number)
    return found


# The verdict ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    key: str
    allowed: bool
    source: str                   # 'national', 'delivered', 'rule', 'recipient', 'registered_sender',
    #                               'administrator', 'default'
    detail: str | None = None     # the rule's name, or the recipient's number
    setting: Setting | None = None


def verdict_on(connection, found, policy, home_country, number=None, named=None):
    """Whether Faxbot may dial ``found`` (a ``DialClass``) now, and why."""
    setting = policy.settings.get(found.key)
    explicit = setting.state if setting is not None and setting.state != 'default' else None
    if explicit == 'blocked':
        return Verdict(found.key, False, 'administrator', setting=setting)
    if explicit == 'allowed':
        source = 'delivered' if setting.reason == 'delivered' else 'administrator'
        return Verdict(found.key, True, source, setting=setting)
    if found.key in NATIONAL:
        return Verdict(found.key, True, 'national', setting=setting)
    if found.key in FENCED:
        return Verdict(found.key, False, 'default', setting=setting)
    if found.key in policy.delivered:
        return Verdict(found.key, True, 'delivered', setting=setting)
    countries, prefixes = named if named is not None else named_by_rules_on(connection)
    if found.region in countries:
        return Verdict(found.key, True, 'rule', countries[found.region], setting=setting)
    if number:
        match = max((prefix for prefix in prefixes if number.startswith(prefix)), key=len, default=None)
        if match is not None:
            return Verdict(found.key, True, 'rule', prefixes[match], setting=setting)
    recipient = recipient_in_on(connection, found, home_country)
    if recipient is not None:
        return Verdict(found.key, True, 'recipient', recipient, setting=setting)
    # A registered sender is an administrator's explicit record naming that recipient (sender_pins, N17).
    from .sender_pins import pinned_in_on
    pinned = pinned_in_on(connection, lambda number: dial_class(number, home_country), found.key)
    if pinned is not None:
        return Verdict(found.key, True, 'registered_sender', pinned, setting=setting)
    return Verdict(found.key, False, 'default', setting=setting)


# Acceptance ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rate:
    account: str
    label: str
    per_minute_micros: int | None    # None: no published price for this kind of number; 0: not billed by the minute
    currency: str | None


@dataclass(frozen=True)
class Preview:
    """Read before the acceptance lock: the dialed number, its class and, when a ceiling may apply, its rates."""
    number: str
    home_country: str
    found: DialClass | None
    rates: tuple | None = None


def _rate(engine, values, account, number):
    from .predict_facts import facts_for
    from .store import RouteStore
    key, provider = account.key, getattr(account, 'provider', None) or account.key
    facts_key = key
    if key != provider and RouteStore(engine).card_for(key) is None:
        facts_key = provider
    facts = facts_for(facts_key, number, engine=engine, values=values, account=key)
    if facts.terms is None:
        return Rate(key, facts.label, None, None)
    card = facts.terms.card
    return Rate(key, getattr(account, 'label', None) or facts.label, card.per_minute_micros, card.currency)


def preview(engine, values, *, destination, decision, accounts):
    """The dialed number's class, and its per-minute prices when its class has a ceiling (read before the lock)."""
    home = getattr(values, 'fax_default_country', 'US') or 'US'
    envelope = decision.envelope
    number = envelope.dial.number if envelope.dial is not None else destination
    found = dial_class(number, home)
    if found is None:
        return Preview(number, home, None)
    with engine.connect() as connection:
        setting = policy_on(connection).settings.get(found.key)
    if setting is None or setting.ceiling_micros is None:
        return Preview(number, home, found)
    by_key = {account.key: account for account in accounts}
    rates = tuple(_rate(engine, values, by_key[key], number) for key in envelope.accounts
                  if key in by_key and key not in NO_CALL_ACCOUNTS)
    return Preview(number, home, found, rates)


@dataclass(frozen=True)
class Held:
    key: str
    why: str                      # schema_dialing.WHY
    sentence: str
    rate_micros: int | None = None
    ceiling_micros: int | None = None
    currency: str | None = None


def _money(micros, currency):
    from .delivered import short_money_text
    return short_money_text(micros, currency or 'USD')


def held_sentence(key, why, *, rate=None, ceiling=None, currency=None, route=None):
    """Why the guard holds a fax, in one sentence for Sent."""
    what = class_text(key)
    if why == 'fenced':
        return (f'Faxbot never dials {what} unless you allow them in {WHERE}; allow them there first, then '
                'approve this fax, or refuse it. Nothing was sent.')
    if why == 'blocked':
        return f'You blocked {what} in {WHERE}, so this fax waits for your approval. Nothing was sent.'
    if why == 'not_allowed':
        return (f'Faxbot has not sent to {what} before and no rule or saved recipient names it, so this fax waits '
                f'for your approval; to allow it from now on, use {WHERE}. Nothing was sent.')
    if why == 'over_ceiling':
        return (f'Calls to {what} cost {_money(rate, currency)} a minute{f" by {route}" if route else ""}, over the '
                f'{_money(ceiling, currency)} a minute you set, so this fax waits for your approval. Nothing was sent.')
    return (f'Faxbot has no per-minute price for calls to {what}, and you set a ceiling of '
            f'{_money(ceiling, currency)} a minute, so this fax waits for your approval. Nothing was sent.')


def _over_ceiling(found, setting, rates):
    """A ``Held`` when the cheapest allowed calling route is over the class's ceiling or unknown, else None."""
    ceiling, currency = setting.ceiling_micros, setting.currency
    if not rates:
        return Held(found.key, 'unknown_rate', held_sentence(found.key, 'unknown_rate', ceiling=ceiling,
                                                             currency=currency), None, ceiling, currency)
    known = [rate for rate in rates if rate.per_minute_micros is not None
             and (rate.per_minute_micros == 0 or rate.currency == currency)]
    if not known:
        return Held(found.key, 'unknown_rate', held_sentence(found.key, 'unknown_rate', ceiling=ceiling,
                                                             currency=currency), None, ceiling, currency)
    cheapest = min(known, key=lambda rate: rate.per_minute_micros)
    if cheapest.per_minute_micros <= ceiling:
        return None
    return Held(found.key, 'over_ceiling', held_sentence(
        found.key, 'over_ceiling', rate=cheapest.per_minute_micros, ceiling=ceiling, currency=currency,
        route=cheapest.label), cheapest.per_minute_micros, ceiling, currency)


def check_on(connection, prepared, now):
    """The ``Held`` reason for a fax at acceptance (``prepared`` is its ``Preview``), or None when it may go."""
    if prepared.found is None:
        return Held('unknown', 'not_allowed', 'Faxbot cannot tell what kind of number this is, so this fax waits '
                                              'for your approval. Nothing was sent.')
    ensure_history_on(connection, prepared.home_country, now)
    policy = policy_on(connection)
    found = prepared.found
    verdict = verdict_on(connection, found, policy, prepared.home_country, number=prepared.number)
    if not verdict.allowed:
        why = 'fenced' if found.key in FENCED else ('blocked' if verdict.source == 'administrator' else 'not_allowed')
        return Held(found.key, why, held_sentence(found.key, why))
    setting = policy.settings.get(found.key)
    if setting is not None and setting.ceiling_micros is not None:
        return _over_ceiling(found, setting, prepared.rates)
    return None


def record_on(connection, prepared, *, job_id, decision_id, now, requested_by=None, digest=None, also_held=False):
    """Check the fax in the acceptance transaction and, when it may not be dialed, hold it for approval.

    Writes an approval hold (``outbound_holds``) with the sentence and its guard row; returns the ``Held`` or None.
    ``also_held``: the sending rules already held the fax, so the held event is already written.
    """
    found = check_on(connection, prepared, now)
    if found is None:
        return None
    from . import envelope as envelopes, holds as hold_store
    t = envelopes.tables(connection)
    hold_id = hold_store.create_on(connection, t, job_id=job_id, kind='approval', decision_id=decision_id, now=now,
                                   requested_by=requested_by, digest=digest, reason=found.sentence)
    connection.execute(_guard_holds().insert().values(
        id=hold_id, job_id=job_id, class_key=found.key[:32], dialed_number=prepared.number[:32], why=found.why,
        rate_micros=found.rate_micros, ceiling_micros=found.ceiling_micros, currency=found.currency, created_at=now))
    if not also_held:
        from ..outbound_store import _event
        _event(connection, sa.table('outbound_events', sa.column('id'), sa.column('job_id'), sa.column('attempt_id'),
                                    sa.column('kind'), sa.column('dedupe_key'), sa.column('details'),
                                    sa.column('created_at')),
               job_id, 'route_held', now)
    return found


def refuse_release_on(connection, hold_row, home_country=None):
    """Raise when approving this hold would dial a premium-rate, special-service or satellite number whose class
    is still not allowed. Other guard holds, and holds the guard did not write, may be approved as usual."""
    from .holds import HoldInputError
    guard = _guard_holds()
    row = connection.execute(sa.select(guard.c.class_key, guard.c.why).where(
        guard.c.id == hold_row['id'])).first()
    if row is None or row.why != 'fenced':
        return
    setting = policy_on(connection).settings.get(row.class_key)
    if setting is not None and setting.state == 'allowed':
        return
    raise HoldInputError(f'Faxbot never dials {class_text(row.class_key)} unless you allow them first in {WHERE}. '
                         'Allow them there, then approve this fax, or refuse it.')


def open_guard_hold_on(connection, job_id):
    """Whether the guard holds this fax (an open approval hold with a guard row)."""
    guard = _guard_holds()
    holds = sa.table('outbound_holds', sa.column('id'), sa.column('state'))
    return connection.execute(sa.select(guard.c.id).select_from(guard.join(holds, holds.c.id == guard.c.id))
                              .where(guard.c.job_id == job_id, holds.c.state == 'open')).first() is not None


# The administrator's view and changes ------------------------------------------------------------------------------

def _when(value):
    return value.isoformat(timespec='seconds') if isinstance(value, datetime) else value


def _row(key, verdict, policy, *, changeable=True):
    setting = verdict.setting
    allowed = verdict.allowed
    if verdict.source == 'national':
        why = 'Allowed: Faxbot dials numbers in your own country.'
    elif verdict.source == 'delivered':
        first = policy.delivered.get(key)
        why = 'Allowed: Faxbot had already delivered faxes there before it started checking.'
        return _view(key, allowed, why, setting, first_delivered=first, source=verdict.source)
    elif verdict.source == 'rule':
        why = f'Allowed: the routing rule ‘{verdict.detail}’ names it.'
    elif verdict.source == 'recipient':
        why = f'Allowed: a saved recipient, {verdict.detail}, is there.'
    elif verdict.source == 'registered_sender':
        why = f'Allowed: a registered sender names {verdict.detail}.'
    elif verdict.source == 'administrator':
        who = f' by {setting.actor_name}' if setting is not None and setting.actor_name else ''
        why = f'{"Allowed" if allowed else "Blocked"}{who}.'
    elif key in FENCED:
        why = 'Blocked: Faxbot never dials these unless you allow them.'
    else:
        why = 'Not allowed yet: faxes there wait for your approval.'
    return _view(key, allowed, why, setting, source=verdict.source, changeable=changeable)


def _view(key, allowed, why, setting, *, first_delivered=None, source=None, changeable=True):
    ceiling = None
    if setting is not None and setting.ceiling_micros is not None:
        ceiling = {'amount': _amount(setting.ceiling_micros), 'currency': setting.currency,
                   'text': f'{_money(setting.ceiling_micros, setting.currency)} a minute'}
    return {'key': key, 'label': class_label(key), 'allowed': allowed, 'sentence': why, 'source': source,
            'chosen': bool(setting is not None and setting.reason == 'administrator' and setting.state != 'default'),
            'ceiling': ceiling, 'first_delivered_at': _when(first_delivered), 'changeable': changeable}


def _amount(micros):
    from decimal import Decimal
    from .costs import MICROS
    return format(Decimal(micros) / MICROS, 'f').rstrip('0').rstrip('.') or '0'


def view_on(connection, home_country, now):
    """Every class and every country Faxbot knows about here, each with whether it may dial it and why."""
    ensure_history_on(connection, home_country, now)
    policy = policy_on(connection)
    named = named_by_rules_on(connection)
    countries, prefixes = named
    recipients = recipients_on(connection, home_country)
    rows = []
    for key in FIXED:
        rows.append(_row(key, verdict_on(connection, DialClass(key), policy, home_country, named=named), policy))
    keys = {key for key in policy.settings if key.startswith(COUNTRY)} | set(policy.delivered)
    keys |= {country_key(region) for region in countries} | {country_key(region) for region in recipients}
    home = (home_country or 'US').upper()
    country_rows = []
    for key in sorted(keys):
        region = key[len(COUNTRY):]
        if region == home:
            continue
        found = DialClass(key, region)
        country_rows.append(_row(key, verdict_on(connection, found, policy, home_country, named=named), policy))
    country_rows.sort(key=lambda row: row['label'])
    prefix_rows = [{'prefix': prefix, 'rule': name} for prefix, name in sorted(prefixes.items())]
    return {'classes': rows, 'countries': country_rows, 'prefixes': prefix_rows,
            'other_countries': 'Any other country: faxes wait for your approval until you allow it here, a routing '
                               'rule names it, or a saved recipient is there.',
            'where': WHERE}


KEEP = object()   # ``change_on``: keep the class's current ceiling


def change_on(connection, key, *, state, ceiling=None, currency='USD', actor_principal_id=None, actor_name=None,
              home_country='US', now):
    """Record an administrator's change to a class: ``state`` 'allowed', 'blocked' or 'default'; ``ceiling``
    decimal text ("0.25") for a per-minute ceiling, '' or None for none, ``KEEP`` for the current one. Returns the
    new ``Setting``."""
    from .costs import InvalidRateCard, parse_amount
    if state not in ('allowed', 'blocked', 'default'):
        raise GuardInputError('Choose allowed, blocked or back to Faxbot’s default.')
    if key not in FIXED and not key.startswith(COUNTRY):
        raise GuardInputError('There is no such class of numbers.')
    if key.startswith(COUNTRY):
        region = key[len(COUNTRY):]
        if region not in phonenumbers.SUPPORTED_REGIONS or region == (home_country or 'US').upper():
            raise GuardInputError('Choose another country than your own.')
    micros = None
    if ceiling is KEEP:
        current = policy_on(connection).settings.get(key)
        micros = current.ceiling_micros if current is not None else None
        currency = current.currency if micros is not None else currency
        ceiling = None
    if ceiling not in (None, ''):
        try:
            micros = parse_amount(str(ceiling))
        except InvalidRateCard as error:
            raise GuardInputError('Enter the ceiling as a price a minute, such as 0.25.') from error
    currency = (currency or 'USD').upper()[:3] if micros is not None else None
    ensure_history_on(connection, home_country, now)
    connection.execute(_changes().insert().values(
        id=uuid.uuid4().hex, class_key=key, state=state, reason='administrator', ceiling_micros=micros,
        currency=currency, first_delivered_at=None, actor_principal_id=actor_principal_id,
        actor_name=(actor_name or None) and actor_name[:200], created_at=now))
    return Setting(key, state, 'administrator', micros, currency, actor_name, now)


def change_sentence(key, setting):
    what = class_text(key)
    ceiling = (f' Calls costing more than {_money(setting.ceiling_micros, setting.currency)} a minute wait for '
               'approval.' if setting.ceiling_micros is not None else '')
    if setting.state == 'allowed':
        return f'Faxbot may dial {what}.{ceiling}'
    if setting.state == 'blocked':
        return f'Faxbot holds faxes to {what} for your approval.{ceiling}'
    return f'{what[0].upper()}{what[1:]} follow Faxbot’s default again.{ceiling}'
