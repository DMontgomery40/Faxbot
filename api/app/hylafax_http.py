"""The SSL Fax engine's result route: each finished job, posted by hylafax/bin/notify.

Authenticated with the same internal secret the Asterisk inbound hand-over
uses; the job's tag names the fax and attempt Faxbot created. The same result
posted twice changes nothing (the delivery store keys events by attempt and
outcome).
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Request

from . import hylafax_engine
from .config import settings

router = APIRouter()


def _store(request: Request):
    from .outbound_store import OutboundStore
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return OutboundStore(runtime.manager.store)


@router.post('/_internal/hylafax/result')
def engine_result(request: Request, payload: dict = Body(...),
                  x_internal_secret: Optional[str] = Header(default=None)):
    expected = settings.asterisk_inbound_secret
    if not expected:
        raise HTTPException(401, detail='Internal secret not configured')
    if not hmac.compare_digest((x_internal_secret or '').encode(), expected.encode()):
        raise HTTPException(401, detail='Invalid internal secret')
    identity = hylafax_engine.parse_tag(payload.get('tag'))
    if identity is None:
        raise HTTPException(400, detail='Unknown fax engine job')
    job_id, attempt_id = identity
    status, sentence, category = hylafax_engine.result_outcome(payload)
    from .config_store import ConfigurationStoreError
    from .outbound_store import DeliveryConflict
    store = _store(request)
    try:
        _, profile = store.attempt_context(job_id, attempt_id)
    except (DeliveryConflict, ConfigurationStoreError, LookupError, ValueError):
        raise HTTPException(404, detail='Unknown fax engine job') from None
    if profile.configuration.provider_id != 'sip' or profile.configuration.manifest is not None:
        raise HTTPException(409, detail='The fax engine job does not match the fax.')
    why = payload.get('why') if isinstance(payload.get('why'), str) else ''
    if status == hylafax_engine.UNCERTAIN:
        from .audit import audit_event
        audit_event('native_result_requires_reconciliation', provider='sip', engine='hylafax')
        return {'status': 'uncertain'}
    try:
        store.observe(job_id, attempt_id=attempt_id, profile_id=profile.id, provider_sid=job_id, status=status,
                      event_key=f'{attempt_id}:hylafax:{why[:40]}', error=sentence, error_category=category)
    except DeliveryConflict:
        raise HTTPException(409, detail='The fax engine result does not match the fax.') from None
    return {'status': 'ok'}
