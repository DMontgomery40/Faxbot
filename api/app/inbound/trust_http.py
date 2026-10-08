"""Certificate authorities you trust for forwarded calls (inbound/trust.py), over HTTP.

Every change is an audited configuration change (``STIR_TRUST_ANCHORS``), checked against the configuration the
request read, like the other settings.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from . import trust

router = APIRouter(prefix='/admin/forwarded-trust', tags=['Forwarded calls'])

NOTE = ('A forwarded call is verified only when the carrier signed the forwarding with a certificate from a '
        'certificate authority you trust. In the US these are the STI-CAs the STI-PA approves; it gives its list '
        'to registered service providers, so ask your carrier for it or download it with your STI-PA account.')


def _view(values):
    found = trust.anchors(values)
    return {'anchors': [trust.view(anchor) for anchor in found], 'note': NOTE,
            'sentence': (f'You trust {len(found)} certificate {"authority" if len(found) == 1 else "authorities"} '
                         'for forwarded calls.' if found else
                         'You trust no certificate authority for forwarded calls yet, so no forwarding is verified.')}


@router.get('')
async def list_anchors(request: Request, identity=Depends(require_permission('settings:read'))):
    """The certificate authorities you trust for forwarded calls."""
    return _view(request.scope['faxbot.configuration'].desired.values)


class AnchorsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pem: str | None = Field(default=None, max_length=trust.LIST_BYTES)
    url: str | None = Field(default=None, max_length=2048)


def _save(request, identity, change):
    from ..access.http import runtime as access_runtime
    snapshot = request.scope['faxbot.configuration']
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    try:
        value = change(snapshot.desired.values)
    except trust.TrustRefused as refused:
        raise HTTPException(409, detail=str(refused)) from None
    access = access_runtime(request)
    access.configuration_access.prepare_settings_write(identity.actor, snapshot, snapshot.desired.id)
    runtime.manager.patch_authorized(snapshot, {trust.SETTING: value}, principal=identity.actor,
                                     control=access.control)
    return _view(runtime.manager.store.read().desired.values)


@router.post('')
async def add_anchors(body: AnchorsIn, request: Request, identity=Depends(require_permission('settings:write'))):
    """Trust the certificate authorities in pasted PEM certificates, or in a list at an https:// address you can
    reach (read once, now). Only certificate authorities are kept, each once."""
    return await run_lifecycle_step(lambda: _save(request, identity, lambda values: trust.added(
        values, pem=(body.pem or '').strip() or None, url=(body.url or '').strip() or None)))


@router.delete('/{fingerprint}')
async def remove_anchor(fingerprint: str, request: Request, identity=Depends(require_permission('settings:write'))):
    """Stop trusting one certificate authority, named by the start of its fingerprint."""
    return await run_lifecycle_step(lambda: _save(request, identity, lambda values: trust.removed(values, fingerprint)))
