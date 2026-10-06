"""Receiving faxes from eFax by asking eFax's API for them (polling).

eFax keeps each received fax until it is downloaded. Faxbot asks for the faxes
eFax lists as not downloaded yet, every ``EFAX_POLL_SECONDS`` (60 by default),
a page of 100 at a time and at most ``MAX_PAGES`` pages per check:

1. A fax Faxbot has not seen begins one durable import (source ``efax``, the
   account digest from ``efax_service.account_key``, eFax's fax ID) before
   anything is downloaded, because reading a fax's image is what marks it
   downloaded in eFax. A fax Faxbot already knows is left to its own retry
   schedule; listing it again never moves that schedule.
2. The received-fax worker leases the import and calls ``acquire``: it checks
   that the eFax account in settings is the one the fax arrived on, asks eFax
   for the fax by ID, downloads the document as PDF (a TIFF is converted),
   validates and stores it. A restart in between resumes from the import.
3. Only once the document is stored does Faxbot tell eFax it was downloaded
   and, when the person turned it on, delete it from eFax. A deletion eFax
   refuses is retried: its state (pending, attempts, since, next try) is one
   ``inbound_provider_deletions`` row keyed by the import, and each check
   retries the due ones on the same schedule as checking (doubling up to 30
   minutes) for seven days, then says the fax has to be deleted in eFax. A
   deletion that succeeds marks the row deleted. The import's ``report`` (what
   eFax listed) is never written again after the import begins.

Nothing here follows an address from a reply; every request goes to the one
eFax API host with the configured account. A notification eFax posts to
``/efax-inbound`` (signed with ``EFAX_WEBHOOK_SECRET``) only makes the next
check start now; its contents are never used. A failure is one plain sentence
and the next check waits longer, up to 30 minutes, or as long as eFax asks.
"""
import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
import os
import tempfile

import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..efax_service import (EfaxBusy, EfaxCredentialsError, EfaxError, EfaxFaxService, EfaxNotFound, PAGE_LIMIT,
                            PENDING_DELETION_NOTE, STOPPED_DELETION_NOTE, account_key,
                            deletion_sentences)  # noqa: F401 - deletion_sentences is used by callers
from .acquisition import (AcquisitionError, InvalidDocument, account_identity, discard, history_table,
                          parse_source_time, store_document)
from .fetch import FetchError


SOURCE = 'efax'
PROVIDER = 'eFax'
MAX_PAGES = 10
MAX_BACKOFF_SECONDS = 1800
# A notification never starts checks closer together than this.
MIN_GAP_SECONDS = 5.0
DELETE_FOR = timedelta(days=7)
OPEN = ('pending', 'stopped')
_TIFF_MAGIC = (b'II*\x00', b'MM\x00*')


@dataclass
class CheckResult:
    listed: int = 0
    added: int = 0
    finished: int = 0


def service_for(values, *, transport=None):
    """The adapter for the eFax account in settings; never from a reply or a notification."""
    return EfaxFaxService(app_id=values.efax_app_id, api_key=values.efax_api_key, user_id=values.efax_user_id,
                          caller_id=values.efax_caller_id, csid=values.efax_csid, transport=transport)


def account_for(values):
    return account_identity(SOURCE, account_key(values.efax_app_id, values.efax_user_id))


def receiving_active(values):
    """Receiving is on, eFax is the receiving provider and its credentials are set."""
    return bool(values.inbound_enabled and values.effective_inbound == SOURCE and values.efax_app_id
                and values.efax_api_key and values.efax_user_id)


def _number(value):
    """eFax writes numbers as digits with their country code (1 and ten digits in North America)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit() and len(text) >= 11:
        return '+' + text
    return text or None


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and 0 <= value <= 100000 else None


def _deletions(store):
    table = history_table(store.engine, 'inbound_provider_deletions')
    if table is None:
        raise AcquisitionError('Received-fax storage is not ready; upgrade the database.')
    return table


def _find(store, account, fax_id):
    imports, deletions = store.imports, _deletions(store)
    with store.engine.connect() as connection:
        row = connection.execute(sa.select(imports.c.id, imports.c.state).where(
            imports.c.source == SOURCE, imports.c.account == account, imports.c.operation_id == fax_id,
            imports.c.revision == '')).mappings().first()
        if row is None:
            return None
        mark = connection.execute(sa.select(deletions).where(deletions.c.id == row['id'],
                                                             deletions.c.state.in_(OPEN))).mappings().first()
    return {**dict(row), 'mark': _mark(mark)}


def _mark(row):
    if row is None or row['state'] not in OPEN:
        return None
    return {'state': row['state'], 'attempts': row['attempts'], 'since': row['since'], 'next_at': row['next_at']}


def _report(item):
    return {'listed_by': 'efax', 'fax': {name: item.get(name) for name in (
        'fax_id', 'completed_timestamp', 'pages', 'size', 'duration', 'originating_fax_tsid')}}


async def check_once(store, values, *, service=None, kick=None):
    """List received faxes not downloaded yet; begin an import for each new one."""
    service = service or service_for(values)
    account = account_for(values)
    result = CheckResult()
    for page in range(MAX_PAGES):
        items = await service.list_received(offset=page * PAGE_LIMIT)
        result.listed += len(items)
        for item in items:
            fax_id = item['fax_id']
            found = await run_lifecycle_step(lambda: _find(store, account, fax_id))
            if found is None:
                await run_lifecycle_step(lambda: store.begin(
                    source=SOURCE, account=account, operation_id=fax_id, backend=SOURCE, inbound_backend=SOURCE,
                    to_number=_number(item.get('destination_fax_number')),
                    from_number=_number(item.get('originating_fax_number')),
                    reported_pages=_int(item.get('pages')), report=_report(item),
                    source_received_at=parse_source_time(item.get('completed_timestamp')),
                    country=values.fax_default_country))
                result.added += 1
            elif found['state'] in ('received', 'conflict'):
                # Stored earlier, but eFax still lists it as not downloaded: say so again.
                await finish(service, fax_id, values, delete=found['state'] == 'received', store=store,
                             import_id=found['id'], marked=found['mark'])
                result.finished += 1
        if len(items) < PAGE_LIMIT:
            break
    if result.added and kick is not None:
        kick()
    return result


async def finish(service, fax_id, values, *, delete, store, import_id, marked=None):
    """Tell eFax the fax was downloaded, then delete it there when the person turned that on.

    Neither step changes the stored fax. A refused deletion is marked on the
    import and retried by later checks; a fax eFax still lists as not
    downloaded is marked downloaded at the next check.
    """
    try:
        await service.mark_downloaded(fax_id)
    except (EfaxError, ValueError):
        logging.getLogger(__name__).warning('Faxbot stored a received fax but could not tell eFax it was downloaded.')
    if delete and (values.efax_delete_after_download or marked is not None):
        await _delete(service, store, import_id, fax_id, marked)


def _write_mark(store, import_id, mark):
    """Record the deletion state, or with None that the fax was deleted; the import itself is never written."""
    imports, deletions = store.imports, _deletions(store)
    now = store.clock()
    with store.store.transaction() as connection:
        current = connection.execute(sa.select(deletions.c.state).where(deletions.c.id == import_id)).first()
        if mark is None:
            if current is not None:
                connection.execute(deletions.update().where(deletions.c.id == import_id).values(
                    state='deleted', next_at=None, updated_at=now))
            return
        values = dict(state=mark['state'], attempts=mark['attempts'], since=mark['since'],
                      next_at=mark.get('next_at') if mark['state'] == 'pending' else None, updated_at=now)
        if current is None:
            inbound_fax_id = connection.execute(sa.select(imports.c.inbound_fax_id).where(
                imports.c.id == import_id)).scalar_one()
            connection.execute(deletions.insert().values(id=import_id, inbound_fax_id=inbound_fax_id,
                                                         created_at=now, **values))
        else:
            connection.execute(deletions.update().where(deletions.c.id == import_id).values(**values))


async def _delete(service, store, import_id, fax_id, marked):
    """One deletion attempt; a refusal schedules the next one, success removes the mark."""
    now = store.clock()
    try:
        await service.delete_fax(fax_id)  # eFax no longer having the fax also counts as deleted
    except (EfaxError, ValueError):
        attempts = int((marked or {}).get('attempts') or 0) + 1
        since = (marked or {}).get('since') or now.replace(microsecond=0)
        if now - since >= DELETE_FOR:
            mark = {'state': 'stopped', 'attempts': attempts, 'since': since}
        else:
            wait = timedelta(seconds=min(MAX_BACKOFF_SECONDS, 60 * 2 ** (attempts - 1)))
            mark = {'state': 'pending', 'attempts': attempts, 'since': since,
                    'next_at': (now + wait).replace(microsecond=0)}
        await run_lifecycle_step(lambda: _write_mark(store, import_id, mark))
        logging.getLogger(__name__).warning('Faxbot stored a received fax but eFax did not delete it; '
                                            'Faxbot will try again.' if mark['state'] == 'pending' else
                                            'Faxbot stopped trying to delete a received fax from eFax.')
        _audit('efax_delete_failed', fax_id)
        return False
    if marked is not None:
        await run_lifecycle_step(lambda: _write_mark(store, import_id, None))
    _audit('efax_deleted', fax_id)
    return True


def _marked_rows(store, account=None):
    imports, deletions = store.imports, _deletions(store)
    query = (sa.select(imports.c.id, imports.c.operation_id, imports.c.account, deletions)
             .select_from(imports.join(deletions, deletions.c.id == imports.c.id))
             .where(imports.c.source == SOURCE, imports.c.state == 'received', deletions.c.state.in_(OPEN)))
    if account is not None:
        query = query.where(imports.c.account == account)
    with store.engine.connect() as connection:
        rows = connection.execute(query.order_by(imports.c.created_at, imports.c.id)).mappings().all()
    return [(row['id'], row['operation_id'], row['account'], _mark(row)) for row in rows]


async def retry_deletions(store, values, *, service=None, limit=50):
    """Retry the due deletions for the eFax account in settings; returns how many were deleted.

    A pending deletion of a fax that arrived on another eFax account (the app
    ID or user ID changed since) can never be retried with these settings, so
    it is marked stopped and reads "delete it in your eFax account".
    """
    service = service or service_for(values)
    account = account_for(values)
    now = store.clock()
    rows = await run_lifecycle_step(lambda: _marked_rows(store))
    deleted = attempted = 0
    for import_id, fax_id, row_account, mark in rows:
        if mark['state'] != 'pending':
            continue
        if row_account != account:
            stopped = {key: value for key, value in mark.items() if key != 'next_at'}
            stopped['state'] = 'stopped'
            await run_lifecycle_step(lambda: _write_mark(store, import_id, stopped))
            continue
        due = mark.get('next_at')
        if (due is not None and due > now) or attempted >= limit:
            continue
        attempted += 1
        if await _delete(service, store, import_id, fax_id, mark):
            deleted += 1
    return deleted


def deletion_counts(store):
    """(still pending, given up) deletions across every eFax import."""
    rows = _marked_rows(store)
    pending = sum(1 for *_, mark in rows if mark['state'] == 'pending')
    return pending, len(rows) - pending


def deletion_note(state):
    """The sentence for one received fax whose deletion from eFax is still open (pending or stopped), or None."""
    if state not in OPEN:
        return None
    return PENDING_DELETION_NOTE if state == 'pending' else STOPPED_DELETION_NOTE


def _pdf_from(data, inbound_fax_id, directory):
    """The stored artifact for eFax's document: a PDF as sent, a TIFF converted first."""
    if not data.startswith(_TIFF_MAGIC):
        return store_document(data, inbound_fax_id, provider=PROVIDER)
    from ..conversion import DocumentConversionError, tiff_to_pdf
    tiff_fd, tiff_path = tempfile.mkstemp(dir=directory, prefix='.inbound-', suffix='.tiff')
    pdf_fd, pdf_path = tempfile.mkstemp(dir=directory, prefix='.inbound-', suffix='.pdf')
    os.close(pdf_fd)
    try:
        with os.fdopen(tiff_fd, 'wb') as handle:
            handle.write(data)
        try:
            tiff_to_pdf(tiff_path, pdf_path)
        except DocumentConversionError:
            raise InvalidDocument('eFax sent a fax image Faxbot cannot turn into a PDF.') from None
        with open(pdf_path, 'rb') as handle:
            converted = handle.read()
    finally:
        for path in (tiff_path, pdf_path):
            try:
                os.unlink(path)
            except OSError:
                pass
    return store_document(converted, inbound_fax_id, provider=PROVIDER)


async def acquire(store, claim, values, *, service=None):
    """Fetch, check and store one leased eFax import; raises FetchError with a plain sentence."""
    if account_for(values) != claim['account'] or not (values.efax_app_id and values.efax_api_key
                                                         and values.efax_user_id):
        raise FetchError('The eFax account in settings changed after this fax arrived, so Faxbot cannot fetch it.')
    service = service or service_for(values)
    fax_id = claim['operation_id']
    try:
        details = await service.get_received_fax(fax_id)
        data = await service.download_received_fax(fax_id)
    except EfaxCredentialsError:
        raise FetchError('eFax refused the app ID, API key or user ID in settings.') from None
    except EfaxBusy:
        raise FetchError('eFax asked Faxbot to wait; Faxbot will try again.') from None
    except (EfaxNotFound, EfaxError) as error:
        raise FetchError(str(error)) from None
    except ValueError:
        raise FetchError('eFax fax ID is invalid.') from None
    inbound_id = claim['inbound_fax_id']
    artifact = await run_lifecycle_step(lambda: _pdf_from(data, inbound_id, values.fax_data_dir))
    completion = await run_lifecycle_step(lambda: store.complete(
        claim['id'], artifact_path=artifact.path, digest=artifact.digest, size=artifact.size,
        pages=artifact.pages, media_type=artifact.media_type,
        source_received_at=parse_source_time(details.get('completed_timestamp'))))
    discard(artifact, completion)
    if completion.state in ('received', 'conflict'):
        await finish(service, fax_id, values, delete=completion.state == 'received', store=store,
                     import_id=claim['id'])


class EfaxReceiver:
    """Checks eFax for received faxes while eFax is the receiving provider."""

    def __init__(self, store, frame, *, kick=None, sleep=asyncio.sleep):
        """``frame()`` returns ``(values, profiles)`` for the active configuration revision."""
        self.store, self.frame, self.kick, self.sleep = store, frame, kick, sleep
        self.failures = 0
        self.last_checked = None
        self.last_problem = None
        # While eFax asked Faxbot to wait (HTTP 429), a notification does not start a check.
        self.hold_until = 0.0
        self.loop = None
        self.event = None
        self.nudged = 0

    async def step(self):
        """One check; returns the seconds to wait before the next one."""
        values, _ = await run_lifecycle_step(self.frame)
        interval = values.efax_poll_seconds
        held = self.hold_until - _monotonic()
        if held > 0:
            return held
        if not receiving_active(values):
            self.failures = 0
            return interval
        try:
            await check_once(self.store, values, kick=self.kick)
            await retry_deletions(self.store, values)
        except EfaxBusy as busy:
            self.failures += 1
            self.last_problem = str(busy)
            wait = min(MAX_BACKOFF_SECONDS, max(interval * 2 ** self.failures, busy.retry_after or 0))
            self.hold_until = _monotonic() + wait
            return wait
        except (EfaxError, AcquisitionError, ValueError) as error:
            self.failures += 1
            self.last_problem = str(error)
            logging.getLogger(__name__).warning('Faxbot could not check eFax for received faxes: %s', error)
            return min(MAX_BACKOFF_SECONDS, interval * 2 ** self.failures)
        from .acquisition import utcnow
        self.failures, self.last_problem, self.last_checked = 0, None, utcnow()
        return interval

    def nudge(self):
        """A signed eFax notification arrived: start the next check now (thread-safe)."""
        self.nudged += 1
        loop, event = self.loop, self.event
        if loop is None or event is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    async def run(self, *, initial_delay=5.0):
        self.loop, self.event = asyncio.get_running_loop(), asyncio.Event()
        await self.sleep(initial_delay)
        while True:
            started = _monotonic()
            try:
                wait = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning('Checking eFax for received faxes is temporarily unavailable.')
                self.failures += 1
                wait = min(MAX_BACKOFF_SECONDS, 60 * 2 ** self.failures)
            try:
                await asyncio.wait_for(self.event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            self.event.clear()
            gap = MIN_GAP_SECONDS - (_monotonic() - started)
            if gap > 0:
                await self.sleep(gap)


def _monotonic():
    import time
    return time.monotonic()


def signature_valid(secret, body, signature):
    """eFax's X-HMAC-Signature: the hex HMAC-SHA256 of the raw body with the shared secret.

    eFax's samples disagree (one compares decoded bytes, one compares hex
    text); both describe a hex digest, which is what is checked here, in
    constant time and ignoring letter case.
    """
    import hashlib
    import hmac
    if not secret or not isinstance(body, (bytes, bytearray)) or not isinstance(signature, str):
        return False
    given = signature.strip().lower()
    if len(given) != 64 or any(character not in '0123456789abcdef' for character in given):
        return False
    expected = hmac.new(secret.encode('utf-8'), bytes(body), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def _audit(event, fax_id):
    try:
        from ..audit import audit_event
        audit_event(event, backend=SOURCE, provider_fax_id=fax_id)
    except Exception:
        pass
