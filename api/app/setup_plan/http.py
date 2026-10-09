"""Guided setup over HTTP: preview a plan, read it, and apply it (System → Setup, ``faxbot system setup``).

Reading plans needs ``settings:read``. Previewing needs ``settings:write``:
it keeps the plan, and nothing else. Applying needs ``settings:write``, and
the service checks again, before anything is written, every permission the
chosen parts need (``mailboxes:manage`` for a mailbox's rules).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from ..access.http import require_identity, runtime as access_runtime
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.database import DeliveryStoreError, read_connection
from ..rules.store import when
from .context import ContextError
from .service import PlanForbidden, PlanNotFound, PlanRefused, PlanStale, SetupPlan


router = APIRouter(prefix='/setup/plans', tags=['Guided setup'])


class MailboxCountry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    country: str = Field(default='', max_length=2)


class ContextBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    organization_name: str = Field(default='', max_length=200)
    country: str = Field(default='', max_length=2)
    mailboxes: dict[str, MailboxCountry] = Field(default_factory=dict, max_length=500)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]{64}$')
    # The suggestions to apply, by key; left out, every suggestion the plan chose for you.
    items: list[str] | None = Field(default=None, max_length=500)


def _engine_and_runtime(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine, runtime


def _bound(runtime, snapshot):
    """The active outbound provider, as the route planner names it (as Costs → Recommendations reads it)."""
    identity = snapshot.active.profile_id('outbound')
    if identity is None:
        return None
    return runtime.manager.store.read_profile(identity).configuration.provider_id


def _service(request):
    engine, runtime = _engine_and_runtime(request)
    snapshot = request.scope['faxbot.configuration']
    from ..direct.relay_http import relay_for
    try:
        return SetupPlan(engine, manager=runtime.manager, access=access_runtime(request), relay=relay_for(request.app),
                         bound=_bound(runtime, snapshot)), snapshot
    except DeliveryStoreError:
        raise HTTPException(503, detail='Setup plans are unavailable just now. Try again in a minute.') from None


def _actor_name(engine, identity):
    principal = getattr(identity.actor, 'principal_id', None)
    if not principal:
        return None
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with read_connection(engine) as connection:
        return connection.execute(sa.select(principals.c.display_name).where(principals.c.id == principal)
                                  ).scalar_one_or_none()


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except PlanNotFound as error:
        raise HTTPException(404, detail=str(error)) from None
    except PlanStale as error:
        raise HTTPException(409, detail=str(error)) from None
    except PlanForbidden as error:
        raise HTTPException(403, detail=str(error)) from None
    except (PlanRefused, ContextError) as error:
        raise HTTPException(400, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Setup plans are unavailable just now. Try again in a minute.') from None


def plan_view(found):
    """A plan as the console and ``faxbot`` show it; the rule targets stay in the stored plan."""
    plan = found['plan']
    return {'number': found['number'], 'revision': found['basis'], 'created_at': when(found['created_at']),
            'actor_name': found.get('actor_name'), 'context': found['context'], 'packs': plan['packs'],
            'missing': plan['missing'], 'mailboxes': plan['mailboxes'], 'workflows': plan['workflows'],
            'checks': plan.get('checks') or {},
            'applications': [{'outcome': row['outcome'], 'items': row['items'], 'steps': row['steps'],
                              'restart_required': bool(row['restart_required']), 'actor_name': row['actor_name'],
                              'created_at': when(row['created_at'])} for row in found.get('applications') or ()]}


def _mailbox_choices(engine, values):
    """Each mailbox for the description form, with the country its numbers are in as a hint (never a default)."""
    from ..routing.destinations import classify
    from .facts import mailboxes
    found = []
    for box in mailboxes(engine, values):
        regions = sorted({classify(number, 'ZZ').region for number in box['numbers']} - {None})
        found.append({'id': box['id'], 'name': box['name'], 'numbers': box['numbers'],
                      'numbers_country': regions[0] if len(regions) == 1 else None})
    return found


@router.get('/latest', dependencies=[Depends(require_permission('settings:read'))])
async def latest_plan(request: Request):
    """The newest plan (or none), and the mailboxes the description form asks about."""
    service, snapshot = _service(request)

    def read():
        found = service.plans.latest()
        if found is not None:
            found['applications'] = service.plans.applications_of(found['id'])
        return {'plan': plan_view(found) if found else None,
                'mailboxes': _mailbox_choices(service.engine, snapshot.desired.values)}
    return await _call(read)


@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def list_plans(request: Request):
    service, _ = _service(request)
    rows = await _call(lambda: service.plans.recent())
    return {'plans': [{'number': row['number'], 'created_at': when(row['created_at']),
                       'actor_name': row['actor_name']} for row in rows]}


@router.post('', dependencies=[Depends(require_permission('settings:write'))])
async def preview_plan(body: ContextBody, request: Request, identity=Depends(require_identity)):
    """Compile a plan from what Faxbot knows and keep it. Sends nothing and changes no setting or rule."""
    service, snapshot = _service(request)

    def run():
        found = service.preview(body.model_dump(), snapshot=snapshot,
                                actor_principal_id=getattr(identity.actor, 'principal_id', None),
                                actor_name=_actor_name(service.engine, identity))
        found['applications'] = []
        return plan_view(found)
    return await _call(run)


@router.get('/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_plan(number: int, request: Request):
    service, _ = _service(request)
    return await _call(lambda: plan_view(service.get(number)))


@router.post('/{number}/apply', dependencies=[Depends(require_permission('settings:write'))])
async def apply_plan(number: int, body: ApplyBody, request: Request, identity=Depends(require_identity)):
    """Apply the chosen suggestions of a previewed plan; refused when anything it builds on changed."""
    service, snapshot = _service(request)

    def run():
        result = service.apply(number, body.expected_revision, snapshot=snapshot, actor=identity.actor,
                               actor_name=_actor_name(service.engine, identity), items=body.items)
        return {**result, 'plan': plan_view(service.get(number))}
    return await _call(run)
