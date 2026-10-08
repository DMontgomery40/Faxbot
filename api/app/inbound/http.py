"""Received-fax notifications, the fetch-again action and the background fetcher.

Nothing is stored before a notification is authenticated:

- Phaxio: the documented ``X-Phaxio-Signature`` (HMAC-SHA1 with the callback
  token over the public callback URL, sorted fields and file digests). With
  signature checks turned off, the notification is only a hint: Faxbot looks
  the fax up by ID in the configured Phaxio account first, and ignores it when
  the account has no such received fax.
- Sinch: HTTP basic auth (only with both a user name and a password), the only
  webhook security Sinch's Fax API offers (it signs no webhooks); otherwise the
  same look-up-by-ID rule in the configured Sinch project.

A notification confirmed by look-up is kept only as the provider's own record
(ID, numbers, pages, time), and its document is always fetched from the
provider; an authenticated notification is kept as evidence, cut to 8 KB by
``acquisition.sanitize_report``.
- Asterisk: the internal shared secret, and a TIFF inside the data folder.
- eFax: the documented ``X-HMAC-Signature`` (hex HMAC-SHA256 of the raw body
  with ``EFAX_WEBHOOK_SECRET``). A verified notification only starts the next
  check of eFax now; nothing in it is stored or followed.
- HumbleFax: no notification at all. Faxbot asks HumbleFax's API for received
  faxes (``humblefax.HumbleFaxReceiver``); "Check now" starts one check at once.

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
import sqlalchemy as sa

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
def _confirmed_report(fax_id, confirmed):
    """With no authenticated notification, only what the provider's own record confirmed is kept."""
    return {'id': fax_id, **{key: confirmed.get(key) for key in ('to_number', 'from_number', 'pages', 'completed_at')}}


class InboundAcquisition:
    """The installation's import store and fetcher, with a thread-safe wake-up."""

    def __init__(self, store, acquirer, runtime=None):
        self.store, self.acquirer = store, acquirer
        self.runtime = runtime
        self.loop = None
        self.wake = None
        self.efax = None
        self.humblefax = None
        # Every poller by (provider, account key), and the tasks of the extra accounts' pollers.
        self.receivers = {}
        self.extra = {}

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


def _binder(runtime):
    """(revision id, profile id) of a receiving account under the active revision, for pollers' faxes."""
    def bind(key):
        store = runtime.manager.store
        return account_binding(store, store.read().active, key)
    return bind


def polling_receiver(service, provider, key):
    """A new poller for one extra eFax or HumbleFax account."""
    if provider == 'efax':
        from .efax import EfaxReceiver
        return EfaxReceiver(service.store, _frame(service.runtime), kick=service.kick, account_key=key,
                            binder=_binder(service.runtime))
    from .humblefax import HumbleFaxReceiver
    return HumbleFaxReceiver(service.store, _frame(service.runtime), kick=service.kick, account_key=key,
                             binder=_binder(service.runtime))


def reconcile_receivers(service, values, *, start=None):
    """Start a poller for each extra eFax and HumbleFax account and stop the pollers of accounts that left.

    ``start(receiver)`` runs one (it returns its task); without it nothing runs (tests). Returns the keys
    started and stopped. Turning an account off needs no stop: its poller checks nothing while it is off.
    """
    from .. import accounts
    wanted = {(account.provider, account.key) for account in accounts.all_accounts(values)
              if not account.primary and account.provider in accounts.POLLING}
    started, stopped = [], []
    for identity in sorted(wanted - set(service.extra)):
        receiver = polling_receiver(service, *identity)
        service.receivers[identity] = receiver
        service.extra[identity] = start(receiver) if start is not None else None
        started.append(identity[1])
    for identity in sorted(set(service.extra) - wanted):
        task = service.extra.pop(identity)
        service.receivers.pop(identity, None)
        if task is not None:
            task.cancel()
        stopped.append(identity[1])
    return started, stopped


RECONCILE_SECONDS = 30


async def _reconcile_forever(service):
    """Every 30 seconds, follow the extra polling accounts in the active configuration."""
    def start(receiver):
        return asyncio.create_task(receiver.run(), name=f'faxbot-inbound-{receiver.account_key}')
    while True:
        try:
            values, _ = await run_lifecycle_step(_frame(service.runtime))
            reconcile_receivers(service, values, start=start)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning('Faxbot could not check which accounts it receives through.')
        await asyncio.sleep(RECONCILE_SECONDS)


@asynccontextmanager
async def _lifespan(app):
    tasks = []
    service = None
    try:
        runtime = app.state.configuration_runtime
        store = ImportStore(app.state.access_runtime.inbound)
        acquirer = Acquirer(store, frame=_frame(runtime), bindings=runtime.manager.store.inbound_context)
        service = InboundAcquisition(store, acquirer, runtime)
        from .efax import EfaxReceiver
        service.efax = EfaxReceiver(store, _frame(runtime), kick=service.kick, binder=_binder(runtime))
        from .humblefax import HumbleFaxReceiver
        service.humblefax = HumbleFaxReceiver(store, _frame(runtime), kick=service.kick, binder=_binder(runtime))
        service.receivers = {('efax', 'efax'): service.efax, ('humblefax', 'humblefax'): service.humblefax}
        app.state.inbound_acquisition = service
        if AUTOMATIC:
            service.loop, service.wake = asyncio.get_running_loop(), asyncio.Event()
            tasks.append(asyncio.create_task(run_forever(acquirer, service.wake), name='faxbot-inbound-acquisition'))
            tasks.append(asyncio.create_task(_recover_forever(service), name='faxbot-inbound-recovery'))
            # Received eFax faxes are found by asking eFax; it does nothing unless eFax receives.
            tasks.append(asyncio.create_task(service.efax.run(), name='faxbot-inbound-efax'))
            # Received HumbleFax faxes are found the same way; it does nothing unless HumbleFax receives.
            tasks.append(asyncio.create_task(service.humblefax.run(), name='faxbot-inbound-humblefax'))
            # One more poller for each extra eFax or HumbleFax account.
            tasks.append(asyncio.create_task(_reconcile_forever(service), name='faxbot-inbound-accounts'))
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
        extra = [task for task in (service.extra.values() if service is not None else ()) if task is not None]
        for task in tasks + extra:
            task.cancel()
        await asyncio.gather(*tasks, *extra, return_exceptions=True)
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


def _blocked(path, path_key=None):
    audit_event('inbound_route_blocked', route=path, active_inbound=active_inbound(),
                inbound_enabled=settings.inbound_enabled, **({'account': path_key} if path_key else {}))
    raise HTTPException(404, detail='Inbound route not active for current backend')


def receiving_account(provider, path, path_key=None):
    """The provider account a receiving address serves (``accounts.receiving_account``); 404 with an audit row
    when it serves none.

    Until an extra account receives, a provider's original address keeps exactly the old gate: with
    ``FAX_INBOUND_BACKEND`` set (or no receiving provider at all) only that provider's address is open;
    otherwise every provider's address is open and checks its own secret. After that, each address serves one
    account: ``/<provider>-inbound/<key>`` that account, ``/<provider>-inbound`` the default receiving account
    of that provider, else its first account, else its only receiving account.
    """
    from .. import accounts
    from ..config import configuration_values
    if not settings.inbound_enabled:
        raise HTTPException(404, detail='Inbound not enabled')
    values = configuration_values()
    if path_key is None and not accounts.extra_receiving(values):
        # With no inbound provider set up yet, no provider's receiving route is active.
        if (os.getenv('FAX_INBOUND_BACKEND') or not active_inbound()) and active_inbound() != provider:
            _blocked(path)
        account = accounts.original_path_account(values, provider)
        if not account.enabled:
            _blocked(path)
        return account
    account = accounts.receiving_account(values, provider, path_key)
    if account is None:
        _blocked(path, path_key)
    return account


def _require_route(provider, path):
    """The original-address gate (the SSL Fax engine's hand-over uses it too)."""
    return receiving_account(provider, path)


def _own_values(account):
    """The configuration as one account sees it: its provider's settings replaced by its own."""
    from .. import accounts
    from ..config import configuration_values
    return accounts.account_values(configuration_values(), account.key)


async def _binding(request, account):
    """(revision id, profile id) of the account receiving a fax, for its provider binding; None when the
    configuration is not ready or the account cannot be built. Resolved before the fax's own transaction."""
    snapshot = request.scope.get('faxbot.configuration')
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if snapshot is None or runtime is None:
        return None
    return await run_lifecycle_step(lambda: account_binding(runtime.manager.store, snapshot.active, account.key))


_BINDINGS = {}


def account_binding(store, revision, key):
    """(revision id, profile id) for an account under a revision: its role profile when the revision built one
    from it, else a stored profile of exactly its configuration (found or created). None when it can't be built."""
    from .. import accounts
    cached = _BINDINGS.get((revision.id, key))
    if cached is not None:
        return cached
    try:
        configuration = accounts.account_configuration(revision.values, key, plugin_state=revision.plugins.as_dict())
    except accounts.AccountsError:
        return None
    profile_id = None
    for role in ('inbound', 'outbound'):
        identity = revision.profile_id(role)
        if identity is not None:
            try:
                if store.read_profile(identity).configuration == configuration:
                    profile_id = identity
                    break
            except Exception:
                continue
    if profile_id is None:
        try:
            profile_id = store.account_profile(configuration)
        except Exception:
            logging.getLogger(__name__).warning('Faxbot could not record which account received a fax.')
            return None
    if len(_BINDINGS) > 256:
        _BINDINGS.clear()
    _BINDINGS[(revision.id, key)] = (revision.id, profile_id)
    return revision.id, profile_id


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
    return await _phaxio_inbound(request, None)


@router.post('/phaxio-inbound/{key}')
async def phaxio_account_inbound(key: str, request: Request):
    """A received-fax notification for one Phaxio account; its signature covers this exact address."""
    return await _phaxio_inbound(request, key)


async def _phaxio_inbound(request, path_key):
    path = '/phaxio-inbound' + (f'/{path_key}' if path_key else '')
    account = receiving_account('phaxio', path, path_key)
    own = _own_values(account)
    service = _acquisition(request)
    verify = own.phaxio_inbound_verify_signature is True
    if not verify:
        _limit_unverified(request, path)
    try:
        fields, files = await read_callback_form(request, max_body_bytes=FORM_BODY_BYTES,
                                                 max_file_bytes=MAX_DOCUMENT_BYTES)
    except CallbackFormError as error:
        raise HTTPException(error.status_code, detail=str(error)) from None
    if verify:
        token = own.phaxio_callback_token
        if not token:
            raise HTTPException(401, detail='Set the Phaxio callback token in settings to accept received faxes.')
        url = settings.public_api_url.rstrip('/') + path
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
        api, _ = provider_service('phaxio', own)
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
    # Unauthenticated, the notification was only a hint: keep the provider's confirmed record instead of it.
    report = report if verify else _confirmed_report(fax_id, confirmed)
    source_time = parse_source_time(confirmed.get('completed_at'))
    begun = await _begin(service, source='phaxio', account=account_identity('phaxio', own.phaxio_api_key),
                         operation_id=fax_id, backend='phaxio', inbound_backend='phaxio',
                         to_number=confirmed.get('to_number'), from_number=confirmed.get('from_number'),
                         reported_pages=confirmed.get('pages'),
                         report={'verified_by': verified_by, 'notification': report},
                         source_received_at=source_time,
                         artifact_digest=hashlib.sha256(attached).hexdigest() if attached else None,
                         schedule=attached is None, account_key=account.key,
                         binding=await _binding(request, account))
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


def sinch_webhook_notes(values):
    """Where the Incoming webhook URL goes in Sinch and, with basic auth, how the password goes in it."""
    where = ('In the Sinch dashboard, open Fax, then Services, click Edit beside your fax service and paste '
             'this address into Incoming webhook URL.')
    login = values.sinch_incoming_webhook_login_url
    if login is None:
        return where + ' Faxbot checks each fax with Sinch before it keeps it.'
    return (where + f' Because you set a user name and password for received faxes, paste it as {login} '
            'with your password in place of PASSWORD; Sinch then shows the password as ***.')


def _sinch_basic_configured(own=None):
    """Basic auth is in force only with both a user name and a password: the one rule, in ConfigurationValues."""
    return (own or settings).sinch_inbound_basic_configured


def _sinch_authenticated(request, own=None):
    """True when basic auth is configured and correct; None when it is not configured.

    Basic auth is the only webhook security Sinch's Fax API (v3) offers: it signs no webhooks. ``own`` is the
    receiving account's configuration; each account checks its own user name and password.
    """
    own = own or settings
    if not _sinch_basic_configured(own):
        return None
    header = request.headers.get('Authorization', '')
    try:
        user, _, password = base64.b64decode(header.split(' ', 1)[1]).decode().partition(':') \
            if header.startswith('Basic ') else ('', '', '')
    except (ValueError, UnicodeError, binascii.Error):
        user, password = '', ''
    if not (hmac.compare_digest(user.encode(), own.sinch_inbound_basic_user.encode())
            and hmac.compare_digest(password.encode(), own.sinch_inbound_basic_pass.encode())):
        raise HTTPException(401, detail='Invalid basic auth')
    return True


async def _sinch_payload(request, raw):
    media = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if media in ('multipart/form-data', 'application/x-www-form-urlencoded'):
        try:
            fields, files = await read_callback_form(_replayed(request, raw), max_body_bytes=FORM_BODY_BYTES,
                                                     max_file_bytes=MAX_DOCUMENT_BYTES)
        except CallbackFormError as error:
            raise HTTPException(error.status_code, detail=str(error)) from None
        data = _form_dict(fields)
        if 'fax' not in data:
            # The fax part arrives as JSON; sent with a file name, it is parsed as a file part.
            part = next((content for name, content in files if name == 'fax'), None)
            if part is not None:
                try:
                    data['fax'] = part.decode('utf-8')
                except UnicodeDecodeError:
                    data['fax'] = None
        if isinstance(data.get('fax'), str):
            try:
                data['fax'] = json.loads(data['fax'])
            except ValueError:
                data['fax'] = None
        # Sinch's reference declares the multipart event part as JSON, so it may arrive quoted ("INCOMING_FAX").
        event = data.get('event')
        if isinstance(event, str) and event.strip().startswith('"'):
            try:
                decoded = json.loads(event)
            except ValueError:
                decoded = None
            data['event'] = decoded if isinstance(decoded, str) else event
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
    return await _sinch_inbound(request, None)


@router.post('/sinch-inbound/{key}')
async def sinch_account_inbound(key: str, request: Request):
    """A received-fax notification for one Sinch account, checked with that account's basic auth."""
    return await _sinch_inbound(request, key)


async def _sinch_inbound(request, path_key):
    path = '/sinch-inbound' + (f'/{path_key}' if path_key else '')
    account = receiving_account('sinch', path, path_key)
    own = _own_values(account)
    service = _acquisition(request)
    if not _sinch_basic_configured(own):
        _limit_unverified(request, path)
    raw = await _bounded_body(request, JSON_BODY_BYTES)
    authenticated = _sinch_authenticated(request, own)
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
    confirmed, verified_by = notification, 'basic auth'
    if not authenticated:
        attached, verified_by = None, 'lookup'
        api, _ = provider_service('sinch', own)
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
    # Unauthenticated, the notification was only a hint: keep the provider's confirmed record instead of it.
    report = ({key: value for key, value in data.items() if key != 'file'} if authenticated
              else _confirmed_report(fax_id, confirmed))
    begun = await _begin(service, source='sinch', account=account_identity('sinch', own.sinch_project_id),
                         operation_id=fax_id, backend='sinch', inbound_backend='sinch',
                         to_number=confirmed.get('to_number'), from_number=confirmed.get('from_number'),
                         reported_pages=confirmed.get('pages'),
                         report={'verified_by': verified_by, 'notification': report},
                         source_received_at=source_time,
                         artifact_digest=hashlib.sha256(attached).hexdigest() if attached else None,
                         schedule=not attached, account_key=account.key, binding=await _binding(request, account))
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
    trunk = _trunk_key(payload)
    receiving_account('sip', '/_internal/asterisk/inbound', trunk)
    if not settings.asterisk_inbound_secret:
        raise HTTPException(401, detail='Internal secret not configured')
    if not hmac.compare_digest((x_internal_secret or '').encode(), settings.asterisk_inbound_secret.encode()):
        raise HTTPException(401, detail='Invalid internal secret')
    return receive_handover(request, payload, settings.fax_data_dir)


def received_number(value, country=None):
    """A caller or called number as Faxbot stores it: E.164 read for the installation's country, as a
    person there would dial it (``3034265097`` in the US is ``+13034265097``); a number that already
    carries its country code without the plus sign is read with it; anything else is kept as given."""
    from ..routing.numbers import InvalidNumber, normalize_number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()[:64]
    country = country or settings.fax_default_country or 'US'
    candidates = [text] if text.startswith('+') else [text, '+' + text.lstrip('0')]
    for candidate in candidates:
        try:
            return normalize_number(candidate, country=country)
        except (InvalidNumber, ValueError):
            continue
    return text


def _trunk_key(payload):
    """The trunk account a hand-over names (``trunk``, once each trunk says which it is), or None."""
    from ..accounts import KEY
    value = payload.get('trunk') if isinstance(payload, dict) else None
    return value if isinstance(value, str) and KEY.fullmatch(value) else None


def stated_subaddress(payload, engine=None):
    """The subaddress the sender stated (T.33 SUB), or None.

    The SSL Fax engine reports it as text (HylaFAX's SubAddr); the built-in engine as the SUB frame in hex
    (patch 0004's ``FAXBOT_FAR_SUB``). Without either, the call's FaxFrames row is read once: that event can
    arrive after the hand-over, so this is best effort, and a fax is never placed again later. A subaddress is
    the sender's statement, never proof of identity.
    """
    from .. import engine_frames
    from ..access.receiving_rules import normalize_subaddress
    text = payload.get('subaddress')
    if isinstance(text, str) and normalize_subaddress(text):
        return normalize_subaddress(text)
    frame = payload.get('sub_hex')
    if isinstance(frame, str) and frame:
        decoded = engine_frames.decode_sub(frame)
        if normalize_subaddress(decoded):
            return normalize_subaddress(decoded)
    uniqueid = payload.get('uniqueid')
    if engine is not None and isinstance(uniqueid, str) and re.fullmatch(r'[0-9.]{1,40}', uniqueid):
        try:
            frames = sa.Table('fax_call_frames', sa.MetaData(), autoload_with=engine)
            with engine.connect() as connection:
                row = connection.execute(sa.select(frames.c.sub).where(frames.c.id == 'in:' + uniqueid)).first()
        except sa.exc.SQLAlchemyError:
            row = None
        if row is not None and row.sub:
            return normalize_subaddress(engine_frames.decode_sub(row.sub))
    return None


def remote_address(payload):
    """The far end's internet fax address (host:port from its TSA) a received SSL Fax engine call reported, or
    None. Partner discovery reads it from the fax's import report (``remote_address``); it is a hint, never
    identity, and carries no passcode."""
    engine = payload.get('engine') if isinstance(payload, dict) else None
    encoded = engine.get('remote_address_b64') if isinstance(engine, dict) else None
    if not isinstance(encoded, str) or not encoded or len(encoded) > 400:
        return None
    try:
        text = base64.b64decode(encoded, validate=True).decode('ascii').strip()
    except (ValueError, UnicodeError, binascii.Error):
        return None
    return text if re.fullmatch(r'[A-Za-z0-9.-]{1,253}(?::[0-9]{1,5})?', text) else None


def receive_handover(request: Request, payload: dict, root: str):
    """Store one received fax a fax engine handed over; its image must sit inside ``root``.

    Asterisk may name any image in Faxbot's data folder; the SSL Fax engine
    (hylafax_http.py) only images in its own out folder. The caller has checked
    that receiving over the trunk is on. The fax is recorded on the trunk account
    the hand-over names, else the trunk the original address serves.
    """
    from .. import accounts
    from ..config import configuration_values
    service = _acquisition(request)
    trunk = _trunk_key(payload)
    account = (accounts.receiving_account(configuration_values(), 'sip', trunk) if trunk
               else accounts.original_path_account(configuration_values(), 'sip'))
    account_key = account.key if account is not None else 'sip'
    try:
        tiff_path = inside_directory(str(payload.get('tiff_path') or ''), root)
    except UnsafeSourcePath:
        raise HTTPException(400, detail='TIFF path invalid') from None
    if not os.path.isfile(tiff_path):
        raise HTTPException(400, detail='TIFF path invalid')

    def text(name, limit=64):
        value = payload.get(name)
        return str(value).strip()[:limit] or None if isinstance(value, (str, int)) else None
    faxstatus = text('faxstatus', 32)
    # Both fax engines' numbers are stored the same way, whatever format the carrier sent.
    payload = {**payload, 'to_number': received_number(text('to_number')),
               'from_number': received_number(text('from_number'))}
    if isinstance(payload.get('call'), dict):
        payload['call'] = {**payload['call'], 'did': received_number(payload['call'].get('did')),
                           'caller': received_number(payload['call'].get('caller'))}
    uniqueid = text('uniqueid', 100)
    if uniqueid is None or re.fullmatch(r'[A-Za-z0-9._:-]{1,100}', uniqueid) is None:
        uniqueid = 'file:' + hashlib.sha256(tiff_path.encode()).hexdigest()[:32]
    call = payload.get('call') if isinstance(payload.get('call'), dict) else None
    source_time = parse_source_time(call.get('ended_at')) if call else None
    report = {'faxstatus': faxstatus, 'faxpages': payload.get('faxpages'), 'uniqueid': payload.get('uniqueid'),
              **({'call': call} if call else {})}
    store = service.store
    own = accounts.account_values(configuration_values(), account_key) if account is not None else settings
    subaddress = stated_subaddress(payload, store.engine)
    if subaddress:
        report['subaddress'] = subaddress
    address = remote_address(payload)
    if address:
        report['remote_address'] = address
    snapshot = request.scope.get('faxbot.configuration')
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    binding = (account_binding(runtime.manager.store, snapshot.active, account_key)
               if snapshot is not None and runtime is not None and account is not None else None)
    begun = private_operation(lambda: store.begin(
        source='sip', account=account_identity('sip', own.sip_trunk_username), operation_id=uniqueid,
        backend='sip', inbound_backend='sip', to_number=text('to_number'),
        from_number=text('from_number'), reported_pages=_int(payload.get('faxpages')), report=report,
        source_received_at=source_time, tiff_path=tiff_path, schedule=False,
        country=settings.fax_default_country, account_key=account_key, subaddress=subaddress, binding=binding))()
    if begun.state == 'pending':
        try:
            artifact = convert_tiff(tiff_path, begun.inbound_fax_id, engine=store.engine)
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
        # Received by the SSL Fax engine: what SSL Fax did on this call.
        from ..hylafax_engine import record_inbound_engine
        record_inbound_engine(engine, payload, call_key=uniqueid, inbound_fax_id=begun.inbound_fax_id,
                              number=text('from_number'))
        # The far end's SSL Fax address (its TSA), a hint for finding partners; no network here (direct/discovery.py).
        if address:
            from ..direct.discovery import record_received_hint
            from ..hylafax_records import safely
            safely(record_received_hint, engine, call_key=uniqueid, number=text('from_number'), address=address)
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
    return await _efax_inbound(request, None)


@router.post('/efax-inbound/{key}')
async def efax_account_inbound(key: str, request: Request):
    """eFax's notification for one eFax account, signed with that account's notification secret."""
    return await _efax_inbound(request, key)


async def _efax_inbound(request, path_key):
    from .efax import signature_valid
    path = '/efax-inbound' + (f'/{path_key}' if path_key else '')
    account = receiving_account('efax', path, path_key)
    own = _own_values(account)
    service = _acquisition(request)
    if not own.efax_webhook_secret:
        raise HTTPException(404, detail='eFax notifications are not set up; Faxbot checks eFax on its own.')
    _limit_unverified(request, path)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > EFAX_NOTIFICATION_BYTES:
            raise HTTPException(413, detail='The notification is larger than Faxbot accepts.')
    if not signature_valid(own.efax_webhook_secret, bytes(body), request.headers.get('x-hmac-signature')):
        audit_event('inbound_notification_refused', backend='efax')
        raise HTTPException(401, detail='The notification signature is not valid.')
    receiver = service.receivers.get(('efax', account.key)) or (service.efax if account.primary else None)
    if receiver is not None:
        receiver.nudge()
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


@router.get('/admin/inbound/humblefax')
async def humblefax_inbound_status(request: Request, identity=Depends(require_permission('providers:read'))):
    """Whether Faxbot is checking HumbleFax for received faxes, when it last checked and what it found."""
    receiver = _acquisition(request).humblefax
    if receiver is None:
        raise HTTPException(503, detail='Receiving faxes is not ready yet; try again shortly.')
    return receiver.status(request.scope['faxbot.configuration'].active.values)


@router.post('/admin/inbound/humblefax/check')
async def humblefax_inbound_check(request: Request,
                                  identity=Depends(require_permission('providers:write', audit=True))):
    """Check HumbleFax for received faxes now, as the next scheduled check would."""
    receiver = _acquisition(request).humblefax
    if receiver is None:
        raise HTTPException(503, detail='Receiving faxes is not ready yet; try again shortly.')
    values = request.scope['faxbot.configuration'].active.values
    reason = receiver.inactive_reason(values)
    if reason is not None:
        raise HTTPException(409, detail=reason)
    if receiver.held() > 0:
        # HumbleFax blocks an address for a minute after too many requests; asking now would extend it.
        raise HTTPException(429, detail='HumbleFax asked Faxbot to slow down; select Check now again in a minute.')
    try:
        await receiver.check(values)
    except Exception:
        logging.getLogger(__name__).warning('Checking HumbleFax now failed.')
        raise HTTPException(503, detail='Faxbot could not check HumbleFax just now; try again shortly.') from None
    return receiver.status(values)


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
        await run_lifecycle_step(lambda: service.store.resume_for_fax(
            inbound_id, principal_id=getattr(identity.actor, 'principal_id', None)))
    except (AlreadyReceived, ImportNotFound) as error:
        raise HTTPException(409, detail=str(error)) from None
    service.kick()
    return await run_lifecycle_step(private_operation(lambda: queries.item(identity.actor, inbound_id)))
