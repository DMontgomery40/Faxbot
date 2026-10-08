"""Local relay through a partner, and sending together across organizations.

An enrolled partner, often the same organization's office in another country,
can send a partner's faxes as local calls in its own country: Leeds HQ's fax
to an Australian number travels over the encrypted direct channel to the
Sydney office's Faxbot, which dials it locally. Several partners' faxes to the
same number can share one call through the relay, when everyone opted in.

Agreements (signed, both sides opt in)
--------------------------------------
- The relay grants: the countries (or rules regions) it relays to, a monthly
  page limit and a monthly spending limit, its hours, and whether the
  partner's faxes may share calls with other senders' faxes. It signs an
  offer (``relay_offer``) with a price statement for its own routes
  (``relay_price``, drawn from its rate cards through the shared predictor:
  ``price_body``). Granting creates the relay's "Relayed for {partner}"
  sender (``access/system_outbound.py``).
- The sender accepts (``relay_acceptance``), naming the reply number the
  relay prints on its faxes, whether its faxes may share calls, and, for
  marketing faxes, its business details.
- Either side withdraws at once (``relay_withdrawal``). Faxes the relay had
  already accepted still go and get their receipts; nothing is ever rerouted
  silently. A new fax after the withdrawal is refused before it is accepted.
- Off by default: nothing is relayed until both sides have signed, because
  the relay sees what each fax contains.

The relay route
---------------
``relay_candidates(destination, shape, now, engine=...)`` lists, for the route
planner, each active agreement that may carry a fax to ``destination`` now,
with its predicted cost: the relay's signed local price (``predict_from``
over the signed terms), plus nothing for the direct leg. A missing or expired
price statement is an unknown cost, never zero. ``RelayRoute``
(``relay_route.py``) sends the original document, sealed as the ``relay``
kind, and the relay answers with a signed receipt that it accepted the fax for
relaying; the sender's fax then waits for the relay's signed outcome.

Relayed sending on the relay
----------------------------
The relay accepts a document only within the agreement (partner, destination,
hours, page and spending limits, checked again under the acceptance lock), adds
the true sender's header line to every page (``relay_pages.py``), and queues it
as its own fax under the "Relayed for {partner}" sender, in one transaction
with the relay ledger row and the direct delivery record. Its outcome goes back
as a signed ``relay_outcome``: delivered (pages and seconds), failed before any
data, or uncertain. An uncertain outcome waits for a person on both sides and
is never sent again by another route.

Costs are kept on both sides (``relay_faxes``): the relay's actual charge (its
carrier's report, else its estimate, a shared call split by pages), and on the
sender the relayed cost the signed price statement gives, with what its own
route would have cost.
"""
from dataclasses import dataclass
from datetime import timedelta, timezone
import json
import logging
import re
from uuid import uuid4

import httpx

from ..config_runtime import run_lifecycle_step
from ..routing.database import utcnow
from .crypto import DirectProtocolError, check_signed, parse_timestamp, signed, timestamp, verify
from .relay_store import COUNTED, RelayConflict, RelayStore


KEY_PREFIX = 'relay:'
# The delivery-route ledger's grammar has no colon: the same route is recorded as ``relay.<partner>``.
LEDGER_PREFIX = 'relay.'
STATEMENTS = {'relay_offer': 'offer', 'relay_acceptance': 'acceptance', 'relay_withdrawal': 'withdrawal',
              'relay_price': 'price', 'relay_quote_request': 'quote_request', 'relay_outcome': 'outcome'}
FRESHNESS = timedelta(hours=24)
REQUEST_SKEW = timedelta(minutes=5)
PRICE_LIFETIME = timedelta(days=31)
PRICE_REFRESH = timedelta(days=7)
DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DAY_NAMES = {'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday', 'thu': 'Thursday', 'fri': 'Friday',
             'sat': 'Saturday', 'sun': 'Sunday'}
MAX_COUNTRIES = 30
MAX_PREFIXES = 100
DELIVERED, FAILED_BEFORE_DATA, UNCERTAIN = 'delivered', 'failed_before_data', 'uncertain'
OUTCOMES = (DELIVERED, FAILED_BEFORE_DATA, UNCERTAIN)
_COUNTRY = re.compile(r'[A-Z]{2}')
_PREFIX = re.compile(r'\+[1-9][0-9]{0,6}')
_NUMBER = re.compile(r'\+[1-9][0-9]{6,14}')
_ID = re.compile(r'[a-f0-9]{32}')
_CURRENCY = re.compile(r'[A-Z]{3}')

# One sentence each; the relay's organization is ``{relay}`` and the partner's ``{partner}``.
PRIVACY = ('{relay} will see what these faxes contain. If they are another organization, check that your '
           'agreement with them covers this; Faxbot does not make relaying exempt from any privacy rules.')
RELAY_PRIVACY = ("You will see what {partner}'s faxes contain. If they are another organization, check that your "
                 'agreement with them covers this; Faxbot does not make relaying exempt from any privacy rules.')
# Telnyx's terms (telnyx.com/terms-and-conditions-of-service, read 2026-10-07; the page shows no date): s. 11.19,
# "Customer may use the Services solely to originate voice traffic through Telnyx", bars handing off, relaying or
# transiting voice traffic; a relay receives the document over the internet and originates its own fax call, so it
# passes no call through. For another organization the relay is still responsible for its end users (s. 10.1:
# "shall cause its customers and end users to comply with the AUP") and for the content sent (s. 11.2).
# SignalWire's, Sinch's and Phaxio's terms were not checked.
OWN_CALL = ('{relay} receives each document over the internet and places its own fax call; no call is passed '
            'through.')
RESALE = ("Because {partner} is another organization, your carrier may count it as your customer: Telnyx's terms "
          'make you responsible for your end users and what they send. Faxbot has not checked the terms of '
          'SignalWire, Sinch or Phaxio.')
SHARED_CALL = ("When faxes from several partners share one call, each document's top line names its own sender, "
               "and the call shows {relay}'s own number.")


class RelayRefused(RuntimeError):
    """A relayed document the relay will not accept; ``reason`` is a stable code and the message one sentence."""

    def __init__(self, reason, message, status=409):
        super().__init__(message)
        self.reason, self.status = reason, status


# Terms ---------------------------------------------------------------------------------------------------------

def money_for(micros, currency, home=None):
    """An amount for a sentence. "$0.84" only for US dollars read on a US installation; every other amount names
    its currency ("0.84 USD", "80.00 AUD"), so a sender never mistakes a relay's currency for its own. Amounts
    are never converted."""
    from ..routing.costs import format_amount, money_text
    if currency == 'USD' and home != 'US':
        return f'{format_amount(micros)} USD'
    return money_text(micros, currency)


def money_list_for(amounts, home=None):
    """{currency: micros} as one phrase, each amount in its own currency."""
    return ' + '.join(money_for(micros, currency, home) for currency, micros in sorted(amounts.items()))


def _minute(text):
    match = re.fullmatch(r'([01]?[0-9]|2[0-3]):([0-5][0-9])', str(text or '').strip())
    return None if match is None else int(match.group(1)) * 60 + int(match.group(2))


def _region_definitions(engine):
    """The organization rules' regions (key -> {name, countries, prefixes}), or {}."""
    try:
        from ..rules.store import RuleStore
        active = RuleStore(engine).active('organization')
        document = json.loads(active['document']) if active else None
    except Exception:
        return {}
    regions = (document or {}).get('regions') if isinstance(document, dict) else None
    return regions if isinstance(regions, dict) else {}


def parse_terms(raw, *, zone_name, regions=None):
    """An administrator's grant as the terms the relay signs; RelayConflict (one sentence) when it is not usable.

    ``raw``: ``countries`` (ISO codes), ``regions`` (rules region keys), ``monthly_pages`` (or None),
    ``monthly_spend`` ({amount, currency} or None), ``hours`` ({days, from, until} or None),
    ``together`` (share calls with other senders' faxes) and ``same_organization``.
    """
    from ..routing.costs import InvalidRateCard, parse_amount
    if not isinstance(raw, dict):
        raise RelayConflict('Choose where this partner may send faxes through you.')
    countries = sorted({str(code).strip().upper() for code in raw.get('countries') or ()})
    if any(not _COUNTRY.fullmatch(code) for code in countries):
        raise RelayConflict('Name countries by their two-letter codes, such as AU.')
    named, prefixes = [], set()
    for key in raw.get('regions') or ():
        region = (regions or {}).get(key)
        if not isinstance(region, dict):
            raise RelayConflict(f'There is no region “{key}” in your rules.')
        inside = sorted({str(code).upper() for code in region.get('countries') or ()
                         if _COUNTRY.fullmatch(str(code).upper())})
        named.append({'key': str(key), 'name': str(region.get('name') or key)[:100], 'countries': inside})
        countries = sorted(set(countries) | set(inside))
        prefixes |= {str(prefix) for prefix in region.get('prefixes') or () if _PREFIX.fullmatch(str(prefix))}
    if not countries and not prefixes:
        raise RelayConflict('Choose at least one country this partner may send faxes to through you.')
    if len(countries) > MAX_COUNTRIES or len(prefixes) > MAX_PREFIXES:
        raise RelayConflict(f'A relay covers at most {MAX_COUNTRIES} countries.')
    pages = raw.get('monthly_pages')
    if pages is not None and (type(pages) is not int or not 1 <= pages <= 1_000_000):
        raise RelayConflict('A monthly page limit is a whole number from 1 to 1,000,000, or none.')
    spend = raw.get('monthly_spend')
    if spend is not None:
        if not isinstance(spend, dict) or not _CURRENCY.fullmatch(str(spend.get('currency') or '')):
            raise RelayConflict('A monthly spending limit is an amount and a three-letter currency, or none.')
        try:
            micros = parse_amount(str(spend.get('amount')), whole_digits=6)
        except InvalidRateCard:
            raise RelayConflict('A monthly spending limit is an amount such as 50.00, or none.') from None
        if micros <= 0:
            raise RelayConflict('A monthly spending limit is more than zero, or none.')
        spend = {'amount_micros': micros, 'currency': spend['currency']}
    hours = raw.get('hours')
    if hours is not None:
        if not isinstance(hours, dict):
            raise RelayConflict('Hours are days and a time from and until, or any time.')
        days = [day for day in DAYS if day in set(hours.get('days') or DAYS)]
        start, end = _minute(hours.get('from')), _minute(hours.get('until'))
        if not days or start is None or end is None or start == end:
            raise RelayConflict('Hours need at least one day and two different times, such as 08:00 and 18:00.')
        hours = {'days': days, 'start_minute': start, 'end_minute': end}
    return {'countries': countries, 'prefixes': sorted(prefixes), 'regions': named, 'monthly_pages': pages,
            'monthly_spend': spend, 'hours': hours, 'together': bool(raw.get('together', False)),
            'same_organization': bool(raw.get('same_organization', False)), 'time_zone': zone_name or ''}


def check_terms(terms):
    """Received terms, strictly shaped; None when they are not."""
    try:
        if not isinstance(terms, dict) or set(terms) != {'countries', 'prefixes', 'regions', 'monthly_pages',
                                                         'monthly_spend', 'hours', 'together',
                                                         'same_organization', 'time_zone'}:
            return None
        if (not isinstance(terms['countries'], list) or any(not _COUNTRY.fullmatch(str(c)) for c in terms['countries'])
                or not isinstance(terms['prefixes'], list)
                or any(not _PREFIX.fullmatch(str(p)) for p in terms['prefixes'])
                or len(terms['countries']) > MAX_COUNTRIES or len(terms['prefixes']) > MAX_PREFIXES
                or not (terms['countries'] or terms['prefixes'])):
            return None
        if not isinstance(terms['regions'], list) or any(
                not isinstance(r, dict) or set(r) != {'key', 'name', 'countries'} or not isinstance(r['name'], str)
                or not isinstance(r['countries'], list) for r in terms['regions']):
            return None
        pages = terms['monthly_pages']
        if pages is not None and (type(pages) is not int or pages < 1):
            return None
        spend = terms['monthly_spend']
        if spend is not None and (not isinstance(spend, dict) or set(spend) != {'amount_micros', 'currency'}
                                  or type(spend['amount_micros']) is not int or spend['amount_micros'] < 1
                                  or not _CURRENCY.fullmatch(str(spend['currency']))):
            return None
        hours = terms['hours']
        if hours is not None and (not isinstance(hours, dict) or set(hours) != {'days', 'start_minute', 'end_minute'}
                                  or not hours['days'] or any(day not in DAYS for day in hours['days'])
                                  or any(type(hours[k]) is not int or not 0 <= hours[k] < 1440
                                         for k in ('start_minute', 'end_minute'))):
            return None
        if type(terms['together']) is not bool or type(terms['same_organization']) is not bool \
                or not isinstance(terms['time_zone'], str) or len(terms['time_zone']) > 64:
            return None
        return terms
    except (TypeError, KeyError):
        return None


def country_of(number):
    import phonenumbers
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(number, None)) or None
    except phonenumbers.NumberParseException:
        return None


def covers(terms, destination):
    """Whether ``terms`` let the relay call ``destination`` (E.164)."""
    if not isinstance(destination, str) or not _NUMBER.fullmatch(destination):
        return False
    if country_of(destination) in set(terms.get('countries') or ()):
        return True
    return any(destination.startswith(prefix) for prefix in terms.get('prefixes') or ())


def _local(now, zone_name):
    from .. import people_time
    aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    return aware.astimezone(people_time.zone(zone_name))


def hours_open(terms, now):
    """Whether the relay's hours include ``now`` (naive UTC), on its own clock; a window may wrap past midnight."""
    hours = terms.get('hours')
    if not hours:
        return True
    local = _local(now, terms.get('time_zone'))
    minute = local.hour * 60 + local.minute
    start, end = hours['start_minute'], hours['end_minute']
    today = DAYS[local.weekday()]
    yesterday = DAYS[(local.weekday() - 1) % 7]
    if start < end:
        return today in hours['days'] and start <= minute < end
    return (today in hours['days'] and minute >= start) or (yesterday in hours['days'] and minute < end)


def month_start(now, zone_name):
    """The first moment of ``now``'s month on the relay's clock, as naive UTC."""
    local = _local(now, zone_name).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _clock_text(minute):
    hour, rest = divmod(minute, 60)
    return f'{hour % 12 or 12}:{rest:02d} {"AM" if hour < 12 else "PM"}'


# Countries whose names read with "the" in a sentence ("numbers in the UK").
_WITH_THE = {'GB': 'the UK', 'US': 'the US', 'NL': 'the Netherlands', 'PH': 'the Philippines',
             'AE': 'the United Arab Emirates', 'CZ': 'the Czech Republic', 'DO': 'the Dominican Republic',
             'BS': 'the Bahamas', 'GM': 'the Gambia'}


def country_name(code):
    """A country's name for a sentence: "Australia", "the UK"; the code itself when it has none."""
    import phonenumbers
    from phonenumbers import geocoder
    if code in _WITH_THE:
        return _WITH_THE[code]
    try:
        number = phonenumbers.example_number(code)
        return (geocoder.country_name_for_number(number, 'en') or code) if number is not None else code
    except Exception:
        return code


def places_text(terms):
    """Where a relay sends faxes, for a sentence: "numbers in Australia", "Australia and New Zealand"."""
    regions = terms.get('regions') or ()
    names = [region['name'] for region in regions]
    within = {code for region in regions for code in region.get('countries') or ()}
    names += [f'numbers in {country_name(code)}' for code in terms.get('countries') or ()
              if code not in within][:6]
    if not names:
        names = ['numbers starting ' + ', '.join(terms.get('prefixes') or ())]
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]


def limits_text(terms, home=None):
    """The agreement's limits as one clause: "up to 500 pages and 80.00 AUD a month, weekdays 8:00 AM to 6:00 PM"."""
    parts = []
    month = []
    if terms.get('monthly_pages'):
        month.append(f"{terms['monthly_pages']:,} pages")
    if terms.get('monthly_spend'):
        month.append(money_for(terms['monthly_spend']['amount_micros'], terms['monthly_spend']['currency'], home))
    if month:
        parts.append('up to ' + ' and '.join(month) + ' a month')
    hours = terms.get('hours')
    if hours:
        days = hours['days']
        when = ('weekdays' if days == list(DAYS[:5]) else 'every day' if days == list(DAYS)
                else ', '.join(DAY_NAMES[day] for day in days))
        parts.append(f"{when} {_clock_text(hours['start_minute'])} to {_clock_text(hours['end_minute'])} "
                     'their time')
    return ', '.join(parts) if parts else 'with no monthly limit, at any time'


# Price statements ------------------------------------------------------------------------------------------------

def _sample(country, kind):
    import phonenumbers
    from phonenumbers import PhoneNumberType
    try:
        number = phonenumbers.example_number_for_type(country, kind == 'toll_free' and PhoneNumberType.TOLL_FREE
                                                      or PhoneNumberType.FIXED_LINE)
    except Exception:
        number = None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164) if number is not None else None


def price_entry(country, facts):
    """One country's priced route, from the relay's ``RouteFacts``; terms None when the route publishes no price."""
    from dataclasses import asdict
    terms = facts.terms
    entry = {'country': country, 'kind': facts.destination.kind, 'label': facts.label[:100], 'terms': None,
             'link': {key: value for key, value in asdict(facts.link).items()}}
    if terms is not None:
        card = terms.card
        entry['terms'] = {
            'currency': card.currency, 'per_minute_micros': card.per_minute_micros,
            'per_page_micros': card.per_page_micros, 'per_call_micros': card.per_call_micros,
            'billing_increment_seconds': card.billing_increment_seconds, 'minimum_seconds': card.minimum_seconds,
            'monthly_fee_micros': card.monthly_fee_micros, 'destination_class': terms.destination_class,
            'page_time_seconds': terms.page_time_seconds, 'included_pages': terms.included_pages,
            'overage_page_micros': terms.overage_page_micros, 'max_pages_per_fax': terms.max_pages_per_fax}
    return entry


def price_body(engine, values, bound, countries, *, now=None):
    """The relay's price statement body for calls to ``countries`` over the route its planner would choose."""
    from ..routing.plan import RoutePlanner
    from ..routing.predict_facts import facts_for
    from ..routing.store import RouteStore
    now = now or utcnow()
    routes = RouteStore(engine)
    entries = []
    for country in countries:
        for kind in ('local', 'toll_free'):
            sample = _sample(country, kind)
            if sample is None:
                continue
            try:
                plan = RoutePlanner(routes).plan(to_number=sample, bound=bound, values=values, pages=1,
                                                 alternates=True)
                choice = next((choice for choice in plan.choices if choice.route.kind == 'provider'), None)
                if choice is None:
                    continue
                facts = facts_for(choice.route.key, sample, now=now, engine=engine, values=values)
            except Exception:
                logging.getLogger(__name__).warning('A relay price could not be worked out for one country.')
                continue
            entries.append(price_entry(country, facts))
    return {'priced_at': timestamp(now), 'valid_until': timestamp(now + PRICE_LIFETIME),
            'home': getattr(values, 'fax_default_country', 'US') or 'US', 'routes': entries}


def check_price(price):
    """A received price body, strictly shaped; None when it is not."""
    try:
        if not isinstance(price, dict) or set(price) != {'priced_at', 'valid_until', 'home', 'routes'}:
            return None
        parse_timestamp(price['priced_at'])
        parse_timestamp(price['valid_until'])
        if not _COUNTRY.fullmatch(str(price['home'])) or not isinstance(price['routes'], list) \
                or len(price['routes']) > 2 * MAX_COUNTRIES:
            return None
        for entry in price['routes']:
            if not isinstance(entry, dict) or set(entry) != {'country', 'kind', 'label', 'terms', 'link'}:
                return None
            if not _COUNTRY.fullmatch(str(entry['country'])) or not isinstance(entry['label'], str):
                return None
            if entry['terms'] is not None and not isinstance(entry['terms'], dict):
                return None
            if not isinstance(entry['link'], dict):
                return None
        return price
    except (DirectProtocolError, TypeError, KeyError):
        return None


def facts_from(price, destination, label):
    """The relay's ``RouteFacts`` for ``destination`` from a signed price body, or None when it has none."""
    from ..routing.costs import RateCard, RateTerms
    from ..routing.destinations import classify
    from ..routing.predict import Link, RouteFacts
    if check_price(price) is None:
        return None
    where = classify(destination, price['home'])
    country = country_of(destination)
    entry = next((item for item in price['routes'] if item['country'] == country and item['kind'] == where.kind),
                 None)
    if entry is None:
        return None
    try:
        link = Link(**{key: value for key, value in entry['link'].items() if key in Link.__dataclass_fields__})
    except (TypeError, ValueError):
        link = Link()
    terms = None
    if entry['terms'] is not None:
        given = entry['terms']
        try:
            card = RateCard(None, 'relay', 'outbound', label[:100] or 'Partner relay', given['currency'],
                            given['per_minute_micros'], given['per_page_micros'], given['per_call_micros'],
                            given['billing_increment_seconds'], given['minimum_seconds'], None,
                            parse_timestamp(price['priced_at']), given.get('monthly_fee_micros'))
            terms = RateTerms(card, given.get('destination_class') or where.kind, (), given.get('page_time_seconds'),
                              given.get('included_pages'), given.get('overage_page_micros'),
                              given.get('max_pages_per_fax'), False)
        except (Exception,):
            terms = None
    return RouteFacts('relay', label, where, terms, link=link, currency=(entry['terms'] or {}).get('currency', 'USD'),
                      missing=None if terms is not None else f'{label} gave no price for these numbers')


def predicted(price, destination, pages, label, *, now=None):
    """The shared predictor's ``Prediction`` for a fax over the relay's signed price, or None (unknown, never 0)."""
    from ..routing.predict import Shape, predict_from
    if price is None or type(pages) is not int or pages < 1:
        return None
    if parse_timestamp(price['valid_until']) < (now or utcnow()):
        return None
    facts = facts_from(price, destination, label)
    if facts is None or facts.terms is None:
        return None
    return predict_from(facts, Shape(pages, None, 'fine', 'normal'))


def own_prediction(engine, values, bound, destination, pages, *, now=None):
    """What this installation's own route would charge for the fax: (Prediction, route label) or (None, None)."""
    from ..routing.plan import RoutePlanner
    from ..routing.predict import Shape, predict_from
    from ..routing.predict_facts import facts_for
    from ..routing.store import RouteStore
    if type(pages) is not int or pages < 1:
        return None, None
    try:
        plan = RoutePlanner(RouteStore(engine)).plan(to_number=destination, bound=bound, values=values, pages=pages,
                                                     alternates=True)
        choice = next((choice for choice in plan.choices if choice.route.kind == 'provider'), None)
        if choice is None:
            return None, None
        facts = facts_for(choice.route.key, destination, now=now, engine=engine, values=values)
        return predict_from(facts, Shape(pages, None, 'fine', 'normal')), facts.label
    except Exception:
        return None, None


# Candidates for the route planner ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class RelayCandidate:
    """A relay the planner may choose for one fax: ``key`` is ``relay:<partner>``, as rules name it."""
    key: str
    peer_id: str
    agreement_id: str
    organization: str
    prediction: object        # routing.predict.Prediction, or None when the price is unknown
    sentence: str

    @property
    def cost(self):
        return None if self.prediction is None else self.prediction.cost

    @property
    def ledger_key(self):
        return LEDGER_PREFIX + self.peer_id


def ledger_key(key):
    """The delivery-route ledger's name for a relay route key (``relay:<id>`` -> ``relay.<id>``)."""
    return LEDGER_PREFIX + key[len(KEY_PREFIX):] if isinstance(key, str) and key.startswith(KEY_PREFIX) else key


def relay_key(peer_id):
    return KEY_PREFIX + peer_id


def _active_for(store, destination, now):
    """(agreement, terms, price) for each active sender agreement whose verified partner covers ``destination``."""
    found = []
    for row in store.agreements_for(role='sender', states=('active',)):
        if row['peer_state'] != 'verified' or (row['peer_expires_at'] is not None and row['peer_expires_at'] <= now):
            continue
        terms = check_terms(json.loads(row['terms']))
        if terms is None or not covers(terms, destination) or not hours_open(terms, now):
            continue
        price = _price_of(store, row)
        found.append((row, terms, price))
    return found


def _price_of(store, row):
    statement = store.statement(row['price_statement_id']) if row.get('price_statement_id') else None
    if statement is None:
        return None
    try:
        body = json.loads(statement['statement'])
    except ValueError:
        return None
    return check_price(body.get('price')) if isinstance(body, dict) else None


def _within_limits(store, row, terms, pages, cost, now):
    """Whether the sender's own count of faxes relayed this month leaves room for one more of ``pages``."""
    used_pages, used_money = store.usage(row['id'], month_start(now, terms.get('time_zone')))
    if terms.get('monthly_pages') and used_pages + (pages or 0) > terms['monthly_pages']:
        return False
    spend = terms.get('monthly_spend')
    if spend:
        if cost is None or cost.currency != spend['currency']:
            return False
        if used_money.get(spend['currency'], 0) + cost.micros > spend['amount_micros']:
            return False
    return True


def relay_candidates(destination, shape, now=None, *, engine, job_id=None, never=(), home=None):
    """The relays the route planner may use for a fax to ``destination`` now, cheapest first.

    For WP-C's planner (``routing/plan.py``): each ``RelayCandidate`` is an
    active agreement whose verified partner relays to ``destination`` during
    its hours and whose monthly limits leave room, with the relay's signed
    price for ``shape`` (unknown when the statement is missing or expired, and
    then it sorts after every known cost). A fax that is itself relayed for a
    partner (``job_id``) is never relayed again, and ``never`` (a rule's
    "never relay" or ``relay:<partner>`` keys) removes candidates. ``home`` is the installation's country, for
    how amounts read (``money_for``).
    """
    now = now or utcnow()
    if 'relay' in never:
        return []
    store = RelayStore(engine)
    if job_id is not None and store.fax_for_job(job_id, role='relay') is not None:
        return []
    pages = getattr(shape, 'pages', None)
    result = []
    for row, terms, price in _active_for(store, destination, now):
        key = relay_key(row['peer_id'])
        if key in never:
            continue
        prediction = predicted(price, destination, pages, row['organization'], now=now)
        cost = prediction.cost if prediction is not None else None
        if not _within_limits(store, row, terms, pages, cost, now):
            continue
        if cost is not None:
            sentence = (f"Through {row['organization']} as a local call there, about "
                        f'{money_for(cost.micros, cost.currency, home)}.')
        else:
            sentence = f"Through {row['organization']} as a local call there; its price is not known."
        result.append(RelayCandidate(key, row['peer_id'], row['id'], row['organization'], prediction, sentence))
    return sorted(result, key=rank)


def rank(candidate):
    """The planner's order among relays and other routes: known costs cheapest first, unknown costs last."""
    cost = getattr(candidate, 'cost', None)
    return (cost is None, cost.currency if cost is not None else '', cost.micros if cost is not None else 0,
            getattr(candidate, 'organization', ''))


# The service -----------------------------------------------------------------------------------------------------

def _statement_body(envelope):
    try:
        return json.loads(envelope['statement'])
    except (ValueError, TypeError, KeyError):
        return None


class RelayService:
    """Agreements, the partner protocol, relayed sending and outcomes, over one installation's direct service."""

    def __init__(self, direct, *, access=None, delivery=None):
        """``access()`` returns the access runtime (None until it is ready); ``delivery()`` the outbound store."""
        self.direct = direct
        self.store = RelayStore(direct.store.engine)
        self.access = access or (lambda: None)
        self.delivery = delivery or (lambda: None)

    @property
    def engine(self):
        return self.direct.store.engine

    def _values(self):
        return self.direct.values()

    def _organization(self):
        return (self._values().direct_organization or '').strip() or 'This installation'

    def _sign(self, identity, peer, kind, **fields):
        return signed(identity, {'type': kind, 'recipient': peer['signing_key'], 'said_at': timestamp(), **fields})

    def _peer(self, peer_id):
        peer = self.direct.store.get_peer(peer_id)
        if peer is None or peer['state'] == 'revoked':
            raise RelayConflict('This partner is not enrolled.')
        return peer

    def _configuration(self):
        access = self.access()
        if access is None:
            raise RelayConflict('Faxbot is still starting; try again in a moment.')
        return access.outbound.configuration, access

    def _bound(self):
        configuration, _ = self._configuration()
        revision = configuration.read().active
        profile_id = revision.profile_id('outbound')
        if profile_id is None:
            return revision, None
        return revision, configuration.read_profile(profile_id).configuration.provider_id

    # Relay side: granting -------------------------------------------------------------------------------------
    def grant(self, peer_id, raw, *, actor, actor_name=None, now=None):
        """Offer to relay for a partner: the signed offer with a price statement, kept until the partner has it."""
        from ..access.system_outbound import SystemSenderError, create_sender
        now = now or utcnow()
        values = self._values()
        if not values.direct_delivery_enabled:
            raise RelayConflict('Turn on direct delivery before relaying for partners.')
        peer = self._peer(peer_id)
        if peer['state'] != 'verified':
            raise RelayConflict(f"Verify {peer['organization']}'s number before relaying for them.")
        if any(row['state'] in ('offered', 'accepting', 'active') for row in self.store.agreements_for(
                peer_id=peer_id, role='relay')):
            raise RelayConflict(f"You already relay for {peer['organization']}; withdraw that first to change it.")
        terms = parse_terms(raw, zone_name=getattr(values, 'time_zone', '') or '',
                            regions=_region_definitions(self.engine))
        identity = self.direct.identity(create=True)
        revision, bound = self._bound()
        if bound is None:
            raise RelayConflict('Outbound fax delivery is turned off, so you cannot relay for partners.')
        price = price_body(self.engine, revision.values, bound, terms['countries'], now=now)
        access = self.access()
        if access is None:
            raise RelayConflict('Faxbot is still starting; try again in a moment.')
        try:
            principal = create_sender(access, actor, f"Relayed for {peer['organization']}"[:200], now=now)
        except SystemSenderError as error:
            raise RelayConflict(str(error)) from None
        agreement_id = uuid4().hex
        offer = self._sign(identity, peer, 'relay_offer', agreement=agreement_id, terms=terms, price=price)
        return self.store.create(agreement_id=agreement_id, peer_id=peer_id, role='relay', state='offered',
                                 terms=terms, statement=offer, direction='sent', principal_id=principal,
                                 send_together=terms['together'], actor_name=actor_name, now=now)

    def refresh_price(self, agreement_id, *, now=None):
        """Sign a new price statement for an agreement this installation relays for; the partner is told."""
        now = now or utcnow()
        row = self.store.agreement(agreement_id)
        if row is None or row['role'] != 'relay' or row['state'] not in ('offered', 'active'):
            raise RelayConflict('This relay agreement is not active.')
        peer = self._peer(row['peer_id'])
        terms = json.loads(row['terms'])
        revision, bound = self._bound()
        if bound is None:
            raise RelayConflict('Outbound fax delivery is turned off, so there is no price to give.')
        price = price_body(self.engine, revision.values, bound, terms['countries'], now=now)
        statement = self._sign(self.direct.identity(create=True), peer, 'relay_price', agreement=agreement_id,
                               price=price)
        return self.store.change(agreement_id, statement=statement, kind='price', direction='sent', now=now)

    # Sender side: accepting -----------------------------------------------------------------------------------
    def accept(self, agreement_id, *, reply_number=None, together=False, same_organization=False, marketing=None,
               actor_name=None, now=None):
        """Accept a partner's offer; it is active once the partner records our signed acceptance."""
        from ..routing.numbers import InvalidNumber, normalize_number
        now = now or utcnow()
        row = self.store.agreement(agreement_id)
        if row is None or row['role'] != 'sender' or row['state'] not in ('offered', 'accepting'):
            raise RelayConflict('There is no open offer to accept.')
        peer = self._peer(row['peer_id'])
        values = self._values()
        if reply_number:
            try:
                reply_number = normalize_number(reply_number, country=values.fax_default_country)
            except InvalidNumber:
                raise RelayConflict('Enter the reply number with its country code, such as +442071234567.') from None
        else:
            reply_number = self.default_reply_number()
        if reply_number is None:
            raise RelayConflict('Set the number replies should reach under Numbers, Sender identity, or enter one '
                                'here.')
        marketing = clean_marketing(marketing)
        offer = self.store.latest(peer_id=peer['id'], kind='offer', direction='received', agreement_id=agreement_id)
        if offer is None:
            raise RelayConflict('There is no open offer to accept.')
        statement = self._sign(self.direct.identity(create=True), peer, 'relay_acceptance', agreement=agreement_id,
                               offer=offer['digest'], reply_number=reply_number, together=bool(together),
                               same_organization=bool(same_organization), marketing=marketing)
        terms = json.loads(row['terms'])
        return self.store.change(agreement_id, expected_state=('offered', 'accepting'), statement=statement,
                                 kind='acceptance', direction='sent', state='accepting', reply_number=reply_number,
                                 send_together=int(bool(together) and terms.get('together', False)),
                                 marketing=json.dumps(marketing, sort_keys=True) if marketing else None,
                                 actor_name=(actor_name or '')[:200] or None, now=now)

    def default_reply_number(self):
        """The number this installation prints on its faxes (its reply number), else its direct-delivery number."""
        from ..routing.numbers import InvalidNumber, normalize_number
        values = self._values()
        for text in (getattr(values, 'fax_reply_number', ''), getattr(values, 'direct_fax_number', '')):
            if not text:
                continue
            try:
                return normalize_number(text, country=values.fax_default_country)
            except InvalidNumber:
                continue
        return None

    # Either side: withdrawing ---------------------------------------------------------------------------------
    def withdraw(self, agreement_id, *, by=None, actor_name=None, now=None):
        """End an agreement at once on this side and tell the partner, signed; faxes already accepted still go."""
        now = now or utcnow()
        row = self.store.agreement(agreement_id)
        if row is None or row['state'] == 'withdrawn':
            raise RelayConflict('This relay agreement has already ended.')
        peer = self.direct.store.get_peer(row['peer_id'])
        statement = None
        if peer is not None and peer['state'] != 'revoked':
            statement = self._sign(self.direct.identity(create=True), peer, 'relay_withdrawal',
                                   agreement=agreement_id)
        changed = self.store.change(agreement_id, expected_state=('offered', 'accepting', 'active'),
                                    statement=statement, kind='withdrawal', direction='sent', state='withdrawn',
                                    withdrawn_by='us', actor_name=(actor_name or '')[:200] or row['actor_name'],
                                    now=now, **({} if statement else {'pending_statement_id': None}))
        if changed is None:
            raise RelayConflict('This relay agreement has already ended.')
        self._retire(changed, by=by)
        return changed

    def _retire(self, row, *, by=None):
        if row['role'] != 'relay' or not row['principal_id']:
            return
        access = self.access()
        if access is None:
            return
        from ..access.system_outbound import retire
        try:
            retire(access.store, row['principal_id'], by=by)
        except Exception:
            logging.getLogger(__name__).warning('The sender for relayed faxes could not be turned off yet.')

    # Telling the partner ----------------------------------------------------------------------------------------
    async def tell(self, row):
        """Send the agreement's latest signed statement to the partner: ``told``, ``refused`` or ``unreachable``."""
        return (await self.tell_answer(row))[0]

    async def tell_answer(self, row):
        """``tell``, with the partner's own sentence when it said no (None otherwise)."""
        statement_id = row['pending_statement_id']
        if not statement_id:
            return 'told', None
        peer = await run_lifecycle_step(lambda: self.direct.store.get_peer(row['peer_id']))
        kept = await run_lifecycle_step(lambda: self.store.statement(statement_id))
        if peer is None or kept is None:
            return 'unreachable', None
        from .service import PartnerUnreachable
        try:
            status, body = await self.direct.http.request('POST', peer['endpoint_url'] + '/direct/relay/statements',
                                                          json=RelayStore.envelope(kept))
        except (PartnerUnreachable, httpx.HTTPError, OSError):
            return 'unreachable', None
        if status == 200 and isinstance(body, dict) and body.get('recorded') is True:
            await run_lifecycle_step(lambda: self.store.told(row['id'], statement_id))
            if kept['kind'] == 'acceptance':
                await run_lifecycle_step(lambda: self.store.change(row['id'], expected_state=('accepting',),
                                                                   state='active'))
            return 'told', None
        if 400 <= status < 500 and status not in (404, 405, 429):
            # The partner read it and said no; asking again would not change that.
            await run_lifecycle_step(lambda: self.store.told(row['id'], statement_id))
            if kept['kind'] == 'acceptance':
                await run_lifecycle_step(lambda: self.store.change(row['id'], expected_state=('accepting',),
                                                                   state='offered'))
            detail = body.get('detail') if isinstance(body, dict) and isinstance(body.get('detail'), str) else None
            return 'refused', (detail[:300] if detail else None)
        return 'unreachable', None

    async def tell_all(self):
        values = await run_lifecycle_step(self._values)
        if not getattr(values, 'direct_delivery_enabled', False) or not self.direct.ready():
            return False
        for row in await run_lifecycle_step(self.store.untold):
            await self.tell(row)
        return False

    # The partner protocol -------------------------------------------------------------------------------------
    def hear(self, statement, signature, *, now=None):
        """A partner's signed relay statement; returns (status, body)."""
        now = now or utcnow()
        _, identity = self.direct._enabled_identity()
        refused = (400, {'recorded': False, 'detail': 'This statement could not be checked.'})
        try:
            encoded = statement.encode('ascii')
            body = json.loads(encoded)
            if (not isinstance(body, dict) or body.get('type') not in STATEMENTS
                    or body.get('recipient') != identity.signing_key or not isinstance(body.get('signer'), str)):
                return refused
            peer = self.direct.store.peer_by_key(body['signer'])
            if peer is None or peer['state'] == 'revoked':
                return 403, {'recorded': False, 'detail': 'This installation does not accept statements from you.'}
            verify(peer['signing_key'], encoded, signature)
            if abs(parse_timestamp(body.get('said_at')) - now) > FRESHNESS:
                return refused
        except (ValueError, TypeError, UnicodeEncodeError, DirectProtocolError):
            return refused
        envelope = {'statement': statement, 'signature': signature}
        kind = STATEMENTS[body['type']]
        handler = getattr(self, '_hear_' + kind)
        try:
            return handler(peer, body, envelope, identity, now)
        except RelayConflict as error:
            return 409, {'recorded': False, 'detail': str(error)}

    def _agreement_from(self, peer, body, role):
        agreement_id = body.get('agreement')
        if not isinstance(agreement_id, str) or not _ID.fullmatch(agreement_id):
            raise RelayConflict('This statement names no relay agreement.')
        row = self.store.agreement(agreement_id)
        if row is not None and (row['peer_id'] != peer['id'] or row['role'] != role):
            raise RelayConflict('This statement names another relay agreement.')
        return agreement_id, row

    def _hear_offer(self, peer, body, envelope, identity, now):
        agreement_id, row = self._agreement_from(peer, body, 'sender')
        terms = check_terms(body.get('terms'))
        price = check_price(body.get('price'))
        if terms is None or price is None or set(body) != {'type', 'recipient', 'said_at', 'signer', 'agreement',
                                                           'terms', 'price'}:
            return 400, {'recorded': False, 'detail': 'This offer could not be read.'}
        if row is None:
            self.store.create(agreement_id=agreement_id, peer_id=peer['id'], role='sender', state='offered',
                              terms=terms, statement=envelope, direction='received',
                              send_together=False, now=now)
        elif row['state'] == 'withdrawn':
            raise RelayConflict('This relay agreement has ended.')
        else:
            self.store.change(agreement_id, statement=envelope, kind='offer', direction='received',
                              terms=json.dumps(terms, sort_keys=True, separators=(',', ':')),
                              price_statement_id=None, now=now)
            kept = self.store.latest(peer_id=peer['id'], kind='offer', direction='received',
                                     agreement_id=agreement_id)
            self.store.change(agreement_id, price_statement_id=kept['id'], now=now)
        return 200, {'recorded': True}

    def _hear_acceptance(self, peer, body, envelope, identity, now):
        agreement_id, row = self._agreement_from(peer, body, 'relay')
        if row is None or row['state'] not in ('offered', 'active'):
            raise RelayConflict('There is no open offer from this installation to accept.')
        offer = self.store.latest(peer_id=peer['id'], kind='offer', direction='sent', agreement_id=agreement_id)
        if offer is None or body.get('offer') != offer['digest']:
            raise RelayConflict('The offer changed; accept the new one.')
        reply = body.get('reply_number')
        if not isinstance(reply, str) or not _NUMBER.fullmatch(reply):
            return 400, {'recorded': False, 'detail': 'The acceptance names no reply number.'}
        marketing = clean_marketing(body.get('marketing'))
        if type(body.get('together')) is not bool or type(body.get('same_organization')) is not bool:
            return 400, {'recorded': False, 'detail': 'This acceptance could not be read.'}
        terms = json.loads(row['terms'])
        self.store.change(agreement_id, statement=envelope, kind='acceptance', direction='received', state='active',
                          reply_number=reply, send_together=int(body['together'] and terms.get('together', False)),
                          marketing=json.dumps(marketing, sort_keys=True) if marketing else None, now=now)
        return 200, {'recorded': True}

    def _hear_withdrawal(self, peer, body, envelope, identity, now):
        # Either side may end it: the relay's offer, or the sender's acceptance.
        agreement_id = body.get('agreement')
        row = self.store.agreement(agreement_id) if isinstance(agreement_id, str) and _ID.fullmatch(agreement_id) \
            else None
        if row is None or row['peer_id'] != peer['id']:
            raise RelayConflict('There is no such relay agreement.')
        changed = self.store.change(agreement_id, expected_state=('offered', 'accepting', 'active'),
                                    statement=envelope, kind='withdrawal', direction='received', state='withdrawn',
                                    withdrawn_by='partner', pending_statement_id=None, now=now)
        if changed is not None:
            self._retire(changed)
        return 200, {'recorded': True}

    def _hear_price(self, peer, body, envelope, identity, now):
        price = check_price(body.get('price'))
        if price is None:
            return 400, {'recorded': False, 'detail': 'This price statement could not be read.'}
        agreement_id = body.get('agreement')
        if agreement_id is None:
            self.store.keep(peer_id=peer['id'], direction='received', kind='price', envelope=envelope, now=now)
            return 200, {'recorded': True}
        agreement_id, row = self._agreement_from(peer, body, 'sender')
        if row is None or row['state'] == 'withdrawn':
            raise RelayConflict('There is no active relay agreement for this price.')
        self.store.change(agreement_id, statement=envelope, kind='price', direction='received', now=now)
        return 200, {'recorded': True}

    def _hear_quote_request(self, peer, body, envelope, identity, now):
        """A verified partner asks what this installation's local calls would cost; the answer is signed."""
        countries = body.get('countries')
        if (not isinstance(countries, list) or not 0 < len(countries) <= 10
                or any(not isinstance(code, str) or not _COUNTRY.fullmatch(code) for code in countries)):
            return 400, {'recorded': False, 'detail': 'Name up to ten countries by their two-letter codes.'}
        if peer['state'] != 'verified':
            raise RelayConflict('This installation gives prices only to verified partners.')
        revision, bound = self._bound()
        if bound is None:
            raise RelayConflict('This installation does not send faxes, so it has no price to give.')
        self.store.keep(peer_id=peer['id'], direction='received', kind='quote_request', envelope=envelope, now=now)
        answer = self._sign(identity, peer, 'relay_price', agreement=None,
                            price=price_body(self.engine, revision.values, bound, sorted(set(countries)), now=now))
        self.store.keep(peer_id=peer['id'], direction='sent', kind='price', envelope=answer, now=now)
        return 200, answer

    def _hear_outcome(self, peer, body, envelope, identity, now):
        message_id = body.get('message_id')
        if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
            return 400, {'recorded': False, 'detail': 'This receipt names no fax.'}
        row = self.store.fax(role='sender', message_id=message_id)
        if row is None or row['peer_id'] != peer['id']:
            raise RelayConflict('This installation sent no such fax through you.')
        if parse_outcome(body) is None:
            return 400, {'recorded': False, 'detail': 'This receipt could not be read.'}
        if self.apply_outcome(row, body, envelope) is None:
            return 503, {'recorded': False, 'detail': 'This installation is not ready for the receipt yet.'}
        return 200, {'recorded': True}

    async def ask_quote(self, peer_id, countries):
        """Ask a verified partner, signed, what relaying to ``countries`` would cost there; True when it answered."""
        peer = await run_lifecycle_step(lambda: self._peer(peer_id))
        identity = await run_lifecycle_step(lambda: self.direct.identity(create=True))
        request = self._sign(identity, peer, 'relay_quote_request', countries=sorted(set(countries))[:10])
        from .service import PartnerUnreachable
        try:
            status, body = await self.direct.http.request('POST', peer['endpoint_url'] + '/direct/relay/statements',
                                                          json=request)
            answer = check_signed(body, peer['signing_key'])
        except (PartnerUnreachable, httpx.HTTPError, OSError, DirectProtocolError):
            return False
        if status != 200 or answer.get('type') != 'relay_price' or answer.get('recipient') != identity.signing_key \
                or check_price(answer.get('price')) is None:
            return False
        await run_lifecycle_step(lambda: self.store.keep(peer_id=peer['id'], direction='sent', kind='quote_request',
                                                         envelope=request))
        await run_lifecycle_step(lambda: self.store.keep(peer_id=peer['id'], direction='received', kind='price',
                                                         envelope=body))
        return True

    # Relay side: receiving a document -------------------------------------------------------------------------
    def receive(self, identity, peer, manifest, document, *, now=None):
        """Accept a partner's document for relaying within its agreement; returns (status, signed body)."""
        now = now or utcnow()
        message_id = manifest['message_id']
        try:
            return 200, self._accept(identity, peer, manifest, document, now)
        except RelayRefused as refusal:
            return refusal.status, self.direct._refusal(identity, message_id, refusal.reason, str(refusal), peer)

    def _relay_terms(self, peer, facts, now):
        relay = self._organization()
        row = self.store.agreement(facts['agreement'])
        if row is None or row['role'] != 'relay' or row['peer_id'] != peer['id'] or row['state'] != 'active':
            raise RelayRefused('no_agreement', f'{relay} has no active relay agreement with you, so nothing was '
                                               'accepted.')
        terms = json.loads(row['terms'])
        if not covers(terms, facts['destination']):
            raise RelayRefused('destination', f'{relay} did not agree to relay faxes to this number, so nothing was '
                                              'accepted.')
        from ..routing.destinations import PREMIUM, classify
        if classify(facts['destination'], country_of(facts['destination']) or 'US').kind == PREMIUM:
            # A partner's fax never makes the relay dial a premium-rate number, whatever the agreement covers.
            raise RelayRefused('destination', f'{relay} does not relay faxes to premium-rate numbers, so nothing was '
                                              'accepted.')
        if not hours_open(terms, now):
            raise RelayRefused('outside_hours', f'It is outside the hours when {relay} relays faxes, so nothing was '
                                                'accepted.')
        return row, terms

    def _check_limits(self, row, terms, pages, cost, now, connection=None):
        relay = self._organization()
        since = month_start(now, terms.get('time_zone'))
        used_pages, used_money = (self.store.usage_on(connection, row['id'], since) if connection is not None
                                  else self.store.usage(row['id'], since))
        if terms.get('monthly_pages') and used_pages + pages > terms['monthly_pages']:
            raise RelayRefused('over_limit', f"This fax would go over the {terms['monthly_pages']:,} pages a month "
                                             f'{relay} agreed to relay for you, so nothing was accepted.')
        spend = terms.get('monthly_spend')
        if spend:
            if cost is None or cost[1] != spend['currency']:
                raise RelayRefused('cost_unknown', f'{relay} cannot tell what this fax would cost, and the agreement '
                                                   'has a spending limit, so nothing was accepted.')
            if used_money.get(spend['currency'], 0) + cost[0] > spend['amount_micros']:
                # The partner reads this: its currency is always named.
                limit = money_for(spend['amount_micros'], spend['currency'])
                raise RelayRefused('over_limit', f'This fax would go over the {limit} a month {relay} agreed to spend '
                                                 'relaying for you, so nothing was accepted.')

    def _accept(self, identity, peer, manifest, document, now):
        import hashlib
        import os
        from pathlib import Path
        from . import relay_pages
        facts = manifest['relay']
        row, terms = self._relay_terms(peer, facts, now)
        if not document.startswith(b'%PDF'):
            raise RelayRefused('not_pdf', 'Only PDF documents can be relayed.', 400)
        configuration, access = self._configuration()
        revision = configuration.read().active
        profile_id = revision.profile_id('outbound')
        if profile_id is None:
            raise RelayRefused('not_sending', f'{self._organization()} is not sending faxes now, so nothing was '
                                              'accepted.')
        profile = configuration.read_profile(profile_id)
        values = revision.values
        destination = facts['destination']
        marketing = json.loads(row['marketing']) if row.get('marketing') else None
        folder = Path(values.fax_data_dir)
        try:
            pdf, tiff, pages, _ = relay_pages.stamp(
                document, header=peer['organization'], station=row['reply_number'] or peer['phone_number'],
                moment=now, zone_name=getattr(values, 'time_zone', '') or '',
                first_page=relay_pages.marketing_lines(marketing, destination), folder=folder)
        except relay_pages.RelayPagesError as error:
            raise RelayRefused('not_readable', str(error), 400) from None
        prediction, _ = own_prediction(self.engine, values, profile.configuration.provider_id, destination, pages,
                                       now=now)
        cost = ((prediction.cost.micros, prediction.cost.currency)
                if prediction is not None and prediction.cost is not None else None)
        self._check_limits(row, terms, pages, cost, now)
        # One fax per partner document: a retried upload names the same fax, which acceptance refuses twice.
        job_id = hashlib.sha256(f"faxbot-relay|{peer['id']}|{manifest['message_id']}".encode('ascii')).hexdigest()[:32]
        needs_tiff = ((profile.configuration.manifest is None and profile.configuration.provider_id in {'sip',
                                                                                                         'freeswitch'})
                      or profile.configuration.traits.get('requires_tiff') is True)
        paths = [folder / (job_id + '.pdf')] + ([folder / (job_id + '.tiff')] if needs_tiff else [])
        for path, data in zip(paths, (pdf, tiff)):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
        receipt = signed(identity, {'type': 'receipt', 'message_id': manifest['message_id'], 'status': 'accepted',
                                    'kind': 'relay', 'document_sha256': manifest['document']['sha256'],
                                    'recipient': manifest['recipient'], 'accepted_at': timestamp(now),
                                    'capabilities': self.direct.offered(peer)})
        hold = self._hold(row, facts, revision, profile, destination, pages)
        job = {'id': job_id, 'to_number': destination, 'file_name': 'relayed.pdf',
               'tiff_path': str(paths[1]) if needs_tiff else '', 'status': 'queued', 'pages': pages,
               'created_at': now, 'updated_at': now}

        def record(connection, moment):
            existing = self.direct.store.find('inbound', manifest['message_id'], connection)
            if existing is not None:
                raise _AlreadyAccepted()
            self._check_limits(row, terms, pages, cost, now, connection)
            self.store.add_fax_on(connection, role='relay', message_id=manifest['message_id'],
                                  agreement_id=row['id'], peer_id=peer['id'], job_id=job_id, destination=destination,
                                  pages=pages, state='accepted', shared=hold is not None, cost=cost, now=moment)
            deliveries = self.direct.store.deliveries
            connection.execute(deliveries.insert().values(
                id=uuid4().hex, direction='inbound', message_id=manifest['message_id'], peer_id=peer['id'],
                recipient_number=manifest['recipient']['fax_number'], digest=manifest['document']['sha256'],
                size_bytes=manifest['document']['size'], manifest=json.dumps(manifest, sort_keys=True,
                                                                             separators=(',', ':')),
                state='accepted', receipt=json.dumps(receipt, sort_keys=True), document_path=None,
                accepted_at=moment, kind='relay', created_at=moment, updated_at=moment))
            if hold is not None:
                from ..batching.acceptance import recorder
                recorder(self.engine, job_id, hold, _RelaySender(row))(connection, moment)
        from ..access.system_outbound import SystemSenderError, accept
        try:
            accept(configuration, access.store, row['principal_id'], revision, job, also=record)
        except _AlreadyAccepted:
            existing = self.direct.store.find('inbound', manifest['message_id'])
            return json.loads(existing['receipt'])
        except SystemSenderError:
            self._discard(paths, job_id)
            raise RelayRefused('no_agreement', f'{self._organization()} has no active relay agreement with you, '
                                               'so nothing was accepted.') from None
        except RelayRefused:
            self._discard(paths, job_id)
            raise
        except Exception:
            existing = self.direct.store.find('inbound', manifest['message_id'])
            if existing is not None and existing['state'] == 'accepted' and existing.get('kind') == 'relay':
                return json.loads(existing['receipt'])
            self._discard(paths, job_id)
            raise
        return receipt

    def _discard(self, paths, job_id):
        """Remove the files of a fax that was not accepted, unless an earlier acceptance of it owns them."""
        if self.store.fax_for_job(job_id, role='relay') is not None:
            return
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass

    def _hold(self, row, facts, revision, profile, destination, pages):
        """A sending-together hold for a relayed fax, only when the relay and the sender both opted in."""
        if not (row['send_together'] and facts['together']):
            return None
        try:
            from ..batching.acceptance import hold_plan
            return hold_plan(self.engine, revision, profile, destination=destination, pages=pages,
                             actor=_RelaySender(row))
        except Exception:
            logging.getLogger(__name__).warning('A relayed fax goes on its own: sending together is unavailable.')
            return None

    # Relay side: outcomes ---------------------------------------------------------------------------------------
    def settle(self, *, now=None):
        """Turn each relayed fax's own result into a signed outcome for its sender; returns those that moved."""
        now = now or utcnow()
        moved = []
        identity = None
        for fax in self.store.faxes_in(role='relay', states=('accepted', UNCERTAIN)):
            found = self._result(fax)
            if found is None or (found['status'] == fax['state']):
                continue
            peer = self.direct.store.get_peer(fax['peer_id'])
            if peer is None:
                continue
            identity = identity or self.direct.identity()
            statement = self._sign(identity, peer, 'relay_outcome', agreement=fax['agreement_id'],
                                   message_id=fax['message_id'], **found)
            charge = found['charge'] or {}
            changed = self.store.move_fax(
                fax['id'], found['status'], expected=('accepted', UNCERTAIN), statement=statement, direction='sent',
                now=now, delivered_pages=found['pages'], seconds=found['seconds'], shared=int(found['shared']),
                charge_micros=charge.get('amount_micros'), charge_currency=charge.get('currency'),
                charge_basis=charge.get('basis'), told_at=None, detail=found['detail'][:300])
            if changed is not None:
                moved.append(changed)
        return moved

    def _result(self, fax):
        """What happened to the relay's own fax: an outcome's fields, or None while it is still going."""
        import sqlalchemy as sa
        from ..routing.database import read_connection, reflect
        tables = reflect(self.engine, ('outbound_deliveries', 'outbound_attempts', 'delivery_attempt_costs',
                                       'fax_jobs'))
        deliveries, attempts = tables['outbound_deliveries'], tables['outbound_attempts']
        costs, jobs = tables['delivery_attempt_costs'], tables['fax_jobs']
        with read_connection(self.engine) as connection:
            delivery = connection.execute(sa.select(deliveries).where(
                deliveries.c.id == fax['job_id'])).mappings().one_or_none()
            if delivery is None:
                return None
            tried = connection.execute(sa.select(attempts).where(attempts.c.job_id == fax['job_id']).order_by(
                attempts.c.created_at)).mappings().all()
            job = connection.execute(sa.select(jobs.c.pages, jobs.c.error).where(
                jobs.c.id == fax['job_id'])).first()
            call = None
            if delivery['attempt_id']:
                call = connection.execute(sa.select(costs).where(
                    costs.c.id == delivery['attempt_id'])).mappings().one_or_none()
        relay = self._organization()
        state = delivery['state']
        shared = self._shared(fax['job_id'])
        if state == 'success':
            seconds = None
            if call is not None and call['connected_at'] is not None and call['ended_at'] is not None:
                seconds = max(0, int((call['ended_at'] - call['connected_at']).total_seconds()))
            elif call is not None and call['billed_seconds'] is not None:
                seconds = int(call['billed_seconds'])
            pages = fax['pages'] if fax['pages'] else (job.pages if job is not None else None)
            return {'status': DELIVERED, 'pages': pages, 'seconds': seconds, 'charge': self._charge(fax['job_id']),
                    'shared': shared, 'detail': f'{relay} delivered it as a local call.'}
        if state in ('failed', 'cancelled'):
            partial = any(attempt['error_category'] in ('partly_sent', 'pages_unconfirmed') for attempt in tried)
            if partial:
                return {'status': UNCERTAIN, 'pages': None, 'seconds': None, 'charge': self._charge(fax['job_id']),
                        'shared': shared, 'detail': f"{relay}'s call ended partway, and part of the fax may have "
                                                    'arrived. Check with the recipient before sending it again.'}
            reason = (job.error if job is not None and isinstance(job.error, str) and job.error else None)
            sentence = (f'{relay} cancelled it before dialing.' if state == 'cancelled' and not any(
                attempt['submitted_at'] for attempt in tried) else
                f"{relay}'s call failed before any page was sent" + (f': {reason}' if reason else '.'))
            return {'status': FAILED_BEFORE_DATA, 'pages': 0, 'seconds': None, 'charge': self._charge(fax['job_id']),
                    'shared': shared, 'detail': sentence}
        if state == 'reconciliation_required':
            return {'status': UNCERTAIN, 'pages': None, 'seconds': None, 'charge': None, 'shared': shared,
                    'detail': f'{relay} cannot confirm whether the fax arrived. Check with the recipient before '
                              'sending it again.'}
        return None

    def _shared(self, job_id):
        try:
            from ..batching.store import member
            row = member(self.engine, job_id)
        except Exception:
            return False
        return bool(row is not None and row['state'] == 'together' and (row.get('documents') or 0) > 1)

    def _charge(self, job_id):
        """The relay's charge for its own fax: the carrier's report, else its estimate; None while unknown."""
        try:
            from ..routing.carriers import CarrierChargeStore
            from ..routing.spending import Spending
            from ..routing.store import RouteStore
            cost = Spending(RouteStore(self.engine), CarrierChargeStore(self.engine)).job(job_id)
        except Exception:
            return None
        for basis, amounts in (('reported', cost.get('reported_cost') or {}),
                               ('estimated', cost.get('estimated_cost') or {})):
            if len(amounts) == 1:
                currency, micros = next(iter(amounts.items()))
                return {'amount_micros': int(micros), 'currency': currency, 'basis': basis}
        return None

    async def report(self):
        """Send each signed outcome the sender has not confirmed yet; returns how many it confirmed."""
        rows = await run_lifecycle_step(lambda: self.store.faxes_in(
            role='relay', states=(DELIVERED, FAILED_BEFORE_DATA, UNCERTAIN), untold=True))
        told = 0
        from .service import PartnerUnreachable
        for fax in rows:
            peer = await run_lifecycle_step(lambda: self.direct.store.get_peer(fax['peer_id']))
            kept = await run_lifecycle_step(lambda: self.store.statement(fax['outcome_statement_id']))
            if peer is None or kept is None or peer['state'] == 'revoked':
                continue
            try:
                status, body = await self.direct.http.request(
                    'POST', peer['endpoint_url'] + '/direct/relay/statements', json=RelayStore.envelope(kept))
            except (PartnerUnreachable, httpx.HTTPError, OSError):
                continue
            if status == 200 and isinstance(body, dict) and body.get('recorded') is True:
                await run_lifecycle_step(lambda: self.store.mark_fax_told(fax['id'], kept['id']))
                told += 1
        return told

    async def step(self):
        """Background on the relay: settle outcomes, report them, tell partners, keep price statements fresh."""
        values = await run_lifecycle_step(self._values)
        if not getattr(values, 'direct_delivery_enabled', False) or not self.direct.ready():
            return False
        await run_lifecycle_step(self.settle)
        await self.report()
        await run_lifecycle_step(self.refresh_due_prices)
        await self.tell_all()
        return False

    def refresh_due_prices(self, *, now=None):
        now = now or utcnow()
        for row in self.store.agreements_for(role='relay', states=('active',)):
            kept = self.store.latest(peer_id=row['peer_id'], kind='price', direction='sent', agreement_id=row['id']) \
                or self.store.latest(peer_id=row['peer_id'], kind='offer', direction='sent', agreement_id=row['id'])
            if kept is None or kept['created_at'] <= now - PRICE_REFRESH:
                try:
                    self.refresh_price(row['id'], now=now)
                except (RelayConflict, Exception):
                    logging.getLogger(__name__).warning('A relay price statement could not be renewed yet.')

    def outcome_answer(self, message_id, *, signer, request_time, signature, now=None):
        """A sender asks, signed, what happened to a fax it relayed through us; the answer is signed."""
        now = now or utcnow()
        _, identity = self.direct._enabled_identity()
        peer = self.direct.store.peer_by_key(signer) if isinstance(signer, str) else None
        if peer is None or peer['state'] == 'revoked' or not _ID.fullmatch(message_id or ''):
            return 403, {'detail': 'This request is not from an enrolled partner.'}
        try:
            verify(peer['signing_key'], f'GET /direct/relay/outcomes/{message_id} {request_time}'.encode('ascii'),
                   signature)
            if abs(parse_timestamp(request_time) - now) > REQUEST_SKEW:
                raise DirectProtocolError('stale', 'The request time does not match this clock.')
        except (DirectProtocolError, UnicodeEncodeError, TypeError, AttributeError):
            return 403, {'detail': 'This request is not from an enrolled partner.'}
        fax = self.store.fax(role='relay', message_id=message_id)
        if fax is None or fax['peer_id'] != peer['id']:
            return 200, self._sign(identity, peer, 'relay_status', message_id=message_id, status='not_relayed')
        if fax['outcome_statement_id'] is None:
            return 200, self._sign(identity, peer, 'relay_status', message_id=message_id, status='pending')
        return 200, RelayStore.envelope(self.store.statement(fax['outcome_statement_id']))

    # Sender side: outcomes --------------------------------------------------------------------------------------
    def apply_outcome(self, fax, body, envelope):
        """Give one of our faxes the result a relay signed, then keep the receipt; None when it must wait.

        Delivered: the fax succeeded. Failed before any data: the fax failed with the relay's sentence (the
        delivery store's fallback rule decides whether another route may still send it, and records that).
        Uncertain: the fax waits for a person and is never sent again by another route. The receipt is kept
        only once the fax took its result, so a receipt that arrives before the fax is marked sent is asked
        for again.
        """
        outcome = parse_outcome(body)
        if outcome is None:
            return None
        expected = ('accepted', 'sending', UNCERTAIN)
        if fax['state'] not in expected:
            return fax
        delivery = self.delivery()
        if delivery is None:
            return None
        peer = self.direct.store.get_peer(fax['peer_id'])
        name = peer['organization'] if peer else 'The partner'
        key = f"relay:{fax['message_id']}:{outcome['status']}"
        try:
            _, profile = delivery.attempt_context(fax['job_id'], fax['attempt_id'])
            if outcome['status'] == DELIVERED:
                delivery.observe(fax['job_id'], attempt_id=fax['attempt_id'], profile_id=profile.id,
                                 provider_sid=None, status='success', event_key=key)
            elif outcome['status'] == FAILED_BEFORE_DATA:
                delivery.observe(fax['job_id'], attempt_id=fax['attempt_id'], profile_id=profile.id,
                                 provider_sid=None, status='failed', event_key=key,
                                 error=(outcome['detail'] or f'{name} could not send it; nothing was sent.')[:300])
            else:
                delivery.record_unconfirmed(fax['job_id'], attempt_id=fax['attempt_id'], profile_id=profile.id,
                                            event_key=key)
        except Exception:
            logging.getLogger(__name__).warning("A partner relay's receipt arrived before the fax was marked sent; "
                                                'Faxbot asks for it again.')
            return None
        charge = outcome['charge'] or {}
        cost = None
        if outcome['status'] == DELIVERED and outcome['pages'] and outcome['pages'] != fax['pages']:
            cost = self._sender_cost(fax, outcome['pages'])
        return self.store.move_fax(
            fax['id'], outcome['status'], expected=expected, statement=envelope, direction='received',
            delivered_pages=outcome['pages'], seconds=outcome['seconds'], shared=int(outcome['shared']),
            charge_micros=charge.get('amount_micros'), charge_currency=charge.get('currency'),
            charge_basis=charge.get('basis'), detail=outcome['detail'][:300],
            **({'cost_micros': cost[0], 'cost_currency': cost[1]} if cost else {}))

    def _sender_cost(self, fax, pages):
        row = self.store.agreement(fax['agreement_id'])
        price = _price_of(self.store, row) if row else None
        peer = self.direct.store.get_peer(fax['peer_id'])
        found = predicted(price, fax['destination'], pages, peer['organization'] if peer else 'Partner')
        return (found.cost.micros, found.cost.currency) if found is not None and found.cost is not None else None

    async def ask_outcome(self, fax):
        """Ask the relay, signed, for a fax's outcome; applies it and returns its status, or None."""
        from .service import PartnerUnreachable
        peer = await run_lifecycle_step(lambda: self.direct.store.get_peer(fax['peer_id']))
        if peer is None:
            return None
        identity = await run_lifecycle_step(self.direct.identity)
        path = f"/direct/relay/outcomes/{fax['message_id']}"
        moment = timestamp()
        headers = {'X-Faxbot-Direct-Key': identity.signing_key, 'X-Faxbot-Direct-Time': moment,
                   'X-Faxbot-Direct-Signature': identity.sign(f'GET {path} {moment}'.encode('ascii'))}
        try:
            status, body = await self.direct.http.request('GET', peer['endpoint_url'] + path, headers=headers)
            statement = check_signed(body, peer['signing_key'])
        except (PartnerUnreachable, httpx.HTTPError, OSError, DirectProtocolError):
            return None
        if status != 200 or statement.get('type') != 'relay_outcome' or statement.get('recipient') != identity.signing_key \
                or statement.get('message_id') != fax['message_id']:
            return None
        await run_lifecycle_step(lambda: self.apply_outcome(fax, statement, body))
        return statement.get('status')

    # Views -------------------------------------------------------------------------------------------------------
    def view(self, row):
        """One agreement for the console and the command line, in plain sentences."""
        terms = check_terms(json.loads(row['terms'])) or {}
        partner = row.get('organization')
        if not partner:
            peer = self.direct.store.get_peer(row['peer_id'])
            partner = peer['organization'] if peer else 'The partner'
        own = self._organization()
        places = places_text(terms) if terms else 'the numbers it agreed'
        home = getattr(self._values(), 'fax_default_country', None)
        limits = limits_text(terms, home) if terms else ''
        if row['role'] == 'sender':
            relay, sender = partner, own
            summary = f'Send our faxes to {places} through {partner}, {limits}.'
            state = {'offered': f'{partner} offers to send your faxes to {places} as local calls; accept to use it.',
                     'accepting': f'Accepted; Faxbot is telling {partner}.',
                     'active': f'Active: faxes to {places} can go through {partner}.',
                     'withdrawn': ('You ended this agreement.' if row['withdrawn_by'] == 'us'
                                   else f'{partner} ended this agreement.')}[row['state']]
        else:
            relay, sender = own, partner
            summary = f'Let {partner} send faxes to {places} through us, {limits}.'
            state = {'offered': f'Offered; waiting for {partner} to accept.',
                     'accepting': f'Waiting for {partner} to accept.',
                     'active': f'Active: {partner} can send faxes to {places} through you.',
                     'withdrawn': ('You ended this agreement.' if row['withdrawn_by'] == 'us'
                                   else f'{partner} ended this agreement.')}[row['state']]
        same = terms.get('same_organization', False)
        if row['role'] == 'sender':
            notes = [PRIVACY.format(relay=relay), OWN_CALL.format(relay=relay)]
        else:
            notes = [RELAY_PRIVACY.format(partner=sender), OWN_CALL.format(relay=relay)]
            if not same:
                notes.append(RESALE.format(partner=sender))
        if terms.get('together'):
            notes.append(SHARED_CALL.format(relay=relay))
        price = _price_of(self.store, row)
        return {'id': row['id'], 'peer_id': row['peer_id'], 'partner': partner, 'role': row['role'],
                'state': row['state'], 'summary': summary, 'status': state, 'notes': notes,
                'countries': terms.get('countries', []), 'regions': [r['name'] for r in terms.get('regions', [])],
                'monthly_pages': terms.get('monthly_pages'), 'monthly_spend': _money_view(terms.get('monthly_spend')),
                'hours': terms.get('hours'), 'together': bool(terms.get('together')),
                'send_together': bool(row['send_together']), 'same_organization': bool(same),
                'reply_number': row['reply_number'], 'marketing': bool(row.get('marketing')),
                'price': price_view(price, home), 'version': row['version']}

    # Costs and recommendations -----------------------------------------------------------------------------------
    def costs(self, *, days=30, now=None):
        """Each agreement's relayed faxes and money over ``days``: what each side's Costs shows."""
        now = now or utcnow()
        since = now - timedelta(days=days)
        home = getattr(self._values(), 'fax_default_country', None)
        result = []
        for role in ('relay', 'sender'):
            groups = {}
            for fax in self.store.faxes_since(since, role=role):
                if fax['state'] == 'refused' or fax['state'] not in COUNTED:
                    continue
                group = groups.setdefault(fax['agreement_id'], {'partner': fax['organization'], 'faxes': 0,
                                                                'pages': 0, 'money': {}, 'own': {},
                                                                'own_unknown': 0, 'unknown': 0})
                group['faxes'] += 1
                group['pages'] += fax['delivered_pages'] or fax['pages'] or 0
                if role == 'relay':
                    micros, currency = ((fax['charge_micros'], fax['charge_currency'])
                                        if fax['charge_micros'] is not None else (fax['cost_micros'],
                                                                                    fax['cost_currency']))
                else:
                    micros, currency = fax['cost_micros'], fax['cost_currency']
                    if fax['own_route_micros'] is not None and fax['own_route_currency']:
                        group['own'][fax['own_route_currency']] = (group['own'].get(fax['own_route_currency'], 0)
                                                                   + int(fax['own_route_micros']))
                    else:
                        group['own_unknown'] += 1
                if micros is None or not currency:
                    group['unknown'] += 1
                else:
                    group['money'][currency] = group['money'].get(currency, 0) + int(micros)
            for agreement_id, group in groups.items():
                count = f"{group['faxes']} fax{'es' if group['faxes'] != 1 else ''}"
                money = money_list_for(group['money'], home) if group['money'] else None
                if role == 'relay':
                    sentence = f"Relayed for {group['partner']}: {count}" + (f', {money}' if money else '')
                    if group['unknown']:
                        sentence += f" ({group['unknown']} not priced yet)"
                else:
                    sentence = f"Sent through {group['partner']}: {count}" + (f', {money}' if money else '')
                    own = money_list_for(group['own'], home)
                    # Compared only in the same currency; amounts are never converted.
                    if group['own'] and not group['own_unknown'] and set(group['own']) == set(group['money']) \
                            and not group['unknown']:
                        sentence += f", against about {own} calling from {country_name(home or 'US')}"
                    elif group['own'] and not group['own_unknown']:
                        sentence += f"; your own route would have cost about {own}"
                result.append({'agreement_id': agreement_id, 'role': role, 'partner': group['partner'],
                               'faxes': group['faxes'], 'pages': group['pages'], 'amounts': _amounts(group['money']),
                               'own_route': _amounts(group['own']) if role == 'sender' else [],
                               'sentence': sentence + '.'})
        return result

    def recommendations(self, *, days=30, now=None):
        """Partners whose signed local price would have cost less than this installation's own calls lately.

        Only partners that gave a signed price (an offer, an agreement or a quote) are named, and only when the
        saving is in the same currency; nothing is relayed until both sides sign an agreement.
        """
        import sqlalchemy as sa
        from ..routing.database import read_connection, reflect
        now = now or utcnow()
        home = getattr(self._values(), 'fax_default_country', None)
        tables = reflect(self.engine, ('delivery_attempt_costs', 'fax_jobs'))
        costs, jobs = tables['delivery_attempt_costs'], tables['fax_jobs']
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(
                costs.c.destination, costs.c.route, costs.c.estimated_cost_micros, costs.c.currency,
                costs.c.reported_cost_micros, costs.c.reported_currency, jobs.c.pages).select_from(
                costs.join(jobs, jobs.c.id == costs.c.job_id)).where(
                costs.c.outcome == 'success', costs.c.created_at >= now - timedelta(days=days))).all()
        prices = self._known_prices()
        result = []
        for peer, price, agreement in prices:
            label = peer['organization']
            countries = {entry['country'] for entry in price['routes']} - {price['home']} | {price['home']}
            for country in sorted(countries):
                own, relayed, count = {}, {}, 0
                for row in rows:
                    if (row.route or '').startswith(LEDGER_PREFIX) or country_of(row.destination) != country:
                        continue
                    micros, currency = ((row.reported_cost_micros, row.reported_currency)
                                        if row.reported_cost_micros is not None
                                        else (row.estimated_cost_micros, row.currency))
                    found = predicted(price, row.destination, row.pages or 1, label, now=now)
                    if micros is None or found is None or found.cost is None or found.cost.currency != currency:
                        continue
                    own[currency] = own.get(currency, 0) + int(micros)
                    relayed[currency] = relayed.get(currency, 0) + found.cost.micros
                    count += 1
                for currency in own:
                    saving = own[currency] - relayed[currency]
                    if count and saving > 0:
                        result.append({
                            'peer_id': peer['id'], 'partner': label, 'country': country,
                            'agreement_id': agreement['id'] if agreement else None,
                            'agreement_state': agreement['state'] if agreement else None, 'faxes': count,
                            'saving': {'amount_micros': saving, 'currency': currency},
                            'sentence': (f'Faxes to numbers in {country_name(country)} would have cost about '
                                         f'{money_for(saving, currency, home)} less through {label}.'),
                            'action': (f'Accept {label}\'s offer under Partners to use it.' if agreement and
                                       agreement['state'] == 'offered' else
                                       f'Ask {label} to relay for you; they grant it under Partners, {label}, '
                                       'Relay.')})
        return sorted(result, key=lambda item: -item['saving']['amount_micros'])

    def _known_prices(self):
        """(partner, price, agreement or None) for each verified partner that gave a signed price still valid."""
        found = []
        now = utcnow()
        for peer in self.direct.store.list_peers():
            if peer['state'] != 'verified':
                continue
            agreements = [row for row in self.store.agreements_for(peer_id=peer['id'], role='sender')
                          if row['state'] != 'withdrawn']
            if any(row['state'] == 'active' for row in agreements):
                continue  # Already relaying: Costs shows what it saved.
            agreement = agreements[-1] if agreements else None
            price = _price_of(self.store, agreement) if agreement else None
            if price is None:
                kept = self.store.latest(peer_id=peer['id'], kind='price', direction='received')
                body = _statement_body(RelayStore.envelope(kept)) if kept else None
                price = check_price(body.get('price')) if isinstance(body, dict) else None
            if price is not None and parse_timestamp(price['valid_until']) >= now:
                found.append((peer, price, agreement))
        return found


class _AlreadyAccepted(Exception):
    """The same partner document was accepted by an earlier request."""


@dataclass(frozen=True)
class _RelaySender:
    """The relay's dedicated sender as sending-together reads it: its principal and one scope per partner."""
    agreement: dict

    @property
    def principal_id(self):
        return self.agreement['principal_id']

    @property
    def replay_scope(self):
        return 'relay:' + self.agreement['peer_id']


def clean_marketing(value):
    """The sender's marketing-fax details ({business_number, contact, opt_out}) or None when not marketing."""
    if not isinstance(value, dict):
        return None
    clean = {key: re.sub(r'\s+', ' ', str(value.get(key) or '')).strip()[:120]
             for key in ('business_number', 'contact', 'opt_out')}
    return clean if any(clean.values()) else None


def parse_outcome(body):
    """A relay outcome's fields, strictly shaped, or None."""
    try:
        if body.get('type') != 'relay_outcome' or body.get('status') not in OUTCOMES:
            return None
        pages, seconds, charge = body.get('pages'), body.get('seconds'), body.get('charge')
        if pages is not None and (type(pages) is not int or pages < 0):
            return None
        if seconds is not None and (type(seconds) is not int or seconds < 0):
            return None
        if charge is not None and (not isinstance(charge, dict) or set(charge) != {'amount_micros', 'currency',
                                                                                   'basis'}
                                   or type(charge['amount_micros']) is not int
                                   or not _CURRENCY.fullmatch(str(charge['currency']))
                                   or charge['basis'] not in ('reported', 'estimated')):
            return None
        if type(body.get('shared')) is not bool or not isinstance(body.get('detail'), str):
            return None
        return {'status': body['status'], 'pages': pages, 'seconds': seconds, 'charge': charge,
                'shared': body['shared'], 'detail': body['detail']}
    except AttributeError:
        return None


def _money_view(spend):
    if not spend:
        return None
    from ..routing.costs import format_amount
    return {'amount': format_amount(spend['amount_micros']), 'currency': spend['currency']}


def _amounts(money):
    from ..routing.costs import format_amount
    return [{'amount': format_amount(micros), 'currency': currency} for currency, micros in sorted(money.items())]


def price_view(price, home=None):
    """A signed price statement for people: each country's local price and when it was given."""
    if price is None:
        return None
    entries = []
    for entry in price['routes']:
        terms = entry['terms']
        if terms is None:
            text = f"{entry['label']} gave no price for these numbers."
        elif terms['monthly_fee_micros'] and not (terms['per_minute_micros'] or terms['per_page_micros']
                                                   or terms['per_call_micros']):
            text = 'Included in their monthly plan.'
        else:
            parts = []
            if terms['per_minute_micros']:
                parts.append(f"{money_for(terms['per_minute_micros'], terms['currency'], home)} a minute")
            if terms['per_page_micros']:
                parts.append(f"{money_for(terms['per_page_micros'], terms['currency'], home)} a page")
            if terms['per_call_micros']:
                parts.append(f"{money_for(terms['per_call_micros'], terms['currency'], home)} a call")
            text = ', '.join(parts) if parts else 'No charge.'
        kind = 'toll-free numbers' if entry['kind'] == 'toll_free' else 'numbers'
        entries.append({'country': entry['country'], 'kind': entry['kind'],
                        'text': f"{kind[0].upper() + kind[1:]} in {country_name(entry['country'])}: {text}"})
    return {'priced_at': price['priced_at'], 'valid_until': price['valid_until'], 'routes': entries}


# The fax engine's hook (AW) --------------------------------------------------------------------------------------

def sender_identity_for(engine, job_id):
    """(header text, station ID) naming the true sender of a fax this installation relays, or None.

    For a relayed fax the engine prints this in its header line and sends it as
    the station ID (TSI), so the recipient sees one sender: the organization
    that sent the fax through the relay and its reply number. None for any
    other fax, and for a call shared with faxes from other senders, which keeps
    the relay's own header and number (each document's top line still names
    its sender). Caller ID is never changed here: it stays the relay's line.
    """
    import sqlalchemy as sa
    from ..routing.database import read_connection
    try:
        store = RelayStore(engine)
        fax = store.fax_for_job(job_id, role='relay')
        if fax is None:
            return None
        agreement = store.agreement(fax['agreement_id'])
        peer = None
        with read_connection(engine) as connection:
            peer = connection.execute(sa.select(store.peers.c.organization, store.peers.c.phone_number).where(
                store.peers.c.id == fax['peer_id'])).first()
        if peer is None or agreement is None:
            return None
        from ..batching.store import call_members, member
        own = member(engine, job_id)
        if own is not None and own['state'] == 'together' and own['batch_id']:
            others = {row['id'] for row in call_members(engine, own['batch_id'])} - {job_id}
            for other in others:
                found = store.fax_for_job(other, role='relay')
                if found is None or found['peer_id'] != fax['peer_id']:
                    return None
        return peer.organization[:50], (agreement['reply_number'] or peer.phone_number)
    except Exception:
        return None
