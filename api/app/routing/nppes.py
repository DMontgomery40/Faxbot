"""The public NPI registry (NPPES) as evidence: toll-free suggestions, your own record, and a check before a first fax.

The CMS NPPES registry API (``https://npiregistry.cms.hhs.gov/api/?version=2.1``,
documented at https://npiregistry.cms.hhs.gov/api-page, read 2026-10-08)
returns each provider's addresses, and an address carries ``telephone_number``
and ``fax_number`` when the provider published them; many fax numbers are
toll-free (an 866 number in a sample of 20 Denver organisations, read
2026-10-07). The API searches by NPI, name, place or taxonomy, never by phone
or fax number, and answers a bad query with an ``Errors`` list instead of
results. Records also list ``practiceLocations`` (other places the provider
works) and ``endpoints`` (Direct and FHIR addresses), which ``records_from``
keeps for other readers.

Everything here is evidence for a person, never a decision or a block:

- toll-free suggestions (``suggested_tollfree``): Faxbot never dials a number
  found here; a person records the recipient's approval (``routing.tollfree``),
  and only an approval changes the number a fax dials (``routing.alternates``);
- your own record (M18 b): the numbers your own NPIs list, so advice about a
  quiet number can say "still printed on your NPI record: keep it". A number
  missing from the record is not safe to give up for that reason alone: it may
  still be on letterhead, forms or a website;
- before the first fax to a number (M18 c, ``recipient_check``): whether the
  registry lists the number for the provider named on the fax. Because the API
  cannot search by number, two kinds of evidence exist. Records Faxbot has
  already read (stored in ``nppes_reads`` and ``nppes_numbers``) can say "this
  number is listed for another provider"; a lookup of the named provider can
  say "the registry lists a different fax number for them". Both are warnings
  shown to the sender; neither ever stops a fax. Registry errors leave the
  number "not checked", never "not listed".

Self-reported registry data can be stale, so every answer carries the date it
was read. Nothing here is called while a fax is sent.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import logging
import re
from uuid import uuid4

import sqlalchemy as sa

from .dialing import is_toll_free
from .numbers import InvalidNumber, normalize_number


API_URL = 'https://npiregistry.cms.hhs.gov/api/'
DOCS_URL = 'https://npiregistry.cms.hhs.gov/api-page'
PURPOSES = {'LOCATION': 'practice location', 'MAILING': 'mailing address'}

def _fetch(params, *, timeout=10.0):
    import httpx
    response = httpx.get(API_URL, params={'version': '2.1', **params}, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _name(basic):
    basic = basic if isinstance(basic, dict) else {}
    if basic.get('organization_name'):
        return str(basic['organization_name'])[:200]
    parts = [basic.get('first_name'), basic.get('last_name')]
    return ' '.join(str(part) for part in parts if part)[:200] or None


def _address(entry):
    parts = [entry.get('address_1'), entry.get('city'), entry.get('state')]
    return ', '.join(str(part).strip() for part in parts if part)[:300] or None


def suggestions_from(document, *, read_on=None):
    """Every toll-free fax number in an NPPES API response, one suggestion per address."""
    read_on = read_on or datetime.utcnow()
    results = document.get('results') if isinstance(document, dict) else None
    found = []
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict):
            continue
        npi = str(result.get('number') or '')
        if re.fullmatch(r'[0-9]{10}', npi) is None:
            continue
        name = _name(result.get('basic'))
        for entry in result.get('addresses') or []:
            if not isinstance(entry, dict) or not entry.get('fax_number'):
                continue
            try:
                fax = normalize_number(str(entry['fax_number']), country='US')
            except InvalidNumber:
                continue
            if not is_toll_free(fax):
                continue
            purpose = PURPOSES.get(str(entry.get('address_purpose') or '').upper(), 'address')
            found.append({
                'source': 'NPPES', 'read_at': read_on,
                'npi': npi, 'name': name, 'address_purpose': purpose, 'address': _address(entry),
                'fax_number': fax,
                'evidence': (f'NPPES record NPI {npi}, {purpose}, read {read_on:%B} {read_on.day}, {read_on.year}'),
                'source_url': f'{API_URL}?version=2.1&number={npi}'})
    return found


def suggested_tollfree(npi=None, *, name=None, city=None, state=None, fetch=None, now=None):
    """Toll-free fax numbers a provider published in NPPES, as suggestions a person may approve.

    Look up by ``npi`` (ten digits), or by organisation ``name`` with ``city``
    or ``state``. Returns a list (empty when nothing toll-free is published).
    Raises ``ValueError`` for an incomplete query; network errors pass to the caller.
    """
    if npi is not None:
        npi = str(npi).strip()
        if re.fullmatch(r'[0-9]{10}', npi) is None:
            raise ValueError('Enter the ten-digit NPI.')
        params = {'number': npi}
    elif name and (city or state):
        params = {'organization_name': str(name).strip()[:100], 'limit': 20}
        if city:
            params['city'] = str(city).strip()[:60]
        if state:
            params['state'] = str(state).strip()[:2].upper()
    else:
        raise ValueError('Enter an NPI, or a name with a city or state.')
    return suggestions_from((fetch or _fetch)(params), read_on=now)


# -- reading records ------------------------------------------------------------------------------------------------

log = logging.getLogger(__name__)
# A check before a first fax waits at most this long for the registry; the sender's form must not hang.
CHECK_TIMEOUT = 6.0
# A lookup of a named provider is reused for a day before the registry is asked again.
REUSE_LOOKUP = timedelta(days=1)
SEARCH_LIMIT = 50
_ADDRESS_PURPOSES = {'LOCATION': 'location', 'MAILING': 'mailing'}
_PURPOSE_WORDS = {'location': 'practice location', 'mailing': 'mailing address', 'practice': 'other practice location'}


class RegistryError(Exception):
    """The registry could not be read, or refused the question: the answer is unknown, never "not listed"."""


class NppesInputError(ValueError):
    """An NPI or name Faxbot cannot use; the message is one plain sentence."""


@dataclass(frozen=True)
class Address:
    purpose: str            # 'location', 'mailing' or 'practice' (a secondary practice location)
    address: str | None     # '1 Example Way, DENVER, CO'
    state: str | None
    phone: str | None       # E.164, when the registry lists one Faxbot can read
    fax: str | None


@dataclass(frozen=True)
class Record:
    """One provider's registry record: its NPI, name, addresses with their numbers, and endpoints."""
    npi: str
    name: str | None
    enumeration_type: str | None    # 'NPI-1' a person, 'NPI-2' an organization
    addresses: tuple = ()
    endpoints: tuple = ()           # ({'type', 'type_description', 'endpoint', 'use', 'content_type', ...}, ...)
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    def numbers(self):
        """[(number, 'fax' or 'phone', Address)] for every number the record lists, each pair once."""
        found, seen = [], set()
        for address in self.addresses:
            for kind, number in (('fax', address.fax), ('phone', address.phone)):
                if number and (number, kind, address.purpose) not in seen:
                    seen.add((number, kind, address.purpose))
                    found.append((number, kind, address))
        return found


def valid_npi(npi):
    """True for ten digits whose last is the Luhn check digit over 80840 and the first nine (the CMS NPI rule)."""
    text = str(npi or '')
    if re.fullmatch(r'[0-9]{10}', text) is None:
        return False
    digits = [int(char) for char in '80840' + text[:9]]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (10 - total % 10) % 10 == int(text[9])


def _us_number(value):
    if not value:
        return None
    try:
        return normalize_number(str(value), country='US')
    except InvalidNumber:
        return None


def _addresses(result):
    found = []
    for purpose_key, entries in (('addresses', result.get('addresses')), ('practiceLocations',
                                                                          result.get('practiceLocations'))):
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            purpose = ('practice' if purpose_key == 'practiceLocations'
                       else _ADDRESS_PURPOSES.get(str(entry.get('address_purpose') or '').upper(), 'location'))
            state = str(entry.get('state') or '').strip().upper()[:2] or None
            found.append(Address(purpose, _address(entry), state, _us_number(entry.get('telephone_number')),
                                 _us_number(entry.get('fax_number'))))
    return tuple(found)


def _endpoints(result):
    """Direct and FHIR addresses as the registry lists them, under plain keys."""
    found = []
    for entry in result.get('endpoints') or () if isinstance(result.get('endpoints'), list) else ():
        if not isinstance(entry, dict) or not entry.get('endpoint'):
            continue
        found.append({'type': str(entry.get('endpointType') or '')[:40] or None,
                      'type_description': str(entry.get('endpointTypeDescription') or '')[:200] or None,
                      'endpoint': str(entry['endpoint'])[:500],
                      'use': str(entry.get('useDescription') or entry.get('use') or '')[:200] or None,
                      'content_type': str(entry.get('contentTypeDescription') or entry.get('contentType') or '')[:200]
                      or None,
                      'affiliation': str(entry.get('affiliationName') or '')[:200] or None})
    return tuple(found)


def records_from(document):
    """Every record in an NPPES API response; RegistryError when the response is an error or not a response."""
    if not isinstance(document, dict):
        raise RegistryError('The registry answered with something Faxbot cannot read.')
    if document.get('Errors'):
        errors = document['Errors'] if isinstance(document['Errors'], list) else [document['Errors']]
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        raise RegistryError(str(first.get('description') or 'The registry refused the question.')[:200])
    results = document.get('results')
    if results is None:
        return []
    if not isinstance(results, list):
        raise RegistryError('The registry answered with something Faxbot cannot read.')
    found = []
    for result in results:
        if not isinstance(result, dict):
            continue
        npi = str(result.get('number') or '')
        if re.fullmatch(r'[0-9]{10}', npi) is None:
            continue
        found.append(Record(npi, _name(result.get('basic')), str(result.get('enumeration_type') or '')[:8] or None,
                            _addresses(result), _endpoints(result), result))
    return found


def _ask(params, fetch, timeout):
    """One registry question; RegistryError for anything but an answer. Only documented failures are caught."""
    import httpx
    try:
        document = fetch(params) if fetch is not None else _fetch(params, timeout=timeout)
    except (httpx.HTTPError, ValueError) as error:
        # httpx.HTTPError: no connection, a timeout or an error status; ValueError: a body that is not JSON.
        log.warning('NPPES registry read failed: %s', type(error).__name__)
        raise RegistryError('Faxbot could not reach NPPES.') from error
    return records_from(document)


def lookup(npi, *, fetch=None, timeout=10.0):
    """The record for one NPI, or None when the registry has none."""
    npi = str(npi or '').strip()
    if not valid_npi(npi):
        raise NppesInputError('Enter the ten-digit NPI exactly as it is on the NPI record.')
    found = _ask({'number': npi}, fetch, timeout)
    return next((record for record in found if record.npi == npi), None)


def _query_name(name):
    text = ' '.join(re.sub(r"[^A-Za-z0-9&'.,\- ]", ' ', str(name or '')).split())[:100]
    if len(re.sub(r'[^A-Za-z0-9]', '', text)) < 2:
        raise NppesInputError('Enter at least two letters of the name.')
    return text


def search(name, *, state=None, fetch=None, timeout=10.0):
    """Records named ``name`` (an organization, else a person), in ``state`` when given; at most two questions."""
    text = _query_name(name)
    place = {'state': state} if state else {}
    found = _ask({'organization_name': text + '*', 'enumeration_type': 'NPI-2', 'limit': SEARCH_LIMIT, **place},
                 fetch, timeout)
    words = [word for word in _words(text) if len(word) > 1]
    if not found and len(words) >= 2:
        found = _ask({'first_name': words[0], 'last_name': words[-1], 'enumeration_type': 'NPI-1',
                      'limit': SEARCH_LIMIT, **place}, fetch, timeout)
    return found


# -- names ----------------------------------------------------------------------------------------------------------

# Words that say what kind of business or title a name has, not whose it is.
_IGNORED = frozenset({'THE', 'INC', 'INCORPORATED', 'LLC', 'LLP', 'LTD', 'PC', 'PLLC', 'PA', 'CORP', 'CORPORATION',
                      'CO', 'COMPANY', 'DR', 'MD', 'DO', 'NP', 'RN', 'DDS', 'DMD', 'OD', 'DPM', 'PHD', 'MR', 'MRS',
                      'MS', 'JR', 'SR', 'II', 'III', 'OF', 'AND'})


def _words(name):
    return [word for word in re.findall(r'[A-Z0-9]+', str(name or '').upper().replace('&', ' AND '))
            if word not in _IGNORED]


def same_provider(first, second):
    """True when one name's words are all in the other's: "Denver Health" and "DENVER HEALTH AND HOSPITAL AUTHORITY"."""
    one, two = set(_words(first)), set(_words(second))
    return bool(one and two) and (one <= two or two <= one)


# -- where a US number is ---------------------------------------------------------------------------------------------

US_STATES = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA', 'Colorado': 'CO',
    'Connecticut': 'CT', 'Delaware': 'DE', 'District of Columbia': 'DC', 'Washington D.C.': 'DC', 'Florida': 'FL',
    'Georgia': 'GA', 'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA',
    'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD', 'Massachusetts': 'MA',
    'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS', 'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE',
    'Nevada': 'NV', 'New Hampshire': 'NH', 'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY',
    'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK', 'Oregon': 'OR',
    'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC', 'South Dakota': 'SD', 'Tennessee': 'TN',
    'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV',
    'Wisconsin': 'WI', 'Wyoming': 'WY', 'Puerto Rico': 'PR', 'Guam': 'GU', 'US Virgin Islands': 'VI',
    'American Samoa': 'AS', 'Northern Mariana Islands': 'MP'}
STATE_NAMES = {code: name for name, code in US_STATES.items() if name != 'Washington D.C.'}


def us_state(number):
    """The two-letter US state of a geographic US number's area code ('CO'), or None (toll-free, not US, unknown).

    From libphonenumber's geocoding data, which names either the state ("Colorado") or a city and state
    ("New York, NY") for each US area code.
    """
    import phonenumbers
    from phonenumbers import geocoder
    try:
        parsed = phonenumbers.parse(str(number or ''), None)
    except phonenumbers.NumberParseException:
        return None
    if phonenumbers.region_code_for_number(parsed) not in ('US', 'PR', 'GU', 'VI', 'AS', 'MP'):
        return None
    where = geocoder.description_for_number(parsed, 'en') or ''
    match = re.search(r',\s*([A-Z]{2})$', where)
    if match and match.group(1) in STATE_NAMES:
        return match.group(1)
    return US_STATES.get(where.strip())


# -- what Faxbot has read ----------------------------------------------------------------------------------------------

def _tables(engine):
    from .database import reflect
    return reflect(engine, ('organization_npis', 'nppes_reads', 'nppes_numbers'))


def _day(moment):
    return f'{moment:%B} {moment.day}, {moment.year}' if moment else None


def shown(number):
    """+13035550101 as +1 303-555-0101."""
    import phonenumbers
    try:
        return phonenumbers.format_number(phonenumbers.parse(number, None), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return number


class NppesStore:
    """Your NPIs, and every registry record Faxbot read with the numbers it lists. Rows are never changed."""

    def __init__(self, engine):
        self.engine = engine
        tables = _tables(engine)
        self.npis, self.reads, self.numbers = (tables[name] for name in ('organization_npis', 'nppes_reads',
                                                                           'nppes_numbers'))

    # Your NPIs --------------------------------------------------------------------------------------------
    def own(self):
        """Your NPIs in the order they were added: [{'npi', 'label', 'added_at', 'added_by_name'}]."""
        from .database import read_connection
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.npis).where(self.npis.c.removed_at.is_(None))
                                      .order_by(self.npis.c.added_at, self.npis.c.id)).mappings().all()
        return [{'npi': row['npi'], 'label': row['label'] or None, 'added_at': row['added_at'],
                 'added_by_name': row['added_by_name']} for row in rows]

    def add(self, npi, label='', *, by_name=None, now=None):
        from .database import write_transaction
        npi = str(npi or '').strip()
        if not valid_npi(npi):
            raise NppesInputError('Enter the ten-digit NPI exactly as it is on the NPI record.')
        label = ' '.join(str(label or '').split())[:200]
        with write_transaction(self.engine) as connection:
            listed = connection.execute(sa.select(self.npis.c.id).where(
                self.npis.c.npi == npi, self.npis.c.removed_at.is_(None))).first()
            if listed is not None:
                raise NppesInputError(f'NPI {npi} is already one of yours.')
            connection.execute(self.npis.insert().values(
                id=uuid4().hex, npi=npi, label=label, added_at=now or datetime.utcnow().replace(microsecond=0),
                added_by_name=(by_name or None) and str(by_name)[:200], removed_at=None, removed_by_name=None))

    def remove(self, npi, *, by_name=None, now=None):
        from .database import write_transaction
        with write_transaction(self.engine) as connection:
            done = connection.execute(self.npis.update().where(
                self.npis.c.npi == str(npi or '').strip(), self.npis.c.removed_at.is_(None)).values(
                removed_at=now or datetime.utcnow().replace(microsecond=0),
                removed_by_name=(by_name or None) and str(by_name)[:200])).rowcount
        if not done:
            raise NppesInputError('That NPI is not one of yours.')

    # Reads ---------------------------------------------------------------------------------------------------
    def record(self, records, purpose, *, now=None):
        """Store each record read and the numbers it lists; returns the read time."""
        from .database import write_transaction
        now = now or datetime.utcnow().replace(microsecond=0)
        with write_transaction(self.engine) as connection:
            for record in records:
                read_id = uuid4().hex
                connection.execute(self.reads.insert().values(
                    id=read_id, npi=record.npi, name=record.name, enumeration_type=record.enumeration_type,
                    purpose=purpose, read_at=now, record=json.dumps(record.raw, sort_keys=True, default=str)[:200_000]))
                for number, kind, address in record.numbers():
                    connection.execute(self.numbers.insert().values(
                        id=uuid4().hex, read_id=read_id, npi=record.npi, number=number, kind=kind,
                        address_purpose=address.purpose, address=address.address, state=address.state))
        return now

    def _latest_read_ids(self, connection, npis=None):
        """{npi: (read id, read_at, name)} of the newest read of each NPI (of ``npis`` when given)."""
        newest = sa.select(self.reads.c.npi, sa.func.max(self.reads.c.read_at).label('read_at')).group_by(
            self.reads.c.npi)
        if npis is not None:
            newest = newest.where(self.reads.c.npi.in_(list(npis)))
        newest = newest.subquery()
        rows = connection.execute(sa.select(self.reads.c.id, self.reads.c.npi, self.reads.c.read_at,
                                            self.reads.c.name).join(
            newest, sa.and_(newest.c.npi == self.reads.c.npi, newest.c.read_at == self.reads.c.read_at))
            .order_by(self.reads.c.id)).all()
        found = {}
        for row in rows:
            found.setdefault(row.npi, (row.id, row.read_at, row.name))
        return found

    def listings(self, number):
        """Who the newest read of each NPI lists ``number`` for: [{'npi', 'name', 'kind', 'address_purpose', ...}]."""
        from .database import read_connection
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.numbers).where(self.numbers.c.number == number)).mappings().all()
            if not rows:
                return []
            latest = self._latest_read_ids(connection, {row['npi'] for row in rows})
        found = []
        for row in rows:
            newest = latest.get(row['npi'])
            if newest is None or newest[0] != row['read_id']:
                continue  # an older read: the newest read of that NPI no longer lists it
            found.append({'npi': row['npi'], 'name': newest[2], 'kind': row['kind'],
                          'address_purpose': row['address_purpose'], 'address': row['address'],
                          'state': row['state'], 'read_at': newest[1]})
        found.sort(key=lambda item: (item['kind'] != 'fax', item['npi'], item['address_purpose']))
        return found

    def latest(self, npis):
        """{npi: {'name', 'read_at', 'numbers': [{'number', 'kind', 'address_purpose', 'address'}]}} (newest reads)."""
        from .database import read_connection
        with read_connection(self.engine) as connection:
            newest = self._latest_read_ids(connection, npis)
            ids = [value[0] for value in newest.values()]
            rows = connection.execute(sa.select(self.numbers).where(self.numbers.c.read_id.in_(ids))
                                      .order_by(self.numbers.c.kind, self.numbers.c.address_purpose,
                                                self.numbers.c.number)).mappings().all() if ids else []
        found = {npi: {'name': value[2], 'read_at': value[1], 'numbers': []} for npi, value in newest.items()}
        for row in rows:
            found[row['npi']]['numbers'].append({'number': row['number'], 'kind': row['kind'],
                                                 'address_purpose': row['address_purpose'],
                                                 'address': row['address']})
        return found

    def recent_named(self, name, since):
        """Records read for a check since ``since`` whose name is ``name``: [(npi, name, numbers)] (no registry call)."""
        from .database import read_connection
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.reads.c.npi, self.reads.c.name).where(
                self.reads.c.read_at >= since, self.reads.c.purpose == 'recipient').distinct().limit(500)).all()
        npis = {row.npi for row in rows if same_provider(row.name, name)}
        return self.latest(npis) if npis else {}

    def looked_up(self, name, since):
        """True when a check looked ``name`` up since ``since`` (whatever it found)."""
        from .database import read_connection
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.reads.c.name).where(
                self.reads.c.read_at >= since, self.reads.c.purpose == 'recipient').distinct().limit(500)).all()
        return any(same_provider(row.name, name) for row in rows)


# -- your own record (M18 b) -------------------------------------------------------------------------------------------

def refresh_own(engine, *, fetch=None, now=None):
    """Read each of your NPIs from the registry and keep what it lists. Returns {'read': [...], 'failed': [...]}."""
    store = NppesStore(engine)
    read, failed, missing = [], [], []
    for item in store.own():
        try:
            record = lookup(item['npi'], fetch=fetch)
        except RegistryError:
            failed.append(item['npi'])
            continue
        if record is None:
            missing.append(item['npi'])
            continue
        store.record([record], 'own', now=now)
        read.append(item['npi'])
    return {'read': read, 'failed': failed, 'missing': missing}


def own_record(engine):
    """Your NPIs, each with the numbers its newest read lists and when it was read; no registry call."""
    store = NppesStore(engine)
    own = store.own()
    latest = store.latest([item['npi'] for item in own])
    items = []
    for item in own:
        read = latest.get(item['npi'])
        numbers = [{**number, 'display': shown(number['number']),
                    'where': _PURPOSE_WORDS.get(number['address_purpose'], 'address')}
                   for number in (read or {}).get('numbers', [])]
        items.append({'npi': item['npi'], 'label': item['label'], 'name': (read or {}).get('name'),
                      'read_at': (read or {}).get('read_at'), 'numbers': numbers})
    if not own:
        sentence = ('Add your NPI so Faxbot can tell you when a number you might give up is still printed on your '
                    'NPI record.')
    elif any(item['read_at'] is None for item in items):
        sentence = 'Faxbot has not read your NPI record yet; choose Check NPPES now.'
    else:
        oldest = min(item['read_at'] for item in items)
        sentence = f'Faxbot last read your NPI record on {_day(oldest)}.'
    return {'npis': items, 'sentence': sentence, 'source_url': DOCS_URL}


def published_numbers(engine):
    """{number: evidence} for every number on your NPI record's newest reads (evidence: kind, where, NPI, read).

    None when you have no NPI, or Faxbot has read none of yours yet: then nothing is known either way.
    """
    record = own_record(engine)['npis']
    if not any(item['read_at'] for item in record):
        return None
    found = {}
    for item in record:
        for number in item['numbers']:
            found.setdefault(number['number'], {'npi': item['npi'], 'kind': number['kind'],
                                                'where': number['where'], 'read_at': item['read_at']})
    return found


def npi_evidence(published, number):
    """What your NPI record says about ``number``: {'state', 'sentence', 'read_at'}, or None when you set no NPI.

    ``published`` is ``published_numbers(engine)`` (empty when you have no NPI or it was never read).
    """
    if published is None:
        return None
    found = published.get(number)
    if found is None:
        return {'state': 'not_listed', 'read_at': None, 'sentence': None}
    kind = 'fax number' if found['kind'] == 'fax' else 'phone number'
    return {'state': 'listed', 'read_at': found['read_at'],
            'sentence': f"Still printed on your NPI record as your {found['where']} {kind}: keep it."}


# -- before the first fax to a number (M18 c) ---------------------------------------------------------------------------

def sent_before(engine, number):
    """True when Faxbot has accepted a fax to ``number`` before."""
    from .database import read_connection, reflect
    jobs = reflect(engine, ('fax_jobs',))['fax_jobs']
    with read_connection(engine) as connection:
        return connection.execute(sa.select(jobs.c.id).where(jobs.c.to_number == number).limit(1)).first() is not None


def _listing_view(item):
    return {'npi': item['npi'], 'name': item['name'], 'kind': item['kind'],
            'where': _PURPOSE_WORDS.get(item['address_purpose'], 'address'), 'address': item['address'],
            'read_on': _day(item['read_at'])}


def _result(number, state, sentence, *, warning=False, first_send=True, checked=True, listed=(), name=None):
    return {'number': number, 'first_send': first_send, 'checked': checked, 'state': state, 'warning': warning,
            'sentence': sentence, 'name': name, 'listed': [_listing_view(item) for item in listed],
            'source_url': DOCS_URL}


def _from_index(number, name, listings):
    """The answer from records already read, or None when they say nothing about ``number``."""
    faxes = [item for item in listings if item['kind'] == 'fax']
    phones = [item for item in listings if item['kind'] == 'phone']
    if name:
        named = [item for item in faxes if same_provider(item['name'], name)]
        if named:
            return _result(number, 'listed_for_named',
                           f"NPPES lists {shown(number)} as the fax number of {named[0]['name']}.", listed=named,
                           name=name)
        if faxes:
            return _result(number, 'listed_for_other',
                           f"This number is listed for {faxes[0]['name']} in NPPES, not {name}.", warning=True,
                           listed=faxes, name=name)
    elif faxes:
        return _result(number, 'listed', f"NPPES lists {shown(number)} as the fax number of {faxes[0]['name']}.",
                       listed=faxes)
    if phones:
        return _result(number, 'phone_number',
                       f"NPPES lists this number as the telephone number of {phones[0]['name']}, not a fax number.",
                       warning=True, listed=phones, name=name)
    return None


def recipient_check(engine, values, number, *, name=None, fetch=None, now=None, timeout=CHECK_TIMEOUT):
    """Before the first fax to ``number``: what NPPES says about it, for the provider ``name`` when given.

    Never a block: the answer is a sentence and a flag for a warning. Numbers outside the US, numbers Faxbot has
    faxed before, and toll-free numbers with no name are not looked up. A registry error is "not checked".
    """
    now = now or datetime.utcnow().replace(microsecond=0)
    name = ' '.join(str(name or '').split())[:200] or None
    if sent_before(engine, number):
        return _result(number, 'sent_before', None, first_send=False, checked=False, name=name)
    import phonenumbers
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(number, None))
    except phonenumbers.NumberParseException:
        region = None
    if region != 'US':
        return _result(number, 'outside_us', None, checked=False, name=name)
    store = NppesStore(engine)
    answer = _from_index(number, name, store.listings(number))
    if answer is not None:
        return answer
    if not name:
        return _result(number, 'no_name', None, checked=False)
    state = us_state(number)
    known = store.recent_named(name, now - REUSE_LOOKUP)
    if not known and not store.looked_up(name, now - REUSE_LOOKUP):
        try:
            found = search(name, state=state, fetch=fetch, timeout=timeout)
        except NppesInputError:
            return _result(number, 'no_name', None, checked=False, name=name)
        except RegistryError:
            return _result(number, 'not_checked', 'Faxbot could not reach NPPES, so this number was not checked.',
                           checked=False, name=name)
        if found:
            store.record(found, 'recipient', now=now)
        answer = _from_index(number, name, store.listings(number))
        if answer is not None:
            return answer
        known = {record.npi: {'name': record.name, 'read_at': now,
                              'numbers': [{'number': value, 'kind': kind, 'address_purpose': address.purpose,
                                           'address': address.address} for value, kind, address in record.numbers()]}
                 for record in found if same_provider(record.name, name)}
    named = [(npi, item) for npi, item in known.items() if same_provider(item['name'], name)]
    faxes = [(npi, item, entry) for npi, item in named for entry in item['numbers'] if entry['kind'] == 'fax']
    if faxes:
        # Prefer a fax number in the same state as the number being faxed.
        faxes.sort(key=lambda found: (us_state(found[2]['number']) != state, found[2]['address_purpose']))
        npi, item, entry = faxes[0]
        listed = [{'npi': npi, 'name': item['name'], 'kind': 'fax', 'address_purpose': entry['address_purpose'],
                   'address': entry['address'], 'state': None, 'read_at': item['read_at']}]
        return _result(number, 'named_lists_other',
                       f"NPPES lists {shown(entry['number'])} as {item['name']}'s fax number, not this one.",
                       warning=True, listed=listed, name=name)
    if named:
        return _result(number, 'named_no_fax',
                       f'NPPES lists {named[0][1]["name"]} with no fax number, so Faxbot could not check this one.',
                       name=name)
    where = f' in {STATE_NAMES[state]}' if state in STATE_NAMES else ''
    return _result(number, 'not_found', f'NPPES lists no provider named {name}{where}, so Faxbot could not check '
                                        'this number.', name=name)


def check_before_sending(engine, values, number, name=None):
    """The hook a send path runs before accepting a first fax: records already read only, never the registry.

    Returns ``recipient_check``'s answer when records Faxbot already read list ``number`` (for ``name`` or for
    someone else), else None, so it adds no wait to a send. A caller shows the sentence beside the fax; it never
    stops the fax.
    """
    import phonenumbers
    name = ' '.join(str(name or '').split())[:200] or None
    try:
        if phonenumbers.region_code_for_number(phonenumbers.parse(number, None)) != 'US':
            return None
    except phonenumbers.NumberParseException:
        return None
    if sent_before(engine, number):
        return None
    return _from_index(number, name, NppesStore(engine).listings(number))
