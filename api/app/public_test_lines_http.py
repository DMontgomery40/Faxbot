"""Public test lines over HTTP (``public_test_lines.py``): Administration → System health → Public test lines.

- ``GET /diagnostics/test-lines``: the lines with their sources, what the dialing guard says about each, whether a
  reply would reach this Faxbot, and the recent test faxes with their results.
- ``POST /diagnostics/test-lines/{line}/send``: one test fax, at your request. A line the dialing guard does not
  let Faxbot dial is not sent: the answer names the class to allow (``PUT /routing/dialing/{class}``).
- ``GET /diagnostics/test-lines/sends/{send}``: one test fax's result.
- ``GET /diagnostics/test-lines/sends/{send}/receipt``: Faxbeep's public page for it, looked up now.
- ``POST /diagnostics/test-lines/sends/{send}/reply``: mark a received fax as the test's reply.
- ``GET /diagnostics/test-lines/replies``: received faxes labelled as a test reply, for Received.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
import sqlalchemy as sa

from .access.route_policy import require_permission
from .config_runtime import run_lifecycle_step
from . import public_test_lines


router = APIRouter(prefix='/diagnostics/test-lines', tags=['Diagnostics'])
UNAVAILABLE = 'Public test lines are unavailable right now. Try again in a moment.'


def _engine(request):
    from .routing.background import installation_engine
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime


def _actor(engine, identity):
    principal = getattr(getattr(identity, 'actor', None), 'principal_id', None)
    if not principal:
        return None, None
    principals = sa.table('access_principals', sa.column('id'), sa.column('display_name'))
    with engine.connect() as connection:
        return principal, connection.execute(sa.select(principals.c.display_name).where(
            principals.c.id == principal)).scalar_one_or_none()


def _refused(call):
    try:
        return call()
    except public_test_lines.TestLineError as error:
        raise HTTPException(400, detail=str(error)) from None


@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def get_test_lines(request: Request):
    engine, _ = _engine(request)
    values = request.scope['faxbot.configuration'].active.values
    return await run_lifecycle_step(lambda: public_test_lines.view(engine, values))


@router.get('/replies', dependencies=[Depends(require_permission('settings:read'))])
async def get_replies(request: Request):
    engine, _ = _engine(request)
    return {'replies': await run_lifecycle_step(lambda: public_test_lines.replies(engine))}


@router.post('/{line_id}/send')
async def send_test_fax(line_id: str, request: Request, identity=Depends(require_permission('settings:write'))):
    """Send one test fax to a public test line: your explicit action (you also need to be allowed to send faxes).
    Faxbot never sends one by itself."""
    from .access.http import runtime as access_runtime
    from .routing.submit import GeneratedFaxBusy, accept_generated_fax
    engine, runtime = _engine(request)
    revision = request.scope['faxbot.configuration'].active
    values = revision.values
    line = _refused(lambda: public_test_lines.line_for(line_id))
    if revision.profile_id('outbound') is None:
        raise HTTPException(409, detail='Sending is turned off in this configuration, so no test fax can go.')
    access = access_runtime(request)

    def send():
        now = datetime.utcnow()
        home = getattr(values, 'fax_default_country', 'US') or 'US'
        with engine.begin() as connection:
            guard_view = public_test_lines.guard_state(connection, line, home, now)
        if not guard_view['allowed']:
            # Nothing is sent: the guard's own sentence, and the class you may allow (never bypassed).
            return {'sent': False, 'needs_allow': guard_view, 'sentence': guard_view['sentence']}
        reply = public_test_lines.reply_check(values, engine)
        actor, name = _actor(engine, identity)
        holder = []
        document = public_test_lines.test_page(line, public_test_lines._when(now) or '')
        try:
            accept_generated_fax(runtime, access, identity.actor, revision, to_number=line.number, document=document,
                                 file_name=f'test-line-{line.id}.pdf', pages=1,
                                 after=public_test_lines.record_step(line, reply_number=reply['caller_id'],
                                                              actor_principal_id=actor, actor_name=name,
                                                              holder=holder))
        except GeneratedFaxBusy as error:
            raise HTTPException(409, detail=str(error)) from None
        except RuntimeError as error:
            raise HTTPException(409, detail=str(error)) from None
        result = public_test_lines.one_view(engine, holder[0]) if holder else None
        sentence = f'The test fax to {line.operator} is on its way. Its result shows here when the call ends.'
        if result and result['fax_state'] == 'held':
            sentence = result['fax_sentence']
        return {'sent': True, 'send': result, 'sentence': sentence}
    return await run_lifecycle_step(send)


@router.get('/sends/{send_id}', dependencies=[Depends(require_permission('settings:read'))])
async def get_send(send_id: str, request: Request):
    engine, _ = _engine(request)
    return await run_lifecycle_step(lambda: _refused(lambda: public_test_lines.one_view(engine, send_id)))


@router.get('/sends/{send_id}/receipt', dependencies=[Depends(require_permission('settings:read'))])
async def get_receipt(send_id: str, request: Request):
    """Faxbeep's public page for this test fax, looked up in Faxbeep's public API now (nothing is sent there)."""
    engine, _ = _engine(request)

    def look():
        row = _refused(lambda: public_test_lines.send_row(engine, send_id))
        line = public_test_lines.BY_ID.get(row['line_id'])
        if line is None or line.receipt != 'faxbeep':
            raise HTTPException(400, detail='Only Faxbeep lists each fax it receives in a way Faxbot can look up.')
        with engine.connect() as connection:
            ended = public_test_lines.call_ended_at(connection, row['job_id'])
        return public_test_lines.faxbeep_receipt(row, ended_at=ended)
    return await run_lifecycle_step(look)


class ReplyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    inbound_id: str = Field(min_length=1, max_length=40)


@router.post('/sends/{send_id}/reply')
async def mark_reply(send_id: str, payload: ReplyIn, request: Request,
                     identity=Depends(require_permission('settings:write'))):
    engine, _ = _engine(request)

    def mark():
        actor, name = _actor(engine, identity)
        return public_test_lines.confirm_reply(engine, send_id, payload.inbound_id, actor_principal_id=actor,
                                        actor_name=name)
    result = await run_lifecycle_step(lambda: _refused(mark))
    from .audit import audit_event
    audit_event('test_line_reply_marked', send_id=send_id, inbound_id=payload.inbound_id)
    return result
