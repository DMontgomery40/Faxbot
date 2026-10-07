"""What each receiving machine accepts, the page settings, and the record of changed and split faxes.

A receiving machine says in its DIS (T.30 Table 2) the longest page it takes
(bits 19-20: A4 297 mm, A4 and B4 364 mm, or unlimited), whether it has error
correction, and its minimum time per scan line. The SSL Fax engine's session
log carries the DIS on every sent call; each report adds one
``page_capability_observations`` row and the newest row is what Faxbot knows.
Until there is one, Faxbot assumes A4, which every fax machine takes, and
never trims a page. Rows are added, never rewritten (migration 0028).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from statistics import median
from uuid import uuid4

import sqlalchemy as sa

from ..config_paths import provider_traits_path

DEFAULT_LIMIT = 'a4'
LIMITS = ('a4', 'b4', 'unlimited')
LIMIT_TEXT = {'a4': 'A4 length (297 mm)', 'b4': 'B4 length (364 mm)', 'unlimited': 'unlimited length'}
WIDTH_TEXT = {'a4': 'A4 width (215 mm)', 'b4': 'B4 width (255 mm)', 'a3': 'A3 width (303 mm)'}
PACKING = ('allow', 'never')
# Routes whose own fax engine sends Faxbot's image: the receiving machine's limit decides.
IMAGE_ROUTES = frozenset({'sip', 'freeswitch'})
# The end-of-page exchange, from T.30 (07/96): two V.21 frames each with a 1 s +/- 15% preamble (5.3.1),
# 75 +/- 20 ms gaps (5.3.2.2, 5.3.2.3), about 0.19 s of frame bits each at 300 bit/s, and the image
# modem's training again (spandsp 0.0.6: V.17 short 0.16 s, V.29 0.25 s, V.27ter 0.93 s). About 3 s;
# a destination's measured value (the SSL Fax engine's session logs) replaces it.
BOUNDARY_SECONDS = 3.0
TABLES = ('page_capability_observations', 'recipient_page_settings', 'route_page_settings', 'fax_page_changes',
          'inbound_page_splits')
_NUMBER = re.compile(r'\+?[0-9]{3,20}', re.ASCII)
_ROUTE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}', re.ASCII)
_ID = re.compile(r'[A-Za-z0-9_-]{1,40}', re.ASCII)
_SOURCE = re.compile(r'[A-Za-z0-9_.:-]{1,100}', re.ASCII)


class PageRecordError(RuntimeError):
    """Sanitized storage failure; never includes SQL or values."""


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def number_of(value):
    text = str(value or '').strip()
    return text if _NUMBER.fullmatch(text) else None


def iso(value):
    return value.replace(microsecond=0).isoformat() + 'Z' if isinstance(value, datetime) else None


@dataclass(frozen=True)
class Capability:
    """What Faxbot knows about one receiving machine's pages."""
    limit: str
    learned_at: datetime | None  # None: nothing learned yet, the A4 default applies
    max_width: str | None = None
    fine: bool | None = None
    ecm: bool | None = None  # whether the machine offers error correction; None: not known
    scan_ms: int | None = None  # its minimum time per scan line at fine resolution
    boundary_seconds: float | None = None  # measured time between pages, when the engine reported it

    @property
    def learned(self):
        return self.learned_at is not None


# Provider page models (config/provider_traits.json, "pages") --------------------------------------------------

def page_models():
    """{provider: page model} from the bundled provider_traits.json; {} when it cannot be read."""
    try:
        data = json.loads(provider_traits_path().read_text())
    except (OSError, ValueError):
        return {}
    return {key: value['pages'] for key, value in data.items()
            if key != '_schema' and isinstance(value, dict) and isinstance(value.get('pages'), dict)}


def page_model(route):
    return page_models().get(route) or {}


def route_default(route):
    """(long pages on by default, can be turned on at all) for a route."""
    model = page_model(route)
    if route in IMAGE_ROUTES:
        return True, True
    if model.get('how_sent') == 'pdf_url':
        return False, False
    return model.get('long_pages') == 'accepts', True


# Storage ------------------------------------------------------------------------------------------------------

class PageRecords:
    def __init__(self, engine):
        self.engine = engine
        self._tables = None

    def table(self, name):
        if self._tables is None:
            metadata = sa.MetaData()
            try:
                self._tables = {table: sa.Table(table, metadata, autoload_with=self.engine) for table in TABLES}
            except sa.exc.SQLAlchemyError:
                raise PageRecordError('Page records are unavailable.') from None
        return self._tables[name]

    def _write(self, operation):
        try:
            with self.engine.begin() as connection:
                return operation(connection)
        except sa.exc.SQLAlchemyError:
            raise PageRecordError('Page record could not be saved.') from None

    def _read(self, operation):
        try:
            with self.engine.connect() as connection:
                return operation(connection)
        except sa.exc.SQLAlchemyError:
            raise PageRecordError('Page records are unavailable.') from None

    # What receiving machines accept ---------------------------------------------------------------------------

    def record_observation(self, number, *, source, engine, values, job_id=None, now=None):
        """One sent call's report of the other machine's pages (fax_negotiation.page_capability); added once
        per call (``source``), never rewritten. Returns the row's ID, or None when nothing was reported."""
        number = number_of(number)
        if (number is None or not isinstance(values, dict) or values.get('max_length') not in LIMITS
                or engine not in ('builtin', 'hylafax') or not _SOURCE.fullmatch(str(source or ''))):
            return None
        now = now or utcnow()
        table = self.table('page_capability_observations')

        def whole(name, highest):
            value = values.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= highest else None
        row = {'number': number, 'max_length': values['max_length'],
               'max_width': values.get('max_width') if values.get('max_width') in WIDTH_TEXT else None,
               'fine': whole('fine', 1), 'ecm': whole('ecm', 1), 'scan_ms': whole('scan_ms', 40),
               'boundary_ms': whole('boundary_ms', 600000), 'boundaries': whole('boundaries', 999),
               'engine': engine, 'source': str(source),
               'job_id': job_id if _ID.fullmatch(str(job_id or '')) else None, 'observed_at': now}
        if row['boundary_ms'] is None or not row['boundaries']:
            row['boundary_ms'] = row['boundaries'] = None

        def apply(connection):
            found = connection.execute(sa.select(table.c.id).where(
                table.c.number == number, table.c.source == row['source'])).scalar()
            if found is not None:
                return found
            identity = uuid4().hex
            connection.execute(table.insert().values(id=identity, **row))
            return identity
        return self._write(apply)

    def capability(self, number):
        """What Faxbot knows about this number's machine; the A4 default when nothing was learned."""
        number = number_of(number)
        if number is None:
            return Capability(DEFAULT_LIMIT, None)
        table = self.table('page_capability_observations')

        def read(connection):
            newest = connection.execute(sa.select(table).where(table.c.number == number).order_by(
                table.c.observed_at.desc(), table.c.id.desc()).limit(1)).mappings().first()
            timed = connection.execute(sa.select(table.c.boundary_ms).where(
                table.c.number == number, table.c.boundary_ms.is_not(None)).order_by(
                table.c.observed_at.desc(), table.c.id.desc()).limit(10)).scalars().all()
            return newest, timed
        newest, timed = self._read(read)
        boundary = round(median(timed) / 1000, 2) if timed else None
        if newest is None:
            return Capability(DEFAULT_LIMIT, None, boundary_seconds=boundary)
        return Capability(newest['max_length'], newest['observed_at'], newest['max_width'],
                          None if newest['fine'] is None else bool(newest['fine']),
                          None if newest['ecm'] is None else bool(newest['ecm']), newest['scan_ms'], boundary)

    # Settings -----------------------------------------------------------------------------------------------

    def _setting_row(self, name, column, key):
        table = self.table(name)
        row = self._read(lambda connection: connection.execute(
            sa.select(table).where(table.c[column] == key)).mappings().first())
        return dict(row) if row is not None else {}

    def _save_setting(self, name, column, key, changes, actor, now):
        """Merge ``changes`` into the row for ``key``; a row left with nothing set is removed."""
        table = self.table(name)
        fields = [field for field in table.c.keys() if field not in ('id', column, 'updated_at', 'updated_by')]

        def apply(connection):
            row = connection.execute(sa.select(table).where(table.c[column] == key)).mappings().first()
            values = {field: (row[field] if row is not None else None) for field in fields}
            values.update(changes)
            connection.execute(table.delete().where(table.c[column] == key))
            if any(value is not None for value in values.values()):
                connection.execute(table.insert().values(id=uuid4().hex, **{column: key}, **values, updated_at=now,
                                                         updated_by=str(actor or '')[:100] or None))
        self._write(apply)

    def recipient_settings(self, number):
        """{'packing': 'allow' | 'never', 'trim_blank': True | False | None (the installation's setting)}."""
        number = number_of(number)
        row = self._setting_row('recipient_page_settings', 'number', number) if number else {}
        trim = row.get('trim_blank')
        return {'packing': row.get('packing') or 'allow', 'trim_blank': None if trim is None else bool(trim)}

    def recipient_packing(self, number):
        """'allow' (as the receiving machine allows; also when nobody set it) or 'never'."""
        return self.recipient_settings(number)['packing']

    def set_recipient_settings(self, number, *, packing=..., trim_blank=..., actor=None, now=None):
        """Change one number's settings; an argument left out keeps its value. ``trim_blank`` None follows the
        installation's setting."""
        number = number_of(number)
        if number is None:
            raise ValueError('Enter the fax number with its country code.')
        changes = {}
        if packing is not ...:
            if packing not in PACKING:
                raise ValueError("Choose 'As the receiving machine allows' or 'Never'.")
            changes['packing'] = None if packing == 'allow' else packing
        if trim_blank is not ...:
            if trim_blank is not None and not isinstance(trim_blank, bool):
                raise ValueError('Choose on, off or the setting for all faxes.')
            changes['trim_blank'] = None if trim_blank is None else int(trim_blank)
        self._save_setting('recipient_page_settings', 'number', number, changes, actor, now or utcnow())
        return self.recipient_settings(number)

    def route_long_pages(self, route):
        """(on, set by a person) for one route: the person's choice, else the route's default."""
        default, possible = route_default(route)
        if not possible:
            return False, False
        value = self._setting_row('route_page_settings', 'route', route).get('long_pages')
        return (default, False) if value is None else (bool(value), True)

    def trim_blank_default(self):
        """The installation's trim setting (held on the SIP trunk's row): on unless a person turned it off."""
        value = self._setting_row('route_page_settings', 'route', 'sip').get('trim_blank')
        return True if value is None else bool(value)

    def set_route_settings(self, route, *, long_pages=..., trim_blank=..., actor=None, now=None):
        """Turn long pages on or off for a route, and trimming for the installation (the SIP trunk's row);
        None goes back to the default; an argument left out keeps its value."""
        if not isinstance(route, str) or not _ROUTE.fullmatch(route):
            raise ValueError('Choose one of your delivery routes.')
        changes = {}
        if long_pages is not ...:
            if long_pages is not None and not route_default(route)[1]:
                raise ValueError('This provider fetches the document from Faxbot itself, so long pages cannot '
                                 'be sent through it.')
            if long_pages is not None and not isinstance(long_pages, bool):
                raise ValueError('Choose on or off.')
            changes['long_pages'] = None if long_pages is None else int(long_pages)
        if trim_blank is not ...:
            if route != 'sip':
                raise ValueError('Blank space is left out on your phone line only.')
            if trim_blank is not None and not isinstance(trim_blank, bool):
                raise ValueError('Choose on or off.')
            changes['trim_blank'] = None if trim_blank is None else int(trim_blank)
        self._save_setting('route_page_settings', 'route', route, changes, actor, now or utcnow())
        return self.route_view(route)

    def route_view(self, route):
        on, chosen = self.route_long_pages(route)
        _, possible = route_default(route)
        model = page_model(route)
        return {'route': route, 'long_pages': on, 'long_pages_chosen': chosen, 'long_pages_possible': possible,
                'trim_blank': self.trim_blank_default() if route == 'sip' else None,
                'provider_says': model.get('long_pages'), 'source_url': model.get('source'),
                'read_on': model.get('read_on')}

    def trim_allowed(self, number):
        """Whether Faxbot may leave out blank page bottoms for this number (its setting, else the installation's)."""
        own = self.recipient_settings(number)['trim_blank']
        return self.trim_blank_default() if own is None else own

    # Changed sends and split received faxes -----------------------------------------------------------------

    def record_change(self, *, job_id, attempt_id, number, route, original_pages, sent_pages, capability=None,
                      billing=None, trimmed_pages=None, trimmed_rows=None, resolution=None, seconds_saved=None,
                      layout=None, reason=None, now=None):
        """One send whose pages Faxbot changed (packed, trimmed, kept at standard resolution, or more than one),
        once per attempt; returns its ID."""
        if not (_ID.fullmatch(str(job_id or '')) and _ID.fullmatch(str(attempt_id or ''))):
            raise ValueError('Unsupported page record')
        if not (isinstance(original_pages, int) and isinstance(sent_pages, int) and original_pages >= 1
                and sent_pages >= 1) or layout not in (None, 'dense', 'codec'):
            raise ValueError('Unsupported page record')
        packed = layout == 'dense'
        trimmed = trimmed_pages if isinstance(trimmed_pages, int) and trimmed_pages > 0 else None
        if resolution not in (None, 'standard') or (layout is None and trimmed is None and resolution is None):
            raise ValueError('Unsupported page record')
        now = now or utcnow()
        table = self.table('fax_page_changes')
        seconds = seconds_saved if isinstance(seconds_saved, int) and seconds_saved >= 0 else None

        def apply(connection):
            found = connection.execute(sa.select(table.c.id).where(table.c.attempt_id == attempt_id)).scalar()
            if found is not None:
                return found
            identity = uuid4().hex
            connection.execute(table.insert().values(
                id=identity, job_id=job_id, attempt_id=attempt_id, number=number_of(number), route=route,
                original_pages=original_pages, sent_pages=sent_pages,
                page_limit=capability.limit if packed and capability is not None else None,
                limit_learned_at=capability.learned_at if packed and capability is not None else None,
                billing=billing if layout else None, pages_saved=original_pages - sent_pages,
                layout=layout, reason=str(reason)[:300] if reason else None,
                trimmed_pages=trimmed, trimmed_rows=trimmed_rows if trimmed and trimmed_rows else None,
                resolution=resolution, seconds_saved=seconds, created_at=now))
            return identity
        return self._write(apply)

    def change_for_job(self, job_id):
        """The newest changed send of a fax, or None."""
        if not _ID.fullmatch(str(job_id or '')):
            return None
        table = self.table('fax_page_changes')
        row = self._read(lambda connection: connection.execute(sa.select(table).where(
            table.c.job_id == job_id).order_by(table.c.created_at.desc(), table.c.id.desc()).limit(1)).mappings().first())
        return dict(row) if row is not None else None

    def record_split(self, inbound_fax_id, *, received_pages, original_pages, now=None):
        if not _ID.fullmatch(str(inbound_fax_id or '')) or not (received_pages >= 1 and original_pages >= 2):
            return None
        now = now or utcnow()
        table = self.table('inbound_page_splits')

        def apply(connection):
            found = connection.execute(sa.select(table.c.id).where(
                table.c.inbound_fax_id == inbound_fax_id)).scalar()
            if found is not None:
                return found
            identity = uuid4().hex
            connection.execute(table.insert().values(id=identity, inbound_fax_id=inbound_fax_id,
                                                     received_pages=received_pages, original_pages=original_pages,
                                                     created_at=now))
            return identity
        return self._write(apply)

    def split_for(self, inbound_fax_id):
        if not _ID.fullmatch(str(inbound_fax_id or '')):
            return None
        table = self.table('inbound_page_splits')
        row = self._read(lambda connection: connection.execute(sa.select(table).where(
            table.c.inbound_fax_id == inbound_fax_id)).mappings().first())
        return dict(row) if row is not None else None


def records_for(engine):
    return PageRecords(engine)


# For the rules engine ------------------------------------------------------------------------------------------

def long_pages_allowed(engine, route, number, *, rule=None):
    """Whether Faxbot may pack pages for this send: (allowed, reason).

    The rules engine calls this with its own decision in ``rule``: None (no
    rule says anything), 'allow' or 'never'. A rule's 'never' and the
    recipient's 'Never' always win; a rule's 'allow' turns long pages on for a
    route that is off by default, but never for a route that cannot send
    them. Packing still happens only when the receiving machine's limit and
    the route's billing make it worth it (``decision``). Reasons: 'rule',
    'recipient', 'route_cannot', 'route_off' or 'route'.
    """
    if rule not in (None, 'allow', 'never'):
        raise ValueError('A rule says allow or never.')
    if rule == 'never':
        return False, 'rule'
    records = records_for(engine)
    if records.recipient_packing(number) == 'never':
        return False, 'recipient'
    if not route_default(route)[1]:
        return False, 'route_cannot'
    on, _ = records.route_long_pages(route)
    if on:
        return True, 'route'
    if rule == 'allow':
        return True, 'rule'
    return False, 'route_off'
