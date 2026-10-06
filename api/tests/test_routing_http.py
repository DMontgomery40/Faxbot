"""Delivery route administration over the real HTTPS stack and access policy."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import main
from app.routing.store import RouteStore


BOOTSTRAP = 'synthetic-routing-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}
TELNYX = {'provider_id': 'sip', 'label': 'Telnyx SIP trunk', 'per_minute': '0.005',
          'billing_increment_seconds': 60, 'source_url': 'https://telnyx.com/pricing/elastic-sip',
          'captured_on': '2026-10-03'}
PHAXIO = {'provider_id': 'phaxio', 'label': 'Phaxio', 'per_page': '0.07', 'captured_on': '2026-10-03'}


@pytest.fixture
def client(isolated_installation, monkeypatch, tmp_path):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_ROUTES': 'sip, signalwire',
                        'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def scoped_key(client, scopes):
    response = client.post('/admin/api-keys', headers=ADMIN, json={'name': 'synthetic', 'scopes': scopes})
    assert response.status_code == 200, response.text
    return {'X-API-Key': response.json()['token']}


def test_shipped_rate_cards_are_in_place_before_the_first_request(client):
    """The seed finishes during startup, so a save right after start is never overwritten by it."""
    shipped = client.get('/routing/rate-cards', headers=ADMIN).json()['cards']
    assert shipped, 'starting rate cards should already be loaded'
    saved = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, PHAXIO]})
    assert saved.status_code == 200, saved.text
    current = client.get('/routing/rate-cards', headers=ADMIN).json()['cards']
    assert sorted(card['provider_id'] for card in current) == ['phaxio', 'sip']


def test_rate_cards_round_trip_as_money(client):
    response = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, PHAXIO]})
    assert response.status_code == 200, response.text
    cards = {card['provider_id']: card for card in response.json()['cards']}
    assert cards['sip']['per_minute'] == '0.005' and cards['sip']['per_page'] == '0.00'
    assert cards['phaxio']['per_page'] == '0.07' and cards['sip']['captured_on'] == '2026-10-03'
    assert client.get('/routing/rate-cards', headers=ADMIN).json()['cards'] == response.json()['cards']
    bad = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [{**PHAXIO, 'per_page': '-1'}]})
    assert bad.status_code == 400 and 'six decimal places' in bad.json()['detail']


def test_the_planned_route_faxbot_send_shows_is_the_numbers_preferred_route(client):
    """faxbot send prints the first recommended route as its Planned route: a number's chosen route comes
    first even when another route is cheaper (live, 5 October: Provider HumbleFax was shown instead)."""
    from app.cli.commands import fax
    assert client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, PHAXIO]}).status_code == 200
    chosen = client.patch('/routing/destinations/+12025550123', headers=ADMIN, json={'preferred_route': 'phaxio'})
    assert chosen.status_code == 200, chosen.text

    class Api:
        def get(self, path, params=None):
            return client.get(path, headers=ADMIN, params=params).json()
    view = Api().get('/routing/destinations/+12025550123', params={'pages': 2})
    assert (view['recommended_routes'][0]['route'], view['recommended_routes'][0]['reason']) == ('phaxio', 'preferred')
    planned = fax._planned_route(Api(), {'to_number': '+12025550123', 'pages': 2})
    assert planned == ('Planned route', view['recommended_routes'][0]['label'])


def test_destination_recommendation_ranks_configured_routes_by_cost(client):
    cards = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, PHAXIO]})
    assert cards.status_code == 200, cards.text
    response = client.patch('/routing/destinations/+1 (202) 555-0123', headers=ADMIN,
                            json={'display_name': 'County clinic', 'preferred_route': None})
    assert response.status_code == 200, response.text
    assert response.json()['number'] == '+12025550123' and response.json()['version'] == 1
    view = client.get('/routing/destinations/+12025550123', headers=ADMIN).json()
    assert view['display_name'] == 'County clinic'
    assert [(route['route'], route['reason']) for route in view['recommended_routes']] == [
        ('sip', 'known_cheapest'), ('phaxio', 'alternative'), ('signalwire', 'alternative')]
    assert view['recommended_routes'][0]['estimated_cost_one_page'] == {'currency': 'USD', 'amount': '0.005'}
    assert view['recommended_routes'][0]['explanation'].endswith('.')
    assert [route['route'] for route in view['available_routes']] == ['phaxio', 'sip', 'signalwire']
    listing = client.get('/routing/destinations', headers=ADMIN).json()
    assert [item['number'] for item in listing['destinations']] == ['+12025550123']
    stale = client.patch('/routing/destinations/+12025550123', headers=ADMIN, json={'notes': 'x', 'version': 0})
    assert stale.status_code == 409
    assert client.patch('/routing/destinations/555', headers=ADMIN, json={}).status_code == 400


def test_costs_report_totals_by_provider(client):
    engine = main.app.state.configuration_runtime.manager.store.engine
    store = RouteStore(engine)
    assert store.cost_totals(datetime(2026, 1, 1)) == []
    response = client.get('/routing/costs', headers=ADMIN, params={'since': '2026-01-01T00:00:00Z'})
    assert response.status_code == 200 and response.json()['providers'] == []


def test_routing_requires_settings_permissions(client):
    sender = scoped_key(client, ['fax:send'])
    for method, path, body in [('GET', '/routing/destinations', None), ('GET', '/routing/costs', None),
                               ('GET', '/routing/rate-cards', None),
                               ('PUT', '/routing/rate-cards', {'cards': []}),
                               ('PATCH', '/routing/destinations/+12025550123', {'notes': 'x'})]:
        response = client.request(method, path, json=body, headers=sender)
        assert response.status_code == 403, (method, path, response.status_code)
    assert client.get('/routing/destinations').status_code == 401


def test_route_fallback_policy_lives_with_the_application(isolated_installation, monkeypatch):
    import time
    from app.outbound_store import OutboundStore
    from app.routing.fallback import FallbackPolicy
    monkeypatch.setenv('API_KEY', BOOTSTRAP)
    monkeypatch.setenv('REQUIRE_API_KEY', 'true')
    assert OutboundStore.fallback_policy is None
    with TestClient(main.app, base_url='https://testserver'):
        deadline = time.monotonic() + 5
        while OutboundStore.fallback_policy is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert isinstance(OutboundStore.fallback_policy, FallbackPolicy)
    assert OutboundStore.fallback_policy is None


# Carrier call charges, per-fax costs and honest recommendations -------------------------

def _engine():
    return main.app.state.configuration_runtime.manager.store.engine


def _sent_fax_with_call(client, answer, end):
    """A fax sent through the API (held in test mode) with one finished SIP attempt and its call record."""
    from datetime import timedelta
    from uuid import uuid4
    from app.routing.carriers import CarrierChargeStore
    from app.routing.store import CaptureTarget
    sent = client.post('/fax', headers=ADMIN, data={'to': '+12025550123'},
                       files={'file': ('note.txt', b'Synthetic\n', 'text/plain')})
    assert sent.status_code == 202, sent.text
    job, attempt = sent.json()['id'], uuid4().hex
    routes, carriers = RouteStore(_engine()), CarrierChargeStore(_engine())
    start = answer - timedelta(seconds=5)
    with _engine().begin() as connection:
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='success',
                                                           created_at=start, submitted_at=start, completed_at=end))
        connection.execute(carriers.calls.insert().values(
            id=uuid4().hex, direction='outbound', call_id=attempt, job_id=job, attempt_id=attempt,
            trunk_preset='telnyx', did='+13035550100', caller='+13035550100', called='+12025550123',
            started_at=start, answered_at=answer, ended_at=end, disposition='answered',
            connected_seconds=int((end - answer).total_seconds()), t38='yes', pages=1, fax_status='SUCCESS',
            fax_preference=0, created_at=start, updated_at=end))
    routes.capture(CaptureTarget(attempt, job, '+12025550123', 'sip', None, 'success', 1, start, end, False))
    return job, attempt


@pytest.fixture
def telnyx_client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx', 'TELNYX_API_KEY': 'KEYsynthetic-telnyx',
                        'MAX_REQUESTS_PER_MINUTE': '0', 'FAXBOT_CONSOLE_ORIGINS': 'https://testserver'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def test_reconcile_needs_a_telnyx_key_and_write_permission(client):
    refused = client.post('/routing/reconcile', headers=ADMIN)
    assert refused.status_code == 409
    assert refused.json()['detail'].startswith('Faxbot needs a Telnyx API key to read call charges.')
    reader = scoped_key(client, ['fax:send'])
    assert client.post('/routing/reconcile', headers=reader).status_code == 403
    costs = client.get('/routing/costs', headers=ADMIN).json()
    assert costs['carrier_charges']['readable'] is False and costs['received'] == []


def test_reconcile_records_telnyx_charges_and_the_console_reads_them(telnyx_client, monkeypatch):
    from datetime import timedelta
    from app.routing import http as routing_http
    from api.tests.test_carrier_charges import FakeTelnyx, telnyx as record
    answer = datetime.utcnow().replace(microsecond=0) - timedelta(hours=1)
    end = answer + timedelta(seconds=45)
    job, attempt = _sent_fax_with_call(telnyx_client, answer, end)
    keys = []
    payload = record('rec-live', 'outbound', answer - timedelta(seconds=1), answer, end, '0.005',
                     cli='+13035550100', cld='+12025550123')

    def source(api_key):
        keys.append(api_key())
        return FakeTelnyx([payload])
    monkeypatch.setattr(routing_http, 'carrier_source', source)
    before = telnyx_client.get(f'/routing/faxes/{job}/cost', headers=ADMIN)
    assert before.status_code == 200, before.text
    before = before.json()
    assert before['summary'] == 'Cost not reported yet.' and before['reported_cost'] == []
    response = telnyx_client.post('/routing/reconcile', headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()['summary'] == 'Checked 1 call: 1 new charge recorded.'
    assert keys == ['KEYsynthetic-telnyx']
    cost = telnyx_client.get(f'/routing/faxes/{job}/cost', headers=ADMIN).json()
    assert cost['summary'] == 'Telnyx charged $0.005 for this call.'
    assert cost['reported_cost'] == [{'currency': 'USD', 'amount': '0.005'}]
    again = telnyx_client.post('/routing/reconcile', headers=ADMIN).json()
    assert again['charges_recorded'] == 0
    costs = telnyx_client.get('/routing/costs', headers=ADMIN).json()
    sip = next(item for item in costs['providers'] if item['provider_id'] == 'sip')
    assert sip['carrier'] == 'Telnyx' and sip['reported_cost'] == [{'currency': 'USD', 'amount': '0.005'}]
    assert (sip['attempts_with_reported_cost'], sip['awaiting_carrier_bill'], sip['unmatched_charges']) == (1, 0, 0)
    assert sip['billed_minutes'] == 1.0
    assert costs['carrier_charges'] == {'carrier': 'Telnyx', 'supported': True, 'readable': True}
    assert 'KEYsynthetic-telnyx' not in response.text + str(costs)
    assert telnyx_client.get('/routing/faxes/' + '0' * 32 + '/cost', headers=ADMIN).status_code == 404
    assert telnyx_client.get(f'/routing/faxes/{job}/cost').status_code == 401
    # The Sent list reads its costs in one request; unknown and unreadable faxes are left out.
    batch = telnyx_client.get('/routing/fax-costs', headers=ADMIN, params={'ids': f'{job},{"0" * 32},{job}'})
    assert batch.status_code == 200, batch.text
    assert {key: value['summary'] for key, value in batch.json()['costs'].items()} == {
        job: 'Telnyx charged $0.005 for this call.'}
    assert batch.json()['costs'][job]['reported_cost'] == [{'currency': 'USD', 'amount': '0.005'}]
    outsider = scoped_key(telnyx_client, ['inbound:list'])
    assert telnyx_client.get('/routing/fax-costs', headers=outsider, params={'ids': job}).json() == {'costs': {}}
    assert telnyx_client.get('/routing/fax-costs', params={'ids': job}).status_code == 401


def _two_faxes_in_one_call(client, answer, end):
    """Two faxes to one number that shared one SIP call placed by the first fax's attempt."""
    from uuid import uuid4
    first, call = _sent_fax_with_call(client, answer, end)  # the fax that placed the call
    second = client.post('/fax', headers=ADMIN, data={'to': '+12025550123'},
                                files={'file': ('note.txt', b'Synthetic second\n', 'text/plain')}).json()['id']
    routes, rider = RouteStore(_engine()), uuid4().hex
    members = routes.batch_members()
    with _engine().begin() as connection:
        connection.execute(routes.attempts.insert().values(id=rider, job_id=second, sequence=1, phase='success',
                                                           created_at=answer, submitted_at=answer, completed_at=end))
        for document, (job, attempt) in enumerate(((first, call), (second, rider)), start=1):
            connection.execute(members.insert().values(
                id=job, phone_number='+12025550123', sender_scope='key:env', pages=1, urgent=0, hold_until=answer,
                state='together', batch_id=call, attempt_id=attempt, document_number=document, documents=2,
                first_page=2 * document - 1, last_page=2 * document, reference='Faxbot ' + job[:8],
                created_at=answer, updated_at=end))
    # The second fax rode in the first fax's call: its own attempt is never costed on its own.
    routes.record_decision(attempt_id=rider, job_id=second, destination='+12025550123', route='sip',
                           reason='configured', provider_id='sip')
    return first, second, call


def test_faxes_sent_together_each_cost_their_share_and_the_call_counts_once(telnyx_client, monkeypatch):
    """Two faxes in one Telnyx call: each shows its share by pages, an estimate until Telnyx reports the call."""
    from datetime import timedelta
    from app.routing import http as routing_http
    from api.tests.test_carrier_charges import FakeTelnyx, telnyx as record
    answer = datetime.utcnow().replace(microsecond=0) - timedelta(hours=1)
    end = answer + timedelta(seconds=45)
    first, second, call = _two_faxes_in_one_call(telnyx_client, answer, end)
    routes = RouteStore(_engine())
    whole = routes.decision(call)['estimated_cost_micros']
    assert whole and whole % 2 == 0

    def costs():
        batch = telnyx_client.get('/routing/fax-costs', headers=ADMIN, params={'ids': f'{first},{second}'}).json()['costs']
        single = {job: telnyx_client.get(f'/routing/faxes/{job}/cost', headers=ADMIN).json() for job in (first, second)}
        assert batch == single
        return single

    half = f'{whole / 2_000_000:g}'
    before = costs()
    for job in (first, second):
        assert before[job]['state'] == 'waiting' and before[job]['reported_cost'] == []
        assert before[job]['estimated_cost'] == [{'currency': 'USD', 'amount': half}]
    monkeypatch.setattr(routing_http, 'carrier_source', lambda api_key: FakeTelnyx([record(
        'rec-shared', 'outbound', answer - timedelta(seconds=1), answer, end, '0.005',
        cli='+13035550100', cld='+12025550123')]))
    assert telnyx_client.post('/routing/reconcile', headers=ADMIN).status_code == 200
    after = costs()
    for job in (first, second):
        assert after[job]['state'] == 'reported'
        assert after[job]['reported_cost'] == [{'currency': 'USD', 'amount': '0.0025'}]
        assert after[job]['summary'] == "Telnyx charged $0.0025 for this fax's share of the call."
    # The spending totals count the call once, at its whole charge.
    sip = next(item for item in telnyx_client.get('/routing/costs', headers=ADMIN).json()['providers']
               if item['provider_id'] == 'sip')
    assert sip['reported_cost'] == [{'currency': 'USD', 'amount': '0.005'}] and sip['attempts'] == 1


def test_savings_count_calls_saved_and_calls_avoided_and_stay_estimates(telnyx_client, monkeypatch):
    """Sending together saves calls; a document a partner accepted avoids one; neither is ever a reported figure."""
    from datetime import timedelta
    from uuid import uuid4
    import sqlalchemy as sa
    from app.routing import http as routing_http
    from app.routing.costs import estimate_cost, format_amount
    from api.tests.test_carrier_charges import FakeTelnyx, telnyx as record
    empty = telnyx_client.get('/routing/savings', headers=ADMIN)
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body['estimate'] is True and body['days'] == 30 and body['total_saved'] == []
    assert body['sending_together']['calls'] == 0 and body['direct_delivery']['faxes'] == 0
    assert body['case_packets']['packets'] == 0 and body['case_packets']['counted_from'] is None
    assert body['sending_together']['sentence'] == 'No faxes were sent together in the last 30 days.'
    answer = datetime.utcnow().replace(microsecond=0) - timedelta(hours=1)
    end = answer + timedelta(seconds=45)
    first, second, call = _two_faxes_in_one_call(telnyx_client, answer, end)
    routes = RouteStore(_engine())
    card, whole = routes.card_for('sip'), routes.decision(call)['estimated_cost_micros']
    separate = estimate_cost(card, 1) * 2
    # A third fax a partner accepted directly, and a fourth the partner refused: only the third counts.
    jobs = [telnyx_client.post('/fax', headers=ADMIN, data={'to': '+12025550188'},
                               files={'file': ('note.txt', b'Synthetic direct\n', 'text/plain')}).json()['id']
            for _ in range(2)]
    direct_deliveries = sa.Table('direct_deliveries', sa.MetaData(), autoload_with=_engine())
    with _engine().begin() as connection:
        connection.execute(sa.text('UPDATE fax_jobs SET pages = 3 WHERE id = :id'), {'id': jobs[0]})
        for job, state in zip(jobs, ('accepted', 'refused')):
            connection.execute(direct_deliveries.insert().values(
                id=uuid4().hex, direction='outbound', message_id=uuid4().hex, job_id=job,
                recipient_number='+12025550188', digest='d' * 64, size_bytes=10, manifest='{}', state=state,
                accepted_at=end if state == 'accepted' else None, created_at=answer, updated_at=end))
    avoided = estimate_cost(card, 3)

    def read():
        response = telnyx_client.get('/routing/savings', headers=ADMIN)
        assert response.status_code == 200, response.text
        return response.json()
    before = read()
    together, direct = before['sending_together'], before['direct_delivery']
    assert (together['numbers'], together['calls'], together['faxes'], together['calls_saved']) == (1, 1, 2, 1)
    assert together['estimate'] is True and together['priced_calls'] == 1
    assert together['saved'] == [{'currency': 'USD', 'amount': format_amount(separate - whole)}]
    assert together['sentence'] == (f"2 faxes to the same number went in 1 call instead of 2, saving 1 call and "
                                    f"about ${format_amount(separate - whole)}.")
    assert (direct['faxes'], direct['calls_avoided'], direct['pages'], direct['priced']) == (1, 1, 3, 1)
    assert direct['saved'] == [{'currency': 'USD', 'amount': format_amount(avoided)}]
    assert direct['sentence'] == (f'1 document went straight to a partner instead of by fax: 1 fax call and '
                                  f'about ${format_amount(avoided)} saved.')
    assert before['total_saved'] == [{'currency': 'USD', 'amount': format_amount(separate - whole + avoided)}]
    # Telnyx reports the shared call: the saving uses the reported charge and is still an estimate.
    monkeypatch.setattr(routing_http, 'carrier_source', lambda api_key: FakeTelnyx([record(
        'rec-shared', 'outbound', answer - timedelta(seconds=1), answer, end, '0.005',
        cli='+13035550100', cld='+12025550123')]))
    assert telnyx_client.post('/routing/reconcile', headers=ADMIN).status_code == 200
    after = read()
    assert after['estimate'] is True and after['sending_together']['estimate'] is True
    assert after['sending_together']['saved'] == [{'currency': 'USD', 'amount': format_amount(separate - 5000)}]
    assert telnyx_client.get('/routing/savings', headers=ADMIN, params={'days': 0}).status_code == 422
    assert telnyx_client.get('/routing/savings', headers=scoped_key(telnyx_client, ['fax:send'])).status_code == 403


def test_a_received_fax_cost_is_read_with_the_fax(telnyx_client):
    from datetime import timedelta
    from uuid import uuid4
    import sqlalchemy as sa
    from app.routing.carriers import CarrierChargeStore, CarrierReconciler
    from api.tests.test_carrier_charges import FakeTelnyx, telnyx as record
    engine = _engine()
    now = datetime.utcnow().replace(microsecond=0)
    answer, end = now - timedelta(minutes=50), now - timedelta(minutes=49, seconds=29)
    carriers = CarrierChargeStore(engine)
    main.app.state.access_runtime.inbound.accept({
        'id': 'inbound-1', 'from_number': '+17205550111', 'to_number': '+13035550100', 'status': 'received',
        'backend': 'sip', 'pages': 1, 'size_bytes': 1024, 'sha256': 'a' * 64, 'pdf_path': '/data/inbound-1.pdf',
        'created_at': now, 'received_at': now, 'updated_at': now})
    with engine.begin() as connection:
        connection.execute(carriers.calls.insert().values(
            id=uuid4().hex, direction='inbound', call_id='1759.1', job_id='inbound-1', trunk_preset='telnyx',
            did='+13035550100', caller='+17205550111', called='+13035550100', started_at=answer, answered_at=answer,
            ended_at=end, disposition='answered', connected_seconds=31, t38='yes', pages=1, fax_status='SUCCESS',
            fax_preference=0, created_at=answer, updated_at=end))
    waiting = telnyx_client.get('/routing/inbound/inbound-1/cost', headers=ADMIN)
    assert waiting.status_code == 200 and waiting.json()['summary'] == 'Cost not reported yet.'
    CarrierReconciler(carriers, RouteStore(engine), FakeTelnyx([record(
        'rec-in', 'inbound', answer, answer, end, '0.0032', cli='+17205550111', cld='+13035550100')])).run_now()
    assert telnyx_client.get('/routing/inbound/inbound-1/cost', headers=ADMIN).json()['summary'] == (
        'Telnyx charged $0.0032 for this call.')
    received = telnyx_client.get('/routing/costs', headers=ADMIN).json()['received']
    assert [(item['carrier'], item['calls'], item['reported_cost']) for item in received] == [
        ('Telnyx', 1, [{'currency': 'USD', 'amount': '0.0032'}])]
    assert telnyx_client.get('/routing/inbound/missing/cost', headers=ADMIN).status_code == 404
    batch = telnyx_client.get('/routing/inbound-costs', headers=ADMIN, params={'ids': 'inbound-1,missing,inbound-1'})
    assert batch.status_code == 200, batch.text
    assert {key: value['summary'] for key, value in batch.json()['costs'].items()} == {
        'inbound-1': 'Telnyx charged $0.0032 for this call.'}
    assert telnyx_client.get('/routing/inbound-costs', params={'ids': 'inbound-1'}).status_code == 401


def test_each_estimate_is_worded_by_its_card_and_counts_this_fax(client):
    """A per-minute trunk is never priced by the page; a 2-page fax is estimated as one call of whole minutes."""
    assert client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, PHAXIO]}).status_code == 200

    def routes(**params):
        response = client.get('/routing/destinations/+12025550123', headers=ADMIN, params=params)
        assert response.status_code == 200, response.text
        return {route['route']: route for route in response.json()['recommended_routes']}
    one = routes()
    assert one['sip']['rate'] == '$0.005 a minute, at least 1 minute'
    assert one['phaxio']['rate'] == '$0.07 a page'
    assert one['signalwire']['rate'] is None and one['signalwire']['estimated_cost'] is None  # no rate card
    assert one['sip']['pages'] == 1 and one['sip']['estimated_cost'] == {'currency': 'USD', 'amount': '0.005'}
    two = routes(pages=2)
    # About 30 seconds to connect and 30 a page: 90 seconds, billed as 2 whole minutes.
    assert two['sip']['pages'] == 2 and two['sip']['estimated_cost'] == {'currency': 'USD', 'amount': '0.01'}
    assert two['sip']['estimated_cost_one_page'] == {'currency': 'USD', 'amount': '0.005'}
    assert two['phaxio']['estimated_cost'] == {'currency': 'USD', 'amount': '0.14'}
    assert client.get('/routing/destinations/+12025550123', headers=ADMIN, params={'pages': 0}).status_code == 422


def test_a_fax_through_a_flat_plan_is_in_the_plan_while_it_is_still_sending(client):
    """Sent shows "In your plan" as soon as the route is chosen, not only after the fax finishes."""
    from uuid import uuid4
    plan = {'provider_id': 'phaxio', 'label': 'Phaxio unlimited plan', 'monthly_fee': '10.00', 'captured_on': '2026-10-03'}
    assert client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [plan]}).status_code == 200
    sent = client.post('/fax', headers=ADMIN, data={'to': '+12025550123'},
                       files={'file': ('note.txt', b'Synthetic\n', 'text/plain')})
    assert sent.status_code == 202, sent.text
    job, attempt = sent.json()['id'], uuid4().hex
    assert client.get(f'/routing/faxes/{job}/cost', headers=ADMIN).json()['state'] == 'none'  # no route yet
    routes, now = RouteStore(_engine()), datetime.utcnow()
    with _engine().begin() as connection:
        connection.execute(routes.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='submitting',
                                                           created_at=now))
    routes.record_decision(attempt_id=attempt, job_id=job, destination='+12025550123', route='phaxio',
                           reason='configured', provider_id='phaxio')
    cost = client.get(f'/routing/faxes/{job}/cost', headers=ADMIN).json()
    assert (cost['state'], cost['summary']) == ('included', 'Included in your Phaxio plan ($10 a month).')
    assert client.get('/routing/fax-costs', headers=ADMIN, params={'ids': job}).json()['costs'][job] == cost


def test_recommendations_never_call_an_unknown_cost_the_cheapest(client):
    """A route with no rate card that wins on reliability says so; a flat plan says it is included."""
    from datetime import timedelta
    from uuid import uuid4
    store = RouteStore(_engine())
    cards = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX]})
    assert cards.status_code == 200, cards.text
    # Recent faxes to this number failed on the SIP trunk; Phaxio has no rate card.
    now = datetime.utcnow()
    configuration = main.app.state.configuration_runtime.manager.store
    jobs = [uuid4().hex for _ in range(4)]
    for job in jobs:
        configuration.accept_outbound(configuration.read().active, {
            'id': job, 'to_number': '+12025550123', 'file_name': 'x.txt', 'tiff_path': '', 'status': 'queued',
            'pages': 1, 'created_at': now, 'updated_at': now})
    with _engine().begin() as connection:
        for index, job in enumerate(jobs):
            attempt = uuid4().hex
            connection.execute(store.attempts.insert().values(id=attempt, job_id=job, sequence=1, phase='failed',
                                                              created_at=now, submitted_at=now, completed_at=now))
            connection.execute(store.costs.insert().values(
                id=attempt, job_id=job, destination='+12025550123', route='sip', route_reason='configured',
                provider_id='sip', outcome='failed', billing_checks=0, created_at=now - timedelta(minutes=index),
                updated_at=now))
    view = client.get('/routing/destinations/+12025550123', headers=ADMIN).json()
    first = view['recommended_routes'][0]
    assert (first['route'], first['reason']) == ('phaxio', 'reliable')
    assert first['explanation'] == 'More reliable for this number; its cost is unknown.'
    assert first['estimated_cost_one_page'] is None and first['included_in_plan'] is False
    assert all(route['reason'] != 'cheapest' for route in view['recommended_routes'] if route['estimated_cost_one_page'] is None)
    plan = {'provider_id': 'phaxio', 'label': 'Phaxio unlimited plan', 'monthly_fee': '10.00',
            'captured_on': '2026-10-03', 'source_url': 'https://example.com/pricing'}
    saved = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [TELNYX, plan]})
    assert saved.status_code == 200, saved.text
    card = next(card for card in saved.json()['cards'] if card['provider_id'] == 'phaxio')
    assert (card['monthly_fee'], card['included_in_plan']) == ('10.00', True)
    first = client.get('/routing/destinations/+12025550123', headers=ADMIN).json()['recommended_routes'][0]
    assert (first['reason'], first['explanation']) == ('included', 'Included in your Phaxio plan ($10 a month).')
    assert first['monthly_fee'] == {'currency': 'USD', 'amount': '10.00'} and first['included_in_plan'] is True
    too_much = client.put('/routing/rate-cards', headers=ADMIN, json={'cards': [{**plan, 'monthly_fee': '5000'}]})
    assert too_much.status_code == 400



def test_listed_rate_cards_save_back_unchanged(client):
    """The console and `faxbot routing rate-cards --replace` send back what GET listed, minus ids."""
    listed = client.get('/routing/rate-cards', headers=ADMIN).json()['cards']
    assert any(card['included_in_plan'] for card in listed)  # the shipped HumbleFax plan
    saved = client.put('/routing/rate-cards', headers=ADMIN,
                       json={'cards': [{key: value for key, value in card.items() if key != 'id'} for card in listed]})
    assert saved.status_code == 200, saved.text
    assert [card['id'] for card in saved.json()['cards']] == [card['id'] for card in listed]  # no new versions


def test_the_check_now_summary_splits_unrecorded_calls_like_the_spending_card():
    from app.routing.http import _reconcile_summary
    base = {'checked': 4, 'matched': 4, 'charges_recorded': 0, 'waiting': 0, 'ambiguous': 0,
            'carrier_unavailable': False}
    assert _reconcile_summary({**base, 'unrecorded_calls': 1, 'unrecorded_matched_to_faxes': 1}) == (
        'Checked 4 calls: 0 new charges recorded. 1 call came in that Faxbot did not record at the time; its fax is in Received.')
    assert _reconcile_summary({**base, 'unrecorded_calls': 3, 'unrecorded_matched_to_faxes': 1}) == (
        'Checked 4 calls: 0 new charges recorded. Telnyx billed 2 calls Faxbot has no record of. '
        '1 call came in that Faxbot did not record at the time; its fax is in Received.')
    assert _reconcile_summary({**base, 'unrecorded_calls': 0}) == 'Checked 4 calls: 0 new charges recorded.'

