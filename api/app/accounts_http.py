"""Provider accounts over HTTP: the account list on Providers -> In use, adding and changing accounts, and health.

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
