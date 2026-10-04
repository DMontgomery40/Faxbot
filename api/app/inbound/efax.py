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
   refuses is retried: the import's report keeps ``efax_delete`` (pending,
   attempts, since, next try) and each check retries the due ones on the same
   schedule as checking (doubling up to 30 minutes) for seven days, then says
   the fax has to be deleted in eFax. A deletion that succeeds removes the mark.

Nothing here follows an address from a reply; every request goes to the one
eFax API host with the configured account. A failure is one plain sentence
and the next check waits longer, up to 30 minutes, or as long as eFax asks.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
import os
import tempfile

import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..efax_service import (EfaxBusy, EfaxCredentialsError, EfaxError, EfaxFaxService, EfaxNotFound, PAGE_LIMIT,
                            PENDING_DELETION_NOTE, STOPPED_DELETION_NOTE, account_key,
                            deletion_sentences)  # noqa: F401 - deletion_sentences is used by callers
from .acquisition import (AcquisitionError, InvalidDocument, account_identity, discard, parse_source_time,
                          store_document)
from .fetch import FetchError


SOURCE = 'efax'
PROVIDER = 'eFax'
MAX_PAGES = 10
MAX_BACKOFF_SECONDS = 1800
DELETE_FOR = timedelta(days=7)
MARK = 'efax_delete'
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


def _find(store, account, fax_id):
    imports = store.imports
    with store.engine.connect() as connection:
        row = connection.execute(sa.select(imports.c.id, imports.c.state, imports.c.report).where(
            imports.c.source == SOURCE, imports.c.account == account, imports.c.operation_id == fax_id,
            imports.c.revision == '')).mappings().first()
    return dict(row) if row is not None else None


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
                             import_id=found['id'], marked=deletion_mark(found['report']))
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


def deletion_mark(report):
    """The pending or stopped deletion recorded in an import's report, or None."""
    try:
        data = json.loads(report or '{}')
    except (TypeError, ValueError):
        return None
    mark = data.get(MARK) if isinstance(data, dict) else None
    return mark if isinstance(mark, dict) and mark.get('state') in ('pending', 'stopped') else None


def _when(value):
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _write_mark(store, import_id, mark):
    """Record (or with None remove) the deletion mark; every other report field stays as it was."""
    imports = store.imports
    now = store.clock()
    with store.store.transaction() as connection:
        report = connection.execute(sa.select(imports.c.report).where(imports.c.id == import_id)).scalar_one_or_none()
        try:
            data = json.loads(report or '{}')
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict):
            data = {}
        if mark is None:
            if MARK not in data:
                return
            data.pop(MARK)
        else:
            data[MARK] = mark
        connection.execute(imports.update().where(imports.c.id == import_id).values(
            report=json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True), updated_at=now))


async def _delete(service, store, import_id, fax_id, marked):
    """One deletion attempt; a refusal schedules the next one, success removes the mark."""
    now = store.clock()
    try:
        await service.delete_fax(fax_id)  # eFax no longer having the fax also counts as deleted
    except (EfaxError, ValueError):
        attempts = int((marked or {}).get('attempts') or 0) + 1
        since = _when((marked or {}).get('since')) or now
        if now - since >= DELETE_FOR:
            mark = {'state': 'stopped', 'attempts': attempts, 'since': since.isoformat(timespec='seconds')}
        else:
            wait = timedelta(seconds=min(MAX_BACKOFF_SECONDS, 60 * 2 ** (attempts - 1)))
            mark = {'state': 'pending', 'attempts': attempts, 'since': since.isoformat(timespec='seconds'),
                    'next_at': (now + wait).isoformat(timespec='seconds')}
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
    imports = store.imports
    query = sa.select(imports.c.id, imports.c.operation_id, imports.c.account, imports.c.report).where(
        imports.c.source == SOURCE, imports.c.state == 'received', imports.c.report.like('%' + MARK + '%'))
    if account is not None:
        query = query.where(imports.c.account == account)
    with store.engine.connect() as connection:
        rows = connection.execute(query.order_by(imports.c.created_at, imports.c.id)).mappings().all()
    return [(row['id'], row['operation_id'], row['account'], mark) for row in rows
            for mark in [deletion_mark(row['report'])] if mark is not None]


async def retry_deletions(store, values, *, service=None, limit=50):
    """Retry the due deletions for the eFax account in settings; returns how many were deleted."""
    service = service or service_for(values)
    account = account_for(values)
    now = store.clock()
    rows = await run_lifecycle_step(lambda: _marked_rows(store, account))
    deleted = 0
    for import_id, fax_id, _, mark in rows[:limit]:
        due = _when(mark.get('next_at'))
        if mark['state'] != 'pending' or (due is not None and due > now):
            continue
        if await _delete(service, store, import_id, fax_id, mark):
            deleted += 1
    return deleted


def deletion_counts(store):
    """(still pending, given up) deletions across every eFax import."""
    rows = _marked_rows(store)
    pending = sum(1 for *_, mark in rows if mark['state'] == 'pending')
    return pending, len(rows) - pending


def deletion_note(report):
    """The sentence for one received fax whose deletion from eFax is still open, or None."""
    mark = deletion_mark(report)
    if mark is None:
        return None
    return PENDING_DELETION_NOTE if mark['state'] == 'pending' else STOPPED_DELETION_NOTE


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

    async def step(self):
        """One check; returns the seconds to wait before the next one."""
        values, _ = await run_lifecycle_step(self.frame)
        interval = values.efax_poll_seconds
        if not receiving_active(values):
            self.failures = 0
            return interval
        try:
            await check_once(self.store, values, kick=self.kick)
            await retry_deletions(self.store, values)
        except EfaxBusy as busy:
            self.failures += 1
            self.last_problem = str(busy)
            return min(MAX_BACKOFF_SECONDS, max(interval * 2 ** self.failures, busy.retry_after or 0))
        except (EfaxError, AcquisitionError, ValueError) as error:
            self.failures += 1
            self.last_problem = str(error)
            logging.getLogger(__name__).warning('Faxbot could not check eFax for received faxes: %s', error)
            return min(MAX_BACKOFF_SECONDS, interval * 2 ** self.failures)
        from .acquisition import utcnow
        self.failures, self.last_problem, self.last_checked = 0, None, utcnow()
        return interval

    async def run(self, *, initial_delay=5.0):
        await self.sleep(initial_delay)
        while True:
            try:
                wait = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning('Checking eFax for received faxes is temporarily unavailable.')
                self.failures += 1
                wait = min(MAX_BACKOFF_SECONDS, 60 * 2 ** self.failures)
            await self.sleep(wait)


def _audit(event, fax_id):
    try:
        from ..audit import audit_event
        audit_event(event, backend=SOURCE, provider_fax_id=fax_id)
    except Exception:
        pass
