"""Submit through the immutable provider and document frame accepted for a job."""
import asyncio
from contextlib import asynccontextmanager
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from .config_runtime import run_lifecycle_step
from .outbound_worker import PreparationFailure, SubmissionReceipt
from .provider_execution import service_from_profile, ProviderExecutionError


def _layout_rule(engine, job_id):
    """``pages.capability.long_pages_allowed``'s rule from the fax's envelope: 'allow', 'never' or None."""
    if engine is None:
        return None
    from .routing import envelope as envelopes
    try:
        pinned = envelopes.load(engine, job_id)
    except envelopes.UnreadableDecision:
        return 'never'  # the pages go as they are
    except Exception:
        return None
    layout = pinned.envelope.page_layout if pinned is not None else None
    return {'as_receiver_allows': 'allow', 'one_per_sheet': 'never'}.get(layout)


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
    # A SIP trunk fax the SSL Fax engine (HylaFAX+) places: its job, created
    # and waiting for submission; None when Faxbot's built-in engine places it.
    engine_job: object = field(default=None, repr=False)
    # Which engine places a SIP trunk fax and why, and this call's T.38 rung, speed and ECM.
    engine_choice: object = field(default=None, repr=False)
    call: object = field(default=None, repr=False)
    records: object = field(default=None, repr=False)
    # The trunk account a SIP fax goes over (its key); None is the first trunk (sip_trunk.py).
    trunk: str | None = None

    def _record_engine(self):
        """The engine record for this attempt; evidence only, never stops the fax."""
        if self.engine_choice is None or self.records is None:
            return
        from . import hylafax_records
        hylafax_records.safely(self.records.record_call, direction='outbound', call_key=self.claim.attempt_id,
                               job_id=self.claim.job_id, engine=self.engine_choice.engine,
                               reason=self.engine_choice.reason or None, number=self.job['to_number'])

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
            from .sinch_service import SinchRefused
            try:
                result = await self.service.send_fax_file(to, self.pdf_path)
            except SinchRefused as refused:
                # Sinch provably did not take the fax: a definite failure another route may send.
                return SubmissionReceipt(None, 'failed', error=refused.sentence)
            return _receipt(result, sinch=True)
        if pid == 'documo':
            return _receipt(await self.service.send_fax_file(to, self.pdf_path))
        if pid == 'humblefax':
            # The attempt identity lets an operator find this fax in HumbleFax history.
            return _receipt(await self.service.send_fax_file(to, self.pdf_path, uuid=self.claim.attempt_id))
        if pid == 'efax':
            # The attempt identity is eFax's client_reference_id, so a person can find this fax there.
            return _receipt(await self.service.send_fax_file(to, self.pdf_path, reference=self.claim.attempt_id))
        if pid == 'sip' and self.engine_job is not None:
            await asyncio.to_thread(self._record_engine)
            # The call record starts now, as for the built-in engine's calls.
            emit = getattr(self.ami, '_emit', None)
            if emit is not None and self.engine_job.submission:
                emit('Submission', self.engine_job.submission)
            # The engine dials once; a lost answer here leaves the fax uncertain, never sent again.
            await asyncio.to_thread(self.engine_job.submit)
            return SubmissionReceipt(self.claim.job_id, 'in_progress')
        if pid == 'sip':
            await asyncio.to_thread(self._record_engine)
            await self.ami.originate_sendfax(self.claim.job_id, to, self.tiff_path,
                attempt_id=self.claim.attempt_id, call=self.call, trunk=self.trunk)
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
        # A trunk account after the first names itself in its settings; its calls use its own endpoint.
        trunk = configuration.settings.get('trunk') if pid == 'sip' else None
        if re.fullmatch('[a-f0-9]{32}', claim.job_id) is None:
            raise PreparationFailure('artifact_unavailable')
        root = Path(values.fax_data_dir)
        pdf = root / (claim.job_id + '.pdf')
        tiff = root / (claim.job_id + '.tiff') if configuration.traits.get('requires_tiff') is True else None
        if any(path.is_symlink() or not path.is_file() for path in (pdf, tiff) if path is not None):
            raise PreparationFailure('artifact_unavailable')
        from .codec.send import transmitted_pdf
        pdf = transmitted_pdf(pdf, claim.job_id, pid)  # encoded pages made for this provider (experimental)
        if claim.members:
            if pid != 'sip' or configuration.manifest is not None:
                raise PreparationFailure('preparation_failed')
            # One call carries every fax in the claim: separator pages and each fax's own image.
            from .batching.transport import call_image
            from .pages.friendly import call_lightener
            # Each fax's shaded areas lightened when this call's route and recipient say so (pages/friendly.py).
            tiff = await run_lifecycle_step(lambda: call_image(self.store, root, claim, lighten=call_lightener(
                getattr(getattr(self.store, 'configuration', None), 'engine', None), values, pid,
                job.get('to_number'), root, claim)))
        from .routing.numbers import InvalidNumber, accepted_destination, is_canonical
        try:
            # Every adapter below receives this canonical number and only formats it.
            recipient = accepted_destination(job['to_number'], country=values.fax_default_country)
        except InvalidNumber:
            raise PreparationFailure('preparation_failed') from None
        # The fax keeps its recipient; the call goes to the number recorded for this attempt (an approved
        # alternate, ``routing.alternates``), or to the recipient's own number.
        dialed = job.get('dialed_number') or await run_lifecycle_step(
            lambda: self._choose_dialed(claim, job, recipient, values, pid))
        if not is_canonical(dialed):
            raise PreparationFailure('preparation_failed')
        job = {**job, 'to_number': dialed, 'recipient_number': recipient}
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
                    from .ami import originate_fields_for
                    originate_fields_for(values, claim.job_id, job['to_number'], str(tiff) if tiff else None,
                        attempt_id=claim.attempt_id, trunk=trunk)
                else:
                    from .freeswitch_service import build_originate_command
                    build_originate_command(job['to_number'], str(tiff) if tiff else None, claim.job_id,
                        gateway_name=values.fs_gateway_name, caller_id_number=values.fs_caller_id_number,
                        t38_enable=values.fs_t38_enable, attempt_id=claim.attempt_id)
            except ValueError:
                raise PreparationFailure('preparation_failed') from None
        if manifest is None and pid == 'humblefax':
            from .humblefax_service import humblefax_destination
            try:
                humblefax_destination(job['to_number'])
            except ValueError:
                # HumbleFax sends only to US/Canadian numbers; refuse before submission.
                raise PreparationFailure('preparation_failed') from None
        if manifest is None and pid == 'efax':
            from .efax_service import EfaxError
            try:
                # Sign in before the fax is marked as sent: a refused key is a definite failure, not an uncertain send.
                await service.authenticate()
            except (EfaxError, ValueError):
                raise PreparationFailure('provider_unavailable') from None
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
        # Dense pages (pages/sending.py): several original pages on one long page when the receiving machine
        # and the route allow it and it saves; blank page bottoms left out for a machine without error
        # correction. The fax's own files never change; this never stops a send.
        from .pages import sending as page_sending
        store_engine = getattr(getattr(self.store, 'configuration', None), 'engine', None)
        # The page layout the fax's sending rules chose (routing/envelope.py): "as the receiver allows" turns long
        # pages on, "one per sheet" keeps every page on its own sheet; with no rule, Faxbot's own default applies.
        layout_rule = await run_lifecycle_step(lambda: _layout_rule(store_engine, claim.job_id))
        changed = await run_lifecycle_step(lambda: page_sending.prepare(
            store_engine, values, configuration, claim, job, pdf, tiff, rule=layout_rule))
        if changed is not None:
            pdf = Path(changed.pdf) if changed.pdf else pdf
            tiff = Path(changed.tiff) if changed.tiff else tiff
        engine_job = choice = call = records = None
        if manifest is None and pid == 'sip':
            engine_job, choice, call, records = await self._prepare_engine(values, claim, job, tiff, trunk=trunk)
        try:
            with self.runtime.frame(revision):
                yield PreparedSubmission(claim, profile, job, str(pdf), str(tiff) if tiff else None,
                                         service, media_url, self.ami, engine_job, choice, call, records, trunk)
        finally:
            if engine_job is not None:
                await self._finish_engine(engine_job)

    def _choose_dialed(self, claim, job, recipient, values, pid):
        """The number this attempt calls when no route choice recorded one, recorded before the submission marker."""
        if 'dial' not in job:
            return recipient  # a store without dialed numbers: the fax calls its recipient
        from .routing.alternates import attempt_number, claim_dial_state
        from .routing.dialing import reaches
        dial = claim_dial_state(self.store, claim, job['dial'])
        alternate = dial['alternate']
        number, _ = attempt_number(recipient, alternate=alternate, refused=dial['refused'],
                                   route_reaches=bool(alternate) and reaches(pid, alternate, values))
        self.store.record_dialed(claim, number, dial['approvals'] if number != recipient else None)
        return number

    async def _prepare_engine(self, values, claim, job, tiff, trunk=None):
        """(engine job or None, engine choice, call settings, engine records) for this trunk fax.

        Runs before the durable marker: the call plan and the engine job exist,
        nothing is dialed. When the engine cannot take the job the built-in
        engine places the call; the attempt's engine record says why.
        """
        from . import hylafax_engine, hylafax_records
        from .ami import trunk_values
        full_values = values
        try:
            values, _ = trunk_values(values, trunk)  # this trunk's own T.38 and number settings
        except ValueError:
            raise PreparationFailure('preparation_failed') from None
        engine = getattr(getattr(self.store, 'configuration', None), 'engine', None)
        records = hylafax_records.records_for(engine) if engine is not None else None
        recipient = await asyncio.to_thread(hylafax_engine.recipient_limits, engine, job['to_number'])
        choice = await hylafax_engine.choose(values, members=bool(claim.members), ami=self.ami)
        if choice.engine == 'hylafax':
            # The engine may be on audio fax on its own after a T.38 call that heard no fax machine.
            call = hylafax_engine.call_settings(values, job['to_number'], recipient=recipient, engine=True)
            try:
                engine_job = await hylafax_engine.prepare_job(full_values, self.ami, job_id=claim.job_id,
                    attempt_id=claim.attempt_id, dest=job['to_number'], tiff_path=str(tiff), settings=call,
                    trunk=trunk)
                return engine_job, choice, call, records
            except (hylafax_engine.EngineError, ConnectionError, TimeoutError, OSError):
                choice = hylafax_engine.EngineChoice('builtin', hylafax_engine.NOT_RUNNING)
            except ValueError:
                raise PreparationFailure('preparation_failed') from None
        call = hylafax_engine.call_settings(values, job['to_number'], recipient=recipient)
        logging.getLogger(__name__).info('Fax %s: %s', claim.job_id, choice.reason)
        return None, choice, call, records

    async def _finish_engine(self, engine_job):
        """Close the engine session; a job that was never submitted is removed with its call plan."""
        from . import hylafax_engine
        if engine_job.submitted:
            await asyncio.to_thread(engine_job.close)
            return
        await asyncio.to_thread(engine_job.discard)
        await hylafax_engine.forget_plan(self.ami, engine_job.tag)
