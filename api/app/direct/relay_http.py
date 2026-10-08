"""Partner relay: agreements for administrators, and the signed relay protocol for partners.

Administrator routes read with settings:read and change with settings:write.
Granting a relay also creates the "Relayed for {partner}" sender, so it needs
permission to manage users as well (checked by the access mutations).
Partner routes carry no API key: each request is a statement signed by an
enrolled partner's key, and they answer 404 while direct delivery is off.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError
from .crypto import DirectProtocolError
from .http import service_for
from .identity import IdentityUnavailable
from .relay import RelayConflict, RelayService, relay_key
from .relay_route import RelayReconciler
from .service import DirectUnavailable


def _delivery(app):
    _, runtime = installation_engine(app)
    if runtime is None:
        return None
    from ..outbound_store import OutboundStore
    return OutboundStore(runtime.manager.store)


def relay_for(app):
    direct = service_for(app)
    return RelayService(direct, access=lambda: getattr(app.state, 'access_runtime', None),
                        delivery=lambda: _delivery(app))


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    relay = relay_for(app)
    reconciler = RelayReconciler(relay.direct, _delivery(app), access=relay.access)
    return [('faxbot-relay', repeat_async(relay.step, interval=60.0, initial_delay=25.0,
                                          warning='Partner relays are waiting to report their faxes; Faxbot tries '
                                                  'again.')),
            ('faxbot-relay-sent', repeat_async(reconciler.step, interval=60.0, initial_delay=35.0,
                                               warning='Faxes sent through partner relays are waiting for their '
                                                       'results; Faxbot asks again.'))]


router = APIRouter(prefix='/direct/relay', tags=['Partner relay'], lifespan=lifespan_tasks(_background))


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except RelayConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except DirectProtocolError as error:
        raise HTTPException(400, detail=str(error)) from None
    except IdentityUnavailable:
        raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Partner relay storage is unavailable.') from None


TOLD = {'told': None, 'refused': '{partner} did not accept it; see their answer under Partners.',
        'unreachable': 'Saved. Faxbot could not reach {partner} just now and tells them as soon as it can.'}


async def _tell(relay, row):
    outcome = await relay.tell(row)
    message = TOLD[outcome]
    return None if message is None else message.format(partner=row.get('organization') or 'The partner')


def _with_partner(relay, row):
    peer = relay.direct.store.get_peer(row['peer_id'])
    return {**row, 'organization': peer['organization'] if peer else 'The partner'}


# Administrator routes --------------------------------------------------------------------------------------------
@router.get('/agreements', dependencies=[Depends(require_permission('settings:read'))])
async def list_agreements(request: Request, partner: str | None = Query(default=None, max_length=40)):
    """Every relay agreement, both ways: partners who relay for you, and partners you relay for."""
    relay = relay_for(request.app)
    rows = await _call(lambda: relay.store.agreements_for(peer_id=partner))
    return {'agreements': [relay.view(row) for row in rows]}


class Spend(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: str = Field(max_length=20)
    currency: str = Field(min_length=3, max_length=3)


class Hours(BaseModel):
    model_config = ConfigDict(extra='forbid')
    days: list[str] = Field(default_factory=lambda: ['mon', 'tue', 'wed', 'thu', 'fri'], max_length=7)
    start: str = Field(alias='from', max_length=5)
    until: str = Field(max_length=5)


class GrantIn(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    partner: str = Field(max_length=40)
    countries: list[str] = Field(default_factory=list, max_length=30)
    regions: list[str] = Field(default_factory=list, max_length=30)
    monthly_pages: int | None = Field(default=None, ge=1, le=1_000_000)
    monthly_spend: Spend | None = None
    hours: Hours | None = None
    together: bool = False
    same_organization: bool = False


@router.post('/agreements', status_code=201)
async def grant(payload: GrantIn, request: Request, identity=Depends(require_permission('settings:write'))):
    """Offer to send a partner's faxes as local calls here; the partner accepts from their Faxbot."""
    relay = relay_for(request.app)
    raw = {'countries': payload.countries, 'regions': payload.regions, 'monthly_pages': payload.monthly_pages,
           'monthly_spend': payload.monthly_spend.model_dump() if payload.monthly_spend else None,
           'hours': ({'days': payload.hours.days, 'from': payload.hours.start, 'until': payload.hours.until}
                     if payload.hours else None),
           'together': payload.together, 'same_organization': payload.same_organization}
    name = await _actor_name(request, identity)
    row = await _call(lambda: _with_partner(relay, relay.grant(payload.partner, raw, actor=identity.actor,
                                                               actor_name=name)))
    detail = await _tell(relay, row)
    row = await _call(lambda: _with_partner(relay, relay.store.agreement(row['id'])))
    return {**relay.view(row), 'detail': detail or f"{row['organization']} has your offer; they accept it from "
                                                    'their Faxbot.'}


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reply_number: str | None = Field(default=None, max_length=32)
    together: bool = False
    same_organization: bool = False
    marketing: dict | None = None


@router.post('/agreements/{agreement_id}/accept')
async def accept(agreement_id: str, payload: AcceptIn, request: Request,
                 identity=Depends(require_permission('settings:write'))):
    """Accept a partner's offer to relay your faxes; it is in force once the partner records it."""
    relay = relay_for(request.app)
    name = await _actor_name(request, identity)
    row = await _call(lambda: _with_partner(relay, relay.accept(
        agreement_id, reply_number=payload.reply_number, together=payload.together,
        same_organization=payload.same_organization, marketing=payload.marketing, actor_name=name)))
    detail = await _tell(relay, row)
    row = await _call(lambda: _with_partner(relay, relay.store.agreement(agreement_id)))
    return {**relay.view(row), 'detail': detail or (f"In force: your faxes can go through {row['organization']}."
                                                    if row['state'] == 'active' else None)}


@router.post('/agreements/{agreement_id}/withdraw')
async def withdraw(agreement_id: str, request: Request, identity=Depends(require_permission('settings:write'))):
    """End an agreement at once; faxes already accepted for relaying still go and get their receipts."""
    relay = relay_for(request.app)
    name = await _actor_name(request, identity)
    row = await _call(lambda: _with_partner(relay, relay.withdraw(
        agreement_id, by=getattr(identity.actor, 'principal_id', None), actor_name=name)))
    detail = await _tell(relay, row)
    return {**relay.view(row), 'detail': detail or 'Ended. Faxes already accepted for relaying still go.'}


@router.post('/agreements/{agreement_id}/price')
async def refresh_price(agreement_id: str, request: Request,
                        identity=Depends(require_permission('settings:write'))):
    """Sign a new price statement from your current rate cards and give it to the partner."""
    relay = relay_for(request.app)
    row = await _call(lambda: _with_partner(relay, relay.refresh_price(agreement_id)))
    detail = await _tell(relay, row)
    return {**relay.view(row), 'detail': detail or f"{row['organization']} has your new prices."}


class QuoteIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    countries: list[str] = Field(min_length=1, max_length=10)


@router.post('/partners/{peer_id}/quote', dependencies=[Depends(require_permission('settings:write'))])
async def ask_quote(peer_id: str, payload: QuoteIn, request: Request):
    """Ask a verified partner, signed, what sending your faxes through them would cost; nothing is relayed."""
    relay = relay_for(request.app)
    peer = await _call(lambda: relay._peer(peer_id))
    answered = await relay.ask_quote(peer_id, [code.upper() for code in payload.countries])
    if not answered:
        raise HTTPException(409, detail=f"{peer['organization']} did not give a price; their Faxbot may not relay "
                                        'yet or could not be reached.')
    return {'detail': f"{peer['organization']} gave its prices; Costs shows what relaying would save."}


@router.get('/costs', dependencies=[Depends(require_permission('settings:read'))])
async def relay_costs(request: Request, days: int = Query(default=30, ge=1, le=366)):
    """What each relay agreement carried and cost, on both sides."""
    relay = relay_for(request.app)
    return {'days': days, 'agreements': await _call(lambda: relay.costs(days=days))}


@router.get('/recommendations', dependencies=[Depends(require_permission('settings:read'))])
async def relay_recommendations(request: Request, days: int = Query(default=30, ge=1, le=366)):
    """Partners whose signed local price would have cost less than your own calls lately."""
    relay = relay_for(request.app)
    return {'days': days, 'recommendations': await _call(lambda: relay.recommendations(days=days))}


FAX_TEXT = {'sending': 'Sending to {partner}.', 'accepted': '{partner} accepted it and is sending it.',
            'delivered': '{partner} delivered it as a local call.',
            'failed_before_data': "{partner}'s call failed before any page was sent.",
            'uncertain': '{partner} cannot confirm whether it arrived; check with the recipient before sending again.',
            'refused': '{partner} did not accept it, so it went by your own route or was not sent.'}


@router.get('/faxes', dependencies=[Depends(require_permission('settings:read'))])
async def relayed_faxes(request: Request, days: int = Query(default=30, ge=1, le=366)):
    """Faxes relayed for partners and sent through partners, newest first."""
    from datetime import timedelta
    from ..routing.database import utcnow
    relay = relay_for(request.app)

    def read():
        since = utcnow() - timedelta(days=days)
        rows = relay.store.faxes_since(since, role='relay') + relay.store.faxes_since(since, role='sender')
        return sorted(rows, key=lambda row: row['created_at'], reverse=True)[:200]
    rows = await _call(read)
    return {'faxes': [{'fax_id': row['job_id'], 'role': row['role'], 'partner': row['organization'],
                       'fax_number': row['destination'], 'pages': row['delivered_pages'] or row['pages'],
                       'seconds': row['seconds'], 'state': row['state'], 'shared': bool(row['shared']),
                       'status': row['detail'] if row['state'] in ('failed_before_data', 'uncertain') and row['detail']
                       else FAX_TEXT[row['state']].format(partner=row['organization']),
                       'created_at': row['created_at']} for row in rows]}


async def _actor_name(request, identity):
    try:
        from ..access.http import runtime as access_runtime
        found = await run_lifecycle_step(lambda: access_runtime(request).reads.user(
            identity.actor, identity.actor.principal_id))
        return found.get('display_name') if isinstance(found, dict) else None
    except Exception:
        return None


# Partner protocol (signature-authenticated) ----------------------------------------------------------------------
class StatementIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statement: str = Field(max_length=65536)
    signature: str = Field(max_length=128)


def _partner_call(operation):
    async def run():
        try:
            status, body = await run_lifecycle_step(operation)
        except DirectUnavailable:
            raise HTTPException(404, detail='Not found.') from None
        return JSONResponse(body, status_code=status)
    return run()


@router.post('/statements')
async def receive_statement(payload: StatementIn, request: Request):
    """A partner's signed relay statement: an offer, acceptance, withdrawal, price, quote request or receipt."""
    relay = relay_for(request.app)
    return await _partner_call(lambda: relay.hear(payload.statement, payload.signature))


@router.get('/outcomes/{message_id}')
async def outcome(message_id: str, request: Request, x_faxbot_direct_key: str | None = Header(default=None),
                  x_faxbot_direct_time: str | None = Header(default=None),
                  x_faxbot_direct_signature: str | None = Header(default=None)):
    """A sender asks, signed, what happened to a fax it relayed through this installation."""
    relay = relay_for(request.app)
    return await _partner_call(lambda: relay.outcome_answer(message_id, signer=x_faxbot_direct_key,
                                                            request_time=x_faxbot_direct_time,
                                                            signature=x_faxbot_direct_signature))


__all__ = ['router', 'relay_for', 'relay_key']
