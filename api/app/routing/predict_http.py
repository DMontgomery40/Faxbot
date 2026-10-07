"""The dry run: what a fax to one number would take and cost on every route it may use, before sending.

``GET /routing/predict?to=&pages=&layout=&resolution=`` answers with one
prediction per allowed route (the outbound provider and the extra routes in
``FAX_OUTBOUND_ROUTES``), from the shared predictor. Nothing is sent, queued
or recorded. Every figure is an estimate and says how it was worked out.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine
from .costs import format_amount, money_text, parse_amount
from .destinations import CLASS_TEXT, INTERNATIONAL, country_name
from .numbers import InvalidNumber, normalize_number
from .plan import extra_routes
from .predict import LAYOUTS, RESOLUTIONS, Shape, boundary, predict_from


router = APIRouter(prefix='/routing', tags=['Delivery routes'])


def _number(value, values):
    try:
        return normalize_number(value, country=getattr(values, 'fax_default_country', 'US'))
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _routes(request, snapshot):
    """The outbound provider and the extra routes this installation may send by, in that order."""
    _, runtime = installation_engine(request.app)
    identity = snapshot.active.profile_id('outbound')
    if runtime is None or identity is None:
        return []
    bound = runtime.manager.store.read_profile(identity).configuration.provider_id
    return [bound, *extra_routes(snapshot.active.values, bound)]


def headline(prediction, pages):
    """One short sentence for the cost: 'About $0.005 for this 3-page fax.'"""
    if prediction.cost is None:
        return 'Cost unknown.'
    if prediction.marginal:
        if prediction.cost.micros == 0:
            return 'Nothing extra for this fax.'
        return f'About {money_text(prediction.cost.micros, prediction.cost.currency)} extra for this fax.'
    if prediction.cost.micros == 0:
        return 'Nothing for this fax.'
    return f'About {money_text(prediction.cost.micros, prediction.cost.currency)} for this {pages}-page fax.'


def class_text(where):
    if where.kind == INTERNATIONAL:
        return f'a number in {country_name(where.region)}'
    return {'local': 'a local number', 'toll_free': 'a toll-free number', 'premium': 'a premium-rate number',
            'unknown': 'a number Faxbot cannot place'}.get(where.kind, CLASS_TEXT.get(where.kind, 'this number'))


def _view(facts, prediction, pages):
    card = facts.terms.card if facts.terms is not None else None
    billed, room = boundary(card, prediction.seconds) if card is not None and card.per_minute_micros else (None, None)
    return {'route': facts.route_key, 'label': facts.label, 'billed_pages': prediction.billed_pages,
            'seconds': None if prediction.seconds is None else round(prediction.seconds, 1),
            'billed_seconds': billed, 'seconds_to_next_step': None if room is None else round(room, 1),
            'cost': (None if prediction.cost is None else
                     {'amount': format_amount(prediction.cost.micros), 'currency': prediction.cost.currency}),
            'cost_text': (None if prediction.cost is None
                          else money_text(prediction.cost.micros, prediction.cost.currency)),
            'marginal': prediction.marginal, 'headline': headline(prediction, pages), 'basis': prediction.basis}


def cheapest_sentence(views):
    """Which route costs least, in one sentence, or why Faxbot cannot say."""
    known = [view for view in views if view['cost'] is not None]
    if not views:
        return 'No sending route is set up yet.'
    if not known:
        return 'No route has a known price for this fax.'
    currencies = {view['cost']['currency'] for view in known}
    if len(currencies) > 1:
        return 'The routes are priced in different currencies, so Faxbot does not compare them.'
    best = min(known, key=lambda view: (_micros(view), view['seconds'] if view['seconds'] is not None else 1e9))
    rest = len(views) - len(known)
    lowered = best['headline'][0].lower() + best['headline'][1:]
    if len(views) == 1:
        return f"{best['label']} is your only sending route: {lowered}"
    sentence = f"{best['label']} would cost least: {lowered}"
    if rest:
        sentence = sentence[:-1] + f"; {rest} other route{'' if rest == 1 else 's'} {'has' if rest == 1 else 'have'} no known price."
    return sentence


def _micros(view):
    return parse_amount(view['cost']['amount'])


@router.get('/predict', dependencies=[Depends(require_permission('settings:read'))])
async def predict_routes(request: Request, to: str = Query(..., min_length=1, max_length=40),
                         pages: int = Query(default=1, ge=1, le=1000),
                         layout: str = Query(default='normal', max_length=16),
                         resolution: str = Query(default='fine', max_length=16)):
    """What a fax of ``pages`` pages to ``to`` would take and cost on each route; estimates only, nothing is sent."""
    if layout not in LAYOUTS[:2]:
        raise HTTPException(400, detail='Choose a normal or dense layout.')
    if resolution not in RESOLUTIONS:
        raise HTTPException(400, detail='Choose standard, fine, superfine, 300 or 400 resolution.')
    snapshot = request.scope.get('faxbot.configuration')
    if snapshot is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    values = snapshot.active.values
    number = _number(to, values)
    engine, _ = installation_engine(request.app)
    shape = Shape(pages, None, resolution, layout)

    def read():
        from .predict_facts import facts_for
        routes = _routes(request, snapshot)
        found = []
        for route in routes:
            facts = facts_for(route, number, values=values, engine=engine)
            found.append((facts, predict_from(facts, shape)))
        where = found[0][0].destination if found else None
        return where, [_view(facts, prediction, pages) for facts, prediction in found]
    where, views = await run_lifecycle_step(read)
    if where is None:
        from .destinations import classify
        where = classify(number, getattr(values, 'fax_default_country', 'US'))
    return {'to': number, 'number_class': where.kind, 'number_class_text': class_text(where), 'pages': pages,
            'layout': layout, 'resolution': resolution, 'routes': views, 'sentence': cheapest_sentence(views),
            'note': 'Estimates before sending; the bill comes from your carrier or provider.'}
