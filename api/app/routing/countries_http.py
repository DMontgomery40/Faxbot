"""Country routes: prices by the caller ID a call presents (N15), and which caller IDs you confirmed.

Reads need ``settings:read``; importing a deck and confirming or withdrawing a caller ID need
``settings:write``. Nothing here changes a caller ID, places a call or contacts a carrier.
"""
from datetime import datetime
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity
from ..access.route_policy import require_permission
from .http import _call, _store
from .number_http import _actor_name


router = APIRouter(prefix='/routing', tags=['Countries'])
MAX_DECK_FILE = 40 * 1024 * 1024
_ROUTE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')


def _values(request):
    return request.scope['faxbot.configuration'].active.values


def _who(engine, identity):
    return {'id': getattr(identity.actor, 'principal_id', None), 'name': _actor_name(engine, identity.actor)}


def _sending_accounts(values):
    from ..accounts import all_accounts
    return [account for account in all_accounts(values) if account.sends and account.enabled]


def callers_view(engine, values):
    """Each sending account's caller ID as its calls present it, and what you confirmed about it."""
    from ..provider_labels import provider_label
    from .origin_classes import eligibility, eligibility_view, presented_caller_id
    found = []
    for account in _sending_accounts(values):
        caller, how = presented_caller_id(values, account.key, engine=engine)
        if how == 'provider':
            sentence = f'{provider_label(account.provider)} sets its own sending number, so its calls are not priced by caller ID.'
        elif caller is None:
            sentence = 'Calls from this account carry no caller ID Faxbot can read, so they get the rate for any other caller ID.'
        else:
            sentence = f'Calls from this account present {caller}.'
        found.append({'account': account.key, 'label': account.label, 'provider': account.provider,
                      'provider_label': provider_label(account.provider), 'site': account.site,
                      'caller_id': caller, 'priced_by_caller_id': how != 'provider', 'sentence': sentence,
                      'eligibility': eligibility_view(eligibility(engine, account.key, caller)) if caller else None})
    return found


@router.get('/caller-id-prices', dependencies=[Depends(require_permission('settings:read'))])
async def caller_id_prices(request: Request):
    """The carrier decks Faxbot prices by caller ID, and the caller ID each sending account presents."""
    from .origin_classes import FAXBOT_LAYOUT, TELNYX_LAYOUT, decks
    store = _store(request)
    values = _values(request)
    return await _call(lambda: {'decks': list(decks(store.engine).values()),
                                'callers': callers_view(store.engine, values),
                                'layouts': {'faxbot': FAXBOT_LAYOUT, 'telnyx': TELNYX_LAYOUT}})


@router.get('/caller-id-prices/quote', dependencies=[Depends(require_permission('settings:read'))])
async def caller_id_quote(request: Request, to: str = Query(..., min_length=3, max_length=40)):
    """How a call to ``to`` is priced by caller ID on each sending account with a deck that covers it."""
    from .destinations import classify
    from .origin_classes import eligibility, presented_caller_id, price_origin, quote_view, rows_for
    from .dialing import route_identity
    store = _store(request)
    values = _values(request)
    where = classify(to, getattr(values, 'fax_default_country', 'US') or 'US')
    if where.number is None:
        raise HTTPException(400, detail='Enter a fax number with its country code, such as +4930123456.')

    def quote():
        found = []
        for account in _sending_accounts(values):
            identity = route_identity(account.provider, getattr(values, 'sip_trunk_preset', '') or '')
            rows = rows_for(store.engine, [account.key, identity], where.number)
            if not rows:
                continue
            caller, _ = presented_caller_id(values, account.key, engine=store.engine)
            record = eligibility(store.engine, account.key, caller) if caller else None
            result = price_origin(rows, where.number, caller, record)
            if result is not None:
                found.append({'account': account.key, 'label': account.label, **quote_view(result)})
        return {'number': where.number, 'quotes': found}
    return await _call(quote)


@router.post('/rate-cards/{route}/caller-id-prices', dependencies=[Depends(require_permission('settings:write'))])
async def import_caller_id_prices(route: str, request: Request, identity=Depends(require_identity),
                                  file: UploadFile = File(...),
                                  deck_format: str | None = Form(default=None, max_length=16),
                                  source_url: str | None = Form(default=None, max_length=512),
                                  published_on: str | None = Form(default=None, max_length=10),
                                  currency: str | None = Form(default=None, max_length=3),
                                  billing_increment_seconds: int | None = Form(default=None, ge=1, le=3600),
                                  minimum_seconds: int | None = Form(default=None, ge=0, le=3600)):
    """Import a carrier's rate deck priced by caller ID for one sending route (Twilio's voice price file, or
    Faxbot's own layout). The route's earlier deck is kept as history."""
    from .origin_classes import DeckError, import_deck, parse_deck
    store = _store(request)
    route = route.strip().lower()
    if _ROUTE.fullmatch(route) is None:
        raise HTTPException(400, detail='Choose a sending route, such as sip-telnyx or an account name.')
    if source_url is not None and source_url and re.fullmatch(r'https?://\S+', source_url) is None:
        raise HTTPException(400, detail='The source must be a web address.')
    data = await file.read(MAX_DECK_FILE + 1)
    if len(data) > MAX_DECK_FILE:
        raise HTTPException(413, detail='The rate deck is larger than 40 MB.')
    try:
        captured = datetime.strptime(published_on, '%Y-%m-%d') if published_on else None
    except ValueError:
        raise HTTPException(400, detail='Write the date the deck was published or read as 2026-10-09.') from None

    def save():
        # A deck without its own increment or currency (Twilio's) takes the route's rate card's.
        card = next((item for item in store.current_cards() if item.provider_id == route
                     and item.direction == 'outbound'), None)
        money = (currency or (card.currency if card else 'USD')).upper()
        increment = billing_increment_seconds or (card.billing_increment_seconds if card else 60)
        least = minimum_seconds if minimum_seconds is not None else (card.minimum_seconds if card else 0)
        try:
            rows, skipped = parse_deck(data.decode('utf-8-sig', errors='replace'), route=route, currency=money,
                                       deck_format=deck_format or None, billing_increment_seconds=increment,
                                       minimum_seconds=least, source_url=source_url or None, captured_on=captured)
        except DeckError as error:
            raise HTTPException(400, detail=str(error)) from None
        summary = import_deck(store.engine, route, rows, actor=_who(store.engine, identity))
        took = (f'Rows without their own billing step use {increment}-second steps'
                + (f' with at least {least} seconds' if least else '') + f', in {money}'
                + (', from your rate card.' if card else ', because this route has no sending rate card.'))
        return {'deck': summary, 'skipped': skipped[:50], 'skipped_count': len(skipped), 'terms': took}
    return await _call(save)


class CallerIdIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account: str = Field(min_length=1, max_length=64)
    caller_id: str = Field(min_length=3, max_length=32)
    bought_here: bool = False
    evidence: str = Field(min_length=1, max_length=2000)
    evidence_url: str | None = Field(default=None, max_length=512)


class CallerIdWithdrawIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account: str = Field(min_length=1, max_length=64)
    caller_id: str = Field(min_length=3, max_length=32)
    note: str = Field(default='', max_length=2000)


def _known_account(values, key):
    if key not in {account.key for account in _sending_accounts(values)}:
        raise HTTPException(404, detail='Faxbot has no sending account with this name.')


@router.post('/caller-ids/confirm', dependencies=[Depends(require_permission('settings:write'))])
async def confirm_caller_id(payload: CallerIdIn, request: Request, identity=Depends(require_identity)):
    """Record that you hold this caller ID and may send faxes from it on this account, with your evidence; with
    ``bought_here``, also that it was bought on this account. Earlier records stay as history."""
    from .origin_classes import EligibilityError, eligibility_view, record_eligibility
    store = _store(request)
    values = _values(request)
    _known_account(values, payload.account)

    def save():
        try:
            record = record_eligibility(store.engine, payload.account, payload.caller_id,
                                        bought_here=payload.bought_here, evidence=payload.evidence,
                                        evidence_url=payload.evidence_url or None,
                                        actor=_who(store.engine, identity))
        except EligibilityError as error:
            raise HTTPException(400, detail=str(error)) from None
        return {'eligibility': eligibility_view(record), 'callers': callers_view(store.engine, values)}
    return await _call(save)


@router.post('/caller-ids/withdraw', dependencies=[Depends(require_permission('settings:write'))])
async def withdraw_caller_id(payload: CallerIdWithdrawIn, request: Request, identity=Depends(require_identity)):
    """Withdraw a caller-ID confirmation; calls from it are priced as unconfirmed again. History is kept."""
    from .origin_classes import EligibilityError, eligibility_view, withdraw_eligibility
    store = _store(request)
    values = _values(request)

    def save():
        try:
            record = withdraw_eligibility(store.engine, payload.account, payload.caller_id, note=payload.note,
                                          actor=_who(store.engine, identity))
        except EligibilityError as error:
            raise HTTPException(409, detail=str(error)) from None
        return {'eligibility': eligibility_view(record), 'callers': callers_view(store.engine, values)}
    return await _call(save)


# -- registered senders (N17) ----------------------------------------------------------------------------------------

class SenderPinIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account: str = Field(min_length=1, max_length=64)
    caller_id: str = Field(min_length=3, max_length=32)
    station_id: str | None = Field(default=None, max_length=20)
    note: str = Field(default='', max_length=2000)


class OriginalIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: str = Field(min_length=1, max_length=16)
    note: str = Field(default='', max_length=2000)


def pins_view(engine, values):
    """Every registered sender, whether its trunk would show the registered identity now, and what each trunk
    shows (for the form)."""
    from .origin_classes import presented_identity
    from .sender_pins import pin_view, pins, presentable
    accounts = {account.key: account for account in _sending_accounts(values)}
    found = []
    for pin in pins(engine):
        ok, why = presentable(values, pin.account, pin.caller_id, pin.station_id, engine=engine)
        name = accounts[pin.account].label if pin.account in accounts else pin.account
        found.append({**pin_view(pin), 'account_label': name, 'ready': ok,
                      'sentence': (f'Faxes to {pin.recipient} go only by {name}, showing {pin.caller_id}.' if ok
                                   else f'Faxes to {pin.recipient} wait in Sent: {why}')})
    trunks = []
    for account in accounts.values():
        caller, station, how = presented_identity(values, account.key, engine=engine)
        if how != 'provider':
            trunks.append({'account': account.key, 'label': account.label, 'caller_id': caller,
                           'station_id': station or caller})
    return {'pins': found, 'trunks': trunks}


@router.get('/sender-pins', dependencies=[Depends(require_permission('settings:read'))])
async def sender_pins(request: Request):
    """Recipients that recognise your faxes by the number they come from, and the trunk registered with each."""
    store = _store(request)
    values = _values(request)
    return await _call(lambda: pins_view(store.engine, values))


@router.put('/sender-pins/{number}', dependencies=[Depends(require_permission('settings:write'))])
async def pin_sender(number: str, payload: SenderPinIn, request: Request, identity=Depends(require_identity)):
    """Pin a recipient to the trunk, caller ID and station ID registered with it; faxes to it never go another way.
    Earlier pins stay as history."""
    from .sender_pins import PinError, record
    store = _store(request)
    values = _values(request)

    def save():
        try:
            record(store.engine, values, number, account=payload.account, caller_id=payload.caller_id,
                   station_id=payload.station_id, note=payload.note or None, actor=_who(store.engine, identity))
        except PinError as error:
            raise HTTPException(400, detail=str(error)) from None
        return pins_view(store.engine, values)
    return await _call(save)


@router.delete('/sender-pins/{number}', dependencies=[Depends(require_permission('settings:write'))])
async def unpin_sender(number: str, request: Request, identity=Depends(require_identity),
                       note: str = Query(default='', max_length=2000)):
    """End a recipient's pin; faxes to it go by your sending rules again. Its history stays."""
    from .sender_pins import PinError, remove
    store = _store(request)
    values = _values(request)

    def save():
        try:
            remove(store.engine, number, note=note or None, actor=_who(store.engine, identity))
        except PinError as error:
            raise HTTPException(404, detail=str(error)) from None
        return pins_view(store.engine, values)
    return await _call(save)


@router.get('/faxes/{job_id}/sender-evidence')
async def sender_evidence(job_id: str, request: Request, identity=Depends(require_identity)):
    """The sender's evidence for a fax to a registered-sender recipient: the identity registered then, the kept
    pages, each call with the answering station, and any request for the original. For anyone who may read it."""
    from ..access.http import private_operation
    from ..config_runtime import run_lifecycle_step
    from .sender_pins import evidence
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.queries.job(identity.actor, job_id)))
    store = _store(request)
    folder = _values(request).fax_data_dir
    found = await _call(lambda: evidence(store.engine, job_id, fax_data_dir=folder))
    if found is None:
        raise HTTPException(404, detail='This fax was not sent to a registered-sender recipient.')
    return found


@router.post('/faxes/{job_id}/original', dependencies=[Depends(require_permission('settings:write'))])
async def original_request(job_id: str, payload: OriginalIn, request: Request, identity=Depends(require_identity)):
    """Record that the recipient asked for the original of this fax, that you sent it, or that the request was
    withdrawn. Faxbot sends nothing itself."""
    from .sender_pins import PinError, evidence, record_original
    store = _store(request)
    folder = _values(request).fax_data_dir

    def save():
        try:
            record_original(store.engine, job_id, payload.state, note=payload.note or None,
                            actor=_who(store.engine, identity))
        except PinError as error:
            raise HTTPException(409, detail=str(error)) from None
        return evidence(store.engine, job_id, fax_data_dir=folder)
    return await _call(save)
