"""Read what Telnyx charged for each SIP trunk call (read-only).

Source, read October 3, 2026: Telnyx API v2 "Search detail records",
``GET https://api.telnyx.com/v2/detail_records`` with
``filter[record_type]=sip-trunking``
(https://developers.telnyx.com/api-reference/detail-records/search-detail-records).
Call records are filtered by ``started_at`` (they have no ``created_at``),
with ``filter[started_at][gte]``/``[lt]``, and paged with ``page[number]`` and
``page[size]`` (at most 50). A read-only request with the owner's key on that
date confirmed these fields on each sip-trunking record: ``id``,
``sip_call_id``, ``direction`` (inbound/outbound), ``cli`` and ``cld``
(+E.164), ``started_at``/``answered_at``/``finished_at`` (UTC, ``Z``),
``call_sec``, ``billed_sec``, ``rate``, ``cost`` (decimal text, for example
"0.005") and ``currency``.

A record carries no revision or update time, so Faxbot dates each report by
when it read it. A missing or null cost is not yet priced and stays unknown;
"0" is a real zero. The key is only ever sent to api.telnyx.com and is never
logged, and response bodies are never logged.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
import re

import httpx


URL = 'https://api.telnyx.com/v2/detail_records'
PAGE_SIZE = 50
_RECORD_ID = re.compile(r'[A-Za-z0-9_.:-]{1,100}', re.ASCII)
_CALL_ID = re.compile(r'[!-~]{1,100}', re.ASCII)
_CURRENCY = re.compile(r'[A-Za-z]{3}', re.ASCII)
_TIME = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d{1,9})?(Z|[+-]00:?00)?', re.ASCII)


class CarrierUnavailable(RuntimeError):
    """The carrier did not answer a charge lookup; nothing about the call changed."""


class CarrierRateLimited(CarrierUnavailable):
    """The carrier asked Faxbot to slow down."""


@dataclass(frozen=True)
class CarrierRecord:
    """One carrier billing record for one telephone call."""
    id: str
    direction: str
    sip_call_id: str | None
    cli: str | None
    cld: str | None
    started_at: datetime
    answered_at: datetime | None
    finished_at: datetime | None
    call_seconds: int | None
    billed_seconds: int | None
    amount_micros: int | None  # None: not priced yet (unknown, never zero)
    raw_amount: str | None
    currency: str | None


def _time(value):
    if not isinstance(value, str):
        return None
    match = _TIME.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None


def _seconds(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if type(value) is int and 0 <= value <= 86_400 else None


def _text(value, limit=32):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def parse_amount(cost, currency):
    """``(micros, raw text, currency)`` for a priced record, or None while it is unknown.

    The carrier's sign is kept (a credit stays negative); amounts are rounded
    up to whole millionths, in the carrier's favour, like an invoice.
    """
    if cost is None or isinstance(cost, bool) or (isinstance(cost, str) and not cost.strip()):
        return None
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency.strip()) is None:
        return None
    raw = str(cost).strip()
    try:
        amount = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or abs(amount) > 10_000 or len(raw) > 32:
        return None
    micros = int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    return micros, raw, currency.strip().upper()


def parse_record(payload):
    """A ``CarrierRecord`` from one detail record, or None when it is not a usable call record."""
    if not isinstance(payload, dict) or payload.get('record_type') != 'sip-trunking':
        return None
    record_id = payload.get('id')
    direction = payload.get('direction')
    started = _time(payload.get('started_at'))
    if (not isinstance(record_id, str) or _RECORD_ID.fullmatch(record_id) is None
            or direction not in ('inbound', 'outbound') or started is None):
        return None
    call_id = payload.get('sip_call_id')
    priced = parse_amount(payload.get('cost'), payload.get('currency'))
    micros, raw, currency = priced if priced is not None else (None, None, None)
    return CarrierRecord(
        id=record_id, direction=direction,
        sip_call_id=call_id if isinstance(call_id, str) and _CALL_ID.fullmatch(call_id) else None,
        cli=_text(payload.get('cli')), cld=_text(payload.get('cld')), started_at=started,
        answered_at=_time(payload.get('answered_at')), finished_at=_time(payload.get('finished_at')),
        call_seconds=_seconds(payload.get('call_sec')), billed_seconds=_seconds(payload.get('billed_sec')),
        amount_micros=micros, raw_amount=raw, currency=currency)


def _iso(moment):
    return moment.replace(microsecond=0).isoformat() + 'Z'


class TelnyxDetailRecords:
    """Fetch every sip-trunking record that started inside a time window."""

    carrier = 'telnyx'
    label = 'Telnyx'

    def __init__(self, api_key, *, timeout=15.0, client_factory=None, max_pages=20):
        """``api_key()`` returns the current key, or '' when none is set."""
        self.api_key = api_key
        self.timeout = timeout
        self.max_pages = max_pages
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout))

    def ready(self):
        try:
            return bool(self.api_key())
        except Exception:
            return False

    def fetch(self, start, end):
        """``(records, complete)``; ``complete`` is False when the window had more pages than allowed."""
        key = self.api_key()
        if not key:
            raise CarrierUnavailable('No Telnyx API key is set.')
        headers = {'Authorization': f'Bearer {key}', 'Accept': 'application/json'}
        records, page = [], 1
        with self.client_factory() as client:
            while True:
                params = {'filter[record_type]': 'sip-trunking', 'filter[started_at][gte]': _iso(start),
                          'filter[started_at][lt]': _iso(end), 'page[size]': PAGE_SIZE, 'page[number]': page}
                try:
                    response = client.get(URL, params=params, headers=headers)
                except httpx.HTTPError:
                    raise CarrierUnavailable('Telnyx could not be reached.') from None
                if response.status_code == 429:
                    raise CarrierRateLimited('Telnyx asked Faxbot to slow down.')
                if response.status_code != 200:
                    raise CarrierUnavailable('Telnyx did not answer the charge lookup.')
                try:
                    body = response.json()
                except ValueError:
                    raise CarrierUnavailable('Telnyx returned an unusable answer.') from None
                data = body.get('data') if isinstance(body, dict) else None
                meta = body.get('meta') if isinstance(body, dict) else None
                if not isinstance(data, list):
                    raise CarrierUnavailable('Telnyx returned an unusable answer.')
                records.extend(record for record in map(parse_record, data) if record is not None)
                pages = meta.get('total_pages') if isinstance(meta, dict) else None
                if type(pages) is not int or page >= pages or not data:
                    return records, True
                if page >= self.max_pages:
                    return records, False
                page += 1
