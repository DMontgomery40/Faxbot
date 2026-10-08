"""Notice faxes: the signed partner protocol, the operator's pairing, and the partner switch (``notice.py``).

Partner routes carry no API key; each is authenticated by an Ed25519 signature
from an enrolled partner and answers 404 while direct delivery is switched off.
Operator routes use settings:read and settings:write.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError
from .http import _peer_view, service_for
from .identity import IdentityUnavailable
from .notice import (SCAN_BACK, NoticeConflict, NoticeReceiver, NoticeSender, NoticeStore, notice_view,
                     received_notice_text)
from .service import DirectUnavailable
from .store import DirectConflict


def _background(app):
    engine, _ = installation_engine(app)
    if engine is None:
        return []
    service = service_for(app)

    async def match():
        if not getattr(await run_lifecycle_step(service.values), 'direct_delivery_enabled', False):
            return False
        return await run_lifecycle_step(NoticeReceiver(service).step)

    async def queue():
        return await run_lifecycle_step(NoticeSender(service).step)

    async def tell():
        if not getattr(await run_lifecycle_step(service.values), 'direct_delivery_enabled', False):
            return False
        return await NoticeReceiver(service).tell_partners()
    return [('faxbot-direct-notices', repeat_async(match, interval=60.0, initial_delay=25.0,
                                                   warning='Received faxes are checked for notices shortly.')),
            ('faxbot-direct-notice-faxes', repeat_async(queue, interval=60.0, initial_delay=35.0,
                                                        warning='Notice faxes are queued shortly.')),
            ('faxbot-direct-notice-tell', repeat_async(tell, interval=600.0, initial_delay=40.0,
                                                       warning='Partners are told about paired notices later.'))]


router = APIRouter(prefix='/direct', tags=['Direct delivery'], lifespan=lifespan_tasks(_background))


class SignedIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statement: str = Field(max_length=4096)
    signature: str = Field(max_length=128)


def _partner_call(operation):
    async def run():
        try:
            status, body = await run_lifecycle_step(operation)
        except DirectUnavailable:
            raise HTTPException(404, detail='Not found.') from None
        return JSONResponse(body, status_code=status)
    return run()


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except (NoticeConflict, DirectConflict) as error:
        raise HTTPException(409, detail=str(error)) from None
    except DirectUnavailable:
        raise HTTPException(409, detail='Turn on direct delivery under Recipients → Partners first.') from None
    except IdentityUnavailable:
        raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


# Partner protocol (signature-authenticated) ----------------------------------------------------

@router.post('/notices')
async def receive_notice_link(payload: SignedIn, request: Request):
    """A partner's signed link between a notice ID and the document it sends next; that document is held."""
    service = service_for(request.app)
    return await _partner_call(lambda: NoticeReceiver(service).link(payload.statement, payload.signature))


@router.post('/notices/paired')
async def receive_notice_paired(payload: SignedIn, request: Request):
    """A partner's signed statement that it paired our notice fax with the original."""
    service = service_for(request.app)
    return await _partner_call(lambda: NoticeSender(service).note_paired(payload.statement, payload.signature))


# Operator routes --------------------------------------------------------------------------------

def _views(service, rows):
    store = NoticeStore(service.store.engine)
    names = {peer['id']: peer['organization'] for peer in service.store.list_peers()}
    views = []
    for row in rows:
        original = store.filed_original(row) if row['role'] == 'receiver' and row['state'] == 'paired' else None
        arrived = True
        if row['role'] == 'receiver' and row['state'] == 'waiting':
            delivery = service.store.find('inbound', row['message_id'])
            arrived = delivery is not None and delivery['state'] == 'accepted'
        views.append(notice_view(row, organization=names.get(row['peer_id']), original_fax_id=original,
                                 arrived=arrived))
    return views


@router.get('/notices', dependencies=[Depends(require_permission('settings:read'))])
async def list_notices(request: Request, fax: str | None = Query(default=None, max_length=40)):
    """Notice faxes sent and received, with how each was paired; ``fax`` narrows to one received fax."""
    service = service_for(request.app)

    def read():
        store = NoticeStore(service.store.engine)
        if fax is not None:
            row = store.for_fax(fax)
            if row is None:
                return {'notices': [], 'notice_text': None}
            (view,) = _views(service, [row])
            delivery = service.store.find('inbound', row['message_id'])
            import json
            pages = json.loads(delivery['manifest'])['document']['pages'] if delivery and delivery['manifest'] else None
            return {'notices': [view], 'notice_text': received_notice_text(
                row, organization=view['partner'] or 'a partner', pages=pages)}
        return {'notices': _views(service, store.recent())}
    return await _call(read)


@router.get('/notices/{notice_id}/faxes', dependencies=[Depends(require_permission('settings:read'))])
async def notice_candidates(notice_id: str, request: Request):
    """Received faxes of one or two pages that may be this document's notice, newest first."""
    service = service_for(request.app)

    def read():
        store = NoticeStore(service.store.engine)
        row = store.get(notice_id)
        if row is None or row['role'] != 'receiver':
            raise NoticeConflict('There is no such document waiting for its notice.')
        return {'faxes': [{'id': fax['id'], 'from_number': fax['from_number'], 'pages': fax['pages'],
                           'received_at': fax['received_at'] or fax['created_at']}
                          for fax in store.recent_faxes(row['created_at'] - SCAN_BACK)]}
    return await _call(read)


class PairIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str | None = Field(default=None, max_length=40)
    fax_id: str | None = Field(default=None, max_length=40)


@router.post('/notices/{notice_id}/pair')
async def pair_notice(notice_id: str, payload: PairIn, request: Request,
                      identity=Depends(require_permission('settings:write'))):
    """Pair a held document with its notice: the code typed from the page, the received fax, or neither (file it)."""
    service = service_for(request.app)
    from .relay_http import _actor_name
    actor = getattr(getattr(identity, 'actor', None), 'principal_id', None)
    name = await _actor_name(request, identity)
    row = await _call(lambda: NoticeReceiver(service).pair_by_person(
        notice_id, code=payload.code, fax_id=payload.fax_id, actor=actor, actor_name=name))
    told = await NoticeReceiver(service).tell(row)
    (view,) = await _call(lambda: _views(service, [row]))
    detail = ('Paired with the received notice fax. The document is in Received.' if payload.fax_id
              else 'The document is in Received, without a notice fax linked to it.')
    return {**view, 'detail': detail, 'partner_told': told}


class NoticeFaxIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    on: bool


@router.post('/peers/{peer_id}/notice-fax', dependencies=[Depends(require_permission('settings:write'))])
async def set_notice_fax(peer_id: str, payload: NoticeFaxIn, request: Request):
    """Send each original to this partner directly with a one-page notice by fax (for intakes that need a fax event)."""
    service = service_for(request.app)
    peer = await _call(lambda: service.store.set_notice_fax(peer_id, payload.on))
    detail = (f"Each document to {peer['organization']} now goes directly, with a one-page notice by fax."
              if payload.on else f"Documents to {peer['organization']} now go directly without a notice fax.")
    return {**_peer_view(peer), 'detail': detail}

