"""Recipients → Details, "Their fax machine": what a number's fax machine said, what Faxbot learned, and IAF.

Reads need ``settings:read``; approving or removing a fax server for Internet
Aware Fax needs ``settings:write`` and writes an access audit row. The router's
background work records every FaxFrames event (patch 0004) and keeps
Asterisk's faxbot-iaf and faxbot-inrate keys exact while Faxbot is connected.
"""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .access.http import require_identity
from .access.route_policy import require_permission
from .config_runtime import run_lifecycle_step
from . import engine_frames
from .routing.background import installation_engine, lifespan_tasks, repeat_async

SYNC_EVERY_SECONDS = 600.0


class FramesWork:
    """Records FaxFrames events and keeps Asterisk's per-caller keys exact; only on an existing connection."""

    def __init__(self, app, ami):
        self.app, self.ami = app, ami
        self.dirty = True
        self.synced_for = None
        self.synced_at = 0.0
        ami.on_frames(self.heard)

    def _values(self):
        _, runtime = installation_engine(self.app)
        return runtime.manager.store.read().active.values if runtime is not None else None

    def heard(self, event):
        engine, _ = installation_engine(self.app)
        if engine is None:
            return
        values = self._values()
        row = engine_frames.parse_event(event, trunk=engine_frames.trunk_key(values) if values is not None else '')
        if row is None:
            return

        def record():
            try:
                if engine_frames.FrameStore(engine).record(row):
                    self.dirty = True
            except Exception:
                logging.getLogger(__name__).warning("A fax call's far-end details could not be recorded.")
        try:
            asyncio.get_running_loop().run_in_executor(None, record)
        except RuntimeError:
            record()

    def wake(self):
        self.dirty = True

    async def step(self):
        if not self.ami._connected.is_set():
            return False
        now = time.monotonic()
        connection = self.ami.connected_at
        if not (self.dirty or self.synced_for != connection or now - self.synced_at > SYNC_EVERY_SECONDS):
            return False
        engine, _ = installation_engine(self.app)
        values = await run_lifecycle_step(self._values)
        if engine is None or values is None:
            return False
        self.dirty = False
        try:
            store = await run_lifecycle_step(lambda: engine_frames.FrameStore(engine))
            await engine_frames.sync(self.ami, store, engine, values)
        except BaseException:
            self.dirty = True
            raise
        self.synced_for, self.synced_at = connection, now
        return False


def _background(app):
    from .ami import ami_client
    work = FramesWork(app, ami_client)
    app.state.frames_work = work
    return [('faxbot-far-end-frames', repeat_async(work.step, interval=10.0, initial_delay=8.0,
                                                   warning='Faxbot could not update its fax engine with what it '
                                                           'learned about callers just now; it tries again shortly.'))]


router = APIRouter(prefix='/fax-machines', tags=['Their fax machine'], lifespan=lifespan_tasks(_background))


class IafBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    number: str = Field(min_length=3, max_length=40)
    kind: str = Field(pattern=r'^(peer|endpoint)$')
    label: str = Field(min_length=1, max_length=200)


def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _number(text, values):
    from .inbound.screening import ScreeningRefused, read_number
    try:
        return read_number(text, getattr(values, 'fax_default_country', 'US') or 'US')
    except ScreeningRefused:
        raise HTTPException(400, detail=engine_frames.NOT_A_NUMBER.format(number=str(text)[:40])) from None


def _endpoint_view(row):
    return {'id': row['id'], 'number': row['number'], 'kind': row['kind'], 'label': row['label'],
            'added_by': row['added_by_name'], 'added_at': row['created_at'].isoformat() + 'Z',
            'removed_at': row['removed_at'].isoformat() + 'Z' if row['removed_at'] else None}


def _call_view(row):
    return {'when': row['created_at'].isoformat() + 'Z', 'direction': row['direction'], 'mode': row['mode'],
            'status': row['status'], 'rate_first': row['rate_first'], 'rate_lowest': row['rate_lowest'],
            'trainings': row['trainings'], 'failures_to_train': row['ftt'], 't38_after_ms': row['t38_after_ms'],
            't38_by': row['t38_by'], 'iaf': row['iaf'], 'sentences': engine_frames.describe(row),
            'capabilities': engine_frames.decode_dis(row['dis']),
            'subaddress': engine_frames.decode_sub(row['sub'])}


@router.get('/iaf', dependencies=[Depends(require_permission('settings:read'))])
async def list_iaf(request: Request):
    engine = _engine(request)

    def run():
        store = engine_frames.FrameStore(engine)
        return {'servers': [_endpoint_view(row) for row in store.endpoints_list()],
                'partners': sorted(engine_frames.partner_iaf_numbers(engine))}
    return await run_lifecycle_step(run)


@router.get('/numbers/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def read_fax_machine(number: str, request: Request):
    values = request.scope['faxbot.configuration'].active.values
    number = _number(number, values)
    engine = _engine(request)

    def run():
        store = engine_frames.FrameStore(engine)
        learned = engine_frames.learned_for(store, values, number)
        calls = store.calls(number, limit=10)
        return {
            'number': number, 'calls': [_call_view(row) for row in calls],
            'learned': {'t38_now': learned.t38_now, 'max_rate': learned.max_rate,
                        'inbound_rate': learned.inbound_rate,
                        'sentences': [text for text in (learned.t38_reason, learned.rate_reason) if text]
                        + (['Calls from this number over audio are received at 14,400 bit/s: its line trained '
                            'cleanly at that speed on your faxes to it.'] if learned.inbound_rate else [])},
            'iaf': engine_frames.iaf_for(store, engine, number),
            'sentence': ('No fax call with this number has reported its fax machine yet.' if not calls else
                         f'From the last {len(calls)} call{"" if len(calls) == 1 else "s"} with this number.'),
        }
    return await run_lifecycle_step(run)


def _actor(request, identity):
    from .inbound.screening_http import _actor as actor_of
    return actor_of(request, identity)


def _audit(request, identity, operation, target, details):
    from .inbound.screening_http import _audit as audit
    audit(request, identity, operation, target, details)


def _wake(request):
    work = getattr(request.app.state, 'frames_work', None)
    if work is not None:
        work.wake()


@router.post('/iaf', dependencies=[Depends(require_permission('settings:write'))])
async def add_iaf(body: IafBody, request: Request, identity=Depends(require_identity)):
    """Approve a number for Internet Aware Fax: another Faxbot (peer) or an IAF fax server (endpoint)."""
    values = request.scope['faxbot.configuration'].active.values
    number = _number(body.number, values)
    engine = _engine(request)

    def run():
        actor_id, actor_name = _actor(request, identity)
        try:
            row = engine_frames.FrameStore(engine).add_endpoint(number, body.kind, body.label, actor_id=actor_id,
                                                                actor_name=actor_name)
        except engine_frames.FramesRefused as refused:
            raise HTTPException(400, detail=str(refused)) from None
        _audit(request, identity, 'iaf.approve', row['id'], {'number': number, 'kind': body.kind, 'label': row['label']})
        return row
    row = await run_lifecycle_step(run)
    _wake(request)
    return {'ok': True, 'server': _endpoint_view(row)}


@router.delete('/iaf/{server_id}', dependencies=[Depends(require_permission('settings:write'))])
async def remove_iaf(server_id: str, request: Request, identity=Depends(require_identity)):
    engine = _engine(request)

    def run():
        store = engine_frames.FrameStore(engine)
        if not any(row['id'] == server_id for row in store.endpoints_list()):
            raise HTTPException(404, detail='That fax server is no longer approved.')
        actor_id, actor_name = _actor(request, identity)
        row = store.remove_endpoint(server_id, actor_id=actor_id, actor_name=actor_name)
        _audit(request, identity, 'iaf.remove', server_id, {'number': row['number']})
        return row
    row = await run_lifecycle_step(run)
    _wake(request)
    return {'ok': True, 'server': _endpoint_view(row)}

