"""Prices by the caller ID a call presents: origin-aware rate decks (N15).

Carriers price calls to many countries, most of all EU countries, by the
caller ID the call presents, not only by the number it reaches. EU termination
caps (Delegated Regulation (EU) 2021/654) do not cover calls from numbers
outside the EU, so a German mobile costs a carrier far more when the caller ID
is American. Carriers pass that on in their rate decks:

- Telnyx defines four origination types (support.telnyx.com, "Updates to
  Global Conversational Rate Deck", read 2026-10-08): **Local** (a caller ID
  from the destination's own country, bought on Telnyx; a number not bought on
  Telnyx is not local), **EEA** (a caller ID from another EEA country),
  **Non-surcharged** (a caller ID from a set of countries listed for that
  destination in the deck's "Origination Prefixes" column) and **Surcharged**
  (anything else). Telnyx publishes its deck only inside Mission Control
  (outbound voice profile, Billing Method, "download and view our rate deck",
  support.telnyx.com article 4320411, read 2026-10-09) and has not published the
  file's column layout, so Faxbot does not guess it: export it into Faxbot's
  own layout (``faxbot``, below) to import it.
- Twilio publishes its whole deck as a CSV
  (twilio.com/content/dam/twilio-com/pricing-data/en/csv/..._OutboundVoicePricing.csv,
  read 2026-10-09) with the columns ``ISO, Country, Description, Price / min,
  Origination Prefixes, Destination Prefixes``. A row whose description ends
  "- from EEA" or "- from US/CA" lists the caller-ID prefixes that get it;
  a row with no origination prefixes is the price for every other caller ID.
  In that file a call to an Austrian landline costs $0.155 a minute, or $0.016
  with a caller ID from the EEA or the US and Canada.

So a deck row here has a destination prefix, an origination type, the caller-ID
prefixes that qualify (none for the surcharged row) and a price. To price a
call, ``price_origin`` takes the rows with the longest destination prefix the
number matches; within them the row whose caller-ID prefixes hold the longest
prefix of the caller ID (a Bahamas +1242 caller ID gets the Bahamas row, not
the US/Canada "1" row), or a local row for a caller ID from the destination's
own country; else the surcharged row.

**Eligibility is recorded, never inferred.** A carrier rates a caller ID by
conditions Faxbot cannot see: whether the number was bought on that account,
ported in or only verified, and whether you may send from it at all. So a
cheaper row applies only when you confirmed that caller ID on that account
(``record_eligibility``, with your evidence); a local row also needs your
confirmation that the number was bought on that account. Until then the call
is priced at the dearer of the surcharged row and the row the caller ID would
get, and the cheaper price is shown beside it as not yet confirmed. An
anonymous or malformed caller ID always gets the surcharged row. Owning a
number, or having it among a trunk's numbers, is never taken as eligibility.

Faxbot never changes the caller ID a trunk presents to reach a cheaper row.
A cheaper row is reached only by sending from another account or site whose
own caller ID is eligible, which the route choice does by price.

The caller ID priced is the one the call would carry
(``ami.originate_fields_for``): the reply number when the carrier gives it to
you on that trunk, else the trunk's own caller ID. Pricing has no mailbox, so
it uses the organization's reply number; a mailbox's own reply number can
present another caller ID. Cloud fax services set their own sending number, so
their calls are not priced here.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import io
import re
from uuid import uuid4

import sqlalchemy as sa

from .costs import InvalidRateCard, RateCard, RateTerms, parse_amount


LOCAL, EEA, NON_SURCHARGED, SURCHARGED = 'local', 'eea', 'non_surcharged', 'surcharged'
TYPES = (LOCAL, EEA, NON_SURCHARGED, SURCHARGED)
TWILIO, FAXBOT = 'twilio', 'faxbot'
FORMATS = (TWILIO, FAXBOT)
CONFIRMED, WITHDRAWN = 'confirmed', 'withdrawn'
# Who may get each row, for a sentence ("calls with a caller ID from the EEA").
TYPE_TEXT = {LOCAL: 'a caller ID from the same country, bought on this account',
             EEA: 'a caller ID from the countries the carrier lists as the EEA',
             NON_SURCHARGED: 'a caller ID from the countries the carrier lists for this destination',
             SURCHARGED: 'any other caller ID'}
TYPE_LABEL = {LOCAL: 'Local caller ID', EEA: 'EEA caller ID', NON_SURCHARGED: 'Listed caller ID',
              SURCHARGED: 'Any other caller ID'}
# What a price's origin says, in the quote screens (``origin_rates.origin_label``).
ORIGIN_PREFIX = 'caller:'
NOT_CONFIRMED = 'unconfirmed'
NO_CALLER_ID = 'none'
ORIGIN_TEXT = {f'{ORIGIN_PREFIX}{kind}': TYPE_LABEL[kind] for kind in TYPES}
ORIGIN_TEXT[f'{ORIGIN_PREFIX}{NOT_CONFIRMED}'] = 'Caller ID not confirmed: the price for any other caller ID'
ORIGIN_TEXT[f'{ORIGIN_PREFIX}{NO_CALLER_ID}'] = 'No usable caller ID: the price for any other caller ID'
TELNYX_LAYOUT = ("Telnyx publishes its rate deck only in Mission Control (your outbound voice profile, Billing Method) "
                 "and has not published the file's columns, so save it in Faxbot's own layout to import it.")
FAXBOT_LAYOUT = ('One row per price: destination_prefix (digits, or several separated by commas), origination_type '
                 '(local, eea, non_surcharged or surcharged), origination_prefixes (the caller-ID prefixes that get '
                 'it; empty for surcharged and local), per_minute, and optionally billing_increment_seconds, '
                 'minimum_seconds and description.')
MAX_ROWS = 200_000
_DIGITS = re.compile(r'[1-9][0-9]{0,14}')


class DeckError(ValueError):
    """A rate deck Faxbot cannot read; the message is one plain sentence."""


class EligibilityError(ValueError):
    """A caller-ID confirmation Faxbot cannot record; the message is one plain sentence."""


@dataclass(frozen=True)
class ClassRow:
    """One deck row: calls to ``destination_prefix`` with a caller ID of ``origination_type`` cost this much."""
    route: str
    destination_prefix: str
    origination_type: str
    origin_prefixes: tuple
    currency: str
    per_minute_micros: int
    billing_increment_seconds: int
    minimum_seconds: int
    description: str | None = None
    source_url: str | None = None
    captured_on: datetime | None = None
    deck_format: str = FAXBOT
    id: str | None = None
    import_id: str | None = None

    def __post_init__(self):
        if self.origination_type not in TYPES:
            raise DeckError('Each row needs an origination type: local, eea, non_surcharged or surcharged.')
        if _DIGITS.fullmatch(self.destination_prefix or '') is None:
            raise DeckError('Write each destination prefix as digits after the plus sign, such as 4915.')
        if any(_DIGITS.fullmatch(prefix) is None for prefix in self.origin_prefixes):
            raise DeckError('Write each caller-ID prefix as digits after the plus sign, such as 44.')
        if self.origination_type in (EEA, NON_SURCHARGED) and not self.origin_prefixes:
            raise DeckError('An EEA or listed row needs the caller-ID prefixes that get it.')
        if self.origination_type in (SURCHARGED, LOCAL) and self.origin_prefixes:
            raise DeckError('A surcharged or local row takes no caller-ID prefixes.')
        self.card()  # the price, increment and minimum checks of a rate card

    def card(self, label=None):
        """This row as a rate card, for the predictor's arithmetic (``costs.terms_cost``)."""
        return RateCard(self.id, self.route, 'outbound', (label or self.description or self.route)[:100], self.currency,
                        self.per_minute_micros, 0, 0, self.billing_increment_seconds, self.minimum_seconds,
                        self.source_url, self.captured_on or datetime(2026, 1, 1))

    def origin_match(self, caller_digits):
        """The length of the longest of this row's caller-ID prefixes that ``caller_digits`` starts with; 0 for none."""
        return max((len(prefix) for prefix in self.origin_prefixes if caller_digits.startswith(prefix)), default=0)


# -- reading a deck --------------------------------------------------------------------------------------------------

_TWILIO = ('iso', 'country', 'description', 'price / min', 'origination prefixes', 'destination prefixes')
_FAXBOT = {'destination': ('destination_prefix', 'destination_prefixes', 'destination prefix', 'destination prefixes',
                           'prefix'),
           'type': ('origination_type', 'origination type'),
           'origins': ('origination_prefixes', 'origination prefixes'),
           'price': ('per_minute', 'rate', 'price / min', 'price_per_minute'),
           'increment': ('billing_increment_seconds', 'billing increment'),
           'minimum': ('minimum_seconds', 'minimum'),
           'description': ('description', 'destination', 'name')}
_TYPE_WORDS = {'local': LOCAL, 'eea': EEA, 'non_surcharged': NON_SURCHARGED, 'non-surcharged': NON_SURCHARGED,
               'nonsurcharged': NON_SURCHARGED, 'non surcharged': NON_SURCHARGED, 'surcharged': SURCHARGED}


def _prefixes(text):
    return [re.sub(r'[^0-9]', '', part) for part in re.split(r'[,;\s]+', str(text or '')) if part.strip()]


def detect_format(text):
    """'twilio' when the header is Twilio's exact one, else 'faxbot'."""
    first = str(text or '').lstrip('﻿').splitlines()[0] if str(text or '').strip() else ''
    columns = tuple(part.strip().lower() for part in first.split(','))
    return TWILIO if columns[:6] == _TWILIO else FAXBOT


def _twilio_type(description, origins):
    """Twilio's description says who gets a row: "... - from EEA" is the EEA row; "... - from US/CA" and any other
    row with caller-ID prefixes is a listed row; a row without them is the price for every other caller ID."""
    words = description.strip().lower()
    if words.endswith('- from eea'):
        return EEA
    if origins:
        return NON_SURCHARGED
    return SURCHARGED


def parse_deck(text, *, route, currency='USD', deck_format=None, billing_increment_seconds=60, minimum_seconds=0,
               source_url=None, captured_on=None):
    """(rows, skipped) from a deck's CSV text; ``skipped`` counts lines Faxbot could not read, with one reason each.

    Twilio's file has no billing increment or currency column: its rows take the card's own
    (``billing_increment_seconds``, ``minimum_seconds``, ``currency``), and the import result says so.
    """
    if not isinstance(text, str) or not text.strip():
        raise DeckError('The rate deck is empty.')
    deck_format = deck_format or detect_format(text)
    if deck_format not in FORMATS:
        raise DeckError("Choose the deck's layout: twilio or faxbot.")
    reader = csv.reader(io.StringIO(text.lstrip('﻿')))
    try:
        header = [column.strip().lower() for column in next(reader)]
    except StopIteration:
        raise DeckError('The rate deck is empty.') from None
    if deck_format == TWILIO:
        if tuple(header[:6]) != _TWILIO:
            raise DeckError("This is not Twilio's voice price file: its first line must be ISO, Country, Description, "
                            'Price / min, Origination Prefixes, Destination Prefixes.')
        at = {'description': 2, 'price': 3, 'origins': 4, 'destination': 5}
    else:
        at = {}
        for key, names in _FAXBOT.items():
            found = next((header.index(name) for name in names if name in header), None)
            if found is not None:
                at[key] = found
        missing = [key for key in ('destination', 'type', 'price') if key not in at]
        if missing:
            raise DeckError('The deck needs the columns destination_prefix, origination_type and per_minute. '
                            + FAXBOT_LAYOUT)
    rows, skipped, seen = [], [], set()
    for number, line in enumerate(reader, start=2):
        if not line or not any(cell.strip() for cell in line):
            continue
        if len(line) <= max(at.values()):
            skipped.append(f'Line {number} has too few columns.')
            continue
        description = line[at['description']].strip()[:200] if 'description' in at else ''
        origins = tuple(prefix for prefix in _prefixes(line[at['origins']]) if prefix) if 'origins' in at else ()
        if deck_format == TWILIO:
            kind = _twilio_type(description, origins)
        else:
            kind = _TYPE_WORDS.get(line[at['type']].strip().lower())
            if kind is None:
                skipped.append(f'Line {number} has an origination type Faxbot does not know.')
                continue
            if kind in (SURCHARGED, LOCAL):
                origins = ()
        try:
            price = parse_amount(line[at['price']].strip())
            increment = int(line[at['increment']]) if 'increment' in at and line[at['increment']].strip() \
                else billing_increment_seconds
            least = int(line[at['minimum']]) if 'minimum' in at and line[at['minimum']].strip() else minimum_seconds
        except (InvalidRateCard, ValueError):
            skipped.append(f'Line {number} has a price Faxbot cannot read.')
            continue
        for prefix in _prefixes(line[at['destination']]):
            key = (prefix, kind, origins)
            if key in seen:
                continue
            seen.add(key)
            try:
                rows.append(ClassRow(route, prefix, kind, origins, currency, price, increment, least,
                                     description or None, source_url, captured_on, deck_format))
            except (DeckError, InvalidRateCard) as error:
                skipped.append(f'Line {number}: {error}')
                break
        if len(rows) > MAX_ROWS:
            raise DeckError(f'The deck has more than {MAX_ROWS:,} prices; import one carrier deck at a time.')
    if not rows:
        raise DeckError('The deck has no prices Faxbot can read. ' + FAXBOT_LAYOUT)
    return rows, skipped


# -- stored decks ----------------------------------------------------------------------------------------------------

# The 0069 tables as light constructs (no reflection), so pricing one call costs one indexed read.
_TYPES = {'per_minute_micros': sa.Integer(), 'billing_increment_seconds': sa.Integer(), 'minimum_seconds': sa.Integer(),
          'captured_on': sa.DateTime(), 'superseded_at': sa.DateTime(), 'created_at': sa.DateTime()}


def _light(name, columns):
    return sa.table(name, *(sa.column(column, _TYPES.get(column, sa.String())) for column in columns))


RATES = _light('origin_class_rates', (
    'id', 'import_id', 'route', 'destination_prefix', 'origination_type', 'origin_prefixes', 'description', 'currency',
    'per_minute_micros', 'billing_increment_seconds', 'minimum_seconds', 'deck_format', 'source_url', 'captured_on',
    'superseded_at', 'imported_by', 'imported_by_name', 'created_at'))
ELIGIBILITY = _light('caller_id_eligibility', (
    'id', 'account', 'caller_id', 'state', 'bought_here', 'evidence', 'evidence_url', 'recorded_by',
    'recorded_by_name', 'created_at'))


def _row(found):
    return ClassRow(found['route'], found['destination_prefix'], found['origination_type'],
                    tuple(prefix for prefix in str(found['origin_prefixes'] or '').split(',') if prefix),
                    found['currency'], int(found['per_minute_micros']), int(found['billing_increment_seconds']),
                    int(found['minimum_seconds']), found['description'], found['source_url'], found['captured_on'],
                    found['deck_format'], found['id'], found['import_id'])


def import_deck(engine, route, rows, *, actor=None, now=None):
    """Make ``rows`` the route's deck; the route's earlier rows are superseded, never changed. Returns its summary."""
    from .database import utcnow, write_transaction
    now = now or utcnow().replace(microsecond=0)
    import_id = uuid4().hex
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(RATES.update().where(RATES.c.route == route, RATES.c.superseded_at.is_(None))
                           .values(superseded_at=now))
        batch = []
        for row in rows:
            batch.append({'id': uuid4().hex, 'import_id': import_id, 'route': route,
                          'destination_prefix': row.destination_prefix, 'origination_type': row.origination_type,
                          'origin_prefixes': ','.join(row.origin_prefixes), 'description': row.description,
                          'currency': row.currency, 'per_minute_micros': row.per_minute_micros,
                          'billing_increment_seconds': row.billing_increment_seconds,
                          'minimum_seconds': row.minimum_seconds, 'deck_format': row.deck_format,
                          'source_url': row.source_url, 'captured_on': row.captured_on or now,
                          'superseded_at': None, 'imported_by': actor.get('id'),
                          'imported_by_name': actor.get('name'), 'created_at': now})
            if len(batch) >= 5_000:
                connection.execute(RATES.insert(), batch)
                batch = []
        if batch:
            connection.execute(RATES.insert(), batch)
    return deck_summary(engine, route)


def has_rows(engine, routes):
    """True when any of ``routes`` has a current deck (one indexed read)."""
    from .database import read_connection
    routes = [route for route in routes if route]
    if engine is None or not routes:
        return False
    with read_connection(engine) as connection:
        return connection.execute(sa.select(RATES.c.id).where(
            RATES.c.route.in_(routes), RATES.c.superseded_at.is_(None)).limit(1)).first() is not None


def rows_for(engine, routes, destination):
    """The current rows of the first of ``routes`` with a deck that covers ``destination``; [] when none does.

    One indexed read: only the rows whose destination prefix is a prefix of the number.
    """
    from .database import read_connection
    digits = re.sub(r'[^0-9]', '', str(destination or ''))[:15]
    routes = [route for route in dict.fromkeys(routes) if route]
    if engine is None or not routes or not digits:
        return []
    prefixes = [digits[:length] for length in range(1, len(digits) + 1)]
    with read_connection(engine) as connection:
        found = connection.execute(sa.select(RATES).where(
            RATES.c.route.in_(routes), RATES.c.destination_prefix.in_(prefixes),
            RATES.c.superseded_at.is_(None))).mappings().all()
    for route in routes:
        own = [_row(row) for row in found if row['route'] == route]
        if own:
            return own
    return []


def deck_summary(engine, route):
    """What the route's current deck holds, for the console and the command line; None without one."""
    from .database import read_connection
    with read_connection(engine) as connection:
        found = connection.execute(
            sa.select(RATES.c.import_id, RATES.c.deck_format, RATES.c.source_url, RATES.c.captured_on,
                      RATES.c.currency, RATES.c.created_at, RATES.c.imported_by_name, RATES.c.origination_type,
                      sa.func.count().label('rows'),
                      sa.func.count(sa.distinct(RATES.c.destination_prefix)).label('destinations'))
            .where(RATES.c.route == route, RATES.c.superseded_at.is_(None))
            .group_by(RATES.c.import_id, RATES.c.deck_format, RATES.c.source_url, RATES.c.captured_on,
                      RATES.c.currency, RATES.c.created_at, RATES.c.imported_by_name, RATES.c.origination_type)
        ).mappings().all()
    if not found:
        return None
    first = found[0]
    return {'route': route, 'format': first['deck_format'], 'source_url': first['source_url'],
            'published_on': first['captured_on'].date().isoformat() if first['captured_on'] else None,
            'imported_at': first['created_at'].isoformat() if first['created_at'] else None,
            'imported_by': first['imported_by_name'], 'currency': first['currency'],
            'rows': sum(int(item['rows']) for item in found),
            'by_type': {item['origination_type']: int(item['rows']) for item in found}}


def decks(engine):
    """{route: summary} for every route with a current deck."""
    from .database import read_connection
    with read_connection(engine) as connection:
        routes = connection.execute(sa.select(sa.distinct(RATES.c.route)).where(
            RATES.c.superseded_at.is_(None))).scalars().all()
    return {route: deck_summary(engine, route) for route in sorted(routes)}


# -- caller-ID eligibility -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Eligibility:
    account: str
    caller_id: str
    state: str
    bought_here: bool
    evidence: str | None = None
    evidence_url: str | None = None
    recorded_by: str | None = None
    recorded_at: datetime | None = None

    @property
    def confirmed(self):
        return self.state == CONFIRMED


def _caller(value):
    """``value`` as an E.164 caller ID Faxbot can place in a country, or None (anonymous, malformed, placeholder)."""
    import phonenumbers
    text = str(value or '').strip()
    if not text.startswith('+'):
        return None
    try:
        parsed = phonenumbers.parse(text, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _account_key(value):
    if not isinstance(value, str) or re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', value.strip()) is None:
        raise EligibilityError('Choose one of your sending accounts.')
    return value.strip()


def record_eligibility(engine, account, caller_id, *, bought_here, evidence, evidence_url=None, actor=None, now=None):
    """Record that you hold ``caller_id`` and may send faxes from it on ``account``, with your evidence."""
    from .database import utcnow, write_transaction
    account = _account_key(account)
    number = _caller(caller_id)
    if number is None:
        raise EligibilityError('Enter the caller ID with its country code, such as +442079460000.')
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
        raise EligibilityError('Say how you know, such as the number order or invoice, in up to 2,000 characters.')
    if evidence_url is not None and (not isinstance(evidence_url, str) or len(evidence_url) > 512
                                     or re.fullmatch(r'https?://\S+', evidence_url) is None):
        raise EligibilityError('The evidence link must be a web address.')
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(ELIGIBILITY.insert().values(
            id=uuid4().hex, account=account, caller_id=number, state=CONFIRMED,
            bought_here='yes' if bought_here else 'no', evidence=evidence.strip(), evidence_url=evidence_url,
            recorded_by=actor.get('id'), recorded_by_name=actor.get('name'),
            created_at=_after_newest(connection, account, number, now or utcnow())))
    return eligibility(engine, account, number)


def _after_newest(connection, account, number, now):
    """``now``, or just after the newest record for this caller ID, so the newest record is always the last one."""
    from datetime import timedelta
    newest = connection.execute(sa.select(sa.func.max(ELIGIBILITY.c.created_at)).where(
        ELIGIBILITY.c.account == account, ELIGIBILITY.c.caller_id == number)).scalar()
    if isinstance(newest, str):
        newest = datetime.fromisoformat(newest)
    return max(now, newest + timedelta(microseconds=1)) if newest is not None else now


def withdraw_eligibility(engine, account, caller_id, *, note=None, actor=None, now=None):
    """Withdraw a confirmation; the earlier one stays as history. Calls from it are priced unconfirmed again."""
    from .database import utcnow, write_transaction
    account = _account_key(account)
    number = _caller(caller_id)
    current = eligibility(engine, account, number) if number else None
    if current is None or not current.confirmed:
        raise EligibilityError('This caller ID is not confirmed on that account.')
    actor = actor or {}
    with write_transaction(engine) as connection:
        connection.execute(ELIGIBILITY.insert().values(
            id=uuid4().hex, account=account, caller_id=number, state=WITHDRAWN, bought_here='no',
            evidence=(note or '').strip()[:2000] or None, evidence_url=None, recorded_by=actor.get('id'),
            recorded_by_name=actor.get('name'), created_at=_after_newest(connection, account, number,
                                                                          now or utcnow())))
    return eligibility(engine, account, number)


def _eligibility(row):
    return Eligibility(row['account'], row['caller_id'], row['state'], row['bought_here'] == 'yes', row['evidence'],
                       row['evidence_url'], row['recorded_by_name'], row['created_at'])


def eligibility(engine, account, caller_id):
    """The newest confirmation or withdrawal for ``caller_id`` on ``account``; None when none was recorded."""
    from .database import read_connection
    number = _caller(caller_id)
    if engine is None or not account or number is None:
        return None
    with read_connection(engine) as connection:
        found = connection.execute(sa.select(ELIGIBILITY).where(
            ELIGIBILITY.c.account == account, ELIGIBILITY.c.caller_id == number)
            .order_by(ELIGIBILITY.c.created_at.desc(), ELIGIBILITY.c.id.desc()).limit(1)).mappings().first()
    return _eligibility(found) if found else None


def eligibility_records(engine):
    """The newest record for every (account, caller ID) ever recorded."""
    from .database import read_connection
    with read_connection(engine) as connection:
        found = connection.execute(sa.select(ELIGIBILITY).order_by(
            ELIGIBILITY.c.created_at, ELIGIBILITY.c.id)).mappings().all()
    newest = {}
    for row in found:
        newest[(row['account'], row['caller_id'])] = _eligibility(row)
    return newest


# -- the caller ID a call presents -------------------------------------------------------------------------------------

def presented_caller_id(values, account_key, *, engine=None):
    """(caller ID or None, how) for a fax sent by ``account_key``: the number the call would carry.

    ``how`` is 'trunk' (a SIP trunk's call: the reply number when the carrier gives it to you on that trunk, else
    the trunk's caller ID, as ``ami.originate_fields_for`` sets it), 'freeswitch' or 'provider' (a cloud fax service
    sets its own sending number, so None).
    """
    from ..accounts import AccountsError, account_named, account_values
    account = account_named(values, account_key)
    provider = account.provider if account is not None else account_key
    if provider == 'freeswitch':
        return (getattr(values, 'fs_caller_id_number', '') or None), 'freeswitch'
    if provider != 'sip':
        return None, 'provider'
    own = values
    if account is not None and not account.primary:
        try:
            own = account_values(values, account_key)
        except AccountsError:
            return None, 'trunk'
    from .. import sip_trunk
    from .reply_number import Choice, caller_id_for, choose
    if not sip_trunk.configured(own):
        return None, 'trunk'
    try:
        store = None
        if engine is not None:
            from .store import RouteStore
            store = RouteStore(engine)
        choice = choose(own, engine=engine, store=store)
    except Exception:  # noqa: BLE001 - mirrors ami.reply_choice: an unreadable reply number means the line's own
        choice = Choice(None, 'line', '')
    return (caller_id_for(own, choice.number) or getattr(own, 'sip_trunk_caller_id', '') or None), 'trunk'


# -- pricing one call --------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class OriginQuote:
    """How one call is priced by caller ID: the row used, why, and the cheaper row a confirmation would unlock."""
    row: ClassRow | None
    caller_id: str | None
    eligibility: str            # 'confirmed', 'unconfirmed', 'not_needed' or 'no_caller_id'
    given: ClassRow | None      # the row the deck gives this caller ID, eligibility aside
    sentence: str

    @property
    def origin(self):
        """The price's origin for ``pricing.Price.origin``: 'caller:eea', 'caller:unconfirmed', 'caller:none'."""
        if self.eligibility == 'no_caller_id':
            return ORIGIN_PREFIX + NO_CALLER_ID
        if self.eligibility == 'unconfirmed':
            return ORIGIN_PREFIX + NOT_CONFIRMED
        return ORIGIN_PREFIX + (self.row.origination_type if self.row is not None else SURCHARGED)

    @property
    def cheaper(self):
        """The row a confirmation would unlock, when it costs less than the one used."""
        if self.eligibility != 'unconfirmed' or self.given is None or self.row is None:
            return None
        return self.given if self.given.per_minute_micros < self.row.per_minute_micros else None


def _region(e164):
    import phonenumbers
    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(e164, None))
    except phonenumbers.NumberParseException:
        return None


def _money(row):
    from .costs import money_text
    return f'{money_text(row.per_minute_micros, row.currency)} a minute'


def price_origin(rows, destination, caller_id, record=None) -> OriginQuote | None:
    """How a call to ``destination`` presenting ``caller_id`` is priced by ``rows`` (one route's deck), or None
    when the deck has no row for the number. ``record`` is the caller ID's newest ``Eligibility`` on the account.

    Pure: no database. The row is None when the deck covers the number but has no price this caller ID may use
    (no surcharged row): unknown, never zero.
    """
    digits = re.sub(r'[^0-9]', '', str(destination or ''))
    matching = [row for row in rows if digits.startswith(row.destination_prefix)]
    if not matching:
        return None
    longest = max(len(row.destination_prefix) for row in matching)
    group = [row for row in matching if len(row.destination_prefix) == longest]
    default = max((row for row in group if row.origination_type == SURCHARGED),
                  key=lambda row: row.per_minute_micros, default=None)
    caller = _caller(caller_id)
    if caller is None:
        sentence = ('The call has no usable caller ID, so it is priced at the rate for any other caller ID'
                    + (f' ({_money(default)}).' if default else ', which this deck does not list.'))
        return OriginQuote(default, None, 'no_caller_id', None, sentence)
    caller_digits = caller[1:]
    given = None
    if record is not None and record.confirmed and record.bought_here and _region(caller) == _region('+' + digits):
        given = min((row for row in group if row.origination_type == LOCAL),
                    key=lambda row: row.per_minute_micros, default=None)
    if given is None:
        listed = [(row.origin_match(caller_digits), row) for row in group
                  if row.origination_type in (EEA, NON_SURCHARGED)]
        listed = [(length, row) for length, row in listed if length]
        if listed:
            best = max(length for length, _ in listed)
            given = max((row for length, row in listed if length == best), key=lambda row: row.per_minute_micros)
    if given is None and _region(caller) == _region('+' + digits):
        # A local row needs the number bought on this account: without that confirmation it is offered, not used.
        given = min((row for row in group if row.origination_type == LOCAL), key=lambda row: row.per_minute_micros,
                    default=None)
    if given is None:
        sentence = (f'{caller} gets the rate for any other caller ID'
                    + (f' ({_money(default)}).' if default else ', which this deck does not list.'))
        return OriginQuote(default, caller, 'not_needed', None, sentence)
    confirmed = record is not None and record.confirmed and (given.origination_type != LOCAL or record.bought_here)
    if confirmed:
        sentence = f'{caller} is confirmed on this account, so the call gets the rate for {TYPE_TEXT[given.origination_type]} ({_money(given)}).'
        return OriginQuote(given, caller, 'confirmed', given, sentence)
    # Not confirmed: the dearer of the surcharged row and the row the caller ID would get, never the cheaper one.
    used = max((row for row in (default, given) if row is not None), key=lambda row: row.per_minute_micros)
    need = ('that it was bought on this account' if given.origination_type == LOCAL
            else 'that you may send from it on this account')
    if used is given:
        sentence = f'{caller} gets {_money(given)}, the rate for {TYPE_TEXT[given.origination_type]}.'
    elif default is None:
        used = None
        sentence = (f'The rate for {TYPE_TEXT[given.origination_type]} ({_money(given)}) needs your confirmation {need}, '
                    'and this deck lists no rate for any other caller ID, so the price is unknown.')
    else:
        sentence = (f'Priced at the rate for any other caller ID ({_money(default)}): {caller} would get '
                    f'{_money(given)}, the rate for {TYPE_TEXT[given.origination_type]}, once you confirm {need}.')
    return OriginQuote(used, caller, 'unconfirmed', given, sentence)


def class_terms(identities, destination, where, *, values, account_key, engine):
    """(RateTerms, OriginQuote) when a deck priced by caller ID covers this call; (None, None) otherwise.

    ``identities`` are the card identities to read decks for, the account's own first. Premium-rate, toll-free and
    invalid numbers keep their own class's price, as ``origin_rates.rated_terms`` keeps them.
    """
    from .destinations import PREMIUM, TOLL_FREE, UNKNOWN
    if engine is None or getattr(where, 'kind', None) in (PREMIUM, TOLL_FREE, UNKNOWN):
        return None, None
    number = getattr(where, 'number', None) or destination
    rows = rows_for(engine, identities, number)
    if not rows:
        return None, None
    caller, _ = presented_caller_id(values, account_key, engine=engine)
    record = eligibility(engine, account_key, caller) if caller else None
    quote = price_origin(rows, number, caller, record)
    if quote is None or quote.row is None:
        return None, quote
    row = quote.row
    label = f'{row.description or row.route} ({TYPE_LABEL[row.origination_type].lower()})'
    prefixes = (f'+{row.destination_prefix}',) if len(row.destination_prefix) <= 7 else ()
    try:
        return RateTerms(row.card(label), getattr(where, 'kind', 'local'), prefixes), quote
    except InvalidRateCard:
        return None, quote


# -- what people read --------------------------------------------------------------------------------------------------

def row_view(row):
    from .costs import format_amount
    return {'destination_prefix': f'+{row.destination_prefix}', 'origination_type': row.origination_type,
            'origination_label': TYPE_LABEL[row.origination_type],
            'origin_prefixes': [f'+{prefix}' for prefix in row.origin_prefixes], 'currency': row.currency,
            'per_minute': format_amount(row.per_minute_micros),
            'billing_increment_seconds': row.billing_increment_seconds, 'minimum_seconds': row.minimum_seconds,
            'description': row.description}


def eligibility_view(record):
    if record is None:
        return None
    return {'account': record.account, 'caller_id': record.caller_id, 'state': record.state,
            'bought_here': record.bought_here, 'evidence': record.evidence, 'evidence_url': record.evidence_url,
            'recorded_by': record.recorded_by,
            'recorded_at': record.recorded_at.isoformat() if record.recorded_at else None}


def quote_view(quote):
    return {'eligibility': quote.eligibility, 'caller_id': quote.caller_id, 'sentence': quote.sentence,
            'row': row_view(quote.row) if quote.row is not None else None,
            'cheaper': row_view(quote.cheaper) if quote.cheaper is not None else None,
            'origin': quote.origin}
