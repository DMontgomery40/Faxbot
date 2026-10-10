"""Delivery setup → Sending identity: the reply number, per organization and per mailbox (routing/reply_number.py).

Reads need ``settings:read``; changes need ``settings:write`` and are saved as
ordinary settings (``fax_reply_number``, ``fax_reply_numbers``) through the
same authorized, audited configuration write as the settings page, after the
number passes the ownership, receiving and same-mailbox checks.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity, runtime as access_runtime
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import reply_number
from .background import installation_engine
from .costs import money_text
from .database import DeliveryStoreError
from .store import RouteStore


router = APIRouter(prefix='/numbers/reply', tags=['Reply number'])


class ReplyNumberBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # Empty: Faxbot chooses (the cheapest of your numbers that receives into a mailbox).
    number: str = Field(default='', max_length=40)


def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _candidate_view(candidate):
    return {'number': candidate.number, 'provider': candidate.provider_name, 'kind': candidate.kind,
            'receives': candidate.receives, 'mailbox': candidate.mailbox, 'mailbox_id': candidate.mailbox_id,
            'price': candidate.price, 'avoid': candidate.avoid,
            'typical_cost': (money_text(candidate.cost_micros, candidate.currency)
                             if candidate.cost_micros is not None and candidate.currency else None)}


def reply_view(values, engine):
    """Everything Delivery setup → Sending identity shows about reply numbers; no secrets, no internal ids beyond mailboxes."""
    try:
        store = RouteStore(engine)
    except DeliveryStoreError:
        store = None
    routes = reply_number.mailbox_routes(engine, values)
    labels = reply_number.mailbox_labels(engine)
    found = reply_number.candidates(values, store=store, routes=routes)
    choice = reply_number.choose(values, store=store, routes=routes)
    advice = reply_number.advice(found)
    suggestion = advice['suggest']
    mailboxes = []
    for mailbox_id, number in sorted(reply_number.mailbox_numbers(values).items(),
                                     key=lambda item: labels.get(item[0], '')):
        problem = reply_number.refusal(values, number, routes=routes, mailbox_id=mailbox_id, labels=labels)
        mailboxes.append({'mailbox_id': mailbox_id, 'mailbox': labels.get(mailbox_id, 'A removed mailbox'),
                          'number': number, 'problem': problem})
    return {
        'number': getattr(values, reply_number.SETTING, '') or None,
        'shows': choice.number, 'source': choice.source, 'sentence': choice.sentence,
        'problems': list(dict.fromkeys(choice.notes)),
        'mailboxes': mailboxes,
        'candidates': [_candidate_view(candidate) for candidate in found],
        'suggestion': None if suggestion is None else {
            **_candidate_view(suggestion),
            'sentence': f'{suggestion.number} is your cheapest number to receive on that reaches a mailbox: '
                        f'{suggestion.price[0].lower()}{suggestion.price[1:]}'},
        'avoid': [{'number': candidate.number, 'sentence': sentence} for candidate, sentence in advice['avoid']],
        'caller_id': reply_number.caller_id(values, choice.number),
        'header_problem': reply_number.header_problem(values),
    }


@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def read_reply_number(request: Request):
    values = request.scope['faxbot.configuration'].active.values
    engine = _engine(request)
    try:
        return await run_lifecycle_step(lambda: reply_view(values, engine))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Faxbot could not read your numbers just now. Try again in a minute.') from None


def _save(request, identity, changes):
    _, runtime = installation_engine(request.app)
    if runtime is None or not getattr(runtime, 'serving', False):
        raise HTTPException(503, detail='Installation configuration is not ready.')
    manager = runtime.manager
    expected = request.scope['faxbot.configuration']
    access = access_runtime(request)
    access.configuration_access.prepare_settings_write(identity.actor, expected, expected.desired.id)
    manager.patch_authorized(expected, changes, principal=identity.actor, control=access.control)


def _checked(request, text, *, mailbox_id=None):
    values = request.scope['faxbot.configuration'].desired.values
    try:
        return values, reply_number.check(values, text, engine=_engine(request), mailbox_id=mailbox_id)
    except reply_number.ReplyNumberRefused as refused:
        raise HTTPException(400, detail=str(refused)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Faxbot could not read your numbers just now. Try again in a minute.') from None


@router.put('', dependencies=[Depends(require_permission('settings:write'))])
async def set_reply_number(body: ReplyNumberBody, request: Request, identity=Depends(require_identity)):
    """Save the organization's reply number; an empty number lets Faxbot choose."""
    def run():
        number = ''
        if body.number.strip():
            _, number = _checked(request, body.number)
        _save(request, identity, {reply_number.SETTING: number})
        return {'ok': True, 'number': number or None}
    return await run_lifecycle_step(run)


@router.put('/mailboxes/{mailbox_id}', dependencies=[Depends(require_permission('settings:write'))])
async def set_mailbox_reply_number(mailbox_id: str, body: ReplyNumberBody, request: Request,
                                   identity=Depends(require_identity)):
    """Give one mailbox a reply number of its own; it must be a number whose faxes reach that mailbox."""
    if not reply_number.MAILBOX_ID.fullmatch(mailbox_id):
        raise HTTPException(404, detail='That mailbox no longer exists.')

    def run():
        values, number = _checked(request, body.number, mailbox_id=mailbox_id)
        numbers = reply_number.mailbox_numbers(values)
        numbers[mailbox_id] = number
        _save(request, identity, {reply_number.MAILBOX_SETTING: reply_number.encode_mailbox_numbers(numbers)})
        return {'ok': True, 'mailbox_id': mailbox_id, 'number': number}
    return await run_lifecycle_step(run)


@router.delete('/mailboxes/{mailbox_id}', dependencies=[Depends(require_permission('settings:write'))])
async def clear_mailbox_reply_number(mailbox_id: str, request: Request, identity=Depends(require_identity)):
    """The mailbox's faxes show the organization's reply number again."""
    def run():
        values = request.scope['faxbot.configuration'].desired.values
        numbers = reply_number.mailbox_numbers(values)
        if numbers.pop(mailbox_id, None) is not None:
            _save(request, identity, {reply_number.MAILBOX_SETTING: reply_number.encode_mailbox_numbers(numbers)})
        return {'ok': True, 'mailbox_id': mailbox_id, 'number': None}
    return await run_lifecycle_step(run)
