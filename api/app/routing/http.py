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
from .charges import PhaxioCharges, SignalWireCharges, SinchCharges
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
    billing = BillingReconciler(routes, {'signalwire': SignalWireCharges(delivery), 'sinch': SinchCharges(delivery),
                                         'phaxio': PhaxioCharges(delivery)})
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
                                             warning='Fax route fallback is temporarily unavailable.')),
            # Time windows your rules hold faxes for open by themselves; this marks them released for the history.
            ('faxbot-route-holds', repeat(_holds_step(delivery), interval=30.0, initial_delay=10.0,
                                          warning='Held faxes could not be checked.'))]


def _holds_step(delivery):
    from .holds import HoldStore
    store = HoldStore(delivery)
    return lambda: bool(store.release_due())


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
    # A fax accepted now dials the recipient's approved toll-free number where a route can, priced for that class.
    from .alternates import current
    approval = current(number, engine=store.engine)
    dial = {'alternate': approval.alternate, 'refused': False} if approval is not None else None
    plan = planner.plan(to_number=number, bound=bound, values=revision.values, pages=pages, alternates=True, dial=dial)
    def plan_fee(card):
        if card is None or not card.flat_plan:
            return None
        return {'currency': card.currency, 'amount': format_amount(card.monthly_fee_micros)}

    def money(card, micros):
        return None if card is None or micros is None else {'currency': card.currency, 'amount': format_amount(micros)}
    return [{'route': choice.route.key, 'label': route_label(choice.route.key), 'reason': choice.reason,
             'explanation': explain(choice, plan.destination, plan.number_for(choice.route.key)),
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
    from .recommendations import NO_SENDING, country_rules, public_items, sending_recommendations as recommend
    store = _store(request)
    revision, bound = await run_lifecycle_step(lambda: _active(request))
    items = await _call(lambda: recommend(store, revision, bound))
    return {'window_days': WINDOW_DAYS, 'min_delivered': MIN_DELIVERED, 'items': public_items(items),
            'country_rules': country_rules(items), 'empty_sentence': NO_SENDING}


@router.get('/recommendations/trunks', dependencies=[Depends(require_permission('settings:read'))])
async def trunk_recommendations(request: Request):
    """Whether one trunk's traffic fits on another and what moving it would save (advice only; WP-T, B6)."""
    from .trunk_advice import WINDOW_DAYS as TRUNK_DAYS, advice
    store = _store(request)
    values = request.scope['faxbot.configuration'].active.values
    found = await _call(lambda: advice(values, store.engine, routes=store))
    return {'window_days': TRUNK_DAYS, **found}


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
    outbound, received, received_faxes = await _call(lambda: (spending.outbound(start), spending.received(start),
                                                              spending.received_faxes(start)))

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
    for entry in [*outbound, *received, *received_faxes]:
        for currency, micros in entry['total_micros'].items():
            grand[currency] = grand.get(currency, 0) + micros
    # Faxes and calls with no charge and no estimate: left out of every total, never counted as $0.
    not_priced = sum(entry['unpriced'] for entry in [*outbound, *received, *received_faxes])
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
            for entry in received],
        # Faxes each cloud provider received (Sinch, Phaxio, HumbleFax…), apart from the trunk's received calls.
        'received_faxes': [{
            'provider_id': entry['provider_id'], 'label': route_label(entry['provider_id']), 'faxes': entry['faxes'],
            'reported_cost': _money(entry['reported_cost_micros']), 'faxes_with_reported_cost': entry['reported'],
            'estimated_cost_not_reported': _money(entry['unreported_estimate_micros']),
            'faxes_without_reported_cost': entry['unreported'], 'faxes_not_priced': entry['unpriced'],
            'faxes_included_in_plan': entry['included'], 'total_cost': _money(entry['total_micros']),
            'summary': received_faxes_sentence(entry)} for entry in received_faxes]}


def received_faxes_sentence(entry):
    """One line per cloud provider, such as: Received faxes: Sinch $0.42 for 6 faxes, 1 more not priced yet."""
    from .costs import money_list_text
    label = route_label(entry['provider_id'])
    word = lambda count: 'fax' if count == 1 else 'faxes'  # noqa: E731
    known = entry['faxes'] - entry['unpriced'] - entry['included']
    if known:
        text = f"{money_list_text(entry['total_micros'])} for {known} {word(known)}"
        more = ([f"{entry['unpriced']} more not priced yet"] if entry['unpriced'] else []) + (
            [f"{entry['included']} more included in the plan"] if entry['included'] else [])
    elif entry['included']:
        text = f"{entry['included']} {word(entry['included'])}, included in the plan"
        more = [f"{entry['unpriced']} more not priced yet"] if entry['unpriced'] else []
    else:
        text, more = f"{entry['unpriced']} {word(entry['unpriced'])}, not priced yet", []
    return f"Received faxes: {label} {', '.join([text, *more])}."


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
    from .carrier_records import trunk_records  # what to set, in one sentence, for this carrier
    found = trunk_records(values)
    return {'carrier': carrier_label(preset), 'supported': True, 'readable': found['readable'],
            'sentence': found['sentence']}


NO_TELNYX_KEY = ('Faxbot needs a Telnyx API key to read call charges. Add it in the console under Providers → '
                 'Telnyx, or run faxbot system settings set --secret telnyx_api_key.')


def _reconcile_summary(result, label='Telnyx'):
    # The same split as the Spending card: a record matched to a received fax is not "no record of".
    attached = result.get('unrecorded_matched_to_faxes', 0)
    unattached = result.get('unrecorded_calls', 0) - attached
    extra = (f" {label} billed {unattached} {'call' if unattached == 1 else 'calls'} Faxbot has no record of."
             if unattached else '')
    if attached:
        extra += (f" {attached} {'call' if attached == 1 else 'calls'} came in that Faxbot did not record at the time; "
                  f"{'its fax is' if attached == 1 else 'their faxes are'} in Received.")
    if result['carrier_unavailable'] and not result['checked']:
        return f'{label} could not be reached; Faxbot will ask again later.'
    if not result['checked']:
        return 'No calls are waiting for a charge.' + extra
    recorded = result['charges_recorded']
    parts = [f"{recorded} new {'charge' if recorded == 1 else 'charges'} recorded"]
    if result['waiting']:
        parts.append(f"{result['waiting']} still waiting for the {label} bill")
    if result['ambiguous']:
        parts.append(f"{result['ambiguous']} could not be matched to one {label} record")
    calls = f"{result['checked']} {'call' if result['checked'] == 1 else 'calls'}"
    return f'Checked {calls}: ' + ', '.join(parts) + '.' + extra


@router.post('/reconcile', dependencies=[Depends(require_permission('settings:write'))])
async def reconcile(request: Request):
    """Ask the SIP trunk carrier now what each open call cost; never changes a delivery."""
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    values = request.scope['faxbot.configuration'].active.values
    from .carrier_records import READERS, reader_for, trunk_records
    preset = values.sip_trunk_preset
    if preset in READERS:
        # Another carrier that publishes its call records (carrier_records.PUBLISHED).
        source = reader_for(preset, values)
        if not source.ready():
            raise HTTPException(409, detail=trunk_records(values)['sentence'])
    else:
        key = values.telnyx_api_key
        if not key:
            raise HTTPException(409, detail=NO_TELNYX_KEY)
        preset, source = 'telnyx', carrier_source(lambda: key)

    def run():
        reconciler = CarrierReconciler(CarrierChargeStore(engine), RouteStore(engine), source, preset=preset,
                                       numbers=lambda: _trunk_numbers(values))
        return reconciler.run_now().as_dict()
    result = await _call(run)
    if result['carrier_unavailable'] and getattr(source, 'preparing', False):
        # Flowroute prepares its call records as an export; the next check reads the same one.
        return {**result, 'summary': f'{source.label} is still preparing its call records; Faxbot reads them '
                                     'within a few minutes, or select Check again.'}
    return {**result, 'summary': _reconcile_summary(result, source.label)}


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
    from .nppes import fax_warning
    from .provenance import dialed_view
    # recipient_warning: what stored NPPES records said about the number when the fax was accepted (never a block).
    return _cost_view(await _call(lambda: {**spending.job(job_id),
                                           'dialed': dialed_view(spending.routes.engine, job_id),
                                           'recipient_warning': fax_warning(spending.routes.engine, job_id)}))


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
    from .nppes import fax_warning
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
            costs[job_id] = _cost_view({**spending.job(job_id), 'dialed': dialed_view(spending.routes.engine, job_id),
                                        'recipient_warning': fax_warning(spending.routes.engine, job_id)})
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
    home = request.scope['faxbot.configuration'].active.values.fax_default_country
    result = await _call(lambda: count_savings(store, store.engine, days=days, home=home))

    def direct_bytes():
        # Bytes partners did not need sent again (a reference or only the changes): counted, never money.
        from ..direct.reuse import savings_view
        return savings_view(store.engine, since=result['since'], days=result['days'])
    return {'days': result['days'], 'since': result['since'], 'estimate': True, 'sentence': SENTENCE,
            'direct_bytes': await _call(direct_bytes),
            # Signed: a negative amount is money that cost more than it saved, said so in total_sentence.
            'total_saved': _money(result['total']), 'total_sentence': result['total_sentence'],
            'sending_together': _saving_view(result['sending_together']),
            # Separator pages shared calls left out (index page or page marks), counted apart from calls saved.
            'separator_pages': _saving_view(result['separator_pages']),
            'direct_delivery': _saving_view(result['direct_delivery']),
            # Telephone calls avoided by fax images partners accepted directly (never counted as faxed).
            'direct_fax_images': _saving_view(result['direct_fax_images']),
            'case_packets': _saving_view(result['case_packets']),
            'sslfax': _saving_view(result['sslfax']),
            # Faxes to the installation's own numbers, delivered inside Faxbot with no call.
            'own_numbers': _saving_view(result['own_numbers']),
            # Faxes that called their recipient's approved toll-free number; the recipient pays those calls.
            'toll_free': _saving_view(result['toll_free']),
            # Pages saved by packing them onto long pages, and blank page bottoms left out (pages/).
            'packing': _saving_view(result['packing']),
            # Pages saved by the experimental encoded pages (pages/views.encoding_sentence).
            'encoding': _saving_view(result['encoding']),
            # The parts the savings map added (routing/mechanism_parts.py). Lightened pages are an estimate of time
            # and the relay is priced from its own records; the rest are exact counts or measurements, no money.
            'fax_friendly': _saving_view(result['fax_friendly']),
            'coding': _saving_view(result['coding']),
            'relay': _saving_view(result['relay']),
            **{key: _count_view(result[key]) for key in COUNTED_PARTS}}


# Savings parts that count or measure what a mechanism did, exactly, and carry no money.
COUNTED_PARTS = ('cheapest_route', 'plan_first', 'continuation', 'partner_repair', 'blocked_calls', 't38', 'digital',
                 'tunnel_calls')


def _count_view(part):
    return {**{key: value for key, value in part.items() if key != 'saved'}, 'estimate': False, 'saved': []}


@router.get('/savings/mechanisms', dependencies=[Depends(require_permission('settings:read'))])
async def savings_mechanisms(request: Request):
    """Every way Faxbot saves money, each with whether it is on, works here and has been tested; never money."""
    from .mechanisms import evaluate
    store = _store(request)
    values = request.scope['faxbot.configuration'].active.values
    return await _call(lambda: evaluate(values, store, store.engine))


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


@router.get('/recommendations/fax-marker', dependencies=[Depends(require_permission('settings:read'))])
async def fax_marker_recommendations(request: Request, days: int = Query(default=90, ge=7, le=366)):
    """Calls marked as fax against calls not marked, from history; never changes the setting."""
    from .preference import fax_marker_report
    store = _store(request)
    values = request.scope['faxbot.configuration'].active.values
    return await _call(lambda: fax_marker_report(store.engine, values, days=days))


@router.get('/recommendations/billing-steps', dependencies=[Depends(require_permission('settings:read'))])
async def billing_step_recommendations(request: Request):
    """Per number: how its calls end against the carrier's billing step over the last 30 days (estimates)."""
    from .boundaries import billing_boundaries
    store = _store(request)
    values = request.scope['faxbot.configuration'].active.values
    return await _call(lambda: billing_boundaries(store.engine, store, values))


@router.get('/recommendations/partners', dependencies=[Depends(require_permission('settings:read'))])
async def partner_recommendations(request: Request):
    """The numbers whose faxes cost the most again and again: candidates for direct partners (advice only)."""
    from .partners import partner_candidates
    store = _store(request)
    return await _call(lambda: partner_candidates(store))


@router.get('/recommendations/toll-free', dependencies=[Depends(require_permission('settings:read'))])
async def toll_free_recommendations(request: Request):
    """Recipients with a toll-free fax number on file, approved or not; the recipient pays for those calls."""
    from .tollfree import toll_free_recommendations as recommend
    store = _store(request)
    return await _call(lambda: recommend(store))


def _toll_free_view(store, number):
    from .tollfree import TollFreeApprovals, state_sentence
    approvals = TollFreeApprovals(store.engine)
    history = approvals.history(number)
    current = history[0] if history else None
    row = store.get_destination(number) or {}
    return {'number': number, 'current': current, 'history': history,
            'approved_alternate': approvals.approved_alternate(number),
            'sentence': state_sentence(current, row.get('display_name'))}


@router.get('/destinations/{number}/toll-free', dependencies=[Depends(require_permission('settings:read'))])
async def get_toll_free(number: str, request: Request):
    """A recipient's toll-free fax number and every approval recorded for it, newest first."""
    number = _number(number, request)
    store = _store(request)
    return await _call(lambda: _toll_free_view(store, number))


NPPES_SENTENCES = {
    'found': 'NPPES lists a toll-free fax number for this provider. Check that it reaches the same intake, then '
             'record who at the recipient agreed before Faxbot uses it.',
    'none': 'NPPES lists no toll-free fax number for this provider.',
}


@router.get('/destinations/{number}/toll-free/suggestions', dependencies=[Depends(require_permission('settings:read'))])
async def toll_free_suggestions(number: str, request: Request, npi: str | None = Query(default=None, max_length=10),
                                name: str | None = Query(default=None, max_length=200),
                                city: str | None = Query(default=None, max_length=100),
                                state: str | None = Query(default=None, max_length=2)):
    """Toll-free fax numbers the public NPI registry (NPPES) lists for a provider; a suggestion, never an approval.

    One read of the public registry per request. Nothing is recorded: the person still records who agreed.
    """
    _number(number, request)
    try:
        from .nppes import suggested_tollfree  # Builder AE's registry lookup
    except ImportError:
        raise HTTPException(503, detail='Looking up the NPI registry is not available in this version of Faxbot.') from None

    def look_up():
        return suggested_tollfree(npi or None, name=name or None, city=city or None, state=state or None)
    try:
        items = await run_lifecycle_step(look_up)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None
    except Exception:
        raise HTTPException(502, detail='Faxbot could not reach the NPI registry; try again.') from None
    from .tollfree import shown
    items = [{**item, 'fax_display': shown(item['fax_number'])} for item in items]
    return {'number': number, 'items': items, 'sentence': NPPES_SENTENCES['found' if items else 'none']}


class TollFreeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str = Field(max_length=16)
    alternate_number: str | None = Field(default=None, max_length=32)
    approved_by: str | None = Field(default=None, max_length=200)
    approved_on: datetime | None = None
    evidence: str | None = Field(default=None, max_length=2000)


TOLL_FREE_OPERATION = 'routing.toll_free_approval'


def _audit_toll_free(request, identity, number, row):
    """One audit row per recorded change: the numbers, the action and who agreed; never the evidence text."""
    import json
    from uuid import uuid4
    access = getattr(request.app.state, 'access_runtime', None)
    if access is None:
        return
    actor = identity.actor
    credential = getattr(actor, 'credential', None)
    details = {'number': number, 'alternate_number': row['alternate_number'], 'action': row['action'],
               'approved_by': row['approved_by'], 'approved_on': row['approved_on']}
    with access.store.transaction() as connection:
        version = access.store.require_lock_on(connection)
        connection.execute(access.store.tables['access_audit'].insert().values(
            id=uuid4().hex, actor_principal_id=getattr(actor, 'principal_id', None),
            actor_key_binding_id=getattr(credential, 'binding_id', None),
            actor_session_id=getattr(credential, 'session_id', None), operation=TOLL_FREE_OPERATION,
            target_kind='installation', target_id='toll_free_approvals', policy_version_before=version,
            policy_version_after=version, outcome='allowed',
            details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
            created_at=utcnow()))


@router.post('/destinations/{number}/toll-free')
async def record_toll_free(number: str, payload: TollFreeIn, request: Request,
                          identity=Depends(require_permission('settings:write'))):
    """Put a recipient's toll-free number on file, record who at the recipient approved it, or withdraw it.

    Each change is a new row (never an edit) and one audit row. Faxbot uses the toll-free number only while an
    approval is the newest row (``tollfree.approved_alternate``).
    """
    from .tollfree import TollFreeApprovals
    number = _number(number, request)
    store = _store(request)
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    approved_on = payload.approved_on
    if approved_on is not None and approved_on.tzinfo is not None:
        approved_on = approved_on.replace(tzinfo=None)

    def write():
        row = TollFreeApprovals(store.engine).record(
            number, action=payload.action, alternate_number=payload.alternate_number,
            approved_by=payload.approved_by, approved_on=approved_on, evidence=payload.evidence, country=country,
            principal_id=getattr(identity.actor, 'principal_id', None))
        _audit_toll_free(request, identity, number, row)
        return _toll_free_view(store, number)
    return await _call(write)


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
    # Prices by where calls start (origin_rates), shown by GET; their own rows are saved separately.
    rows: list | None = None


class RateCardsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cards: list[RateCardIn] = Field(max_length=100)


@router.get('/rate-cards', dependencies=[Depends(require_permission('settings:read'))])
async def list_rate_cards(request: Request):
    store = _store(request)
    cards = await _call(store.current_cards)
    # What each sending route publishes about calling toll-free numbers (an approved alternate), with its source.
    from .dialing import terms_view
    from .origin_rates import card_rows, organization_sites
    values = request.scope['faxbot.configuration'].active.values

    def rows():
        # Each sending card's prices by where calls start (origin_rates), shipped and saved, with source and date.
        sites = organization_sites(store.engine)
        return {card.provider_id: card_rows(card.provider_id, store.engine, sites)
                for card in cards if card.direction == 'outbound'}
    by_card = await _call(rows)
    return {'cards': [{**_card_view(card), 'rows': by_card.get(card.provider_id, []) if card.direction == 'outbound'
                       else []} for card in cards], 'toll_free': terms_view(values)}


class RateRowIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    origin: str = Field(max_length=40)
    destination_prefix: str = Field(max_length=16)
    per_minute: str | int = '0'
    per_page: str | int = '0'
    per_call: str | int = '0'
    billing_increment_seconds: int = 60
    minimum_seconds: int = 0
    source_url: str | None = Field(default=None, max_length=512)
    captured_on: datetime | None = None


class RateRowsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rows: list[RateRowIn] = Field(max_length=500)


@router.put('/rate-cards/{provider_id}/rows', dependencies=[Depends(require_permission('settings:write'))])
async def put_rate_rows(provider_id: str, payload: RateRowsIn, request: Request):
    """Replace the prices by where calls start that you entered for one sending card; earlier rows are kept as
    history (superseded), never changed. Shipped published rows are not affected."""
    from .origin_rates import OriginRate, card_rows, organization_sites, save_rows
    store = _store(request)
    identity = provider_id.strip().lower()[:64]

    def save():
        card = next((item for item in store.current_cards() if item.provider_id == identity
                     and item.direction == 'outbound'), None)
        if card is None or card.id is None:
            raise HTTPException(404, detail='Save a sending rate card for this route first; rows belong to a card.')
        try:
            rows = [OriginRate(identity, item.origin.strip(), item.destination_prefix.strip().lstrip('+'),
                               card.currency, parse_amount(str(item.per_minute)), parse_amount(str(item.per_page)),
                               parse_amount(str(item.per_call)), item.billing_increment_seconds, item.minimum_seconds,
                               item.source_url or None,
                               item.captured_on.replace(tzinfo=None) if item.captured_on else None)
                    for item in payload.rows]
            save_rows(store.engine, card.id, rows)
        except InvalidRateCard as error:
            raise HTTPException(400, detail=str(error)) from None
        return {'rows': card_rows(identity, store.engine, organization_sites(store.engine))}
    return await _call(save)


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


# Sending rules in delivery: held faxes, why a fax took its route, quotes, and applying the rules again ---------

def _delivery(request):
    _, runtime = installation_engine(request.app)
    if runtime is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    from ..outbound_store import OutboundStore
    return OutboundStore(runtime.manager.store)


def _hold_store(request):
    from .holds import HoldStore
    access = getattr(request.app.state, 'access_runtime', None)
    return HoldStore(_delivery(request), access_store=access.store if access is not None else None)


def _approver(request, identity):
    """Whether this person holds "Approve faxes" at the installation."""
    from ..access.types import ResourceRef
    service = request.app.state.access_runtime
    with service.store.transaction() as connection:
        service.store.require_lock_on(connection)
        return service.control.authorize_on(connection, identity.actor, 'fax:approve', ResourceRef('installation'),
                                            now=utcnow()).allowed


def _person_name(request, identity):
    import sqlalchemy as sa
    principal = getattr(identity.actor, 'principal_id', None)
    engine, _ = installation_engine(request.app)
    if not principal or engine is None:
        return None
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with engine.connect() as connection:
        return connection.execute(sa.select(principals.c.display_name).where(principals.c.id == principal)
                                  ).scalar_one_or_none()


async def _hold_call(operation):
    from .holds import HoldConflict, HoldForbidden, HoldInputError
    try:
        return await run_lifecycle_step(operation)
    except HoldInputError as error:
        raise HTTPException(400, detail=str(error)) from None
    except HoldForbidden as error:
        raise HTTPException(403, detail=str(error)) from None
    except HoldConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Held faxes are unavailable.') from None


@router.get('/holds')
async def list_holds(request: Request, state: str = Query(default='open', pattern='^(open|released|refused|all)$'),
                     identity=Depends(require_identity)):
    """Faxes your rules held: everyone's for someone with Approve faxes, else the ones you sent."""
    store = _hold_store(request)
    approver = await run_lifecycle_step(lambda: _approver(request, identity))
    zone = request.scope['faxbot.configuration'].active.values.time_zone

    def read():
        rows = store.holds(state=None if state == 'all' else state, every=approver,
                           principal_id=getattr(identity.actor, 'principal_id', None))
        return {'holds': [store.view(row, identity.actor, approver=approver, zone_name=zone) for row in rows],
                'can_approve': approver}
    return await _hold_call(read)


class HoldDecisionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    account: str | None = Field(default=None, max_length=64)


class HoldRefusalIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)


@router.post('/holds/{hold_id}/approve')
async def approve_hold(hold_id: str, payload: HoldDecisionIn, request: Request,
                       identity=Depends(require_permission('fax:approve'))):
    """Approve a held fax, or send a fax with no allowed route by an account anyway. Audited."""
    store, name = _hold_store(request), await run_lifecycle_step(lambda: _person_name(request, identity))
    return await _hold_call(lambda: store.approve(hold_id, version=payload.version, actor=identity.actor,
                                                  actor_name=name, account=payload.account))


@router.post('/holds/{hold_id}/refuse')
async def refuse_hold(hold_id: str, payload: HoldRefusalIn, request: Request,
                      identity=Depends(require_permission('fax:approve'))):
    """Refuse a held fax: it fails with your name and reason, and nothing is sent. Audited."""
    store, name = _hold_store(request), await run_lifecycle_step(lambda: _person_name(request, identity))
    return await _hold_call(lambda: store.refuse(hold_id, version=payload.version, actor=identity.actor,
                                                 actor_name=name, reason=payload.reason))


@router.post('/holds/{hold_id}/check-again')
async def check_hold_again(hold_id: str, payload: HoldDecisionIn, request: Request,
                           identity=Depends(require_identity)):
    """Let Faxbot try the accounts a no-route fax's rules allow again (its sender, or someone with Approve faxes)."""
    store = _hold_store(request)
    approver = await run_lifecycle_step(lambda: _approver(request, identity))

    def run():
        rows = store.holds(state='open', every=approver, principal_id=getattr(identity.actor, 'principal_id', None))
        if not any(row['id'] == hold_id for row in rows):
            from .holds import HoldInputError
            raise HoldInputError('There is no such held fax.')
        return store.check_again(hold_id, version=payload.version, actor=identity.actor)
    return await _hold_call(run)


@router.get('/faxes/{job_id}/route')
async def fax_route(job_id: str, request: Request, identity=Depends(require_identity)):
    """Why a sent fax took its route, for anyone who may read that fax."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.queries.job(identity.actor, job_id)))
    delivery, store = _delivery(request), _hold_store(request)
    approver = await run_lifecycle_step(lambda: _approver(request, identity))
    zone = request.scope['faxbot.configuration'].active.values.time_zone

    def read():
        from ..rules.store import RuleStore
        from .route_view import fax_route as route_view
        engine = delivery.configuration.engine
        try:
            rules = RuleStore(engine)
        except DeliveryStoreError:
            rules = None
        rows = store.holds(state='open', job_id=job_id)
        hold = store.view(rows[0], identity.actor, approver=approver, zone_name=zone) if rows else None
        return route_view(engine, delivery.configuration, job_id, rules=rules, hold_view=hold)
    return await _hold_call(read)


@router.post('/rules/apply-to-waiting')
async def apply_rules_to_waiting(request: Request, identity=Depends(require_permission('settings:write'))):
    """Re-decide faxes not yet sent under the active rules; never a fax already submitted or uncertain. Audited."""
    from ..rules.store import RuleStore
    from .route_view import apply_to_waiting
    delivery = _delivery(request)
    access = getattr(request.app.state, 'access_runtime', None)
    name = await run_lifecycle_step(lambda: _person_name(request, identity))
    rules = await _call(lambda: RuleStore(delivery.configuration.engine))
    return await _hold_call(lambda: apply_to_waiting(delivery, rules, actor=identity.actor, actor_name=name,
                                                     access_store=access.store if access is not None else None))


@router.get('/quote')
async def quote(request: Request, to: str = Query(max_length=64), pages: int = Query(default=1, ge=1, le=1000),
                site: str | None = Query(default=None, max_length=64),
                identity=Depends(require_permission('settings:read'))):
    """What one fax would cost by each account your rules allow, from where its calls start. Sends nothing.

    A fax a monthly plan carries reads "In your plan", never $0.00; an unknown price says so.
    """
    from ..accounts import all_accounts, sending_accounts
    from ..rules.evaluate import decide
    from ..rules.explain import FactsReader
    from ..rules.store import RuleStore
    from .pricing import plan_text
    from .rules_acceptance import alternate_lookup, sender_of
    values = request.scope['faxbot.configuration'].active.values
    number = _number(to, request)
    store = _store(request)

    def run():
        accounts = sending_accounts(values)
        principal, kind, key_id = sender_of(identity.actor)
        facts = FactsReader(store.engine, values, store, alternates=alternate_lookup(store.engine)).read(
            to_number=number, accounts=accounts, pages=pages, principal_id=principal, sender_kind=kind,
            key_id=key_id)
        decision = decide(RuleStore(store.engine).compiled_active(), facts, accounts)
        envelope = decision.envelope
        quoted = 'alternate' if envelope.dial is not None else 'original'
        by_key = {account.key: account for account in all_accounts(values)}
        keys = list(envelope.accounts)
        if site:
            keys.sort(key=lambda key: getattr(by_key.get(key), 'site', None) != site)
        # Where each call starts, in words (origin_rates: the row that priced it, else the account's site).
        from .origin_rates import organization_sites, origin_label
        from .pricing import price
        from ..rules import model as rules_model
        sites = organization_sites(store.engine)
        dialed = envelope.dial.number if envelope.dial is not None else number
        found = []
        for key in keys:
            item = next((q for q in facts.quotes if q.account == key and q.number == quoted), None)
            account = by_key.get(key)
            if site and account is not None:
                # Priced as a call from that site ("faxbot costs fax --from-site"), with its rows.
                try:
                    priced = price(store, values, key, dialed, pages, provider=account.provider, site=site,
                                   number=quoted)
                    item = rules_model.Quote(key, priced.micros, priced.currency if priced.micros is not None else None,
                                             number=quoted, origin=priced.origin, pages=pages, plan=priced.plan)
                except Exception:
                    pass
            in_plan = item is not None and item.plan is not None
            account_site = site or getattr(account, 'site', None)
            where = (origin_label(item.origin, sites) if item is not None and item.origin
                     else origin_label(account_site, sites) if account_site else None)
            found.append({
                'account': key, 'label': account.label if account is not None else route_label(key),
                'site': getattr(account, 'site', None), 'origin_label': where,
                'origin': item.origin if item is not None else None,
                'estimate': (None if item is None or in_plan or item.micros is None
                             else {'currency': item.currency, 'amount': format_amount(item.micros)}),
                'estimate_text': plan_text(item.plan if item else None, item.micros if item else None),
                'in_plan': in_plan, 'over_budget': bool(item is not None and item.plan == 'over_budget'),
                'sentence': _quote_sentence(item)})
        return {'to': number, 'pages': pages, 'quotes': found,
                'sentence': None if found else 'No account your rules allow can send to this number.'}
    return await _call(run)


def _quote_sentence(item):
    if item is None:
        return 'Faxbot could not price this account.'
    if item.plan == 'over_budget':
        return 'Your monthly plan covers it, but this month is past the normal-use budget you set for it.'
    if item.plan == 'included':
        return 'Your monthly plan covers it; this fax adds nothing to the bill.'
    if item.micros is None:
        return 'Its price for this number is not published, so the cost is unknown.'
    return 'An estimate from its price for this number and the usual time on the line.'
