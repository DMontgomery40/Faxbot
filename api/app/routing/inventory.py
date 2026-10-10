"""Your fax lines, matched to carriers' discontinuance lists and to their contract end dates (N19).

An enterprise whose fax "just works" moves when someone else sets a date: a carrier discontinues the copper
service a line runs on, or the telecom contract that holds its price ends. Faxbot keeps your line inventory and
the carrier lists you import, matches each line to them, and puts every line with a date at the top of the
retirement plan (Delivery setup → Number moves), with the source and the date.

**The line inventory** is a CSV or Excel file with one line per row. Only the number is required. Columns
(names are matched ignoring case, spaces and underscores; the first listed spelling is the documented one):
``number``, ``service address``, ``city``, ``state``, ``postal code``, ``country``, ``carrier``,
``product`` (or ``usoc``), ``wire center`` (the CLLI code of the line's serving office), ``distribution area``,
``contract end``, ``use`` (fax, alarm, elevator, emergency or other), ``monthly price`` and ``currency``, and
``note``. A new import replaces the old one; the old rows are kept as history. Contract end dates become the
line's contract-end notice (``closures.line_notices``, kind ``contract_end``), next to any carrier letter.

**AT&T's Discontinued TDM Service Areas workbook** (``ATT_WORKBOOK``, downloaded and read 2026-10-10: 100,512
locations in 21 states, 3,994 wire centers, effective dates from 5 November 2016 to 16 March 2026) is read as AT&T
publishes it: the "Location" sheet's columns Region, State, CITY / TOWNSHIP, Development Name, Wire Center,
Distribution Area, Effective Date (an Excel day number) and Ref Table. Table A says AT&T discontinued every service
under its federal tariffs and its state general exchange tariffs (where business lines are) in those areas; rows
with Ref Table G cover a whole wire center (Distribution Area ALL) except the services Table G lists. The workbook
has no street addresses: it is keyed by wire center and distribution area, so a line matches only by its wire
center (and its distribution area when AT&T lists only parts of the wire center). Faxbot never guesses a match from
a city, a street or a map point. AT&T's customer service record for a line names its wire center.

**Any other carrier's list** (Lumen's and Verizon's notices, AT&T's grandfathering applications, which are PDF
exhibits listing wire centers) is imported as CSV with the columns ``wire center`` and ``effective date``, and
optionally ``carrier``, ``kind`` (discontinued or grandfathered), ``state``, ``city``, ``wire center name``,
``distribution area`` and ``place``. A newer import of the same carrier and kind replaces the older one.

Advice only: nothing here orders, ports or cancels a line.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from uuid import uuid4

import sqlalchemy as sa

from .workbook import WorkbookError, excel_date, normal, table


ATT = 'att'
ATT_WORKBOOK = ('https://clec.att.com/clec_documents/unrestr/clec/common/'
                'PrimeAccess_Model-Discontinued_Service_Areas.xlsx')
ATT_MAP = 'http://cpr.att.com/'
ATT_READ_ON = '2026-10-10'
ATT_LABEL = "AT&T's Discontinued TDM Service Areas workbook"
# AT&T and the Bell companies its workbook's tariffs belong to.
ATT_NAMES = ('at&t', 'att', 'at and t', 'at & t', 'bellsouth', 'bell south', 'ameritech', 'southwestern bell', 'sbc',
             'pacific bell', 'nevada bell', 'illinois bell', 'indiana bell', 'michigan bell', 'ohio bell',
             'wisconsin bell')
KINDS = ('discontinued', 'grandfathered')
USES = ('fax', 'alarm', 'elevator', 'emergency', 'other', 'unknown')
LIFE_SAFETY = ('alarm', 'elevator', 'emergency')
USE_LABELS = {'fax': 'Fax', 'alarm': 'Alarm', 'elevator': 'Elevator', 'emergency': 'Emergency phone',
              'other': 'Other', 'unknown': 'Not known'}
_USE_WORDS = {
    'fax': ('fax', 'facsimile', 'fax machine', 'fax line', 'efax'),
    'alarm': ('alarm', 'fire alarm', 'burglar alarm', 'security', 'security alarm', 'fire panel', 'facp'),
    'elevator': ('elevator', 'lift', 'elevator phone'),
    'emergency': ('emergency', 'emergency phone', 'call box', 'blue phone', '911', 'e911', 'red phone'),
    'other': ('other', 'voice', 'phone', 'modem', 'pos', 'credit card', 'data', 'dial-up', 'postage'),
}
WARN_DAYS = 365
MAX_LINES = 50_000
MAX_SKIPPED_SHOWN = 20
CLLI = re.compile(r'[A-Z0-9]{8}')
INVENTORY_COLUMNS = {
    'number': ('number', 'phone number', 'telephone number', 'fax number', 'line', 'line number', 'tn', 'phone',
               'btn', 'wtn'),
    'street': ('service address', 'address', 'street', 'street address', 'service street'),
    'city': ('city', 'town', 'service city'),
    'region': ('state', 'province', 'region', 'service state'),
    'postal_code': ('postal code', 'zip', 'zip code', 'postcode'),
    'country': ('country',),
    'carrier': ('carrier', 'provider', 'telephone company', 'telco'),
    'product': ('product', 'usoc', 'product/usoc', 'product or usoc', 'service'),
    'wire_center': ('wire center', 'wire center clli', 'clli', 'serving wire center', 'wire centre'),
    'distribution_area': ('distribution area', 'da'),
    'contract_end': ('contract end', 'contract end date', 'term end', 'contract ends', 'term end date'),
    'use': ('use', 'line use', 'purpose', 'used for'),
    'monthly': ('monthly price', 'monthly', 'monthly cost', 'monthly charge', 'price a month', 'mrc'),
    'currency': ('currency',),
    'note': ('note', 'notes'),
}
LIST_COLUMNS = {
    'carrier': ('carrier',),
    'kind': ('kind', 'status', 'list'),
    'region': ('state', 'province'),
    'city': ('city', 'city / township', 'city/township', 'township'),
    'place': ('place', 'development name', 'location', 'development'),
    'wire_center': ('wire center', 'wire center clli', 'clli', 'wire centre'),
    'wire_center_name': ('wire center name', 'wire centre name'),
    'distribution_area': ('distribution area', 'da'),
    'effective': ('effective date', 'effective on', 'date', 'effective date (on or after)'),
    'ref_table': ('ref table',),
}
INVENTORY_HELP = ('One line per row; only the number is required. Columns: number, service address, city, state, '
                  'postal code, country, carrier, product (or USOC), wire center, distribution area, contract end, '
                  'use (fax, alarm, elevator, emergency or other), monthly price, currency, note.')
LIST_HELP = ('For a carrier list other than AT&T\'s workbook, a CSV with the columns wire center and effective date, '
             'and optionally carrier, kind (discontinued or grandfathered), state, city, wire center name, '
             'distribution area and place.')
KEYED = ("AT&T's workbook lists wire centers and the distribution areas inside them, not street addresses, so Faxbot "
         "matches a line only by its wire center (the 8-character CLLI code on AT&T's customer service record for "
         'the line) and, where AT&T lists only parts of a wire center, its distribution area. It never guesses a '
         'match from a city or a street.')
ADVICE_ONLY = 'Faxbot only advises: it never orders, ports or cancels a line.'


class InventoryError(ValueError):
    """A file Faxbot cannot import; the message is one plain sentence."""


@dataclass(frozen=True)
class Line:
    number: str
    street: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None
    carrier: str | None = None
    product: str | None = None
    wire_center: str | None = None
    distribution_area: str | None = None
    contract_end: date | None = None
    use: str = 'unknown'
    monthly_amount: str | None = None
    currency: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Area:
    wire_center: str
    effective_on: date | None
    region: str | None = None
    city: str | None = None
    place: str | None = None
    wire_center_name: str | None = None
    distribution_area: str | None = None
    ref_table: str | None = None
    carrier: str | None = None
    kind: str | None = None


@dataclass
class Parsed:
    items: list
    skipped: list = field(default_factory=list)   # one plain sentence per row Faxbot could not read (first 20)
    skipped_count: int = 0
    layout: str | None = None                     # 'att_workbook' or 'csv' for a carrier list
    carrier: str | None = None
    kind: str | None = None

    def skip(self, sentence):
        self.skipped_count += 1
        if len(self.skipped) < MAX_SKIPPED_SHOWN:
            self.skipped.append(sentence)


# -- small readers -------------------------------------------------------------------------------------------------------

def carrier_key(name):
    """'att' for AT&T and its Bell companies, else a short key of the name; None for no name."""
    text = ' '.join(str(name or '').lower().split())
    if not text:
        return None
    if any(text == item or text.startswith(item + ' ') or f' {item} ' in f' {text} ' for item in ATT_NAMES):
        return ATT
    return re.sub(r'[^a-z0-9]+', '-', text).strip('-')[:40] or None


def carrier_label(key):
    return 'AT&T' if key == ATT else (key or '').replace('-', ' ').title()


def _text(value, limit):
    text = ' '.join(str(value).split()) if value is not None else ''
    return text[:limit] or None


def parse_day(value, *, order='mdy', date1904=False):
    """A date from a cell: ISO (2026-11-04), an Excel day number, or slashes, dashes or dots in ``order``.

    A first part above 12 is a day and a second part above 12 is a day whatever ``order`` says; otherwise ``order``
    ('mdy', as US files write dates, or 'dmy') decides. Raises ValueError when the text is not a date.
    """
    text = str(value or '').strip()
    if not text:
        return None
    iso = re.fullmatch(r'([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?:[T ][0-9:.]+)?', text)
    if iso:
        return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    if re.fullmatch(r'[0-9]{4,6}(?:\.[0-9]+)?', text):
        found = excel_date(text, date1904=date1904)
        if found is not None:
            return found
    parts = re.fullmatch(r'([0-9]{1,2})[/.-]([0-9]{1,2})[/.-]([0-9]{2}|[0-9]{4})', text)
    if parts is None:
        raise ValueError(text)
    first, second, year = int(parts.group(1)), int(parts.group(2)), int(parts.group(3))
    year = year + 2000 if year < 100 else year
    if first > 12 or (second <= 12 and order == 'dmy'):
        return date(year, second, first)
    return date(year, first, second)


def _wire_center(value):
    """A wire center's CLLI code as Faxbot compares it, or None.

    The code is 8 characters: a 4-character place (padded with spaces, as in AT&T's "ADA MIMN"), the state and the
    building. Faxbot drops the spaces, so "ADA MIMN" and "ADAMIMN" are the same wire center, and keeps the first 8
    characters of a longer switch code (ALGNILAQDS0 is ALGNILAQ).
    """
    text = str(value or '').strip().upper()
    compact = re.sub(r'\s+', '', text[:8] if ' ' in text else text)
    if len(compact) >= 8 and ' ' not in text:
        compact = compact[:8]
    return compact if 6 <= len(compact) <= 8 and CLLI.fullmatch(compact.ljust(8, '0')) else None


def _area_code(value):
    text = re.sub(r'\s+', '', str(value or '')).upper()
    return text[:16] or None


def _use(value):
    text = normal(value)
    if not text:
        return 'unknown'
    for use, words in _USE_WORDS.items():
        if text in words:
            return use
    return 'unknown'


def _money(amount, currency):
    """(decimal text, currency) for a monthly price as entered, or raises ValueError; (None, None) when blank."""
    text = str(amount or '').strip()
    if not text:
        return None, None
    symbol = {'$': 'USD', '€': 'EUR', '£': 'GBP'}.get(text[:1]) or {'$': 'USD', '€': 'EUR'}.get(text[-1:])
    cleaned = text.strip('$€£ ').replace(',', '')
    currency = (str(currency or '').strip().upper() or symbol or '')
    if re.fullmatch(r'[0-9]{1,9}(?:\.[0-9]{1,6})?', cleaned) is None:
        raise ValueError('price')
    if re.fullmatch(r'[A-Z]{3}', currency) is None:
        raise ValueError('currency')
    return cleaned, currency


def _columns(header, names):
    at = {}
    for key, options in names.items():
        found = next((header.index(option) for option in options if option in header), None)
        if found is not None:
            at[key] = found
    return at


def _cell(row, at, key):
    index = at.get(key)
    if index is None or index >= len(row):
        return None
    value = row[index]
    return value if value is None or str(value).strip() else None


# -- the line inventory ------------------------------------------------------------------------------------------------

def parse_inventory(data, *, default_country='US', date_order='mdy'):
    """``Parsed`` of ``Line`` from a CSV or Excel inventory; rows Faxbot cannot read are counted with a reason."""
    from .numbers import AmbiguousNumber, InvalidNumber, normalize_number
    if date_order not in ('mdy', 'dmy'):
        raise InventoryError('Choose the date order mdy (11/4/2026 is 4 November) or dmy (4/11/2026 is 4 November).')
    try:
        header, rows, date1904, first = table(data, [INVENTORY_COLUMNS['number']])
    except WorkbookError as error:
        raise InventoryError(f'{error} The inventory needs a column named number.') from None
    at = _columns(header, INVENTORY_COLUMNS)
    parsed, seen = Parsed([]), set()
    for position, row in enumerate(rows, start=first):
        if not any(cell is not None and str(cell).strip() for cell in row):
            continue
        raw = _cell(row, at, 'number')
        if raw is None:
            parsed.skip(f'Row {position} has no number.')
            continue
        country = (_text(_cell(row, at, 'country'), 2) or default_country or 'US').upper()
        try:
            number = normalize_number(str(raw).strip(), country=country if len(country) == 2 else 'US')
        except (AmbiguousNumber, InvalidNumber, ValueError):
            parsed.skip(f'Row {position}: {_text(raw, 40)} is not a phone number Faxbot can read.')
            continue
        if number in seen:
            parsed.skip(f'Row {position}: {number} is listed twice; the first row counts.')
            continue
        try:
            contract_end = parse_day(_cell(row, at, 'contract_end'), order=date_order, date1904=date1904)
        except ValueError:
            parsed.skip(f'Row {position}: the contract end {_text(_cell(row, at, "contract_end"), 20)} is not a date.')
            continue
        try:
            amount, currency = _money(_cell(row, at, 'monthly'), _cell(row, at, 'currency'))
        except ValueError as error:
            parsed.skip(f'Row {position}: the monthly price needs a currency, such as USD.' if str(error) == 'currency'
                        else f'Row {position}: the monthly price is not a number.')
            continue
        seen.add(number)
        wire = _cell(row, at, 'wire_center')
        parsed.items.append(Line(
            number=number, street=_text(_cell(row, at, 'street'), 200), city=_text(_cell(row, at, 'city'), 100),
            region=_text(_cell(row, at, 'region'), 40), postal_code=_text(_cell(row, at, 'postal_code'), 20),
            country=country if len(country) == 2 else None, carrier=_text(_cell(row, at, 'carrier'), 100),
            product=_text(_cell(row, at, 'product'), 100), wire_center=_wire_center(wire) or _text(wire, 16),
            distribution_area=_area_code(_cell(row, at, 'distribution_area')), contract_end=contract_end,
            use=_use(_cell(row, at, 'use')), monthly_amount=amount, currency=currency,
            note=_text(_cell(row, at, 'note'), 2000)))
        if len(parsed.items) > MAX_LINES:
            raise InventoryError(f'The inventory has more than {MAX_LINES:,} lines.')
    if not parsed.items:
        raise InventoryError('The inventory has no line Faxbot can read.')
    return parsed


def _tables(engine):
    from .database import reflect
    return reflect(engine, ('line_inventory', 'carrier_service_areas'))


def _datetime(value):
    return datetime(value.year, value.month, value.day) if value else None


def _day(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.date() if isinstance(value, datetime) else value


def import_inventory(engine, lines, *, file_name=None, actor=None, now=None):
    """Make ``lines`` the current inventory; earlier rows are superseded, never changed. Contract end dates become
    each line's contract-end notice, and a line whose contract end went away has its notice withdrawn."""
    from . import closures
    from .database import utcnow, write_transaction
    now = (now or utcnow()).replace(microsecond=0)
    actor = actor or {}
    inventory = _tables(engine)['line_inventory']
    import_id = uuid4().hex
    label = f'Line inventory ({file_name})' if file_name else 'Line inventory'
    with write_transaction(engine) as connection:
        connection.execute(inventory.update().where(inventory.c.superseded_at.is_(None)).values(superseded_at=now))
        batch = []
        for line in lines:
            batch.append({'id': uuid4().hex, 'import_id': import_id, 'number': line.number, 'street': line.street,
                          'city': line.city, 'region': line.region, 'postal_code': line.postal_code,
                          'country': line.country, 'carrier': line.carrier, 'product': line.product,
                          'wire_center': line.wire_center, 'distribution_area': line.distribution_area,
                          'contract_end': _datetime(line.contract_end), 'line_use': line.use,
                          'monthly_amount': line.monthly_amount, 'currency': line.currency, 'note': line.note,
                          'file_name': (file_name or '')[:200] or None, 'superseded_at': None,
                          'imported_by': actor.get('id'), 'imported_by_name': actor.get('name'), 'created_at': now})
            if len(batch) >= 2_000:
                connection.execute(inventory.insert(), batch)
                batch = []
        if batch:
            connection.execute(inventory.insert(), batch)
        current = {number: kinds[closures.CONTRACT_END]
                   for number, kinds in closures.all_notices(engine, connection).items()
                   if closures.CONTRACT_END in kinds}
        wanted = {line.number: line for line in lines if line.contract_end is not None}
        for number, line in wanted.items():
            notice = current.get(number)
            if notice is None or notice['closes_on'] != line.contract_end.isoformat() \
                    or (notice['carrier'] or None) != (line.carrier or None):
                closures.insert_notice(connection, number, kind=closures.CONTRACT_END, closes_on=line.contract_end,
                                       carrier=line.carrier, source_label=label, actor=actor, now=now)
        for number, notice in current.items():
            if number not in wanted:
                closures.insert_notice(connection, number, kind=closures.CONTRACT_END, state='removed',
                                       carrier=notice['carrier'], source_label=label, actor=actor, now=now)
    return {'lines': len(lines), 'contract_ends': len(wanted)}


def inventory_rows(engine):
    """The current inventory, one dict per line, in the order it was imported."""
    from .database import read_connection
    inventory = _tables(engine)['line_inventory']
    with read_connection(engine) as connection:
        rows = connection.execute(sa.select(inventory).where(inventory.c.superseded_at.is_(None))
                                  .order_by(inventory.c.created_at, inventory.c.id)).mappings().all()
    return [dict(row) for row in rows]


def _inventory_summary(rows):
    if not rows:
        return None
    first = rows[0]
    created = first['created_at']
    return {'file_name': first['file_name'], 'lines': len(rows), 'imported_by': first['imported_by_name'],
            'imported_at': created.isoformat() if hasattr(created, 'isoformat') else created,
            'fax_lines': sum(1 for row in rows if row['line_use'] == 'fax')}


# -- carrier lists -------------------------------------------------------------------------------------------------------

def parse_carrier_list(data, *, carrier=None, kind=None, date_order='mdy'):
    """``Parsed`` of ``Area``: AT&T's workbook as published, or a CSV with the documented columns."""
    if kind is not None and kind not in KINDS:
        raise InventoryError('Choose the list kind discontinued or grandfathered.')
    wanted = [LIST_COLUMNS['wire_center'], LIST_COLUMNS['effective']]
    try:
        header, rows, date1904, first = table(data, wanted, sheet_hint='Location')
    except WorkbookError as error:
        raise InventoryError(f'{error} A carrier list needs the columns wire center and effective date.') from None
    at = _columns(header, LIST_COLUMNS)
    att_layout = {'wire center', 'distribution area', 'effective date', 'ref table'} <= set(header) \
        and 'development name' in header
    parsed = Parsed([], layout='att_workbook' if att_layout else 'csv')
    default_carrier = ATT if att_layout else carrier_key(carrier)
    default_kind = 'discontinued' if att_layout else (kind or 'discontinued')
    if att_layout and carrier and carrier_key(carrier) != ATT:
        raise InventoryError("This is AT&T's workbook; import it as AT&T's list.")
    if not att_layout and default_carrier is None and 'carrier' not in at:
        raise InventoryError('Name the carrier whose list this is, or add a carrier column.')
    carriers = set()
    for position, row in enumerate(rows, start=first):
        if not any(cell is not None and str(cell).strip() for cell in row):
            continue
        wire = _wire_center(_cell(row, at, 'wire_center'))
        if wire is None:
            parsed.skip(f'Row {position}: {_text(_cell(row, at, "wire_center"), 20) or "no wire center"} is not an '
                        '8-character wire center code.')
            continue
        try:
            effective = parse_day(_cell(row, at, 'effective'), order=date_order, date1904=date1904)
        except ValueError:
            parsed.skip(f'Row {position}: the effective date {_text(_cell(row, at, "effective"), 20)} is not a date.')
            continue
        row_carrier = carrier_key(_cell(row, at, 'carrier')) or default_carrier
        row_kind = normal(_cell(row, at, 'kind')) or default_kind
        if row_kind not in KINDS:
            parsed.skip(f'Row {position}: the kind must be discontinued or grandfathered.')
            continue
        if row_carrier is None:
            parsed.skip(f'Row {position} names no carrier.')
            continue
        carriers.add((row_carrier, row_kind))
        area = _area_code(_cell(row, at, 'distribution_area'))
        parsed.items.append(Area(
            wire_center=wire, effective_on=effective, region=_text(_cell(row, at, 'region'), 40),
            city=_text(_cell(row, at, 'city'), 100), place=_text(_cell(row, at, 'place'), 200),
            wire_center_name=_text(_cell(row, at, 'wire_center_name'), 100),
            distribution_area=None if area in (None, 'ALL') else area,
            ref_table=_text(_cell(row, at, 'ref_table'), 8), carrier=row_carrier, kind=row_kind))
    if not parsed.items:
        raise InventoryError('The list has no area Faxbot can read.')
    if len(carriers) > 1:
        raise InventoryError('The file mixes carriers or kinds of list; import one carrier\'s list of one kind at a '
                             'time.')
    parsed.carrier, parsed.kind = next(iter(carriers))
    return parsed


def import_carrier_list(engine, parsed, *, source_url=None, file_date=None, file_name=None, actor=None, now=None):
    """Make ``parsed`` the current list of its carrier and kind; the earlier list is superseded, never changed."""
    from .database import utcnow, write_transaction
    if source_url is not None and re.fullmatch(r'https?://\S+', source_url) is None:
        raise InventoryError('The source must be a web address.')
    if source_url is None and parsed.layout == 'att_workbook':
        source_url = ATT_WORKBOOK
    now = (now or utcnow()).replace(microsecond=0)
    actor = actor or {}
    areas = _tables(engine)['carrier_service_areas']
    import_id = uuid4().hex
    with write_transaction(engine) as connection:
        connection.execute(areas.update().where(
            areas.c.carrier == parsed.carrier, areas.c.kind == parsed.kind, areas.c.superseded_at.is_(None))
            .values(superseded_at=now))
        batch = []
        for item in parsed.items:
            batch.append({'id': uuid4().hex, 'import_id': import_id, 'carrier': parsed.carrier, 'kind': parsed.kind,
                          'region': item.region, 'city': item.city, 'place': item.place,
                          'wire_center': item.wire_center, 'wire_center_name': item.wire_center_name,
                          'distribution_area': item.distribution_area, 'effective_on': _datetime(item.effective_on),
                          'ref_table': item.ref_table, 'source_url': source_url, 'file_date': _datetime(file_date),
                          'file_name': (file_name or '')[:200] or None, 'superseded_at': None,
                          'imported_by': actor.get('id'), 'imported_by_name': actor.get('name'), 'created_at': now})
            if len(batch) >= 5_000:
                connection.execute(areas.insert(), batch)
                batch = []
        if batch:
            connection.execute(areas.insert(), batch)
    return carrier_lists(engine)


def carrier_lists(engine):
    """Each current carrier list: carrier, kind, where it came from, and how many areas and wire centers it has."""
    from .database import read_connection
    areas = _tables(engine)['carrier_service_areas']
    with read_connection(engine) as connection:
        rows = connection.execute(
            sa.select(areas.c.carrier, areas.c.kind, areas.c.source_url, areas.c.file_date, areas.c.file_name,
                      areas.c.created_at, areas.c.imported_by_name, sa.func.count().label('areas'),
                      sa.func.count(sa.distinct(areas.c.wire_center)).label('wire_centers'),
                      sa.func.min(areas.c.effective_on).label('first'), sa.func.max(areas.c.effective_on).label('last'))
            .where(areas.c.superseded_at.is_(None))
            .group_by(areas.c.carrier, areas.c.kind, areas.c.source_url, areas.c.file_date, areas.c.file_name,
                      areas.c.created_at, areas.c.imported_by_name)).mappings().all()
    found = []
    for row in rows:
        first, last = _day(row['first']), _day(row['last'])
        found.append({'carrier': row['carrier'], 'carrier_label': carrier_label(row['carrier']), 'kind': row['kind'],
                      'label': ATT_LABEL if row['carrier'] == ATT and row['kind'] == 'discontinued'
                      else f"{carrier_label(row['carrier'])}'s {row['kind']} list",
                      'source_url': row['source_url'], 'file_name': row['file_name'],
                      'file_date': _day(row['file_date']).isoformat() if row['file_date'] else None,
                      'imported_at': row['created_at'].isoformat() if hasattr(row['created_at'], 'isoformat')
                      else row['created_at'], 'imported_by': row['imported_by_name'],
                      'areas': int(row['areas']), 'wire_centers': int(row['wire_centers']),
                      'first': first.isoformat() if first else None, 'last': last.isoformat() if last else None})
    return sorted(found, key=lambda item: (item['carrier'], item['kind']))


def _areas_by_wire_center(engine, wire_centers):
    from .database import read_connection
    areas = _tables(engine)['carrier_service_areas']
    wanted, found = sorted({item for item in wire_centers if item}), {}
    with read_connection(engine) as connection:
        for start in range(0, len(wanted), 500):
            for row in connection.execute(sa.select(areas).where(
                    areas.c.wire_center.in_(wanted[start:start + 500]), areas.c.superseded_at.is_(None))).mappings():
                found.setdefault(row['wire_center'], []).append(dict(row))
    return found


# -- matching ------------------------------------------------------------------------------------------------------------

def _when(day):
    return f'{day.day} {day:%B %Y}'


def _state(day, today):
    left = (day - today).days
    return 'passed' if left < 0 else 'soon' if left <= WARN_DAYS else 'later'


def match(line, rows, today):
    """What the carrier lists say about one inventory line (a dict from ``inventory_rows``), or None.

    ``state`` is ``listed`` (its wire center is listed whole, or with its distribution area), ``possible`` (AT&T
    lists only some distribution areas of its wire center and the line has none), ``no_wire_center`` (an AT&T line,
    or one with no carrier, that has no wire center) or None when nothing matches.
    """
    key = carrier_key(line.get('carrier'))
    wire = _wire_center(line.get('wire_center'))
    if wire is None:
        if key in (ATT, None) and line.get('line_use') in ('fax', 'unknown'):
            return {'state': 'no_wire_center', 'kind': None, 'sentence': (
                "Faxbot cannot check this line against AT&T's list without its wire center: add the 8-character "
                "code from AT&T's customer service record for the line to the inventory.")}
        return None
    candidates = [row for row in rows if key is None or row['carrier'] == key]
    if not candidates:
        return None
    area = _area_code(line.get('distribution_area'))
    hits = [row for row in candidates if not row['distribution_area'] or (area and row['distribution_area'] == area)]
    unsure = '' if key else " if this is AT&T's line" if all(row['carrier'] == ATT for row in candidates) else \
        ' if the carrier is right'
    if not hits:
        if area:
            return None
        listed = sorted({row['distribution_area'] for row in candidates})
        sample = ', '.join(listed[:3]) + (f' and {len(listed) - 3:,} more' if len(listed) > 3 else '')
        return {'state': 'possible', 'kind': candidates[0]['kind'], 'carrier': candidates[0]['carrier'],
                'wire_center': wire, 'sentence': (
                    f"{carrier_label(candidates[0]['carrier'])} lists parts of wire center {wire} (distribution areas "
                    f"{sample}){unsure}. Add the line's distribution area from the carrier's customer service record "
                    'to know whether it is one of them.')}
    dated = [row for row in hits if row['effective_on'] is not None]
    chosen = min(dated, key=lambda row: (_day(row['effective_on']), row['kind'] != 'discontinued')) if dated \
        else hits[0]
    effective = _day(chosen['effective_on'])
    who = carrier_label(chosen['carrier'])
    where = f'wire center {wire}' + (f', distribution area {chosen["distribution_area"]}'
                                     if chosen['distribution_area'] else ' (the whole wire center)')
    source = ATT_LABEL if chosen['carrier'] == ATT and chosen['kind'] == 'discontinued' \
        else f"{who}'s {chosen['kind']} list"
    if chosen['kind'] == 'grandfathered':
        when = f' from {_when(effective)}' if effective else ''
        sentence = (f'{who} grandfathered the service in {where}{when}{unsure}: the line keeps working, but {who} takes '
                    'no new orders or changes for it and can raise its price. Plan the move before its next notice.')
    elif effective is None:
        sentence = f'{source} lists {where} as discontinued{unsure}, with no date.'
    elif effective < today:
        sentence = (f'{source} lists {where} as discontinued from {_when(effective)}{unsure}: {who} no longer '
                    f'provides its copper services there. Ask {who} what this line runs on now, and move the number to '
                    'a trunk Faxbot uses before the line stops.')
    else:
        sentence = (f'{source} lists {where} as discontinued from {_when(effective)}{unsure}. Move the number to a '
                    'trunk Faxbot uses before then.')
    if chosen.get('ref_table') == 'G':
        sentence += f' {who} keeps a few services there, listed in its Table G.'
    return {'state': 'listed', 'kind': chosen['kind'], 'carrier': chosen['carrier'], 'wire_center': wire,
            'distribution_area': chosen['distribution_area'], 'effective_on': effective.isoformat() if effective
            else None, 'date_state': _state(effective, today) if effective else None, 'sentence': sentence,
            'source': source, 'source_url': chosen['source_url'],
            'file_date': _day(chosen['file_date']).isoformat() if chosen['file_date'] else None,
            'certain': not unsure}


def _price(row):
    from .costs import InvalidRateCard, money_text, parse_amount
    if not row.get('monthly_amount') or not row.get('currency'):
        return None
    try:
        return money_text(parse_amount(row['monthly_amount'], whole_digits=9), row['currency'])
    except InvalidRateCard:
        return None


def _placed(values):
    from .number_placement import placed_numbers
    return {number: host for number, host in placed_numbers(values)} if values is not None else {}


def _date_item(kind, day, today, sentence, *, source, source_url=None, source_date=None):
    return {'kind': kind, 'date': day.isoformat(), 'state': _state(day, today), 'sentence': sentence,
            'source': source, 'source_url': source_url, 'source_date': source_date}


def line_dates(engine, *, today, rows=None, matches=None):
    """{number: [dated items, earliest first]}: carrier letters, contract ends and carrier-list matches."""
    from . import closures
    found = {}
    for number, kinds in closures.all_notices(engine).items():
        letter, contract = kinds.get(closures.LETTER), kinds.get(closures.CONTRACT_END)
        if letter and letter.get('closes_on'):
            day = date.fromisoformat(letter['closes_on'])
            _, sentence = closures._warning(day, today, f'{letter.get("carrier") or "The carrier"} says this line')
            found.setdefault(number, []).append(_date_item(
                'letter', day, today, sentence, source=f'{letter.get("carrier") or "The carrier"}\'s letter',
                source_date=letter.get('notice_received_on')))
        if contract and contract.get('closes_on'):
            day = date.fromisoformat(contract['closes_on'])
            _, sentence = closures.contract_warning(day, today, contract.get('carrier'))
            found.setdefault(number, []).append(_date_item(
                'contract_end', day, today, sentence, source=contract.get('source_label') or 'Your line inventory'))
    if rows is None:
        rows = inventory_rows(engine)
    if matches is None:
        areas = _areas_by_wire_center(engine, [_wire_center(row['wire_center']) for row in rows])
        matches = {row['number']: match(row, areas.get(_wire_center(row['wire_center'])) or [], today) for row in rows}
    for number, found_match in matches.items():
        if found_match and found_match['state'] == 'listed' and found_match.get('effective_on'):
            found.setdefault(number, []).append(_date_item(
                'carrier_list', date.fromisoformat(found_match['effective_on']), today, found_match['sentence'],
                source=found_match['source'], source_url=found_match['source_url'],
                source_date=found_match['file_date']))
    for items in found.values():
        items.sort(key=lambda item: item['date'])
    return found


def view(engine, values=None, *, today=None):
    """The inventory with each line's carrier-list match and dates, the imported lists, and how matching works."""
    from .receiving import shown_number
    today = today or datetime.utcnow().date()
    rows = inventory_rows(engine)
    areas = _areas_by_wire_center(engine, [_wire_center(row['wire_center']) for row in rows])
    matches = {row['number']: match(row, areas.get(_wire_center(row['wire_center'])) or [], today) for row in rows}
    dates = line_dates(engine, today=today, rows=rows, matches=matches)
    placed = _placed(values)
    lines = []
    for row in rows:
        number = row['number']
        host = placed.get(number)
        lines.append({
            'number': number, 'display': shown_number(number), 'street': row['street'], 'city': row['city'],
            'region': row['region'], 'postal_code': row['postal_code'], 'carrier': row['carrier'],
            'product': row['product'], 'wire_center': row['wire_center'],
            'distribution_area': row['distribution_area'],
            'contract_end': _day(row['contract_end']).isoformat() if row['contract_end'] else None,
            'use': row['line_use'], 'use_label': USE_LABELS.get(row['line_use'], row['line_use']),
            'monthly': _price(row), 'note': row['note'], 'in_faxbot': host is not None,
            'account': host.name if host is not None else None, 'match': matches.get(number),
            'dates': dates.get(number, [])})
    state_order = {'passed': 0, 'soon': 1, 'later': 2}
    lines.sort(key=lambda line: (0, state_order[line['dates'][0]['state']], line['dates'][0]['date'])
               if line['dates'] else (1, 0, ''))
    lists = carrier_lists(engine)
    counts = {'lines': len(lines), 'fax': sum(1 for line in lines if line['use'] == 'fax'),
              'dated': sum(1 for line in lines if line['dates']),
              'listed': sum(1 for line in lines if (line['match'] or {}).get('state') == 'listed'),
              'possible': sum(1 for line in lines if (line['match'] or {}).get('state') == 'possible'),
              'no_wire_center': sum(1 for line in lines if (line['match'] or {}).get('state') == 'no_wire_center')}
    if not lines:
        sentence = 'No line inventory yet. Import one to match your fax lines to carrier lists and contract dates.'
    else:
        sentence = (f"{counts['lines']:,} {'line' if counts['lines'] == 1 else 'lines'} in your inventory, "
                    f"{counts['dated']:,} with a date set by a carrier or a contract.")
    return {'sentence': sentence, 'lines': lines, 'inventory': _inventory_summary(rows), 'lists': lists,
            'counts': counts, 'keyed': KEYED, 'note': ADVICE_ONLY,
            'help': {'inventory': INVENTORY_HELP, 'list': LIST_HELP},
            'sources': {'att_workbook': ATT_WORKBOOK, 'att_map': ATT_MAP, 'att_read_on': ATT_READ_ON}}


def plan_rows(engine, placed, *, today):
    """For the retirement plan: {number: dated items} and the inventory's dated fax lines no Faxbot account
    receives on (life-safety lines are left out: they need their own replacement)."""
    rows = inventory_rows(engine)
    areas = _areas_by_wire_center(engine, [_wire_center(row['wire_center']) for row in rows])
    matches = {row['number']: match(row, areas.get(_wire_center(row['wire_center'])) or [], today) for row in rows}
    dates = line_dates(engine, today=today, rows=rows, matches=matches)
    outside = [dict(row, monthly=_price(row)) for row in rows
               if row['number'] not in placed and row['line_use'] in ('fax', 'unknown') and dates.get(row['number'])]
    return dates, outside
