"""Monthly invoices and the part of each your faxes don't explain (M27), on SQLite and PostgreSQL.

HumbleFax's plan is $10 a month and reports no charge per fax; Sinch reports a
charge for each fax. Every number, fax and invoice here is synthetic. Expected
money is worked out here from the cards and invoices below, never read back
from the code under test.
"""
from datetime import date, datetime
import hashlib
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from api.app.accounts import account_named
from api.app.config_values import ConfigurationValues
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.invoices import (InvoiceInputError, InvoiceStore, attribute, explain, file_kind, parse_total,
                                      period_for, reconcile, recurring)
from api.app.routing.store import RouteStore
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


CAPTURED = datetime(2026, 10, 5)
FAR = '+12025550123'
HUMBLE = '+13035550150'
PDF = b'%PDF-1.4\n% synthetic invoice\n%%EOF\n'


def card(provider, direction='outbound', *, page='0', minute='0', monthly=None):
    return RateCard(None, provider, direction, provider.title(), 'USD', parse_amount(minute), parse_amount(page), 0,
                    60, 0, None, CAPTURED, None if monthly is None else parse_amount(monthly, whole_digits=4))


def configuration(**extra):
    return ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sinch', 'FAX_OUTBOUND_ROUTES': 'humblefax', 'FAX_DISABLED': 'true',
        'SINCH_PROJECT_ID': 'project-1', 'SINCH_API_KEY': 'synthetic-sinch-key',
        'SINCH_API_SECRET': 'synthetic-sinch-secret', 'HUMBLEFAX_ACCESS_KEY': 'synthetic-access',
        'HUMBLEFAX_SECRET_KEY': 'synthetic-secret', 'HUMBLEFAX_FROM_NUMBER': HUMBLE, 'FAX_TIME_ZONE': 'UTC',
        **extra})


@pytest.fixture
def ledger(database, tmp_path):  # noqa: F811
    upgrade_schema(database)
    routes = RouteStore(database)
    routes.replace_cards([card('humblefax', monthly='10'), card('humblefax', 'inbound', monthly='10'),
                          card('sinch', page='0.045'), card('sinch', 'inbound', page='0.07')])
    return database, routes, tmp_path


def tables(engine):
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=engine)
            for name in ('fax_jobs', 'outbound_attempts', 'delivery_attempt_costs', 'inbound_faxes', 'inbound_imports',
                         'provider_received_charges', 'provider_unrecorded_faxes', 'provider_fax_sweeps')}


def sent(engine, route, when, *, pages=2, reported=None, estimated=None, provider=None):
    """One finished sent attempt on ``route``: ``reported`` as the provider charged it, ``estimated`` from the card."""
    t, job, attempt = tables(engine), uuid4().hex, uuid4().hex
    with engine.begin() as connection:
        connection.execute(t['fax_jobs'].insert().values(id=job, to_number=FAR, file_name='synthetic.pdf',
                                                         tiff_path='', status='SUCCESS', backend=provider or route,
                                                         pages=pages, created_at=when, updated_at=when))
        connection.execute(t['outbound_attempts'].insert().values(id=attempt, job_id=job, sequence=1, phase='success',
                                                                  created_at=when, submitted_at=when,
                                                                  completed_at=when, provider_sid=uuid4().hex))
        connection.execute(t['delivery_attempt_costs'].insert().values(
            id=attempt, job_id=job, destination=FAR, route=route, route_reason='configured',
            provider_id=provider or route, billing_checks=0, outcome='success', billed_pages=pages,
            estimated_cost_micros=estimated, currency='USD' if estimated is not None else None,
            reported_cost_micros=reported, reported_currency='USD' if reported is not None else None,
            created_at=when, updated_at=when))
    return attempt


def received(engine, source, when, *, account_key=None, pages=1, operation_id=None, charge=None):
    """One received fax from ``source``; ``charge`` adds the provider's reported charge in micros."""
    t, fax = tables(engine), uuid4().hex
    with engine.begin() as connection:
        connection.execute(t['inbound_faxes'].insert().values(id=fax, status='received', backend=source, pages=pages,
                                                              created_at=when, received_at=when, updated_at=when))
        connection.execute(t['inbound_imports'].insert().values(
            id=uuid4().hex, source=source, account=f'{source}:synthetic', operation_id=operation_id or uuid4().hex,
            revision='', state='received', attempts=1, imported_at=when, source_received_at=when, acquired_at=when,
            artifact_digest='0' * 64, artifact_size=10, inbound_fax_id=fax, account_key=account_key,
            created_at=when, updated_at=when))
        if charge is not None:
            connection.execute(t['provider_received_charges'].insert().values(
                id=uuid4().hex, inbound_fax_id=fax, provider_id=source, account_key=account_key, charge_id='c-' + fax,
                version=1, amount_micros=charge, raw_amount='x', currency='USD', applied=1, is_final=1,
                effective_at=when, observed_at=when, created_at=when))
    return fax


SEPTEMBER = datetime(2026, 9, 10, 15)


# Input -------------------------------------------------------------------------------------------------------

def test_totals_are_exact_decimals_and_never_floats_or_formatted_text():
    assert parse_total('13.20') == ('13.20', 13_200_000)
    assert parse_total('13') == ('13.00', 13_000_000)
    assert parse_total(' 0.000001 ') == ('0.000001', 1)
    assert parse_total('987654321012.5') == ('987654321012.50', 987_654_321_012_500_000)
    for refused in ('1,000.00', '$13.20', '-5', '13.2.0', '', '1e3', '0.0000001', None, 13.2):
        with pytest.raises(InvoiceInputError):
            parse_total(refused)


def test_the_billing_period_follows_the_plans_billing_day_and_whole_local_days():
    values = configuration()
    september = period_for(values, month='2026-09')
    assert (september.first_day, september.last_day) == (date(2026, 9, 1), date(2026, 9, 30))
    assert (september.start, september.end) == (datetime(2026, 9, 1), datetime(2026, 10, 1))
    # Billed on the 31st: February's period starts on its last day, and March's on the 31st.
    february = period_for(values, month='2026-02', billing_day=31)
    assert (february.first_day, february.last_day) == (date(2026, 2, 28), date(2026, 3, 30))
    denver = period_for(configuration(FAX_TIME_ZONE='America/Denver'), month='2026-09', billing_day=15)
    assert (denver.first_day, denver.last_day) == (date(2026, 9, 15), date(2026, 10, 14))
    assert (denver.start, denver.end) == (datetime(2026, 9, 15, 6), datetime(2026, 10, 15, 6))
    given = period_for(values, first_day='2026-09-03', last_day='2026-10-02')
    assert (given.first_day, given.last_day) == (date(2026, 9, 3), date(2026, 10, 2))
    for wrong in ({'month': '2026-13'}, {'month': 'September'}, {'first_day': '2026-09-03'},
                  {'first_day': '2026-10-02', 'last_day': '2026-09-03'},
                  {'first_day': '2026-01-01', 'last_day': '2026-06-30'}, {'first_day': '2026-02-30', 'last_day': '2026-03-01'}):
        with pytest.raises(InvoiceInputError):
            period_for(values, **wrong)


def test_an_invoice_file_is_recognised_by_its_content_never_its_name():
    assert file_kind(PDF, 'invoice.exe') == 'application/pdf'
    assert file_kind(b'\x89PNG\r\n\x1a\n...', 'x') == 'image/png'
    assert file_kind(b'date,amount\n2026-09-30,13.20\n', 'invoice.csv') == 'text/csv'
    for refused, name in ((b'MZ\x90\x00', 'invoice.pdf'), (b'date,amount\x00', 'invoice.csv'),
                          (b'<html>', 'invoice.html'), (b'\xff\xfe\x00', 'invoice.csv')):
        with pytest.raises(InvoiceInputError):
            file_kind(refused, name)


# Storage ------------------------------------------------------------------------------------------------------

def test_a_correction_is_a_new_version_and_the_earlier_one_and_its_file_are_kept(ledger):
    engine, _, tmp_path = ledger
    store = InvoiceStore(engine, str(tmp_path / 'faxdata'))
    period = period_for(configuration(), month='2026-09')
    first = store.add(account_key='humblefax', provider_id='humblefax', period=period, total='13.20', currency='usd',
                      note='  INV-0042  ', file=PDF, file_name='/tmp/../invoice-september.pdf', entered_by='p-1',
                      entered_by_name='Synthetic Admin', now=datetime(2026, 10, 2, 9))
    second = store.add(account_key='humblefax', provider_id='humblefax', period=period, total='13.40', currency='USD',
                       now=datetime(2026, 10, 3, 9))
    assert (first['version'], second['version'], second['supersedes_id']) == (1, 2, first['id'])
    assert (first['note'], first['file_name'], first['currency']) == ('INV-0042', 'invoice-september.pdf', 'USD')
    # The correction keeps the file entered with the first version.
    assert (second['file_digest'], second['file_type']) == (hashlib.sha256(PDF).hexdigest(), 'application/pdf')
    assert [row['total'] for row in store.history('humblefax', '2026-09-01')] == ['13.20', '13.40']
    assert [row['id'] for row in store.latest()] == [second['id']]
    assert store.read_file(store.get(first['id'])) == PDF
    kept = tmp_path / 'faxdata' / 'invoices' / first['file_digest']
    kept.write_bytes(b'%PDF-1.4 changed')
    with pytest.raises(InvoiceInputError, match='has changed'):
        store.read_file(second)


# The residual ------------------------------------------------------------------------------------------------

def test_a_flat_plan_invoice_shows_the_residual_and_explains_it(ledger):
    engine, _, _ = ledger
    values = configuration(FAX_PLAN_BUDGETS='humblefax:pages=200,faxes=3,day=1')
    for day in (3, 9, 17, 24):
        sent(engine, 'humblefax', datetime(2026, 9, day, 15), pages=2, estimated=0)
    received(engine, 'humblefax', datetime(2026, 9, 12, 8), pages=1)  # before accounts: no key, the provider as source
    sent(engine, 'humblefax', datetime(2026, 10, 1, 9), estimated=0)  # October: not this invoice
    sent(engine, 'sinch', SEPTEMBER, reported=135_000)  # another account: not this invoice
    account = account_named(values, 'humblefax')
    invoice = InvoiceStore(engine).add(account_key='humblefax', provider_id='humblefax',
                                       period=period_for(values, month='2026-09'), total='13.20', currency='USD')
    found = attribute(engine, values, account, period_for(values, month='2026-09'), 'USD')
    assert (found.sent, found.received, found.included, found.plan_fee, found.unpriced) == (4, 1, 5, 10_000_000, 0)
    result = explain(invoice, found, 'HumbleFax')
    assert (result['residual'], result['explained'], result['state'], result['complete']) == ('3.20', '10.00',
                                                                                              'residual', True)
    assert result['summary'] == "$3.20 of your HumbleFax invoice for September 2026 isn't explained by your faxes."
    assert result['parts'] == [{'label': 'Plan fee', 'amount': '10.00', 'kind': 'plan'}]
    assert result['notes'] == ['5 faxes included in the plan at no extra charge.',
                               '2 faxes went past your normal-use budget of 3 faxes a month; HumbleFax may charge for '
                               'use past normal use.']


def test_reported_charges_estimates_and_unrecorded_faxes_are_each_attributed(ledger):
    engine, _, _ = ledger
    values = configuration()
    sent(engine, 'sinch', SEPTEMBER, reported=135_000)
    sent(engine, 'sinch', SEPTEMBER, pages=2, estimated=90_000)
    received(engine, 'sinch', SEPTEMBER, account_key='sinch', charge=70_000)
    received(engine, 'sinch', SEPTEMBER, account_key='sinch', pages=2)  # not priced yet: the card's $0.07 a page
    t = tables(engine)
    with engine.begin() as connection:
        connection.execute(t['provider_unrecorded_faxes'].insert().values(
            id=uuid4().hex, sweep_id='sweep-1', account_key='sinch', provider_id='sinch', provider_fax_id='01SYNTHETIC',
            direction='sent', to_number=FAR, provider_time=SEPTEMBER, pages=1, status='COMPLETED',
            amount_micros=45_000, raw_amount='0.0450', currency='USD', version=1, created_at=SEPTEMBER))
    period = period_for(values, month='2026-09')
    invoice = InvoiceStore(engine).add(account_key='sinch', provider_id='sinch', period=period, total='0.48',
                                       currency='USD')
    found = attribute(engine, values, account_named(values, 'sinch'), period, 'USD')
    result = explain(invoice, found, 'Sinch')
    # $0.135 + $0.07 reported, $0.09 + $0.14 estimated, $0.045 for the fax Faxbot has no record of: $0.48.
    assert [(part['label'], part['amount']) for part in result['parts']] == [
        ('2 faxes Sinch reported', '0.205'), ('2 faxes estimated from your prices', '0.23'),
        ('1 fax Faxbot has no record of', '0.045')]
    assert (result['residual'], result['state']) == ('0.00', 'explained')
    assert result['summary'] == 'Your faxes explain all of your Sinch invoice for September 2026.'


def test_a_fax_with_no_price_is_counted_never_zero_and_the_residual_is_incomplete(ledger):
    engine, routes, _ = ledger
    values = configuration()
    sent(engine, 'sinch', SEPTEMBER, reported=135_000)
    sent(engine, 'sinch', SEPTEMBER, estimated=None)  # neither reported nor estimated: unknown
    period = period_for(values, month='2026-09')
    invoice = InvoiceStore(engine).add(account_key='sinch', provider_id='sinch', period=period, total='0.135',
                                       currency='USD')
    result = explain(invoice, attribute(engine, values, account_named(values, 'sinch'), period, 'USD'), 'Sinch')
    assert (result['residual'], result['state'], result['complete'], result['faxes']['not_priced']) == (
        '0.00', 'incomplete', False, 1)
    assert result['summary'] == ('Your faxes explain your Sinch invoice for September 2026, apart from the faxes with '
                                 'no price.')
    assert '1 fax has no price, so part of the difference may be its.' in result['notes']


def test_charges_in_another_currency_are_left_out_and_said_so(ledger):
    engine, _, _ = ledger
    values = configuration()
    t = tables(engine)
    attempt = sent(engine, 'sinch', SEPTEMBER, reported=100_000)
    with engine.begin() as connection:
        connection.execute(t['delivery_attempt_costs'].update().where(t['delivery_attempt_costs'].c.id == attempt)
                           .values(reported_currency='EUR'))
    period = period_for(values, month='2026-09')
    invoice = InvoiceStore(engine).add(account_key='sinch', provider_id='sinch', period=period, total='5',
                                       currency='USD')
    result = explain(invoice, attribute(engine, values, account_named(values, 'sinch'), period, 'USD'), 'Sinch')
    assert result['residual'] == '5.00'
    assert 'Charges of 0.10 EUR are in another currency than the invoice and are left out.' in result['notes']


def _item(account, month, total, residual, complete=True):
    first = date(2026, month, 1)
    return {'invoice': {'id': f'{account}-{month}', 'account_key': account, 'currency': 'USD'},
            'explanation': {'residual_micros': residual, 'total_micros': total, 'complete': complete,
                            'period_name': f'{first:%B} 2026'}}


def test_a_recommendation_appears_only_when_residuals_recur():
    dollars = 1_000_000
    # More than the faxes explain in July and September: at least $1 and 5% each time.
    advice = recurring('HumbleFax', [_item('humblefax', 9, 13_200_000, 3_200_000), _item('humblefax', 8, 10_000_000, 0),
                                     _item('humblefax', 7, 13_100_000, 3_100_000)])
    assert advice['direction'] == 'more' and advice['invoices'] == ['humblefax-7', 'humblefax-9']
    assert advice['text'] == ('Your HumbleFax invoices were more than your faxes explain in July 2026 and September '
                              '2026 ($3.10 and $3.20). Look on the invoice for a charge Faxbot does not know about, '
                              'such as a number fee, taxes or a plan change, and add it to HumbleFax\'s prices under '
                              'Costs → Prices & plans.')
    less = recurring('Sinch', [_item('sinch', 9, 20 * dollars, -2 * dollars), _item('sinch', 8, 20 * dollars, -3 * dollars)])
    assert less['direction'] == 'less' and 'may be higher than what Sinch charges you' in less['text']
    # Once, below $1, below 5%, incomplete, mixed signs or older than the last three: no recommendation.
    for items in ([_item('a', 9, 13_200_000, 3_200_000)],
                  [_item('a', 9, 13_000_000, 900_000), _item('a', 8, 13_000_000, 900_000)],
                  [_item('a', 9, 100 * dollars, 4 * dollars), _item('a', 8, 100 * dollars, 4 * dollars)],
                  [_item('a', 9, 13_200_000, 3_200_000, False), _item('a', 8, 13_200_000, 3_200_000)],
                  [_item('a', 9, 13_200_000, 3_200_000), _item('a', 8, 13_200_000, -3_200_000)],
                  [_item('a', 9, 1, 0), _item('a', 8, 1, 0), _item('a', 7, 1, 0), _item('a', 6, 13_200_000, 3_200_000),
                   _item('a', 5, 13_200_000, 3_200_000)]):
        assert recurring('Account', items) is None


def test_reconcile_lists_each_invoice_newest_first_with_its_recommendation(ledger):
    engine, _, _ = ledger
    values = configuration()
    store = InvoiceStore(engine)
    for month, total in ((7, '13.10'), (8, '10.00'), (9, '13.20')):
        store.add(account_key='humblefax', provider_id='humblefax', period=period_for(values, month=f'2026-{month:02d}'),
                  total=total, currency='USD')
    results, advice = reconcile(engine, values, store.latest())
    assert [item['explanation']['residual'] for item in results] == ['3.20', '0.00', '3.10']
    assert [item['account_key'] for item in advice] == ['humblefax']


# Over HTTP and in the console's terms ----------------------------------------------------------------------------

BOOTSTRAP = 'synthetic-invoices-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


@pytest.fixture
def client(isolated_installation, monkeypatch):
    from app import main
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'humblefax',
                        'HUMBLEFAX_ACCESS_KEY': 'synthetic-access', 'HUMBLEFAX_SECRET_KEY': 'synthetic-secret',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        saved = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [
            {'provider_id': 'humblefax', 'label': 'HumbleFax', 'monthly_fee': '10', 'captured_on': '2026-10-05'}]})
        assert saved.status_code == 200, saved.text
        yield client


def scoped_key(client, scopes):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': scopes})
    assert response.status_code == 200, response.text
    return {'X-API-Key': response.json()['token']}


def test_invoices_are_entered_corrected_listed_and_their_file_is_returned(client):
    entered = client.post('/routing/invoices', headers=ADMIN,
                          data={'account': 'humblefax', 'month': '2026-09', 'total': '13.20', 'note': 'INV-0042'},
                          files={'file': ('invoice.pdf', PDF, 'application/pdf')})
    assert entered.status_code == 201, entered.text
    body = entered.json()
    assert (body['label'], body['period_name'], body['total'], body['residual']) == (
        'HumbleFax', 'September 2026', {'currency': 'USD', 'amount': '13.20'}, {'currency': 'USD', 'amount': '3.20'})
    assert body['summary'] == "$3.20 of your HumbleFax invoice for September 2026 isn't explained by your faxes."
    assert body['file'] == {'name': 'invoice.pdf', 'type': 'application/pdf', 'size': len(PDF)}
    corrected = client.post('/routing/invoices', headers=ADMIN,
                            data={'account': 'humblefax', 'month': '2026-09', 'total': '13.40'})
    assert corrected.status_code == 201, corrected.text
    listed = client.get('/routing/invoices', headers=ADMIN).json()
    assert [item['total']['amount'] for item in listed['invoices']] == ['13.40']
    assert {account['account_key'] for account in listed['accounts']} >= {'humblefax', 'phaxio'}
    shown = client.get(f"/routing/invoices/{body['id']}", headers=ADMIN).json()
    assert (shown['current'], [entry['total']['amount'] for entry in shown['history']]) == (False, ['13.20', '13.40'])
    document = client.get(f"/routing/invoices/{corrected.json()['id']}/file", headers=ADMIN)
    assert (document.status_code, document.content, document.headers['content-type']) == (200, PDF, 'application/pdf')
    assert document.headers['content-disposition'] == 'attachment; filename="invoice-humblefax-2026-09-01.pdf"'


def test_invoice_input_is_refused_in_one_sentence_and_needs_settings_write(client):
    for data, status, detail in (
            ({'account': 'nobody', 'month': '2026-09', 'total': '1'}, 404, 'Faxbot has no account with this key.'),
            ({'account': 'humblefax', 'month': '2026-09', 'total': '1,000'}, 400,
             'Enter the invoice total as a number with up to six decimal places, such as 13.20.'),
            ({'account': 'humblefax', 'total': '10'}, 400,
             'Write the invoice month as year-month, for example 2026-09.'),
            ({'account': 'humblefax', 'month': '2099-01', 'total': '10'}, 400, 'That billing period has not started yet.')):
        response = client.post('/routing/invoices', headers=ADMIN, data=data)
        assert (response.status_code, response.json()['detail']) == (status, detail)
    refused = client.post('/routing/invoices', headers=ADMIN, data={'account': 'humblefax', 'month': '2026-09',
                                                                    'total': '10'},
                          files={'file': ('invoice.pdf', b'MZ\x90\x00', 'application/pdf')})
    assert refused.status_code == 400
    # A key limited to faxes reads and enters no invoices, and lists no charges.
    limited = scoped_key(client, ['fax:read', 'fax:send'])
    assert client.get('/routing/invoices', headers=limited).status_code == 403
    assert client.get('/routing/charges', headers=limited).status_code == 403
    assert client.post('/routing/charges/sweep', headers=limited, json={}).status_code == 403
    assert client.post('/routing/invoices', headers=limited, data={'account': 'humblefax', 'month': '2026-09',
                                                                   'total': '10'}).status_code == 403
