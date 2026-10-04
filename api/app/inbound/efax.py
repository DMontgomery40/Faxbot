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
   and, when the person turned it on, delete it from eFax.

Nothing here follows an address from a reply; every request goes to the one
eFax API host with the configured account. A failure is one plain sentence
and the next check waits longer, up to 30 minutes, or as long as eFax asks.
"""
import asyncio
from dataclasses import dataclass
import logging
import os
import tempfile

import sqlalchemy as sa

from ..config_runtime import run_lifecycle_step
from ..efax_service import (EfaxBusy, EfaxCredentialsError, EfaxError, EfaxFaxService, EfaxNotFound, PAGE_LIMIT,
                            account_key)
from .acquisition import (AcquisitionError, InvalidDocument, account_identity, discard, parse_source_time,
                          store_document)
from .fetch import FetchError


SOURCE = 'efax'
PROVIDER = 'eFax'
MAX_PAGES = 10
MAX_BACKOFF_SECONDS = 1800
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
        row = connection.execute(sa.select(imports.c.id, imports.c.state, imports.c.artifact_digest).where(
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
                await finish(service, fax_id, values, delete=found['state'] == 'received')
                result.finished += 1
        if len(items) < PAGE_LIMIT:
            break
    if result.added and kick is not None:
        kick()
    return result


async def finish(service, fax_id, values, *, delete):
    """Tell eFax the fax was downloaded, then delete it there when the person turned that on.

    Neither step affects the stored fax. A failure is logged as one sentence;
    a fax eFax still lists as not downloaded is finished at the next check.
    """
    try:
        await service.mark_downloaded(fax_id)
    except (EfaxError, ValueError):
        logging.getLogger(__name__).warning('Faxbot stored a received fax but could not tell eFax it was downloaded.')
    if not (delete and values.efax_delete_after_download):
        return
    try:
        await service.delete_fax(fax_id)
    except (EfaxError, ValueError):
        logging.getLogger(__name__).warning('Faxbot stored a received fax but could not delete it from eFax; '
                                            'it stays in the eFax account.')
        _audit('efax_delete_failed', fax_id)
    else:
        _audit('efax_deleted', fax_id)


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
        await finish(service, fax_id, values, delete=completion.state == 'received')


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
