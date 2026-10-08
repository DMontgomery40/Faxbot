"""The trunk carrier's call records, for carriers other than Telnyx, and which carriers publish them (B2).

``routing/carriers.py`` matches a carrier's billing records to Faxbot's own
call records and keeps their charges; ``routing/telnyx.py`` reads Telnyx's.
A reader here gives ``CarrierReconciler`` the same thing for another carrier:
``carrier`` and ``label``, ``ready()``, and ``fetch(start, end)`` returning
``(records, complete)`` of ``telnyx.CarrierRecord``. Matching, corrections,
unrecorded calls and Spending then work exactly as for Telnyx.

``PUBLISHED`` says, for every trunk preset, whether the carrier publishes an
API for its call records with a charge per call, with the source and the date
it was read. Where none is published the trunk's page says so, and the
carrier's invoice goes under Costs → Invoices instead. Unknown stays unknown:
a carrier is never assumed to publish one.

Nothing here contacts a carrier unless a reader's ``fetch`` is called, and a
reader only ever asks its carrier's own documented host with the account's own
credentials. Response bodies and credentials are never logged.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import re

import httpx

from .telnyx import CarrierRateLimited, CarrierRecord, CarrierUnavailable


@dataclass(frozen=True)
class Published:
    """Whether a carrier publishes its call records with charges, in one sentence, with sources."""
    api: bool
    sentence: str
    sources: tuple = ()          # ((title, url, read on), ...)
    reader: str | None = None    # the reader class name here, when Faxbot reads them


READ_ON = '2026-10-08'

PUBLISHED = {
    'telnyx': Published(
        True, 'Telnyx publishes each call\'s charge; Faxbot reads it with the Telnyx API key.',
        (('Telnyx API: Search detail records', 'https://developers.telnyx.com/api-reference/detail-records/'
          'search-detail-records', '2026-10-06'),), reader='telnyx'),
    'signalwire': Published(
        True, 'SignalWire publishes each call\'s charge; Faxbot reads it with your SignalWire project ID and API '
              'token (Providers → SignalWire).',
        (('SignalWire Compatibility API: List all calls', 'https://developer.signalwire.com/compatibility-api/'
          'rest/calls/list-all-calls', READ_ON),), reader='SignalWireCallRecords'),
}


def published(preset):
    """What Faxbot knows about a preset's call records; a preset not looked up yet reads as not published."""
    from ..sip_trunk import PRESETS
    found = PUBLISHED.get(preset)
    if found is not None:
        return found
    label = PRESETS[preset].label if preset in PRESETS and preset != 'custom' else 'Your carrier'
    return Published(False, f'{label} publishes no call records with charges that Faxbot can read; enter its '
                            'invoice under Costs → Invoices.')


# Readers ------------------------------------------------------------------------------------------------------

_SID = re.compile(r'[A-Za-z0-9_-]{1,100}', re.ASCII)
_HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', re.ASCII)
_CURRENCY = re.compile(r'[A-Za-z]{3}', re.ASCII)


def _time(value):
    from .provider_sweep import _time as parse
    return parse(value)


def _seconds(value):
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if type(value) is int and 0 <= value <= 86_400 else None


def _number(value):
    text = value.strip() if isinstance(value, str) else ''
    if text.startswith('sip:'):
        text = text[4:].split('@', 1)[0]
    return text[:32] or None


class SignalWireCallRecords:
    """SignalWire Compatibility API "List all calls": ``GET https://{space}/api/laml/2010-04-01/Accounts/{project}/Calls.json``.

    Basic auth with the project ID and API token; filtered by ``StartTime>=`` and ``StartTime<`` (dates), paged by
    ``PageSize`` with ``next_page_uri``. Each call has ``sid``, ``direction`` (``inbound``, ``outbound-api`` or
    ``outbound-dial``), ``from``, ``to``, ``status``, ``start_time`` and ``end_time`` (RFC 2822), ``duration``
    (seconds), ``price`` (decimal text, negative for a charge, null until priced) and ``price_unit`` (SignalWire
    Compatibility API reference, read 2026-10-08). A call carries no SIP Call-ID, so a call is matched by its
    numbers and times; its answer time is its end less its duration.
    """
    carrier = 'signalwire'
    label = 'SignalWire'
    PAGE_SIZE = 100

    def __init__(self, credentials, *, timeout=15.0, client_factory=None, max_pages=20):
        """``credentials()`` returns ``(space, project, token)``, each '' when not set."""
        self.credentials = credentials
        self.timeout = timeout
        self.max_pages = max_pages
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout, follow_redirects=False))

    def _account(self):
        try:
            space, project, token = self.credentials()
        except Exception:
            return '', '', ''
        space = re.sub(r'^https?://', '', str(space or '').strip()).rstrip('/')
        return space, str(project or '').strip(), str(token or '').strip()

    def ready(self):
        space, project, token = self._account()
        return bool(_HOST.fullmatch(space) and space.endswith('.signalwire.com') and _SID.fullmatch(project) and token)

    @staticmethod
    def parse(item):
        """A ``CarrierRecord`` from one call, or None when it is not a usable call record."""
        from .telnyx import parse_amount
        if not isinstance(item, dict) or not isinstance(item.get('sid'), str) or not _SID.fullmatch(item['sid']):
            return None
        kind = str(item.get('direction') or '').lower()
        direction = 'inbound' if kind == 'inbound' else 'outbound' if kind.startswith('outbound') else None
        started = _time(item.get('start_time'))
        if direction is None or started is None:
            return None
        finished = _time(item.get('end_time'))
        seconds = _seconds(item.get('duration'))
        answered = None
        if str(item.get('status') or '').lower() == 'completed' and finished is not None and seconds:
            answered = finished - timedelta(seconds=seconds)
        micros = raw = currency = None
        price, unit = item.get('price'), item.get('price_unit')
        if price is not None and not isinstance(price, bool) and isinstance(unit, str) \
                and _CURRENCY.fullmatch(unit.strip()):
            raw = str(price).strip()[:32]
            try:
                # SignalWire reports a charge as a negative amount; Faxbot keeps a charge positive and a credit
                # negative, as Telnyx reports them.
                cost = -Decimal(raw)
            except (InvalidOperation, ValueError):
                cost = None
            priced = parse_amount(format(cost if cost else Decimal(0), 'f'), unit) if cost is not None and \
                cost.is_finite() else None
            if priced is None:
                raw = None
            else:
                micros, _, currency = priced
        return CarrierRecord(id=item['sid'], direction=direction, sip_call_id=None, cli=_number(item.get('from')),
                             cld=_number(item.get('to')), started_at=started, answered_at=answered,
                             finished_at=finished, call_seconds=seconds, billed_seconds=None, amount_micros=micros,
                             raw_amount=raw, currency=currency)

    def fetch(self, start, end):
        """``(records, complete)`` for calls that started in ``[start, end)``; read-only."""
        space, project, token = self._account()
        if not self.ready():
            raise CarrierUnavailable('SignalWire is not fully set up.')
        url = f'https://{space}/api/laml/2010-04-01/Accounts/{project}/Calls.json'
        # The filter takes dates: read whole days and keep the window's calls.
        params = {'StartTime>': (start - timedelta(days=1)).date().isoformat(),
                  'StartTime<': (end + timedelta(days=1)).date().isoformat(), 'PageSize': self.PAGE_SIZE}
        records, pages = [], 0
        with self.client_factory() as client:
            while True:
                try:
                    response = client.get(url, params=params, auth=(project, token),
                                          headers={'Accept': 'application/json'})
                except httpx.HTTPError:
                    raise CarrierUnavailable('SignalWire could not be reached.') from None
                if response.status_code == 429:
                    raise CarrierRateLimited('SignalWire asked Faxbot to slow down.')
                if response.status_code != 200:
                    raise CarrierUnavailable('SignalWire did not answer the charge lookup.')
                try:
                    body = response.json()
                except ValueError:
                    raise CarrierUnavailable('SignalWire returned an unusable answer.') from None
                items = body.get('calls') if isinstance(body, dict) else None
                if not isinstance(items, list):
                    raise CarrierUnavailable('SignalWire returned an unusable answer.')
                records.extend(record for record in map(self.parse, items) if record is not None)
                pages += 1
                following = body.get('next_page_uri')
                if not following:
                    return [record for record in records if start <= record.started_at < end], True
                if pages >= self.max_pages or not isinstance(following, str) or not following.startswith('/api/laml/'):
                    return [record for record in records if start <= record.started_at < end], False
                url, params = f'https://{space}{following}', None


READERS = {'signalwire': SignalWireCallRecords}


def reader_for(preset, values, *, client_factory=None):
    """The call-record reader for a trunk preset other than Telnyx, or None when the carrier publishes none."""
    if preset == 'signalwire':
        def credentials():
            current = values() if callable(values) else values
            return (getattr(current, 'signalwire_space_url', ''), getattr(current, 'signalwire_project_id', ''),
                    getattr(current, 'signalwire_api_token', ''))
        return SignalWireCallRecords(credentials, client_factory=client_factory)
    return None


def trunk_records(values):
    """The trunk carrier's call records: whether published, whether Faxbot can read them now, and one sentence."""
    preset = getattr(values, 'sip_trunk_preset', '') or ''
    if not preset:
        return {'preset': None, 'published': False, 'readable': False, 'sources': [], 'sentence':
                'No carrier trunk is set up.'}
    found = published(preset)
    if preset == 'telnyx':
        readable = bool(getattr(values, 'telnyx_api_key', ''))
    else:
        reader = reader_for(preset, values)
        readable = reader is not None and reader.ready()
    sentence = found.sentence
    if found.api and not readable:
        from ..sip_trunk import PRESETS
        label = PRESETS[preset].label if preset in PRESETS else 'Your carrier'
        sentence = (f'{label} publishes each call\'s charge, but Faxbot cannot read it yet: '
                    + ('add the Telnyx API key under Providers → Telnyx.' if preset == 'telnyx' else
                       'add your SignalWire project ID and API token under Providers → SignalWire.'
                       if preset == 'signalwire' else 'add its API credentials.'))
    return {'preset': preset, 'published': found.api, 'readable': readable,
            'sources': [{'title': title, 'url': url, 'read_on': read_on} for title, url, read_on in found.sources],
            'sentence': sentence}


def other_carrier_task(engine, values):
    """The background task matching another carrier's call records to Faxbot's calls, or None.

    Telnyx's runs in ``routing/http.py``. This one follows the trunk preset in the active configuration, so a
    change of carrier takes effect without a restart; it does nothing while the carrier is not readable.
    """
    from .background import repeat
    from .carriers import CarrierChargeStore, CarrierReconciler
    from .database import DeliveryStoreError
    from .store import RouteStore
    try:
        store, routes = CarrierChargeStore(engine), RouteStore(engine)
    except DeliveryStoreError:
        return None
    reconcilers = {}

    def numbers():
        current = values()
        if current is None:
            return ()
        return tuple(number for number in (*getattr(current, 'sip_trunk_did_list', ()),
                                           getattr(current, 'sip_trunk_caller_id', '')) if number)

    def step():
        current = values()
        preset = getattr(current, 'sip_trunk_preset', '') if current is not None else ''
        if preset not in READERS:
            return False
        if preset not in reconcilers:
            reconcilers[preset] = CarrierReconciler(store, routes, reader_for(preset, values), preset=preset,
                                                    numbers=numbers)
        return reconcilers[preset].step()
    return ('faxbot-other-carrier-charges', repeat(step, interval=60.0, initial_delay=55.0,
                                                   warning='Carrier call charges are temporarily unavailable.'))
