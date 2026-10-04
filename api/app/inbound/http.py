"""Received-fax notifications, the fetch-again action and the background fetcher.

Nothing is stored before a notification is authenticated:

- Phaxio: the documented ``X-Phaxio-Signature`` (HMAC-SHA1 with the callback
  token over the public callback URL, sorted fields and file digests). With
  signature checks turned off, the notification is only a hint: Faxbot looks
  the fax up by ID in the configured Phaxio account first, and ignores it when
  the account has no such received fax.
- Sinch: HTTP basic auth and/or the HMAC header when configured; otherwise the
  same look-up-by-ID rule in the configured Sinch project.
- Asterisk: the internal shared secret, and a TIFF inside the data folder.
- eFax: the documented ``X-HMAC-Signature`` (hex HMAC-SHA256 of the raw body
  with ``EFAX_WEBHOOK_SECRET``). A verified notification only starts the next
  check of eFax now; nothing in it is stored or followed.

A document attached to a notification is used only when the notification was
authenticated by signature or basic auth. Faxbot never requests an address a
notification supplies; documents come from the provider's API by fax ID.
"""
import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request

from ..access.http import private_operation, runtime as access_runtime
from ..access.route_policy import require_permission
from ..audit import audit_event
from ..callback_forms import CallbackFormError, read_callback_form
from ..config import active_inbound, settings
from ..config_runtime import run_lifecycle_step
from ..provider_signatures import verify_phaxio_signature
from .acquisition import (AcquisitionError, AlreadyReceived, ImportNotFound, ImportStore, InvalidDocument,
                          account_identity, convert_tiff, discard, parse_source_time, store_document, utcnow)
from .fetch import FetchError, MAX_DOCUMENT_BYTES
from .worker import Acquirer, UnsafeSourcePath, inside_directory, provider_service, run_forever


# Tests turn this off and run single steps; production fetches in the background.
AUTOMATIC = True
UNVERIFIED_PER_MINUTE = 60
FORM_BODY_BYTES = MAX_DOCUMENT_BYTES + 1024 * 1024
JSON_BODY_BYTES = MAX_DOCUMENT_BYTES * 4 // 3 + 1024 * 1024


class InboundAcquisition:
    """The installation's import store and fetcher, with a thread-safe wake-up."""

    def __init__(self, store, acquirer, runtime=None):
        self.store, self.acquirer = store, acquirer
        self.runtime = runtime
        self.loop = None
        self.wake = None
        self.efax = None

    def recover(self):
        """Bring in received SIP images that were never handed over (see sip_handover)."""
        from .sip_handover import recover
        values = self.runtime.manager.store.read().active.values
        return recover(self.store, self.runtime.manager.store.engine, values)

    def kick(self):
        loop, wake = self.loop, self.wake
        if loop is None or wake is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(wake.set)
        except RuntimeError:
            pass


def _frame(runtime):
    def frame():
        store = runtime.manager.store
        revision = store.read().active
        return revision.values, {role: store.read_profile(identity) for role, identity in revision.profiles}
    return frame


@asynccontextmanager
async def _lifespan(app):
    tasks = []
    try:
        runtime = app.state.configuration_runtime
        store = ImportStore(app.state.access_runtime.inbound)
        acquirer = Acquirer(store, frame=_frame(runtime))
        service = InboundAcquisition(store, acquirer, runtime)
        from .efax import EfaxReceiver
        service.efax = EfaxReceiver(store, _frame(runtime), kick=service.kick)
        app.state.inbound_acquisition = service
        if AUTOMATIC:
            service.loop, service.wake = asyncio.get_running_loop(), asyncio.Event()
            tasks.append(asyncio.create_task(run_forever(acquirer, service.wake), name='faxbot-inbound-acquisition'))
            tasks.append(asyncio.create_task(_recover_forever(service), name='faxbot-inbound-recovery'))
            # Received eFax faxes are found by asking eFax; it does nothing unless eFax receives.
            tasks.append(asyncio.create_task(service.efax.run(), name='faxbot-inbound-efax'))
    except Exception:
        logging.getLogger(__name__).warning('Received-fax fetching could not start; the API is still available.')
    try:
        # The secret Asterisk sends with each received fax: created when none is set, written for Asterisk.
        from .sip_handover import prepare_handover
        runtime = app.state.configuration_runtime
        await run_lifecycle_step(lambda: prepare_handover(runtime.manager, runtime.manager.store.read().active.values))
    except Exception:
        logging.getLogger(__name__).warning('Faxbot could not prepare the inbound secret for the fax engine; '
                                            'select Apply and connect in Settings.')
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        app.state.inbound_acquisition = None


async def _recover_forever(service):
    """Every minute, bring in received SIP images that were never handed over."""
    from .sip_handover import SCAN_SECONDS
    while True:
        await asyncio.sleep(SCAN_SECONDS)
        try:
            await run_lifecycle_step(service.recover)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning('Faxbot could not check for received faxes that were not handed over.')


router = APIRouter(tags=['Inbound'], lifespan=_lifespan)


def _acquisition(request):
    service = getattr(request.app.state, 'inbound_acquisition', None)
    if service is None:
        raise HTTPException(503, detail='Receiving faxes is not ready yet; try again shortly.')
    return service


def _require_route(provider, path):
    if not settings.inbound_enabled:
        raise HTTPException(404, detail='Inbound not enabled')
    # With no inbound provider set up yet, no provider's receiving route is active.
    if (os.getenv('FAX_INBOUND_BACKEND') or not active_inbound()) and active_inbound() != provider:
        audit_event('inbound_route_blocked', route=path, active_inbound=active_inbound(),
                    inbound_enabled=settings.inbound_enabled)
        raise HTTPException(404, detail='Inbound route not active for current backend')


def _limit_unverified(request, path):
    from ..main import _enforce_rate_limit
    host = request.client.host if request.client else 'unknown'
    _enforce_rate_limit({'key_id': 'inbound-notification:' + host}, path, UNVERIFIED_PER_MINUTE)


def _distinct(values):
    found = {str(value).strip() for value in values if value is not None and str(value).strip() != ''
             and not isinstance(value, (dict, list))}
    return found.pop() if len(found) == 1 else None


def _digits(value):
    return ''.join(character for character in str(value or '') if character.isdigit())


def _matches(reported, confirmed):
    """An event's number must agree with the provider's record when both are present."""
    if not reported or not confirmed:
        return True
    return _digits(reported)[-10:] == _digits(confirmed)[-10:]


def _int(value):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100000 else None


def _form_dict(fields):
    data = {}
    for name, value in fields:
        data.setdefault(name, value)
    return data


async def _store_inline(service, begun, data, provider, source_time):
    """Keep a document an authenticated notification carried; otherwise fetch it instead."""
    store = service.store
    try:
        artifact = await run_lifecycle_step(lambda: store_document(data, begun.inbound_fax_id, provider=provider))
        completion = await run_lifecycle_step(lambda: store.complete(
            begun.import_id, artifact_path=artifact.path, digest=artifact.digest, size=artifact.size,
            pages=artifact.pages, media_type=artifact.media_type, source_received_at=source_time))
        discard(artifact, completion)
        return
    except InvalidDocument:
        message = f'{provider} attached a file that is not a readable PDF; Faxbot will fetch the document instead.'
    except Exception:
        logging.getLogger(__name__).warning('An attached received fax could not be stored; Faxbot will fetch it.')
        message = f'Faxbot could not store the attached document; it will fetch it from {provider}.'
    await run_lifecycle_step(lambda: store.fail(begun.import_id, message, retry_at=utcnow()))
    service.kick()


async def _begin(service, **values):
    begun = await run_lifecycle_step(private_operation(lambda: service.store.begin(
        country=settings.fax_default_country, **values)))
    return begun


# Phaxio ----------------------------------------------------------------------
def _phaxio_notification(fields):
    """Read the Fax Object from form fields: a JSON ``fax`` field (v2/v2.1) or bracketed fields."""
    data = _form_dict(fields)
    fax = {}
    for name, value in fields:
        if name == 'fax':
            try:
                parsed = json.loads(value)
            except ValueError:
                return None, data
            if isinstance(parsed, dict):
                fax = parsed

    def pick(*names):
        values = [fax.get(name) for name in names] + [value for key, value in fields
                                                      for name in names if key in (name, f'fax[{name}]')]
        return _distinct(values)
    notification = {
        'id': pick('id'), 'direction': pick('direction'), 'status': pick('status'),
        'from_number': pick('from_number'), 'to_number': pick('to_number'),
        'pages': _int(pick('num_pages')), 'completed_at': pick('completed_at'),
    }
    return notification, {**data, **({'fax': fax} if fax else {})}


@router.post('/phaxio-inbound')
async def phaxio_inbound(request: Request):
    _require_route('phaxio', '/phaxio-inbound')
    service = _acquisition(request)
    verify = settings.phaxio_inbound_verify_signature is True
    if not verify:
        _limit_unverified(request, '/phaxio-inbound')
    try:
        fields, files = await read_callback_form(request, max_body_bytes=FORM_BODY_BYTES,
                                                 max_file_bytes=MAX_DOCUMENT_BYTES)
    except CallbackFormError as error:
        raise HTTPException(error.status_code, detail=str(error)) from None
    if verify:
        token = settings.phaxio_callback_token
        if not token:
            raise HTTPException(401, detail='Set the Phaxio callback token in settings to accept received faxes.')
        url = settings.public_api_url.rstrip('/') + '/phaxio-inbound'
        if not verify_phaxio_signature(token, url, fields, files, request.headers.get('X-Phaxio-Signature', '')):
            raise HTTPException(401, detail='Invalid Phaxio signature')
    notification, report = _phaxio_notification(fields)
    from ..phaxio_service import PhaxioFaxService
    try:
        fax_id = PhaxioFaxService.received_fax_id(notification and notification['id'])
    except FetchError:
        return {'status': 'ignored'}
    if notification['direction'] not in (None, 'received'):
        return {'status': 'ignored'}
    confirmed = notification
    verified_by = 'signature'
    if not verify:
        verified_by = 'lookup'
        api, _ = provider_service('phaxio', settings)
        if not api.is_configured():
            raise HTTPException(401, detail='Faxbot cannot confirm this fax with Phaxio; add the Phaxio API key or '
                                            'turn signature checks on.')
        try:
            confirmed = await api.get_received_fax(fax_id)
        except FetchError:
            raise HTTPException(503, detail='Faxbot could not confirm this fax with Phaxio; try again later.') from None
        if (confirmed is None or not _matches(notification['to_number'], confirmed['to_number'])
                or not _matches(notification['from_number'], confirmed['from_number'])):
            return {'status': 'ignored'}
    attached = next((content for name, content in files if name == 'file'), None) if verify else None
    source_time = parse_source_time(confirmed.get('completed_at'))
    begun = await _begin(service, source='phaxio', account=account_identity('phaxio', settings.phaxio_api_key),
                         operation_id=fax_id, backend='phaxio', inbound_backend=active_inbound(),
                         to_number=confirmed.get('to_number'), from_number=confirmed.get('from_number'),
                         reported_pages=confirmed.get('pages'),
                         report={'verified_by': verified_by, 'notification': report},
                         source_received_at=source_time,
                         artifact_digest=hashlib.sha256(attached).hexdigest() if attached else None,
                         schedule=attached is None)
    if begun.state == 'pending':
        if attached:
            await _store_inline(service, begun, attached, 'Phaxio', source_time)
        else:
            service.kick()
    return {'status': 'ok'}


# Sinch -----------------------------------------------------------------------
async def _bounded_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, detail='Notification exceeds limits.')
    return bytes(body)


def _replayed(request, body):
    from starlette.requests import Request as StarletteRequest
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        sent = True
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return StarletteRequest(request.scope, receive)


def _sinch_authenticated(request, raw):
    """True when basic auth or HMAC is configured and correct; None when neither is configured."""
    configured = False
    if settings.sinch_inbound_basic_user:
        configured = True
        header = request.headers.get('Authorization', '')
        try:
            user, _, password = base64.b64decode(header.split(' ', 1)[1]).decode().partition(':') \
                if header.startswith('Basic ') else ('', '', '')
        except (ValueError, UnicodeError, binascii.Error):
            user, password = '', ''
        if not (hmac.compare_digest(user.encode(), settings.sinch_inbound_basic_user.encode())
                and hmac.compare_digest(password.encode(), (settings.sinch_inbound_basic_pass or '').encode())):
            raise HTTPException(401, detail='Invalid basic auth')
    if settings.sinch_inbound_hmac_secret:
        configured = True
        digest = hmac.new(settings.sinch_inbound_hmac_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, request.headers.get('X-Sinch-Signature', '').strip().lower()):
            raise HTTPException(401, detail='Invalid signature')
    return configured or None


async def _sinch_payload(request, raw):
    media = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if media in ('multipart/form-data', 'application/x-www-form-urlencoded'):
        try:
            fields, files = await read_callback_form(_replayed(request, raw), max_body_bytes=FORM_BODY_BYTES,
                                                     max_file_bytes=MAX_DOCUMENT_BYTES)
        except CallbackFormError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        data = _form_dict(fields)
        if isinstance(data.get('fax'), str):
            try:
                data['fax'] = json.loads(data['fax'])
            except ValueError:
                data['fax'] = None
        return data, next((content for name, content in files if name == 'file'), None)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    attached = None
    if isinstance(data.get('file'), str) and data['file']:
        try:
            attached = base64.b64decode(data['file'], validate=True)
        except (ValueError, binascii.Error):
            attached = b''
    return data, attached


@router.post('/sinch-inbound')
async def sinch_inbound(request: Request):
    _require_route('sinch', '/sinch-inbound')
    service = _acquisition(request)
    if not (settings.sinch_inbound_basic_user or settings.sinch_inbound_hmac_secret):
        _limit_unverified(request, '/sinch-inbound')
    raw = await _bounded_body(request, JSON_BODY_BYTES)
    authenticated = _sinch_authenticated(request, raw)
    data, attached = await _sinch_payload(request, raw)
    if data.get('event') not in (None, 'INCOMING_FAX'):
        return {'status': 'ignored'}
    fax = data.get('fax') if isinstance(data.get('fax'), dict) else data
    from ..sinch_service import SinchFaxService
    try:
        fax_id = SinchFaxService.received_fax_id(fax.get('id') or data.get('fax_id'))
    except FetchError:
        return {'status': 'ignored'}
    if fax.get('direction') not in (None, 'INBOUND'):
        return {'status': 'ignored'}
    notification = {'from_number': fax.get('from') or fax.get('from_number'),
                    'to_number': fax.get('to') or fax.get('to_number'),
                    'pages': _int(fax.get('numberOfPages') or fax.get('num_pages') or fax.get('pages')),
                    'completed_at': fax.get('completedTime')}
    confirmed, verified_by = notification, 'basic auth' if settings.sinch_inbound_basic_user else 'signature'
    if not authenticated:
        attached, verified_by = None, 'lookup'
        api, _ = provider_service('sinch', settings)
        if not api.is_configured():
            raise HTTPException(401, detail='Faxbot cannot confirm this fax with Sinch; add the Sinch project and '
                                            'key, or set up basic auth for the webhook.')
        try:
            confirmed = await api.get_received_fax(fax_id)
        except FetchError:
            raise HTTPException(503, detail='Faxbot could not confirm this fax with Sinch; try again later.') from None
        if (confirmed is None or not _matches(notification['to_number'], confirmed['to_number'])
                or not _matches(notification['from_number'], confirmed['from_number'])):
            return {'status': 'ignored'}
    source_time = parse_source_time(confirmed.get('completed_at'))
    report = {key: value for key, value in data.items() if key != 'file'}
    begun = await _begin(service, source='sinch', account=account_identity('sinch', settings.sinch_project_id),
                         operation_id=fax_id, backend='sinch', inbound_backend=active_inbound(),
                         to_number=confirmed.get('to_number'), from_number=confirmed.get('from_number'),
                         reported_pages=confirmed.get('pages'),
                         report={'verified_by': verified_by, 'notification': report},
                         source_received_at=source_time,
                         artifact_digest=hashlib.sha256(attached).hexdigest() if attached else None,
                         schedule=not attached)
    if begun.state == 'pending':
        if attached:
            await _store_inline(service, begun, attached, 'Sinch', source_time)
        else:
            service.kick()
    return {'status': 'ok'}


# Asterisk --------------------------------------------------------------------
@router.post('/_internal/asterisk/inbound')
def asterisk_inbound(request: Request, payload: dict = Body(...),
                     x_internal_secret: Optional[str] = Header(default=None)):
    _require_route('sip', '/_internal/asterisk/inbound')
    if not settings.asterisk_inbound_secret:
        raise HTTPException(401, detail='Internal secret not configured')
    if not hmac.compare_digest((x_internal_secret or '').encode(), settings.asterisk_inbound_secret.encode()):
        raise HTTPException(401, detail='Invalid internal secret')
    service = _acquisition(request)
    try:
        tiff_path = inside_directory(str(payload.get('tiff_path') or ''), settings.fax_data_dir)
    except UnsafeSourcePath:
        raise HTTPException(400, detail='TIFF path invalid') from None
    if not os.path.isfile(tiff_path):
        raise HTTPException(400, detail='TIFF path invalid')

    def text(name, limit=64):
        value = payload.get(name)
        return str(value).strip()[:limit] or None if isinstance(value, (str, int)) else None
    faxstatus = text('faxstatus', 32)
    uniqueid = text('uniqueid', 100)
    if uniqueid is None or re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', uniqueid) is None:
        uniqueid = 'file:' + hashlib.sha256(tiff_path.encode()).hexdigest()[:32]
    call = payload.get('call') if isinstance(payload.get('call'), dict) else None
    source_time = parse_source_time(call.get('ended_at')) if call else None
    report = {'faxstatus': faxstatus, 'faxpages': payload.get('faxpages'), 'uniqueid': payload.get('uniqueid'),
              **({'call': call} if call else {})}
    store = service.store
    begun = private_operation(lambda: store.begin(
        source='sip', account=account_identity('sip', settings.sip_trunk_username), operation_id=uniqueid,
        backend='sip', inbound_backend=active_inbound(), to_number=text('to_number'),
        from_number=text('from_number'), reported_pages=_int(payload.get('faxpages')), report=report,
        source_received_at=source_time, tiff_path=tiff_path, schedule=False,
        country=settings.fax_default_country))()
    if begun.state == 'pending':
        try:
            artifact = convert_tiff(tiff_path, begun.inbound_fax_id)
            discard(artifact, store.complete(begun.import_id, artifact_path=artifact.path, digest=artifact.digest,
                                             size=artifact.size, pages=artifact.pages,
                                             media_type=artifact.media_type, source_received_at=source_time))
        except AcquisitionError as error:
            store.fail(begun.import_id, str(error))
            service.kick()
        except Exception:
            logging.getLogger(__name__).warning('A received fax image could not be converted; Faxbot will try again.')
            store.fail(begun.import_id, 'Faxbot could not convert the received fax image.')
            service.kick()
    if call:
        from .. import sip_calls
        from ..routing.background import installation_engine
        engine, _ = installation_engine(request.app)
        sip_calls.record_inbound_call(engine, call, call_id=uniqueid, inbound_fax_id=begun.inbound_fax_id,
                                      preset=settings.sip_trunk_preset, fax_status=faxstatus)
    return {'id': begun.inbound_fax_id, 'status': 'ok'}


# Received over the trunk but never handed over -------------------------------
@router.post('/admin/inbound/recover')
async def recover_inbound(request: Request, identity=Depends(require_permission('providers:write', audit=True))):
    """Bring in faxes the SIP trunk received but could not hand to Faxbot; Faxbot also checks every minute."""
    from .sip_handover import receives_over_trunk
    service = _acquisition(request)
    values = request.scope['faxbot.configuration'].active.values
    if not values.inbound_enabled or not receives_over_trunk(values):
        raise HTTPException(409, detail='Turn on receiving over the SIP trunk first.')
    result = await run_lifecycle_step(service.recover)
    count = len(result.imported)
    if not count:
        message = 'No received faxes are waiting to be brought in.'
    else:
        message = f"Brought in {count} received {'fax' if count == 1 else 'faxes'}"
        message += f'; Faxbot is still reading {result.waiting} of them.' if result.waiting else '.'
    return {'found': result.found, 'imported': count, 'waiting': result.waiting, 'message': message}


# Fetch again -----------------------------------------------------------------
EFAX_NOTIFICATION_BYTES = 64 * 1024


@router.post('/efax-inbound')
async def efax_inbound(request: Request):
    """eFax's notification that a fax arrived: with a valid signature, check eFax now."""
    from .efax import signature_valid
    _require_route('efax', '/efax-inbound')
    service = _acquisition(request)
    if not settings.efax_webhook_secret:
        raise HTTPException(404, detail='eFax notifications are not set up; Faxbot checks eFax on its own.')
    _limit_unverified(request, '/efax-inbound')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > EFAX_NOTIFICATION_BYTES:
            raise HTTPException(413, detail='The notification is larger than Faxbot accepts.')
    if not signature_valid(settings.efax_webhook_secret, bytes(body), request.headers.get('x-hmac-signature')):
        audit_event('inbound_notification_refused', backend='efax')
        raise HTTPException(401, detail='The notification signature is not valid.')
    if service.efax is not None:
        service.efax.nudge()
    audit_event('inbound_notification', backend='efax')
    return {'status': 'SUCCESS'}


@router.get('/admin/inbound/efax')
async def efax_inbound_status(request: Request, identity=Depends(require_permission('providers:read'))):
    """Whether Faxbot is checking eFax for received faxes, and received faxes still stored at eFax."""
    from .efax import deletion_counts, deletion_sentences, receiving_active
    service = _acquisition(request)
    pending, stopped = await run_lifecycle_step(private_operation(lambda: deletion_counts(service.store)))
    receiver = service.efax
    return {'receiving': receiving_active(settings),
            'checked_at': receiver.last_checked if receiver is not None else None,
            'problem': receiver.last_problem if receiver is not None else None,
            'pending_deletions': pending, 'stopped_deletions': stopped,
            'notes': deletion_sentences(pending, stopped)}


@router.post('/inbound/{inbound_id}/fetch')
async def fetch_inbound(inbound_id: str, request: Request,
                        identity=Depends(require_permission('providers:write', audit=True))):
    """Ask Faxbot to fetch a received fax's document again now."""
    if not settings.inbound_enabled:
        raise HTTPException(404, detail='Inbound not enabled')
    service = _acquisition(request)
    queries = access_runtime(request).inbound_queries
    await run_lifecycle_step(private_operation(lambda: queries.require_fetchable(identity.actor, inbound_id)))
    try:
        await run_lifecycle_step(lambda: service.store.resume_for_fax(inbound_id))
    except (AlreadyReceived, ImportNotFound) as error:
        raise HTTPException(409, detail=str(error)) from None
    service.kick()
    return await run_lifecycle_step(private_operation(lambda: queries.item(identity.actor, inbound_id)))
