"""Delivery setup → Blocked senders, and "Mark sender as junk" on a received fax (inbound/screening.py).

Reading needs ``settings:read``; blocking and unblocking need ``settings:write``
and write one access audit row each (who, which number, why). The router's
background task keeps Asterisk's copy of the list exact and records the calls
Asterisk turned away; it only uses an existing connection to Asterisk.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity, runtime as access_runtime
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from . import screening

SYNC_EVERY_SECONDS = 300.0
DRAIN_EVERY_SECONDS = 60.0


class ScreeningSync:
    """Keeps Asterisk's blocked list exact and records its rejections; runs only while Faxbot is connected."""

    def __init__(self, app, ami):
        self.app, self.ami = app, ami
        self.dirty = True
        self.drain_now = True
        self.synced_for = None  # the connection (connected_at) the list was last loaded on
        self.synced_at = 0.0
        self.drained_at = 0.0
        ami.on_screened(lambda _event: self.wake(drain=True))

    def wake(self, *, sync=False, drain=False):
        self.dirty = self.dirty or sync
        self.drain_now = self.drain_now or drain

    @property
    def synced(self):
        return self.ami._connected.is_set() and self.synced_for == self.ami.connected_at and not self.dirty

    def _store(self):
        engine, _ = installation_engine(self.app)
        return screening.ScreeningStore(engine) if engine is not None else None

    async def step(self):
        if not self.ami._connected.is_set():
            return False
        now = time.monotonic()
        connection = self.ami.connected_at
        store = None
        if self.dirty or self.synced_for != connection or now - self.synced_at > SYNC_EVERY_SECONDS:
            store = await run_lifecycle_step(self._store)
            if store is None:
                return False
            self.dirty = False
            try:
                await screening.sync(self.ami, store)
            except BaseException:
                self.dirty = True
                raise
            self.synced_for, self.synced_at = connection, now
        if self.drain_now or now - self.drained_at > DRAIN_EVERY_SECONDS:
            store = store or await run_lifecycle_step(self._store)
            if store is None:
                return False
            self.drain_now = False
            await screening.drain(self.ami, store)
            self.drained_at = now
        return False


def _background(app):
    from ..ami import ami_client
    sync = ScreeningSync(app, ami_client)
    app.state.screening_sync = sync
    return [('faxbot-junk-screening', repeat_async(sync.step, interval=5.0, initial_delay=5.0,
                                                   warning='Blocked senders could not be loaded into the fax engine '
                                                           'just now; Faxbot tries again shortly.'))]


router = APIRouter(prefix='/screening', tags=['Blocked senders'], lifespan=lifespan_tasks(_background))


class BlockBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # One of the two: a number, or the received fax whose sender is junk.
    number: str | None = Field(default=None, max_length=40)
    inbound_id: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=1, max_length=200)
    days: int = Field(default=screening.DEFAULT_DAYS, ge=1, le=screening.MAX_DAYS)


def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _wake(request):
    sync = getattr(request.app.state, 'screening_sync', None)
    if sync is not None:
        sync.wake(sync=True)


def _actor(request, identity):
    """(principal ID, display name as it is now)."""
    import sqlalchemy as sa
    actor = identity.actor
    principal_id = getattr(actor, 'principal_id', None)
    if principal_id is None:
        return None, None
    service = access_runtime(request)
    principals = service.store.tables['access_principals']
    with service.store.engine.connect() as connection:
        name = connection.execute(sa.select(principals.c.display_name)
                                  .where(principals.c.id == principal_id)).scalar()
    return principal_id, name


def _audit(request, identity, operation, target_id, details):
    from ..access.http import utcnow
    service = access_runtime(request)
    actor = identity.actor
    credential = getattr(actor, 'credential', None)
    with service.store.transaction() as connection:
        version = service.store.require_lock_on(connection)
        connection.execute(service.store.tables['access_audit'].insert().values(
            id=uuid.uuid4().hex, actor_principal_id=getattr(actor, 'principal_id', None),
            actor_key_binding_id=getattr(credential, 'binding_id', None),
            actor_session_id=getattr(credential, 'session_id', None), operation=operation,
            target_kind='installation', target_id=target_id[:100], policy_version_before=version,
            policy_version_after=version, outcome='allowed',
            details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
            created_at=utcnow()))
    try:
        from ..audit import audit_event
        audit_event(operation.replace('.', '_'), **details)
    except Exception:
        pass


def _entry_view(entry, counts, now):
    return {'id': entry['id'], 'number': entry['number'], 'reason': entry['reason'],
            'added_by': entry['added_by_name'], 'added_at': entry['created_at'].isoformat() + 'Z',
            'expires_at': entry['expires_at'].isoformat() + 'Z',
            'removed_at': entry['removed_at'].isoformat() + 'Z' if entry['removed_at'] else None,
            'removed_by': entry['removed_by_name'],
            'active': entry['removed_at'] is None and entry['expires_at'] > now,
            'rejected_calls': counts.get(entry['id'], 0), 'inbound_id': entry['inbound_fax_id']}


def _sentence(values, active, synced):
    from .sip_handover import receives_over_trunk
    if not receives_over_trunk(values):
        return ('Blocking works for faxes received over your SIP trunk. Faxes from your other providers still '
                'arrive from blocked senders.')
    if not active:
        return 'No sender is blocked. Mark a received fax\'s sender as junk to turn their calls away before answering.'
    count = f'{active} blocked number' + ('' if active == 1 else 's')
    if synced:
        return f'Your fax engine turns away calls from {count} before answering, so they cost nothing.'
    return f'Faxbot loads {count} into your fax engine as soon as it reaches it.'


@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def read_screening(request: Request):
    values = request.scope['faxbot.configuration'].active.values
    engine = _engine(request)

    def run():
        store = screening.ScreeningStore(engine)
        now = screening.utcnow()
        counts = store.counts()
        entries = [_entry_view(entry, counts, now) for entry in store.entries()]
        rejections = [{'id': row['id'], 'number': row['number'], 'called': row['called'],
                       'rejected_at': row['rejected_at'].isoformat() + 'Z', 'entry_id': row['entry_id']}
                      for row in store.rejections()]
        return entries, rejections
    entries, rejections = await run_lifecycle_step(run)
    sync = getattr(request.app.state, 'screening_sync', None)
    synced = bool(sync is not None and sync.synced)
    active = sum(1 for entry in entries if entry['active'])
    return {'sentence': _sentence(values, active, synced), 'synced': synced, 'entries': entries,
            'rejections': rejections}


@router.post('/senders', dependencies=[Depends(require_permission('settings:write'))])
async def block_sender(body: BlockBody, request: Request, identity=Depends(require_identity)):
    """Block a sender: a number, or the sender of a received fax. Expires after ``days`` (90 by default)."""
    values = request.scope['faxbot.configuration'].active.values
    engine = _engine(request)
    if (body.number is None) == (body.inbound_id is None):
        raise HTTPException(400, detail='Choose a number or a received fax to block.')

    def run():
        raw = body.number
        if body.inbound_id is not None:
            raw = screening.sender_of(engine, body.inbound_id)
            if raw is None and not _fax_exists(engine, body.inbound_id):
                raise HTTPException(404, detail='That received fax no longer exists.')
        try:
            number = screening.read_number(raw, getattr(values, 'fax_default_country', 'US') or 'US')
        except screening.ScreeningRefused as refused:
            raise HTTPException(400, detail=str(refused)) from None
        actor_id, actor_name = _actor(request, identity)
        try:
            entry = screening.ScreeningStore(engine).add(number, body.reason, actor_id=actor_id,
                                                         actor_name=actor_name, inbound_fax_id=body.inbound_id,
                                                         days=body.days)
        except screening.ScreeningRefused as refused:
            raise HTTPException(400, detail=str(refused)) from None
        _audit(request, identity, 'screening.block', entry['id'],
               {'number': number, 'reason': entry['reason'], 'days': body.days, 'inbound_id': body.inbound_id})
        return entry
    entry = await run_lifecycle_step(run)
    _wake(request)
    return {'ok': True, 'entry': _entry_view(entry, {}, screening.utcnow())}


def _fax_exists(engine, inbound_id):
    import sqlalchemy as sa
    faxes = sa.Table('inbound_faxes', sa.MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        return connection.execute(sa.select(faxes.c.id).where(faxes.c.id == inbound_id)).first() is not None


@router.delete('/senders/{entry_id}', dependencies=[Depends(require_permission('settings:write'))])
async def unblock_sender(entry_id: str, request: Request, identity=Depends(require_identity)):
    """Unblock: the entry keeps its history, marked removed by you, now."""
    engine = _engine(request)

    def run():
        store = screening.ScreeningStore(engine)
        if store.get(entry_id) is None:
            raise HTTPException(404, detail='That blocked sender is no longer listed.')
        actor_id, actor_name = _actor(request, identity)
        entry = store.remove(entry_id, actor_id=actor_id, actor_name=actor_name)
        _audit(request, identity, 'screening.unblock', entry_id, {'number': entry['number']})
        return entry
    entry = await run_lifecycle_step(run)
    _wake(request)
    return {'ok': True, 'entry': _entry_view(entry, {}, screening.utcnow())}
