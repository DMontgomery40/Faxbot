"""Fax servers that send through Faxbot (``fax_servers.py``), for Delivery setup → the trunk page and the command line.

- ``GET /admin/sip/fax-servers``: your fax servers and where to point them.
- ``POST /admin/sip/fax-servers``: enrol a fax server; its password is shown once.
- ``PUT /admin/sip/fax-servers/{id}``: change its countries, caps, outside-line prefix or notice contacts.
- ``POST /admin/sip/fax-servers/{id}/remove``: take it away and revoke its sending key.
- ``GET /admin/sip/fax-servers/calls``: its calls, newest first, each with what became of the fax.

Asterisk's own calls (``extensions-fax-server.conf``, through ``faxbot-fax-server``), with the internal secret:

- ``POST /_internal/asterisk/fax-server/admit``: before answering, ``send:+E164``, ``receive:+E164`` or
  ``refuse:CODE`` as plain text.
- ``POST /_internal/asterisk/fax-server/handover``: the fax once the call ends.
"""
import hmac
import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from .access.route_policy import require_permission
from .config_runtime import run_lifecycle_step
from .routing.background import lifespan_tasks, repeat_async
from . import fax_servers

UNAVAILABLE = 'Fax servers are unavailable right now. Try again in a moment.'


def _background(app):
    return [('faxbot-fax-server-settle', repeat_async(
        fax_servers.attach_settle(app), interval=60.0, initial_delay=30.0,
        warning="Faxbot could not check its fax servers' faxes just now; it tries again in a minute."))]


router = APIRouter(prefix='/admin/sip/fax-servers', tags=['SIP trunk'], lifespan=lifespan_tasks(_background))
internal = APIRouter(tags=['SIP trunk'])


class ServerIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    address: str = Field(min_length=7, max_length=45)
    countries: list[str] = Field(default_factory=list, max_length=60)
    per_hour: int | None = Field(default=None, ge=1, le=fax_servers.MAX_PER_HOUR)
    per_day: int | None = Field(default=None, ge=1, le=fax_servers.MAX_PER_DAY)
    dial_prefix: str | None = Field(default=None, max_length=4)
    contact_email: str | None = Field(default=None, max_length=254)
    contact_webhook: str | None = Field(default=None, max_length=512)
    mailbox_id: str | None = Field(default=None, max_length=40)


class ServerChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    countries: list[str] | None = Field(default=None, max_length=60)
    per_hour: int | None = Field(default=None, ge=1, le=fax_servers.MAX_PER_HOUR)
    per_day: int | None = Field(default=None, ge=1, le=fax_servers.MAX_PER_DAY)
    dial_prefix: str | None = Field(default=None, max_length=4)
    contact_email: str | None = Field(default=None, max_length=254)
    contact_webhook: str | None = Field(default=None, max_length=512)


def _runtime(request):
    from .routing.background import installation_engine
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime


def _country_names(codes):
    from .direct.relay import country_name
    return [country_name(code) for code in codes]


def _view(engine):
    from .routing.reply_number import mailbox_labels
    labels = mailbox_labels(engine)
    found = []
    for item in fax_servers.servers(engine):
        places = ', '.join(_country_names(item.countries))
        contacts = [value for value in (item.contact_email, item.contact_webhook) if value]
        found.append({
            'id': item.id, 'name': item.name, 'address': item.address, 'username': item.username,
            'countries': list(item.countries), 'per_hour': item.per_hour, 'per_day': item.per_day,
            'dial_prefix': item.dial_prefix, 'contact_email': item.contact_email,
            'contact_webhook': item.contact_webhook, 'mailbox_id': item.mailbox_id,
            'mailbox': labels.get(item.mailbox_id) if item.mailbox_id else None,
            'sentence': (f'{item.name} at {item.address} may send to {places}, up to {item.per_hour:,} faxes an hour '
                         f'and {item.per_day:,} a day. '
                         + (f"Faxbot tells {' and '.join(contacts)} when one of its faxes is not delivered."
                            if contacts else 'No one is told when one of its faxes is not delivered; add a contact.')),
        })
    return found


def _taken(values, engine):
    """Addresses Faxbot already recognises as something else: a phone system or gateway trunk, a partner's tunnel."""
    from . import sip_trunk
    from .direct import peer_call
    from .routing.database import DeliveryStoreError
    found = set()
    for account in sip_trunk.trunk_accounts(values):
        try:
            trunk = sip_trunk.effective_trunk(account.values)
        except sip_trunk.TrunkConfigurationError:
            continue
        if trunk.preset.kind in sip_trunk.LAN_KINDS:
            found.add(trunk.host)
    try:
        for peer in peer_call.installation_peers(engine):
            shown = peer_call.renderable(peer)
            if shown is not None:
                found.add(shown[0])
    except DeliveryStoreError:
        logging.getLogger(__name__).warning('Partners could not be read to check a fax server address.')
    return found


@router.get('', dependencies=[Depends(require_permission('providers:read'))])
async def list_servers(request: Request):
    """Your fax servers that send through Faxbot, and where each one sends its calls."""
    engine, runtime = _runtime(request)
    values = runtime.manager.store.read().active.values
    found = await run_lifecycle_step(lambda: _view(engine))
    return {'servers': found, 'where_text': fax_servers.where_to_point(values)}


@router.post('', status_code=201)
async def add_server(body: ServerIn, request: Request, identity=Depends(require_permission('providers:write'))):
    """Enrol a fax server and make its sending key. The password is in this answer only; enter it on the server."""
    from .intake.sources import keys
    from .intake.sources.http import _require_key_right, _require_mailbox
    from .intake.sources.settings import SourceInputError
    engine, runtime = _runtime(request)
    access = request.app.state.access_runtime
    values = runtime.manager.store.read().active.values
    await run_lifecycle_step(lambda: _require_key_right(access, identity.actor))

    def create():
        _require_mailbox(access, identity.actor, body.mailbox_id)
        home = values.fax_default_country or 'US'
        # Everything that can be refused is checked before the sending key exists.
        address = fax_servers.private_address(body.address)
        fax_servers.clean_countries(body.countries, home)
        if address in _taken(values, engine) or any(item.address == address for item in fax_servers.servers(engine)):
            raise fax_servers.FaxServerError(f'{address} is already your phone system, a partner or a fax server; '
                                             'a fax server needs its own address.')
        token, _, binding_id, principal_id = keys.issue(access, identity.actor, f'Fax server {body.name.strip()}')
        try:
            return fax_servers.add(
                engine, name=body.name, address=address, home=home, countries=body.countries,
                per_hour=body.per_hour, per_day=body.per_day, dial_prefix=body.dial_prefix,
                contact_email=body.contact_email, contact_webhook=body.contact_webhook, mailbox_id=body.mailbox_id,
                actor={'name': getattr(identity.actor, 'display_name', None)},
                key=(token, binding_id, principal_id), seal=fax_servers.ServerSecrets(runtime.manager.store))
        except BaseException:
            keys.revoke(access, identity.actor, binding_id)
            raise
    try:
        server, password = await run_lifecycle_step(create)
    except (fax_servers.FaxServerError, SourceInputError) as error:
        raise HTTPException(400, detail=str(error)) from None
    view = next(item for item in _view(engine) if item['id'] == server.id)
    return {**view, 'password': password, 'realm': fax_servers.REALM,
            'password_text': 'Enter this user name and password on the fax server now; Faxbot shows the password '
                             'only once and keeps only its digest.',
            'apply_text': 'Press Apply on the trunk page so the fax engine loads the fax server; it can send after '
                          'that.',
            'where_text': fax_servers.where_to_point(values)}


@router.put('/{server_id}')
async def change_server(server_id: str, body: ServerChange, request: Request,
                        identity=Depends(require_permission('providers:write'))):
    """Change where a fax server may send, its caps, its outside-line prefix or who is told about failures."""
    engine, runtime = _runtime(request)
    values = runtime.manager.store.read().active.values
    fields = body.model_dump(exclude_unset=True)
    try:
        server = await run_lifecycle_step(lambda: fax_servers.change(
            engine, server_id, home=values.fax_default_country or 'US',
            actor={'name': getattr(identity.actor, 'display_name', None)}, **fields))
    except fax_servers.FaxServerError as error:
        raise HTTPException(404 if 'No fax server' in str(error) else 400, detail=str(error)) from None
    return next(item for item in _view(engine) if item['id'] == server.id)


@router.post('/{server_id}/remove')
async def remove_server(server_id: str, request: Request, identity=Depends(require_permission('providers:write'))):
    from .intake.sources import keys
    engine, _ = _runtime(request)
    access = request.app.state.access_runtime

    def remove():
        found = fax_servers.remove(engine, server_id, actor={'name': getattr(identity.actor, 'display_name', None)})
        keys.revoke(access, identity.actor, found.key_binding_id)
        return found
    try:
        found = await run_lifecycle_step(remove)
    except fax_servers.FaxServerError as error:
        raise HTTPException(404, detail=str(error)) from None
    return {'id': found.id, 'detail': f'{found.name} can no longer send through Faxbot. Press Apply on the trunk page '
                                      'so the fax engine forgets it.'}


@router.get('/calls', dependencies=[Depends(require_permission('providers:read'))])
async def server_calls(request: Request, server: str | None = Query(default=None, max_length=40),
                       limit: int = Query(default=100, ge=1, le=500)):
    """Calls from your fax servers, newest first, each with what became of its fax and whether its contact was told."""
    engine, _ = _runtime(request)
    return {'calls': await run_lifecycle_step(lambda: fax_servers.calls(engine, server_id=server, limit=limit))}


# Asterisk's calls ------------------------------------------------------------------------------------------------

def _check_secret(value):
    from .config import settings
    if not settings.asterisk_inbound_secret:
        raise HTTPException(401, detail='Internal secret not configured')
    if not hmac.compare_digest((value or '').encode(), settings.asterisk_inbound_secret.encode()):
        raise HTTPException(401, detail='Invalid internal secret')


def _call_id(payload):
    import base64
    import binascii
    text = payload.get('callid64')
    if not isinstance(text, str) or not text:
        return None
    try:
        return base64.b64decode(text, validate=True).decode('utf-8', 'replace')[:512] or None
    except (ValueError, binascii.Error):
        return None


def _server_id(payload):
    value = payload.get('server')
    return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value) else None


@internal.post('/_internal/asterisk/fax-server/admit', response_class=PlainTextResponse)
async def admit_call(request: Request, payload: dict = Body(...),
                     x_internal_secret: Optional[str] = Header(default=None)):
    """Before answering a fax server's call: whether Faxbot takes the fax, as one line for the dialplan."""
    _check_secret(x_internal_secret)
    engine, runtime = _runtime(request)
    server_id = _server_id(payload)
    if server_id is None:
        return PlainTextResponse('refuse:403')
    values = runtime.manager.store.read().active.values

    def decide():
        server = fax_servers.find(engine, server_id)
        rules = fax_servers.rules_check(request.app, server) if server is not None else None
        return fax_servers.admit(engine, values, server_id, address=payload.get('address'),
                                 dialled=payload.get('dialed'), call_id=_call_id(payload),
                                 caller=payload.get('caller'), uniqueid=payload.get('uniqueid'), rules=rules)
    admission = await run_lifecycle_step(decide)
    if admission.verdict == 'refuse':
        logging.getLogger(__name__).info('A fax server call was refused before answering: %s.', admission.reason)
    return PlainTextResponse(admission.answer)


@internal.post('/_internal/asterisk/fax-server/handover')
async def handover(request: Request, payload: dict = Body(...),
                   x_internal_secret: Optional[str] = Header(default=None)):
    """A fax server's fax after its call: sent on, or recorded with why not. Idempotent per call."""
    from .config import settings
    from .inbound.worker import UnsafeSourcePath, inside_directory
    _check_secret(x_internal_secret)
    _runtime(request)
    server_id = _server_id(payload)
    if server_id is None:
        raise HTTPException(400, detail='Fax server invalid')
    tiff_path = None
    if payload.get('tiff_path'):
        try:
            tiff_path = inside_directory(str(payload['tiff_path']),
                                         os.path.join(settings.fax_data_dir, fax_servers.FOLDER))
        except UnsafeSourcePath:
            raise HTTPException(400, detail='TIFF path invalid') from None
    body = {'server': server_id, 'call_id': _call_id(payload), 'uniqueid': payload.get('uniqueid'),
            'faxstatus': payload.get('faxstatus'), 'faxpages': payload.get('faxpages')}
    try:
        return await run_lifecycle_step(lambda: fax_servers.hand_over(request.app, body, tiff_path))
    except fax_servers.HandoverError as error:
        raise HTTPException(error.status, detail=str(error)) from None


__all__ = ['router', 'internal']
