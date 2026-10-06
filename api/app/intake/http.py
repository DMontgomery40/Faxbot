"""Intake queue and email connector administration.

Reads use ``mailboxes:read`` and changes use ``settings:write``; the queue is an
installation-wide view of every received document's delivery.
"""
import logging
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat
from ..routing.database import DeliveryStoreError
from .email import AmbiguousFailure, DefiniteFailure, send, test_message
from .store import IntakeConflict, IntakeInputError, IntakeStore, recipients_of
from .worker import ConnectorSecrets, IntakeWorker


def _active_values(runtime):
    return lambda: runtime.manager.store.read().active.values


# The connector from installation settings follows each settings change at once: before the
# connector list is read and before each delivery step, whenever the active settings changed.
# The 300-second task below stays as a backstop. One lock keeps two callers from creating it twice.
_SYNC_LOCK = threading.Lock()
_SYNCED: dict[int, str] = {}


def sync_settings_now(store, runtime, *, always=False):
    """Bring the connector from installation settings in step with the active settings."""
    revision = runtime.manager.store.read().active
    key = id(runtime.manager.store)
    with _SYNC_LOCK:
        if not always and _SYNCED.get(key) == revision.id:
            return
        store.sync_managed(revision.values)
        _SYNCED[key] = revision.id


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    values = _active_values(runtime)
    store = IntakeStore(engine, ConnectorSecrets(runtime.manager.store),
                        country=lambda: values().fax_default_country)
    worker = IntakeWorker(store, values=values)

    def deliver():
        try:
            sync_settings_now(store, runtime)
        except Exception:
            logging.getLogger(__name__).warning('The email connector from installation settings could not be updated.')
        return worker.step()

    def sync_settings():
        sync_settings_now(store, runtime, always=True)
        return False
    return [('faxbot-intake-delivery', repeat(deliver, interval=5.0, initial_delay=2.0,
                                              warning='Intake delivery is temporarily unavailable.')),
            ('faxbot-intake-settings', repeat(sync_settings, interval=300.0, initial_delay=1.0,
                                              warning='The email connector from installation settings could not be updated.'))]


router = APIRouter(prefix='/intake', tags=['Intake'], lifespan=lifespan_tasks(_background))


def _store(request):
    engine, runtime = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    try:
        values = _active_values(runtime)
        return IntakeStore(engine, ConnectorSecrets(runtime.manager.store),
                           country=lambda: values().fax_default_country)
    except DeliveryStoreError:
        raise HTTPException(503, detail='Intake storage is unavailable.') from None


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except IntakeInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    except IntakeConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Intake storage is unavailable.') from None


STATE_TEXT = {
    'received': 'Waiting to be delivered.',
    'sending': 'Being delivered now.',
    'delivered': 'Delivered.',
    'failed': 'Not delivered.',
}


def _item_view(item, connectors):
    waiting = item['state'] == 'received' and item['next_attempt_at'] is None
    connector = connectors.get(item['connector_id'])
    # Who it went to as stored when the email server accepted it; never the connector's addresses today.
    recipients = recipients_of(item) if item['state'] == 'delivered' else None
    return {'id': item['id'], 'source': item['source'], 'inbound_fax_id': item['inbound_fax_id'],
            'received_at': item['received_at'],
            'pages': item['pages'], 'from_number': item['from_number'], 'to_number': item['to_number'],
            'state': item['state'], 'status': item['last_error'] or STATE_TEXT[item['state']],
            'needs_action': item['state'] == 'failed' or waiting, 'attempts': item['attempts'],
            'next_attempt_at': item['next_attempt_at'], 'delivered_at': item['delivered_at'],
            'connector': connector.name if connector is not None else None,
            'delivered_to': recipients or [], 'recipients_recorded': recipients is not None}


def _connector_view(connector):
    settings = connector.settings
    return {'id': connector.id, 'kind': 'email', 'name': connector.name, 'enabled': connector.enabled,
            'match_number': connector.match_number, 'host': settings.host, 'port': settings.port,
            'security': settings.security, 'username': settings.username, 'has_password': connector.has_password,
            'from_address': settings.from_address, 'recipients': list(settings.recipients),
            'subject_template': settings.subject_template, 'managed': connector.managed,
            'version': connector.version}


@router.get('/items', dependencies=[Depends(require_permission('mailboxes:read'))])
async def list_items(request: Request, state: str | None = Query(default=None, pattern='^(received|sending|delivered|failed)$'),
                     limit: int = Query(default=100, ge=1, le=500)):
    store = _store(request)

    def read():
        connectors = {connector.id: connector for connector in store.list_connectors()}
        return [_item_view(item, connectors) for item in store.list_items(state=state, limit=limit)], store.counts()
    items, counts = await _call(read)
    return {'items': items, 'counts': {name: counts.get(name, 0) for name in STATE_TEXT}}


@router.post('/items/{item_id}/retry', dependencies=[Depends(require_permission('settings:write'))])
async def retry_item(item_id: str, request: Request):
    store = _store(request)
    item = await _call(lambda: store.schedule_retry(item_id))
    return _item_view(item, {})


class ConnectorIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=100)
    enabled: bool = True
    match_number: str | None = Field(default=None, max_length=64)
    host: str = Field(max_length=255)
    port: int = 587
    security: str = 'starttls'
    username: str = Field(default='', max_length=200)
    password: str | None = Field(default=None, max_length=1024)
    from_address: str = Field(max_length=320)
    recipients: list[str] | str = Field(max_length=20)
    subject_template: str = Field(default='Fax from {from_number}', max_length=200)
    version: int | None = Field(default=None, ge=1)

    def settings(self):
        return {'host': self.host, 'port': self.port, 'security': self.security, 'username': self.username,
                'from_address': self.from_address, 'recipients': self.recipients,
                'subject_template': self.subject_template}


@router.get('/connectors', dependencies=[Depends(require_permission('mailboxes:read'))])
async def list_connectors(request: Request):
    store = _store(request)
    _, runtime = installation_engine(request.app)

    def read():
        sync_settings_now(store, runtime)
        return store.list_connectors()
    connectors = await _call(read)
    return {'connectors': [_connector_view(connector) for connector in connectors]}


@router.post('/connectors', dependencies=[Depends(require_permission('settings:write'))], status_code=201)
async def create_connector(payload: ConnectorIn, request: Request):
    store = _store(request)
    connector = await _call(lambda: store.create_connector(
        name=payload.name, settings=payload.settings(), password=payload.password or None,
        enabled=payload.enabled, match_number=payload.match_number))
    return _connector_view(connector)


@router.put('/connectors/{connector_id}', dependencies=[Depends(require_permission('settings:write'))])
async def update_connector(connector_id: str, payload: ConnectorIn, request: Request):
    store = _store(request)
    connector = await _call(lambda: store.update_connector(
        connector_id, version=payload.version, name=payload.name, settings=payload.settings(),
        password=payload.password, enabled=payload.enabled, match_number=payload.match_number))
    return _connector_view(connector)


@router.delete('/connectors/{connector_id}', dependencies=[Depends(require_permission('settings:write'))])
async def delete_connector(connector_id: str, request: Request):
    store = _store(request)
    if not await _call(lambda: store.delete_connector(connector_id)):
        raise HTTPException(404, detail='This connector no longer exists.')
    return {'deleted': True}


@router.post('/connectors/{connector_id}/test', dependencies=[Depends(require_permission('settings:write'))])
async def test_connector(connector_id: str, request: Request):
    store = _store(request)

    def run():
        connector = store.get_connector(connector_id)
        if connector is None:
            raise IntakeConflict('This connector no longer exists.')
        try:
            send(connector.settings, store.password(connector.id), test_message(connector.settings, connector.id))
        except (DefiniteFailure, AmbiguousFailure) as error:
            return {'ok': False, 'detail': str(error)}
        recipients = ', '.join(connector.settings.recipients)
        return {'ok': True, 'detail': f'A test email was sent to {recipients}.'}
    return await _call(run)
