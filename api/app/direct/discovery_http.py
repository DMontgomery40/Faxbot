"""Partners → Find partners: suggestions, introductions, the well-known document and directory publishing.

Operator routes need ``settings:read`` to read and ``settings:write`` to
change anything or to look something up; each change writes an access audit
row. Two routes carry no API key: ``GET /.well-known/faxbot-direct`` (this
installation's partner card, public by design and 404 while direct delivery
or the setting is off) and ``POST /direct/introductions`` (a partner's
introduction, authenticated by its Ed25519 signature and 404 while direct
delivery is off). The router's background step reads hints from recent calls
and makes the lookups (``discovery.py``).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..access.http import require_identity
from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat_async
from ..routing.database import DeliveryStoreError
from . import discovery
from .crypto import DirectProtocolError
from .identity import IdentityUnavailable
from .store import DirectConflict


STEP_SECONDS = 300.0


def discovery_for(app):
    from .http import service_for
    direct = service_for(app)
    try:
        return discovery.DiscoveryService(direct, fetcher=getattr(app.state, 'discovery_fetcher', None),
                                          txt=getattr(app.state, 'discovery_txt', None))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


def _background(app):
    engine, _ = installation_engine(app)
    if engine is None:
        return []

    async def step():
        try:
            service = discovery_for(app)
        except HTTPException:
            return False
        return await service.step()
    return [('faxbot-direct-discovery', repeat_async(step, interval=STEP_SECONDS, initial_delay=45.0,
                                                     warning='Looking for partners from recent calls is temporarily '
                                                             'unavailable; Faxbot tries again.'))]


router = APIRouter(tags=['Direct delivery'], lifespan=lifespan_tasks(_background))


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except DirectProtocolError as error:
        raise HTTPException(400, detail=str(error)) from None
    except DirectConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except IdentityUnavailable:
        raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


async def _run(coroutine):
    try:
        return await coroutine
    except DirectProtocolError as error:
        raise HTTPException(400, detail=str(error)) from None
    except DirectConflict as error:
        raise HTTPException(409, detail=str(error)) from None
    except IdentityUnavailable:
        raise HTTPException(503, detail='Direct delivery keys are unavailable on this installation.') from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct delivery storage is unavailable.') from None


def _actor(request, identity):
    from ..inbound.screening_http import _actor as actor_of
    return actor_of(request, identity)


def _audit(request, identity, operation, target, details):
    from ..inbound.screening_http import _audit as audit
    audit(request, identity, operation, target, details)


def _when(value):
    return value.isoformat() + 'Z' if value is not None else None


# -- the public document and the partner protocol ---------------------------------------------------

@router.get('/.well-known/faxbot-direct', include_in_schema=False)
async def well_known(request: Request):
    """This installation's partner card, for Faxbots that fax it; 404 while off."""
    try:
        service = discovery_for(request.app)
        document = await run_lifecycle_step(service.well_known)
    except (HTTPException, DeliveryStoreError):
        document = None
    if document is None:
        return JSONResponse({'detail': 'Not found.'}, status_code=404)
    return JSONResponse(document, headers={'Cache-Control': 'public, max-age=3600'})


class IntroductionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    statement: str = Field(max_length=4096)
    signature: str = Field(max_length=128)


@router.post('/direct/introductions')
async def receive_introduction(payload: IntroductionIn, request: Request):
    """A partner's signed introduction to one of its partners (a hint only; the challenge fax still decides)."""
    from .service import DirectUnavailable
    service = discovery_for(request.app)
    try:
        status, body = await run_lifecycle_step(lambda: service.receive_introduction(payload.model_dump()))
    except DirectUnavailable:
        raise HTTPException(404, detail='Not found.') from None
    return JSONResponse(body, status_code=status)


# -- the console and command line ----------------------------------------------------------------------

def settings_texts(values, settings):
    direct_on = bool(getattr(values, 'direct_delivery_enabled', False))
    url = (getattr(values, 'public_api_url', '') or '').rstrip('/') + discovery.WELL_KNOWN_PATH
    if not direct_on:
        well_known = ('Turn on "Use direct delivery" under Recipients → Partners → Direct delivery to answer lookups '
                      'and to find partners.')
    elif settings.well_known:
        well_known = 'Faxbots that fax you can read your partner card and suggest enrolling you as a partner.'
    else:
        well_known = 'Other Faxbots cannot find your partner card from their calls to you.'
    from_calls = ('When a fax call shows the other side runs Faxbot, Faxbot asks that address once whether it '
                  'takes faxes directly. This never places a call.' if settings.from_calls else
                  'Faxbot does not look up the other side of your fax calls.')
    directories = (f"Faxbot looks up the numbers you fax in {', '.join(settings.directories)}."
                   if settings.directories else
                   'Faxbot looks numbers up only in directories you trust. None is listed, so nothing is looked up.')
    private = (None if getattr(values, 'direct_allow_private_peers', False) else
               'Addresses on private networks are not looked up. Turn on "Allow partners on private networks '
               '(advanced)" under Recipients → Partners → Direct delivery to look them up.')
    return {'well_known': well_known, 'from_calls': from_calls, 'directories': directories, 'private': private,
            'well_known_url': url}


def _suggestion_view(row, organizations):
    return {'id': row['id'], 'number': row['number'], 'organization': row['organization'], 'source': row['source'],
            'endpoint': row['endpoint'], 'directory': row['directory'], 'created_at': _when(row['created_at']),
            'sentence': discovery.FINDING,
            'source_text': discovery.suggestion_source_text(row, organizations.get(row['introduced_by'])),
            'certificate': discovery.fingerprint_text(row['certificate_sha256']),
            'network_text': discovery.OWN_NETWORK if row['certificate_sha256'] else None}


def _publication_view(row, now):
    expired = row['expires_at'] <= now
    state = 'expired' if expired else 'created'
    # The record's expiry is a calendar day (x=YYYY-MM-DD), so it is shown as that day.
    day = f"{row['expires_at'].day} {row['expires_at']:%B %Y}"
    return {'id': row['id'], 'number': row['number'], 'directory': row['directory'], 'name': row['record_name'],
            'value': row['record_value'], 'zone': f"{row['record_name']}. 3600 IN TXT "
                                                  f"{discovery.zone_strings(row['record_value'])}",
            'expires_at': _when(row['expires_at']), 'expires_text': day, 'expired': expired,
            'sentence': discovery.publication_text(state, row, expires_text=day)}


def _lookup_view(row):
    return {'when': _when(row['started_at']), 'kind': row['kind'], 'host': row['host'], 'number': row['number'],
            'outcome': row['outcome'], 'organization': row['organization'],
            'certificate': discovery.fingerprint_text(row['certificate_sha256']),
            'sentence': discovery.OUTCOME_TEXT.get(row['outcome'], '')}


def _view(service):
    from ..routing.database import utcnow
    now = utcnow()
    values = service.values()
    settings = service.store.settings()
    peers = service.store.peer_list()
    organizations = {peer['id']: peer['organization'] for peer in peers}
    pins = service.store.pinned()

    def certificate_text(peer):
        pin = pins.get(peer['id'])
        last = service.store.last_lookup(host=pin['host'], kind='certificate') if pin else None
        return discovery.OUTCOME_TEXT['certificate_changed'] if last and last['outcome'] == 'certificate_changed' \
            else None
    try:
        number, receives = service.publishable()
        publishable = {'number': number, 'receives': receives, 'sentence': (
            f'You can publish {number}, the number on your partner card.' if receives else
            f'Faxbot does not receive faxes on {number}, the number on your partner card, so it cannot be published.')}
    except DirectConflict as error:
        publishable = {'number': None, 'receives': False, 'sentence': str(error)}
    introductions = []
    for row in service.store.recent_introductions(10):
        first = organizations.get(row['first_peer_id'], 'A removed partner')
        second = organizations.get(row['second_peer_id'], 'a removed partner')
        introductions.append({'id': row['id'], 'when': _when(row['created_at']), 'first': first, 'second': second,
                              'sentence': discovery.introduction_text(first, row['first_outcome'], second,
                                                                      row['second_outcome'])})
    return {
        'direct_delivery': bool(getattr(values, 'direct_delivery_enabled', False)),
        'settings': {'well_known': settings.well_known, 'from_calls': settings.from_calls,
                     'directories': list(settings.directories),
                     'private_allowed': bool(getattr(values, 'direct_allow_private_peers', False))},
        'texts': settings_texts(values, settings),
        'suggestions': [_suggestion_view(row, organizations) for row in service.store.open_suggestions()],
        'partners': [{'id': peer['id'], 'organization': peer['organization'], 'fax_number': peer['phone_number'],
                      'verified': peer['state'] == 'verified', 'may_introduce': service.store.consent(peer),
                      'certificate': discovery.fingerprint_text((pins.get(peer['id']) or {}).get('certificate_sha256')),
                      'certificate_text': certificate_text(peer)}
                     for peer in peers],
        'introductions': introductions,
        'publications': [_publication_view(row, now) for row in service.store.active_publications(now)],
        'publishable': publishable,
        'lookups': [_lookup_view(row) for row in service.store.recent_lookups(10)],
    }


@router.get('/direct/discovery', dependencies=[Depends(require_permission('settings:read'))])
async def read_discovery(request: Request):
    service = discovery_for(request.app)
    return await _call(lambda: _view(service))


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    well_known: bool | None = None
    from_calls: bool | None = None
    directories: list[str] | None = Field(default=None, max_length=20)


@router.put('/direct/discovery/settings', dependencies=[Depends(require_permission('settings:write'))])
async def save_settings(payload: SettingsIn, request: Request, identity=Depends(require_identity)):
    service = discovery_for(request.app)

    def run():
        actor_id, actor_name = _actor(request, identity)
        saved = service.store.save_settings(well_known=payload.well_known, from_calls=payload.from_calls,
                                            directories=payload.directories, actor_id=actor_id,
                                            actor_name=actor_name)
        _audit(request, identity, 'discovery.settings', 'discovery', {
            'well_known': saved.well_known, 'from_calls': saved.from_calls, 'directories': list(saved.directories)})
        return _view(service)
    return {**await _call(run), 'detail': 'Saved.'}


@router.post('/direct/discovery/suggestions/{suggestion_id}/enroll',
             dependencies=[Depends(require_permission('settings:write'))])
async def enroll_suggestion(suggestion_id: str, request: Request, identity=Depends(require_identity)):
    """Enroll a suggested recipient; it still has to be verified with a code by fax."""
    from .http import _peer_view
    service = discovery_for(request.app)
    actor_id, actor_name = await run_lifecycle_step(lambda: _actor(request, identity))
    peer, detail = await _run(service.enroll(suggestion_id, actor_id=actor_id, actor_name=actor_name))
    await run_lifecycle_step(lambda: _audit(request, identity, 'discovery.enroll', suggestion_id,
                                            {'partner': peer['id']}))
    return {**_peer_view(peer), 'detail': detail}


@router.post('/direct/discovery/suggestions/{suggestion_id}/dismiss',
             dependencies=[Depends(require_permission('settings:write'))])
async def dismiss_suggestion(suggestion_id: str, request: Request, identity=Depends(require_identity)):
    service = discovery_for(request.app)

    def run():
        actor_id, actor_name = _actor(request, identity)
        detail = service.dismiss(suggestion_id, actor_id=actor_id, actor_name=actor_name)
        _audit(request, identity, 'discovery.dismiss', suggestion_id, {})
        return detail
    return {'detail': await _call(run)}


class LookupIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    number: str = Field(min_length=3, max_length=40)


@router.post('/direct/discovery/lookup', dependencies=[Depends(require_permission('settings:write'))])
async def look_up_number(payload: LookupIn, request: Request, identity=Depends(require_identity)):
    """Look a number up in the trusted directories now."""
    service = discovery_for(request.app)
    suggestion, detail = await _run(service.look_up(payload.number))
    await run_lifecycle_step(lambda: _audit(request, identity, 'discovery.lookup', 'discovery',
                                            {'found': suggestion is not None}))
    return {'detail': detail, 'suggestion_id': suggestion['id'] if suggestion else None}


class ConsentIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    allowed: bool


@router.post('/direct/discovery/partners/{peer_id}/may-introduce',
             dependencies=[Depends(require_permission('settings:write'))])
async def set_may_introduce(peer_id: str, payload: ConsentIn, request: Request, identity=Depends(require_identity)):
    """Whether this partner may be introduced to your other partners (off by default)."""
    service = discovery_for(request.app)

    def run():
        actor_id, actor_name = _actor(request, identity)
        allowed = service.set_consent(peer_id, payload.allowed, actor_id=actor_id, actor_name=actor_name)
        _audit(request, identity, 'discovery.consent', peer_id, {'allowed': allowed})
        peer = service.store.peer(peer_id)
        return {'may_introduce': allowed, 'detail': (
            f"{peer['organization']} may be introduced to your other partners." if allowed else
            f"{peer['organization']} is not introduced to anyone.")}
    return await _call(run)


class IntroduceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    first: str = Field(min_length=1, max_length=40)
    second: str = Field(min_length=1, max_length=40)


@router.post('/direct/discovery/introductions', dependencies=[Depends(require_permission('settings:write'))])
async def introduce(payload: IntroduceIn, request: Request, identity=Depends(require_identity)):
    """Introduce two partners who both agreed; each gets a signed hint about the other."""
    service = discovery_for(request.app)
    actor_id, actor_name = await run_lifecycle_step(lambda: _actor(request, identity))
    row, detail = await _run(service.introduce(payload.first, payload.second, actor_id=actor_id,
                                               actor_name=actor_name))
    await run_lifecycle_step(lambda: _audit(request, identity, 'discovery.introduce', row['id'], {
        'first': payload.first, 'second': payload.second, 'first_outcome': row['first_outcome'],
        'second_outcome': row['second_outcome']}))
    return {'detail': detail, 'first_outcome': row['first_outcome'], 'second_outcome': row['second_outcome']}


class PublishIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    number: str = Field(min_length=3, max_length=40)
    directory: str = Field(min_length=3, max_length=200)


@router.post('/direct/discovery/publications', dependencies=[Depends(require_permission('settings:write'))],
             status_code=201)
async def publish(payload: PublishIn, request: Request, identity=Depends(require_identity)):
    """The signed DNS record that publishes your number in a directory you control; Faxbot never writes DNS."""
    from ..routing.database import utcnow
    service = discovery_for(request.app)

    def run():
        actor_id, actor_name = _actor(request, identity)
        row = service.publish(payload.number, payload.directory, actor_id=actor_id, actor_name=actor_name)
        _audit(request, identity, 'discovery.publish', row['id'], {'directory': row['directory']})
        view = _publication_view(row, utcnow())
        return {**view, 'detail': discovery.publication_text('created', row)}
    return await _call(run)


@router.post('/direct/discovery/publications/{publication_id}/check',
             dependencies=[Depends(require_permission('settings:write'))])
async def check_publication(publication_id: str, request: Request):
    service = discovery_for(request.app)
    row, state = await _run(service.check_publication(publication_id))
    return {'state': state, 'detail': discovery.publication_text(state, row)}


@router.post('/direct/discovery/publications/{publication_id}/withdraw',
             dependencies=[Depends(require_permission('settings:write'))])
async def withdraw_publication(publication_id: str, request: Request, identity=Depends(require_identity)):
    service = discovery_for(request.app)

    def run():
        actor_id, actor_name = _actor(request, identity)
        row = service.withdraw(publication_id, actor_id=actor_id, actor_name=actor_name)
        _audit(request, identity, 'discovery.withdraw', publication_id, {'directory': row['directory']})
        return {'detail': discovery.publication_text('withdrawn', row)}
    return await _call(run)
