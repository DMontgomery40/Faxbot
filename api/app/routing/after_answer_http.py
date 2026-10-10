"""Digits after answer over HTTP (``after_answer.py``): Recipients details and Sent details.

- ``GET``/``PUT /routing/after-answer/{number}``: the keys Faxbot presses once the number answers, before the fax
  starts; ``{"digits": null}`` (or an empty string) presses none.
- ``GET /routing/after-answer/faxes/{fax}``: what Sent details say about the keys each of a fax's calls pressed.

The router's background work keeps, for each placed call, the keys it pressed (both engines' Submission events).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import private_operation, require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import after_answer
from .background import installation_engine, lifespan_tasks


UNAVAILABLE = 'Digits after answer are unavailable right now. Try again in a moment.'


def _background(app):
    """Keep the keys each placed call pressed; nothing runs on a schedule."""
    from ..ami import ami_client

    def submitted(event):
        if not isinstance(event, dict) or not event.get('Digits'):
            return
        engine, _ = installation_engine(app)
        if engine is None:
            return

        def record():
            import sqlalchemy as sa
            try:
                after_answer.record_submission(engine, event)
            except sa.exc.SQLAlchemyError as error:
                logging.getLogger(__name__).warning('The keys a fax call pressed could not be recorded (%s).',
                                                    type(error).__name__)
        try:
            asyncio.get_running_loop().run_in_executor(None, record)
        except RuntimeError:
            record()
    ami_client.on_submission(submitted)
    return []


router = APIRouter(prefix='/routing/after-answer', tags=['Delivery routes'], lifespan=lifespan_tasks(_background))


class AfterAnswerChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # Keys 0-9, * and #, with w or W (or a comma) for a pause; null or empty presses none.
    digits: str | None = Field(default=None, max_length=64)


def _runtime(request):
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime.manager.store


def _number(value, request):
    from .numbers import InvalidNumber, normalize_number
    country = getattr(request.scope['faxbot.configuration'].active.values, 'fax_default_country', 'US')
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def _actor(engine, identity):
    from .stations_http import _actor as actor
    return actor(engine, identity)


@router.get('/faxes/{job_id}')
async def fax_keys(job_id: str, request: Request, identity=Depends(require_identity)):
    """What Sent details say about the keys a fax's calls pressed, for anyone who may read that fax."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.queries.job(identity.actor, job_id)))
    engine, _ = _runtime(request)
    return {'sentences': await run_lifecycle_step(lambda: after_answer.fax_sentences(engine, job_id))}


@router.get('/{number}', dependencies=[Depends(require_permission('settings:read'))])
async def get_keys(number: str, request: Request):
    target = _number(number, request)
    engine, _ = _runtime(request)
    return await run_lifecycle_step(lambda: after_answer.recipient_view(engine, target))


@router.put('/{number}')
async def put_keys(number: str, payload: AfterAnswerChange, request: Request,
                   identity=Depends(require_permission('settings:write'))):
    target = _number(number, request)
    engine, configuration = _runtime(request)
    try:
        keys = after_answer.clean(payload.digits)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from None

    def save():
        principal, name = _actor(engine, identity)
        with configuration._locked() as connection:
            after_answer.set_on(connection, target, keys, actor_principal_id=principal, actor_name=name,
                                now=datetime.utcnow())
        return after_answer.recipient_view(engine, target)
    view = await run_lifecycle_step(save)
    from ..audit import audit_event
    audit_event('after_answer_changed', number=target, digits=keys or '')
    return {**view, 'saved': 'Saved. ' + after_answer.change_sentence(keys)}
