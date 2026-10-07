"""Administrative HTTP surface for delivery routes, costs and rate cards."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import private_operation, require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine, lifespan_tasks, repeat
from .billing import BillingReconciler
from .capture import CostRecorder
from .carriers import CarrierChargeStore, CarrierReconciler, carrier_label
from .charges import SignalWireCharges
from .spending import CARRIER_PRESETS, Spending
from .telnyx import TelnyxDetailRecords
from .fallback import FallbackPolicy, FallbackScheduler
from .seed import load_cards
from .costs import InvalidRateCard, RateCard, estimate_cost, format_amount, parse_amount, plan_fee_text, rate_text
from .database import DeliveryStoreError, utcnow
from .numbers import InvalidNumber, normalize_number
from .plan import RoutePlanner, explain, extra_routes, route_label
from .store import WINDOW_DAYS, RouteStore, RoutingConflict, RoutingInputError


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    from ..ami import ami_client
    from ..outbound_store import OutboundStore
    routes, delivery = RouteStore(engine), OutboundStore(runtime.manager.store)
    from ..sip_calls import SipCallRecords  # connected seconds measured on the SIP trunk
    recorder = CostRecorder(routes, observed_seconds=SipCallRecords(engine).observed_seconds)

    # Starting rate cards go in before the API serves its first request, so a
    # rate-card save can never race the seed.
    try:
        shipped = load_cards()
        routes.seed_cards(shipped)
        _add_shipped_cards(app, routes, shipped, runtime)
    except Exception:
        import logging
        logging.getLogger(__name__).warning('Starting rate cards could not be loaded.')
    billing = BillingReconciler(routes, {'signalwire': SignalWireCharges(delivery)})
    carriers = CarrierReconciler(CarrierChargeStore(engine), routes, carrier_source(_telnyx_key),
                                 numbers=lambda: _trunk_numbers(_managed_values()))
    fallback = FallbackScheduler(delivery, routes, ami=ami_client)
    # Faxes to the installation's own numbers whose answer was lost: delivered, or sent normally.
    from .local import LocalReconciler, for_application
    local_delivery, local_values, _ = for_application(app, runtime)
    local = LocalReconciler(local_delivery, delivery, routes, values=local_values)
    return [('faxbot-local-delivery', repeat(local.step, interval=30.0, initial_delay=20.0,
                                             warning='Faxes to your own numbers could not be checked.')),
            ('faxbot-route-policy', _install_policy(OutboundStore, FallbackPolicy(fallback))),
            ('faxbot-route-costs', repeat(recorder.step, interval=15.0, initial_delay=5.0,
                                          warning='Fax cost estimates are temporarily unavailable.')),
            ('faxbot-route-billing', repeat(billing.step, interval=60.0, initial_delay=30.0,
                                            warning='Provider charges are temporarily unavailable.')),
            ('faxbot-carrier-charges', repeat(carriers.step, interval=60.0, initial_delay=45.0,
                                              warning='Carrier call charges are temporarily unavailable.')),
            ('faxbot-route-fallback', repeat(fallback.step, interval=3.0, initial_delay=3.0,
                                             warning='Fax route fallback is temporarily unavailable.'))]


def _add_shipped_cards(app, routes, shipped, runtime):
    """Give each provider in use that never had a rate card its shipped, dated card; audited."""
    from .seed import cards_in_use
    values = runtime.manager.store.read().active.values
    added = routes.add_missing_cards(cards_in_use(values, shipped))
    if not added:
        return
    details = {'source': 'shipped advertised prices',
               'cards': sorted(f'{card.provider_id} {card.direction}' for card in added)}
    access = getattr(app.state, 'access_runtime', None)
    if access is not None:
        import json
        from uuid import uuid4
        with access.store.transaction() as connection:
            version = access.store.require_lock_on(connection)
            connection.execute(access.store.tables['access_audit'].insert().values(
                id=uuid4().hex, actor_principal_id=None, actor_key_binding_id=None, actor_session_id=None,
                operation='routing.rate_cards_added', target_kind='installation', target_id='rate_cards',
                policy_version_before=version, policy_version_after=version, outcome='allowed',
                details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
                created_at=utcnow()))
    from ..audit import audit_event
    audit_event('rate_cards_added', **details)


def carrier_source(api_key):
    """The trunk carrier's charge records; ``api_key()`` returns the current key."""
    return TelnyxDetailRecords(api_key)


def _managed_values():
    from ..config import managed_configuration_values
    return managed_configuration_values()


def _telnyx_key():
    values = _managed_values()
    return getattr(values, 'telnyx_api_key', '') if values is not None else ''


def _trunk_numbers(values):
    """The SIP trunk's own fax numbers: its DIDs and caller ID."""
    if values is None:
        return ()
    return tuple(number for number in (*getattr(values, 'sip_trunk_did_list', ()),
                                       getattr(values, 'sip_trunk_caller_id', '')) if number)


async def _install_policy(store_class, policy):
    """Hold the route fallback policy on the delivery store for this application's lifetime."""
    import asyncio
    store_class.fallback_policy = policy
    try:
        await asyncio.Event().wait()
    finally:
        if store_class.fallback_policy is policy:
            store_class.fallback_policy = None


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


def _number(value, request):
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        return normalize_number(value, country=country)
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
            # Calls at once to this number: None is the default (one at a time), 0 means no limit.
            'max_calls': row.get('max_calls') if row else None,
            'version': row['version'] if row else 0,
            'routes': sorted((_route_view(entry) for entry in routes.values()), key=lambda item: item['route']),
            'estimated_cost_30_days': _money(total)}


@router.get('/destinations', dependencies=[Depends(require_permission('settings:read'))])
async def list_destinations(request: Request):
    from .recommendations import delivered_costs_for
    store = _store(request)
    rows, evidence, delivered = await _call(lambda: (*store.destination_evidence(), delivered_costs_for(store)))
    numbers = sorted(set(rows) | set(evidence))
    # Each route's cost per delivered fax, the cheapest first (the Recipients list column).
    return {'window_days': WINDOW_DAYS,
            'destinations': [{**_destination_view(number, rows.get(number), evidence.get(number, {})),
                              'delivered_costs': delivered.get(number, [])} for number in numbers]}


def _recommendation(store, number, revision, bound, pages=1):
    """Routes in the order the next fax of ``pages`` pages would try them, each with its estimate.

    ``rate`` words each card by its own unit ("$0.005 a minute, at least 1 minute"),
    so a per-minute carrier is never described as charging by the page.
    """
    if revision is None or bound is None:
        return []
    planner = RoutePlanner(store, direct_ready=lambda: True, local_ready=lambda: True)
    plan = planner.plan(to_number=number, bound=bound, values=revision.values, pages=pages, alternates=True)
    def plan_fee(card):
        if card is None or not card.flat_plan:
            return None
        return {'currency': card.currency, 'amount': format_amount(card.monthly_fee_micros)}

    def money(card, micros):
        return None if card is None or micros is None else {'currency': card.currency, 'amount': format_amount(micros)}
    return [{'route': choice.route.key, 'label': route_label(choice.route.key), 'reason': choice.reason,
             'explanation': explain(choice, plan.destination),
             'estimated_cost_one_page': money(choice.route.card, None if choice.route.card is None
                                               else estimate_cost(choice.route.card, 1)),
             # This fax: setup plus typical seconds a page, rounded the way the card bills.
             'pages': pages, 'estimated_cost': money(choice.route.card, choice.estimated_cost_micros),
             'rate': rate_text(choice.route.card),
             'included_in_plan': plan_fee(choice.route.card) is not None,
             'monthly_fee': plan_fee(choice.route.card)}
            for choice in plan.choices]


@router.get('/destinations/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_destination(number: str, request: Request, pages: int = Query(default=1, ge=1, le=1000)):
    number = _number(number, request)
    store = _store(request)
    revision, bound = await run_lifecycle_step(lambda: _active(request))

    def read():
        from .recommendations import delivered_costs_for
        rows, evidence = store.destination_evidence(number=number)
        peer = store.verified_peer(number)
        delivered = delivered_costs_for(store, number).get(number, [])
        return rows, evidence, peer, _recommendation(store, number, revision, bound, pages), delivered
    rows, evidence, peer, recommended, delivered = await _call(read)
    view = _destination_view(number, rows.get(number), evidence.get(number, {}))
    # Per route over the window: delivered share, cost per delivered fax, average pages and connected time.
    view['delivered_costs'] = delivered
    view['direct_partner'] = None if peer is None else {'organization': peer['organization'], 'verified': True}
    view['recommended_routes'] = recommended
    view['available_routes'] = ([] if bound is None else
                                [{'route': key, 'label': route_label(key)}
                                 for key in [bound, *extra_routes(revision.values, bound)]])
    return view


@router.get('/recommendations/sending', dependencies=[Depends(require_permission('settings:read'))])
async def sending_recommendations(request: Request):
    """Numbers where another route cost less per delivered fax than the one Faxbot uses first now."""
    from .delivered import MIN_DELIVERED
    from .recommendations import NO_SENDING, sending_recommendations as recommend
    store = _store(request)
    revision, bound = await run_lifecycle_step(lambda: _active(request))
    items = await _call(lambda: recommend(store, revision, bound))
    return {'window_days': WINDOW_DAYS, 'min_delivered': MIN_DELIVERED, 'items': items, 'empty_sentence': NO_SENDING}


@router.get('/recommendations/plans', dependencies=[Depends(require_permission('settings:read'))])
async def plan_recommendations(request: Request):
    """Whether each monthly plan is worth its fee at your traffic (estimates; Faxbot never cancels anything)."""
    from .plan_check import plan_report
    from .recommendations import sending_recommendations as recommend
    store = _store(request)
    revision, bound = await run_lifecycle_step(lambda: _active(request))
    values = request.scope['faxbot.configuration'].active.values

    def read():
        # Plans the Sending section already points numbers to: Plans then says what the fee question is.
        suggested = {item['suggested']['route'] for item in recommend(store, revision, bound) if item['kind'] == 'plan'}
        return plan_report(store, values, bound=bound, suggested=suggested)
    return await _call(read)


class DestinationPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    display_name: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    preferred_route: str | None = Field(default=None, max_length=64)
    accepts_references: bool | None = None
    max_calls: int | None = Field(default=None, ge=0, le=20)
    version: int | None = Field(default=None, ge=0)


@router.patch('/destinations/{number}', dependencies=[Depends(require_permission('settings:write'))])
async def patch_destination(number: str, payload: DestinationPatch, request: Request):
    number = _number(number, request)
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
    spending = _spending(request)
    values = request.scope['faxbot.configuration'].active.values
    outbound, received = await _call(lambda: (spending.outbound(start), spending.received(start)))

    def carrier(entry):
        names = sorted(entry['carriers'])
        return carrier_label(names[0]) if len(names) == 1 else None

    def plan(entry):
        card = entry['plan_card']
        if card is None:
            return None
        return {'label': route_label(card.provider_id),
                'monthly_fee': {'currency': card.currency, 'amount': format_amount(card.monthly_fee_micros)},
                'monthly_fee_text': plan_fee_text(card.monthly_fee_micros, card.currency),
                # The fee is counted once per 30 days, pro-rated by day for other periods.
                'period_fee': {'currency': card.currency, 'amount': format_amount(entry['plan_fee_micros'])},
                'period_days': entry['plan_days']}

    grand = {}
    for entry in [*outbound, *received]:
        for currency, micros in entry['total_micros'].items():
            grand[currency] = grand.get(currency, 0) + micros
    # Faxes and calls with no charge and no estimate: left out of every total, never counted as $0.
    not_priced = sum(entry['unpriced'] for entry in [*outbound, *received])
    # Calls the carrier priced only in part by the give-up time: their priced part is in the totals.
    never_priced = sum(entry['never_priced'] for entry in [*outbound, *received])
    return {'since': start, 'carrier_charges': _carrier_status(values), 'total_cost': _money(grand),
            'not_priced': not_priced, 'never_priced': never_priced, 'providers': [{
        'provider_id': entry['provider_id'], 'label': route_label(entry['provider_id']), 'carrier': carrier(entry),
        'attempts': entry['attempts'], 'successes': entry['successes'], 'failures': entry['failures'],
        'uncertain': entry['uncertain'], 'billed_minutes': round(entry['billed_seconds'] / 60, 1),
        'billed_pages': entry['billed_pages'], 'estimated_cost': _money(entry['cost_micros']),
        'reported_cost': _money(entry['reported_cost_micros']), 'settled_cost': _money(entry['settled_cost_micros']),
        'attempts_without_reported_cost': entry['unreported'], 'attempts_with_reported_cost': entry['reported'],
        # Of attempts_without_reported_cost, those with no estimate either: not in any total.
        'attempts_not_priced': entry['unpriced'],
        # Faxes whose call the carrier never priced in full: the priced part is in reported_cost.
        'attempts_never_priced': entry['never_priced'],
        'estimated_cost_not_reported': _money(entry['unreported_estimate_micros']),
        'awaiting_carrier_bill': entry['awaiting'], 'unmatched_charges': entry['unmatched'],
        **_unrecorded_view(entry), 'plan': plan(entry), 'priced': entry['has_card'] or bool(entry['reported']),
        'total_cost': _money(entry['total_micros'])} for entry in outbound],
        'received': [{
            'provider_id': entry['provider_id'], 'label': route_label(entry['provider_id']),
            'carrier': carrier_label(entry['carrier']) if entry['carrier'] else None,
            'calls': entry['calls'], 'faxes': entry['faxes'], 'billed_minutes': round(entry['billed_seconds'] / 60, 1),
            'estimated_cost': _money(entry['cost_micros']), 'reported_cost': _money(entry['reported_cost_micros']),
            'calls_with_reported_cost': entry['reported'], 'calls_without_reported_cost': entry['unreported'],
            'calls_not_priced': entry['unpriced'], 'calls_never_priced': entry['never_priced'],
            'estimated_cost_not_reported': _money(entry['unreported_estimate_micros']),
            'awaiting_carrier_bill': entry['awaiting'], 'unmatched_charges': entry['unmatched'],
            **_unrecorded_view(entry), 'total_cost': _money(entry['total_micros'])}
            for entry in received]}


def _unrecorded_view(entry):
    """Carrier records Faxbot has no call record of: already in the charged total, counted here too."""
    return {'unrecorded_calls': entry.get('unrecorded', 0), 'unrecorded_cost': _money(entry.get('unrecorded_micros', {})),
            'unrecorded_matched_to_faxes': entry.get('unrecorded_attached', 0),
            # The part of unrecorded_cost not matched to a received fax: calls Faxbot has no record of at all.
            'unrecorded_unmatched_cost': _money(entry.get('unrecorded_unattached_micros', {}))}


def _spending(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    try:
        return Spending(RouteStore(engine), CarrierChargeStore(engine))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Delivery route storage is unavailable.') from None


def _carrier_status(values):
    """Whether Faxbot can read the configured SIP trunk carrier's call charges."""
    preset = values.sip_trunk_preset
    if preset not in CARRIER_PRESETS:
        return {'carrier': carrier_label(preset) if preset else None, 'readable': False, 'supported': False}
    return {'carrier': carrier_label(preset), 'supported': True, 'readable': bool(values.telnyx_api_key)}


NO_TELNYX_KEY = ('Faxbot needs a Telnyx API key to read call charges. Add it in the console under Providers → '
                 'Telnyx, or run faxbot system settings set --secret telnyx_api_key.')


def _reconcile_summary(result):
    # The same split as the Spending card: a record matched to a received fax is not "no record of".
    attached = result.get('unrecorded_matched_to_faxes', 0)
    unattached = result.get('unrecorded_calls', 0) - attached
    extra = (f" Telnyx billed {unattached} {'call' if unattached == 1 else 'calls'} Faxbot has no record of."
             if unattached else '')
    if attached:
        extra += (f" {attached} {'call' if attached == 1 else 'calls'} came in that Faxbot did not record at the time; "
                  f"{'its fax is' if attached == 1 else 'their faxes are'} in Received.")
    if result['carrier_unavailable'] and not result['checked']:
        return 'Telnyx could not be reached; Faxbot will ask again later.'
    if not result['checked']:
        return 'No calls are waiting for a charge.' + extra
    recorded = result['charges_recorded']
    parts = [f"{recorded} new {'charge' if recorded == 1 else 'charges'} recorded"]
    if result['waiting']:
        parts.append(f"{result['waiting']} still waiting for the Telnyx bill")
    if result['ambiguous']:
        parts.append(f"{result['ambiguous']} could not be matched to one Telnyx record")
    calls = f"{result['checked']} {'call' if result['checked'] == 1 else 'calls'}"
    return f'Checked {calls}: ' + ', '.join(parts) + '.' + extra


@router.post('/reconcile', dependencies=[Depends(require_permission('settings:write'))])
async def reconcile(request: Request):
    """Ask the SIP trunk carrier now what each open call cost; never changes a delivery."""
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    key = request.scope['faxbot.configuration'].active.values.telnyx_api_key
    if not key:
        raise HTTPException(409, detail=NO_TELNYX_KEY)

    values = request.scope['faxbot.configuration'].active.values

    def run():
        reconciler = CarrierReconciler(CarrierChargeStore(engine), RouteStore(engine), carrier_source(lambda: key),
                                       numbers=lambda: _trunk_numbers(values))
        return reconciler.run_now().as_dict()
    result = await _call(run)
    return {**result, 'summary': _reconcile_summary(result)}


def _cost_view(cost):
    view = {**cost, 'reported_cost': _money(cost.get('reported_cost', {}))}
    if 'estimated_cost' in cost:
        view['estimated_cost'] = _money(cost['estimated_cost'])
    if 'route_reason' in cost:
        # Sent details: why the latest attempt went by its route, in one sentence.
        from .plan import decided_text
        view['route_explanation'] = (decided_text(cost.get('route'), cost['route_reason'])
                                     if cost['route_reason'] else None)
    return view


@router.get('/faxes/{job_id}/cost')
async def fax_cost(job_id: str, request: Request, identity=Depends(require_identity)):
    """What one sent fax cost, for anyone who may read that fax."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.queries.job(identity.actor, job_id)))
    spending = _spending(request)
    from .provenance import dialed_view
    return _cost_view(await _call(lambda: {**spending.job(job_id),
                                           'dialed': dialed_view(spending.routes.engine, job_id)}))


@router.get('/inbound-costs')
async def inbound_costs(request: Request, ids: str = Query(default='', max_length=4200),
                        identity=Depends(require_identity)):
    """Costs for up to 100 received faxes at once; faxes this person cannot read are left out."""
    from ..access.fax_resources import FaxAccessError
    wanted = list(dict.fromkeys(item.strip() for item in ids.split(',') if item.strip()))[:100]
    runtime = request.app.state.access_runtime
    spending = _spending(request)

    def read():
        costs = {}
        for inbound_id in wanted:
            try:
                private_operation(lambda: runtime.inbound_queries.item(identity.actor, inbound_id))()
            except FaxAccessError:
                continue  # not visible to this person: left out, never explained
            costs[inbound_id] = _cost_view(spending.inbound(inbound_id))
        return costs
    return {'costs': await _call(read)}


@router.get('/fax-costs')
async def fax_costs(request: Request, ids: str = Query(default='', max_length=4200),
                    identity=Depends(require_identity)):
    """Costs for up to 100 sent faxes at once, for the Sent list; faxes this person cannot read are left out."""
    from ..access.fax_resources import FaxAccessError
    from .provenance import dialed_view
    wanted = list(dict.fromkeys(item.strip() for item in ids.split(',') if item.strip()))[:100]
    runtime = request.app.state.access_runtime
    spending = _spending(request)

    def read():
        costs = {}
        for job_id in wanted:
            try:
                private_operation(lambda: runtime.queries.job(identity.actor, job_id))()
            except FaxAccessError:
                continue  # not visible to this person: left out, never explained
            costs[job_id] = _cost_view({**spending.job(job_id), 'dialed': dialed_view(spending.routes.engine, job_id)})
        return costs
    return {'costs': await _call(read)}


def _saving_view(part):
    """One kind of saving: always an estimate, money as amounts per currency."""
    return {**{key: value for key, value in part.items() if key != 'saved'}, 'estimate': True,
            'saved': _money(part['saved'])}


@router.get('/savings', dependencies=[Depends(require_permission('settings:read'))])
async def savings(request: Request, days: int = Query(default=WINDOW_DAYS, ge=1, le=366)):
    """What sending together, direct delivery and case packets saved in the last ``days`` (estimates)."""
    from .savings import SENTENCE, savings as count_savings
    store = _store(request)
    result = await _call(lambda: count_savings(store, store.engine, days=days))
    return {'days': result['days'], 'since': result['since'], 'estimate': True, 'sentence': SENTENCE,
            # Signed: a negative amount is money that cost more than it saved, said so in total_sentence.
            'total_saved': _money(result['total']), 'total_sentence': result['total_sentence'],
            'sending_together': _saving_view(result['sending_together']),
            'direct_delivery': _saving_view(result['direct_delivery']),
            'case_packets': _saving_view(result['case_packets']),
            'sslfax': _saving_view(result['sslfax']),
            # Faxes to the installation's own numbers, delivered inside Faxbot with no call.
            'own_numbers': _saving_view(result['own_numbers']),
            # Faxes that called their recipient's approved toll-free number; the recipient pays those calls.
            'toll_free': _saving_view(result['toll_free'])}


@router.get('/recommendations/receiving', dependencies=[Depends(require_permission('settings:read'))])
async def receiving_recommendations(request: Request, days: int = Query(default=30, ge=7, le=183)):
    """Receiving advice from call history: shared channels, quiet numbers and connections (estimates, never applied).

    The shared-channel advice is chosen on the ``days`` before the last ``days``
    and checked on the last ``days``.
    """
    from .receiving import receiving_report
    store = _store(request)
    values = request.scope['faxbot.configuration'].active.values
    return await _call(lambda: receiving_report(store.engine, store, values, days=days))


@router.get('/inbound/{inbound_id}/cost')
async def inbound_cost(inbound_id: str, request: Request, identity=Depends(require_identity)):
    """What the call that brought in one received fax cost, for anyone who may read that fax."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.inbound_queries.item(identity.actor, inbound_id)))
    spending = _spending(request)
    return _cost_view(await _call(lambda: spending.inbound(inbound_id)))


def _provider_name(provider_id):
    """A rate card's provider in words: "Telnyx" for the trunk or a carrier's card, "Phaxio" for a provider."""
    if provider_id.startswith('sip-'):
        return carrier_label(provider_id[len('sip-'):])
    return route_label(provider_id)


def _card_view(card):
    return {'id': card.id, 'provider_id': card.provider_id, 'provider_name': _provider_name(card.provider_id),
            'label': card.label, 'direction': card.direction,
            'currency': card.currency, 'per_minute': format_amount(card.per_minute_micros),
            'per_page': format_amount(card.per_page_micros), 'per_call': format_amount(card.per_call_micros),
            'billing_increment_seconds': card.billing_increment_seconds, 'minimum_seconds': card.minimum_seconds,
            'source_url': card.source_url, 'captured_on': card.captured_on.date().isoformat(),
            'monthly_fee': None if card.monthly_fee_micros is None else format_amount(card.monthly_fee_micros),
            'included_in_plan': card.flat_plan}


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
    monthly_fee: str | int | None = None
    # Shown by GET; accepted and ignored so a listed card can be saved back unchanged.
    included_in_plan: bool | None = None
    provider_name: str | None = None


class RateCardsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cards: list[RateCardIn] = Field(max_length=100)


@router.get('/rate-cards', dependencies=[Depends(require_permission('settings:read'))])
async def list_rate_cards(request: Request):
    store = _store(request)
    cards = await _call(store.current_cards)
    # What each sending route publishes about calling toll-free numbers (an approved alternate), with its source.
    from .dialing import terms_view
    values = request.scope['faxbot.configuration'].active.values
    return {'cards': [_card_view(card) for card in cards], 'toll_free': terms_view(values)}


@router.get('/published-plans', dependencies=[Depends(require_permission('settings:read'))])
async def published_plans(provider_id: str, request: Request):
    """A provider's published plans for the installation country, where its API has no published price."""
    from .reference import suggestion
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    found = await run_lifecycle_step(lambda: suggestion(provider_id.strip().lower()[:64], country))
    if found is None:
        raise HTTPException(404, detail='Faxbot knows no published plans for this provider.')
    return found


@router.get('/published-plans/in-use', dependencies=[Depends(require_permission('settings:read'))])
async def published_plans_in_use(request: Request):
    """Published plans for each sending provider in use with no sending rate card yet, such as eFax."""
    from .reference import suggestion
    values = request.scope['faxbot.configuration'].active.values
    cards = await _call(_store(request).current_cards)
    priced = {card.provider_id for card in cards if card.direction == 'outbound'}
    sending = [provider for provider in dict.fromkeys([values.effective_outbound, *values.outbound_route_providers])
               if provider and provider not in priced]
    country = values.fax_default_country
    found = await run_lifecycle_step(lambda: [suggestion(provider, country) for provider in sending])
    return {'items': [item for item in found if item is not None]}


@router.put('/rate-cards', dependencies=[Depends(require_permission('settings:write'))])
async def put_rate_cards(payload: RateCardsIn, request: Request):
    try:
        cards = [RateCard(None, item.provider_id.strip().lower(), item.direction, item.label, item.currency.upper(),
                          parse_amount(item.per_minute), parse_amount(item.per_page), parse_amount(item.per_call),
                          item.billing_increment_seconds, item.minimum_seconds, item.source_url or None,
                          item.captured_on.replace(tzinfo=None),
                          None if item.monthly_fee in (None, '') else parse_amount(item.monthly_fee, whole_digits=4))
                 for item in payload.cards]
    except InvalidRateCard as error:
        raise HTTPException(400, detail=str(error)) from None
    store = _store(request)
    saved = await _call(lambda: store.replace_cards(cards))
    return {'cards': [_card_view(card) for card in saved]}
