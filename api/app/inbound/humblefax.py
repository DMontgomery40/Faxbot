"""Receiving faxes from HumbleFax by asking HumbleFax's API for them (polling).

HumbleFax keeps every received fax in the account and offers no "not fetched
yet" list, so Faxbot keeps track of which faxes it already has:

1. Every ``HUMBLEFAX_POLL_SECONDS`` (60 by default) Faxbot lists the faxes
   HumbleFax received since one day before the newest fax it already recorded
   for this HumbleFax user, or, before the first one, in the last 30 days
   (HumbleFax's own default window). It never asks for more than 30 days.
2. A listed fax whose ID Faxbot has not recorded begins one durable import
   (source ``humblefax``, account ``humblefax:<HumbleFax user ID>``, HumbleFax's
   fax ID) and nothing else. A fax Faxbot already recorded, in any state, is
   left alone: listing it again never resumes, reschedules or duplicates it.
3. The received-fax worker leases the import and calls ``acquire``: it checks
   that the keys in settings still belong to the HumbleFax user the fax arrived
   for, downloads the document as a PDF from HumbleFax's API host by fax ID,
   validates and stores it. A restart in between resumes from the import; a
   failed download retries on the import's schedule and keeps its history.

Faxbot never deletes or changes anything in HumbleFax, so every fax also stays
in the HumbleFax account. Receiving is on when receiving is on in Settings,
both HumbleFax keys are set, and either "Receive faxes from HumbleFax" is on or
HumbleFax is the receiving provider; it can run beside another receiving
provider.

Everything here works on one ``HumbleFaxAccount`` (a key, its credentials and
its receiving settings), never on the flat settings directly. Today
``accounts(values)`` gives the one account in settings under the key
``humblefax``; with several provider accounts (the provider-rules design, §3.4)
it gives one per HumbleFax account and one ``HumbleFaxReceiver`` runs for each.
Each received fax records its account key in its report (``account_key``) and
as its ``inbound_backend``, and is fetched with that account's credentials. A failure is one plain sentence, and the next check waits longer, up
to 30 minutes, or at least the minute HumbleFax blocks an address for.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import logging
import threading
import time

import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..humblefax_service import (HumbleFaxBusy, HumbleFaxCredentialsError, HumbleFaxError, HumbleFaxFaxService,
                                 HumbleFaxNotFound)
from .acquisition import AcquisitionError, account_identity, discard, parse_source_time, store_document, utcnow
from .fetch import FetchError


SOURCE = 'humblefax'
PROVIDER = 'HumbleFax'
# HumbleFax lists the last 30 days when asked for no window; Faxbot never asks for more.
LOOKBACK = timedelta(days=30)
# Each check lists again from a day before the newest recorded fax, for faxes HumbleFax lists late.
OVERLAP = timedelta(days=1)
MAX_BACKOFF_SECONDS = 1800
_USER_SECONDS = 3600
_LOOKUP_BATCH = 500
PARTIAL = 'partial fax received'
# Faxbot's times are naive UTC; HumbleFax's are Unix seconds.
_EPOCH = datetime(1970, 1, 1)

_users_lock = threading.Lock()
_users: dict[str, tuple[float, str]] = {}


@dataclass
class CheckResult:
    listed: int = 0
    added: int = 0


@dataclass(frozen=True)
class HumbleFaxAccount:
    """One HumbleFax account Faxbot can receive through: its key, credentials and receiving settings."""

    key: str = SOURCE
    access_key: str = field(default='', repr=False)
    secret_key: str = field(default='', repr=False)
    # Receiving through this account is turned on (the switch, or HumbleFax chosen as the receiving provider).
    receives: bool = False
    poll_seconds: int = 60

    def keys_set(self):
        return bool(self.access_key and self.secret_key)


def settings_account(values):
    """The one HumbleFax account in today's settings, under the key ``humblefax``."""
    return HumbleFaxAccount(SOURCE, values.humblefax_access_key, values.humblefax_secret_key,
                            receives=bool(values.humblefax_receive_enabled or values.effective_inbound == SOURCE),
                            poll_seconds=values.humblefax_poll_seconds)


def accounts(values):
    """Every HumbleFax account in settings; one today (provider accounts add more later)."""
    return (settings_account(values),)


def account_named(values, key):
    """The HumbleFax account with this key, or None when settings no longer have it."""
    return next((account for account in accounts(values) if account.key == key), None)


def service_for(account, *, transport=None):
    """The adapter for one account's keys; never from a reply."""
    return HumbleFaxFaxService(account.access_key, account.secret_key, transport=transport)


def inactive_reason(values, account=None):
    """Why Faxbot is not checking this HumbleFax account, in one sentence; None while it is."""
    account = settings_account(values) if account is None else account
    if not account.receives:
        return 'Receive faxes from HumbleFax is off.'
    if not values.inbound_enabled:
        return 'Receiving faxes is turned off in Settings, so Faxbot is not checking HumbleFax.'
    if not account.keys_set():
        return 'Add the HumbleFax access key and secret key so Faxbot can check HumbleFax.'
    return None


def receiving_active(values, account=None):
    """Receiving is on, the account's keys are set, and receiving through it is turned on."""
    return inactive_reason(values, account) is None


def forget_accounts():
    """Drop the remembered HumbleFax users (tests)."""
    with _users_lock:
        _users.clear()


async def account_for(account, service):
    """``humblefax:<user ID>`` for the account's keys, read with GetUser and kept an hour."""
    key = hashlib.sha256((account.access_key + ':' + account.secret_key).encode('utf-8')).hexdigest()
    with _users_lock:
        known = _users.get(key)
    if known is not None and time.monotonic() - known[0] < _USER_SECONDS:
        return known[1]
    user = await service.user()
    if user['inbound_access'] is False:
        raise HumbleFaxError('The HumbleFax user these keys belong to cannot see received faxes; '
                             'allow it in your HumbleFax account.')
    identity = account_identity(SOURCE, user['id'])
    with _users_lock:
        _users[key] = (time.monotonic(), identity)
    return identity


def _known(store, identity, fax_ids):
    """The fax IDs among ``fax_ids`` Faxbot already recorded for this HumbleFax user, in any state."""
    imports = store.imports
    found = set()
    with store.engine.connect() as connection:
        for start in range(0, len(fax_ids), _LOOKUP_BATCH):
            chunk = fax_ids[start:start + _LOOKUP_BATCH]
            found.update(connection.execute(sa.select(imports.c.operation_id).where(
                imports.c.source == SOURCE, imports.c.account == identity, imports.c.revision == '',
                imports.c.operation_id.in_(chunk))).scalars())
    return found


def _newest(store, identity):
    """When HumbleFax received the newest fax Faxbot recorded for this HumbleFax user, or None."""
    imports = store.imports
    with store.engine.connect() as connection:
        return connection.execute(sa.select(sa.func.max(imports.c.source_received_at)).where(
            imports.c.source == SOURCE, imports.c.account == identity)).scalar()


def window_start(newest, now):
    """The first moment to list: a day before the newest recorded fax, never more than 30 days back."""
    floor = now - LOOKBACK
    return floor if newest is None else max(newest - OVERLAP, floor)


def _report(item, account):
    return {'listed_by': SOURCE, 'account_key': account.key, 'fax': {name: item.get(name) for name in (
        'id', 'status', 'time', 'pages', 'transmission_seconds', 'bit_rate', 'sender_station')}}


def account_key_of(record):
    """The key of the HumbleFax account an import arrived on (its report); ``humblefax`` when it names none."""
    try:
        report = json.loads(record.get('report') or '{}')
    except (TypeError, ValueError):
        report = {}
    key = report.get('account_key') if isinstance(report, dict) else None
    return key if isinstance(key, str) and key else SOURCE


async def check_once(store, values, account=None, *, service=None, kick=None):
    """List the faxes one HumbleFax account received in the window; begin one import for each new one."""
    account = settings_account(values) if account is None else account
    service = service or service_for(account)
    identity = await account_for(account, service)
    newest = await run_lifecycle_step(lambda: _newest(store, identity))
    start = window_start(newest, store.clock())
    items = await service.list_received(time_from=int((start - _EPOCH).total_seconds()))
    result = CheckResult(listed=len(items))
    fax_ids = [item['id'] for item in items]
    known = await run_lifecycle_step(lambda: _known(store, identity, fax_ids))
    for item in items:
        if item['id'] in known:
            continue
        known.add(item['id'])
        if item.get('time') is None:
            try:
                item = await service.get_received(item['id'])  # listed without its details
            except HumbleFaxNotFound:
                continue
        await run_lifecycle_step(lambda: store.begin(
            source=SOURCE, account=identity, operation_id=item['id'], backend=SOURCE, inbound_backend=account.key,
            to_number=item.get('to_number'), from_number=item.get('from_number'),
            reported_pages=item.get('pages'), report=_report(item, account),
            source_received_at=parse_source_time(item.get('time')), country=values.fax_default_country))
        result.added += 1
    if result.added and kick is not None:
        kick()
    return result


async def acquire(store, claim, values, *, service=None):
    """Download, check and store one leased HumbleFax import with the credentials of the account it arrived on.

    Raises FetchError with a plain sentence.
    """
    account = account_named(values, account_key_of(claim))
    if account is None:
        raise FetchError('The HumbleFax account this fax arrived on is no longer in settings, so Faxbot cannot '
                         'fetch it.')
    if not account.keys_set():
        raise FetchError('Add the HumbleFax access key and secret key in settings so Faxbot can fetch this fax.')
    service = service or service_for(account)
    try:
        if await account_for(account, service) != claim['account']:
            raise FetchError('The HumbleFax keys in settings belong to a different HumbleFax user than the one '
                             'this fax arrived for, so Faxbot cannot fetch it.')
        data = await service.download_received(claim['operation_id'])
    except HumbleFaxCredentialsError as error:
        raise FetchError(str(error)) from None
    except HumbleFaxBusy:
        raise FetchError('HumbleFax asked Faxbot to slow down; Faxbot will try again.') from None
    except HumbleFaxError as error:
        raise FetchError(str(error)) from None
    except ValueError:
        raise FetchError('The HumbleFax fax ID is not valid.') from None
    inbound_id = claim['inbound_fax_id']
    artifact = await run_lifecycle_step(lambda: store_document(data, inbound_id, provider=PROVIDER))
    completion = await run_lifecycle_step(lambda: store.complete(
        claim['id'], artifact_path=artifact.path, digest=artifact.digest, size=artifact.size,
        pages=artifact.pages, media_type=artifact.media_type))
    discard(artifact, completion)


def partial_note(report):
    """The sentence for a fax HumbleFax says arrived only in part, or None."""
    fax = report.get('fax') if isinstance(report, dict) else None
    if isinstance(fax, dict) and fax.get('status') == PARTIAL:
        return 'HumbleFax received only part of this fax; ask the sender to send it again if pages are missing.'
    return None


class HumbleFaxReceiver:
    """Checks one HumbleFax account for received faxes while receiving through it is on."""

    def __init__(self, store, frame, *, account_key=SOURCE, kick=None, sleep=asyncio.sleep):
        """``frame()`` returns ``(values, profiles)`` for the active configuration revision; ``account_key``
        names the HumbleFax account (``accounts(values)``) this receiver checks."""
        self.store, self.frame, self.kick, self.sleep = store, frame, kick, sleep
        self.account_key = account_key
        self.failures = 0
        self.last_checked = None
        self.last_found = None
        self.last_problem = None
        # While HumbleFax blocks Faxbot's address (HTTP 429), nothing is asked of it.
        self.hold_until = 0.0
        self._lock = None
        self._lock_loop = None

    def held(self):
        """Seconds left before HumbleFax may be asked again; 0 when it may be asked now."""
        return max(0.0, self.hold_until - time.monotonic())

    def _checking(self):
        loop = asyncio.get_running_loop()
        if self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def account(self, values):
        """This receiver's account in the given settings, or None when settings no longer have it."""
        return account_named(values, self.account_key)

    def _interval(self, values, account):
        return account.poll_seconds if account is not None else values.humblefax_poll_seconds

    async def check(self, values):
        """One check now (the loop's or Check now's, never both at once); returns the seconds until the next."""
        account = self.account(values)
        interval = self._interval(values, account)
        async with self._checking():
            try:
                if account is None:
                    raise HumbleFaxError('This HumbleFax account is no longer in settings.')
                result = await check_once(self.store, values, account, kick=self.kick)
            except HumbleFaxBusy as busy:
                self.failures += 1
                self.last_problem = str(busy)
                wait = min(MAX_BACKOFF_SECONDS, max(interval * 2 ** self.failures, busy.retry_after))
                self.hold_until = time.monotonic() + wait
                return wait
            except (HumbleFaxCredentialsError, HumbleFaxError, AcquisitionError, ValueError) as error:
                self.failures += 1
                self.last_problem = str(error)
                logging.getLogger(__name__).warning('Faxbot could not check HumbleFax for received faxes: %s', error)
                return min(MAX_BACKOFF_SECONDS, interval * 2 ** self.failures)
            self.failures, self.last_problem = 0, None
            self.last_checked, self.last_found = utcnow(), result.added
            return interval

    async def step(self):
        """One scheduled check; returns the seconds to wait before the next one."""
        values, _ = await run_lifecycle_step(self.frame)
        account = self.account(values)
        interval = self._interval(values, account)
        held = self.held()
        if held > 0:
            return held
        if account is None or not receiving_active(values, account):
            self.failures = 0
            return interval
        return await self.check(values)

    def inactive_reason(self, values):
        """Why this receiver is not checking HumbleFax, in one sentence; None while it is."""
        account = self.account(values)
        return inactive_reason(values, account) if account is not None else 'Receive faxes from HumbleFax is off.'

    def status(self, values):
        """What the console and the command line say about receiving through this HumbleFax account."""
        reason = self.inactive_reason(values)
        account = self.account(values)
        return {'account': self.account_key, 'receiving': reason is None, 'reason': reason,
                'turned_on': bool(account is not None and account.receives),
                'receiving_provider': values.effective_inbound == SOURCE,
                'poll_seconds': self._interval(values, self.account(values)), 'checked_at': self.last_checked,
                'found': self.last_found, 'problem': self.last_problem if reason is None else None}

    async def run(self, *, initial_delay=5.0):
        await self.sleep(initial_delay)
        while True:
            try:
                wait = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning('Checking HumbleFax for received faxes is temporarily unavailable.')
                self.failures += 1
                wait = min(MAX_BACKOFF_SECONDS, 60 * 2 ** self.failures)
            await self.sleep(wait)
