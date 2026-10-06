"""Faxes to the installation's own numbers are delivered inside Faxbot, with no call (SQLite and PostgreSQL)."""
from datetime import datetime
import hashlib
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app.config_values import ConfigurationValues
from api.app.routing import local
from api.app.routing.plan import REASON_TEXT, decided_text, explain
from api.app.routing.policy import RouteCandidate, RoutePolicy
from api.tests.test_routing_multiprovider import Inner, card, write_pdf
from api.tests.test_schema import database  # noqa: F401 (fixture)


NUMBER = '+12025550123'


def values(**environment):
    return ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', **environment})


# -- which numbers are the installation's own --------------------------------------------

def test_own_numbers_are_the_trunk_numbers_it_receives_on_and_the_direct_card_number():
    trunk = values(SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS='+17205550101, +17205550102')
    assert local.own_numbers(trunk) == {'+17205550101', '+17205550102'}
    # Without a trunk, numbers listed for one reach no Faxbot here, so they are not its own.
    assert local.own_numbers(values(SIP_TRUNK_DIDS='+17205550101')) == set()
    card_number = values(DIRECT_DELIVERY_ENABLED='true', DIRECT_FAX_NUMBER='(202) 555-0123')
    assert local.own_numbers(card_number) == {NUMBER}
    assert local.own_numbers(values(DIRECT_FAX_NUMBER='(202) 555-0123')) == set()


def test_a_humblefax_account_number_is_not_an_own_number_while_humblefax_only_sends(monkeypatch):
    """HumbleFax reports its numbers but cannot receive into Faxbot: a fax there must still place a call."""
    from api.app import humblefax_service
    monkeypatch.setattr(humblefax_service, 'account_numbers', lambda *args, **kwargs: ('+13035550100',))
    sending = values(FAX_BACKEND='humblefax', HUMBLEFAX_ACCESS_KEY='synthetic', HUMBLEFAX_SECRET_KEY='synthetic')
    assert '+13035550100' not in local.own_numbers(sending)
    assert not local.applies(sending, '+13035550100')


def test_the_setting_and_a_real_call_request_turn_it_off():
    own = values(SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS='+17205550101')
    assert local.applies(own, '+17205550101')
    assert not local.applies(own, '+17205550199')
    assert not local.applies(own, '+17205550101', by_call=True)
    assert not local.applies(values(SIP_TRUNK_PRESET='telnyx', SIP_TRUNK_DIDS='+17205550101',
                                    FAX_LOCAL_DELIVERY='false'), '+17205550101')
    assert values().local_delivery_enabled is True


def test_the_route_comes_first_unless_you_chose_another_and_says_why():
    phaxio = RouteCandidate('phaxio', 'provider', 'phaxio', card('phaxio', page='0.07'), bound=True)
    here = RouteCandidate(local.LOCAL, 'local', local.LOCAL, None)
    direct = RouteCandidate('direct', 'direct', 'direct', None, peer_id='peer-1')
    first = RoutePolicy().order([phaxio, here, direct])
    assert [(c.route.key, c.reason) for c in first] == [('local', 'own_number'), ('direct', 'direct_peer'),
                                                        ('phaxio', 'configured')]
    assert explain(first[0], '+17208565062') == ('+1 720-856-5062 is one of your own fax numbers, so the fax goes '
                                                 'straight into Received without a phone call.')
    assert decided_text('local', 'own_number') == ('This is one of your own fax numbers, so the fax went straight into '
                                                   'Received without a phone call.')
    assert REASON_TEXT['own_number'].endswith('with no phone call.')
    chosen = RoutePolicy().order([phaxio, here], preferred='phaxio')
    assert [(c.route.key, c.reason) for c in chosen] == [('phaxio', 'preferred'), ('local', 'alternative')]


# -- the send path, on SQLite and PostgreSQL ------------------------------------------------

@pytest.fixture
def own(database, tmp_path, monkeypatch):
    """An installation whose direct card names NUMBER, so NUMBER is one of its own numbers."""
    from api.app.access.runtime import AccessRuntime
    from api.app.config_profiles import ProviderConfiguration
    from api.app.config_store import ConfigurationStore
    from api.app.outbound_store import OutboundStore
    from api.app.routing.store import RouteStore
    from api.app.schema import upgrade_schema
    monkeypatch.setenv('FAX_DATA_DIR', str(tmp_path))
    upgrade_schema(database)
    configuration = ConfigurationStore(database, tmp_path / 'installation.key')
    environment = {'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'PHAXIO_API_KEY': 'synthetic-key',
                   'PHAXIO_API_SECRET': 'synthetic-secret', 'DIRECT_DELIVERY_ENABLED': 'true',
                   'DIRECT_FAX_NUMBER': NUMBER, 'DIRECT_ORGANIZATION': 'County Clinic',
                   'PUBLIC_API_URL': 'https://faxbot.example.org'}
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'synthetic-key', 'api_secret': 'synthetic-secret'})
    snapshot = configuration.initialize(ConfigurationValues.from_environment(environment), actor='test',
                                        providers={'outbound': phaxio})
    routes = RouteStore(database)
    routes.replace_cards([card('phaxio', page='0.07')])
    runtime = AccessRuntime(configuration)
    delivery = local.LocalDelivery(lambda: runtime.inbound, data_dir=lambda: str(tmp_path))
    route = local.LocalRoute(delivery, values=lambda: configuration.read().active.values)

    def accept(*, by_call=False, pages=2):
        job, now = uuid4().hex, datetime.utcnow()
        write_pdf(tmp_path / f'{job}.pdf', pages)
        configuration.accept_outbound(configuration.read().active, {
            'id': job, 'to_number': NUMBER, 'file_name': 'synthetic.pdf', 'tiff_path': '', 'status': 'queued',
            'pages': pages, 'created_at': now, 'updated_at': now, **({'send_by_call': 1} if by_call else {})})
        return job
    return {'configuration': configuration, 'delivery': OutboundStore(configuration), 'routes': routes,
            'runtime': runtime, 'local': delivery, 'route': route, 'accept': accept, 'dir': tmp_path,
            'snapshot': snapshot}


async def _send(own, job_receipts=()):
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing.transport import RoutedTransport
    inner = Inner(own['delivery'], list(job_receipts))
    assert await OutboundWorker(own['delivery'], RoutedTransport(inner, direct=None, local=own['route'])).step()
    return inner


def _received(own):
    faxes = own['runtime'].inbound.tables['inbound_faxes']
    with own['configuration'].engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.select(faxes)).mappings()]


@pytest.mark.asyncio
async def test_a_fax_to_an_own_number_is_received_here_with_no_call(own):
    from api.app.routing.carriers import CarrierChargeStore
    from api.app.routing.spending import Spending
    job = own['accept']()
    inner = await _send(own)
    assert inner.used == []
    row = own['delivery'].get(job)
    assert row['state'] == 'success'
    decision = own['routes'].decision(row['attempt_id'])
    assert (decision['route'], decision['route_reason'], decision['provider_id']) == ('local', 'own_number', 'local')
    [received] = _received(own)
    document = (own['dir'] / f'{job}.pdf').read_bytes()
    assert (received['backend'], received['inbound_backend'], received['status']) == ('local', 'local', 'received')
    assert (received['to_number'], received['from_number'], received['pages']) == (NUMBER, NUMBER, 2)
    assert received['sha256'] == hashlib.sha256(document).hexdigest()
    record = own['local'].find(job)
    assert (record['source'], record['state'], local.report(record)['sent_fax']) == ('local', 'received', job)
    from api.app.inbound.acquisition import describe
    assert describe(received, record)['status_text'] == ('Delivered straight into Received from a fax sent to this '
                                                         'number; no phone call was made.')
    # Repeating the delivery for the same sent fax never makes a second received fax.
    values = own['configuration'].read().active.values
    again = own['local'].deliver(job_id=job, attempt_id='another', values=values, destination=NUMBER, pages=2)
    assert again == received['id'] and len(_received(own)) == 1
    cost = Spending(own['routes'], CarrierChargeStore(own['configuration'].engine)).job(job)
    assert (cost['state'], cost['summary']) == ('local', 'No call needed; it went straight into Received.')
    # Savings: the call its own provider would have placed, priced by that rate card (2 pages at $0.07).
    from api.app.routing.capture import CostRecorder
    from api.app.routing.savings import savings
    CostRecorder(own['routes']).step()
    saved = savings(own['routes'], own['configuration'].engine)['own_numbers']
    assert (saved['faxes'], saved['calls_avoided'], saved['saved']) == (1, 1, {'USD': 140_000})
    assert saved['sentence'] == ('1 fax to your own numbers went straight into Received, so 1 phone call was not '
                                 'needed, saving about $0.14.')
    assert Spending(own['routes'], CarrierChargeStore(own['configuration'].engine)).outbound(
        datetime(2026, 1, 1)) == []
    # Email delivery picks it up once, like any received fax.
    from api.app.intake.store import IntakeStore
    from api.app.intake.worker import ConnectorSecrets
    intake = IntakeStore(own['configuration'].engine, ConnectorSecrets(own['configuration']))
    intake.feed_inbound()
    intake.feed_inbound()
    with own['configuration'].engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(intake.items)).scalar() == 1


@pytest.mark.asyncio
async def test_a_real_call_request_a_preferred_route_or_the_setting_off_place_the_call(own):
    from api.app.outbound_worker import SubmissionReceipt
    job = own['accept'](by_call=True)
    inner = await _send(own, [SubmissionReceipt('PX1', 'success')])
    assert inner.used == ['phaxio'] and own['delivery'].get(job)['state'] == 'success'
    own['routes'].update_destination(NUMBER, preferred_route='phaxio')
    own['accept']()
    assert (await _send(own, [SubmissionReceipt('PX2', 'success')])).used == ['phaxio']
    own['routes'].update_destination(NUMBER, preferred_route=None)
    from api.app.routing.plan import RoutePlanner
    planner = RoutePlanner(own['routes'], local_ready=lambda: True)
    active = own['configuration'].read().active.values
    assert planner.plan(to_number=NUMBER, bound='phaxio', values=active, pages=1).first.route.key == 'local'
    off = active.model_copy(update={'local_delivery_enabled': False})
    assert planner.plan(to_number=NUMBER, bound='phaxio', values=off, pages=1).first.route.key == 'phaxio'
    assert planner.plan(to_number=NUMBER, bound='phaxio', values=active, pages=1, by_call=True).first.route.key == 'phaxio'
    assert _received(own) == []


@pytest.mark.asyncio
async def test_a_lost_answer_is_settled_from_the_records_and_never_makes_two_received_faxes(own):
    from api.app.routing.local import LocalReconciler, LocalRefused
    first = own['accept']()
    # A crash between the two steps: the received-fax record was begun but not completed.
    store = own['local']._store()
    store.begin(source=local.SOURCE, account=local.ACCOUNT, operation_id=first, backend='local', inbound_backend='local',
                to_number=NUMBER, from_number=NUMBER, reported_pages=2, schedule=False)
    await _send(own)
    assert own['delivery'].get(first)['state'] == 'success' and len(_received(own)) == 1

    class Lost:
        """The delivery commits, then its answer is lost."""
        def __init__(self, real):
            self.real, self.document, self.find = real, real.document, real.find

        def deliver(self, **kwargs):
            self.real.deliver(**kwargs)
            raise RuntimeError('answer lost')

        def delivered(self, job_id):
            raise RuntimeError('records unavailable')
    second = own['accept']()
    lost = local.LocalRoute(Lost(own['local']), values=lambda: own['configuration'].read().active.values)
    from api.app.outbound_worker import OutboundWorker
    from api.app.routing.transport import RoutedTransport
    await OutboundWorker(own['delivery'], RoutedTransport(Inner(own['delivery'], []), direct=None, local=lost)).step()
    assert own['delivery'].get(second)['state'] == 'reconciliation_required'
    reconciler = LocalReconciler(own['local'], own['delivery'], own['routes'],
                                 values=lambda: own['configuration'].read().active.values)
    assert reconciler.step() is True
    assert own['delivery'].get(second)['state'] == 'success' and len(_received(own)) == 2
    assert reconciler.step() is False
    # Uncertain, and no received fax can be made (the document is gone): sent by its normal route instead.
    third = own['accept']()

    class Gone(Lost):
        def deliver(self, **kwargs):
            raise RuntimeError('answer lost')
    gone = local.LocalRoute(Gone(own['local']), values=lambda: own['configuration'].read().active.values)
    await OutboundWorker(own['delivery'], RoutedTransport(Inner(own['delivery'], []), direct=None, local=gone)).step()
    (own['dir'] / f'{third}.pdf').unlink()
    with pytest.raises(LocalRefused):
        own['local'].document(third)
    assert reconciler.step() is True
    row = own['delivery'].get(third)
    assert row['state'] == 'ready' and row['attempt_id'] is None and len(_received(own)) == 2


@pytest.mark.asyncio
async def test_a_definite_failure_records_nothing_received_and_falls_back_to_the_normal_route(own):
    from api.app.routing.fallback import FallbackScheduler
    job = own['accept']()
    (own['dir'] / f'{job}.pdf').write_bytes(b'%PDF-1.4 not really a pdf')
    await _send(own)
    row = own['delivery'].get(job)
    assert row['state'] == 'failed'
    record = own['local'].find(job)
    assert record['state'] == 'failed' and not own['local'].delivered(job)
    assert all(fax['status'] != 'received' for fax in _received(own))
    choice = FallbackScheduler(own['delivery'], own['routes']).next_route(job, row['attempt_id'], 'local')
    assert choice.route.key == 'phaxio'
