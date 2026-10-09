"""Read saved analysis; only the complete owner can start paid model requests."""
from fastapi import APIRouter, Depends, HTTPException, Request
from ..access.http import PrivateAuthRoute, runtime as access_runtime
from ..access.route_policy import authorize, require_permission, request_audit
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine
from ..routing.database import DeliveryStoreError
from .store import AnalysisStore, configured
from .agent import test_connection

router = APIRouter(route_class=PrivateAuthRoute)


def _configuration(request):
    engine, runtime = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return AnalysisStore(engine), runtime.manager.store.read().active.values


@router.get('/analysis', dependencies=[Depends(require_permission('settings:read'))])
async def get_analysis(request: Request):
    def read():
        store, values = _configuration(request)
        return store.status(values)
    try:
        return await run_lifecycle_step(read)
    except DeliveryStoreError:
        raise HTTPException(503, detail='Saved analysis is temporarily unavailable.') from None


@router.post('/analysis/run', status_code=202)
async def run_analysis(request: Request,
                       identity=Depends(require_permission('settings:write', complete_owner=True))):
    def queue():
        store, values = _configuration(request)
        authorize(access_runtime(request), identity.actor, 'settings:write', complete_owner=True,
                  audit=request_audit(request, action='analysis.refresh'))
        return store.queue(values)
    try:
        return await run_lifecycle_step(queue)
    except ValueError:
        raise HTTPException(400, detail='Configure and enable analysis before refreshing it.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Analysis could not be queued. Try again.') from None


@router.post('/analysis/test')
async def test_analysis(request: Request,
                        identity=Depends(require_permission('settings:write', complete_owner=True))):
    def prepare():
        _, values = _configuration(request)
        authorize(access_runtime(request), identity.actor, 'settings:write', complete_owner=True,
                  audit=request_audit(request, action='analysis.test'))
        return values
    values = await run_lifecycle_step(prepare)
    if not configured(values):
        return {'ok': False, 'message': 'Save an analysis model and API key before testing the connection.'}
    return await test_connection(values)
