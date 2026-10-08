"""Digital routes over HTTP: HISP accounts and FHIR clients, recipients' addresses, and messages sent and received.

- Accounts (Providers): reading needs ``providers:read``; adding or changing
  one needs ``providers:write`` and is an audited configuration change through
  the accounts apply path, checked against the generation the console read.
  Secrets are write-only. A FHIR client's public key set is also served
  without a key at ``/digital/jwks/{key}``, so the recipient's system can
  register it by address (SMART Backend Services prefers that).
- Recipients' addresses (Recipients → Details): reading needs
  ``settings:read``; adding, confirming, withdrawing and the NPPES suggestion
  need ``settings:write``, and each change writes one audit row.
- Messages (Sent and Received): ``settings:read``.

The background work (``worker.py``) runs once a minute from this router's lifespan.
"""
import base64
import json
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..accounts import AccountsError
from ..config_profiles import ConfigurationDocument
from ..config_runtime import run_lifecycle_step
from ..routing.background import installation_engine, lifespan_tasks, repeat
from ..routing.database import DeliveryStoreError, utcnow
from . import accounts as digital_accounts
from . import certificates, reachability
from .store import DigitalInputError, DigitalStore
from .text import STATE_TEXT, address_label


SettingValue = str | int | float | bool | None
Key = Annotated[str, Field(min_length=1, max_length=32)]


def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _store(request):
    try:
        return DigitalStore(_engine(request))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct and FHIR records are unavailable.') from None


def _snapshot(request):
    snapshot = request.scope.get('faxbot.configuration')
    if snapshot is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return snapshot


async def _call(operation):
    try:
        return await run_lifecycle_step(operation)
    except (DigitalInputError, certificates.CertificateRefused) as error:
        raise HTTPException(400, detail=str(error)) from None
    except AccountsError as error:
        raise HTTPException(error.status, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Direct and FHIR records are unavailable.') from None


# Accounts -----------------------------------------------------------------------------------------------------------

class DigitalAccountInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: Key
    provider: Literal['hisp', 'fhir']
    label: Annotated[str, Field(max_length=100)] | None = None
    settings: dict[Annotated[str, Field(max_length=64)], SettingValue] = {}
    credentials: dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=32768)]] = {}
    expected_generation: int


class DigitalAccountPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: Annotated[str, Field(max_length=100)] | None = None
    enabled: bool | None = None
    settings: dict[Annotated[str, Field(max_length=64)], SettingValue] | None = None
    credentials: dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=32768)]] | None = None
    expected_generation: int


class SigningKeyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    algorithm: Literal['RS384', 'ES384'] | None = None
    expected_generation: int


class TrustBundleInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: Annotated[str, Field(max_length=500)] | None = None
    content: Annotated[str, Field(max_length=6 * 1024 * 1024)] | None = None   # PEM text, or base64 of a .p7b


def accounts_state(snapshot, engine):
    values = snapshot.desired.values
    store = DigitalStore(engine)
    from .fhir import jwks
    from .routes import plan_sentence
    views = []
    for account in digital_accounts.digital_accounts(values):
        bundle = store.bundle(account.key) if account.kind == 'hisp' else None
        public = None
        if account.kind == 'fhir':
            try:
                public = jwks(account)
            except certificates.CertificateRefused:
                public = None
        views.append(digital_accounts.account_view(account, bundle=bundle, plan_sentence=plan_sentence(account),
                                                   jwks=public))
    return {'generation': snapshot.generation, 'accounts': views, 'kinds': digital_accounts.kinds_view(),
            'presets': [{'id': key, **preset} for key, preset in digital_accounts.PLAN_PRESETS.items()]}


def _expect(snapshot, generation):
    if generation != snapshot.generation:
        raise HTTPException(409, detail='The accounts changed since this page was opened. Reload and try again.')


def _write(request, identity, documents):
    from ..access.http import runtime as access_runtime
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    control = access_runtime(request).control
    result = runtime.manager.apply_accounts(_snapshot(request), ConfigurationDocument(documents), {},
                                            principal=identity.actor, control=control)
    return accounts_state(result, _engine(request))


def _provider_ids(values):
    from ..accounts_http import _provider_ids as fax_providers
    return fax_providers(values)


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None or runtime is None:
        return []
    from ..outbound_store import OutboundStore
    from .worker import DigitalWorker
    worker = DigitalWorker(engine, values=lambda: runtime.manager.store.read().active.values,
                           access=lambda: getattr(app.state, 'access_runtime', None),
                           delivery=lambda: OutboundStore(runtime.manager.store))
    app.state.digital_worker = worker
    return [('faxbot-digital', repeat(worker.step, interval=60.0, initial_delay=40.0,
                                      warning='Direct and FHIR work could not finish; Faxbot tries again in a '
                                              'minute.'))]


router = APIRouter(prefix='/digital', tags=['Direct and FHIR'], lifespan=lifespan_tasks(_background))


@router.get('/accounts', summary='HISP accounts and FHIR clients')
async def list_accounts(request: Request, identity=Depends(require_permission('providers:read'))):
    snapshot, engine = _snapshot(request), _engine(request)
    return await _call(lambda: accounts_state(snapshot, engine))


@router.post('/accounts', summary='Add a HISP account or FHIR client')
def add_account(body: DigitalAccountInput, request: Request,
                identity=Depends(require_permission('providers:write', audit=True))):
    snapshot = _snapshot(request)
    _expect(snapshot, body.expected_generation)
    values = snapshot.desired.values
    try:
        documents = digital_accounts.added(values, body.model_dump(exclude={'expected_generation'}),
                                           provider_ids=_provider_ids(values))
    except AccountsError as error:
        raise HTTPException(error.status, detail=str(error)) from None
    return _write(request, identity, documents)


@router.patch('/accounts/{key}', summary='Change a HISP account or FHIR client')
def update_account(key: str, body: DigitalAccountPatch, request: Request,
                   identity=Depends(require_permission('providers:write', audit=True))):
    snapshot = _snapshot(request)
    _expect(snapshot, body.expected_generation)
    change = body.model_dump(exclude_unset=True, exclude={'expected_generation'})
    if not change:
        raise HTTPException(400, detail='Nothing to change. Give at least one setting.')
    try:
        documents = digital_accounts.patched(snapshot.desired.values, key, change)
    except AccountsError as error:
        raise HTTPException(error.status, detail=str(error)) from None
    return _write(request, identity, documents)


@router.post('/accounts/{key}/signing-key', summary="Make a FHIR client's signing key")
def make_signing_key(key: str, body: SigningKeyInput, request: Request,
                     identity=Depends(require_permission('providers:write', audit=True))):
    snapshot = _snapshot(request)
    _expect(snapshot, body.expected_generation)
    try:
        documents = digital_accounts.with_signing_key(snapshot.desired.values, key, algorithm=body.algorithm)
    except AccountsError as error:
        raise HTTPException(error.status, detail=str(error)) from None
    return _write(request, identity, documents)


@router.post('/accounts/{key}/trust-bundle', summary="Load a HISP account's trust bundle")
async def load_trust_bundle(key: str, body: TrustBundleInput, request: Request,
                            identity=Depends(require_permission('providers:write'))):
    snapshot = _snapshot(request)
    account = digital_accounts.digital_account(snapshot.desired.values, key)
    if account is None or account.kind != 'hisp':
        raise HTTPException(404, detail='Faxbot has no HISP account with this key.')
    if bool(body.url) == bool(body.content):
        raise HTTPException(400, detail='Give the trust bundle\'s web address or paste the bundle, not both.')
    store = _store(request)
    principal = getattr(identity.actor, 'principal_id', None)

    def load():
        from .lookup import https_fetch
        if body.url:
            if not body.url.startswith('https://'):
                raise DigitalInputError('The trust bundle address must start with https://.')
            try:
                data = https_fetch(body.url)
            except LookupError as error:
                raise DigitalInputError(f'Faxbot could not read the trust bundle: {error}') from None
        else:
            text = body.content.strip()
            if '-----BEGIN' in text:
                data = text.encode('ascii', 'replace')
            else:
                try:
                    data = base64.b64decode(text, validate=True)
                except ValueError:
                    raise DigitalInputError('Paste the bundle as PEM text, or as base64 of the .p7b file.') from None
        anchors = certificates.load_certificates(data)
        store.add_bundle(key, certificates.pem(anchors).decode('ascii'), anchors=len(anchors),
                         source_url=body.url or None, principal_id=principal)
        return accounts_state(_snapshot(request), _engine(request))
    return await _call(load)


@router.get('/jwks/{key}', summary="A FHIR client's public key set")
async def public_keys(key: str, request: Request):
    """Public keys only, served without a key so the recipient's system can register this address."""
    snapshot = _snapshot(request)
    account = digital_accounts.digital_account(snapshot.active.values, key)
    if account is None or account.kind != 'fhir' or not account.enabled:
        raise HTTPException(404, detail='Not found.')
    from .fhir import jwks
    found = await _call(lambda: jwks(account))
    if found is None:
        raise HTTPException(404, detail='Not found.')
    return found


# Recipients' addresses ---------------------------------------------------------------------------------------------

class AddressInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['direct', 'fhir']
    address: Annotated[str, Field(min_length=3, max_length=500)]
    account_key: Annotated[str, Field(max_length=32)] | None = None
    organization: Annotated[str, Field(max_length=200)] | None = None
    confirm: bool = False
    note: Annotated[str, Field(max_length=2000)] | None = None


class AddressChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['confirm', 'withdraw', 'dismiss']
    note: Annotated[str, Field(max_length=2000)] | None = None


class NppesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    npi: Annotated[str, Field(min_length=10, max_length=10)]


def _number(value, request):
    from ..routing.numbers import InvalidNumber, normalize_number
    country = _snapshot(request).active.values.fax_default_country
    try:
        return normalize_number(value, country=country)
    except InvalidNumber as error:
        raise HTTPException(400, detail=str(error)) from None


def recipient_view(store, values, number):
    addresses = [reachability.view(item) for item in store.addresses_for(number)]
    accounts = [{'key': account.key, 'label': account.label, 'kind': account.kind}
                for account in digital_accounts.digital_accounts(values) if account.enabled]
    confirmed = [item for item in addresses if item['state'] == 'confirmed']
    if confirmed:
        sentence = ('Faxes to this number may go as ' + ' or '.join(item['label'] for item in confirmed)
                    + ' when that is the better route.')
    else:
        sentence = 'Faxes to this number go only by fax until you confirm a Direct address or FHIR endpoint.'
    return {'number': number, 'addresses': addresses, 'accounts': accounts, 'sentence': sentence}


def _audit(request, identity, number, action, address):
    """One audit row per change: the number, the action and the address; never notes or evidence."""
    access = getattr(request.app.state, 'access_runtime', None)
    if access is None:
        return
    actor = identity.actor
    credential = getattr(actor, 'credential', None)
    details = {'number': number, 'action': action, 'kind': address['kind'], 'address': address['address'][:200]}
    with access.store.transaction() as connection:
        version = access.store.require_lock_on(connection)
        connection.execute(access.store.tables['access_audit'].insert().values(
            id=uuid4().hex, actor_principal_id=getattr(actor, 'principal_id', None),
            actor_key_binding_id=getattr(credential, 'binding_id', None),
            actor_session_id=getattr(credential, 'session_id', None), operation='digital.address',
            target_kind='installation', target_id='digital_addresses', policy_version_before=version,
            policy_version_after=version, outcome='allowed',
            details=json.dumps(details, ensure_ascii=True, separators=(',', ':'), sort_keys=True),
            created_at=utcnow()))


@router.get('/recipients/{number}', summary="A recipient's Direct address and FHIR endpoint")
async def get_recipient(number: str, request: Request, identity=Depends(require_permission('settings:read'))):
    number = _number(number, request)
    store, values = _store(request), _snapshot(request).active.values
    return await _call(lambda: recipient_view(store, values, number))


@router.post('/recipients/{number}', summary="Add a recipient's Direct address or FHIR endpoint")
async def add_address(number: str, body: AddressInput, request: Request,
                      identity=Depends(require_permission('settings:write'))):
    number = _number(number, request)
    store, values = _store(request), _snapshot(request).active.values
    principal = getattr(identity.actor, 'principal_id', None)

    def write():
        added = reachability.add(store, values, number, kind=body.kind, address=body.address,
                                 account_key=body.account_key or None, organization=body.organization,
                                 confirm=body.confirm, note=body.note, principal_id=principal)
        _audit(request, identity, number, 'confirmed' if body.confirm else 'added', added)
        return recipient_view(store, values, number)
    return await _call(write)


@router.post('/recipients/{number}/addresses/{address_id}', summary='Confirm, withdraw or dismiss an address')
async def change_address(number: str, address_id: str, body: AddressChange, request: Request,
                         identity=Depends(require_permission('settings:write'))):
    number = _number(number, request)
    store, values = _store(request), _snapshot(request).active.values
    principal = getattr(identity.actor, 'principal_id', None)
    action = {'confirm': 'confirmed', 'withdraw': 'withdrawn', 'dismiss': 'dismissed'}[body.action]

    def write():
        found = store.address(address_id)
        if found is None or found['phone_number'] != number:
            raise HTTPException(404, detail='Faxbot has no such address for this recipient.')
        changed = store.record(address_id, action, note=body.note, principal_id=principal)
        _audit(request, identity, number, action, changed)
        return recipient_view(store, values, number)
    return await _call(write)


@router.post('/recipients/{number}/nppes', summary='Suggest addresses from the NPI registry')
async def nppes_suggestions(number: str, body: NppesInput, request: Request,
                            identity=Depends(require_permission('settings:write'))):
    """One read of the public NPI registry. What it lists for this number is filed as suggestions, never used."""
    number = _number(number, request)
    store, values = _store(request), _snapshot(request).active.values
    principal = getattr(identity.actor, 'principal_id', None)

    def look_up():
        _, sentence = reachability.suggest_from_nppes(store, values, number, body.npi, principal_id=principal)
        return {**recipient_view(store, values, number), 'nppes_sentence': sentence}
    import httpx
    try:
        return await _call(look_up)
    except (httpx.HTTPError, ValueError):
        # A network failure or an answer that is not the registry's JSON; nothing was filed.
        raise HTTPException(502, detail='Faxbot could not reach the NPI registry; try again.') from None


# Messages -----------------------------------------------------------------------------------------------------------

def message_view(store, row, *, with_events=False):
    view = {'id': row['id'], 'direction': row['direction'], 'kind': row['kind'], 'account_key': row['account_key'],
            'job_id': row['job_id'], 'counterpart': row['counterpart'], 'state': row['state'],
            'sentence': row['detail'] or STATE_TEXT.get(row['state']),
            'label': address_label(row['kind'], row['counterpart']) if row['direction'] == 'out'
            else f'Direct message from {row["counterpart"]}', 'pages': row['pages'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'], 'settled_at': row['settled_at']}
    if with_events:
        view['events'] = [{'kind': event['kind'], 'at': event['created_at'], 'details': event['details']}
                          for event in store.events_for(row['id'])]
    return view


@router.get('/messages', summary='Direct messages and FHIR documents sent and received')
async def list_messages(request: Request, direction: Literal['out', 'in'] | None = None,
                        kind: Literal['direct', 'fhir'] | None = None, limit: int = 50,
                        identity=Depends(require_permission('settings:read'))):
    store = _store(request)
    limit = max(1, min(int(limit), 200))
    return await _call(lambda: {'messages': [message_view(store, row) for row in store.recent(
        direction=direction, kind=kind, limit=limit)]})


@router.get('/faxes/{job_id}', summary="A sent fax's Direct message or FHIR document")
async def fax_messages(job_id: str, request: Request, identity=Depends(require_permission('settings:read'))):
    store = _store(request)
    return await _call(lambda: {'job_id': job_id, 'messages': [message_view(store, row, with_events=True)
                                                               for row in store.for_job(job_id)]})
