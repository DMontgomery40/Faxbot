"""Provider accounts over HTTP: the account list on Delivery setup -> Providers & accounts, adding and changing accounts, and health.

Reading needs ``providers:read``; adding or changing an account needs ``providers:write`` and is an audited
configuration change through the existing apply path, checked against the configuration generation the
console read. Secrets are write-only: the list says which secret fields hold a value, never the value.
"""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import accounts
from .access.route_policy import require_permission
from .config_profiles import ConfigurationDocument
from .config_runtime import run_lifecycle_step


router = APIRouter(prefix='/admin/providers/accounts', tags=['Provider accounts'])

Key = Annotated[str, Field(min_length=1, max_length=32)]
SettingValue = str | int | float | bool | None


class Money(BaseModel):
    model_config = ConfigDict(extra='forbid')
    currency: Annotated[str, Field(min_length=3, max_length=3)]
    amount: Annotated[str, Field(min_length=1, max_length=20)]


class Limits(BaseModel):
    model_config = ConfigDict(extra='forbid')
    at_once: int | None = None
    calls_per_second: int | None = None
    daily_limit: Money | None = None


class AccountInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: Key
    provider: Annotated[str, Field(min_length=1, max_length=64)]
    label: Annotated[str, Field(max_length=100)] | None = None
    site: Annotated[str, Field(max_length=64)] | None = None
    sends: bool = True
    receives: bool = False
    numbers: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=200)] = []
    limits: Limits | None = None
    settings: dict[Annotated[str, Field(max_length=64)], SettingValue] = {}
    credentials: dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=1024)]] = {}
    expected_generation: int


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    label: Annotated[str, Field(max_length=100)] | None = None
    site: Annotated[str, Field(max_length=64)] | None = None
    sends: bool | None = None
    receives: bool | None = None
    numbers: Annotated[list[Annotated[str, Field(max_length=40)]], Field(max_length=200)] | None = None
    limits: Limits | None = None
    settings: dict[Annotated[str, Field(max_length=64)], SettingValue] | None = None
    credentials: dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=1024)]] | None = None
    enabled: bool | None = None
    default_sending: Literal[True] | None = None
    default_receiving: Literal[True] | None = None
    expected_generation: int


def _snapshot(request):
    snapshot = request.scope.get('faxbot.configuration')
    if snapshot is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return snapshot


def _engine(request):
    from .routing.background import installation_engine
    engine, _ = installation_engine(request.app)
    return engine


def _provider_ids(values):
    from .config_activation import _catalog
    try:
        return set(_catalog(values).provider_ids)
    except Exception:
        return set(accounts.FIELDS)


def _sites(engine):
    """The organization's sites from its active sending rules ({key: name}), or None when they can't be read."""
    if engine is None:
        return None
    try:
        import json
        from .rules.store import RuleStore
        row = RuleStore(engine).active('organization', '')
        document = json.loads(row['document']) if row else {}
    except Exception:
        return None
    return {site['key']: site.get('name') or site['key'] for site in document.get('sites') or ()
            if isinstance(site, dict) and isinstance(site.get('key'), str)}


def _money(micros, currency):
    from .routing.costs import format_amount
    if micros is None or not currency:
        return None
    return {'currency': currency, 'amount': format_amount(micros)}


def receiver_problems(app):
    """{account key: the last problem a received-fax poller met}, for health."""
    service = getattr(app.state, 'inbound_acquisition', None)
    problems = {}
    for receiver in (getattr(service, 'receivers', None) or {}).values():
        problem = getattr(receiver, 'last_problem', None)
        if problem:
            problems[receiver.account_key] = problem
    return problems


def account_view(values, account, engine, problems=None):
    state, sentence, _ = accounts.health(values, account, engine, problem=(problems or {}).get(account.key))
    return {
        'key': account.key, 'provider': account.provider, 'label': account.label, 'site': account.site,
        'primary': account.primary, 'sends': account.sends, 'receives': account.receives, 'enabled': account.enabled,
        'numbers': list(account.numbers),
        'limits': {'at_once': account.at_once, 'calls_per_second': account.calls_per_second,
                   'daily_limit': _money(account.daily_spend_micros, account.currency)},
        'health': {'state': state, 'sentence': sentence},
        'webhook_address': accounts.webhook_address(values, account),
        'settings': dict(account.settings), 'secrets_set': list(account.secrets_set),
    }


def accounts_state(snapshot, engine, problems=None):
    values = snapshot.desired.values
    sites = _sites(engine) or {}
    return {
        'generation': snapshot.generation,
        'default_sending': accounts.default_sending_key(values),
        'default_receiving': accounts.default_receiving_key(values),
        'accounts': [account_view(values, account, engine, problems) for account in accounts.all_accounts(values)],
        'providers': accounts.provider_kinds(values, _provider_ids(values)),
        'sites': [{'key': key, 'name': name} for key, name in sorted(sites.items())],
    }


def _refusal(error):
    raise HTTPException(error.status, detail=str(error)) from None


def _expect(snapshot, generation):
    if generation != snapshot.generation:
        raise HTTPException(409, detail='The provider accounts changed since this page was opened. Reload and try '
                                        'again.')


def _write(request, identity, documents, changes):
    from .access.http import runtime as access_runtime
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    snapshot = _snapshot(request)
    control = access_runtime(request).control
    result = runtime.manager.apply_accounts(snapshot, ConfigurationDocument(documents), changes,
                                            principal=identity.actor, control=control)
    return accounts_state(result, _engine(request), receiver_problems(request.app))


@router.get('', summary='Provider accounts')
async def list_accounts(request: Request, identity=Depends(require_permission('providers:read'))):
    snapshot = _snapshot(request)
    return await run_lifecycle_step(lambda: accounts_state(snapshot, _engine(request), receiver_problems(request.app)))


@router.post('', summary='Add a provider account')
def add_account(body: AccountInput, request: Request,
                identity=Depends(require_permission('providers:write', audit=True))):
    snapshot = _snapshot(request)
    _expect(snapshot, body.expected_generation)
    values = snapshot.desired.values
    try:
        documents = accounts.added(values, body.model_dump(exclude={'expected_generation'}),
                                   provider_ids=_provider_ids(values), sites=_sites(_engine(request)))
    except accounts.AccountsError as error:
        _refusal(error)
    return _write(request, identity, documents, {})


@router.patch('/{key}', summary='Change a provider account')
def update_account(key: str, body: AccountPatch, request: Request,
                   identity=Depends(require_permission('providers:write', audit=True))):
    snapshot = _snapshot(request)
    _expect(snapshot, body.expected_generation)
    values = snapshot.desired.values
    change = body.model_dump(exclude_unset=True, exclude={'expected_generation'})
    if not change:
        raise HTTPException(400, detail='Nothing to change. Give at least one setting.')
    try:
        documents, changes = accounts.patched(values, key, change, provider_ids=_provider_ids(values),
                                              sites=_sites(_engine(request)))
    except accounts.AccountsError as error:
        _refusal(error)
    return _write(request, identity, documents, changes)


class FaxApplicationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    expected_generation: int


@router.post('/{key}/telnyx-fax-application', summary='Create a Telnyx Fax Application for a Telnyx Fax API account')
async def create_telnyx_fax_application(key: str, body: FaxApplicationInput, request: Request,
                                        identity=Depends(require_permission('providers:write', audit=True))):
    """Create the Fax Application the account sends with, in the account's own Telnyx account, and save its ID.

    Telnyx sends the application's fax results to Faxbot's results address. The account must have its API key and
    no Fax Application yet, so a second press never makes a second application.
    """
    from .outbound_transport import TELNYX_CALLBACK_PATH
    from .telnyx_fax_service import TelnyxFaxError, TelnyxFaxService
    snapshot = _snapshot(request)
    _expect(snapshot, body.expected_generation)
    values = snapshot.desired.values
    account = accounts.account_named(values, key)
    if account is None and key == 'telnyx_fax':
        # The first Telnyx Fax API account is listed once it is set up; creating its application is part of that.
        account = accounts.ProviderAccount(key=key, provider=key, label=accounts.provider_name(key, values),
                                           primary=True, sends=True, receives=False)
    if account is None:
        raise HTTPException(404, detail='Faxbot has no account with this key.')
    if account.provider != 'telnyx_fax':
        raise HTTPException(400, detail='Only a Telnyx Fax API account sends with a Fax Application.')
    try:
        own = accounts.account_values(values, key)
    except accounts.AccountsError as error:
        _refusal(error)
    if own.telnyx_fax_connection_id:
        raise HTTPException(400, detail='This account already has a Fax Application. To create another, clear its '
                                        'Fax Application ID first.')
    if not own.telnyx_api_key:
        raise HTTPException(400, detail='Enter the Telnyx API key first.')
    name = body.name or f'Faxbot ({account.label})'
    try:
        created = await TelnyxFaxService(api_key=own.telnyx_api_key, connection_id='', from_number='') \
            .create_fax_application(name, values.public_api_url.rstrip('/') + TELNYX_CALLBACK_PATH)
    except TelnyxFaxError as error:
        raise HTTPException(502, detail=error.sentence) from None
    documents, changes = dict(accounts.documents(values)), {}
    if account.primary:
        changes['telnyx_fax_connection_id'] = created
    else:
        documents, changes = accounts.patched(values, key, {'settings': {'connection_id': created}},
                                              provider_ids=_provider_ids(values), sites=None)
    try:
        state = await run_lifecycle_step(lambda: _write(request, identity, documents, changes))
    except HTTPException as error:
        # The application exists in Telnyx either way; give the ID so it is never created twice.
        raise HTTPException(error.status_code, detail=f'Telnyx created the Fax Application {created}, but Faxbot '
                                                      'could not save its ID. Enter that ID on the Telnyx Fax API '
                                                      'page.') from None
    sentence = (f'Faxbot created the Fax Application "{name}" in your Telnyx account and saved its ID. Before you '
                'send, assign your fax number and an outbound voice profile to it in Telnyx.')
    return {**state, 'created': {'key': key, 'connection_id': created, 'sentence': sentence}}


@router.get('/{key}/health', summary="One provider account's health")
async def account_health(key: str, request: Request, identity=Depends(require_permission('providers:read'))):
    snapshot = _snapshot(request)
    values = snapshot.desired.values
    account = accounts.account_named(values, key)
    if account is None:
        raise HTTPException(404, detail='Faxbot has no account with this key.')
    problems = receiver_problems(request.app)
    state, sentence, details = await run_lifecycle_step(
        lambda: accounts.health(values, account, _engine(request), problem=problems.get(key)))
    return {'key': key, 'state': state, 'sentence': sentence, 'details': details}
