"""Received-fax charges, faxes a provider billed that Faxbot has no record of, and other carriers' call records (B2).

Every provider answer is a fixture shaped like the provider's documented
response (the source is named in each fixture's comment) and is served by an
``httpx.MockTransport`` that refuses anything but a read. No provider is ever
called. Numbers and IDs are synthetic.
"""
from datetime import datetime, timedelta
import json
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa

from api.app.config_values import ConfigurationValues
from api.app.routing import provider_sweep
from api.app.routing.billing import ReceivedChargeReconciler, ReceivedChargeStore
from api.app.routing.carrier_records import (PUBLISHED, SignalWireCallRecords, published, reader_for,
                                             trunk_records)
from api.app.routing.carriers import CarrierChargeStore, CarrierReconciler
from api.app.routing.charges import PhaxioReceivedCharges, SinchReceivedCharges
from api.app.routing.provider_sweep import (PhaxioListing, ProviderSweep, SignalWireListing, SinchListing,
                                            SweepStore, fax_sentence, unrecorded)
from api.app.routing.spending import Spending
from api.app.routing.store import RouteStore
from api.app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 (fixture)


NOW = datetime(2026, 10, 8, 12, 0)
FAR, OURS, CALLER = '+12025550123', '+13035550100', '+12025550188'
SINCH_PROJECT = 'project-1'


def values(**extra):
    return ConfigurationValues.from_environment({
        'FAX_BACKEND': 'sinch', 'FAX_OUTBOUND_ROUTES': 'phaxio', 'FAX_DISABLED': 'true', 'FAX_TIME_ZONE': 'UTC',
        'SINCH_PROJECT_ID': SINCH_PROJECT, 'SINCH_API_KEY': 'synthetic-sinch-key',
        'SINCH_API_SECRET': 'synthetic-sinch-secret', 'PHAXIO_API_KEY': 'synthetic-phaxio-key',
        'PHAXIO_API_SECRET': 'synthetic-phaxio-secret', **extra})


class Provider:
    """A fake provider API: answers GETs from ``answer(request)`` and records them; anything else fails the test."""

    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def handler(self, request):
        assert request.method == 'GET', 'Faxbot only ever reads a provider here.'
        assert not request.url.path.endswith('/file'), 'Faxbot never fetches a document here.'
        self.requests.append(request)
        return self.answer(request)

    def factory(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def engine(database):  # noqa: F811
    upgrade_schema(database)
    return database


def tables(engine):
    metadata = sa.MetaData()
    return {name: sa.Table(name, metadata, autoload_with=engine)
            for name in ('fax_jobs', 'outbound_attempts', 'outbound_deliveries', 'inbound_faxes', 'inbound_imports',
                         'sip_call_records')}


def received_fax(engine, source, operation_id, *, account, account_key=None, when=NOW - timedelta(hours=2)):
    t, fax = tables(engine), uuid4().hex
    with engine.begin() as connection:
        connection.execute(t['inbound_faxes'].insert().values(id=fax, status='received', backend=source, pages=1,
                                                              created_at=when, received_at=when, updated_at=when))
        connection.execute(t['inbound_imports'].insert().values(
            id=uuid4().hex, source=source, account=account, operation_id=operation_id, revision='', state='received',
            attempts=1, imported_at=when, source_received_at=when, acquired_at=when, artifact_digest='0' * 64,
            artifact_size=10, inbound_fax_id=fax, account_key=account_key, created_at=when, updated_at=when))
    return fax


def sent_attempt(engine, provider_sid, *, phase='success', to=FAR, when=NOW - timedelta(days=1)):
    t, job, attempt = tables(engine), uuid4().hex, uuid4().hex
    with engine.begin() as connection:
        connection.execute(t['fax_jobs'].insert().values(id=job, to_number=to, file_name='synthetic.pdf', tiff_path='',
                                                         status='queued', backend='sinch', pages=1, created_at=when,
                                                         updated_at=when))
        connection.execute(t['outbound_attempts'].insert().values(id=attempt, job_id=job, sequence=1, phase=phase,
                                                                  created_at=when, provider_sid=provider_sid))
    return job, attempt


def snapshot(engine):
    t = tables(engine)
    with engine.connect() as connection:
        return {name: sorted(map(tuple, connection.execute(sa.select(table)).all()), key=repr)
                for name, table in t.items()}


# Sinch Fax API v3, "Get a fax" (GET /v3/projects/{projectId}/faxes/{id}), read 2026-10-08: price is Money
# (amount as a decimal string, currencyCode), "populated after the final fax price is calculated".
def sinch_fax(fax_id, *, status='COMPLETED', amount='0.0700', direction='INBOUND'):
    fax = {'id': fax_id, 'direction': direction, 'from': CALLER, 'to': OURS, 'numberOfPages': 1, 'status': status,
           'createTime': '2026-10-08T10:00:00Z', 'completedTime': '2026-10-08T10:01:00Z'}
    if amount is not None:
        fax['price'] = {'amount': amount, 'currencyCode': 'USD'}
    return fax


# Received faxes ------------------------------------------------------------------------------------------------

def test_a_sinch_received_fax_charge_is_read_corrected_settled_and_unknown_stays_unknown(engine):
    current = values()
    fax = received_fax(engine, 'sinch', '01SYNTHETICIN0001', account=f'sinch:{SINCH_PROJECT}', account_key='sinch')
    answers = [sinch_fax('01SYNTHETICIN0001', status='IN_PROGRESS', amount='0.0000'),
               sinch_fax('01SYNTHETICIN0001', amount=None), sinch_fax('01SYNTHETICIN0001'),
               sinch_fax('01SYNTHETICIN0001', amount='0.1400'), sinch_fax('01SYNTHETICIN0001', amount='0.1400')]
    provider = Provider(lambda request: httpx.Response(200, json=answers[len(provider.requests) - 1]))
    store = ReceivedChargeStore(engine)
    reconciler = ReceivedChargeReconciler(store, {'sinch': SinchReceivedCharges(lambda: current,
                                                                              client_factory=provider.factory())})
    spending = Spending(RouteStore(engine), CarrierChargeStore(engine))
    # A placeholder while the fax is going, then a finished fax not priced yet: no charge, never $0.
    reconciler.step(now=NOW)
    reconciler.step(now=NOW + timedelta(minutes=11))
    assert store.in_effect([fax]) == {}
    assert spending.inbound(fax) == {'state': 'waiting', 'summary': 'Cost not reported yet.', 'reported_cost': {}}
    reconciler.step(now=NOW + timedelta(minutes=40))
    assert spending.inbound(fax)['summary'] == 'Sinch charged $0.07 for this fax.'
    # A different price later is a correction: both kept, the later one in effect; a day on, it settles.
    reconciler.step(now=NOW + timedelta(hours=2))
    reconciler.step(now=NOW + timedelta(hours=30))
    with engine.connect() as connection:
        rows = connection.execute(sa.text('SELECT amount_micros, applied, is_final, version FROM '
                                          'provider_received_charges ORDER BY version')).all()
    assert [tuple(row) for row in rows] == [(70_000, 1, 0, 1), (140_000, 1, 0, 2), (140_000, 1, 1, 3)]
    assert store.state(fax) == 'settled'
    assert spending.inbound(fax)['summary'] == 'Sinch charged $0.14 for this fax.'
    url = provider.requests[0].url
    assert (url.host, url.path) == ('fax.api.sinch.com', f'/v3/projects/{SINCH_PROJECT}/faxes/01SYNTHETICIN0001')
    assert provider.requests[0].headers['authorization'].startswith('Basic ')
    # Settled: never asked again.
    reconciler.step(now=NOW + timedelta(hours=60))
    assert len(provider.requests) == 5


def test_another_projects_fax_is_never_asked_about(engine):
    received_fax(engine, 'sinch', '01SYNTHETICIN0002', account='sinch:another-project', account_key='sinch')
    provider = Provider(lambda request: httpx.Response(200, json=sinch_fax('01SYNTHETICIN0002')))
    store = ReceivedChargeStore(engine)
    ReceivedChargeReconciler(store, {'sinch': SinchReceivedCharges(values, client_factory=provider.factory())}).step(
        now=NOW)
    assert provider.requests == []


def test_a_received_fax_the_provider_never_prices_stops_being_asked_about_after_a_week(engine):
    current = values()
    fax = received_fax(engine, 'sinch', '01SYNTHETICIN0003', account=f'sinch:{SINCH_PROJECT}', account_key='sinch')
    provider = Provider(lambda request: httpx.Response(200, json=sinch_fax('01SYNTHETICIN0003', amount=None)))
    store = ReceivedChargeStore(engine)
    reconciler = ReceivedChargeReconciler(store, {'sinch': SinchReceivedCharges(lambda: current,
                                                                              client_factory=provider.factory())})
    reconciler.step(now=NOW)
    reconciler.step(now=NOW + timedelta(days=8))
    assert store.state(fax) == 'unreported'
    spending = Spending(RouteStore(engine), CarrierChargeStore(engine))
    assert spending.inbound(fax)['summary'] == 'Sinch never reported a price for this fax, so its cost is unknown.'


# Phaxio API v2.1, "Get fax info" (GET /v2.1/faxes/{id}), read 2026-10-08: cost is "the total cost of the fax in
# cents"; direction is sent or received.
def test_a_phaxio_received_fax_charge_is_read_in_us_cents(engine):
    from api.app.inbound.acquisition import account_identity
    current = values()
    fax = received_fax(engine, 'phaxio', '987654', account=account_identity('phaxio', 'synthetic-phaxio-key'),
                       account_key='phaxio')
    provider = Provider(lambda request: httpx.Response(200, json={'success': True, 'message': 'Retrieved fax',
        'data': {'id': 987654, 'direction': 'received', 'status': 'success', 'num_pages': 2, 'cost': 14,
                 'is_test': False, 'created_at': '2026-10-08T10:00:00.000-06:00'}}))
    store = ReceivedChargeStore(engine)
    ReceivedChargeReconciler(store, {'phaxio': PhaxioReceivedCharges(lambda: current,
                                                                    client_factory=provider.factory())}).step(now=NOW)
    [report] = store.in_effect([fax])[fax]
    assert (report['amount_micros'], report['currency'], report['raw_amount']) == (140_000, 'USD', '14 cents')
    assert str(provider.requests[0].url) == 'https://api.phaxio.com/v2.1/faxes/987654'


# The unrecorded-fax sweep --------------------------------------------------------------------------------------

def sinch_list(faxes, *, total_pages=1):
    # Sinch Fax API v3 "List faxes" (GET /v3/projects/{projectId}/faxes), read 2026-10-08.
    return {'faxes': faxes, 'totalPages': total_pages, 'pageSize': 100}


def test_a_fax_the_provider_billed_that_faxbot_has_no_record_of_is_listed_and_never_acted_on(engine):
    current = values()
    job, _ = sent_attempt(engine, '01SYNTHETICOUT001')
    received_fax(engine, 'sinch', '01SYNTHETICIN0010', account=f'sinch:{SINCH_PROJECT}', account_key='sinch')
    listing = [sinch_fax('01SYNTHETICOUT001', direction='OUTBOUND', amount='0.0450'),
               sinch_fax('01SYNTHETICIN0010'),
               {**sinch_fax('01SYNTHETICOUT999', direction='OUTBOUND', amount='0.0900'), 'to': '+12025550177',
                'createTime': '2026-10-07T15:30:00Z'},
               {**sinch_fax('01SYNTHETICIN0999', amount=None, status='IN_PROGRESS'), 'from': CALLER}]
    provider = Provider(lambda request: httpx.Response(200, json=sinch_list(listing)))
    before = snapshot(engine)
    sweep = ProviderSweep(engine, lambda: current, client_factory=provider.factory())
    [result] = sweep.run_now(account_key='sinch', now=NOW)
    assert (result.outcome, result.listed, result.unrecorded) == ('complete', 4, 2)
    assert result.sentence == 'Sinch listed 4 faxes; Faxbot has no record of 2 of them.'
    # Read only: no fax, attempt, delivery or received fax changed or was created.
    assert snapshot(engine) == before
    params = provider.requests[0].url.params
    assert (params['createTime>'], params['createTime<'], params['pageSize']) == (
        '2026-10-01T12:00:00Z', '2026-10-08T11:30:00Z', '100')
    rows = unrecorded(engine)
    assert [(row['provider_fax_id'], row['direction'], row['amount_micros']) for row in rows] == [
        ('01SYNTHETICOUT999', 'sent', 90_000), ('01SYNTHETICIN0999', 'received', None)]
    assert fax_sentence(rows[0], 'Sinch', current) == (
        "Sinch billed a fax to +12025550177 on 7 Oct ($0.09) that Faxbot didn't send.")
    assert fax_sentence(rows[1], 'Sinch', current) == (
        f"Sinch lists a received fax from {CALLER} on 8 Oct that isn't in Received.")
    # A late notification brings the received fax in: it is no longer listed, and nothing was deleted.
    received_fax(engine, 'sinch', '01SYNTHETICIN0999', account=f'sinch:{SINCH_PROJECT}', account_key='sinch')
    assert [row['provider_fax_id'] for row in unrecorded(engine)] == ['01SYNTHETICOUT999']
    with engine.connect() as connection:
        assert connection.execute(sa.text('SELECT COUNT(*) FROM provider_unrecorded_faxes')).scalar_one() == 2
    # The same listing again adds nothing; a corrected price adds a version, and the earlier one is kept.
    sweep.run_now(account_key='sinch', now=NOW + timedelta(hours=1))
    listing[2] = {**listing[2], 'price': {'amount': '0.0950', 'currencyCode': 'USD'}}
    sweep.run_now(account_key='sinch', now=NOW + timedelta(hours=2))
    with engine.connect() as connection:
        versions = connection.execute(sa.text("SELECT version, amount_micros FROM provider_unrecorded_faxes WHERE "
                                              "provider_fax_id = '01SYNTHETICOUT999' ORDER BY version")).all()
    assert [tuple(row) for row in versions] == [(1, 90_000), (2, 95_000)]
    assert [row['amount_micros'] for row in unrecorded(engine)] == [95_000]


def test_a_sent_fax_near_an_uncertain_attempt_to_that_number_may_be_that_fax(engine):
    current = values()
    job, _ = sent_attempt(engine, None, phase='uncertain', to='+12025550177', when=datetime(2026, 10, 7, 15, 10))
    listing = [{**sinch_fax('01SYNTHETICOUT998', direction='OUTBOUND', amount='0.0450'), 'to': '+12025550177',
                'createTime': '2026-10-07T15:30:00Z'}]
    provider = Provider(lambda request: httpx.Response(200, json=sinch_list(listing)))
    ProviderSweep(engine, lambda: current, client_factory=provider.factory()).run_now(account_key='sinch', now=NOW)
    [row] = unrecorded(engine)
    assert row['uncertain_job'] == job
    assert fax_sentence(row, 'Sinch', current) == (
        'Sinch billed a fax to +12025550177 on 7 Oct ($0.045) that may be the fax Faxbot was unsure about; check that '
        'fax in Sent before sending it again.')


def test_an_incomplete_listing_keeps_nothing_and_a_failure_is_said_plainly(engine):
    current = values()
    many = [sinch_fax(f'01SYNTHETICOUT{index:03d}', direction='OUTBOUND') for index in range(100)]
    provider = Provider(lambda request: httpx.Response(200, json=sinch_list(many, total_pages=50)))
    sweep = ProviderSweep(engine, lambda: current, client_factory=provider.factory())
    [result] = sweep.run_now(account_key='sinch', now=NOW)
    assert result.outcome == 'partial' and unrecorded(engine) == []
    assert len(provider.requests) == 20
    failing = Provider(lambda request: httpx.Response(401, json={'error': 'synthetic'}))
    [result] = ProviderSweep(engine, lambda: current, client_factory=failing.factory()).run_now(account_key='sinch',
                                                                                                 now=NOW)
    assert (result.outcome, result.sentence) == ('unavailable', 'Sinch refused the account key. Faxbot will try '
                                                                'again later.')
    assert [row['outcome'] for row in SweepStore(engine).latest_sweeps().values()] == ['unavailable']


def test_a_provider_with_no_list_says_so_and_is_never_asked():
    from api.app.accounts import account_named
    current = values(HUMBLEFAX_ACCESS_KEY='synthetic-access', HUMBLEFAX_SECRET_KEY='synthetic-secret',
                     FAX_OUTBOUND_ROUTES='humblefax')
    account = account_named(current, 'humblefax')
    assert provider_sweep.listing_for(account, current) is None
    sweep = ProviderSweep(None, lambda: current)
    result = sweep.sweep_account(account, current, NOW - timedelta(days=7), NOW, now=NOW)
    assert (result.outcome, result.sentence) == ('unsupported', provider_sweep.UNSUPPORTED['humblefax'])


def test_phaxio_and_signalwire_listings_read_each_documented_shape():
    # Phaxio API v2.1 "List faxes" (GET /v2.1/faxes), read 2026-10-08: data with paging; cost in cents.
    phaxio = PhaxioListing.parse({'id': 123456, 'direction': 'sent', 'status': 'success', 'num_pages': 2, 'cost': 14,
                                  'is_test': False, 'created_at': '2026-10-07T09:30:00.000-06:00', 'caller_id': OURS,
                                  'recipients': [{'phone_number': FAR, 'status': 'success'}]})
    assert (phaxio.id, phaxio.direction, phaxio.to_number, phaxio.from_number, phaxio.time, phaxio.amount_micros) == (
        '123456', 'sent', FAR, OURS, datetime(2026, 10, 7, 15, 30), 140_000)
    assert PhaxioListing.parse({'id': 1, 'direction': 'sent', 'status': 'queued', 'cost': 7}).amount_micros is None
    assert PhaxioListing.parse({'id': 2, 'direction': 'received', 'status': 'success', 'is_test': True}).test is True
    # SignalWire Compatibility API "List all faxes", read 2026-10-08: price is negative for a charge.
    signalwire = SignalWireListing.parse({'sid': 'FX0123', 'direction': 'outbound', 'from': OURS, 'to': FAR,
                                          'status': 'delivered', 'num_pages': 1, 'price': '-0.0105',
                                          'price_unit': 'USD', 'date_created': 'Tue, 07 Oct 2026 15:30:00 +0000'})
    assert (signalwire.amount_micros, signalwire.currency, signalwire.time) == (10_500, 'USD',
                                                                                datetime(2026, 10, 7, 15, 30))
    for unusable in ({'sid': 'FX1', 'direction': 'sideways'}, {'direction': 'outbound'}, None, {'id': 'x/../y'}):
        assert SignalWireListing.parse(unusable) is None and SinchListing.parse(unusable) is None


def test_a_signalwire_listing_follows_its_own_next_page_only_on_its_own_host():
    pages = {'/api/laml/2010-04-01/Accounts/project-1/Faxes.json': {
        'faxes': [{'sid': 'FX1', 'direction': 'outbound', 'to': FAR, 'status': 'delivered',
                   'date_created': 'Tue, 07 Oct 2026 15:30:00 +0000', 'price': '-0.0105', 'price_unit': 'USD'}],
        'next_page_uri': '/api/laml/2010-04-01/Accounts/project-1/Faxes.json?Page=1&PageToken=PA2'},
        '/api/laml/2010-04-01/Accounts/project-1/Faxes.json?Page=1&PageToken=PA2': {
            'faxes': [], 'next_page_uri': None}}
    seen = []

    def answer(request):
        seen.append(str(request.url))
        assert request.url.host == 'example.signalwire.com'
        raw = request.url.raw_path.decode()
        return httpx.Response(200, json=pages[raw if 'PageToken' in raw else request.url.path])
    provider = Provider(answer)
    own = values(SIGNALWIRE_SPACE_URL='example.signalwire.com', SIGNALWIRE_PROJECT_ID='project-1',
                 SIGNALWIRE_API_TOKEN='synthetic-token')
    faxes, complete = SignalWireListing(own, client_factory=provider.factory()).fetch(datetime(2026, 10, 7),
                                                                                       datetime(2026, 10, 8))
    assert complete and [fax.id for fax in faxes] == ['FX1'] and len(seen) == 2
    pages['/api/laml/2010-04-01/Accounts/project-1/Faxes.json']['next_page_uri'] = 'https://elsewhere.example/x'
    faxes, complete = SignalWireListing(own, client_factory=provider.factory()).fetch(datetime(2026, 10, 7),
                                                                                       datetime(2026, 10, 8))
    assert not complete


# Other carriers' call records -------------------------------------------------------------------------------

def test_every_trunk_preset_says_whether_its_carrier_publishes_call_records():
    from api.app.sip_trunk import PRESETS
    for preset in PRESETS:
        found = published(preset)
        assert found.sentence.endswith('.')
        if found.api:
            assert found.sources and all(url.startswith('https://') for _, url, _ in found.sources)
    assert published('gamma').api is False and 'Costs → Invoices' in published('gamma').sentence
    sentence = trunk_records(values(SIP_TRUNK_PRESET='signalwire'))['sentence']
    assert sentence == ('SignalWire publishes each call\'s charge, but Faxbot cannot read it yet: add your SignalWire '
                        'project ID and API token under Providers → SignalWire.')
    assert set(PUBLISHED) >= {'telnyx', 'signalwire'}


def signalwire_call(sid, direction, start, seconds, price, *, frm, to, status='completed'):
    # SignalWire Compatibility API "List all calls" (GET .../Calls.json), read 2026-10-08.
    end = start + timedelta(seconds=seconds + 5)
    rfc = lambda moment: moment.strftime('%a, %d %b %Y %H:%M:%S +0000')  # noqa: E731
    return {'sid': sid, 'direction': direction, 'from': frm, 'to': to, 'status': status, 'start_time': rfc(start),
            'end_time': rfc(end), 'duration': str(seconds), 'price': price, 'price_unit': 'USD'}


def test_signalwire_call_records_feed_the_same_per_call_charge(engine):
    t = tables(engine)
    job, attempt = sent_attempt(engine, None, when=NOW - timedelta(hours=3))
    start = NOW - timedelta(hours=3)
    answered, ended = start + timedelta(seconds=5), start + timedelta(seconds=70)
    call = uuid4().hex
    with engine.begin() as connection:
        connection.execute(t['sip_call_records'].insert().values(
            id=call, direction='outbound', call_id=attempt, job_id=job, attempt_id=attempt, trunk_preset='signalwire',
            did=OURS, caller=OURS, called=FAR, started_at=start, answered_at=answered, ended_at=ended,
            disposition='answered', connected_seconds=65, t38='yes', pages=1, fax_status='SUCCESS', fax_preference=0,
            created_at=start, updated_at=ended))
    routes = RouteStore(engine, sip_preset=lambda: 'signalwire')
    from api.app.routing.store import CaptureTarget
    routes.capture(CaptureTarget(attempt, job, FAR, 'sip', None, 'success', 1, start, ended, False),
                   observed_seconds=65)
    answer = {'calls': [signalwire_call('CA0001', 'outbound-api', start, 65, '-0.0190', frm=OURS, to=FAR)],
              'next_page_uri': None}
    provider = Provider(lambda request: httpx.Response(200, json=answer))
    own = values(SIP_TRUNK_PRESET='signalwire', SIGNALWIRE_SPACE_URL='example.signalwire.com',
                 SIGNALWIRE_PROJECT_ID='project-1', SIGNALWIRE_API_TOKEN='synthetic-token')
    source = reader_for('signalwire', own, client_factory=provider.factory())
    assert isinstance(source, SignalWireCallRecords) and source.ready()
    carriers = CarrierChargeStore(engine)
    result = CarrierReconciler(carriers, routes, source, preset='signalwire',
                               numbers=lambda: (OURS,)).run_now(now=NOW)
    assert (result.checked, result.matched, result.recorded) == (1, 1, 1)
    [charge] = carriers.in_effect([call])[call]
    assert (charge['provider_id'], charge['record_id'], charge['amount_micros'], charge['raw_amount'],
            charge['match_method']) == ('signalwire', 'CA0001', 19_000, '-0.0190', 'time_window')
    assert routes.decision(attempt)['reported_cost_micros'] == 19_000
    params = provider.requests[0].url.params
    assert (provider.requests[0].url.host, params['PageSize']) == ('example.signalwire.com', '100')
    # SignalWire's time filters are RFC 2822 GMT.
    assert params['StartTime>'] == (NOW - timedelta(hours=3) - timedelta(minutes=10)).strftime(
        '%a, %d %b %Y %H:%M:%S +0000')


# Flowroute CDR Exports v2.0 (query-cdrs, cdr-status, cdr-results), read 2026-10-08: POST /v2/cdrs/exports answers 202
# with the export's id and status; GET /v2/cdrs/exports/{id} gives a signed S3 download_url once completed; the file
# is a gzip CSV with a header line, times like "2019-06-25 18:18:54+00" and total_cost in decimal dollars.
FLOWROUTE_CSV = ('direction,start_time,end_time,destination,number_alias,callerid,total_cost,destination_name,'
                 'callerid_country,line_information,result,call_fail_sip_code,call_fail_reason,duration,'
                 'billed_duration,rate,first_increment,subsequent_increment,cost_subtotal,connect_fee,usf_fee,ccrf,'
                 'cnam_lookup_fee,custom_x_tag,customer_ip\n'
                 'outbound,2026-10-08 09:00:00+00,2026-10-08 09:01:10+00,12025550123,,13035550100,0.00474784,'
                 'Washington DC,US,,completed,,,65,66,0.0042,6,6,0.0046,0,0.0001,0,0,,192.0.2.10\n'
                 'inbound,2026-10-08 09:20:00+00,2026-10-08 09:20:40+00,13035550100,,12025550188,0.0021,,US,,'
                 'completed,,,35,36,0.0035,6,6,0.0021,0,0,0,0,,\n')


def test_flowroute_call_records_come_from_an_export_that_is_prepared_then_read():
    import gzip
    from api.app.routing.carrier_records import FlowrouteCallRecords
    from api.app.routing.telnyx import CarrierUnavailable
    state = {'status': 'processing', 'link': 'https://faxbot-synthetic.s3.us-east-2.amazonaws.com/cdr.csv.gz?X-Amz-S=1'}
    seen = []

    def answer(request):
        seen.append((request.method, request.url.host, request.headers.get('authorization')))
        if request.method == 'POST':
            assert json.loads(request.content) == {'data': {'type': 'cdrexport', 'attributes': {'filter_parameters': {
                'start_call_start_time': '2026-10-08 08:00:00', 'start_call_end_time': '2026-10-08 11:00:00'}}}}
            return httpx.Response(202, json={'data': {'type': 'cdrexport', 'id': 4242,
                                                      'attributes': {'status': 'processing', 'download_url': None}}})
        if request.url.host == 'api.flowroute.com':
            done = state['status'] == 'completed'
            return httpx.Response(200, json={'data': {'type': 'cdrexport', 'id': 4242, 'attributes': {
                'status': state['status'], 'download_url': state['link'] if done else None}}})
        return httpx.Response(200, content=gzip.compress(FLOWROUTE_CSV.encode()))

    reader = FlowrouteCallRecords(lambda: ('synthetic-access', 'synthetic-secret'), clock=lambda: NOW,
                                  client_factory=lambda: httpx.Client(transport=httpx.MockTransport(answer)))
    start, end = datetime(2026, 10, 8, 8, 50), datetime(2026, 10, 8, 10, 10)
    with pytest.raises(CarrierUnavailable, match='preparing'):
        reader.fetch(start, end)
    with pytest.raises(CarrierUnavailable, match='preparing'):
        reader.fetch(start, end)  # the same export is asked about, never a second one
    state['status'] = 'completed'
    records, complete = reader.fetch(start, end)
    assert complete and [(record.direction, record.cli, record.cld, record.amount_micros, record.billed_seconds,
                          record.answered_at) for record in records] == [
        ('outbound', '13035550100', '12025550123', 4748, 66, datetime(2026, 10, 8, 9, 0, 5)),
        ('inbound', '12025550188', '13035550100', 2100, 36, datetime(2026, 10, 8, 9, 20, 5))]
    assert len({record.id for record in records}) == 2 and all(record.id.startswith('flowroute-') for record in records)
    assert [item[0] for item in seen].count('POST') == 1
    # The signed file link is fetched without the Flowroute key.
    assert seen[-1][1].endswith('.amazonaws.com') and seen[-1][2] is None
    # A link to any other host is refused.
    reader.exports.clear()
    state['link'] = 'https://files.example.net/cdr.csv.gz'
    with pytest.raises(CarrierUnavailable, match='preparing'):
        reader.fetch(start, end)
    with pytest.raises(CarrierUnavailable, match='does not fetch'):
        reader.fetch(start, end)


def test_flowroute_keys_are_needed_and_said_where_to_add():
    current = values(SIP_TRUNK_PRESET='flowroute')
    assert reader_for('flowroute', current).ready() is False
    assert trunk_records(current)['sentence'] == (
        "Flowroute publishes each call's charge, but Faxbot cannot read it yet: add your Flowroute API access key and "
        'secret key under Providers → Flowroute.')
    keyed = values(SIP_TRUNK_PRESET='flowroute', FLOWROUTE_ACCESS_KEY='synthetic-access',
                   FLOWROUTE_SECRET_KEY='synthetic-secret')
    assert trunk_records(keyed)['readable'] is True
    assert trunk_records(keyed)['sources'][0]['url'].startswith('https://developer.flowroute.com/')


def test_a_phaxio_listing_pages_by_the_page_size_phaxio_answered_with():
    # Phaxio API v2.1 "List faxes", read 2026-10-08: paging is {total, per_page, page}; no largest page is named.
    def fax(identity):
        return {'id': identity, 'direction': 'sent', 'status': 'success', 'cost': 7, 'num_pages': 1,
                'created_at': '2026-10-07T09:30:00.000-06:00', 'recipients': [{'phone_number': FAR}]}

    def answer(request):
        page = int(request.url.params['page'])
        assert (request.url.params['created_after'], request.url.params['per_page']) == ('2026-10-07T00:00:00Z', '100')
        items = [fax(page * 10 + index) for index in range(25 if page == 1 else 5)]
        return httpx.Response(200, json={'success': True, 'message': 'Retrieved faxes', 'data': items,
                                         'paging': {'total': 30, 'per_page': 25, 'page': page}})
    provider = Provider(answer)
    faxes, complete = PhaxioListing(values(), client_factory=provider.factory()).fetch(datetime(2026, 10, 7),
                                                                                       datetime(2026, 10, 8))
    assert complete and len(faxes) == 30 and len(provider.requests) == 2
