"""The dry run: what a fax to one number would take and cost on every route it may use, before sending.

``GET /routing/predict?to=&pages=&layout=&resolution=`` answers with one
prediction per allowed route (the outbound provider and the extra routes in
``FAX_OUTBOUND_ROUTES``), from the shared predictor and the page count only, so
it is marked approximate. ``POST /routing/predict`` takes the document itself
(``to`` and ``file``, a PDF or plain text): your sending rules decide its
accounts as they would for this fax sent now by you, its pages are drawn as fax
pages, every account's best pages are measured and priced on that account's own
tariff, and the accounts are ranked exactly as the delivery worker ranks them
(``routing.joint``): ``selected`` is the account and pages the worker would
send, ``runner_up`` the cheapest other account. Nothing is sent, queued,
recorded or kept. Every figure is an estimate and says how it was worked out,
with the time 9 in 10 such calls finish within.
"""
from pathlib import Path
import tempfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine
from .costs import format_amount, money_text, parse_amount
from .destinations import CLASS_TEXT, INTERNATIONAL, country_name
from .numbers import InvalidNumber, normalize_number
from .plan import extra_routes
from .predict import LAYOUTS, RESOLUTIONS, Shape, boundary, duration_text, predict_from


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


def _outbound(request, snapshot):
    """The active outbound profile's configuration, or None."""
    _, runtime = installation_engine(request.app)
    identity = snapshot.active.profile_id('outbound')
    if runtime is None or identity is None:
        return None
    return runtime.manager.store.read_profile(identity).configuration


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


def finish_sentence(prediction):
    """'9 in 10 such calls should finish within about 1 minute 6 seconds, from an assumed spread ...'; None when
    the time is unknown or there is no call."""
    if not prediction.p90_seconds or not prediction.spread:
        return None
    return (f'9 in 10 such calls should finish within {duration_text(prediction.p90_seconds)}, from '
            f'{prediction.spread}.')


def _view(facts, prediction, pages):
    card = facts.terms.card if facts.terms is not None else None
    billed, room = boundary(card, prediction.seconds) if card is not None and card.per_minute_micros else (None, None)
    return {'route': facts.route_key, 'label': facts.label, 'billed_pages': prediction.billed_pages,
            'seconds': None if prediction.seconds is None else round(prediction.seconds, 1),
            'billed_seconds': billed, 'seconds_to_next_step': None if room is None else round(room, 1),
            # Over the spread of the call's time: what is expected to be billed, and when 9 in 10 calls finish.
            'expected_billed_seconds': (None if prediction.expected_billed_seconds is None
                                        else round(prediction.expected_billed_seconds, 1)),
            'p90_seconds': None if prediction.p90_seconds is None else round(prediction.p90_seconds, 1),
            'finish_sentence': finish_sentence(prediction),
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
    # From the page count only: the document itself can change the pages sent and the account chosen.
    return {'to': number, 'number_class': where.kind, 'number_class_text': class_text(where), 'pages': pages,
            'layout': layout, 'resolution': resolution, 'routes': views, 'sentence': cheapest_sentence(views),
            'approximate': True,
            'note': 'Estimates before sending; the bill comes from your carrier or provider.'}


# The dry run with the document itself --------------------------------------------------------------------------

def _document_pages(source, folder, stem='document'):
    """``(frames, pdf, tiff)``: the document's pages as fax pages (mode "1" frames), from a PDF or plain text, with
    its PDF and fax image in ``folder`` named ``<stem>.pdf`` and ``<stem>.tiff`` as an accepted fax's files are;
    raises DocumentConversionError."""
    from .. import conversion
    import shutil
    pdf, tiff = Path(folder) / f'{stem}.pdf', Path(folder) / f'{stem}.tiff'
    with open(source, 'rb') as handle:
        is_pdf = handle.read(4) == b'%PDF'
    # The same checks as a fax accepted with POST /fax (documents.prepare_upload): pages, sizes and raster limits.
    if is_pdf:
        conversion.validate_pdf(str(source))
        shutil.copyfile(source, pdf)
    else:
        conversion.txt_to_pdf(str(source), str(pdf))
        conversion.validate_pdf(str(pdf))
    conversion.pdf_to_tiff(str(pdf), str(tiff))
    frames = conversion.read_fax_frames(str(tiff))
    if not frames:
        raise conversion.DocumentConversionError('The document has no fax pages.')
    return frames, pdf, tiff


def _coded_view(engine, values, number, facts, frames, measured, resolution):
    """One route's prediction for these pages: Faxbot's own engines with the coding they would be asked for (and
    why), any other route with the pages as Faxbot prepares them (a fax service codes them itself)."""
    from ..pages import coding
    pages = len(frames)
    if facts.route_key != 'sip':
        shape = Shape(pages, tuple(measured['MMR']), resolution, 'normal')
        return {**_view(facts, predict_from(facts, shape), pages), 'coding': None}
    from .. import hylafax_engine
    from ..pages.capability import records_for
    usable = coding.usable_for(engine, values, number, recipient=hylafax_engine.recipient_limits(engine, number),
                               capability=records_for(engine).capability(number) if engine is not None else None)
    choice = coding.best_coding(frames, usable.codings, ecm=usable.ecm, measured=measured)
    shape = Shape(pages, tuple(measured['MMR']), resolution, 'normal', measured,
                  choice.priced)
    return {**_view(facts, predict_from(facts, shape), pages),
            'coding': {'coding': choice.coding, 'measured': choice.measured,
                       'sentence': f'Faxbot would send these pages with {choice.reason}'}}


def _account_label(values, key):
    """An account's name as you gave it (``Page trunk``), else its route's (``Telnyx``)."""
    from .plan import route_label
    try:
        from ..accounts import account_named
        account = account_named(values, key)
    except Exception:
        account = None
    if account is not None and not account.primary and account.label:
        return account.label
    return route_label(key)


def preview_plan(engine, revision, number, pages, actor, *, measured=None):
    """``(plan, prices, decision)`` for a fax of ``pages`` pages to ``number`` sent now by ``actor``: your sending
    rules decided as at acceptance (``rules_acceptance.prepare``), and the accounts they allow ranked as the delivery
    worker ranks them (``RoutedTransport._plan``), by ``measured`` pages where given."""
    from ..accounts import default_sending_key
    from . import envelope as envelopes, rules_acceptance
    from .alternates import current
    from .plan import RoutePlanner
    from .pricing import prices_for
    from .store import RouteStore
    values = revision.values
    routes = RouteStore(engine)
    prepared = rules_acceptance.prepare(engine, revision, actor=actor, destination=number, pages=pages)
    pinned = envelopes.Pinned('preview', 1, prepared.decision, prepared.facts)
    bound = prepared.bound_key or default_sending_key(values)
    approval = current(number, engine=engine)
    dial = {'alternate': approval.alternate, 'refused': False} if approval is not None else None
    prices = prices_for(routes, values, number, pages, pinned=pinned, bound=bound, dial=dial, measured=measured)
    plan = RoutePlanner(routes).plan(to_number=number, bound=bound, values=values, pages=pages, alternates=True,
                                     dial=dial, pinned=pinned, current=values, prices=prices)
    return plan, prices, prepared.decision


def _layout_rule(decision):
    layout = decision.envelope.page_layout if decision is not None else None
    return {'as_receiver_allows': 'allow', 'one_per_sheet': 'never'}.get(layout)


def _measured_view(values, key, evaluated, price):
    """One account's view from its measured best pages, priced as the worker prices it."""
    from .predict import predict_from
    facts = evaluated.account.facts
    chosen = evaluated.chosen
    prediction = predict_from(facts, evaluated.shape)
    view = _view(facts, prediction, chosen.pages)
    if price is not None:
        view['cost'] = price.money()
        view['cost_text'] = (None if price.micros is None or price.in_plan
                             else money_text(price.micros, price.currency))
        view['marginal'] = bool(price.in_plan or view['marginal'])
    coded = None
    choice = (evaluated.work.get('choice') or {}).get('coding')
    if choice is not None:
        # The coding the call would ask for, measured on these pages, and why (pages.coding.CodingChoice).
        coded = {'coding': choice.coding, 'measured': True,
                 'sentence': f'Faxbot would send these pages with {choice.reason}'}
    return {**view, 'route': key, 'label': _account_label(values, key), 'layout': chosen.layout,
            'rendering': chosen.rendering, 'sent_pages': chosen.pages, 'original_pages': evaluated.original_pages,
            'coding': coded}


def _plan_choice(values, key, evaluated, price):
    """The selected or runner-up account, as Sent details word it."""
    chosen = evaluated.chosen
    return {'route': key, 'label': _account_label(values, key), 'layout': chosen.layout,
            'rendering': chosen.rendering, 'coding': chosen.coding, 'original_pages': evaluated.original_pages,
            'sent_pages': chosen.pages, 'cost': price.money() if price is not None else None,
            'in_plan': bool(price is not None and price.in_plan), 'measured': bool(evaluated.measured)}


def _configuration(revision, key, bound, outbound):
    """The configuration an account sends with: the outbound profile's for the fax's own account, else the account's
    own from the active revision (``accounts.route_configuration``, raising ``routes.RouteUnavailable``)."""
    from ..accounts import route_configuration
    if key == bound and outbound is not None:
        return outbound
    return route_configuration(revision, key)


def measured_plan(engine, revision, number, claim, pdf, tiff, pages, actor, *, outbound=None):
    """``(views, plan_view)`` for this document sent now by ``actor``: each account the rules allow priced on its own
    measured best pages, in the order the delivery worker ranks them, and the account it would bind with the
    runner-up and one sentence (``plan_view`` None when no account could be measured). The same functions as the
    worker: ``preview_plan`` (``RoutedTransport._plan``) and ``joint.measure_document`` (``joint.measure``).
    ``outbound`` is the outbound profile's configuration, which a fax accepted now is bound to (its default
    sending account), as the worker uses the fax's own accepted profile."""
    from ..accounts import default_sending_key
    from . import joint, selections
    values = revision.values
    plan, prices, decision = preview_plan(engine, revision, number, pages, actor)
    bound = default_sending_key(values)
    found = joint.measure_document(
        engine, values, claim, plan, {'to_number': number, 'pages': pages}, pdf, tiff,
        configuration_for=lambda key: _configuration(revision, key, bound, outbound), rule=_layout_rule(decision))
    if found.measured:
        plan, prices, decision = preview_plan(engine, revision, number, pages, actor, measured=found.measured)
    views = [_measured_view(values, choice.route.key, found.frontiers[choice.route.key], prices.get(choice.route.key))
             for choice in plan.choices if choice.route.key in found.frontiers]
    first = next((choice for choice in plan.choices if choice.route.key in found.frontiers), None)
    if first is None:
        return views, None
    key = first.route.key
    summary = selections.summary(plan, found, key, prices)
    runner = summary['runner']
    selected = _plan_choice(values, key, found.frontiers[key], prices.get(key))
    runner_view = (_plan_choice(values, runner['key'], found.frontiers[runner['key']], prices.get(runner['key']))
                   if runner else None)
    expected = prices.get(key)
    record = {'account_key': key, 'sent_pages': selected['sent_pages'], 'layout': selected['layout'],
              'original_pages': selected['original_pages'],
              'expected_micros': expected.micros if expected is not None else None,
              'currency': expected.currency if expected is not None else None,
              'runner_key': runner['key'] if runner else None, 'runner_layout': runner['layout'] if runner else None,
              'runner_pages': runner['pages'] if runner else None,
              'runner_micros': runner['micros'] if runner else None,
              'runner_currency': runner['currency'] if runner else None}
    return views, {'selected': selected, 'runner_up': runner_view, 'compared': summary['compared'],
                   'sentence': selections.sentence(record, phase='preview',
                                                   label=lambda item: _account_label(values, item)),
                   'held': decision.outcome != 'route'}


@router.post('/predict')
async def predict_document(request: Request, to: str = Form(..., min_length=1, max_length=40),
                           file: UploadFile = File(...),
                           identity=Depends(require_permission('fax:send', resource='personal'))):
    """What this document faxed to ``to`` would take and cost, and the account and pages Faxbot would send it with;
    estimates only, nothing is sent or kept. Your sending rules decide its accounts as for a fax you send now, every
    account's best pages are measured on its own tariff, and the accounts are ranked as the delivery worker ranks
    them. It draws the document's pages as sending one does, so it takes the permission to send faxes and the same
    size and page limits as ``POST /fax``."""
    import sqlalchemy as sa
    from types import SimpleNamespace
    from uuid import uuid4
    from .. import conversion
    from ..pages import coding
    from .database import DeliveryStoreError
    snapshot = request.scope.get('faxbot.configuration')
    if snapshot is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    values = snapshot.active.values
    number = _number(to, values)
    engine, _ = installation_engine(request.app)
    limit = int(getattr(values, 'max_file_size_mb', 10) or 10) * 1024 * 1024
    with tempfile.TemporaryDirectory(prefix='faxbot-dry-run-') as folder:
        source, total = Path(folder) / 'source', 0
        with source.open('xb') as output:
            while chunk := await file.read(64 * 1024):
                total += len(chunk)
                if total > limit:
                    raise HTTPException(413, detail='File exceeds the configured upload limit.')
                output.write(chunk)
        if total == 0:
            raise HTTPException(400, detail='Document is empty.')

        def read():
            from .destinations import classify
            # Named as an accepted fax's files are, so measuring and its caches stay inside this folder.
            claim = SimpleNamespace(job_id=uuid4().hex, attempt_id=uuid4().hex, members=())
            frames, pdf, tiff = _document_pages(source, folder, stem=claim.job_id)
            measured = coding.measure(frames)
            revision = snapshot.active
            views, plan_view = measured_plan(engine, revision, number, claim, pdf, tiff, len(frames), identity.actor,
                                             outbound=_outbound(request, snapshot))
            if not views:
                # No account could be measured: each one priced from the document's pages as they are.
                from .predict_facts import facts_for
                facts = [facts_for(route, number, values=values, engine=engine)
                         for route in _routes(request, snapshot)]
                resolution = conversion.frames_resolution(frames)
                views = [_coded_view(engine, values, number, item, frames, measured, resolution) for item in facts]
            return frames, measured, classify(number, getattr(values, 'fax_default_country', 'US')), views, plan_view
        try:
            frames, measured, where, views, plan_view = await run_lifecycle_step(read)
        except conversion.DocumentConversionError as error:
            raise HTTPException(503 if error.operational else 400, detail=str(error)) from None
        except coding.CodingRefused as error:
            raise HTTPException(400, detail=str(error)) from None
        except (DeliveryStoreError, sa.exc.SQLAlchemyError):
            raise HTTPException(503, detail='Faxbot could not read what it knows about this number; try again.') \
                from None
    if where is None:
        from .destinations import classify
        where = classify(number, getattr(values, 'fax_default_country', 'US'))
    pages = len(frames)
    totals = {name: sum(bits) for name, bits in measured.items()}
    sentence = cheapest_sentence(views)
    if plan_view is not None and plan_view['sentence']:
        sentence = plan_view['sentence']
        if plan_view['held']:
            sentence = sentence[:-1] + '; your sending rules hold it first.'
    selected = plan_view['selected'] if plan_view is not None else None
    return {'to': number, 'number_class': where.kind, 'number_class_text': class_text(where), 'pages': pages,
            'layout': selected['layout'] if selected else 'normal', 'resolution': conversion.frames_resolution(frames),
            'routes': views, 'measured': totals, 'measured_sentence': coding.measured_sentence(totals),
            'jbig_measured': 'JBIG' in measured, 'sentence': sentence, 'approximate': False,
            'selected': selected, 'runner_up': plan_view['runner_up'] if plan_view is not None else None,
            'compared': plan_view['compared'] if plan_view is not None else 0,
            'held': bool(plan_view and plan_view['held']),
            'note': 'Estimates before sending; the bill comes from your carrier or provider.'}
