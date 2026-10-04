"""An attempt's own route is authoritative for every result path; fallbacks are bounded."""
import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import hashlib
import hmac
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.config_profiles import ProviderConfiguration
from api.app.callback_locator import callback_base_url, callback_url_with_locators
from api.app.outbound_callbacks import CallbackRejected, CapturedCallbacks
from api.app.outbound_store import DeliveryConflict, OutboundStore
from api.app.outbound_worker import OutboundWorker, PreparationFailure, SubmissionReceipt
from api.app.routing.costs import RateCard, parse_amount
from api.app.routing.fallback import FallbackScheduler
from api.app.routing.store import RouteStore
from api.app.routing.transport import RoutedTransport


SIGNING_KEY = 'synthetic-signalwire-signing-key'
ENVIRONMENT = {
    'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'signalwire', 'FAX_DISABLED': 'false',
    'PHAXIO_API_KEY': 'synthetic-key', 'PHAXIO_API_SECRET': 'synthetic-secret',
    'SIGNALWIRE_SPACE_URL': 'example.signalwire.com', 'SIGNALWIRE_PROJECT_ID': 'project-1',
    'SIGNALWIRE_API_TOKEN': 'synthetic-token', 'SIGNALWIRE_WEBHOOK_SIGNING_KEY': SIGNING_KEY,
    'PUBLIC_API_URL': 'https://faxbot.example.org',
}


def write_pdf(path, pages):
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(path))
    for number in range(pages):
        pdf.drawString(72, 720, f'Synthetic page {number + 1}')
        pdf.showPage()
    pdf.save()


def card(provider, **rates):
    return RateCard(None, provider, 'outbound', provider.title(), 'USD',
                    parse_amount(rates.get('minute', '0')), parse_amount(rates.get('page', '0')), 0, 60, 0,
                    None, datetime(2026, 10, 3))


@pytest.fixture
def multi(database, tmp_path):
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    values = ConfigurationValues.from_environment(ENVIRONMENT)
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key', 'api_secret': 'synthetic-secret'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    delivery, routes = OutboundStore(configuration), RouteStore(database)
    routes.replace_cards([card('phaxio', page='0.07'), card('signalwire', minute='0.0095')])
    return configuration, delivery, routes, snapshot


def accept(multi, to='+12025550123'):
    configuration, _, _, snapshot = multi
    identity, now = uuid4().hex, datetime.utcnow()
    configuration.accept_outbound(snapshot.active, {'id': identity, 'to_number': to, 'file_name': 'synthetic.txt',
        'tiff_path': '', 'status': 'queued', 'pages': 3, 'created_at': now, 'updated_at': now})
    return identity


class Inner:
    """Captured-transport double that records which account each submission used."""
    def __init__(self, store, receipts):
        self.store, self.receipts = store, list(receipts)
        self.used = []
        self.fail_for = set()

    @asynccontextmanager
    async def prepare(self, claim):
        _, profile, _ = self.store.load_dispatch(claim)
        if profile.configuration.provider_id in self.fail_for:
            raise PreparationFailure('provider_unavailable')
        self.current = profile.configuration.provider_id
        yield self

    async def submit(self):
        self.used.append(self.current)
        return self.receipts.pop(0)


def signed_callback(configuration, delivery, job, attempt, fields):
    revision, profile = delivery.attempt_context(job, attempt)
    url = callback_url_with_locators(callback_base_url(revision, profile), job, attempt)
    suffix = ''.join(name + value for name, value in sorted(fields, key=lambda pair: pair[0]))
    digest = hmac.new(SIGNING_KEY.encode(), (url + suffix).encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


@pytest.mark.asyncio
async def test_cheaper_extra_route_is_bound_to_the_attempt_before_submission(multi):
    configuration, delivery, routes, snapshot = multi
    job = accept(multi)
    inner = Inner(delivery, [SubmissionReceipt('FX1', 'in_progress')])
    assert await OutboundWorker(delivery, RoutedTransport(inner)).step() is True
    assert inner.used == ['signalwire']
    row = delivery.get(job)
    assert row['state'] == 'in_progress'
    decision = routes.decision(row['attempt_id'])
    assert (decision['route'], decision['route_reason'], decision['provider_id']) == ('signalwire', 'cheapest', 'signalwire')
    revision, profile = delivery.attempt_context(job, row['attempt_id'])
    assert profile.configuration.provider_id == 'signalwire'
    assert profile.id != snapshot.active.profile_id('outbound')
    # Credentials come from the revision the fax was accepted under.
    assert profile.configuration.credentials['api_token'] == 'synthetic-token'
    kinds = [event['kind'] for event in delivery.history(job)]
    assert kinds.index('route_assigned') < kinds.index('submission_authorized')
    view = delivery.operator_view(job)
    assert view['provider_id'] == 'signalwire'


@pytest.mark.asyncio
async def test_results_are_accepted_only_from_the_account_the_attempt_used(multi):
    configuration, delivery, routes, _ = multi
    job = accept(multi)
    inner = Inner(delivery, [SubmissionReceipt('FX1', 'in_progress')])
    await OutboundWorker(delivery, RoutedTransport(inner)).step()
    attempt = delivery.get(job)['attempt_id']
    callbacks = CapturedCallbacks(delivery)
    fields = [('FaxSid', 'FX1'), ('FaxStatus', 'delivered')]
    # The fax's default provider did not send this attempt; its callback is refused.
    with pytest.raises(CallbackRejected):
        callbacks.receive('phaxio', job, attempt, fields=fields, files=[], signature='x')
    with pytest.raises(CallbackRejected):
        callbacks.receive('signalwire', job, attempt, fields=fields, files=[], signature='A' * 27 + '=')
    signature = signed_callback(configuration, delivery, job, attempt, fields)
    assert callbacks.receive('signalwire', job, attempt, fields=fields, files=[], signature=signature) is True
    assert delivery.get(job)['state'] == 'success'
    # Status polling also reads through the attempt's own account.
    with pytest.raises(DeliveryConflict):
        delivery.observe(job, attempt_id=attempt, profile_id=configuration.read().active.profile_id('outbound'),
                         provider_sid='FX1', status='failed', event_key='wrong-account')


@pytest.mark.asyncio
async def test_poll_target_uses_the_attempt_account(multi):
    _, delivery, _, _ = multi
    job = accept(multi)
    await OutboundWorker(delivery, RoutedTransport(Inner(delivery, [SubmissionReceipt('FX1', 'in_progress')]))).step()
    profile, attempt, sid = delivery.poll_target(job)
    assert profile.configuration.provider_id == 'signalwire' and sid == 'FX1'


def test_native_result_requires_the_attempt_provider(multi, monkeypatch):
    from api.app import main
    configuration, delivery, _, _ = multi
    job = accept(multi)
    claim = delivery.claim('worker-one')
    delivery.begin_submission(claim)
    monkeypatch.setattr(main, '_deliveries', lambda: delivery)
    # The attempt uses Phaxio, so an Asterisk result for it is refused.
    with pytest.raises(DeliveryConflict):
        main._observe_native(job, claim.attempt_id, 'success', 'sip', event_key='ami-result:success')
    assert delivery.get(job)['state'] == 'submitting'


@pytest.mark.asyncio
async def test_native_route_result_is_accepted_for_the_attempt_that_used_it(database, tmp_path, monkeypatch):
    from api.app import main
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    data = tmp_path / 'faxdata'
    data.mkdir()
    values = ConfigurationValues.from_environment({**ENVIRONMENT, 'FAX_OUTBOUND_ROUTES': 'sip',
                                                   'FAX_DATA_DIR': str(data)})
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'k', 'api_secret': 's'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    delivery, routes = OutboundStore(configuration), RouteStore(database)
    routes.replace_cards([card('phaxio', page='0.07'), card('sip', minute='0.005')])
    job = accept((configuration, delivery, routes, snapshot))
    # Accepted for Phaxio, so only the PDF exists; the SIP route needs a fax TIFF.
    write_pdf(data / (job + '.pdf'), pages=3)

    class Ami:
        _connected = asyncio.Event()
    Ami._connected.set()
    inner = Inner(delivery, [SubmissionReceipt(job, 'in_progress')])
    inner.ami = Ami()
    await OutboundWorker(delivery, RoutedTransport(inner)).step()
    assert inner.used == ['sip']
    tiff = data / (job + '.tiff')
    from PIL import Image
    with Image.open(tiff) as image:
        assert image.n_frames == 3
    assert (data / (job + '.pdf')).read_bytes().startswith(b'%PDF')
    attempt = delivery.get(job)['attempt_id']
    monkeypatch.setattr(main, '_deliveries', lambda: delivery)
    assert main._observe_native(job, attempt, 'success', 'sip', event_key='ami-result:success') is True
    assert delivery.get(job)['state'] == 'success'


@pytest.mark.asyncio
async def test_unready_or_failing_extra_route_uses_the_fax_provider(multi):
    _, delivery, routes, _ = multi
    job = accept(multi)
    inner = Inner(delivery, [SubmissionReceipt('PX1', 'in_progress')])
    inner.fail_for = {'signalwire'}
    await OutboundWorker(delivery, RoutedTransport(inner)).step()
    assert inner.used == ['phaxio']
    row = delivery.get(job)
    assert routes.decision(row['attempt_id'])['route'] == 'phaxio'
    revision, profile = delivery.attempt_context(job, row['attempt_id'])
    assert profile.configuration.provider_id == 'phaxio'


def test_route_assignment_is_refused_outside_the_preparation_lease_or_revision(multi):
    configuration, delivery, _, snapshot = multi
    job = accept(multi)
    claim = delivery.claim('worker-one')
    unlisted = ProviderConfiguration('documo', credentials={'api_key': 'x'})
    with pytest.raises(DeliveryConflict):
        delivery.assign_route(claim, unlisted)
    delivery.begin_submission(claim)
    signalwire = ProviderConfiguration('signalwire', credentials={'api_token': 't'})
    with pytest.raises(DeliveryConflict):
        delivery.assign_route(claim, signalwire)


@pytest.mark.asyncio
async def test_definite_failure_falls_back_to_the_next_route_at_most_twice(multi):
    configuration, delivery, routes, _ = multi
    job = accept(multi)
    inner = Inner(delivery, [SubmissionReceipt('FX1', 'failed'), SubmissionReceipt('PX1', 'failed')])
    worker = OutboundWorker(delivery, RoutedTransport(inner))
    await worker.step()
    first = delivery.get(job)['attempt_id']
    assert delivery.get(job)['state'] == 'failed'
    scheduler = FallbackScheduler(delivery, routes)
    assert scheduler.step() is True
    row = delivery.get(job)
    assert row['state'] == 'ready' and row['attempt_id'] is None
    # A late result for the finished attempt can no longer move the requeued fax.
    with pytest.raises(DeliveryConflict):
        delivery.observe(job, attempt_id=first, profile_id=None, provider_sid='FX1', status='success', event_key='late')
    await worker.step()
    assert inner.used == ['signalwire', 'phaxio']
    assert delivery.get(job)['state'] == 'failed'
    # Every route has now failed once; nothing is sent again.
    assert scheduler.step() is False
    assert await worker.step() is False
    kinds = [event['kind'] for event in delivery.history(job)]
    assert kinds.count('route_fallback') == 1 and kinds.count('claimed') == 2
    fallback = next(event for event in delivery.operator_view(job)['events'] if event['kind'] == 'route_fallback')
    assert fallback['details'] == {'category': 'provider_failed'}


@pytest.mark.asyncio
async def test_installed_policy_requeues_in_the_failure_transaction(multi, monkeypatch):
    from api.app.routing.fallback import FallbackPolicy
    configuration, delivery, routes, _ = multi
    monkeypatch.setattr(OutboundStore, 'fallback_policy', FallbackPolicy(FallbackScheduler(delivery, routes)))
    job = accept(multi)
    inner = Inner(delivery, [SubmissionReceipt('FX1', 'failed'), SubmissionReceipt('PX1', 'failed')])
    worker = OutboundWorker(delivery, RoutedTransport(inner))
    await worker.step()
    # The fax never reads as failed while another route remains.
    row = delivery.get(job)
    assert row['state'] == 'ready' and row['attempt_id'] is None
    with configuration.engine.connect() as connection:
        assert connection.scalar(sa.select(configuration.jobs.c.status).where(configuration.jobs.c.id == job)) == 'queued'
    kinds = [event['kind'] for event in delivery.history(job)]
    assert kinds.count('route_fallback') == 1 and 'provider_observed' in kinds  # same instant, either order
    await worker.step()
    assert inner.used == ['signalwire', 'phaxio'] and delivery.get(job)['state'] == 'failed'
    assert await worker.step() is False


@pytest.mark.asyncio
async def test_policy_errors_leave_the_failure_standing(multi, monkeypatch):
    _, delivery, _, _ = multi

    def broken(job_id, attempt_id):
        raise RuntimeError('synthetic policy failure')
    monkeypatch.setattr(OutboundStore, 'fallback_policy', broken)
    job = accept(multi)
    await OutboundWorker(delivery, RoutedTransport(Inner(delivery, [SubmissionReceipt('FX1', 'failed')]))).step()
    assert delivery.get(job)['state'] == 'failed'


@pytest.mark.asyncio
async def test_uncertain_outcomes_are_never_requeued(multi):
    _, delivery, routes, _ = multi
    job = accept(multi)

    class Lost(Inner):
        async def submit(self):
            self.used.append(self.current)
            raise TimeoutError('lost')
    await OutboundWorker(delivery, RoutedTransport(Lost(delivery, []))).step()
    attempt = delivery.get(job)['attempt_id']
    assert delivery.get(job)['state'] == 'reconciliation_required'
    assert FallbackScheduler(delivery, routes).step() is False
    assert delivery.requeue_after_failure(job, attempt_id=attempt, category='provider_failed') is False


def test_requeue_cap_and_partner_refusal_rules(multi):
    configuration, delivery, _, _ = multi
    job = accept(multi)
    for expected in (True, True, False):
        claim = delivery.claim('worker')
        delivery.begin_submission(claim)
        delivery.record_uncertain(claim)
        assert delivery.requeue_after_failure(job, attempt_id=claim.attempt_id,
                                              category='partner_not_received') is expected
    assert delivery.get(job)['state'] == 'reconciliation_required'
    assert delivery.fallback_count(job) == 2
    with pytest.raises(ValueError):
        delivery.requeue_after_failure(job, attempt_id='x', category='timeout')


_NATIVE_NO_DATA = {'Event': 'UserEvent', 'UserEvent': 'FaxResult', 'Status': 'FAILED',
                   'Error': 'The call dropped prematurely', 'Pages': '0', 'Mode': 'T38', 'Station64': '',
                   'Answered': '1791075343', 'Ended': '1791075367', 'Cause': '16'}


@pytest.mark.asyncio
@pytest.mark.parametrize('handler,event,state,sentence', [
    ('_handle_fax_result', _NATIVE_NO_DATA, 'failed',
     'The call connected but no fax data came back from the carrier.'),
    ('_handle_fax_result', {**_NATIVE_NO_DATA, 'Error': 'Received no response to DCS or TCF',
                            'Station64': 'KzE1NTU1NTUwMTk5'}, 'failed',
     'The other fax machine answered but the fax failed: Received no response to DCS or TCF.'),
    ('_handle_originate_response', {'Event': 'OriginateResponse', 'Response': 'Failure', 'Reason': '5'}, 'failed',
     'The number was busy.'),
    ('_handle_fax_result', {**_NATIVE_NO_DATA, 'Status': 'SUCCESS', 'Pages': '2'}, 'success', None),
])
async def test_native_trunk_result_explains_a_failed_fax_in_one_sentence(database, tmp_path, monkeypatch,
                                                                        handler, event, state, sentence):
    """Jobs show why a fax over Faxbot's own trunk failed, instead of a bare failure."""
    from api.app import main
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    data = tmp_path / 'faxdata'
    data.mkdir()
    values = ConfigurationValues.from_environment({**ENVIRONMENT, 'FAX_OUTBOUND_ROUTES': 'sip',
                                                   'FAX_DATA_DIR': str(data)})
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'k', 'api_secret': 's'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    delivery, routes = OutboundStore(configuration), RouteStore(database)
    routes.replace_cards([card('phaxio', page='0.07'), card('sip', minute='0.005')])
    job = accept((configuration, delivery, routes, snapshot))
    write_pdf(data / (job + '.pdf'), pages=1)

    class Ami:
        _connected = asyncio.Event()
    Ami._connected.set()
    inner = Inner(delivery, [SubmissionReceipt(job, 'in_progress')])
    inner.ami = Ami()
    await OutboundWorker(delivery, RoutedTransport(inner)).step()
    attempt = delivery.get(job)['attempt_id']
    monkeypatch.setattr(main, '_deliveries', lambda: delivery)
    monkeypatch.setattr(OutboundStore, 'fallback_policy', None)
    fields = {**event, 'JobID': job, 'AttemptID': attempt, 'ActionID': f'faxbot:{job}:{attempt}'}
    getattr(main, handler)(fields)
    assert delivery.get(job)['state'] == state
    with database.connect() as connection:
        row = connection.execute(sa.select(configuration.jobs).where(configuration.jobs.c.id == job)).mappings().one()
    assert (row['status'], row['error']) == (state, sentence)
