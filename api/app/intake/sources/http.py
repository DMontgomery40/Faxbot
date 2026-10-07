"""Intake connectors over HTTP: email mailboxes and watched folders that file documents or send faxes.

Reads need ``settings:read``; changes need ``settings:write``. A sending
connector also needs the administrator's own ``keys:manage``, because adding,
pausing, resuming or removing it issues or revokes the connector's own key.
``GET /intake/sources/faxes/{id}`` tells the Sent screen who asked for a fax
that came in by email, to anyone who may see that fax.
"""
import logging
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ...access.http import require_identity
from ...access.route_policy import authorize, require_permission
from ...access.types import AccessError
from ...config_runtime import run_lifecycle_step
from ...routing.background import installation_engine, lifespan_tasks, repeat
from ...routing.database import DeliveryStoreError
from . import keys, settings as settings_module, text
from .mail import MICROSOFT_365
from .poller import Poller
from .settings import SourceInputError
from .store import SourceConflict, SourceSecrets, SourceStore


UNAVAILABLE = 'Connectors are temporarily unavailable.'
KEY_RIGHT = 'A connector that sends faxes gets its own sending key, so this needs permission to manage keys.'


def _store(engine, runtime):
    return SourceStore(engine, SourceSecrets(runtime.manager.store))


def _own_addresses(engine, runtime, store):
    """Addresses Faxbot sends from: email delivery connectors and every email connector's mailbox."""
    def read():
        found = set()
        try:
            from ..store import IntakeStore
            from ..worker import ConnectorSecrets
            for connector in IntakeStore(engine, ConnectorSecrets(runtime.manager.store)).list_connectors():
                found.add(connector.settings.from_address)
        except Exception:
            pass
        found.update(source.settings.get('address') for source in store.list() if source.kind == 'email')
        return tuple(value for value in found if value)
    return read


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    try:
        store = _store(engine, runtime)
    except DeliveryStoreError:
        return []
    poller = Poller(store, lambda: app.state.access_runtime, runtime,
                    own_addresses=_own_addresses(engine, runtime, store))
    app.state.intake_sources_poller = poller
    return [('faxbot-intake-connectors', repeat(poller.step, interval=5.0, initial_delay=3.0,
                                                warning='Intake connectors could not be checked.'))]


router = APIRouter(prefix='/intake/sources', tags=['Intake'], lifespan=lifespan_tasks(_background))


def _context(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or getattr(request.app.state, 'access_runtime', None) is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    try:
        store = _store(engine, runtime)
    except DeliveryStoreError:
        raise HTTPException(503, detail=UNAVAILABLE) from None
    poller = getattr(request.app.state, 'intake_sources_poller', None)
    if poller is None or poller.store.engine is not engine:
        poller = Poller(store, lambda: request.app.state.access_runtime, runtime,
                        own_addresses=_own_addresses(engine, runtime, store))
    else:
        poller.store = store
    return store, poller, request.app.state.access_runtime


def _values(request):
    snapshot = request.scope.get('faxbot.configuration')
    if snapshot is not None:
        return snapshot.active.values
    _, runtime = installation_engine(request.app)
    return runtime.manager.store.read().active.values


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except SourceInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    except SourceConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail=UNAVAILABLE) from None


# -- views -----------------------------------------------------------------------------

STATE_TEXT = {
    'sending': 'Being sent.',
    'imported': 'Filed.',
    'sent': 'Sent on to be faxed.',
    'refused': 'Refused.',
    'failed': 'Not done.',
    'conflict': 'Kept the first copy.',
    'uncertain': 'Uncertain; check before trying again.',
}
REPLY_TEXT = {
    'none': None,
    'waiting': 'A reply goes to the sender when the fax is done.',
    'due': 'A reply to the sender is waiting to go.',
    'sent': 'The sender was told by email.',
    'failed': 'The reply to the sender could not be sent.',
    'uncertain': 'The reply to the sender may not have arrived; it is not sent again.',
}


def _status(source):
    if source.removed:
        return 'Removed.'
    if not source.enabled:
        return source.paused_reason or text.PAUSED
    if source.last_result:
        return source.last_result
    return text.NOT_CHECKED


def _what(source):
    if source.kind == 'email':
        return 'Brings in documents from an email mailbox' if source.direction == 'receive' else 'Faxes what people email'
    return 'Brings in documents from a folder' if source.direction == 'receive' else 'Faxes files put in a folder'


def _source_view(source, counts, senders=None, mailbox=None):
    settings = dict(source.settings)
    view = {'id': source.id, 'name': source.name, 'kind': source.kind, 'direction': source.direction,
            'what': _what(source), 'enabled': source.enabled, 'paused': not source.enabled,
            'status': _status(source), 'ok': source.last_ok, 'last_checked_at': source.last_checked_at,
            'has_secret': source.has_secret, 'has_sending_key': bool(source.key_id),
            'sending_key_id': source.key_id, 'settings': settings, 'version': source.version,
            'created_at': source.created_at,
            'counts': counts.get(source.id, {'items': 0, 'duplicates': 0, 'refused': 0, 'failed': 0})}
    if source.kind == 'email':
        view['guidance'] = settings_module.what_to_set(settings.get('provider', 'other'))
        view['checks_senders_with'] = ('Microsoft 365' if settings.get('checked_by') == MICROSOFT_365
                                       else settings.get('checked_by'))
    if senders is not None:
        view['senders'] = senders
    if mailbox is not None:
        view['mailbox'] = mailbox
    return view


def _item_view(item, names):
    duplicates = item['duplicates']
    detail = item['reason'] or STATE_TEXT[item['state']]
    if duplicates:
        template = text.DUPLICATE_SEND if item['direction'] == 'send' else text.DUPLICATE_RECEIVE
        detail = f'{detail} {template.format(count=text.times(duplicates))}'
    return {'id': item['id'], 'connector_id': item['source_id'], 'connector': names.get(item['source_id']),
            'direction': item['direction'], 'state': item['state'], 'status': detail,
            'what': item['subject'] or item['reference'], 'sender': item['sender'],
            'sender_name': item['sender_name'], 'to_number': item['to_number'], 'duplicates': duplicates,
            'refused': item['state'] == 'refused', 'fax_id': item['fax_job_id'],
            'inbound_id': item['inbound_fax_id'], 'reply': REPLY_TEXT.get(item['reply_state']),
            'reply_detail': item['reply_note'] if item['reply_state'] in ('failed', 'uncertain') else None,
            'received_at': item['source_received_at'], 'created_at': item['created_at'],
            'last_seen_again_at': item['last_duplicate_at']}


# -- inputs ----------------------------------------------------------------------------

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class SenderIn(Strict):
    address: str = Field(max_length=320)
    principal_id: str = Field(min_length=1, max_length=40)


class SourceIn(Strict):
    name: str = Field(max_length=100)
    kind: Literal['email', 'folder']
    direction: Literal['receive', 'send']
    settings: dict = Field(default_factory=dict)
    secret: dict[str, str] | None = Field(default=None, repr=False)
    senders: list[SenderIn] = Field(default_factory=list, max_length=500)


class SourceUpdate(Strict):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=100)
    settings: dict | None = None
    secret: dict[str, str] | None = Field(default=None, repr=False)
    senders: list[SenderIn] | None = Field(default=None, max_length=500)


def _people(store, senders):
    """Each sender address mapped to an enabled Faxbot user; raises SourceInputError."""
    entries = []
    found = store.people({entry.principal_id for entry in senders})
    for entry in senders:
        address = settings_module.address(entry.address, 'sender address')
        person = found.get(entry.principal_id)
        if person is None or person['kind'] != 'user':
            raise SourceInputError(f'Choose a Faxbot person for {address}.')
        entries.append({'address': address, 'principal_id': entry.principal_id})
    return entries


def _secret(kind, direction, settings, supplied, current=None):
    if kind != 'email':
        return {}
    return settings_module.secrets(settings['sign_in'], supplied, current)


def _require_key_right(access, actor):
    try:
        authorize(access, actor, 'keys:manage')
    except AccessError:
        raise HTTPException(403, detail=KEY_RIGHT) from None


def _actor_name(store, actor):
    return (store.people({actor.principal_id}).get(actor.principal_id) or {}).get('name') or 'an administrator'


# -- routes ------------------------------------------------------------------------------

@router.get('', summary='Connectors with their status and counts',
            dependencies=[Depends(require_permission('settings:read'))])
async def list_sources(request: Request):
    store, _, _ = _context(request)

    def read():
        counts = store.counts()
        boxes = {box['id']: box for box in store.mailbox_choices()}
        return [_source_view(source, counts, store.senders_of(source.id) if source.direction == 'send' else None,
                             boxes.get(source.settings.get('mailbox_id'))) for source in store.list()]
    return {'connectors': await _call(read)}


@router.get('/choices', summary='Mailboxes, people and mail services a connector can use',
            dependencies=[Depends(require_permission('settings:read'))])
async def choices(request: Request):
    store, _, access = _context(request)

    def read():
        principals = access.store.tables['access_principals']
        users = access.store.tables['access_users']
        with access.store.engine.connect() as connection:
            rows = connection.execute(sa.select(principals.c.id, principals.c.display_name, users.c.login)
                                      .select_from(principals.join(users, users.c.id == principals.c.id))
                                      .where(principals.c.kind == 'user', principals.c.enabled == 1)
                                      .order_by(principals.c.display_name, principals.c.id)).all()
        return {'mailboxes': store.mailbox_choices(),
                'people': [{'id': row.id, 'name': row.display_name, 'login': row.login} for row in rows],
                'providers': [{'id': name, 'guidance': settings_module.what_to_set(name),
                               **{key: value for key, value in settings_module.PRESETS[name].items()
                                  if key != 'checked_by'},
                               'checks_senders_with': ('Microsoft 365'
                                                       if settings_module.PRESETS[name].get('checked_by') == MICROSOFT_365
                                                       else settings_module.PRESETS[name].get('checked_by'))}
                              for name in settings_module.PROVIDERS]}
    return await _call(read)


@router.get('/items', summary='What connectors brought in or sent, newest first',
            dependencies=[Depends(require_permission('settings:read'))])
async def list_items(request: Request, connector: str | None = Query(default=None, max_length=100),
                     limit: int = Query(default=100, ge=1, le=500)):
    store, _, _ = _context(request)

    def read():
        sources = store.list(include_removed=True)
        names = {source.id: source.name for source in sources}
        source_id = None
        if connector:
            match = store.find(connector)
            if match is None:
                raise SourceConflict('No single connector has this name.')
            source_id = match.id
        return [_item_view(item, names) for item in store.list_items(source_id=source_id, limit=limit)]
    return {'items': await _call(read)}


@router.get('/faxes/{job_id}', summary='Who asked for a fax that came in by email or from a folder')
async def fax_requester(job_id: str, request: Request, identity=Depends(require_identity)):
    store, _, access = _context(request)

    def read():
        access.queries.job(identity.actor, job_id)  # the fax must be visible to this person
        item = store.for_fax(job_id)
        if item is None:
            return None
        if item['sender_name']:
            sentence = f"Sent by email from {item['sender_name']} ({item['sender']}) through {item['source_name']}."
        elif item['sender']:
            sentence = f"Sent by email from {item['sender']} through {item['source_name']}."
        else:
            sentence = f"Sent from the folder connector {item['source_name']}."
        return {'fax_id': job_id, 'connector': item['source_name'], 'person': item['sender_name'],
                'address': item['sender'], 'sentence': sentence}
    found = await _call(read)
    if found is None:
        raise HTTPException(404, detail='This fax did not come from a connector.')
    return found


@router.post('', summary='Add a connector', status_code=201,
             dependencies=[Depends(require_permission('settings:write'))])
async def create_source(body: SourceIn, request: Request, identity=Depends(require_identity)):
    store, _, access = _context(request)
    sending = body.direction == 'send'
    if sending:
        await run_lifecycle_step(lambda: _require_key_right(access, identity.actor))

    def create():
        settings = settings_module.validate(body.kind, body.direction, body.settings)
        secret = _secret(body.kind, body.direction, settings, body.secret)
        senders = _people(store, body.senders) if sending and body.kind == 'email' else []
        if sending and body.kind == 'email' and not senders:
            raise SourceInputError('Add at least one person who may send faxes by email, with their address.')
        if body.kind == 'folder':
            folder_check(settings['path'], _values(request))
        key = None
        if sending:
            if store.find(body.name) is not None:
                raise SourceConflict('Another connector already has this name.')
            token, public_id, binding_id, principal_id = keys.issue(access, identity.actor, body.name.strip())
            secret = {**secret, 'key_token': token}
            key = (public_id, binding_id, principal_id)
        try:
            source = store.create(kind=body.kind, direction=body.direction, name=body.name, settings=settings,
                                  secret=secret or None, senders=senders, key=key)
        except BaseException:
            if key is not None:
                try:
                    keys.revoke(access, identity.actor, key[1])
                except Exception:
                    logging.getLogger(__name__).warning('A connector key could not be revoked after a failed add.')
            raise
        return _source_view(source, {}, store.senders_of(source.id) if sending else None)
    return await _call(create)


SYSTEM_FOLDERS = ('/bin', '/boot', '/dev', '/etc', '/lib', '/lib64', '/proc', '/root', '/run', '/sbin', '/sys',
                  '/usr', '/var/lib', '/app')


def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip('/') + '/')


def folder_check(path, values=None):
    """A folder Faxbot may watch: it exists, can be read and written, and is not one of Faxbot's or the system's."""
    from . import folder
    own = [getattr(values, 'fax_data_dir', None)] if values is not None else []
    if (any(_inside(path, system) for system in SYSTEM_FOLDERS)
            or any(item and (_inside(path, item) or _inside(item, path)) for item in own)):
        raise SourceInputError(text.FOLDER_IS_FAXBOT.format(path=path))
    try:
        folder.check(path)
    except folder.FolderError as error:
        raise SourceInputError((text.FOLDER_MISSING if str(error) == 'missing'
                                else text.FOLDER_UNREADABLE).format(path=path)) from None


@router.put('/{source_id}', summary='Change a connector; settings you leave out stay as they are',
            dependencies=[Depends(require_permission('settings:write'))])
async def update_source(source_id: str, body: SourceUpdate, request: Request):
    store, poller, _ = _context(request)

    def change():
        current = store.get(source_id)
        if current is None or current.removed:
            raise SourceConflict('This connector no longer exists.')
        settings = None
        if body.settings is not None:
            settings = settings_module.validate(current.kind, current.direction, {**current.settings, **body.settings})
            if current.kind == 'folder':
                folder_check(settings['path'], _values(request))
        secret = None
        effective = settings or current.settings
        if current.kind == 'email' and (body.secret is not None or settings is not None):
            stored = store.secret(current)
            fresh = _secret(current.kind, current.direction, effective, body.secret, stored)
            secret = {**fresh, **({'key_token': stored['key_token']} if stored.get('key_token') else {})}
            poller.tokens.forget(source_id)
        senders = None
        if body.senders is not None and current.direction == 'send' and current.kind == 'email':
            senders = _people(store, body.senders)
            if not senders:
                raise SourceInputError('Add at least one person who may send faxes by email, with their address.')
        source = store.update(source_id, version=body.version, name=body.name, settings=settings, secret=secret,
                              senders=senders)
        return _source_view(source, store.counts(), store.senders_of(source_id) if source.direction == 'send'
                            else None)
    return await _call(change)


@router.post('/{source_id}/test', summary='Sign in or open the folder and look, without changing anything',
             dependencies=[Depends(require_permission('settings:write'))])
async def test_source(source_id: str, request: Request):
    store, poller, _ = _context(request)

    def run():
        source = store.get(source_id)
        if source is None or source.removed:
            raise SourceConflict('This connector no longer exists.')
        ok, detail = poller.test(source)
        return {'ok': ok, 'detail': detail}
    return await _call(run)


class VersionIn(Strict):
    version: int | None = Field(default=None, ge=1)


@router.post('/{source_id}/pause', summary='Stop checking a connector; a sending connector loses its key',
             dependencies=[Depends(require_permission('settings:write'))])
async def pause_source(source_id: str, request: Request, body: VersionIn | None = None,
                       identity=Depends(require_identity)):
    store, _, access = _context(request)
    current = await _call(lambda: store.get(source_id))
    if current is None or current.removed:
        raise HTTPException(409, detail='This connector no longer exists.')
    if current.direction == 'send':
        await run_lifecycle_step(lambda: _require_key_right(access, identity.actor))

    def pause():
        if current.direction == 'send':
            keys.revoke(access, identity.actor, current.key_binding_id)
            secret = {key: value for key, value in store.secret(current).items() if key != 'key_token'}
            store.update(source_id, version=current.version, secret=secret or {})
            version = None
        else:
            version = body.version if body else None
        source = store.set_paused(source_id, paused=True, version=version, clear_key=current.direction == 'send',
                                  reason=text.PAUSED_BY.format(person=_actor_name(store, identity.actor)))
        return _source_view(source, store.counts())
    return await _call(pause)


@router.post('/{source_id}/resume', summary='Check a connector again; a sending connector gets a new key',
             dependencies=[Depends(require_permission('settings:write'))])
async def resume_source(source_id: str, request: Request, identity=Depends(require_identity)):
    store, _, access = _context(request)
    current = await _call(lambda: store.get(source_id))
    if current is None or current.removed:
        raise HTTPException(409, detail='This connector no longer exists.')
    if current.direction == 'send':
        await run_lifecycle_step(lambda: _require_key_right(access, identity.actor))

    def resume():
        key = None
        if current.direction == 'send' and not current.key_binding_id:
            token, public_id, binding_id, principal_id = keys.reissue(access, identity.actor,
                                                                      current.key_principal_id, current.name)
            key = (public_id, binding_id, principal_id)
            try:
                store.replace_secret_fields(current, {'key_token': token})
            except BaseException:
                keys.revoke(access, identity.actor, binding_id)
                raise
        source = store.set_paused(source_id, paused=False, key=key)
        return _source_view(source, store.counts())
    return await _call(resume)


@router.delete('/{source_id}', summary='Remove a connector; what it filed and sent stays listed',
               dependencies=[Depends(require_permission('settings:write'))])
async def remove_source(source_id: str, request: Request, identity=Depends(require_identity)):
    store, _, access = _context(request)
    current = await _call(lambda: store.get(source_id))
    if current is None or current.removed:
        raise HTTPException(404, detail='This connector no longer exists.')
    if current.direction == 'send':
        await run_lifecycle_step(lambda: _require_key_right(access, identity.actor))

    def remove():
        if current.direction == 'send':
            keys.revoke(access, identity.actor, current.key_binding_id)
        store.remove(source_id)
        return {'removed': True}
    return await _call(remove)
