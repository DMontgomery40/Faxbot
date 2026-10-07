"""Registered forms: import, versions, previews, sending, and the partner protocol.

Operator routes: reading forms needs settings:read and importing them
settings:write. Filling, previewing filled pages and sending need fax:send,
like ``POST /fax``. The values of received forms are the content of a
received fax, so they need mailboxes:read, as the list of documents partners
delivered directly does.

Partner routes (``/forms/partner/…``) carry no API key: each request is
signed by an enrolled direct delivery partner, and they answer 404 while
direct delivery is switched off.
"""
import json

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..direct.crypto import DirectProtocolError
from ..direct.http import service_for as direct_service_for
from ..direct.identity import IdentityUnavailable
from ..direct.service import DirectUnavailable
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError
from ..routing.numbers import InvalidNumber, normalize_number
from . import model, renderer
from .exchange import STATE_TEXT, FormExchange
from .importer import MAX_TEMPLATE_BYTES, from_template
from .store import FormConflict


SOURCE_TEXT = {
    'pdf_acroform': 'Imported from a fillable PDF.',
    'pdf_positions': 'Imported from a PDF with a field-position file.',
    'svg_positions': 'Imported from an SVG drawing with a field-position file.',
    'partner': 'Received from a partner.',
}
TYPE_TEXT = {'text': 'Text', 'date': 'Date', 'checkbox': 'Checkbox', 'choice': 'Choice', 'number': 'Number',
             'signature': 'Signature picture'}


def exchange_for(app):
    try:
        return FormExchange(direct_service_for(app))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Registered forms are unavailable.') from None


def _background(app):
    engine, _ = installation_engine(app)
    if engine is None:
        return []
    exchange = exchange_for(app)
    return [('faxbot-forms-reconcile', repeat_async(exchange.step, interval=60.0, initial_delay=25.0,
                                                    warning='Confirmations of forms sent to partners are '
                                                            'temporarily unavailable.'))]


router = APIRouter(prefix='/forms', tags=['Registered forms'], lifespan=lifespan_tasks(_background))


def _problems(problems):
    """Every field problem in one short paragraph (at most 300 characters)."""
    shown = []
    for index, problem in enumerate(problems):
        rest = len(problems) - index
        tail = f' {rest} more field{"s need" if rest != 1 else " needs"} attention.'
        if len(' '.join(shown + [problem])) + (len(tail) if rest > 1 else 0) > 300:
            return ' '.join(shown) + tail
        shown.append(problem)
    return ' '.join(shown)


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except model.FormValueError as error:
        raise HTTPException(422, detail=_problems(error.problems)) from None
    except (model.FormError, FormConflict) as error:
        raise HTTPException(409, detail=str(error)) from None
    except renderer.RendererUnavailable as error:
        raise HTTPException(503, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Registered forms are unavailable.') from None


def _field_view(item):
    view = {'name': item['name'], 'label': item['label'], 'type': item['type'], 'type_text': TYPE_TEXT[item['type']],
            'page': item['page'], 'required': item['required']}
    for key in ('options', 'format', 'decimals', 'multiline', 'max_length'):
        if key in item:
            view[key] = item[key]
    return view


def _version_view(row):
    return {'id': row['id'], 'number': row['number'], 'title': row['title'], 'address': row['address'],
            'source': row['source'], 'source_text': SOURCE_TEXT[row['source']], 'pages': row['page_count'],
            'fields': row['field_count'], 'created_at': row['created_at']}


def _form_view(form):
    versions = [_version_view(version) for version in form['versions']]
    return {'id': form['id'], 'name': form['name'], 'origin': form['origin'], 'versions': versions,
            'latest': versions[-1] if versions else None}


def _version_detail(version):
    return {**_version_view(version.row), 'form_id': version.form['id'], 'form_name': version.form['name'],
            'page_sizes': [{'width': page['width'], 'height': page['height']} for page in version.content['pages']],
            'field_list': [_field_view(item) for item in version.content['fields']],
            'has_template': version.row['template'] is not None, 'renderer': renderer.RENDERER}


def _delivery_view(row, *, values=False, titles=None):
    title = (titles or {}).get(row['form_version_id'])
    view = {'id': row['id'], 'direction': row['direction'], 'route': row['route'], 'state': row['state'],
            'status': STATE_TEXT[row['state']], 'detail': row['detail'], 'partner': row['partner'],
            'fax_number': row['fax_number'], 'pages': row['pages'], 'form_version_id': row['form_version_id'],
            'form': title[0] if title else None, 'form_version': title[1] if title else None,
            'fax_id': row['fax_job_id'] if row['fax_job_id'] and not row['fax_job_id'].startswith('claim-') else None,
            'can_fax': (row['direction'] == 'outbound' and row['fax_job_id'] is None
                        and row['state'] in ('mismatch', 'refused', 'not_sent', 'not_received')),
            'created_at': row['created_at'], 'updated_at': row['updated_at']}
    if values:
        view['values'] = json.loads(row['field_values']) if row['field_values'] else None
    return view


def _titles(store):
    return {version['id']: (form['name'], version['number']) for form in store.list_forms()
            for version in form['versions']}


async def _upload(file, limit, what):
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, detail=f'The {what} is too large.')
    return data


# Forms ----------------------------------------------------------------------------------------
@router.get('', dependencies=[Depends(require_permission('settings:read'))])
async def list_forms(request: Request):
    exchange = exchange_for(request.app)
    forms = await _call(exchange.store.list_forms)
    return {'forms': [_form_view(form) for form in forms], 'renderer': renderer.RENDERER}


async def _import(request, *, file, positions, name=None, form_id=None, identity=None):
    exchange = exchange_for(request.app)
    template = await _upload(file, MAX_TEMPLATE_BYTES, 'template')
    placed = await _upload(positions, 1024 * 1024, 'field-position file') if positions is not None else None
    imported = await _call(lambda: from_template(template, file_name=file.filename or '', positions=placed))
    version, created = await _call(lambda: exchange.store.add_version(
        imported, name=name, form_id=form_id, created_by=getattr(identity.actor, 'principal_id', None)))
    detail = _version_detail(version)
    message = (f'Imported {version.form["name"]} version {version.number}.' if created
               else f'This form is already registered as {version.form["name"]} version {version.number}.')
    return {'created': created, 'message': message, 'version': detail}


@router.post('', status_code=201)
async def import_form(request: Request, name: str = Form(..., max_length=200), file: UploadFile = File(...),
                      positions: UploadFile | None = File(default=None),
                      identity=Depends(require_permission('settings:write'))):
    """Import a new form from a fillable PDF, or from a PDF or SVG template with a field-position file."""
    return await _import(request, file=file, positions=positions, name=name, identity=identity)


@router.get('/received', dependencies=[Depends(require_permission('mailboxes:read'))])
async def received_forms(request: Request):
    """Forms partners delivered whose pages matched, with their values, for the Received screen."""
    exchange = exchange_for(request.app)

    def read():
        rows = exchange.store.received()
        return rows, _titles(exchange.store)
    rows, titles = await _call(read)
    return {'received': [{**_delivery_view(row, values=True, titles=titles),
                          'message_id': row['message_id'], 'intake_item_id': row['intake_item_id'],
                          'inbound_fax_id': row['inbound_fax_id'],
                          'fields': _labels(exchange, row['form_version_id'])} for row in rows]}


def _labels(exchange, version_id):
    version = exchange.store.version(version_id=version_id) if version_id else None
    return [{'name': item['name'], 'label': item['label'], 'type': item['type']}
            for item in version.content['fields']] if version else []


@router.get('/deliveries', dependencies=[Depends(require_permission('settings:read'))])
async def list_deliveries(request: Request, direction: str | None = Query(default=None, pattern='^(outbound|inbound)$')):
    exchange = exchange_for(request.app)

    def read():
        return exchange.store.recent(direction=direction), _titles(exchange.store)
    rows, titles = await _call(read)
    return {'deliveries': [_delivery_view(row, titles=titles) for row in rows]}


@router.get('/deliveries/{delivery_id}', dependencies=[Depends(require_permission('settings:read'))])
async def get_delivery(delivery_id: str, request: Request):
    exchange = exchange_for(request.app)
    row = await _call(lambda: exchange.store.delivery(delivery_id))
    if row is None:
        raise HTTPException(404, detail='This form delivery does not exist.')
    titles = await _call(lambda: _titles(exchange.store))
    view = _delivery_view(row, values=row['direction'] == 'outbound', titles=titles)
    view['fields'] = await _call(lambda: _labels(exchange, row['form_version_id']))
    return view


@router.get('/partners/{peer_id}', dependencies=[Depends(require_permission('settings:read'))])
async def partner_forms(peer_id: str, request: Request):
    """Ask a partner, signed, which form versions it holds."""
    exchange = exchange_for(request.app)
    peer = await _call(lambda: exchange.direct.store.get_peer(peer_id))
    if peer is None or peer['state'] == 'revoked':
        raise HTTPException(404, detail='This partner is not enrolled.')
    try:
        held = await exchange.partner_holdings(peer)
    except IdentityUnavailable:
        held = None
    if held is None:
        return {'reached': False, 'forms': [], 'message': f'Faxbot could not ask {peer["organization"]} right now.'}
    own = {item['address'] for item in await _call(exchange.store.addresses)}
    return {'reached': True, 'forms': [{'address': item['address'], 'title': str(item.get('title') or '')[:200],
                                        'version': item.get('version') if type(item.get('version')) is int else None,
                                        'also_here': item['address'] in own} for item in held],
            'message': f'{peer["organization"]} holds {len(held)} form version{"" if len(held) == 1 else "s"}.'}


@router.get('/versions/{version_id}', dependencies=[Depends(require_permission('settings:read'))])
async def get_version(version_id: str, request: Request):
    exchange = exchange_for(request.app)
    version = await _call(lambda: exchange.store.version(version_id=version_id))
    if version is None:
        raise HTTPException(404, detail='This form version does not exist.')
    return _version_detail(version)


@router.get('/versions/{version_id}/template', dependencies=[Depends(require_permission('settings:read'))])
async def get_template(version_id: str, request: Request):
    exchange = exchange_for(request.app)
    found = await _call(lambda: exchange.store.template(version_id))
    if found is None:
        raise HTTPException(404, detail='This form version keeps no imported file.')
    data, media_type = found
    return Response(content=data, media_type=media_type or 'application/octet-stream')


@router.get('/versions/{version_id}/pages/{page}', dependencies=[Depends(require_permission('settings:read'))])
async def preview_page(version_id: str, page: int, request: Request, fields: bool = Query(default=False)):
    """The blank page as it is faxed, optionally with each field's box outlined (preview only)."""
    exchange = exchange_for(request.app)
    version = await _call(lambda: exchange.store.version(version_id=version_id))
    if version is None or not 1 <= page <= len(version.content['pages']):
        raise HTTPException(404, detail='This page does not exist.')

    def draw():
        bitmap = version.backgrounds[page - 1].copy()
        if fields:
            for item in version.content['fields']:
                if item['page'] == page:
                    bitmap.frame(*item['box'], thickness=2)
                    for box in item.get('option_boxes', {}).values():
                        bitmap.frame(*box, thickness=2)
        return renderer.to_png(bitmap)
    return Response(content=await _call(draw), media_type='image/png')


@router.post('/{form_id}/versions', status_code=201)
async def import_version(form_id: str, request: Request, file: UploadFile = File(...),
                         positions: UploadFile | None = File(default=None),
                         identity=Depends(require_permission('settings:write'))):
    """Import a changed template or fields as the form's next version; earlier versions never change."""
    return await _import(request, file=file, positions=positions, form_id=form_id, identity=identity)


class FillIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    values: dict = Field(default_factory=dict)
    resolution: str = Field(default='fine', pattern='^(fine|standard)$')


@router.post('/versions/{version_id}/render', dependencies=[Depends(require_permission('fax:send', resource='personal'))])
async def render_version(version_id: str, payload: FillIn, request: Request,
                         format: str = Query(default='pdf', pattern='^(pdf|png|json)$'), page: int = Query(default=1)):
    """The filled pages exactly as they would be faxed, with each page's hash."""
    exchange = exchange_for(request.app)
    version = await _call(lambda: exchange.store.version(version_id=version_id))
    if version is None:
        raise HTTPException(404, detail='This form version does not exist.')
    values, rendered = await _call(lambda: exchange.prepare(version, payload.values, payload.resolution))
    headers = {'X-Faxbot-Page-Hashes': ','.join(rendered.hashes)}
    if format == 'json':
        return {'pages': len(rendered.pages), 'page_hashes': list(rendered.hashes), 'renderer': rendered.renderer,
                'resolution': rendered.resolution, 'values': values,
                'render_address': renderer.render_address(version.address, values, renderer=rendered.renderer,
                                                          resolution=rendered.resolution)}
    if format == 'png':
        if not 1 <= page <= len(rendered.pages):
            raise HTTPException(404, detail='This page does not exist.')
        return Response(content=await _call(lambda: renderer.to_png(rendered.pages[page - 1], rendered.resolution)),
                        media_type='image/png', headers=headers)
    return Response(content=await _call(lambda: renderer.to_pdf(rendered)), media_type='application/pdf',
                    headers=headers)


class SendIn(FillIn):
    version_id: str = Field(min_length=1, max_length=40)
    to: str = Field(min_length=3, max_length=40)
    route: str = Field(default='auto', pattern='^(auto|fax)$')


def _fax(request, identity, version, rendered, number):
    """Queue the rendered pages as an ordinary fax; returns the fax id."""
    from ..access.http import runtime as access_runtime
    from ..routing.submit import accept_generated_fax
    _, runtime = installation_engine(request.app)
    revision = request.scope['faxbot.configuration'].active
    if revision.profile_id('outbound') is None:
        raise HTTPException(409, detail='Outbound fax delivery is disabled in this configuration.')
    document = renderer.to_pdf(rendered)
    access = access_runtime(request)
    name = f'{version.form["name"]} v{version.number}.pdf'
    return lambda: accept_generated_fax(runtime, access, identity.actor, revision, to_number=number,
                                        document=document, file_name=name[:200], pages=len(rendered.pages))


@router.post('/send')
async def send_form(payload: SendIn, request: Request,
                    identity=Depends(require_permission('fax:send', resource='personal'))):
    """Send a filled form: to a verified partner as values and page hashes, otherwise as a fax."""
    exchange = exchange_for(request.app)
    country = request.scope['faxbot.configuration'].active.values.fax_default_country
    try:
        number = normalize_number(payload.to, country=country)
    except InvalidNumber:
        raise HTTPException(400, detail='Enter a valid fax number.') from None
    version = await _call(lambda: exchange.store.version(version_id=payload.version_id))
    if version is None:
        raise HTTPException(404, detail='This form version does not exist.')
    actor_id = getattr(identity.actor, 'principal_id', None)
    peer = None if payload.route == 'fax' else await _call(lambda: exchange.partner_for(number))
    resolution = payload.resolution if peer is not None else 'fine'
    values, rendered = await _call(lambda: exchange.prepare(version, payload.values, resolution))
    if peer is not None:
        try:
            row = await exchange.send_direct(version, values, rendered, peer, actor_id=actor_id, actor_name=None)
        except (IdentityUnavailable, DirectUnavailable):
            raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    else:
        queue = _fax(request, identity, version, rendered, number)

        def accept():
            job_id = queue()
            return exchange.record_fax(version, values, rendered, number=number, job_id=job_id, actor_id=actor_id,
                                       actor_name=None)
        row = await _call(accept)
    titles = await _call(lambda: _titles(exchange.store))
    return _delivery_view(row, titles=titles)


@router.post('/deliveries/{delivery_id}/fax')
async def fax_delivery(delivery_id: str, request: Request,
                       identity=Depends(require_permission('fax:send', resource='personal'))):
    """A person's decision: send the full pages of a form that did not reach the partner as an ordinary fax."""
    exchange = exchange_for(request.app)
    row = await _call(lambda: exchange.store.delivery(delivery_id))
    if row is None or row['direction'] != 'outbound':
        raise HTTPException(404, detail='This form delivery does not exist.')
    version = await _call(lambda: exchange.store.version(version_id=row['form_version_id']))
    if version is None:
        raise HTTPException(409, detail='This form version is no longer available.')
    values = json.loads(row['field_values'])
    rendered = await _call(lambda: renderer.render(version.content, version.backgrounds, values))
    actor_id = getattr(identity.actor, 'principal_id', None)
    claim = await _call(lambda: exchange.store.claim_fax(delivery_id, decided_by=actor_id))
    if claim is None:
        current = await _call(lambda: exchange.store.delivery(delivery_id))
        if current['fax_job_id'] and not current['fax_job_id'].startswith('claim-'):
            return {**_delivery_view(current, titles=await _call(lambda: _titles(exchange.store))),
                    'message': 'These pages were already sent as a fax.'}
        raise HTTPException(409, detail='This form cannot be sent as a fax from its current state.')
    queue = _fax(request, identity, version, rendered, row['fax_number'])
    try:
        job_id = await _call(queue)
    except BaseException:
        await _call(lambda: exchange.store.release_fax(delivery_id, claim))
        raise
    await _call(lambda: exchange.store.finish_fax(delivery_id, claim, job_id))
    current = await _call(lambda: exchange.store.delivery(delivery_id))
    return {**_delivery_view(current, titles=await _call(lambda: _titles(exchange.store))),
            'message': 'The pages are on their way as a fax.'}


# Partner protocol (signature-authenticated) ---------------------------------------------------
def _partner_call(operation):
    async def run():
        try:
            status, body = await run_lifecycle_step(operation)
        except DirectUnavailable:
            raise HTTPException(404, detail='Not found.') from None
        except (DeliveryStoreError, IdentityUnavailable, DirectProtocolError):
            raise HTTPException(503, detail='Registered forms are unavailable.') from None
        return JSONResponse(body, status_code=status)
    return run()


@router.get('/partner/holdings')
async def partner_holdings(request: Request, x_faxbot_direct_key: str | None = Header(default=None),
                           x_faxbot_direct_time: str | None = Header(default=None),
                           x_faxbot_direct_signature: str | None = Header(default=None)):
    exchange = exchange_for(request.app)
    return await _partner_call(lambda: exchange.holdings(signer=x_faxbot_direct_key, request_time=x_faxbot_direct_time,
                                                         signature=x_faxbot_direct_signature))


@router.get('/partner/forms/{address}')
async def partner_form(address: str, request: Request, x_faxbot_direct_key: str | None = Header(default=None),
                       x_faxbot_direct_time: str | None = Header(default=None),
                       x_faxbot_direct_signature: str | None = Header(default=None)):
    exchange = exchange_for(request.app)
    return await _partner_call(lambda: exchange.bundle(address, signer=x_faxbot_direct_key,
                                                       request_time=x_faxbot_direct_time,
                                                       signature=x_faxbot_direct_signature))
