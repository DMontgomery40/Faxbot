"""Faxes a provider billed that Faxbot has no record of: the unrecorded-fax sweep (B2).

A provider bills every fax it carried for an account, including faxes sent
from the provider's own website and faxes whose record Faxbot lost (a crash
between the provider accepting a fax and Faxbot saving its answer, or a
received-fax notification that never arrived). For each provider account
whose provider publishes a list of faxes, Faxbot lists the account's faxes in
a time window and keeps every fax it has no record of:

- a sent fax is Faxbot's when an attempt or its cost record carries the
  provider's fax ID (``outbound_attempts.provider_sid``,
  ``delivery_attempt_costs.provider_sid``);
- a received fax is Faxbot's when an import carries it
  (``inbound_imports.operation_id`` from the same provider).

Only a complete listing proves a fax is missing, so a listing with more pages
than Faxbot reads keeps nothing. The window ends ``GRACE`` before now, so a
notification still on its way is not taken for a missing fax. A test fax is
left out. A sent fax to a number Faxbot was unsure about around that time (an
attempt whose outcome is uncertain, which may have gone without its fax ID) is
kept, and said to be possibly that fax.

Everything here is read only. The sweep never resends, fetches or changes a
fax, and never creates a received fax: it lists what the provider billed so
the administrator can look. Kept rows are append-only
(``provider_unrecorded_faxes``, migration 0051): a later listing with a
different status or price adds a version, and a fax Faxbot records later (a
late notification) is left out when read, never deleted.

Sources, read 2026-10-07 and 2026-10-08 (see each listing class): the Sinch
Fax API v3 "List faxes", Phaxio API v2.1 "List faxes" and the SignalWire
Compatibility API "List all faxes". HumbleFax, eFax Corporate and Documo are
listed in ``UNSUPPORTED`` with what each publishes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import logging
import re
from uuid import uuid4

import httpx
import sqlalchemy as sa

from .database import DeliveryStoreError, read_connection, reflect, utcnow, write_transaction


GRACE = timedelta(minutes=30)
WINDOW = timedelta(days=3)
EVERY = timedelta(hours=6)
NEAR = timedelta(hours=1)
MAX_DAYS = 31
# Tests replace this with an ``httpx.MockTransport``; production uses the network.
_TRANSPORT = None
_ID = re.compile(r'[A-Za-z0-9_-]{1,100}', re.ASCII)
_CURRENCY = re.compile(r'[A-Za-z]{3}', re.ASCII)
_HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', re.ASCII)


class ListingUnavailable(RuntimeError):
    """The provider did not answer the listing; nothing changed."""


@dataclass(frozen=True)
class ListedFax:
    """One fax a provider listed for an account."""
    id: str
    direction: str                 # 'sent' or 'received'
    from_number: str | None
    to_number: str | None
    time: datetime | None          # naive UTC: when the provider created it
    pages: int | None
    status: str | None
    amount_micros: int | None      # None: no price (unknown, never zero)
    raw_amount: str | None
    currency: str | None
    test: bool = False


def _time(value):
    """Naive UTC from RFC 3339 / ISO 8601 (with Z or an offset) or RFC 2822 text; None when unusable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.replace(microsecond=0)


def _text(value, limit=64):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _pages(value):
    return value if type(value) is int and 0 <= value <= 100_000 else None


def _iso(moment):
    return moment.replace(microsecond=0).isoformat() + 'Z'


def _decimal_micros(text, *, negate_charge=False):
    """``(micros, raw)`` from a decimal amount; a provider's negative charge reads as its size."""
    from .charges import _micros
    if not isinstance(text, (str, int)) or isinstance(text, bool):
        return None, None
    micros = _micros(text)
    return (None, None) if micros is None else (micros, str(text).strip()[:32])


class _Listing:
    provider_id = None
    label = None
    PAGE_SIZE = 100

    def __init__(self, own, *, timeout=15.0, client_factory=None, max_pages=20):
        """``own``: the account's configuration values (``accounts.account_values``)."""
        self.own = own
        self.timeout = timeout
        self.max_pages = max_pages
        self.client_factory = client_factory or (lambda: httpx.Client(timeout=self.timeout, follow_redirects=False,
                                                                      transport=_TRANSPORT))

    def _get(self, client, url, *, params=None, auth=None):
        try:
            response = client.get(url, params=params, auth=auth, headers={'Accept': 'application/json'})
        except httpx.HTTPError:
            raise ListingUnavailable(f'{self.label} could not be reached.') from None
        if response.status_code in (401, 403):
            raise ListingUnavailable(f'{self.label} refused the account key.')
        if response.status_code != 200:
            raise ListingUnavailable(f'{self.label} did not answer the fax listing.')
        try:
            body = response.json()
        except ValueError:
            raise ListingUnavailable(f'{self.label} returned an unusable answer.') from None
        if not isinstance(body, dict):
            raise ListingUnavailable(f'{self.label} returned an unusable answer.')
        return body


class SinchListing(_Listing):
    """Sinch Fax API v3 "List faxes": ``GET /v3/projects/{projectId}/faxes``.

    Basic auth with the access key; filtered by ``createTime>=`` and ``createTime<=`` (sent as ``createTime>`` and
    ``createTime<`` with the value after "=", the documented form; full or partial RFC 3339 times), paged by
    ``pageSize`` (at most 1000) and ``page`` with ``totalPages`` in the answer (``faxes``, ``totalItems``,
    ``pageSize``, ``page``, ``totalPages``); ``format`` is left out, so the answer is JSON; each fax has ``id``, ``direction``
    (OUTBOUND or INBOUND), ``from``, ``to``, ``numberOfPages``, ``status``, ``createTime`` and ``price``
    (``amount``, ``currencyCode``) once the final price is calculated (Sinch Fax API v3 reference, read
    2026-10-08).
    """
    provider_id, label = 'sinch', 'Sinch'
    FINAL = ('COMPLETED', 'FAILURE')

    def _account(self):
        from ..sinch_service import SinchFaxService
        project = str(getattr(self.own, 'sinch_project_id', '') or '').strip()
        key = str(getattr(self.own, 'sinch_api_key', '') or '').strip()
        secret = str(getattr(self.own, 'sinch_api_secret', '') or '').strip()
        base = str(getattr(self.own, 'sinch_base_url', '') or SinchFaxService.DEFAULT_BASES[0]).strip().rstrip('/')
        return project, key, secret, base

    def ready(self):
        project, key, secret, _ = self._account()
        return bool(re.fullmatch(r'[A-Za-z0-9-]{1,64}', project, re.ASCII) and key and secret)

    @staticmethod
    def parse(item):
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not _ID.fullmatch(item['id']):
            return None
        direction = {'OUTBOUND': 'sent', 'INBOUND': 'received'}.get(str(item.get('direction') or '').upper())
        if direction is None:
            return None
        status = str(item.get('status') or '').upper() or None
        micros = raw = currency = None
        price = item.get('price')
        if status in SinchListing.FINAL and isinstance(price, dict) and isinstance(price.get('amount'), str):
            unit = price.get('currencyCode')
            if isinstance(unit, str) and _CURRENCY.fullmatch(unit):
                micros, raw = _decimal_micros(price['amount'])
                currency = unit.upper() if micros is not None else None
        return ListedFax(item['id'], direction, _text(item.get('from')), _text(item.get('to')),
                         _time(item.get('createTime')), _pages(item.get('numberOfPages')), status, micros, raw,
                         currency)

    def fetch(self, start, end):
        from ..inbound.fetch import FetchError, SINCH_HOSTS, require_host
        project, key, secret, base = self._account()
        if not self.ready():
            raise ListingUnavailable('Sinch is not fully set up for this account.')
        url = f'{base}/projects/{project}/faxes'
        try:
            require_host(url, SINCH_HOSTS, 'Sinch')
        except FetchError:
            raise ListingUnavailable('The Sinch address in settings is not a Sinch API address.') from None
        found, page = [], 1
        with self.client_factory() as client:
            while True:
                body = self._get(client, url, auth=(key, secret), params={
                    'createTime>': _iso(start), 'createTime<': _iso(end), 'pageSize': self.PAGE_SIZE, 'page': page})
                items = body.get('faxes')
                if not isinstance(items, list):
                    raise ListingUnavailable('Sinch returned an unusable answer.')
                found.extend(fax for fax in map(self.parse, items) if fax is not None)
                pages = body.get('totalPages')
                if not items or (type(pages) is int and page >= pages) or (type(pages) is not int
                                                                          and len(items) < self.PAGE_SIZE):
                    return [fax for fax in found if fax.time is None or start <= fax.time < end], True
                if page >= self.max_pages:
                    return found, False
                page += 1


class PhaxioListing(_Listing):
    """Phaxio API v2.1 "List faxes": ``GET https://api.phaxio.com/v2.1/faxes``.

    Basic auth with the API key and secret; filtered by ``created_after`` and ``created_before`` (RFC 3339), paged
    by ``per_page`` (largest not documented) and ``page`` with ``paging`` (``total``, ``per_page``, ``page``) in the
    answer; each fax has ``id``,
    ``direction`` (sent or received), ``num_pages``, ``status``, ``is_test``, ``created_at``, ``caller_id``,
    ``from_number``, ``to_number``, ``recipients`` (each with ``phone_number``) and ``cost`` in cents (Phaxio API
    v2.1 reference, read 2026-10-08).
    """
    provider_id, label = 'phaxio', 'Phaxio'
    URL = 'https://api.phaxio.com/v2.1/faxes'
    FINAL = ('success', 'failure')

    def ready(self):
        return bool(str(getattr(self.own, 'phaxio_api_key', '') or '').strip()
                    and str(getattr(self.own, 'phaxio_api_secret', '') or '').strip())

    @staticmethod
    def parse(item):
        if not isinstance(item, dict):
            return None
        identity = str(item.get('id')) if type(item.get('id')) is int or isinstance(item.get('id'), str) else ''
        direction = {'sent': 'sent', 'received': 'received'}.get(item.get('direction'))
        if not re.fullmatch(r'[0-9]{1,20}', identity) or direction is None:
            return None
        status = _text(item.get('status'), 32)
        cost = item.get('cost')
        priced = status in PhaxioListing.FINAL and type(cost) is int and 0 <= cost <= 1_000_000
        to = _text(item.get('to_number'))
        if direction == 'sent' and to is None:
            recipients = item.get('recipients') if isinstance(item.get('recipients'), list) else []
            numbers = [_text(entry.get('phone_number')) for entry in recipients if isinstance(entry, dict)]
            to = ', '.join(number for number in numbers if number)[:64] or None
        return ListedFax(identity, direction, _text(item.get('from_number')) or _text(item.get('caller_id')), to,
                         _time(item.get('created_at')), _pages(item.get('num_pages')), status,
                         cost * 10_000 if priced else None, f'{cost} cents' if priced else None,
                         'USD' if priced else None, test=item.get('is_test') is True)

    def fetch(self, start, end):
        if not self.ready():
            raise ListingUnavailable('Phaxio is not fully set up for this account.')
        auth = (str(self.own.phaxio_api_key).strip(), str(self.own.phaxio_api_secret).strip())
        found, page = [], 1
        with self.client_factory() as client:
            while True:
                body = self._get(client, self.URL, auth=auth, params={
                    'created_after': _iso(start), 'created_before': _iso(end), 'per_page': self.PAGE_SIZE,
                    'page': page})
                items = body.get('data') if body.get('success') is True else None
                if not isinstance(items, list):
                    raise ListingUnavailable('Phaxio returned an unusable answer.')
                found.extend(fax for fax in map(self.parse, items) if fax is not None)
                paging = body.get('paging') if isinstance(body.get('paging'), dict) else {}
                # Phaxio names no largest page, so the page size it answered with counts, not the one asked for.
                total, size = paging.get('total'), paging.get('per_page')
                size = size if type(size) is int and size > 0 else self.PAGE_SIZE
                if not items or (type(total) is int and page * size >= total) or (
                        type(total) is not int and len(items) < size):
                    return [fax for fax in found if fax.time is None or start <= fax.time < end], True
                if page >= self.max_pages:
                    return found, False
                page += 1


class SignalWireListing(_Listing):
    """SignalWire Compatibility API "List all faxes": ``GET https://{space}/api/laml/2010-04-01/Accounts/{project}/Faxes.json``.

    Basic auth with the project ID and API token; filtered by ``DateCreatedAfter`` and ``DateCreatedOnOrBefore``,
    paged by ``PageSize`` with ``next_page_uri`` in the answer; each fax has ``sid``, ``direction`` (outbound or
    inbound), ``from``, ``to``, ``num_pages``, ``status``, ``date_created`` (RFC 2822), ``price`` (decimal text,
    negative for a charge, null until priced) and ``price_unit`` (SignalWire Compatibility API reference, read
    2026-10-08).
    """
    provider_id, label = 'signalwire', 'SignalWire'
    FINAL = ('delivered', 'received', 'failed', 'no-answer', 'busy', 'canceled')

    def _account(self):
        space = str(getattr(self.own, 'signalwire_space_url', '') or '').strip().rstrip('/')
        space = re.sub(r'^https?://', '', space)
        project = str(getattr(self.own, 'signalwire_project_id', '') or '').strip()
        token = str(getattr(self.own, 'signalwire_api_token', '') or '').strip()
        return space, project, token

    def ready(self):
        space, project, token = self._account()
        return bool(_HOST.fullmatch(space) and space.endswith('.signalwire.com') and _ID.fullmatch(project)
                    and token)

    @staticmethod
    def parse(item):
        if not isinstance(item, dict) or not isinstance(item.get('sid'), str) or not _ID.fullmatch(item['sid']):
            return None
        direction = {'outbound': 'sent', 'inbound': 'received'}.get(str(item.get('direction') or '').lower())
        if direction is None:
            return None
        status = _text(item.get('status'), 32)
        micros = raw = currency = None
        unit = item.get('price_unit')
        if item.get('price') is not None and isinstance(unit, str) and _CURRENCY.fullmatch(unit):
            micros, raw = _decimal_micros(item.get('price'))
            currency = unit.upper() if micros is not None else None
        return ListedFax(item['sid'], direction, _text(item.get('from')), _text(item.get('to')),
                         _time(item.get('date_created')), _pages(item.get('num_pages')), status, micros, raw,
                         currency)

    def fetch(self, start, end):
        space, project, token = self._account()
        if not self.ready():
            raise ListingUnavailable('SignalWire is not fully set up for this account.')
        url = f'https://{space}/api/laml/2010-04-01/Accounts/{project}/Faxes.json'
        params = {'DateCreatedAfter': _iso(start), 'DateCreatedOnOrBefore': _iso(end), 'PageSize': self.PAGE_SIZE}
        found, pages = [], 0
        with self.client_factory() as client:
            while True:
                body = self._get(client, url, auth=(project, token), params=params)
                items = body.get('faxes')
                if not isinstance(items, list):
                    raise ListingUnavailable('SignalWire returned an unusable answer.')
                found.extend(fax for fax in map(self.parse, items) if fax is not None)
                pages += 1
                following = body.get('next_page_uri')
                if not following:
                    return [fax for fax in found if fax.time is None or start <= fax.time < end], True
                if pages >= self.max_pages or not isinstance(following, str) or not following.startswith('/api/laml/'):
                    return found, False
                # Only ever the same host: the next page's path, never an address the answer names.
                url, params = f'https://{space}{following}', None


LISTINGS = {'sinch': SinchListing, 'phaxio': PhaxioListing, 'signalwire': SignalWireListing}

# Providers that publish no list of an account's faxes Faxbot can read, with what they publish instead.
UNSUPPORTED = {
    'humblefax': ('HumbleFax lists received faxes for its own app but publishes no charge per fax; its plan is a '
                  'monthly fee, so enter its invoice under Costs → Invoices.'),
    'efax': ('eFax Corporate publishes no charge per fax and no list of sent faxes Faxbot can read; enter its '
             'invoice under Costs → Invoices.'),
    'documo': ('Documo publishes no charge per fax; enter its invoice under Costs → Invoices.'),
    'freeswitch': 'Your own FreeSWITCH places the calls; your carrier bills them.',
    'sip': 'The carrier trunk is checked call by call against your carrier\'s call records.',
}


def listing_for(account, values, *, client_factory=None):
    """The listing for one account, or None when its provider publishes none."""
    from ..accounts import AccountsError, account_values
    kind = LISTINGS.get(account.provider)
    if kind is None:
        return None
    try:
        own = account_values(values, account.key)
    except AccountsError:
        return None
    return kind(own, client_factory=client_factory)


# Storage ----------------------------------------------------------------------------------------------------

class SweepStore:
    TABLES = ('provider_fax_sweeps', 'provider_unrecorded_faxes', 'outbound_attempts', 'inbound_imports',
              'fax_jobs', 'delivery_attempt_costs')

    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, self.TABLES)
        self.sweeps, self.faxes = tables['provider_fax_sweeps'], tables['provider_unrecorded_faxes']
        self.attempts, self.imports, self.jobs = tables['outbound_attempts'], tables['inbound_imports'], tables['fax_jobs']
        self.costs = tables['delivery_attempt_costs']

    def known(self, provider_id, faxes):
        """The listed fax IDs Faxbot has a record of: an attempt or its cost record carrying a sent one, an import a
        received one."""
        sent = [fax.id for fax in faxes if fax.direction == 'sent']
        received = [fax.id for fax in faxes if fax.direction == 'received']
        found = set()
        with read_connection(self.engine) as connection:
            for chunk in _chunks(sent):
                found.update(connection.execute(sa.select(self.attempts.c.provider_sid).where(
                    self.attempts.c.provider_sid.in_(chunk))).scalars())
                # The fax ID a cost record kept for an attempt (a route decided before submission keeps it there).
                found.update(connection.execute(sa.select(self.costs.c.provider_sid).where(
                    self.costs.c.provider_sid.in_(chunk))).scalars())
            for chunk in _chunks(received):
                found.update(connection.execute(sa.select(self.imports.c.operation_id).where(
                    self.imports.c.source == provider_id, self.imports.c.operation_id.in_(chunk))).scalars())
        return found

    def last_sweep(self, account_key):
        table = self.sweeps
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(table).where(table.c.account_key == account_key)
                                     .order_by(table.c.started_at.desc()).limit(1)).mappings().one_or_none()
        return dict(row) if row is not None else None

    def latest_sweeps(self):
        table = self.sweeps
        found = {}
        with read_connection(self.engine) as connection:
            for row in connection.execute(sa.select(table).order_by(table.c.started_at)).mappings():
                found[row['account_key']] = dict(row)
        return found

    def record(self, *, account_key, provider_id, start, end, outcome, listed, missing, started_at, now):
        """Record one sweep and the faxes it found missing; returns the sweep row."""
        sweep = dict(id=uuid4().hex, account_key=account_key, provider_id=provider_id, window_start=start,
                     window_end=end, outcome=outcome, listed=listed, unrecorded=len(missing), started_at=started_at,
                     finished_at=now)
        table = self.faxes
        with write_transaction(self.engine) as connection:
            connection.execute(self.sweeps.insert().values(**sweep))
            for fax in missing:
                rows = connection.execute(sa.select(table).where(
                    table.c.account_key == account_key, table.c.provider_fax_id == fax.id)
                    .order_by(table.c.version)).mappings().all()
                content = (fax.status, fax.amount_micros, fax.currency, fax.pages)
                if rows and (rows[-1]['status'], rows[-1]['amount_micros'], rows[-1]['currency'],
                             rows[-1]['pages']) == content:
                    continue
                connection.execute(table.insert().values(
                    id=uuid4().hex, sweep_id=sweep['id'], account_key=account_key, provider_id=provider_id,
                    provider_fax_id=fax.id, direction=fax.direction, from_number=fax.from_number,
                    to_number=fax.to_number, provider_time=fax.time, pages=fax.pages, status=fax.status,
                    amount_micros=fax.amount_micros, raw_amount=fax.raw_amount, currency=fax.currency,
                    version=len(rows) + 1, created_at=now))
        return sweep


def _chunks(items, size=500):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def unrecorded(engine, *, account_key=None, start=None, end=None):
    """The latest version of each kept fax that Faxbot still has no record of, oldest first.

    ``start``/``end`` bound the provider's time for the fax. A sent fax near an attempt whose outcome is uncertain,
    to the same number, carries ``uncertain_job`` (that fax's ID): it may be that fax.
    """
    try:
        store = SweepStore(engine)
    except DeliveryStoreError:
        return []
    table = store.faxes
    query = sa.select(table)
    if account_key is not None:
        query = query.where(table.c.account_key == account_key)
    if start is not None:
        query = query.where(table.c.provider_time >= start)
    if end is not None:
        query = query.where(table.c.provider_time < end)
    with read_connection(engine) as connection:
        rows = connection.execute(query.order_by(table.c.version)).mappings().all()
    latest = {}
    for row in rows:
        latest[(row['account_key'], row['provider_fax_id'])] = dict(row)
    if not latest:
        return []
    by_provider = {}
    for row in latest.values():
        by_provider.setdefault(row['provider_id'], []).append(
            ListedFax(row['provider_fax_id'], row['direction'], None, None, None, None, None, None, None, None))
    recorded = set()
    for provider_id, faxes in by_provider.items():
        recorded |= {(provider_id, identity) for identity in store.known(provider_id, faxes)}
    found = [row for row in latest.values() if (row['provider_id'], row['provider_fax_id']) not in recorded]
    _uncertain(store, found)
    return sorted(found, key=lambda row: (row['provider_time'] or row['created_at'], row['provider_fax_id']))


def _uncertain(store, rows):
    """Mark each sent fax that may be an attempt Faxbot was unsure about (same number, within an hour)."""
    from .carriers import same_number
    sent = [row for row in rows if row['direction'] == 'sent' and row['provider_time'] is not None]
    for row in rows:
        row['uncertain_job'] = None
    if not sent:
        return
    attempts, jobs = store.attempts, store.jobs
    earliest = min(row['provider_time'] for row in sent) - NEAR
    latest = max(row['provider_time'] for row in sent) + NEAR
    with read_connection(store.engine) as connection:
        candidates = connection.execute(sa.select(attempts.c.job_id, attempts.c.created_at, jobs.c.to_number)
                                        .select_from(attempts.join(jobs, jobs.c.id == attempts.c.job_id))
                                        .where(attempts.c.phase == 'uncertain', attempts.c.provider_sid.is_(None),
                                               attempts.c.created_at >= earliest,
                                               attempts.c.created_at <= latest)).all()
    for row in sent:
        for job_id, created, to_number in candidates:
            if abs((created - row['provider_time']).total_seconds()) <= NEAR.total_seconds() and \
                    same_number(to_number, row['to_number']):
                row['uncertain_job'] = job_id
                break


# The sweep --------------------------------------------------------------------------------------------------

@dataclass
class SweepResult:
    account_key: str
    provider_id: str
    label: str
    outcome: str          # complete, partial, unavailable, unsupported
    listed: int = 0
    unrecorded: int = 0
    sentence: str = ''

    def as_dict(self):
        return {'account_key': self.account_key, 'provider_id': self.provider_id, 'label': self.label,
                'outcome': self.outcome, 'listed': self.listed, 'unrecorded': self.unrecorded,
                'summary': self.sentence}


def _count(value, one, many=None):
    return f'{value} {one if value == 1 else (many or one + "s")}'


class ProviderSweep:
    """List each account's faxes at its provider and keep the ones Faxbot has no record of (read only)."""

    def __init__(self, engine, values, *, client_factory=None, window=WINDOW, every=EVERY, grace=GRACE):
        """``values()`` returns the active configuration values."""
        self.engine, self.values = engine, values
        self.client_factory = client_factory
        self.window, self.every, self.grace = window, every, grace
        self.paused_until = {}

    def accounts(self, values):
        from ..accounts import all_accounts
        return [account for account in all_accounts(values) if account.provider in LISTINGS and account.set_up]

    def step(self, *, now=None):
        """One background pass: sweep each account whose last sweep is older than ``every``."""
        now = (now or utcnow()).replace(microsecond=0)
        values = self.values()
        if values is None:
            return False
        store = SweepStore(self.engine)
        latest = store.latest_sweeps()
        for account in self.accounts(values):
            last = latest.get(account.key)
            if last is not None and now - last['started_at'] < self.every:
                continue
            if self.paused_until.get(account.key) and now < self.paused_until[account.key]:
                continue
            result = self.sweep_account(account, values, now - self.window, now, now=now, store=store)
            if result.outcome == 'unavailable':
                self.paused_until[account.key] = now + timedelta(hours=1)
        return False

    def run_now(self, *, account_key=None, days=7, now=None):
        """Sweep now (``faxbot costs charges sweep``): one account or every account that has a listing."""
        from ..accounts import account_named
        now = (now or utcnow()).replace(microsecond=0)
        values = self.values()
        days = max(1, min(int(days), MAX_DAYS))
        store = SweepStore(self.engine)
        if account_key is not None:
            account = account_named(values, account_key)
            if account is None:
                raise LookupError('Faxbot has no account with this key.')
            accounts = [account]
        else:
            accounts = self.accounts(values)
        return [self.sweep_account(account, values, now - timedelta(days=days), now, now=now, store=store)
                for account in accounts]

    def sweep_account(self, account, values, start, now_end, *, now, store=None):
        end = min(now_end, now - self.grace)
        listing = listing_for(account, values, client_factory=self.client_factory)
        label = account.label
        if listing is None:
            sentence = UNSUPPORTED.get(account.provider) or f'{label} publishes no list of faxes Faxbot can read.'
            return SweepResult(account.key, account.provider, label, 'unsupported', sentence=sentence)
        store = store or SweepStore(self.engine)
        if not listing.ready():
            return SweepResult(account.key, account.provider, label, 'unavailable',
                               sentence=f'{label} is not fully set up, so Faxbot could not list its faxes.')
        try:
            faxes, complete = listing.fetch(start, end)
        except ListingUnavailable as error:
            logging.getLogger(__name__).warning('A provider fax listing could not be read; Faxbot will try again '
                                                'later.')
            store.record(account_key=account.key, provider_id=account.provider, start=start, end=end,
                         outcome='unavailable', listed=0, missing=[], started_at=now, now=utcnow())
            return SweepResult(account.key, account.provider, label, 'unavailable',
                               sentence=f'{error} Faxbot will try again later.')
        faxes = [fax for fax in faxes if not fax.test]
        if not complete:
            store.record(account_key=account.key, provider_id=account.provider, start=start, end=end,
                         outcome='partial', listed=len(faxes), missing=[], started_at=now, now=utcnow())
            return SweepResult(account.key, account.provider, label, 'partial', listed=len(faxes),
                               sentence=f'{label} listed more faxes than Faxbot reads at once, so Faxbot could not '
                                        'check them all; try a shorter period.')
        known = store.known(account.provider, faxes)
        missing = [fax for fax in faxes if fax.id not in known]
        store.record(account_key=account.key, provider_id=account.provider, start=start, end=end, outcome='complete',
                     listed=len(faxes), missing=missing, started_at=now, now=utcnow())
        listed = _count(len(faxes), 'fax', 'faxes')
        if missing and len(missing) == len(faxes):
            sentence = f'{label} listed {listed}, and Faxbot has no record of {"it" if len(faxes) == 1 else "any"}.'
        elif missing:
            sentence = (f'{label} listed {listed}; Faxbot has no record of '
                        f'{"one" if len(missing) == 1 else len(missing)} of them.')
        else:
            sentence = (f'{label} listed {_count(len(faxes), "fax", "faxes")}, and Faxbot has a record of each.'
                        if faxes else f'{label} listed no faxes in this period.')
        return SweepResult(account.key, account.provider, label, 'complete', listed=len(faxes),
                           unrecorded=len(missing), sentence=sentence)


def fax_sentence(row, label, values=None):
    """One sentence for a kept fax: "Sinch billed a fax to +13035550100 on 3 Oct that Faxbot didn't send."."""
    from .costs import money_text
    when = ''
    if row.get('provider_time') is not None:
        from ..people_time import zone
        local = row['provider_time'].replace(tzinfo=timezone.utc).astimezone(
            zone(getattr(values, 'time_zone', '') or ''))
        when = f' on {local.day} {local:%b}'
    priced = row.get('amount_micros') is not None and row.get('currency')
    price = f' ({money_text(row["amount_micros"], row["currency"])})' if priced else ''
    if row['direction'] == 'sent':
        to = f' to {row["to_number"]}' if row.get('to_number') else ''
        if row.get('uncertain_job'):
            return (f'{label} {"billed" if priced else "lists"} a fax{to}{when}{price} that may be the fax Faxbot '
                    'was unsure about; check that fax in Sent before sending it again.')
        verb = 'billed' if priced else 'lists'
        return f"{label} {verb} a fax{to}{when}{price} that Faxbot didn't send."
    sender = f' from {row["from_number"]}' if row.get('from_number') else ''
    verb = 'billed' if priced else 'lists'
    return f"{label} {verb} a received fax{sender}{when}{price} that isn't in Received."
