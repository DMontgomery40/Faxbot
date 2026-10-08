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
# Tests replace this with an ``httpx.MockTransport``; production uses the network.
_TRANSPORT = None

PUBLISHED = {
    'telnyx': Published(
        True, 'Telnyx publishes each call\'s charge; Faxbot reads it with the Telnyx API key.',
        (('Telnyx API: Search detail records', 'https://developers.telnyx.com/api-reference/detail-records/'
          'search-detail-records', '2026-10-06'),), reader='telnyx'),
    'signalwire': Published(
        True, 'SignalWire publishes each call\'s charge; Faxbot reads it with your SignalWire project ID and API '
              'token (Providers → SignalWire).',
        (('SignalWire Compatibility API: List all calls', 'https://signalwire.com/docs/compatibility-api/rest/'
          'calls/list-all-calls', READ_ON),), reader='SignalWireCallRecords'),
    'flowroute': Published(
        True, 'Flowroute publishes each call\'s charge in a call-record export; Faxbot reads it with your Flowroute '
              'API access key and secret key.',
        (('Flowroute CDR Exports: query CDRs', 'https://developer.flowroute.com/api/cdrexports/v2.0/query-cdrs/',
          READ_ON),
         ('Flowroute CDR Exports: export status', 'https://developer.flowroute.com/api/cdrexports/v2.0/cdr-status/',
          READ_ON),
         ('Flowroute CDR Exports: CDR results', 'https://developer.flowroute.com/api/cdrexports/v2.0/cdr-results/',
          READ_ON)), reader='FlowrouteCallRecords'),
    # Searched on 8 October 2026; none found. Each says so on the trunk's page, never "unknown" as "none".
    'gamma': Published(False, 'Gamma gives call records to its channel partners in its own portal and publishes no '
                              'call-record API Faxbot could find; enter its invoice under Costs → Invoices.'),
    'bt-one-voice': Published(False, 'BT publishes no call-record API for One Voice that Faxbot could find; enter its '
                                     'invoice under Costs → Invoices.'),
    'telstra-sip-connect': Published(False, 'Telstra publishes no call-record API for SIP Connect that Faxbot could '
                                            'find; enter its invoice under Costs → Invoices.'),
}

# Carriers without a trunk preset, for the record: what each publishes, from the same search.
OTHER_CARRIERS = {
    'bandwidth': Published(
        False, 'Bandwidth says its Call Logs and Call Search API give recent calls for up to 30 days, with an '
               'estimated cost per call through Insights; its API reference could not be read, so Faxbot does not '
               'read it yet.',
        (('Bandwidth support: Call Logs and Call Search', 'https://www.bandwidth.com/support/en/articles/12824002',
          READ_ON),)),
}


def published(preset):
    """What Faxbot knows about a preset's call records. A preset not looked up says only that Faxbot can't read them."""
    from ..sip_trunk import PRESETS
    found = PUBLISHED.get(preset)
    if found is not None:
        return found
    item = PRESETS.get(preset)
    if item is not None and item.phone_system:
        return Published(False, f'{item.label} is your phone system; the carrier behind it bills these calls, so '
                                'enter its invoice under Costs → Invoices.')
    label = item.label if item is not None and preset != 'custom' else 'your carrier'
    return Published(False, f"Faxbot can't read {label}'s call charges; enter its invoice under Costs → Invoices.")


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


def _rfc2822(moment):
    """A naive UTC time as SignalWire's filters take it: "Wed, 07 Oct 2026 15:30:00 +0000"."""
    days = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
    months = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
    return (f'{days[moment.weekday()]}, {moment.day:02d} {months[moment.month - 1]} {moment.year} '
            f'{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d} +0000')


def _number(value):
    text = value.strip() if isinstance(value, str) else ''
    if text.startswith('sip:'):
        text = text[4:].split('@', 1)[0]
    return text[:32] or None


class SignalWireCallRecords:
    """SignalWire Compatibility API "List all calls": ``GET https://{space}/api/laml/2010-04-01/Accounts/{project}/Calls.json``.

    Basic auth with the project ID and API token; filtered by ``StartTime>`` and ``StartTime<`` ("RFC 2822 GMT
    format", for example "Wed, 19 Sep 2018 20:00:01 +0000"), paged by
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
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout, follow_redirects=False,
                                                                      transport=_TRANSPORT))

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
        params = {'StartTime>': _rfc2822(start), 'StartTime<': _rfc2822(end), 'PageSize': self.PAGE_SIZE}
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


class FlowrouteCallRecords:
    """Flowroute's call-record export (CDR Exports v2.0), read 8 October 2026.

    - ``POST https://api.flowroute.com/v2/cdrs/exports`` with basic auth (access key, secret key) and a JSON:API body
      ``{"data": {"type": "cdrexport", "attributes": {"filter_parameters": {"start_call_start_time": ...,
      "start_call_end_time": ...}}}}`` (UTC, "YYYY-MM-DD HH:MM:SS") answers 202 with the export's ``id`` and
      ``status`` (processing, completed or failed).
    - ``GET /v2/cdrs/exports/{id}`` gives its ``status`` and, once completed, a ``download_url``: a signed Amazon S3
      link that needs no other authentication.
    - The file is a gzip CSV whose first line names the fields: ``direction``, ``start_time``, ``end_time``,
      ``destination``, ``callerid``, ``total_cost`` (decimal dollars), ``result``, ``duration`` and
      ``billed_duration`` (seconds), ``rate`` and fees. Times read like "2019-06-25 18:18:54+00".

    An export takes a while. A fetch asks about it a few times, ``WAIT`` apart; if it is still being prepared the
    fetch raises ``ExportPreparing`` and the reconciler asks again after its back-off. Windows are widened to whole
    UTC days, and one reader is shared per set of keys (``reader_for``), so the background job, the hourly check
    for unrecorded calls and Check now all poll the same export rather than each making another. A completed
    export's records are reused for ``KEEP``, as fresh as a Telnyx read, then read again so a corrected cost is
    seen. A record carries no ID and no SIP Call-ID,
    so its identity is a digest of its direction, numbers and times, and a call is matched by numbers and times; its
    answer time is its end less its duration. The download link is followed only to an ``amazonaws.com`` host, never
    with the Flowroute key, and at most 20 MB compressed and 100 MB of text are read.
    """
    carrier = 'flowroute'
    label = 'Flowroute'
    URL = 'https://api.flowroute.com/v2/cdrs/exports'
    MAX_COMPRESSED = 20 * 1024 * 1024
    MAX_TEXT = 100 * 1024 * 1024
    KEEP = timedelta(minutes=30)
    POLLS = 6
    WAIT = 2.0

    def __init__(self, credentials, *, timeout=30.0, client_factory=None, clock=None, sleep=None):
        """``credentials()`` returns ``(access key, secret key)``, each '' when not set."""
        import time
        self.credentials = credentials
        self.timeout = timeout
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout, follow_redirects=False,
                                                                      transport=_TRANSPORT))
        self.clock = clock
        self.sleep = sleep or time.sleep
        self.exports = {}   # (first day, day after) -> {'id': ..., 'records': [...] | None, 'at': when read}
        self.preparing = False

    def _keys(self):
        try:
            access, secret = self.credentials()
        except Exception:
            return '', ''
        return str(access or '').strip(), str(secret or '').strip()

    def ready(self):
        access, secret = self._keys()
        return bool(access and secret)

    @staticmethod
    def _days(start, end):
        floor = start.replace(hour=0, minute=0, second=0, microsecond=0)
        ceiling = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return floor, ceiling if ceiling >= end else ceiling + timedelta(days=1)

    @staticmethod
    def parse(row):
        """A ``CarrierRecord`` from one CSV row (a dict by the file's own header), or None when unusable."""
        import hashlib
        from .telnyx import parse_amount
        if not isinstance(row, dict):
            return None
        direction = str(row.get('direction') or '').strip().lower()
        if direction not in ('inbound', 'outbound'):
            return None
        started, finished = _time(row.get('start_time')), _time(row.get('end_time'))
        if started is None:
            return None
        cli, cld = _number(row.get('callerid')), _number(row.get('destination'))
        seconds = _seconds(str(row.get('duration') or '').split('.')[0])
        billed = _seconds(str(row.get('billed_duration') or '').split('.')[0])
        answered = finished - timedelta(seconds=seconds) if finished is not None and seconds else None
        identity = hashlib.sha256('|'.join((direction, str(row.get('start_time')), str(row.get('end_time')),
                                            cli or '', cld or '')).encode('utf-8')).hexdigest()[:40]
        cost = str(row.get('total_cost') or '').strip()
        priced = parse_amount(cost, 'USD') if cost else None  # Flowroute bills in US dollars and names no currency
        micros, raw, currency = priced if priced is not None else (None, None, None)
        return CarrierRecord(id=f'flowroute-{identity}', direction=direction, sip_call_id=None, cli=cli, cld=cld,
                             started_at=started, answered_at=answered, finished_at=finished, call_seconds=seconds,
                             billed_seconds=billed, amount_micros=micros, raw_amount=raw, currency=currency)

    def _read_file(self, client, url):
        import csv
        import gzip
        import io
        from urllib.parse import urlsplit
        parts = urlsplit(url) if isinstance(url, str) else None
        if parts is None or parts.scheme != 'https' or not (parts.hostname or '').endswith('.amazonaws.com'):
            raise CarrierUnavailable('Flowroute named a call-record file Faxbot does not fetch.')
        try:
            response = client.get(url)
        except httpx.HTTPError:
            raise CarrierUnavailable('Flowroute\'s call-record file could not be reached.') from None
        if response.status_code != 200 or len(response.content) > self.MAX_COMPRESSED:
            raise CarrierUnavailable('Flowroute\'s call-record file could not be read.')
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as handle:
                text = handle.read(self.MAX_TEXT + 1)
        except (OSError, EOFError):
            text = response.content  # served already uncompressed
        if len(text) > self.MAX_TEXT:
            raise CarrierUnavailable('Flowroute\'s call-record file is larger than Faxbot reads.')
        try:
            rows = csv.DictReader(io.StringIO(text.decode('utf-8-sig')))
            return [record for record in map(self.parse, rows) if record is not None]
        except (UnicodeDecodeError, csv.Error):
            raise CarrierUnavailable('Flowroute\'s call-record file could not be read.') from None

    def _now(self):
        from .database import utcnow
        return self.clock() if self.clock is not None else utcnow()

    def fetch(self, start, end):
        """``(records, complete)`` for calls that started in ``[start, end)``, once Flowroute's export is ready."""
        access, secret = self._keys()
        if not (access and secret):
            raise CarrierUnavailable('No Flowroute API key is set.')
        window = self._days(start, end)
        now = self._now()
        for key in list(self.exports):
            item = self.exports[key]
            if item['records'] is not None and now - item['at'] > self.KEEP:
                del self.exports[key]  # read again later: Flowroute may correct a call's cost
        found = next((item for key, item in self.exports.items() if key[0] <= window[0] and window[1] <= key[1]), None)
        headers = {'Accept': 'application/vnd.api+json', 'Content-Type': 'application/vnd.api+json'}
        with self.client_factory() as client:
            if found is None:
                body = {'data': {'type': 'cdrexport', 'attributes': {'filter_parameters': {
                    'start_call_start_time': window[0].strftime('%Y-%m-%d %H:%M:%S'),
                    'start_call_end_time': window[1].strftime('%Y-%m-%d %H:%M:%S')}}}}
                answer = self._call(client, 'POST', self.URL, (access, secret), headers, body)
                export = str(answer.get('id') or '')
                if not _SID.fullmatch(export):
                    raise CarrierUnavailable('Flowroute returned an unusable answer.')
                if len(self.exports) >= 8:
                    self.exports.pop(next(iter(self.exports)))
                found = self.exports[window] = {'id': export, 'records': None, 'at': now}
            polls = 0
            while found['records'] is None:
                answer = self._call(client, 'GET', f"{self.URL}/{found['id']}", (access, secret), headers)
                status = str((answer.get('attributes') or {}).get('status') or '').lower()
                if status == 'failed':
                    self.exports = {key: item for key, item in self.exports.items() if item is not found}
                    raise CarrierUnavailable('Flowroute could not prepare the call records; Faxbot will ask again.')
                if status == 'completed':
                    found['records'] = self._read_file(client, (answer.get('attributes') or {}).get('download_url'))
                    found['at'] = now
                    break
                polls += 1
                if polls >= self.POLLS:
                    self.preparing = True
                    raise ExportPreparing('Flowroute is preparing the call records; Faxbot will ask again shortly.')
                self.sleep(self.WAIT)
        self.preparing = False
        return [record for record in found['records'] if start <= record.started_at < end], True

    @staticmethod
    def _call(client, method, url, auth, headers, body=None):
        try:
            response = client.request(method, url, auth=auth, headers=headers,
                                      content=None if body is None else json_text(body))
        except httpx.HTTPError:
            raise CarrierUnavailable('Flowroute could not be reached.') from None
        if response.status_code == 429:
            raise CarrierRateLimited('Flowroute asked Faxbot to slow down.')
        if response.status_code not in (200, 201, 202):
            raise CarrierUnavailable('Flowroute did not answer the call-record request.')
        try:
            data = response.json().get('data')
        except (ValueError, AttributeError):
            data = None
        if not isinstance(data, dict):
            raise CarrierUnavailable('Flowroute returned an unusable answer.')
        return data


class ExportPreparing(CarrierUnavailable):
    """The carrier is still preparing its call records; nothing is wrong."""


def json_text(body):
    import json
    return json.dumps(body, separators=(',', ':'))


READERS = {'signalwire': SignalWireCallRecords, 'flowroute': FlowrouteCallRecords}


_SHARED = {}


def _credentials(preset, values):
    if preset == 'signalwire':
        return (str(getattr(values, 'signalwire_space_url', '') or ''),
                str(getattr(values, 'signalwire_project_id', '') or ''),
                str(getattr(values, 'signalwire_api_token', '') or ''))
    if preset == 'flowroute':
        return (str(getattr(values, 'flowroute_access_key', '') or ''),
                str(getattr(values, 'flowroute_secret_key', '') or ''))
    return None


def reader_for(preset, values, *, client_factory=None):
    """The call-record reader for a trunk preset other than Telnyx, or None when the carrier publishes none.

    One reader is kept per carrier and keys, so the background job and Check now share Flowroute's pending
    export. A test passing its own ``client_factory`` gets a reader of its own.
    """
    import hashlib
    current = values() if callable(values) else values
    keys = _credentials(preset, current)
    if keys is None:
        return None
    if client_factory is not None:
        return READERS[preset](lambda: keys, client_factory=client_factory)
    identity = hashlib.sha256(repr((preset, keys)).encode('utf-8')).hexdigest()
    reader = _SHARED.get(identity)
    if reader is None:
        if len(_SHARED) >= 8:
            _SHARED.pop(next(iter(_SHARED)))
        reader = _SHARED[identity] = READERS[preset](lambda: keys)
    return reader


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
                       if preset == 'signalwire' else
                       'add your Flowroute API access key and secret key under Providers → Flowroute.'
                       if preset == 'flowroute' else 'add its API credentials.'))
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
        source = reader_for(preset, current)
        key = (preset, id(source))
        if key not in reconcilers:
            # A new carrier or new keys: start again, keeping no back-off from the earlier ones.
            reconcilers.clear()
            reconcilers[key] = CarrierReconciler(store, routes, source, preset=preset, numbers=numbers)
        return reconcilers[key].step()
    return ('faxbot-other-carrier-charges', repeat(step, interval=60.0, initial_delay=55.0,
                                                   warning='Carrier call charges are temporarily unavailable.'))
