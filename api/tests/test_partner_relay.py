"""Local relay through a partner, and sending together across organizations, between real installations.

Installation B is the running application (TestClient): the Sydney office, which relays. Installations A
(Leeds HQ) and C (Harbour Clinic) are durable delivery stores whose direct services post to B; B answers them
in-process. SQLite and PostgreSQL. Every number is a drama number (Ofcom 0113 496 0xxx, ACMA 0x 5550 xxxx),
every document synthetic, and no provider is contacted: B's own provider is a stand-in.
"""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from api.app.config_profiles import ProviderConfiguration
from api.app.config_store import ConfigurationStore
from api.app.config_values import ConfigurationValues
from api.app.direct.relay import RelayService, relay_candidates, sender_identity_for
from api.app.direct.relay_route import RelayReconciler, RelayRoute
from api.app.direct.service import DirectService
from api.app.direct.store import DirectStore
from api.app.outbound_store import OutboundStore
from api.app.outbound_worker import OutboundWorker
from api.app.routing.predict import Shape
from api.app.routing.transport import RoutedTransport
from api.app.schema import create_database_engine, upgrade_schema
from api.tests.test_direct_delivery import Conventional, ToB
from api.tests.test_peer_fax import _namespaces, rows
from api.tests.test_routing_http import ADMIN, BOOTSTRAP


SYDNEY = '+61255501234'
LEEDS = '+441134960123'
HARBOUR = '+441134960456'
DEST = '+61755501234'
# Faxbot calls one number once at a time, so faxes in flight together go to different numbers.
OTHERS = ('+61755501235', '+61755501236', '+61755501237')
# ACMA's number reserved for fiction in the premium-rate range (1900 654 321).
PREMIUM = '+611900654321'
CODE = '48291374'


class Partners:
    """B's transport to A and C, answered in-process; ``drop`` loses every request before it arrives."""

    def __init__(self):
        self.services, self.drop = {}, False

    async def request(self, method, url, **kwargs):
        if self.drop:
            raise httpx.ConnectTimeout('partner offline')
        for base, (direct, relay) in self.services.items():
            if url.startswith(base):
                path = url[len(base):]
                break
        else:
            raise AssertionError(url)
        body = kwargs.get('json') or {}
        if method == 'POST' and path == '/direct/verifications':
            return await asyncio.to_thread(direct.confirm, body['statement'], body['signature'])
        if method == 'POST' and path == '/direct/capabilities':
            return await asyncio.to_thread(direct.note, body['statement'], body['signature'])
        if method == 'POST' and path == '/direct/relay/statements':
            return await asyncio.to_thread(relay.hear, body['statement'], body['signature'])
        raise AssertionError(path)


class Provider:
    """B's own fax provider, a stand-in: it takes each fax and reports nothing until the test says so."""

    def __init__(self):
        self.sent = []

    status_callback_url = None

    def is_configured(self):
        return True

    async def send_fax(self, to, url, job_id, *, attempt_id):
        self.sent.append((job_id, to))
        return {'provider_sid': f'PX-{len(self.sent)}', 'status': 'queued'}

    async def get_fax_status(self, sid):
        return {'provider_sid': sid, 'status': 'queued'}


def _sender(engine, tmp_path, name, organization, number, base, client):
    data = tmp_path / f'{name}-data'
    data.mkdir()
    configuration = ConfigurationStore(engine, tmp_path / f'{name}-installation.key')
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(data), 'PUBLIC_API_URL': base,
        'DIRECT_DELIVERY_ENABLED': 'true', 'FAX_HEADER': organization, 'DIRECT_ORGANIZATION': organization,
        'DIRECT_FAX_NUMBER': number, 'DIRECT_ALLOW_PRIVATE_PEERS': 'true', 'FAX_DEFAULT_COUNTRY': 'GB'})
    phaxio = ProviderConfiguration('phaxio', credentials={'api_key': 'k', 'api_secret': 's'})
    snapshot = configuration.initialize(values, actor='test', providers={'outbound': phaxio})
    to_b = ToB(client)
    direct = DirectService(engine, values=lambda: values,
                           environment={'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / f'{name}.key')}, http=to_b)
    delivery = OutboundStore(configuration)
    relay = RelayService(direct, delivery=lambda: delivery)
    return SimpleNamespace(engine=engine, data=data, configuration=configuration, values=values, snapshot=snapshot,
                           direct=direct, relay=relay, delivery=delivery, to_b=to_b, organization=organization)


def _verify_both_ways(client, partners, side, b_engine):
    """B and the sender enroll each other and each confirms the other's code: verified both ways."""
    b_card = client.get('/direct/card', headers=ADMIN).json()['card']
    enrolled = client.post('/direct/peers', headers=ADMIN, json={'card': side.direct.own_card()})
    assert enrolled.status_code == 201, enrolled.text
    side.on_b = enrolled.json()['id']
    side.b = side.direct.enroll(b_card)['id']
    # The sender faxed B a code; B's administrator confirms it, so the sender has B verified.
    side.direct.store.start_challenge(side.b, code=CODE, job_id=None)
    confirmed = client.post(f'/direct/peers/{side.on_b}/confirm', headers=ADMIN, json={'code': CODE})
    assert confirmed.json()['confirmed'] is True, confirmed.text
    # B faxed the sender a code; the sender confirms it, so B has the sender verified.
    DirectStore(b_engine).start_challenge(side.on_b, code=CODE, job_id=None)
    asyncio.run(side.direct.send_confirmation(side.b, CODE))
    assert DirectStore(b_engine).get_peer(side.on_b)['state'] == 'verified'
    assert side.direct.store.get_peer(side.b)['state'] == 'verified'


@pytest.fixture(params=['sqlite', 'postgresql'])
def relay_trio(request, isolated_installation, monkeypatch, tmp_path):
    scoped = _namespaces(request, 3)
    b_url = scoped[1].render_as_string(hide_password=False) if scoped else None
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'PHAXIO_API_KEY': 'synthetic-key',
                        'PHAXIO_API_SECRET': 'synthetic-secret', 'MAX_REQUESTS_PER_MINUTE': '0',
                        'DIRECT_DELIVERY_ENABLED': 'true', 'DIRECT_ORGANIZATION': 'Sydney office',
                        'DIRECT_FAX_NUMBER': SYDNEY, 'FAX_DEFAULT_COUNTRY': 'AU', 'FAX_TIME_ZONE': 'Australia/Sydney',
                        'DIRECT_ALLOW_PRIVATE_PEERS': 'true', 'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'b.key'),
                        'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', **({'DATABASE_URL': b_url} if b_url else {})
                        }.items():
        monkeypatch.setenv(name, value)
    provider = Provider()
    monkeypatch.setattr('app.outbound_transport.service_from_profile', lambda profile: provider)

    async def idle(self):
        return False
    # B's own background relay steps would race the steps each test runs itself; they are covered by those calls.
    monkeypatch.setattr('app.direct.relay.RelayService.step', idle)
    monkeypatch.setattr('app.direct.relay_route.RelayReconciler.step', idle)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        engines = []
        for index, name in ((0, 'a'), (2, 'c')):
            url = (scoped[index].render_as_string(hide_password=False) if scoped
                   else 'sqlite:///' + str(tmp_path / f'{name}.db'))
            engine = create_database_engine(url)
            upgrade_schema(engine)
            engines.append(engine)
        a = _sender(engines[0], tmp_path, 'a', 'Leeds HQ', LEEDS, 'https://a.example', client)
        c = _sender(engines[1], tmp_path, 'c', 'Harbour Clinic', HARBOUR, 'https://c.example', client)
        partners = Partners()
        partners.services = {'https://a.example': (a.direct, a.relay), 'https://c.example': (c.direct, c.relay)}
        main.app.state.direct_http = partners
        b_engine = main.app.state.configuration_runtime.manager.store.engine
        for side in (a, c):
            _verify_both_ways(client, partners, side, b_engine)
        b_configuration = main.app.state.configuration_runtime.manager.store
        try:
            yield SimpleNamespace(a=a, c=c, client=client, partners=partners, provider=provider, b_engine=b_engine,
                                  b_delivery=OutboundStore(b_configuration))
        finally:
            main.app.state.direct_http = None
            for engine in engines:
                engine.dispose()


def b_relay():
    from app.direct.relay_http import relay_for
    return relay_for(main.app)


def grant(trio, side, **terms):
    body = {'partner': side.on_b, 'countries': ['AU'], **terms}
    response = trio.client.post('/direct/relay/agreements', headers=ADMIN, json=body)
    assert response.status_code == 201, response.text
    return response.json()


def offer_on(side):
    (row,) = side.relay.store.agreements_for(role='sender')
    return row


def accept_offer(side, **options):
    row = offer_on(side)
    accepted = side.relay.accept(row['id'], **options)
    assert asyncio.run(side.relay.tell(accepted)) == 'told'
    return side.relay.store.agreement(row['id'])


def agree(trio, side, *, together=False, **terms):
    grant(trio, side, together=together, **terms)
    return accept_offer(side, together=together)


def queue(side, *, pages=1, number=DEST):
    job = uuid4().hex
    from reportlab.pdfgen import canvas
    pdf = canvas.Canvas(str(side.data / (job + '.pdf')))
    for page in range(pages):
        pdf.drawString(72, 720, f'Synthetic confirmation for the regulator, page {page + 1}')
        pdf.showPage()
    pdf.save()
    now = datetime.utcnow()
    side.configuration.accept_outbound(side.snapshot.active, {
        'id': job, 'to_number': number, 'file_name': 'confirmation.pdf', 'tiff_path': '', 'status': 'queued',
        'pages': pages, 'created_at': now, 'updated_at': now})
    return job


class PlannedTransport(RoutedTransport):
    """The routed transport with the plan the planner will make once it lists relays (WP-C): relay, then own."""

    def __init__(self, inner, relay, peer_id):
        super().__init__(inner, direct=None, relay=relay)
        self.peer_id = peer_id

    def _plan(self, claim):
        revision, _, job = self.store.load_dispatch(claim)
        relay = SimpleNamespace(key='relay:' + self.peer_id, kind='relay', provider_id='relay', bound=False,
                                peer_id=self.peer_id)
        own = SimpleNamespace(key='phaxio', kind='provider', provider_id='phaxio', bound=True, peer_id=None)
        plan = SimpleNamespace(destination=job['to_number'], peer=None,
                               choices=(SimpleNamespace(route=relay, reason='cheapest'),
                                        SimpleNamespace(route=own, reason='configured')))
        return plan, job, revision


async def send(side):
    conventional = Conventional(side.delivery)
    transport = PlannedTransport(conventional, RelayRoute(side.direct, delivery=lambda: side.delivery), side.b)
    assert await OutboundWorker(side.delivery, transport).step() is True
    return conventional


def relayed_job(trio, side):
    (row,) = [row for row in rows(trio.b_engine, "SELECT * FROM relay_faxes WHERE role = 'relay'")
              if row['peer_id'] == side.on_b]
    return row


def wait_for(condition, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = condition()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError('The condition did not hold in time.')


def b_sent(trio, job_id):
    """B's own provider took the relayed fax (its worker sends it as B's own fax)."""
    wait_for(lambda: trio.b_delivery.get(job_id)['state'] == 'in_progress')
    return trio.b_delivery.get(job_id)


def b_result(trio, job_id, status):
    row = b_sent(trio, job_id)
    _, profile = trio.b_delivery.attempt_context(job_id, row['attempt_id'])
    if status == 'uncertain':
        trio.b_delivery.record_unconfirmed(job_id, attempt_id=row['attempt_id'], profile_id=profile.id,
                                           event_key='test:' + status)
    else:
        trio.b_delivery.observe(job_id, attempt_id=row['attempt_id'], profile_id=profile.id, provider_sid=None,
                                status=status, event_key='test:' + status,
                                error='The fax machine was busy.' if status == 'failed' else None)


def b_report():
    relay = b_relay()
    relay.settle()
    return asyncio.run(relay.report())


# Agreements ---------------------------------------------------------------------------------------------------

def test_an_agreement_is_offered_accepted_and_withdrawn_with_every_signed_statement_kept(relay_trio):
    trio, a = relay_trio, relay_trio.a
    # Off by default: no agreement, so no relay is a candidate and nothing is suggested without a price.
    assert relay_candidates(DEST, Shape(1, None, 'fine', 'normal'), engine=a.engine) == []
    offered = grant(trio, a, monthly_pages=500, hours=None)
    assert offered['summary'] == ('Let Leeds HQ send faxes to numbers in Australia through us, up to 500 pages a '
                                  'month.')
    assert offered['status'] == 'Offered; waiting for Leeds HQ to accept.'
    assert offered['detail'] == 'Leeds HQ has your offer; they accept it from their Faxbot.'
    # Granting created B's dedicated sender for Leeds HQ's faxes.
    (principal,) = rows(trio.b_engine, "SELECT * FROM access_principals WHERE display_name = 'Relayed for Leeds HQ'")
    assert (principal['kind'], principal['enabled']) == ('integration', 1)

    row = offer_on(a)
    view = a.relay.view(row)
    assert row['state'] == 'offered' and view['status'] == ('Sydney office offers to send your faxes to numbers in '
                                                            'Australia as local calls; accept to use it.')
    assert view['notes'][0] == ('Sydney office will see what these faxes contain. If they are another organization, '
                                'check that your agreement with them covers this; Faxbot does not make relaying '
                                'exempt from any privacy rules.')
    assert view['price']['routes'] and view['price']['routes'][0]['country'] == 'AU'
    assert relay_candidates(DEST, Shape(1, None, 'fine', 'normal'), engine=a.engine) == []

    active = accept_offer(a, reply_number='+441134960999')
    assert active['state'] == 'active' and active['reply_number'] == '+441134960999'
    (on_b,) = trio.client.get('/direct/relay/agreements', headers=ADMIN).json()['agreements']
    assert on_b['state'] == 'active' and on_b['reply_number'] == '+441134960999'
    assert on_b['status'] == 'Active: Leeds HQ can send faxes to numbers in Australia through you.'
    # Another organization: B is told what its carrier's terms make it responsible for.
    assert any(note.startswith('Because Leeds HQ is another organization') for note in on_b['notes'])
    (candidate,) = relay_candidates(DEST, Shape(1, None, 'fine', 'normal'), engine=a.engine)
    assert candidate.key == 'relay:' + a.b and candidate.organization == 'Sydney office'
    # A number the agreement does not cover has no relay.
    assert relay_candidates('+15555550123', Shape(1, None, 'fine', 'normal'), engine=a.engine) == []

    ended = trio.client.post(f"/direct/relay/agreements/{row['id']}/withdraw", headers=ADMIN)
    assert ended.status_code == 200, ended.text
    assert ended.json()['state'] == 'withdrawn'
    assert a.relay.store.agreement(row['id'])['state'] == 'withdrawn'
    assert a.relay.view(a.relay.store.agreement(row['id']))['status'] == 'Sydney office ended this agreement.'
    assert relay_candidates(DEST, Shape(1, None, 'fine', 'normal'), engine=a.engine) == []
    # The dedicated sender is off from the moment the agreement ends.
    (principal,) = rows(trio.b_engine, "SELECT enabled FROM access_principals WHERE id = :id", id=principal['id'])
    assert principal['enabled'] == 0
    # Both sides keep each signed statement exactly as signed; nothing was rewritten.
    kinds = lambda engine: sorted((r['direction'], r['kind']) for r in rows(  # noqa: E731
        engine, 'SELECT direction, kind FROM relay_statements WHERE agreement_id = :id', id=row['id']))
    assert kinds(trio.b_engine) == [('received', 'acceptance'), ('sent', 'offer'), ('sent', 'withdrawal')]
    assert kinds(a.engine) == [('received', 'offer'), ('received', 'withdrawal'), ('sent', 'acceptance')]


def test_the_sender_can_withdraw_and_an_unreachable_partner_is_told_later(relay_trio):
    trio, a = relay_trio, relay_trio.a
    trio.partners.drop = True
    offered = grant(trio, a)
    assert offered['detail'] == 'Saved. Faxbot could not reach Leeds HQ just now and tells them as soon as it can.'
    assert a.relay.store.agreements_for(role='sender') == []
    trio.partners.drop = False
    asyncio.run(b_relay().tell_all())
    accept_offer(a)
    row = offer_on(a)
    a.relay.withdraw(row['id'])
    assert asyncio.run(a.relay.tell(a.relay.store.agreement(row['id']))) == 'told'
    (on_b,) = trio.client.get('/direct/relay/agreements', headers=ADMIN).json()['agreements']
    assert on_b['state'] == 'withdrawn' and on_b['status'] == 'Leeds HQ ended this agreement.'


# Relayed faxes ------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_relayed_fax_within_the_limits_goes_as_the_relays_own_fax_and_one_over_the_limit_is_refused(
        relay_trio):
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a, monthly_pages=2)
    job = queue(a, pages=1)
    conventional = await send(a)
    assert conventional.submissions == 0
    row = a.delivery.get(job)
    assert row['state'] == 'in_progress'  # Accepted for relaying, which is not delivered.
    sent = a.direct.store.find('outbound', row['attempt_id'])
    assert (sent['state'], sent['kind']) == ('accepted', 'relay')
    (mine,) = rows(a.engine, "SELECT * FROM relay_faxes WHERE role = 'sender'")
    assert (mine['state'], mine['job_id'], mine['destination']) == ('accepted', job, DEST)
    assert rows(a.engine, 'SELECT route FROM delivery_attempt_costs')[0]['route'] == 'relay.' + a.b

    # B queued it as its own fax under the dedicated sender, and it shows in B's Sent.
    relayed = relayed_job(trio, a)
    (fax,) = rows(trio.b_engine, 'SELECT * FROM fax_jobs WHERE id = :id', id=relayed['job_id'])
    assert (fax['to_number'], fax['pages']) == (DEST, 1)
    shown = trio.client.get(f"/fax/{relayed['job_id']}", headers=ADMIN)
    assert shown.status_code == 200, shown.text
    (owner,) = rows(trio.b_engine, """SELECT p.display_name FROM access_resources r
        JOIN access_resources personal ON personal.id = r.parent_id
        JOIN access_principals p ON p.id = personal.principal_id WHERE r.fax_job_id = :id""", id=relayed['job_id'])
    assert owner['display_name'] == 'Relayed for Leeds HQ'
    # Never filed or emailed at the relay: it is B's fax to send, not a fax B received.
    assert rows(trio.b_engine, 'SELECT id FROM inbound_faxes') == []
    assert rows(trio.b_engine, 'SELECT id FROM intake_items') == []
    assert DirectStore(trio.b_engine).unfiled() == []
    (arrival,) = rows(trio.b_engine, "SELECT kind, state FROM direct_deliveries WHERE direction = 'inbound'")
    assert arrival == {'kind': 'relay', 'state': 'accepted'}
    b_sent(trio, relayed['job_id'])
    assert trio.provider.sent == [(relayed['job_id'], DEST)]

    # Two more pages would go over the 2 pages a month: refused before anything was accepted, so A's own route
    # sends it in the same attempt, recorded as a fallback.
    second = queue(a, pages=2, number=OTHERS[0])
    conventional = await send(a)
    assert conventional.submissions == 1
    refused = a.delivery.get(second)
    assert a.direct.store.find('outbound', refused['attempt_id'])['state'] == 'refused'
    (detail,) = [r['detail'] for r in rows(a.engine, "SELECT * FROM relay_faxes WHERE state = 'refused'")]
    assert detail == ('This fax would go over the 2 pages a month Sydney office agreed to relay for you, so nothing was '
                      'accepted.')
    assert rows(a.engine, 'SELECT route FROM delivery_attempt_costs WHERE id = :id',
                id=refused['attempt_id'])[0]['route'] == 'phaxio'
    assert len(rows(trio.b_engine, "SELECT id FROM relay_faxes WHERE role = 'relay'")) == 1


@pytest.mark.asyncio
async def test_the_header_line_and_station_id_name_the_true_sender(relay_trio):
    from PIL import Image
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    job = queue(a)
    await send(a)
    relayed = relayed_job(trio, a)
    reply = a.relay.default_reply_number()
    assert reply == LEEDS
    # The engine's header line and station ID for this fax (AW's hook): Leeds HQ and its reply number.
    assert sender_identity_for(trio.b_engine, relayed['job_id']) == ('Leeds HQ', LEEDS)
    # The pages B sends are A's pages with a band above each: taller than A's own rendering of the same page.
    from app.conversion import pdf_to_tiff
    from app.direct.relay_pages import stamp_tiff
    data = Path(trio.b_delivery.configuration.read().active.values.fax_data_dir)
    sent = data / (relayed['job_id'] + '.pdf')
    assert sent.read_bytes().startswith(b'%PDF')
    original = a.data / (job + '.tiff')
    pdf_to_tiff(str(a.data / (job + '.pdf')), str(original))
    stamped = data / 'check.tiff'
    pdf_to_tiff(str(sent), str(stamped))
    with Image.open(original) as before, Image.open(stamped) as after:
        assert after.size[1] > before.size[1]
    # The stamped line itself, as the band prints it: date and time, sender, reply number, page.
    _, pages, line = stamp_tiff(_engine_page(), header='Leeds HQ', station=LEEDS, moment=datetime(2026, 10, 7, 3),
                                zone_name='Australia/Sydney')
    assert pages == 1 and line == ' 7-Oct-2026   14:00   Leeds HQ   +441134960123   p.1'
    assert sender_identity_for(trio.b_engine, uuid4().hex) is None


def _engine_page():
    import io
    from PIL import Image
    frame = Image.new('1', (1728, 200), 1)
    output = io.BytesIO()
    frame.save(output, 'TIFF', compression='group4', dpi=(204, 196))
    return output.getvalue()


# Outcomes -----------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivered_failed_before_data_and_uncertain_receipts_and_no_resend_on_uncertain(relay_trio):
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    delivered, failed, uncertain = queue(a), queue(a, number=OTHERS[0]), queue(a, number=OTHERS[1])
    for _ in range(3):
        assert (await send(a)).submissions == 0
    jobs = {row['job_id']: row for row in rows(a.engine, "SELECT * FROM relay_faxes WHERE role = 'sender'")}
    on_b = {row['message_id']: row for row in rows(trio.b_engine, "SELECT * FROM relay_faxes WHERE role = 'relay'")}
    b_job = {job: on_b[jobs[job]['message_id']]['job_id'] for job in (delivered, failed, uncertain)}

    await asyncio.to_thread(b_result, trio, b_job[delivered], 'success')
    await asyncio.to_thread(b_result, trio, b_job[failed], 'failed')
    await asyncio.to_thread(b_result, trio, b_job[uncertain], 'uncertain')
    assert await asyncio.to_thread(b_report) == 3

    assert a.delivery.get(delivered)['state'] == 'success'
    assert a.delivery.get(failed)['state'] == 'failed'
    (job,) = rows(a.engine, 'SELECT error FROM fax_jobs WHERE id = :id', id=failed)
    assert job['error'] == "Sydney office's call failed before any page was sent: The fax machine was busy."
    # Uncertain waits for a person on A; it is never sent again by another route.
    assert a.delivery.get(uncertain)['state'] == 'reconciliation_required'
    conventional = Conventional(a.delivery)
    transport = PlannedTransport(conventional, RelayRoute(a.direct, delivery=lambda: a.delivery), a.b)
    assert await OutboundWorker(a.delivery, transport).step() is False
    assert conventional.submissions == 0 and len(trio.provider.sent) == 3
    states = {row['job_id']: row['state'] for row in rows(a.engine, "SELECT * FROM relay_faxes WHERE role = 'sender'")}
    assert states == {delivered: 'delivered', failed: 'failed_before_data', uncertain: 'uncertain'}
    (signed,) = rows(a.engine, "SELECT statement FROM relay_statements WHERE kind = 'outcome' AND message_id = :m",
                     m=jobs[delivered]['message_id'])
    outcome = json.loads(signed['statement'])
    assert (outcome['status'], outcome['pages']) == ('delivered', 1)
    # The relay answers a signed question about a fax's outcome too.
    assert await RelayReconciler(a.direct, a.delivery).relay.ask_outcome(
        a.relay.store.fax(role='sender', message_id=jobs[delivered]['message_id'])) == 'delivered'
    # Every receipt reached A once; B does not send them again.
    assert await asyncio.to_thread(b_report) == 0


@pytest.mark.asyncio
async def test_a_fax_that_failed_before_any_page_takes_the_senders_next_route_and_says_so(relay_trio, monkeypatch):
    """With the installation's fallback rule (as the running app installs it), a definite failure before any page
    lets the sender's next route send the fax, recorded with the relay's sentence; an uncertain one never does."""
    from api.app.outbound_store import OutboundStore as SenderStore
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    failed, uncertain = queue(a), queue(a, number=OTHERS[0])
    # Leeds HQ's rule finds another route for its own faxes (the relay's faxes keep their own rules).
    monkeypatch.setattr(SenderStore, 'fallback_policy', lambda job_id, attempt_id: job_id in (failed, uncertain))
    await send(a)
    await send(a)
    on_b = {row['destination']: row for row in rows(trio.b_engine, "SELECT * FROM relay_faxes WHERE role = 'relay'")}
    await asyncio.to_thread(b_result, trio, on_b[DEST]['job_id'], 'failed')
    await asyncio.to_thread(b_result, trio, on_b[OTHERS[0]]['job_id'], 'uncertain')
    await asyncio.to_thread(b_report)
    assert a.delivery.get(failed)['state'] == 'ready'
    (moved,) = [event for event in a.delivery.history(failed) if event['kind'] == 'route_fallback']
    assert json.loads(moved['details'])['reason'] == ("Sydney office's call failed before any page was sent: The fax "
                                                      'machine was busy.')
    assert a.delivery.get(uncertain)['state'] == 'reconciliation_required'
    assert 'route_fallback' not in [event['kind'] for event in a.delivery.history(uncertain)]


@pytest.mark.asyncio
async def test_costs_on_both_sides(relay_trio):
    from api.app.routing.costs import RateCard, parse_amount
    from api.app.routing.store import RouteStore
    trio, a = relay_trio, relay_trio.a
    # The relay's own price for a local Australian call: 3 cents a page on its provider.
    RouteStore(trio.b_engine).replace_cards([RateCard(None, 'phaxio', 'outbound', 'Phaxio', 'USD', 0,
                                                      parse_amount('0.03'), 0, 60, 0, None, datetime(2026, 10, 7))])
    await asyncio.to_thread(agree, trio, a)
    queue(a, pages=2)
    await send(a)
    relayed = relayed_job(trio, a)
    await asyncio.to_thread(b_result, trio, relayed['job_id'], 'success')
    await asyncio.to_thread(b_report)
    (b_costs,) = trio.client.get('/direct/relay/costs', headers=ADMIN).json()['agreements']
    assert b_costs['role'] == 'relay' and b_costs['faxes'] == 1
    assert b_costs['sentence'].startswith('Relayed for Leeds HQ: 1 fax')
    (a_costs,) = a.relay.costs()
    assert a_costs['role'] == 'sender' and a_costs['sentence'].startswith('Sent through Sydney office: 1 fax')
    (mine,) = rows(a.engine, "SELECT * FROM relay_faxes WHERE role = 'sender'")
    (theirs,) = rows(trio.b_engine, "SELECT * FROM relay_faxes WHERE role = 'relay'")
    # The sender's cost comes from the relay's signed price; the relay's charge travels in its signed receipt.
    assert (mine['charge_micros'], mine['charge_currency']) == (theirs['charge_micros'], theirs['charge_currency'])
    # Two pages at the relay's signed 3 cents, against Leeds HQ's own call abroad; amounts in US dollars are named
    # as such on an installation outside the US, and never converted.
    assert (mine['cost_micros'], mine['cost_currency']) == (60_000, 'USD')
    assert mine['own_route_currency'] == 'USD' and mine['own_route_micros'] > mine['cost_micros']
    own = relay_money(mine['own_route_micros'])
    assert a_costs['sentence'] == f'Sent through Sydney office: 1 fax, 0.06 USD, against about {own} calling from the UK.'
    assert b_costs['sentence'] == 'Relayed for Leeds HQ: 1 fax, 0.06 USD.'


def relay_money(micros):
    from api.app.direct.relay import money_for
    return money_for(micros, 'USD', 'GB')


# Off by default, and the suggestion ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_relaying_is_off_by_default_and_faxbot_suggests_it_from_a_signed_quote(relay_trio):
    trio, a = relay_trio, relay_trio.a
    assert a.relay.recommendations() == []
    # A sent faxes to Australia on its own route lately, at a known cost.
    for number in (DEST, *OTHERS[:2]):
        queue(a, number=number)
        own = RoutedTransport(Conventional(a.delivery), direct=None, relay=None)
        assert await OutboundWorker(a.delivery, own).step() is True
    assert len(rows(a.engine, 'SELECT id FROM delivery_attempt_costs')) == 3
    with a.engine.begin() as connection:
        connection.execute(sa.text("UPDATE delivery_attempt_costs SET outcome = 'success', "
                                   "estimated_cost_micros = 3000000, currency = :c"),
                           {'c': _relay_currency(trio, a)})
    assert await a.relay.ask_quote(a.b, ['AU']) is True
    (suggestion,) = a.relay.recommendations()
    assert suggestion['partner'] == 'Sydney office' and suggestion['faxes'] == 3
    assert suggestion['sentence'].startswith('Faxes to numbers in Australia would have cost about ')
    assert suggestion['sentence'].endswith(' less through Sydney office.')
    # Still nothing is relayed: there is no agreement.
    assert relay_candidates(DEST, Shape(1, None, 'fine', 'normal'), engine=a.engine) == []


def _relay_currency(trio, a):
    """The currency the relay's signed prices use, so the suggestion can compare like with like."""
    relay = b_relay()
    revision, bound = relay._bound()
    from app.direct.relay import price_body
    body = price_body(trio.b_engine, revision.values, bound, ['AU'])
    priced = [entry['terms']['currency'] for entry in body['routes'] if entry['terms']]
    assert priced, 'The relay has no price for Australian numbers.'
    return priced[0]


# Sending together across organizations -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_partners_faxes_go_in_one_call_each_with_its_own_header_receipt_and_cost_share(
        relay_trio, monkeypatch):
    trio, a, c = relay_trio, relay_trio.a, relay_trio.c
    # The destination takes combined faxes from mixed senders (AC's setting, on B).
    from app.batching import acceptance
    from app.batching.store import BatchingSettings, HoldPlan
    BatchingSettings(trio.b_engine).save(DEST, enabled=True, recipient_agreed=True, mixed_senders=True,
                                         actor='principal:test', actor_name='Owner')
    # B's provider here is a stand-in, not a SIP trunk; the hold AC's policy would give a trunk is given here.
    held = []

    def hold(engine, revision, profile, *, destination, pages, actor, send_now=False):
        held.append(actor.replay_scope)
        return HoldPlan(destination, actor.replay_scope, None, pages, False, 600)
    monkeypatch.setattr(acceptance, 'hold_plan', hold)
    await asyncio.to_thread(agree, trio, a, together=True)
    await asyncio.to_thread(agree, trio, c, together=True)
    queue(a, pages=1)
    queue(c, pages=2)
    await send(a)
    await send(c)
    assert sorted(held) == sorted(['relay:' + a.on_b, 'relay:' + c.on_b])
    first, second = relayed_job(trio, a), relayed_job(trio, c)
    # One call carries both documents, in order.
    claim = await asyncio.to_thread(lambda: trio.b_delivery.claim('test-worker',
                                                                  now=datetime.utcnow() + timedelta(minutes=11)))
    assert claim is not None and sorted(member.job_id for member in claim.everyone) == sorted(
        [first['job_id'], second['job_id']])
    # Each document keeps its own header naming its real sender; the shared call keeps the relay's own number.
    assert sender_identity_for(trio.b_engine, first['job_id']) is None
    from app.batching.store import call_members
    names = {row['id']: row['sender_name'] for row in call_members(trio.b_engine, claim.attempt_id)}
    assert names == {first['job_id']: 'Relayed for Leeds HQ', second['job_id']: 'Relayed for Harbour Clinic'}
    # The call succeeds; each fax gets its own receipt and its share of the call's charge.
    from app.routing.store import RouteStore
    routes = RouteStore(trio.b_engine)
    routes.record_decision(attempt_id=claim.attempt_id, job_id=claim.job_id, destination=DEST, route='phaxio',
                           reason='configured', provider_id='phaxio')
    with trio.b_engine.begin() as connection:
        connection.execute(sa.text("UPDATE delivery_attempt_costs SET outcome = 'success', "
                                   "estimated_cost_micros = 500000, currency = 'AUD' WHERE id = :id"),
                           {'id': claim.attempt_id})
    assert trio.b_delivery.begin_submission(claim)
    trio.b_delivery.record_receipt(claim, provider_sid='PX-SHARED', status='success')
    assert await asyncio.to_thread(b_report) == 2
    shares = {row['job_id']: row for row in rows(trio.b_engine, "SELECT * FROM relay_faxes WHERE role = 'relay'")}
    # Split by pages, each counting its separator page: 2 of 5 and 3 of 5 of the call.
    assert (shares[first['job_id']]['charge_micros'], shares[second['job_id']]['charge_micros']) == (200000, 300000)
    assert shares[first['job_id']]['shared'] == 1
    for side, share in ((a, 200000), (c, 300000)):
        (mine,) = rows(side.engine, "SELECT * FROM relay_faxes WHERE role = 'sender'")
        assert (mine['state'], mine['charge_micros'], mine['charge_currency']) == ('delivered', share, 'AUD')
        assert side.delivery.get(mine['job_id'])['state'] == 'success'


@pytest.mark.asyncio
async def test_a_partner_that_did_not_opt_in_never_shares_a_call(relay_trio, monkeypatch):
    trio, a = relay_trio, relay_trio.a
    from app.batching import acceptance
    held = []
    monkeypatch.setattr(acceptance, 'hold_plan', lambda *args, **kwargs: held.append(kwargs) or None)
    await asyncio.to_thread(agree, trio, a, together=False)
    queue(a)
    await send(a)
    assert held == []  # Never asked: it goes on its own straight away.
    assert relayed_job(trio, a)['shared'] == 0


# Withdrawal and lost answers -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_withdrawal_lets_accepted_faxes_finish_and_refuses_new_ones_before_anything_is_accepted(relay_trio):
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    queue(a)
    await send(a)
    accepted = relayed_job(trio, a)
    # B ends the agreement while A cannot hear it, so A still believes it may relay.
    trio.partners.drop = True
    (agreement,) = trio.client.get('/direct/relay/agreements', headers=ADMIN).json()['agreements']
    ended = trio.client.post(f"/direct/relay/agreements/{agreement['id']}/withdraw", headers=ADMIN)
    assert ended.json()['state'] == 'withdrawn'
    assert offer_on(a)['state'] == 'active'
    # The fax B accepted before the withdrawal still goes, as B's own fax, and gets its receipt.
    await asyncio.to_thread(b_result, trio, accepted['job_id'], 'success')
    trio.partners.drop = False
    assert await asyncio.to_thread(b_report) == 1
    assert rows(a.engine, "SELECT state FROM relay_faxes WHERE role = 'sender'") == [{'state': 'delivered'}]
    # A new fax is refused, signed, before anything is accepted: A's own route sends it in the same attempt, and
    # the fallback is recorded, never hidden.
    second = queue(a, number=OTHERS[0])
    conventional = await send(a)
    assert conventional.submissions == 1
    row = a.delivery.get(second)
    assert a.direct.store.find('outbound', row['attempt_id'])['state'] == 'refused'
    (refused,) = rows(a.engine, "SELECT detail FROM relay_faxes WHERE role = 'sender' AND state = 'refused'")
    assert refused['detail'] == 'Sydney office has no active relay agreement with you, so nothing was accepted.'
    assert rows(a.engine, 'SELECT route, route_reason FROM delivery_attempt_costs WHERE id = :id',
                id=row['attempt_id']) == [{'route': 'phaxio', 'route_reason': 'alternative'}]
    assert len(rows(trio.b_engine, "SELECT id FROM relay_faxes WHERE role = 'relay'")) == 1


@pytest.mark.asyncio
async def test_a_lost_answer_is_asked_about_and_an_accepted_fax_waits_for_its_receipt(relay_trio):
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    job = queue(a)
    a.to_b.mode = 'lose_answer'
    conventional = await send(a)
    assert a.delivery.get(job)['state'] == 'reconciliation_required' and conventional.submissions == 0
    a.to_b.mode = 'normal'
    reconciler = RelayReconciler(a.direct, a.delivery)
    (lost,) = await asyncio.to_thread(reconciler._lost)
    assert await reconciler.reconcile(lost) == 'accepted'
    # Accepted for relaying is in progress, not delivered, and nothing was sent twice.
    assert a.delivery.get(job)['state'] == 'in_progress'
    assert len(rows(trio.b_engine, "SELECT id FROM relay_faxes WHERE role = 'relay'")) == 1
    relayed = relayed_job(trio, a)
    await asyncio.to_thread(b_result, trio, relayed['job_id'], 'success')
    await asyncio.to_thread(b_relay().settle)
    # The receipt has not been sent yet; A asks for it and settles the fax.
    (mine,) = rows(a.engine, "SELECT * FROM relay_faxes WHERE role = 'sender'")
    assert await reconciler.relay.ask_outcome(mine) == 'delivered'
    assert a.delivery.get(job)['state'] == 'success'


@pytest.mark.asyncio
async def test_a_premium_rate_number_is_never_relayed(relay_trio):
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    job = queue(a, number=PREMIUM)
    conventional = await send(a)
    assert conventional.submissions == 1
    (refused,) = rows(a.engine, "SELECT detail FROM relay_faxes WHERE state = 'refused'")
    assert refused['detail'] == 'Sydney office does not relay faxes to premium-rate numbers, so nothing was accepted.'
    assert a.delivery.get(job)['state'] == 'in_progress'


# The relay's own sending rules -------------------------------------------------------------------------------------

def b_rules(trio, document):
    """Publish B's organization rules (Providers → Rules)."""
    from api.app.rules.store import RuleStore
    store = RuleStore(trio.b_engine)
    current = store.draft('organization', '')
    draft = store.save_draft('organization', '', document, expected_version=current['version'] if current else 0)
    active = store.active('organization', '')
    store.publish('organization', '', expected_active_revision=active['number'] if active else None,
                  expected_draft_version=draft['version'])


def b_decision(trio, job_id):
    (decision,) = rows(trio.b_engine, 'SELECT outcome, actor_principal_id, facts FROM fax_job_rule_decisions '
                                      'WHERE job_id = :id', id=job_id)
    return decision


def b_relays_through(trio, organization, number):
    """B itself sends through a partner relay to Australia, as cheap as can be: an active agreement where B is the
    sender, with a signed price (``direct.relay.relay_candidates`` offers it for any of B's own faxes)."""
    from api.app.direct.relay import RelayStore, parse_terms
    from api.tests.test_partner_relay_terms import _price
    now = datetime.utcnow()
    peer_id = uuid4().hex
    peers = sa.Table('direct_peers', sa.MetaData(), autoload_with=trio.b_engine)
    with trio.b_engine.begin() as connection:
        connection.execute(peers.insert().values(
            id=peer_id, organization=organization, phone_number=number, endpoint_url=f'https://{peer_id[:6]}.example',
            signing_key=peer_id[:43].ljust(43, 'a'), exchange_key='e' * 43, state='verified', challenge_failures=0,
            version=1, created_at=now, updated_at=now))
    offer = {'statement': json.dumps({'type': 'relay_offer', 'price': _price(1_000, now=now)}), 'signature': 's'}
    RelayStore(trio.b_engine).create(agreement_id=uuid4().hex, peer_id=peer_id, role='sender', state='active',
                                     terms=parse_terms({'countries': ['AU']}, zone_name='Australia/Sydney'),
                                     statement=offer, direction='received', now=now)
    assert [c.peer_id for c in relay_candidates(DEST, Shape(1, None, 'fine', 'normal'), engine=trio.b_engine)] == [
        peer_id]
    return peer_id


@pytest.mark.asyncio
@pytest.mark.parametrize('limit, reason', [
    ({'never': ['phaxio']}, 'No account is allowed for this fax: the rule ‘Relayed faxes’ removes every one it could '
                            'use. It waits for you in Sent.'),
    ({'cap_cost': {'currency': 'AUD', 'amount': '0.01'}}, None),
])
async def test_the_relays_own_sending_rules_decide_each_relayed_fax_in_its_acceptance(relay_trio, limit, reason):
    """B's limits apply to the faxes it relays, like any fax it sends: one its rules allow no route for is accepted
    and held in B's Sent with its sentence, never sent outside them."""
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    b_rules(trio, {'format': 1, 'limits': [{'id': 'l-relayed', 'name': 'Relayed faxes', 'on': True, 'when': {},
                                            'then': limit}]})
    queue(a)
    await send(a)
    relayed = relayed_job(trio, a)
    assert relayed['state'] == 'accepted'
    decision = b_decision(trio, relayed['job_id'])
    (agreement,) = trio.client.get('/direct/relay/agreements', headers=ADMIN).json()['agreements']
    sender = rows(trio.b_engine, 'SELECT principal_id FROM relay_agreements WHERE id = :id',
                  id=agreement['id'])[0]['principal_id']
    assert decision['outcome'] == 'blocked' and decision['actor_principal_id'] == sender
    assert json.loads(decision['facts'])['sender'] == {'principal_id': sender, 'kind': 'system', 'key_id': None,
                                                       'groups': []}
    (hold,) = rows(trio.b_engine, 'SELECT kind, state, reason FROM outbound_holds WHERE job_id = :id',
                   id=relayed['job_id'])
    assert (hold['kind'], hold['state']) == ('no_route', 'open')
    assert hold['reason'] == reason if reason else hold['reason'].startswith('No account is estimated to cost less')
    assert trio.b_delivery.get(relayed['job_id'])['state'] == 'ready'
    assert await asyncio.to_thread(trio.b_delivery.claim, 'test-worker') is None
    assert trio.provider.sent == []


@pytest.mark.asyncio
async def test_refusing_a_relayed_fax_the_relays_rules_hold_lets_the_sender_take_its_own_next_route(
        relay_trio, monkeypatch):
    """While B's rules hold a relayed fax it waits in B's Sent and A's fax reads accepted for relaying. When B's
    administrator refuses it, nothing was sent: A hears failed before any page and its own next route sends it."""
    from api.app.outbound_store import OutboundStore as SenderStore
    from api.app.routing.holds import HoldStore
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    b_rules(trio, {'format': 1, 'limits': [{'id': 'l-relayed', 'name': 'Relayed faxes', 'on': True, 'when': {},
                                            'then': {'never': ['phaxio']}}]})
    job = queue(a)
    monkeypatch.setattr(SenderStore, 'fallback_policy', lambda job_id, attempt_id: job_id == job)
    await send(a)
    assert a.delivery.get(job)['state'] == 'in_progress'
    relayed = relayed_job(trio, a)
    (hold,) = HoldStore(trio.b_delivery).holds(job_id=relayed['job_id'])
    await asyncio.to_thread(lambda: HoldStore(trio.b_delivery).refuse(
        hold['id'], version=hold['version'], actor=None, actor_name='Ada Admin', reason='We do not send these'))
    assert await asyncio.to_thread(b_report) == 1
    (mine,) = rows(a.engine, "SELECT state, detail FROM relay_faxes WHERE role = 'sender'")
    assert mine['state'] == 'failed_before_data'
    assert mine['detail'] == ("Sydney office's call failed before any page was sent: Refused by Ada Admin: We do not "
                              'send these')
    assert a.delivery.get(job)['state'] == 'ready'  # A's own next route takes it
    assert trio.provider.sent == []


@pytest.mark.asyncio
async def test_a_relayed_fax_is_never_relayed_again_even_when_the_relays_rules_name_a_relay_first(relay_trio):
    trio, a = relay_trio, relay_trio.a
    await asyncio.to_thread(agree, trio, a)
    onward = await asyncio.to_thread(b_relays_through, trio, 'Perth office', '+61855501234')
    b_rules(trio, {'format': 1, 'routes': [{'id': 'r-onward', 'name': 'Relay to Australia', 'on': True,
                                            'when': {}, 'then': {'try_in_order': ['relay:' + onward, 'phaxio']}}]})
    queue(a)
    await send(a)
    relayed = relayed_job(trio, a)
    assert b_decision(trio, relayed['job_id'])['outcome'] == 'route'
    b_sent(trio, relayed['job_id'])
    assert trio.provider.sent == [(relayed['job_id'], DEST)]  # by B's own account, never through Perth
    assert rows(trio.b_engine, "SELECT id FROM direct_deliveries WHERE direction = 'outbound'") == []
    assert rows(trio.b_engine, "SELECT id FROM relay_faxes WHERE role = 'sender'") == []


def test_signed_relay_times_are_utc_whatever_the_servers_time_zone(monkeypatch, tmp_path):
    """A naive UTC time (utcnow) is signed as that UTC time, also on a server whose TZ is not UTC."""
    import time
    from datetime import datetime
    from app.direct.relay import price_body, signed_time
    from app.schema import create_database_engine, upgrade_schema
    engine = create_database_engine('sqlite:///' + str(tmp_path / 'relay.db'))
    upgrade_schema(engine)
    monkeypatch.setenv('TZ', 'America/Denver')
    time.tzset()
    try:
        moment = datetime(2026, 10, 7, 18, 30, 5)
        assert signed_time(moment) == '2026-10-07T18:30:05Z'
        body = price_body(engine, SimpleNamespace(fax_default_country='US'), None, [], now=moment)
        assert (body['priced_at'], body['valid_until']) == ('2026-10-07T18:30:05Z', '2026-11-07T18:30:05Z')
    finally:
        monkeypatch.undo()
        time.tzset()
        engine.dispose()
