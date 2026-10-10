"""The header notice over HTTP (``header_notice.py``): Numbers → Sender identity, Send a fax, Sent and Recipients.

- ``GET /header-notice`` and ``PUT /header-notice``: the organization's notice;
  ``PUT /header-notice/mailboxes/{mailbox}``: one mailbox's (empty removes it).
- ``GET /header-notice/for-send``: the notice a fax from a mailbox would carry,
  for Send a fax's "the first page is a cover sheet" choice.
- ``GET /header-notice/faxes/{fax}``: what Sent details say about a fax's notice and cover.
- ``GET`` and ``PUT /header-notice/recipients/{number}``: whether a recipient needs a cover sheet.

Every change is a new row; the newest counts.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
import sqlalchemy as sa

from .access.http import private_operation, require_identity
from .access.route_policy import require_permission
from .config_runtime import run_lifecycle_step
from . import header_notice


router = APIRouter(prefix='/header-notice', tags=['Header notice'])
UNAVAILABLE = 'Header notices are unavailable right now. Try again in a moment.'


class NoticeIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # One line printed at the top of every page; empty or None removes it.
    notice: str | None = Field(default=None, max_length=400)


class CoverIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    needs_cover: StrictBool


def _runtime(request):
    from .routing.background import installation_engine
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime.manager.store


def _labels(engine):
    from .routing.reply_number import mailbox_labels
    return mailbox_labels(engine)


def _actor(engine, identity):
    principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)
    if not principal:
        return None, None
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with engine.connect() as connection:
        name = connection.execute(sa.select(principals.c.display_name).where(
            principals.c.id == principal)).scalar_one_or_none()
    return principal, name


def _number(value, request):
    from .routing.numbers import InvalidNumber, normalize_number
    country = getattr(request.scope['faxbot.configuration'].active.values, 'fax_default_country', 'US')
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _view(engine):
    with engine.connect() as connection:
        return header_notice.settings_view(connection, _labels(engine))


@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def get_notices(request: Request):
    engine, _ = _runtime(request)
    return await run_lifecycle_step(lambda: _view(engine))


async def _save(request, identity, mailbox_id, payload):
    engine, configuration = _runtime(request)

    def save():
        if mailbox_id is not None and mailbox_id not in _labels(engine):
            raise header_notice.NoticeRefused('There is no such mailbox. Choose one of your mailboxes.')
        principal, name = _actor(engine, identity)
        with configuration._locked() as connection:
            notice = header_notice.set_notice_on(connection, mailbox_id=mailbox_id, text=payload.notice,
                                                 actor_principal_id=principal, actor_name=name,
                                                 now=datetime.utcnow())
        return notice, _view(engine)
    try:
        notice, view = await run_lifecycle_step(save)
    except header_notice.NoticeRefused as error:
        raise HTTPException(400, detail=str(error)) from None
    from .audit import audit_event
    audit_event('header_notice_changed', scope='mailbox' if mailbox_id else 'organization', mailbox_id=mailbox_id,
                removed=notice is None)
    whose = 'this mailbox’s faxes' if mailbox_id else 'your faxes'
    sentence = (f'Saved. Every page of {whose} carries this notice from now on.' if notice
                else 'Removed. New faxes carry no header notice of their own.' if mailbox_id is None
                else 'Removed. This mailbox’s faxes carry the organization’s notice, if you set one.')
    return {**view, 'sentence': sentence}


@router.put('')
async def put_notice(payload: NoticeIn, request: Request, identity=Depends(require_permission('settings:write'))):
    return await _save(request, identity, None, payload)


@router.put('/mailboxes/{mailbox_id}')
async def put_mailbox_notice(mailbox_id: str, payload: NoticeIn, request: Request,
                             identity=Depends(require_permission('settings:write'))):
    return await _save(request, identity, mailbox_id[:40], payload)


@router.get('/for-send', dependencies=[Depends(require_permission('fax:send', resource='personal'))])
async def notice_for_send(request: Request, mailbox: str | None = Query(default=None, max_length=100)):
    """The notice a fax sent now (from ``mailbox``) would carry, so Send a fax can offer the cover choice."""
    engine, _ = _runtime(request)

    def read():
        with engine.connect() as connection:
            found = header_notice.notice_for_on(connection, (mailbox or '').strip() or None)
        return {'notice': found[0] if found else None, 'whose': found[1] if found else None}
    return await run_lifecycle_step(read)


@router.get('/faxes/{job_id}')
async def fax_notice(job_id: str, request: Request, identity=Depends(require_identity)):
    """What Sent details say about a fax's header notice and cover, for anyone who may read that fax."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.queries.job(identity.actor, job_id)))
    engine, _ = _runtime(request)
    found = await run_lifecycle_step(lambda: header_notice.fax_view(engine, job_id))
    return found or {'notice': None, 'cover': None, 'sentence': None}


@router.get('/recipients/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_recipient_cover(number: str, request: Request):
    target = _number(number, request)
    engine, _ = _runtime(request)

    def read():
        with engine.connect() as connection:
            return header_notice.needs_cover_on(connection, target)
    return {'number': target, 'needs_cover': await run_lifecycle_step(read)}


@router.put('/recipients/{number}')
async def put_recipient_cover(number: str, payload: CoverIn, request: Request,
                              identity=Depends(require_permission('settings:write'))):
    target = _number(number, request)
    engine, configuration = _runtime(request)

    def save():
        principal, name = _actor(engine, identity)
        with configuration._locked() as connection:
            header_notice.set_needs_cover_on(connection, target, payload.needs_cover, actor_principal_id=principal,
                                             actor_name=name, now=datetime.utcnow())
    await run_lifecycle_step(save)
    from .audit import audit_event
    audit_event('recipient_cover_changed', number=target, needs_cover=payload.needs_cover)
    sentence = ('Saved. Faxes to this number always keep their cover sheet.' if payload.needs_cover
                else 'Saved. Senders may send a cover sheet’s notice in the header to this number.')
    return {'number': target, 'needs_cover': payload.needs_cover, 'sentence': sentence}
