"""Administrative HTTP surface for delivery routes, costs and rate cards."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine, lifespan_tasks, repeat
from .billing import BillingReconciler
from .capture import CostRecorder
from .charges import SignalWireCharges
from .fallback import FallbackScheduler
from .seed import load_cards
from .costs import InvalidRateCard, RateCard, format_amount, parse_amount
from .database import DeliveryStoreError, utcnow
from .numbers import InvalidNumber, normalize_number
from .plan import REASON_TEXT, RoutePlanner, extra_routes, route_label
from .store import WINDOW_DAYS, RouteStore, RoutingConflict, RoutingInputError


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    from ..ami import ami_client
    from ..outbound_store import OutboundStore
    routes, delivery = RouteStore(engine), OutboundStore(runtime.manager.store)
    # A SIP call-record source can be passed as observed_seconds=; see docs/operations/delivery-routes.md.
    recorder = CostRecorder(routes)

    def seed():
        routes.seed_cards(load_cards())
        return False
    billing = BillingReconciler(routes, {'signalwire': SignalWireCharges(delivery)})
    fallback = FallbackScheduler(delivery, routes, ami=ami_client)
    return [('faxbot-route-seed', _once(seed)),
            ('faxbot-route-costs', repeat(recorder.step, interval=15.0, initial_delay=5.0,
                                          warning='Fax cost estimates are temporarily unavailable.')),
            ('faxbot-route-billing', repeat(billing.step, interval=60.0, initial_delay=30.0,
                                            warning='Provider charges are temporarily unavailable.')),
            ('faxbot-route-fallback', repeat(fallback.step, interval=3.0, initial_delay=3.0,
                                             warning='Fax route fallback is temporarily unavailable.'))]


async def _once(step):
    try:
        await run_lifecycle_step(step)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Starting rate cards could not be loaded.')


router = APIRouter(prefix='/routing', tags=['Delivery routes'], lifespan=lifespan_tasks(_background))


def _store(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    try:
        return RouteStore(engine)
    except DeliveryStoreError:
        raise HTTPException(503, detail='Delivery route storage is unavailable.') from None


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except (RoutingInputError, InvalidRateCard, InvalidNumber) as error:
        raise HTTPException(400, detail=str(error)) from None
    except RoutingConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Delivery route storage is unavailable.') from None


def _money(micros_by_currency):
    return [{'currency': currency, 'amount': format_amount(micros)}
            for currency, micros in sorted(micros_by_currency.items())]


def _number(value):
    try:
        return normalize_number(value)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _active(request):
    """The active revision and its outbound provider identity, without secrets."""
    snapshot = request.scope.get('faxbot.configuration')
    _, runtime = installation_engine(request.app)
    if snapshot is None or runtime is None:
        return None, None
    identity = snapshot.active.profile_id('outbound')
    if identity is None:
        return snapshot.active, None
    return snapshot.active, runtime.manager.store.read_profile(identity).configuration.provider_id


def _route_view(entry):
    attempts = entry['successes'] + entry['failures']
    return {'route': entry['route'], 'label': route_label(entry['route']), 'attempts': entry['attempts'],
            'successes': entry['successes'], 'failures': entry['failures'], 'uncertain': entry['uncertain'],
            'success_percent': None if attempts == 0 else (100 * entry['successes']) // attempts,
            'estimated_cost_30_days': _money(entry['cost_micros']),
            'reported_cost_30_days': _money(entry['reported_cost_micros']),
            'last_attempt_at': entry['last_attempt_at']}


def _destination_view(number, row, routes):
    total = {}
    for entry in routes.values():
        for currency, micros in entry['cost_micros'].items():
            total[currency] = total.get(currency, 0) + micros
    return {'number': number, 'display_name': row['display_name'] if row else None,
            'notes': row['notes'] if row else None, 'preferred_route': row['preferred_route'] if row else None,
            'accepts_references': bool(row['accepts_references']) if row else False,
            'version': row['version'] if row else 0,
            'routes': sorted((_route_view(entry) for entry in routes.values()), key=lambda item: item['route']),
            'estimated_cost_30_days': _money(total)}


@router.get('/destinations', dependencies=[Depends(require_permission('settings:read'))])
async def list_destinations(request: Request):
    store = _store(request)
    rows, evidence = await _call(lambda: store.destination_evidence())
    numbers = sorted(set(rows) | set(evidence))
    return {'window_days': WINDOW_DAYS,
            'destinations': [_destination_view(number, rows.get(number), evidence.get(number, {}))
                             for number in numbers]}


def _recommendation(store, number, revision, bound):
    if revision is None or bound is None:
        return []
    planner = RoutePlanner(store, direct_ready=lambda: True)
    plan = planner.plan(to_number=number, bound=bound, values=revision.values, pages=1, alternates=True)
    return [{'route': choice.route.key, 'label': route_label(choice.route.key), 'reason': choice.reason,
             'explanation': REASON_TEXT[choice.reason],
             'estimated_cost_one_page': None if choice.estimated_cost_micros is None else
             {'currency': choice.route.card.currency, 'amount': format_amount(choice.estimated_cost_micros)}}
            for choice in plan.choices]


@router.get('/destinations/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_destination(number: str, request: Request):
    number = _number(number)
    store = _store(request)
    revision, bound = await run_lifecycle_step(lambda: _active(request))

    def read():
        rows, evidence = store.destination_evidence(number=number)
        peer = store.verified_peer(number)
        return rows, evidence, peer, _recommendation(store, number, revision, bound)
    rows, evidence, peer, recommended = await _call(read)
    view = _destination_view(number, rows.get(number), evidence.get(number, {}))
    view['direct_partner'] = None if peer is None else {'organization': peer['organization'], 'verified': True}
    view['recommended_routes'] = recommended
    view['available_routes'] = ([] if bound is None else
                                [{'route': key, 'label': route_label(key)}
                                 for key in [bound, *extra_routes(revision.values, bound)]])
    return view


class DestinationPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    preferred_route: str | None = Field(default=None, max_length=64)
    accepts_references: bool | None = None
    version: int | None = Field(default=None, ge=0)


@router.patch('/destinations/{number}', dependencies=[Depends(require_permission('settings:write'))])
async def patch_destination(number: str, payload: DestinationPatch, request: Request):
    number = _number(number)
    store = _store(request)
    changes = payload.model_dump(exclude_unset=True)
    version = changes.pop('version', None)
    if 'accepts_references' in changes and changes['accepts_references'] is None:
        changes.pop('accepts_references')
    row = await _call(lambda: store.update_destination(number, expected_version=version, **changes))
    return _destination_view(number, row, {})


@router.get('/costs', dependencies=[Depends(require_permission('settings:read'))])
async def costs(request: Request, since: datetime | None = Query(default=None)):
    if since is not None and since.tzinfo is not None:
        since = (since - since.utcoffset()).replace(tzinfo=None)  # stored times are naive UTC
    start = since or (utcnow() - timedelta(days=WINDOW_DAYS))
    store = _store(request)
    totals = await _call(lambda: store.cost_totals(start))
    return {'since': start, 'providers': [{
        'provider_id': entry['provider_id'], 'label': route_label(entry['provider_id']),
        'attempts': entry['attempts'], 'successes': entry['successes'], 'failures': entry['failures'],
        'uncertain': entry['uncertain'], 'billed_minutes': round(entry['billed_seconds'] / 60, 1),
        'billed_pages': entry['billed_pages'], 'estimated_cost': _money(entry['cost_micros']),
        'reported_cost': _money(entry['reported_cost_micros']), 'settled_cost': _money(entry['settled_cost_micros']),
        'attempts_without_reported_cost': entry['unreported']} for entry in totals]}


def _card_view(card):
    return {'id': card.id, 'provider_id': card.provider_id, 'label': card.label, 'direction': card.direction,
            'currency': card.currency, 'per_minute': format_amount(card.per_minute_micros),
            'per_page': format_amount(card.per_page_micros), 'per_call': format_amount(card.per_call_micros),
            'billing_increment_seconds': card.billing_increment_seconds, 'minimum_seconds': card.minimum_seconds,
            'source_url': card.source_url, 'captured_on': card.captured_on.date().isoformat()}


class RateCardIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider_id: str = Field(max_length=64)
    direction: str = 'outbound'
    label: str = Field(max_length=100)
    currency: str = 'USD'
    per_minute: str | int = '0'
    per_page: str | int = '0'
    per_call: str | int = '0'
    billing_increment_seconds: int = 60
    minimum_seconds: int = 0
    source_url: str | None = Field(default=None, max_length=512)
    captured_on: datetime


class RateCardsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cards: list[RateCardIn] = Field(max_length=100)


@router.get('/rate-cards', dependencies=[Depends(require_permission('settings:read'))])
async def list_rate_cards(request: Request):
    store = _store(request)
    cards = await _call(store.current_cards)
    return {'cards': [_card_view(card) for card in cards]}


@router.put('/rate-cards', dependencies=[Depends(require_permission('settings:write'))])
async def put_rate_cards(payload: RateCardsIn, request: Request):
    try:
        cards = [RateCard(None, item.provider_id.strip().lower(), item.direction, item.label, item.currency.upper(),
                          parse_amount(item.per_minute), parse_amount(item.per_page), parse_amount(item.per_call),
                          item.billing_increment_seconds, item.minimum_seconds, item.source_url or None,
                          item.captured_on.replace(tzinfo=None))
                 for item in payload.cards]
    except InvalidRateCard as error:
        raise HTTPException(400, detail=str(error)) from None
    store = _store(request)
    saved = await _call(lambda: store.replace_cards(cards))
    return {'cards': [_card_view(card) for card in saved]}
