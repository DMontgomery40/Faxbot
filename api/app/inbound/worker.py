"""Fetch, validate and store received documents in the background.

Each step recovers expired fetch leases, leases the import due longest and
acquires it: a Phaxio or Sinch fax is looked up and downloaded from the
provider's API with the configured account, a SIP fax is converted from its
retained TIFF. Any failure becomes one plain sentence and a scheduled retry;
the source stays for the next attempt.
"""
import asyncio
import logging
import os
from pathlib import Path

from ..config import use_configuration
from ..config_runtime import run_lifecycle_step
from .acquisition import (AcquisitionError, ImportStore, InvalidDocument, account_identity, convert_tiff,
                          discard, parse_source_time, store_document)
from .fetch import FetchError


_FAILED = {'failure', 'failed', 'error'}
_DONE = {'success', 'completed', ''}


class UnsafeSourcePath(AcquisitionError):
    pass


def inside_directory(path, directory):
    """Resolve a TIFF path that must sit inside ``directory`` with no symbolic link below it."""
    if not isinstance(path, str) or not path or '\x00' in path or not isinstance(directory, str) or not directory:
        raise UnsafeSourcePath('The received fax image path is not valid.')
    given = os.path.abspath(path)
    for base in (os.path.abspath(directory), os.path.realpath(directory)):
        if given.startswith(base.rstrip(os.sep) + os.sep):
            break
    else:
        raise UnsafeSourcePath('The received fax image must be inside the Faxbot data folder.')
    current = base
    for part in Path(os.path.relpath(given, base)).parts:
        current = os.path.join(current, part)
        if part in ('.', '..') or os.path.islink(current):
            raise UnsafeSourcePath('The received fax image path must not use a symbolic link.')
    resolved = os.path.realpath(given)
    root = os.path.realpath(directory)
    if not resolved.startswith(root.rstrip(os.sep) + os.sep):
        raise UnsafeSourcePath('The received fax image must be inside the Faxbot data folder.')
    return resolved


class Acquirer:
    def __init__(self, store: ImportStore, *, frame=None):
        """``frame()`` returns ``(values, profiles)`` for the active configuration revision."""
        self.store = store
        self.frame = frame

    async def step(self):
        """One unit of work; True when an import was attempted."""
        if not await run_lifecycle_step(self.store.has_work):
            return False
        if self.frame is None:
            return await self._step()
        values, profiles = await run_lifecycle_step(self.frame)
        with use_configuration(values, profiles):
            return await self._step()

    async def _step(self):
        await run_lifecycle_step(self.store.recover_expired)
        claim = await run_lifecycle_step(self.store.claim)
        if claim is None:
            return False
        await self.acquire(claim)
        return True

    async def acquire(self, claim):
        """Acquire one leased import; never raises for a provider or document problem."""
        try:
            await self._acquire(claim)
        except (FetchError, AcquisitionError) as error:
            await run_lifecycle_step(lambda: self.store.fail(claim['id'], str(error), claim_token=claim['claim_token']))
        except Exception:
            logging.getLogger(__name__).warning('A received fax could not be fetched; Faxbot will try again.')
            await run_lifecycle_step(lambda: self.store.fail(
                claim['id'], 'Faxbot could not fetch the document.', claim_token=claim['claim_token']))

    async def _acquire(self, claim):
        from ..config import settings
        source, inbound_id = claim['source'], claim['inbound_fax_id']
        source_time = None
        if source == 'sip':
            path = inside_directory(claim.get('tiff_path'), settings.fax_data_dir)
            artifact = await run_lifecycle_step(lambda: convert_tiff(path, inbound_id))
        elif source in ('phaxio', 'sinch'):
            service, name = provider_service(source, settings)
            if account_identity(source, _account_value(source, settings)) != claim['account']:
                raise FetchError(f'The {name} account in settings changed after this fax arrived, '
                                 'so Faxbot cannot fetch it.')
            metadata = await service.get_received_fax(claim['operation_id'])
            if metadata is None:
                raise FetchError(f'{name} has no received fax with this ID in the configured account.')
            if metadata['status'] in _FAILED:
                await run_lifecycle_step(lambda: self.store.abandon(
                    claim['id'], f'{name} reported that this fax did not arrive completely.'))
                return
            if metadata['status'] not in _DONE:
                raise FetchError(f'{name} is still receiving this fax.')
            source_time = parse_source_time(metadata.get('completed_at'))
            data = await service.download_received_fax(claim['operation_id'])
            artifact = await run_lifecycle_step(lambda: store_document(data, inbound_id, provider=name))
        else:
            raise AcquisitionError('This document has to be added again.')
        completion = await run_lifecycle_step(lambda: self.store.complete(
            claim['id'], artifact_path=artifact.path, digest=artifact.digest, size=artifact.size,
            pages=artifact.pages, media_type=artifact.media_type, source_received_at=source_time))
        discard(artifact, completion)


def _account_value(source, values):
    return values.phaxio_api_key if source == 'phaxio' else values.sinch_project_id


def provider_service(source, values):
    """A provider adapter built from configuration values; never from a notification."""
    if source == 'phaxio':
        from ..phaxio_service import PhaxioFaxService
        return PhaxioFaxService(api_key=values.phaxio_api_key, api_secret=values.phaxio_api_secret), 'Phaxio'
    from ..sinch_service import SinchFaxService
    return SinchFaxService(project_id=values.sinch_project_id, api_key=values.sinch_api_key,
                           api_secret=values.sinch_api_secret, base_url=values.sinch_base_url or None), 'Sinch'


async def run_forever(acquirer, wake, *, interval=5.0, initial_delay=1.0):
    """Repeat ``acquirer.step``; ``wake`` (an asyncio.Event) starts the next step at once."""
    await asyncio.sleep(initial_delay)
    while True:
        try:
            busy = await acquirer.step()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning('Received-fax fetching is temporarily unavailable.')
            busy = False
        if busy:
            continue
        try:
            await asyncio.wait_for(wake.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        wake.clear()
