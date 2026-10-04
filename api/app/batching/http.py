"""HTTP surface for sending faxes to one number together.

Per-number settings use the settings permissions. Whether a destination sends
together, and a fax's own waiting or shared-call details, are for anyone who
may send faxes or read that fax.
"""
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import private_operation, require_identity
from ..access.route_policy import require_permission
from ..audit import audit_event
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.costs import format_amount
from ..routing.database import DeliveryStoreError
from ..routing.numbers import InvalidNumber, normalize_number
from . import money, policy
from .store import BatchingConflict, BatchingInputError, BatchingSettings, member, members_for


router = APIRouter(prefix='/batching', tags=['Sending together'])


def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _number(value, request):
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except BatchingInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    except BatchingConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Sending-together storage is unavailable.') from None


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value is not None else None


def _active_provider(request):
    """The active revision's outbound provider identity, or None."""
    snapshot = request.scope.get('faxbot.configuration')
    _, runtime = installation_engine(request.app)
    if snapshot is None or runtime is None:
        return None
    identity = snapshot.active.profile_id('outbound')
    return None if identity is None else runtime.manager.store.read_profile(identity).configuration.provider_id


def _verdict(engine, request, number):
    from ..routing.store import RouteStore
    revision = request.scope['faxbot.configuration'].active
    bound = _active_provider(request)
    return policy.evaluate(RouteStore(engine), number, revision.values, bound)


def summary(row):
    """A fax's sending-together state for Jobs, or None when it never waited to go with others."""
    if row is None:
        return None
    view = {'state': row['state'], 'reference': row['reference'] or 'Faxbot ' + row['id'][:8]}
    if row['state'] == 'waiting':
        view.update(waiting_until=_utc(row['hold_until']), send_now=bool(row['urgent']))
    elif row['state'] == 'together':
        view.update(documents=row['documents'], document_number=row['document_number'],
                    others=row['documents'] - 1)
    return view


def summaries(engine, job_ids):
    try:
        rows = members_for(engine, job_ids)
    except DeliveryStoreError:
        return {}
    return {job_id: summary(row) for job_id, row in rows.items()}


def _sentence(view):
    if view is None:
        return None
    if view['state'] == 'waiting':
        return 'Waiting to go with other faxes to this number.'
    if view['state'] == 'together':
        others = view['others']
        return f'Sent in one call with {"1 other fax" if others == 1 else f"{others} other faxes"}.'
    return 'Sent on its own.'


def _change_view(row):
    return {'action': row['action'], 'by': row['actor_name'] or 'Someone with settings access',
            'at': _utc(row['created_at']), 'recipient_agreed': bool(row['recipient_agreed']),
            'max_wait_minutes': row['max_wait_seconds'] // 60, 'max_pages': row['max_pages'],
            'mixed_senders': bool(row['mixed_senders'])}


def _number_view(engine, request, number):
    from ..routing.store import RouteStore
    settings = BatchingSettings(engine)
    setting = settings.get(number)
    verdict = _verdict(engine, request, number)
    history = settings.history(number)
    agreement = next((row for row in history if row['action'] == 'on'), None) if setting['enabled'] else None
    found = money.savings(RouteStore(engine), engine, number)
    if not setting['enabled']:
        state = 'Off: faxes to this number go straight away.'
    elif verdict.saves:
        state = (f'On: a fax to this number waits up to {policy.wait_text(setting["max_wait_seconds"])} '
                 'to go in one call with others.')
    else:
        state = 'On, but faxes go straight away because sending together saves nothing on this route.'
    return {'number': number, 'enabled': setting['enabled'],
            'max_wait_minutes': setting['max_wait_seconds'] // 60, 'max_pages': setting['max_pages'],
            'mixed_senders': setting['mixed_senders'], 'version': setting['version'],
            'saves_money': verdict.saves, 'route_sentence': verdict.sentence, 'state_sentence': state,
            'agreement': None if agreement is None else _change_view(agreement),
            'history': [_change_view(row) for row in history],
            'savings': {'calls': found['calls'], 'faxes': found['faxes'], 'calls_saved': found['calls_saved'],
                        'estimated_saving': [{'currency': currency, 'amount': format_amount(micros)}
                                             for currency, micros in sorted(found['saved'].items())],
                        'is_estimate': True, 'sentence': money.savings_sentence(found)},
            'agreement_text': policy.AGREEMENT}


@router.get('/numbers/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_number(number: str, request: Request):
    number = _number(number, request)
    engine = _engine(request)
    return await _call(lambda: _number_view(engine, request, number))


class NumberSetting(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    recipient_agreed: bool = False
    max_wait_minutes: int | None = Field(default=None, ge=1, le=60)
    max_pages: int | None = Field(default=None, ge=policy.MIN_PAGES, le=policy.MAX_PAGES)
    mixed_senders: bool | None = None
    version: int | None = Field(default=None, ge=0)


def _actor_name(engine, actor):
    import sqlalchemy as sa
    from ..routing.database import read_connection
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    try:
        with read_connection(engine) as connection:
            return connection.execute(sa.select(principals.c.display_name).where(
                principals.c.id == actor.principal_id)).scalar()
    except DeliveryStoreError:
        return None


async def _save(request, identity, number, payload):
    engine = _engine(request)
    actor = identity.actor

    def save():
        name = _actor_name(engine, actor)
        setting, action = BatchingSettings(engine).save(
            number, enabled=payload.enabled, recipient_agreed=payload.recipient_agreed, actor=actor.replay_scope,
            actor_name=name, expected_version=payload.version,
            max_wait_seconds=None if payload.max_wait_minutes is None else payload.max_wait_minutes * 60,
            max_pages=payload.max_pages, mixed_senders=payload.mixed_senders)
        if action is not None:
            audit_event('batching_' + action, to_number=number, recipient_agreed=payload.enabled,
                        max_wait_seconds=setting['max_wait_seconds'], max_pages=setting['max_pages'],
                        mixed_senders=setting['mixed_senders'])
        return _number_view(engine, request, number)
    return await _call(save)


@router.put('/numbers/{number}')
async def put_number(number: str, payload: NumberSetting, request: Request,
                     identity=Depends(require_permission('settings:write'))):
    return await _save(request, identity, _number(number, request), payload)


@router.delete('/numbers/{number}')
async def delete_number(number: str, request: Request, identity=Depends(require_permission('settings:write'))):
    return await _save(request, identity, _number(number, request), NumberSetting(enabled=False))


@router.get('/check')
async def check(request: Request, to: str = Query(min_length=1, max_length=64), identity=Depends(require_identity)):
    """Whether a fax to ``to`` would wait to go with others, for the Send page's "Send now" choice."""
    access = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: access.outbound.check_send(identity.actor)))
    number = _number(to, request)
    engine = _engine(request)

    def read():
        setting = BatchingSettings(engine).get(number)
        if not setting['enabled'] or _active_provider(request) != 'sip':
            return {'number': number, 'sends_together': False, 'wait_minutes': None, 'sentence': None}
        verdict = _verdict(engine, request, number)
        if not verdict.saves:
            return {'number': number, 'sends_together': False, 'wait_minutes': None, 'sentence': None}
        wait = policy.wait_text(setting['max_wait_seconds'])
        return {'number': number, 'sends_together': True, 'wait_minutes': setting['max_wait_seconds'] // 60,
                'sentence': f'Faxes to this number wait up to {wait} to go in one call with others.'}
    return await _call(read)


def _fax_view(engine, job_id):
    from ..routing.store import RouteStore
    row = member(engine, job_id)
    view = summary(row)
    if view is None:
        return {'state': None, 'sentence': None}
    view['sentence'] = _sentence(view)
    view['share'] = None
    if row['state'] == 'together':
        found = money.share(RouteStore(engine), engine, row)
        if found is not None:
            view['share'] = {'amount': format_amount(found['amount_micros']),
                             'call_amount': format_amount(found['call_micros']), 'currency': found['currency'],
                             'basis': found['basis'], 'sentence': found['sentence']}
    return view


@router.get('/faxes/{job_id}')
async def fax(job_id: str, request: Request, identity=Depends(require_identity)):
    """A fax's waiting or shared-call details, for anyone who may read that fax."""
    access = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: access.queries.job(identity.actor, job_id)))
    engine = _engine(request)
    return await _call(lambda: _fax_view(engine, job_id))


@router.post('/faxes/{job_id}/send-now')
async def send_now(job_id: str, request: Request, identity=Depends(require_identity)):
    """Send a waiting fax now; the faxes waiting with it go in the same call. Never sends anything twice."""
    access = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: access.queries.job(identity.actor, job_id)))
    await run_lifecycle_step(private_operation(lambda: access.outbound.check_send(identity.actor)))
    from ..outbound_store import OutboundStore
    _, runtime = installation_engine(request.app)
    if runtime is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    delivery = OutboundStore(runtime.manager.store)
    urged = await _call(lambda: delivery.urge(job_id))
    if not urged:
        raise HTTPException(409, detail='This fax is not waiting; it is already on its way.')
    audit_event('batching_send_now', job_id=job_id)
    return await _call(lambda: _fax_view(_engine(request), job_id))
