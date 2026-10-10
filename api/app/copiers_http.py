"""Copiers that fax over the network (``copiers.py``) and copiers as Faxbot's own extensions
(``copier_extensions.py``), for Delivery setup → the trunk page and the command line.

- ``GET /admin/sip/copiers``: which makers document SIP fax with T.38, and your copier extensions.
- ``POST /admin/sip/copiers/extensions``: a new copier extension; its password is shown once.
- ``POST /admin/sip/copiers/extensions/{id}/remove``: take a copier extension away and revoke its sending key.
- ``GET /admin/sip/copiers/received/{inbound_id}``: whether a received fax was sent on to a copier.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .access.http import private_operation, require_identity
from .access.route_policy import require_permission
from .config_runtime import run_lifecycle_step
from . import copier_extensions, copiers


router = APIRouter(prefix='/admin/sip/copiers', tags=['SIP trunk'])
UNAVAILABLE = 'Copier extensions are unavailable right now. Try again in a moment.'


class ExtensionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    extension: str = Field(pattern=r'^[1-9][0-9]{2,5}$')
    mailbox_id: str | None = Field(default=None, max_length=40)
    numbers: list[str] = Field(default_factory=list, max_length=20)
    receive: str = Field(default='copier', pattern='^(copier|intake)$')


def _runtime(request):
    from .routing.background import installation_engine
    engine, runtime = installation_engine(request.app)
    if engine is None or runtime is None:
        raise HTTPException(503, detail=UNAVAILABLE)
    return engine, runtime


def _view(engine, values):
    from .routing.reply_number import mailbox_labels
    labels = mailbox_labels(engine)
    return [{'id': item.id, 'name': item.name, 'extension': item.extension, 'username': item.username,
             'mailbox_id': item.mailbox_id, 'mailbox': labels.get(item.mailbox_id) if item.mailbox_id else None,
             'numbers': list(item.numbers), 'receive': item.receive,
             'sentence': (f"{item.name} sends from extension {item.extension}"
                          f"{' as ' + labels[item.mailbox_id] if item.mailbox_id in labels else ''}; "
                          + (f"faxes to {', '.join(item.numbers)} go on to it."
                             if item.numbers and item.receive == 'copier' else
                             'faxes to its numbers stay in Received.' if item.numbers else
                             'no number sends faxes on to it.'))}
            for item in copier_extensions.copiers(engine)]


@router.get('', dependencies=[Depends(require_permission('providers:read'))])
async def copier_catalog(request: Request):
    """Which copier makers document SIP fax with T.38, with what to set and where it was read, and your copier
    extensions."""
    engine, runtime = _runtime(request)
    values = runtime.manager.store.read().active.values
    extensions = await run_lifecycle_step(lambda: _view(engine, values))
    return {'copiers': copiers.catalog(), 'extensions': extensions}


@router.post('/extensions', status_code=201)
async def add_extension(body: ExtensionIn, request: Request, identity=Depends(require_permission('providers:write'))):
    """A copier's own extension and sending key. The password is in this answer only; enter it on the copier."""
    from .intake.sources import keys
    from .intake.sources.http import _require_key_right, _require_mailbox
    from .routing.numbers import InvalidNumber, normalize_number
    engine, runtime = _runtime(request)
    access = request.app.state.access_runtime
    values = runtime.manager.store.read().active.values
    await run_lifecycle_step(lambda: _require_key_right(access, identity.actor))

    def create():
        _require_mailbox(access, identity.actor, body.mailbox_id)
        numbers = []
        for number in body.numbers:
            try:
                numbers.append(normalize_number(number, country=values.fax_default_country))
            except (InvalidNumber, ValueError):
                raise copier_extensions.CopierError(f"{number} isn't a fax number Faxbot can read.") from None
        if any(item.extension == body.extension for item in copier_extensions.copiers(engine)):
            raise copier_extensions.CopierError(f'Extension {body.extension} is already a copier.')
        token, _, binding_id, principal_id = keys.issue(access, identity.actor, f'Copier {body.name.strip()}')
        try:
            copier, password = copier_extensions.add(
                engine, name=body.name, extension=body.extension, mailbox_id=body.mailbox_id, numbers=numbers,
                receive=body.receive, actor={'name': getattr(identity.actor, 'display_name', None)},
                key=(token, binding_id, principal_id), seal=copier_extensions.CopierSecrets(runtime.manager.store))
        except BaseException:
            keys.revoke(access, identity.actor, binding_id)
            raise
        return copier, password
    try:
        copier, password = await run_lifecycle_step(create)
    except copier_extensions.CopierError as error:
        raise HTTPException(400, detail=str(error)) from None
    view = next(item for item in _view(engine, values) if item['id'] == copier.id)
    return {**view, 'password': password, 'realm': copier_extensions.REALM,
            'password_text': 'Enter this password on the copier now; Faxbot shows it only once and keeps only its '
                             'digest.',
            'apply_text': 'Press Apply on the trunk page so the fax engine loads the extension; the copier can '
                          'register after that.'}


@router.post('/extensions/{copier_id}/remove')
async def remove_extension(copier_id: str, request: Request, identity=Depends(require_permission('providers:write'))):
    from .intake.sources import keys
    engine, runtime = _runtime(request)
    access = request.app.state.access_runtime

    def remove():
        found = copier_extensions.remove(engine, copier_id, actor={'name': getattr(identity.actor, 'display_name',
                                                                                    None)})
        keys.revoke(access, identity.actor, found.key_binding_id)
        return found
    try:
        found = await run_lifecycle_step(remove)
    except copier_extensions.CopierError as error:
        raise HTTPException(404, detail=str(error)) from None
    return {'id': found.id, 'detail': f'{found.name} is no longer an extension. Press Apply on the trunk page so the '
                                      'fax engine forgets it.'}


@router.get('/received/{inbound_id}')
async def received_view(inbound_id: str, request: Request, identity=Depends(require_identity)):
    """Whether a received fax was sent on to a copier, for anyone who may read that fax."""
    runtime = request.app.state.access_runtime
    await run_lifecycle_step(private_operation(lambda: runtime.inbound_queries.item(identity.actor, inbound_id)))
    engine, _ = _runtime(request)
    return {'sentence': await run_lifecycle_step(lambda: copier_extensions.fax_view(engine, inbound_id))}


__all__ = ['router']
