"""Submit through the immutable provider and document frame accepted for a job."""
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from .config_runtime import run_lifecycle_step
from .outbound_worker import PreparationFailure, SubmissionReceipt
from .provider_execution import service_from_profile, ProviderExecutionError


def normalize_status(value):
    if not isinstance(value, str):
        raise ValueError('Unusable provider status.')
    status = value.lower()
    if status in {'success', 'completed', 'completed_ok', 'delivered'}:
        return 'success'
    if status in {'failed', 'failure', 'error', 'busy', 'no-answer'}:
        return 'failed'
    if status in {'cancelled', 'canceled'}:
        return 'cancelled'
    if status in {'queued', 'in_progress', 'in-progress', 'inprogress', 'pendingbatch', 'sending', 'processing', 'pending'}:
        return 'in_progress'
    raise ValueError('Unrecognized provider status requires reconciliation.')


def _receipt(result, *, manifest=False, sinch=False):
    if not isinstance(result, dict):
        raise ValueError('Unusable provider receipt.')
    if sinch:
        result = result.get('data', result)
        if not isinstance(result, dict):
            raise ValueError('Unusable provider receipt.')
    sid = result.get('job_id' if manifest else 'id' if sinch else 'provider_sid')
    if isinstance(sid, int) and not isinstance(sid, bool):
        sid = str(sid)
    if not isinstance(sid, str) or not sid:
        raise ValueError('Provider did not acknowledge a fax identity.')
    return SubmissionReceipt(sid, normalize_status(result.get('status', 'queued')))


@dataclass(repr=False)
class PreparedSubmission:
    claim: object
    profile: object
    job: dict = field(repr=False)
    pdf_path: str
    tiff_path: str | None
    service: object = field(repr=False)
    media_url: str | None = field(default=None, repr=False)
    ami: object = None

    async def submit(self):
        configuration = self.profile.configuration
        pid, to = configuration.provider_id, self.job['to_number']
        if configuration.manifest is not None:
            result = await self.service.send_fax(to=to, file_path=self.pdf_path, file_url=self.media_url,
                extra={'job_id': self.claim.job_id, 'attempt_id': self.claim.attempt_id})
            return _receipt(result, manifest=True)
        if pid in {'phaxio', 'signalwire'}:
            result = await self.service.send_fax(to, self.media_url, self.claim.job_id,
                attempt_id=self.claim.attempt_id)
            return _receipt(result)
        if pid == 'sinch':
            return _receipt(await self.service.send_fax_file(to, self.pdf_path), sinch=True)
        if pid == 'documo':
            return _receipt(await self.service.send_fax_file(to, self.pdf_path))
        if pid == 'humblefax':
            # The attempt identity lets an operator find this fax in HumbleFax history.
            return _receipt(await self.service.send_fax_file(to, self.pdf_path, uuid=self.claim.attempt_id))
        if pid == 'sip':
            await self.ami.originate_sendfax(self.claim.job_id, to, self.tiff_path,
                attempt_id=self.claim.attempt_id)
            return SubmissionReceipt(self.claim.job_id, 'in_progress')
        if pid == 'freeswitch':
            from .freeswitch_service import originate_txfax
            sid = await run_lifecycle_step(lambda: originate_txfax(to, self.tiff_path,
                self.claim.job_id, attempt_id=self.claim.attempt_id))
            return SubmissionReceipt(sid, 'in_progress')
        raise ValueError('Unsupported captured transport.')


class CapturedTransport:
    def __init__(self, store, runtime, *, ami=None):
        self.store, self.runtime, self.ami = store, runtime, ami

    @asynccontextmanager
    async def prepare(self, claim):
        revision, profile, job = await run_lifecycle_step(lambda: self.store.load_dispatch(claim))
        values = revision.values
        configuration = profile.configuration
        pid = configuration.provider_id
        if re.fullmatch('[a-f0-9]{32}', claim.job_id) is None:
            raise PreparationFailure('artifact_unavailable')
        root = Path(values.fax_data_dir)
        pdf = root / (claim.job_id + '.pdf')
        tiff = root / (claim.job_id + '.tiff') if configuration.traits.get('requires_tiff') is True else None
        if any(path.is_symlink() or not path.is_file() for path in (pdf, tiff) if path is not None):
            raise PreparationFailure('artifact_unavailable')
        service = None
        manifest = configuration.manifest
        if manifest is not None or pid not in {'sip', 'freeswitch'}:
            try:
                service = service_from_profile(profile)
                if not service.is_configured():
                    raise PreparationFailure('provider_unavailable')
                if manifest is None and pid in {'phaxio', 'signalwire'}:
                    from .callback_locator import callback_base_url, callback_url_with_locators
                    service.status_callback_url = callback_base_url(revision, profile)
                    # Validate now, before the durable marker authorizes I/O.
                    callback_url_with_locators(service.status_callback_url, claim.job_id, claim.attempt_id)
            except ProviderExecutionError:
                raise PreparationFailure('provider_unavailable') from None
            except ValueError:
                raise PreparationFailure('provider_unavailable') from None
        elif pid == 'sip' and (self.ami is None or not self.ami._connected.is_set()):
            raise PreparationFailure('provider_unavailable')
        elif pid == 'freeswitch':
            from .freeswitch_service import fs_cli_available
            if not fs_cli_available():
                raise PreparationFailure('provider_unavailable')
        if manifest is None and pid in {'sip', 'freeswitch'}:
            try:
                if pid == 'sip':
                    from .ami import prepare_originate_fields
                    prepare_originate_fields(claim.job_id, job['to_number'], str(tiff) if tiff else None,
                        caller_id=values.fax_station_id, header=values.fax_header,
                        attempt_id=claim.attempt_id)
                else:
                    from .freeswitch_service import build_originate_command
                    build_originate_command(job['to_number'], str(tiff) if tiff else None, claim.job_id,
                        gateway_name=values.fs_gateway_name, caller_id_number=values.fs_caller_id_number,
                        t38_enable=values.fs_t38_enable, attempt_id=claim.attempt_id)
            except ValueError:
                raise PreparationFailure('preparation_failed') from None
        if manifest is None and pid == 'humblefax':
            from .humblefax_service import humblefax_number
            try:
                humblefax_number(job['to_number'])
            except ValueError:
                # HumbleFax sends only to US/Canadian numbers; refuse before submission.
                raise PreparationFailure('preparation_failed') from None
        # Multipart manifests consume the already prepared local PDF. Other
        # HTTP templates can refer to the captured, tokenized media capability.
        needs_url = pid in {'phaxio', 'signalwire'} and manifest is None
        if manifest is not None:
            needs_url = service.m.actions['send_fax'].body_kind != 'multipart'
        media_url = None
        if needs_url:
            base = values.public_api_url.rstrip('/')
            try:
                parsed = urlsplit(base)
                parsed.port
            except ValueError:
                raise PreparationFailure('provider_unavailable') from None
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment
                    or any(ord(char) <= 32 or ord(char) == 127 for char in base)
                    or (values.enforce_public_https and parsed.scheme != 'https'
                        and parsed.hostname not in {'localhost', '127.0.0.1', '::1'})):
                raise PreparationFailure('provider_unavailable')
            token = secrets.token_urlsafe(32)
            media_url = base + '/fax/' + claim.job_id + '/pdf?token=' + token
            try:
                await run_lifecycle_step(lambda: self.store.grant_pdf(claim, url=media_url, token=token,
                    expires_at=datetime.utcnow() + timedelta(minutes=values.pdf_token_ttl_minutes)))
            except ValueError:
                raise PreparationFailure('provider_unavailable') from None
        with self.runtime.frame(revision):
            yield PreparedSubmission(claim, profile, job, str(pdf), str(tiff) if tiff else None,
                                     service, media_url, self.ami)
