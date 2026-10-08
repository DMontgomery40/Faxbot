"""Costs → Charges and Costs → Invoices: what each provider charged, faxes it billed that Faxbot has no record of,
and monthly invoices compared with your faxes (B2, M27).

- ``GET /routing/charges``: how each account's charges are read, received-fax charges, the faxes providers listed
  that Faxbot has no record of, and each account's last check.
- ``POST /routing/charges/sweep``: list one account's (or every account's) faxes at its provider now. Read only:
  it never sends, fetches or changes a fax.
- ``GET /routing/invoices``, ``POST /routing/invoices``, ``GET /routing/invoices/{id}`` and
  ``GET /routing/invoices/{id}/file``: monthly invoice totals per account, each with the part your faxes don't
  explain, and a recommendation when that recurs.

This router's lifespan also runs the background reads: received-fax charges (every minute), the unrecorded-fax
sweep (each account every six hours), and the trunk carrier's call records where a carrier other than Telnyx
publishes them (``carrier_records``).
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from ..access.route_policy import require_permission
from ..config_runtime import run_lifecycle_step
from .background import installation_engine, lifespan_tasks, repeat
from .costs import format_amount, money_text
from .database import DeliveryStoreError, utcnow


def _active_values(app):
    def read():
        _, runtime = installation_engine(app)
        manager = getattr(runtime, 'manager', None)
        try:
            return manager.store.read().active.values
        except Exception:
            return None
    return read


def _background(app):
    engine, runtime = installation_engine(app)
    if engine is None:
        return []
    from .billing import ReceivedChargeReconciler, ReceivedChargeStore
    from .charges import PhaxioReceivedCharges, SinchReceivedCharges
    from .provider_sweep import ProviderSweep
    values = _active_values(app)
    tasks = []
    try:
        received = ReceivedChargeReconciler(ReceivedChargeStore(engine), {
            'sinch': SinchReceivedCharges(values), 'phaxio': PhaxioReceivedCharges(values)})
        sweep = ProviderSweep(engine, values)
    except DeliveryStoreError:
        return []
    tasks.append(('faxbot-received-charges', repeat(received.step, interval=60.0, initial_delay=50.0,
                                                    warning='Received fax charges are temporarily unavailable.')))
    tasks.append(('faxbot-provider-sweep', repeat(sweep.step, interval=600.0, initial_delay=120.0,
                                                  warning='Provider fax lists are temporarily unavailable.')))
    from .carrier_records import other_carrier_task
    task = other_carrier_task(engine, values)
    if task is not None:
        tasks.append(task)
    return tasks


router = APIRouter(prefix='/routing', tags=['Delivery routes'], lifespan=lifespan_tasks(_background))


def _engine(request):
    engine, _ = installation_engine(request.app)
    if engine is None:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return engine


def _values(request):
    return request.scope['faxbot.configuration'].active.values


async def _call(operation):
    from .invoices import InvoiceInputError
    try:
        return await run_lifecycle_step(operation)
    except InvoiceInputError as error:
        raise HTTPException(error.status, detail=str(error)) from None
    except LookupError as error:
        raise HTTPException(404, detail=str(error)) from None
    except DeliveryStoreError:
        raise HTTPException(503, detail='Charge records are unavailable.') from None


def _money(micros_by_currency):
    return [{'currency': currency, 'amount': format_amount(micros)}
            for currency, micros in sorted(micros_by_currency.items())]


def _person(request, identity):
    from .http import _person_name
    try:
        return _person_name(request, identity)
    except Exception:
        return None


# Charges ------------------------------------------------------------------------------------------------------

def _account_sources(values, sweeps):
    """How Faxbot reads each account's charges, in one sentence, and when it last listed the account's faxes."""
    from ..accounts import all_accounts
    from .provider_sweep import LISTINGS, UNSUPPORTED
    from .carrier_records import trunk_records
    reading = {
        'sinch': '{label} reports what each sent and received fax cost; Faxbot reads it after each fax.',
        'phaxio': '{label} reports what each sent and received fax cost; Faxbot reads it after each fax.',
        'signalwire': '{label} reports what each sent fax cost; Faxbot reads it after each fax.',
        'humblefax': '{label} charges a monthly plan, not each fax. Enter its invoice under Costs → Invoices.',
        'efax': '{label} prices by quote. Enter its monthly invoice under Costs → Invoices.',
        'documo': "Faxbot can't read {label}'s charges yet. Enter its monthly invoice under Costs → Invoices.",
    }
    found = []
    for account in all_accounts(values):
        if account.provider == 'sip':
            sentence = trunk_records(values)['sentence']
        elif account.provider in reading:
            sentence = reading[account.provider].format(label=account.label)
        else:
            sentence = UNSUPPORTED.get(account.provider) or f'{account.label} reports no charge per fax.'
        last = sweeps.get(account.key)
        found.append({'account_key': account.key, 'provider_id': account.provider, 'label': account.label,
                      'sentence': sentence, 'listing': account.provider in LISTINGS,
                      'last_checked': last['started_at'] if last else None,
                      'last_outcome': last['outcome'] if last else None})
    return found


def _unrecorded_view(row, values):
    from .invoices import account_label
    from .provider_sweep import fax_sentence
    label = account_label(values, row['account_key'])
    return {'id': row['id'], 'account_key': row['account_key'], 'provider_id': row['provider_id'], 'label': label,
            'direction': row['direction'], 'from_number': row['from_number'], 'to_number': row['to_number'],
            'time': row['provider_time'], 'pages': row['pages'],
            'cost': None if row['amount_micros'] is None else {'currency': row['currency'],
                                                               'amount': format_amount(row['amount_micros'])},
            'may_be_uncertain_fax': row.get('uncertain_job'), 'summary': fax_sentence(row, label, values)}


@router.get('/charges', dependencies=[Depends(require_permission('settings:read'))])
async def charges(request: Request, days: int = Query(default=30, ge=1, le=366)):
    """How each account's charges are read, received-fax charges, and faxes Faxbot has no record of."""
    engine, values = _engine(request), _values(request)
    since = utcnow() - timedelta(days=days)

    def read():
        from .billing import ReceivedChargeStore
        from .carrier_records import trunk_records
        from .provider_sweep import SweepStore, unrecorded
        from ..accounts import provider_name
        sweeps = SweepStore(engine).latest_sweeps()
        received = []
        for entry in ReceivedChargeStore(engine).summary(since):
            label = provider_name(entry['provider_id'], values)
            parts = []
            if entry['charged']:
                parts.append(f"{label} charged {', '.join(money_text(m, c) for c, m in sorted(entry['amounts'].items()))}"
                             f" for {entry['charged']} received {'fax' if entry['charged'] == 1 else 'faxes'}")
            if entry['waiting']:
                parts.append(f"{entry['waiting']} {'is' if entry['waiting'] == 1 else 'are'} waiting for {label}'s "
                             'price')
            if entry['never_priced']:
                parts.append(f"{label} never priced {entry['never_priced']}")
            received.append({'provider_id': entry['provider_id'], 'label': label, 'faxes': entry['faxes'],
                             'charged': entry['charged'], 'waiting': entry['waiting'],
                             'never_priced': entry['never_priced'], 'cost': _money(entry['amounts']),
                             'summary': ('; '.join(parts) + '.') if parts else None})
        rows = [_unrecorded_view(row, values) for row in unrecorded(engine, start=since)]
        return {'since': since, 'accounts': _account_sources(values, sweeps), 'received': received,
                'unrecorded': rows, 'trunk': trunk_records(values)}
    return await _call(read)


class SweepIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account: str | None = Field(default=None, max_length=64)
    days: int = Field(default=7, ge=1, le=31)


@router.post('/charges/sweep', dependencies=[Depends(require_permission('settings:write'))])
async def sweep(payload: SweepIn, request: Request):
    """List the account's faxes at its provider now and keep any Faxbot has no record of; never changes a fax."""
    engine, values = _engine(request), _values(request)
    from .provider_sweep import ProviderSweep

    def run():
        results = ProviderSweep(engine, lambda: values).run_now(account_key=payload.account, days=payload.days)
        return [result.as_dict() for result in results]
    results = await _call(run)
    if not results:
        summary = 'None of your accounts is at a provider that lists its faxes.'
    else:
        summary = ' '.join(result['summary'] for result in results)
    return {'results': results, 'summary': summary}


# Invoices -----------------------------------------------------------------------------------------------------

def _invoice_view(item, values):
    invoice, explanation = item['invoice'], item['explanation']
    return {'id': invoice['id'], 'account_key': invoice['account_key'], 'provider_id': invoice['provider_id'],
            'label': item['label'], 'first_day': invoice['first_day'], 'last_day': invoice['last_day'],
            'period_name': explanation['period_name'], 'total': {'currency': invoice['currency'],
                                                                 'amount': explanation['total']},
            'explained': {'currency': invoice['currency'], 'amount': explanation['explained']},
            'residual': {'currency': invoice['currency'], 'amount': explanation['residual']},
            'state': explanation['state'], 'complete': explanation['complete'], 'summary': explanation['summary'],
            'parts': [{**part, 'amount': {'currency': invoice['currency'], 'amount': part['amount']}}
                      for part in explanation['parts']],
            'notes': explanation['notes'], 'faxes': explanation['faxes'], 'note': invoice['note'],
            'file': ({'name': invoice['file_name'], 'type': invoice['file_type'], 'size': invoice['file_size']}
                     if invoice['file_digest'] else None),
            'version': invoice['version'], 'entered_by': invoice['entered_by_name'], 'entered_at': invoice['created_at']}


def _terms(engine, values, account):
    """(currency, billing day) Faxbot uses for an account's invoices: its plan's, else its card's currency and the 1st."""
    from . import plan_budget
    from .store import RouteStore
    try:
        routes = RouteStore(engine, sip_preset=lambda: getattr(values, 'sip_trunk_preset', '') or None)
        card = routes.card_for_route(account.key, account.provider, 'outbound')
        inbound = routes.card_for_route(account.key, account.provider, 'inbound')
        budget = plan_budget.budget_for(account.key, card, values, inbound=inbound)
    except Exception:
        return account.currency or 'USD', 1
    if budget is not None:
        return budget.currency, budget.day
    found = card or inbound
    return (found.currency if found is not None else account.currency or 'USD'), 1


def _accounts_view(engine, values):
    """The accounts an invoice can be entered for, with the currency and billing day Faxbot would use."""
    from ..accounts import all_accounts
    found = []
    for account in all_accounts(values):
        currency, day = _terms(engine, values, account)
        found.append({'account_key': account.key, 'provider_id': account.provider, 'label': account.label,
                      'currency': currency, 'billing_day': day})
    return found


def _store(request, engine=None):
    from .invoices import InvoiceStore
    values = _values(request)
    try:
        return InvoiceStore(engine or _engine(request), getattr(values, 'fax_data_dir', None))
    except DeliveryStoreError:
        raise HTTPException(503, detail='Invoice records are unavailable.') from None


@router.get('/invoices', dependencies=[Depends(require_permission('settings:read'))])
async def list_invoices(request: Request, account: str | None = Query(default=None, max_length=64)):
    """Each invoice entered (its newest version), with the part your faxes don't explain."""
    engine, values = _engine(request), _values(request)
    store = _store(request, engine)

    def read():
        from .invoices import reconcile
        results, advice = reconcile(engine, values, store.latest(account))
        return {'invoices': [_invoice_view(item, values) for item in results], 'recommendations': advice,
                'accounts': _accounts_view(engine, values)}
    return await _call(read)


@router.post('/invoices', status_code=201)
async def add_invoice(request: Request, account: str = Form(..., max_length=64), total: str = Form(..., max_length=32),
                      currency: str = Form(default='USD', max_length=3), month: str | None = Form(default=None,
                                                                                                 max_length=7),
                      first_day: str | None = Form(default=None, max_length=10),
                      last_day: str | None = Form(default=None, max_length=10),
                      note: str | None = Form(default=None, max_length=500), file: UploadFile | None = File(default=None),
                      identity=Depends(require_permission('settings:write'))):
    """Enter an invoice total for one account and billing period; entering it again adds a corrected version."""
    from ..accounts import account_named
    from .invoices import InvoiceInputError, period_for, reconcile
    engine, values = _engine(request), _values(request)
    store = _store(request, engine)
    found = account_named(values, account)
    if found is None:
        raise HTTPException(404, detail='Faxbot has no account with this key.')
    data = name = None
    if file is not None and (file.filename or file.size):
        limit = max(1, int(getattr(values, 'max_file_size_mb', 10) or 10)) * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, detail='The invoice file is larger than the upload limit.')
        name = file.filename
    who = _person(request, identity)

    def add():
        period = period_for(values, month=month, billing_day=_terms(engine, values, found)[1], first_day=first_day,
                            last_day=last_day)
        if period.first_day > utcnow().date() + timedelta(days=1):
            raise InvoiceInputError('That billing period has not started yet.')
        row = store.add(account_key=found.key, provider_id=found.provider, period=period, total=total,
                        currency=currency, note=note, file=data, file_name=name,
                        entered_by=getattr(identity.actor, 'principal_id', None), entered_by_name=who)
        results, advice = reconcile(engine, values, [row])
        return row, results[0], advice
    row, item, _ = await _call(add)
    from ..audit import audit_event
    audit_event('invoice_entered', account=found.key, period=row['first_day'], version=row['version'],
                file=bool(row['file_digest']))
    return _invoice_view(item, values)


@router.get('/invoices/{invoice_id}', dependencies=[Depends(require_permission('settings:read'))])
async def get_invoice(invoice_id: str, request: Request):
    """One invoice, its explanation, and every version entered for its account and period."""
    engine, values = _engine(request), _values(request)
    store = _store(request, engine)

    def read():
        from .invoices import reconcile
        row = store.get(invoice_id)
        history = store.history(row['account_key'], row['first_day'])
        results, _ = reconcile(engine, values, [row])
        view = _invoice_view(results[0], values)
        view['current'] = history[-1]['id'] == row['id']
        view['history'] = [{'id': item['id'], 'version': item['version'],
                            'total': {'currency': item['currency'], 'amount': item['total']},
                            'entered_by': item['entered_by_name'], 'entered_at': item['created_at'],
                            'note': item['note']} for item in history]
        return view
    return await _call(read)


@router.get('/invoices/{invoice_id}/file', dependencies=[Depends(require_permission('settings:read'))])
async def invoice_file(invoice_id: str, request: Request):
    """The invoice file entered with this invoice, checked against what was kept."""
    store = _store(request)

    def read():
        row = store.get(invoice_id)
        return row, store.read_file(row)
    row, data = await _call(read)
    from .invoices import SUFFIXES
    name = f"invoice-{row['account_key']}-{row['first_day']}{SUFFIXES.get(row['file_type'], '')}"
    return Response(content=data, media_type=row['file_type'] or 'application/octet-stream',
                    headers={'Content-Disposition': f'attachment; filename="{name}"',
                             'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store'})
