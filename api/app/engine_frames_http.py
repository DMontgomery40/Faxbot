"""Recipients → Details, "Their fax machine": what a number's fax machine said, what Faxbot learned, and IAF.

Reads need ``settings:read``; approving or removing a fax server for Internet
Aware Fax, and telling Faxbot to forget what failed with a number, need
``settings:write`` and write an access audit row. The router's background work
records every FaxFrames event (patch 0004) and what each placed call used
(``engine_learning``), keeps what recent calls taught, and keeps Asterisk's
faxbot-iaf, faxbot-inrate and faxbot-inmode keys exact while Faxbot is connected.
"""
import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .access.http import require_identity
from .access.route_policy import require_permission
from .config_runtime import run_lifecycle_step
from . import engine_frames, engine_learning
from .routing.background import installation_engine, lifespan_tasks, repeat_async

SYNC_EVERY_SECONDS = 600.0
# What failed calls taught is kept at least this often: the SSL Fax engine's result can be stored after the call's
# last event, and a failed fax may try another call soon after.
LEARN_EVERY_SECONDS = 60.0


class FramesWork:
    """Records FaxFrames events and keeps Asterisk's per-caller keys exact; only on an existing connection."""

    def __init__(self, app, ami):
        self.app, self.ami = app, ami
        self.dirty = True
        self.synced_for = None
        self.synced_at = 0.0
        self.learned_at = 0.0
        ami.on_frames(self.heard)
        # What each placed call used (both engines' Submission events), and new results to learn from.
        ami.on_submission(self.submitted)
        for listen in (ami.on_fax_result, ami.on_inbound_call, ami.on_engine_call):
            listen(self.result)

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

    def submitted(self, event):
        """Keep what a placed call used and why (fax_call_choices), off the event loop; never raises."""
        if not isinstance(event.get('Learned'), dict):
            return
        engine, _ = installation_engine(self.app)
        if engine is None:
            return

        def record():
            try:
                engine_learning.record_choice(engine, event)
            except Exception:
                logging.getLogger(__name__).warning('What Faxbot changed for a fax call could not be recorded.')
        try:
            asyncio.get_running_loop().run_in_executor(None, record)
        except RuntimeError:
            record()

    def result(self, _event):
        self.dirty = True

    def wake(self):
        self.dirty = True

    async def step(self):
        if not self.ami._connected.is_set():
            return False
        now = time.monotonic()
        connection = self.ami.connected_at
        if not (self.dirty or self.synced_for != connection or now - self.synced_at > SYNC_EVERY_SECONDS
                or now - self.learned_at > LEARN_EVERY_SECONDS):
            return False
        engine, _ = installation_engine(self.app)
        values = await run_lifecycle_step(self._values)
        if engine is None or values is None:
            return False
        self.dirty = False
        try:
            # What recent failed calls taught, received ones too (their callers' keys follow below).
            await run_lifecycle_step(lambda: engine_learning.learn_recent(engine, values))
            self.learned_at = now
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


MODE_WORDS = {'t38': 'Fax over IP (T.38)', 'audio': 'Audio fax'}
# Only the fax engine is named to people; the built-in engine is simply Faxbot.
ENGINE_WORDS = {'hylafax': "Faxbot's fax engine"}


def _call_view(view):
    """One call with a number: its call record, its engine's report and its frames, joined (engine_learning)."""
    from .fax_negotiation import call_sentence
    from .sip_calls import call_summary
    frame, record, negotiated = view['frame'] or {}, view['record'], view['engine_row']
    sentences = engine_frames.describe(frame) if frame else []
    if negotiated is not None and (negotiated.get('negotiation_by') or negotiated.get('sslfax') == 1):
        sentences.append(call_sentence(negotiated, record))
    elif frame and (view['compression'] or view['ecm']):
        coding = ', '.join(part for part in (
            f"{view['compression']} compression" if view['compression'] not in (None, 'mixed') else None,
            {'on': 'error correction', 'off': 'no error correction'}.get(view['ecm'])) if part)
        if coding:
            sentences.append(f'The call used {coding}.')
    try:
        outcome = call_summary(record) if record is not None else None
    except Exception:
        outcome = None
    return {'when': view['when'].isoformat() + 'Z', 'direction': 'out' if view['direction'] == 'outbound' else 'in',
            'mode': {'t38': 'T38', 'audio': 'audio'}.get(view['mode']), 'status': view['status'],
            'rate_first': view['rate_first'], 'rate_lowest': view['rate_lowest'], 'trainings': view['trainings'],
            'failures_to_train': view['ftt'], 't38_after_ms': view['t38_after_ms'], 't38_by': view['t38_by'],
            'iaf': view['iaf'], 'sentences': sentences,
            'capabilities': engine_frames.decode_dis(frame.get('dis')) if frame else None,
            'subaddress': engine_frames.decode_sub(frame.get('sub')) if frame else None,
            # Joined from the call record and the engine's report (both engines).
            'engine': view['engine'], 'engine_label': ENGINE_WORDS.get(view['engine']),
            'mode_label': MODE_WORDS.get(view['mode']), 'outcome': outcome, 'pages': view['pages'],
            'seconds': view['seconds'], 'compression': view['compression'], 'ecm': view['ecm'],
            'resolution': view['resolution'],
            'changes': engine_learning.choice_sentences(view['choice'])}


def _memory_view(row):
    return {'direction': row['direction'], 'kind': row['kind'], 'engine': row['engine'],
            'learned_at': row['learned_at'].isoformat() + 'Z', 'expires_at': row['expires_at'].isoformat() + 'Z',
            'active': row['active'], 'ended': row['ended']}


def fax_machine_view(engine, values, number, *, now=None):
    """Everything "Their fax machine" shows for one number; reads only."""
    from .hylafax_engine import try_t38
    now = now or engine_learning.utcnow()
    store = engine_frames.FrameStore(engine)
    epoch = engine_learning.current_epoch(engine, values, now=now, write=False)
    calls = engine_learning.joined_calls(engine, number, limit=10)
    recent = engine_learning.joined_calls(engine, number, limit=50, since=engine_learning.evidence_since(
        epoch, now, max(engine_learning.LEARN_DAYS, engine_learning.MEMORY_DAYS)))
    learned = engine_frames.learned_for(store, values, number, now=now)
    t38 = try_t38(values)
    decisions = [engine_learning.decide(values, number, engine=kind, t38=t38, base_ecm=getattr(values, 'sip_fax_ecm', True),
                                        base_compression=getattr(values, 'sip_fax_compression', None), db=engine,
                                        now=now) for kind in ('builtin', 'hylafax')]
    rows = engine_learning.memories(engine, number, epoch=epoch, now=now, views=recent)
    sentences, notes = [], []
    for decision in decisions:
        sentences += [text for text in decision.reasons if text not in sentences]
        notes += [text for text in decision.notes if text not in notes and text not in sentences]
    received = engine_learning.memory_sentence(rows, 'inbound')
    if received:
        sentences.append(received)
    if learned.inbound_rate:
        sentences.append('Calls from this number over audio are received at 14,400 bit/s: its line trained '
                         'cleanly at that speed on your faxes to it.')
    since = None
    if not epoch.get('first'):
        since = (f'Faxbot started learning about fax numbers again on {engine_learning._day(epoch["started_at"])}, '
                 'when your trunk settings or a fax engine changed.')
    return {
        'number': number, 'calls': [_call_view(view) for view in calls],
        'learned': {'t38_now': learned.t38_now, 'max_rate': learned.max_rate, 'inbound_rate': learned.inbound_rate,
                    'audio': decisions[0].audio, 'compression': decisions[1].compression,
                    'ecm_on': any(decision.ecm_on for decision in decisions), 'sentences': sentences,
                    'notes': notes, 'since': since},
        'memory': [_memory_view(row) for row in rows], 'can_forget': any(row['active'] for row in rows),
        'iaf': engine_frames.iaf_for(store, engine, number),
        'sentence': ('No fax call with this number has reported its fax machine yet.' if not calls else
                     f'From the last {len(calls)} call{"" if len(calls) == 1 else "s"} with this number.'),
    }


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

    return await run_lifecycle_step(lambda: fax_machine_view(engine, values, number))


@router.post('/numbers/{number}/forget', dependencies=[Depends(require_permission('settings:write'))])
async def forget_fax_machine(number: str, request: Request, identity=Depends(require_identity)):
    """Forget what failed with this number (fax over IP or audio fax, both directions): its next calls use the
    usual settings. What the calls themselves recorded stays."""
    values = request.scope['faxbot.configuration'].active.values
    number = _number(number, values)
    engine = _engine(request)

    def run():
        actor_id, actor_name = _actor(request, identity)
        count = engine_learning.forget(engine, number, actor_id=actor_id, actor_name=actor_name)
        if count:
            _audit(request, identity, 'fax_machine.forget', number, {'number': number, 'forgotten': count})
        return count
    count = await run_lifecycle_step(run)
    _wake(request)
    sentence = (f'Faxbot forgot what failed with {number}; its next calls use the usual settings.' if count
                else engine_learning.FORGET_NOTHING)
    return {'ok': True, 'forgotten': count, 'sentence': sentence}


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

