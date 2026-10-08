"""Partner discovery from calls already made (M16): hints, one GET per host, suggestions and the well-known card.

Installation B is the running application (TestClient), which answers
``/.well-known/faxbot-direct``. Installation A is a direct delivery service on
its own database, whose discovery reads A's call frames and asks B's
well-known address through a stand-in fetcher. SQLite and PostgreSQL. Every
number, host, card and passcode is synthetic; no network, provider or DNS is
used.
"""
import asyncio
import base64
import json
from datetime import datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app import main
from api.app.config_values import ConfigurationValues
from api.app.direct import discovery
from api.app.direct.crypto import Identity, card as make_card
from api.app.direct.discovery import DiscoveryService, Fetched, LookupRefused, WellKnownFetcher
from api.app.direct.service import DirectService
from api.app.schema import create_database_engine, upgrade_schema
from api.tests.test_direct_delivery import A_NUMBER, B_NUMBER
from api.tests.test_peer_fax import _namespaces
from api.tests.test_routing_http import ADMIN, BOOTSTRAP


PASSCODE = 'Zq7secretPass'
# Patch 0004 keeps 32 octets of a frame, so frames in these tests carry a short passcode.
SHORT = 'Zq7s'


def csa_hex(address, *, fcf=0x24, kind=0x02, pad_to=None):
    """A CSA (or TSA) frame as patch 0004 keeps it: address, control, FCF, type, length, characters."""
    text = address.encode('ascii')
    frame = bytes([0xFF, 0x03, fcf, kind, len(text)]) + text
    if pad_to:
        frame = frame[:pad_to]
    return frame.hex()


class StandIn:
    """A's fetcher: each host answers what ``routes`` says; every GET is recorded."""

    def __init__(self):
        self.routes, self.calls = {}, []

    def serve(self, host, answer):
        self.routes[host] = answer

    async def get(self, host, *, allow_private):
        self.calls.append((host, allow_private))
        answer = self.routes.get(host)
        if answer is None:
            raise LookupRefused('unreachable')
        if isinstance(answer, LookupRefused):
            raise answer
        if callable(answer):
            return await asyncio.to_thread(answer)
        return answer


def document(card):
    return Fetched(200, json.dumps({'faxbot_direct': 1, 'card': card}).encode(), None)


def a_values(data, **changes):
    environment = {'FAX_BACKEND': 'phaxio', 'FAX_DISABLED': 'false', 'FAX_DATA_DIR': str(data),
                   'PUBLIC_API_URL': 'https://a.example', 'DIRECT_DELIVERY_ENABLED': 'true',
                   'DIRECT_ORGANIZATION': 'Valley Hospital', 'DIRECT_FAX_NUMBER': A_NUMBER}
    environment.update(changes)
    return ConfigurationValues.from_environment(environment)


@pytest.fixture(params=['sqlite', 'postgresql'])
def pair(request, isolated_installation, monkeypatch, tmp_path):
    scoped = _namespaces(request, 2)
    b_url = scoped[1].render_as_string(hide_password=False) if scoped else None
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'MAX_REQUESTS_PER_MINUTE': '0', 'DIRECT_DELIVERY_ENABLED': 'true',
                        'DIRECT_ORGANIZATION': 'County Clinic', 'DIRECT_FAX_NUMBER': B_NUMBER,
                        'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'b-direct.key'),
                        'FAXBOT_CONSOLE_ORIGINS': 'https://testserver', **({'DATABASE_URL': b_url} if b_url else {})}.items():
        monkeypatch.setenv(name, value)
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        engine = (create_database_engine(scoped[0].render_as_string(hide_password=False)) if scoped
                  else create_database_engine('sqlite:///' + str(tmp_path / 'a.db')))
        upgrade_schema(engine)
        data = tmp_path / 'a-data'
        data.mkdir()
        holder = {'values': a_values(data)}
        a = DirectService(engine, values=lambda: holder['values'],
                          environment={'FAXBOT_DIRECT_KEY_PATH': str(tmp_path / 'a.key')},
                          resolver=lambda host, port: ['93.184.215.14'])
        a.identity(create=True)
        fetcher = StandIn()
        try:
            yield {'a': a, 'discovery': DiscoveryService(a, fetcher=fetcher), 'fetcher': fetcher, 'client': client,
                   'engine': engine, 'holder': holder, 'data': data, 'tmp': tmp_path}
        finally:
            engine.dispose()


def frame(engine, *, number=B_NUMBER, csa=None, tsa=None, direction='out', created=None):
    table = sa.Table('fax_call_frames', sa.MetaData(), autoload_with=engine)
    attempt = uuid4().hex
    row = {'id': f'{direction}:{attempt}', 'direction': direction, 'attempt_id': attempt if direction == 'out' else None,
           'call_key': None if direction == 'out' else '1700000000.1', 'number': number, 'csa': csa, 'tsa': tsa,
           'created_at': created or datetime.utcnow()}
    with engine.begin() as connection:
        connection.execute(table.insert().values(**row))
    return row['id']


def rows(engine, table, **where):
    meta = sa.Table(table, sa.MetaData(), autoload_with=engine)
    query = sa.select(meta)
    for key, value in where.items():
        query = query.where(meta.c[key] == value)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(query).mappings()]


def b_card(client):
    return client.get('/direct/card', headers=ADMIN).json()['card']


def b_answers(pair):
    """B's well-known address, answered by the running application."""
    def answer():
        response = pair['client'].get('/.well-known/faxbot-direct')
        return Fetched(response.status_code, response.content, None)
    return answer


# -- reading the address from a call -------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('ssl://Zq7secretPass@b.example:10443', ('b.example', 10443)),
    ('ssl://(passcode hidden)@203.0.113.9:10443', ('203.0.113.9', 10443)),
    ('ssl://b.example:10443', ('b.example', 10443)),
    ('ssl://pa@ss@fax.b.example:1', ('fax.b.example', 1)),
    ('ssl://pw@203.0.113.9:', ('203.0.113.9', None)),  # the port cut off; the host is whole before its colon
    ('ssl://Zq7secretPass', None),  # cut inside the passcode: never read as a host
    ('ssl://ab:12', None),  # no dotted host
    ('ssl://pw@b.exam', None),  # cut inside the host
    ('ssl://pw@999.1.1.1:10443', None),
    ('ssl://pw@b.example:99999', None),
    ('mailto:fax@b.example', None),
])
def test_only_the_host_and_port_after_the_last_at_are_read(text, expected):
    assert discovery.ssl_address(text) == expected
    if expected:
        assert PASSCODE not in repr(discovery.ssl_address(text))


def test_a_frame_is_read_in_any_of_its_byte_orders_and_a_cut_passcode_is_never_a_host():
    address = f'ssl://{PASSCODE[:4]}@b.example:10443'
    assert discovery.frame_address(csa_hex(address)) == ('b.example', 10443)
    raw = bytes.fromhex(csa_hex(address))
    reversed_fif = raw[:3] + raw[3:][::-1]
    assert discovery.frame_address(reversed_fif.hex()) == ('b.example', 10443)
    flipped = raw[:3] + bytes(int(f'{octet:08b}'[::-1], 2) for octet in raw[3:])
    assert discovery.frame_address(flipped.hex()) == ('b.example', 10443)
    # Patch 0004 keeps 32 octets: a frame cut there before the passcode's "@" is not used.
    assert discovery.frame_address(csa_hex('ssl://' + 'P' * 40 + '@b.example:10443', pad_to=32)) is None
    assert discovery.frame_address(csa_hex('ssl://k@b.example:10443' + 'x' * 10, pad_to=32)) == ('b.example', 10443)
    assert discovery.frame_address(csa_hex('fax@b.example')) is None
    assert discovery.frame_address('zz') is None and discovery.frame_address(None) is None


# -- the well-known card ------------------------------------------------------------------------------------

def test_the_well_known_card_needs_direct_delivery_its_setting_and_existing_keys(pair):
    client, key = pair['client'], pair['tmp'] / 'b-direct.key'
    # No keys yet: an anonymous request never creates them.
    assert client.get('/.well-known/faxbot-direct').status_code == 404
    assert not key.exists()
    card = b_card(client)
    answer = client.get('/.well-known/faxbot-direct')
    assert answer.status_code == 200 and answer.json() == {'faxbot_direct': 1, 'card': card}
    assert 'max-age=3600' in answer.headers['cache-control']
    off = client.put('/direct/discovery/settings', headers=ADMIN, json={'well_known': False})
    assert off.status_code == 200, off.text
    assert off.json()['texts']['well_known'] == 'Other Faxbots cannot find your partner card from their calls to you.'
    assert client.get('/.well-known/faxbot-direct').status_code == 404
    on = client.put('/direct/discovery/settings', headers=ADMIN, json={'well_known': True})
    assert on.json()['texts']['well_known'] == ('Faxbots that fax you can read your partner card and suggest '
                                                'enrolling you as a partner.')
    assert client.get('/.well-known/faxbot-direct').status_code == 200


def test_the_well_known_card_is_off_while_direct_delivery_is_off(pair):
    holder, a = pair['holder'], pair['a']
    assert pair['discovery'].well_known()['card']['fax_number'] == A_NUMBER
    holder['values'] = a_values(pair['data'], DIRECT_DELIVERY_ENABLED='false')
    assert pair['discovery'].well_known() is None
    assert a.ready()


# -- a hint leads to one GET and a suggestion -------------------------------------------------------------

def no_calls_placed(engine):
    """Discovery never places a fax call: no fax, delivery or attempt row exists."""
    return all(not rows(engine, table) for table in ('fax_jobs', 'outbound_deliveries', 'outbound_attempts'))


@pytest.mark.asyncio
async def test_a_csa_hint_leads_to_one_get_and_a_suggestion_and_never_a_call(pair):
    engine, fetcher, service = pair['engine'], pair['fetcher'], pair['discovery']
    card = b_card(pair['client'])
    fetcher.serve('b.example', b_answers(pair))
    frame(engine, csa=csa_hex(f'ssl://{SHORT}@b.example:10443'))
    assert await service.step() is False
    assert [host for host, _ in fetcher.calls] == ['b.example']
    hints = rows(engine, 'direct_discovery_hints')
    assert [(hint['host'], hint['port'], hint['number']) for hint in hints] == [('b.example', 10443, B_NUMBER)]
    lookups = rows(engine, 'direct_discovery_lookups')
    assert [(row['kind'], row['outcome'], row['url']) for row in lookups] == [
        ('call', 'faxbot', 'https://b.example/.well-known/faxbot-direct')]
    suggestions = rows(engine, 'direct_discovery_suggestions')
    assert [(row['number'], row['organization'], row['source']) for row in suggestions] == [
        (B_NUMBER, 'County Clinic', 'call')]
    assert json.loads(suggestions[0]['card']) == card
    # The passcode the far end advertised is kept nowhere.
    for table in ('direct_discovery_hints', 'direct_discovery_lookups', 'direct_discovery_suggestions'):
        assert SHORT not in repr(rows(engine, table))
    assert no_calls_placed(engine)
    # The answer is reused: another call to the same host makes no second GET and no second suggestion.
    frame(engine, csa=csa_hex('ssl://other@b.example:10443'))
    await service.step()
    assert len(fetcher.calls) == 1 and len(rows(engine, 'direct_discovery_suggestions')) == 1
    assert {hint['lookup_id'] for hint in rows(engine, 'direct_discovery_hints')} == {lookups[0]['id']}


@pytest.mark.asyncio
async def test_enrolling_a_suggestion_makes_a_partner_that_still_needs_the_challenge(pair):
    engine, service = pair['engine'], pair['discovery']
    pair['fetcher'].serve('b.example', b_answers(pair))
    b_card(pair['client'])
    frame(engine, csa=csa_hex('ssl://k@b.example:10443'))
    await service.step()
    suggestion = rows(engine, 'direct_discovery_suggestions')[0]
    peer, sentence = await service.enroll(suggestion['id'], actor_name='Office admin')
    assert sentence == 'County Clinic added. Send them a code by fax to confirm their number.'
    assert peer['state'] == 'pending' and peer['phone_number'] == B_NUMBER
    closed = rows(engine, 'direct_discovery_suggestions')[0]
    assert closed['enrolled_peer_id'] == peer['id'] and closed['enrolled_by_name'] == 'Office admin'
    with pytest.raises(discovery.DirectConflict, match='This suggestion is no longer open.'):
        await service.enroll(suggestion['id'])
    # A partner's number is never looked up again.
    frame(engine, csa=csa_hex('ssl://k@b2.example:10443'))
    await service.step()
    assert [host for host, _ in pair['fetcher'].calls] == ['b.example']
    assert [hint['lookup_id'] for hint in rows(engine, 'direct_discovery_hints') if hint['host'] == 'b2.example'] == [
        discovery.SKIPPED]
    assert no_calls_placed(engine)


@pytest.mark.asyncio
async def test_a_tsa_on_a_received_call_is_a_hint_for_the_caller(pair):
    engine, service = pair['engine'], pair['discovery']
    pair['fetcher'].serve('b.example', b_answers(pair))
    b_card(pair['client'])
    frame(engine, direction='in', number=B_NUMBER.lstrip('+'), tsa=csa_hex('ssl://k@b.example:10443', fcf=0x62))
    await service.step()
    assert [row['number'] for row in rows(engine, 'direct_discovery_suggestions')] == [B_NUMBER]


@pytest.mark.parametrize('case', ['bad_signature', 'other_number', 'our_own', 'too_large', 'redirect', 'not_json',
                                  'unverified_ip'])
@pytest.mark.asyncio
async def test_a_spoofed_or_wrong_identity_is_refused(pair, case):
    engine, service, fetcher = pair['engine'], pair['discovery'], pair['fetcher']
    host = '203.0.113.9' if case == 'unverified_ip' else 'b.example'
    spoof = Identity.generate()
    card = make_card(spoof, organization='County Clinic', fax_number=B_NUMBER, endpoint='https://b.example')
    answer = {
        'bad_signature': document({**card, 'organization': 'Somebody Else'}),
        'other_number': document(make_card(spoof, organization='County Clinic', fax_number='+15550100009',
                                           endpoint='https://b.example')),
        'our_own': document(pair['a'].own_card()),
        'too_large': Fetched(200, None, None),
        'redirect': Fetched(301, b'', None),
        'not_json': Fetched(200, b'<html>hello</html>', None),
        'unverified_ip': Fetched(200, json.dumps({'faxbot_direct': 1, 'card': card}).encode(), ('other.example',)),
    }[case]
    fetcher.serve(host, answer)
    frame(engine, csa=csa_hex(f'ssl://k@{host}:10443'))
    await service.step()
    assert len(fetcher.calls) == 1
    outcome = rows(engine, 'direct_discovery_lookups')[0]['outcome']
    assert outcome == {'bad_signature': 'invalid', 'other_number': 'faxbot', 'our_own': 'own',
                       'too_large': 'not_faxbot', 'redirect': 'not_faxbot', 'not_json': 'not_faxbot',
                       'unverified_ip': 'unverified'}[case]
    assert rows(engine, 'direct_discovery_suggestions') == []
    assert not rows(engine, 'direct_peers')


@pytest.mark.asyncio
async def test_a_bare_ip_answer_counts_only_when_its_certificate_names_the_cards_host(pair):
    engine, service = pair['engine'], pair['discovery']
    spoof = Identity.generate()
    card = make_card(spoof, organization='County Clinic', fax_number=B_NUMBER, endpoint='https://fax.b.example')
    pair['fetcher'].serve('203.0.113.9', Fetched(200, json.dumps({'faxbot_direct': 1, 'card': card}).encode(),
                                                 ('*.b.example',)))
    frame(engine, csa=csa_hex('ssl://k@203.0.113.9:10443'))
    await service.step()
    assert [row['outcome'] for row in rows(engine, 'direct_discovery_lookups')] == ['faxbot']
    assert [row['endpoint'] for row in rows(engine, 'direct_discovery_suggestions')] == ['https://fax.b.example']


# -- rate limits, the cache and private addresses -----------------------------------------------------

def lookup_row(engine, *, host, outcome='unreachable', started, expires):
    table = sa.Table('direct_discovery_lookups', sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=uuid4().hex, kind='call', host=host, url='https://x/',
                                                 outcome=outcome, started_at=started, expires_at=expires))


@pytest.mark.asyncio
async def test_lookups_are_limited_per_host_and_per_hour_and_a_failure_is_not_retried_in_a_loop(pair):
    engine, service, fetcher = pair['engine'], pair['discovery'], pair['fetcher']
    now = datetime.utcnow()
    # A failure an hour ago is kept for a day: the hint takes that answer, with no GET.
    lookup_row(engine, host='down.example', started=now - timedelta(hours=1), expires=now + timedelta(hours=23))
    frame(engine, csa=csa_hex('ssl://k@down.example:10443'))
    await service.step()
    assert fetcher.calls == []
    assert rows(engine, 'direct_discovery_hints')[0]['lookup_id'] is not None
    # The hour's lookups are used up: a new host waits, and is asked once the hour has passed.
    for index in range(discovery.LOOKUPS_PER_HOUR):
        lookup_row(engine, host=f'h{index}.example', started=now - timedelta(minutes=30),
                   expires=now + timedelta(days=1))
    fetcher.serve('b.example', b_answers(pair))
    b_card(pair['client'])
    frame(engine, csa=csa_hex('ssl://k@b.example:10443'))
    await service.step()
    assert fetcher.calls == []
    waiting = [hint for hint in rows(engine, 'direct_discovery_hints') if hint['host'] == 'b.example']
    assert waiting[0]['lookup_id'] is None
    await service.answer_hints(now=now + timedelta(hours=2))
    assert [host for host, _ in fetcher.calls] == ['b.example']


@pytest.mark.asyncio
async def test_a_private_address_is_not_contacted_unless_private_partners_are_allowed(pair):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={'faxbot_direct': 1})
    fetcher = WellKnownFetcher(resolver=lambda host, port: ['192.168.68.230'], transport=httpx.MockTransport(handler))
    with pytest.raises(LookupRefused) as refused:
        await fetcher.get('partner.lan.example', allow_private=False)
    assert refused.value.outcome == 'private' and requests == []
    with pytest.raises(LookupRefused) as refused:
        await fetcher.get('127.0.0.1', allow_private=False)
    assert refused.value.outcome == 'private' and requests == []
    answer = await fetcher.get('partner.lan.example', allow_private=True)
    assert answer.status == 200 and requests == ['https://partner.lan.example/.well-known/faxbot-direct']
    # The service passes the administrator's choice, and records a refused address for a day.
    engine, service = pair['engine'], pair['discovery']
    pair['fetcher'].serve('lan.example', LookupRefused('private'))
    frame(engine, csa=csa_hex('ssl://k@lan.example:10443'))
    await service.step()
    assert pair['fetcher'].calls == [('lan.example', False)]
    lookup = rows(engine, 'direct_discovery_lookups')[0]
    assert lookup['outcome'] == 'private' and lookup['expires_at'] - lookup['started_at'] == timedelta(days=1)
    assert discovery.OUTCOME_TEXT['private'] == ('Its address is on a private or local network, which Faxbot looks '
                                                 'up only when partners on private networks are allowed.')


@pytest.mark.asyncio
async def test_the_get_reads_a_bounded_body_follows_no_redirect_and_pins_the_checked_address():
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get('host')))
        if request.url.path == '/.well-known/faxbot-direct' and request.headers.get('host') == 'big.example':
            return httpx.Response(200, content=b'x' * (discovery.MAX_DOCUMENT_BYTES + 10))
        return httpx.Response(302, headers={'location': 'https://elsewhere.example/'})
    fetcher = WellKnownFetcher(resolver=lambda host, port: ['93.184.215.14'], transport=httpx.MockTransport(handler))
    big = await fetcher.get('big.example', allow_private=False)
    assert big.status == 200 and big.body is None
    moved = await fetcher.get('moved.example', allow_private=False)
    assert moved.status == 302
    # Connected to the address that was checked, with the host's own name; the redirect was not followed.
    assert seen == [('https://93.184.215.14/.well-known/faxbot-direct', 'big.example'),
                    ('https://93.184.215.14/.well-known/faxbot-direct', 'moved.example')]

    def down(request):
        raise httpx.ConnectError('refused')
    with pytest.raises(LookupRefused) as refused:
        await WellKnownFetcher(resolver=lambda host, port: ['93.184.215.14'],
                               transport=httpx.MockTransport(down)).get('b.example', allow_private=False)
    assert refused.value.outcome == 'unreachable'


@pytest.mark.asyncio
async def test_nothing_is_looked_up_with_lookups_from_calls_off_or_direct_delivery_off(pair):
    engine, service, client = pair['engine'], pair['discovery'], pair['client']
    pair['fetcher'].serve('b.example', b_answers(pair))
    service.store.save_settings(from_calls=False)
    frame(engine, csa=csa_hex('ssl://k@b.example:10443'))
    await service.step()
    assert pair['fetcher'].calls == [] and rows(engine, 'direct_discovery_hints') == []
    service.store.save_settings(from_calls=True)
    pair['holder']['values'] = a_values(pair['data'], DIRECT_DELIVERY_ENABLED='false')
    await service.step()
    assert pair['fetcher'].calls == [] and rows(engine, 'direct_discovery_hints') == []
    view = client.get('/direct/discovery', headers=ADMIN).json()
    assert view['texts']['from_calls'] == ('When a fax call shows the other side runs Faxbot, Faxbot asks that '
                                           'address once whether it takes faxes directly. This never places a call.')


# -- the SSL Fax engine's report ---------------------------------------------------------------------

def test_the_ssl_fax_engines_report_keeps_only_host_and_port_as_a_hint(pair):
    engine = pair['engine']
    payload = {'remote_address_b64': base64.b64encode(b'b.example:10443').decode()}
    assert discovery.record_engine_hint(engine, attempt_id='a' * 32, job_id=None, number=B_NUMBER, payload=payload)
    # The same attempt reported again changes nothing; a passcode left in the text is dropped.
    assert not discovery.record_engine_hint(engine, attempt_id='a' * 32, job_id=None, number=B_NUMBER,
                                            payload=payload)
    sneaky = {'remote_address_b64': base64.b64encode(f'{PASSCODE}@b2.example:10443'.encode()).decode()}
    assert discovery.record_engine_hint(engine, attempt_id='b' * 32, job_id=None, number=B_NUMBER, payload=sneaky)
    hints = rows(engine, 'direct_discovery_hints')
    assert sorted((hint['source'], hint['host'], hint['port']) for hint in hints) == [
        ('engine', 'b.example', 10443), ('engine', 'b2.example', 10443)]
    assert PASSCODE not in repr(hints)
    assert not discovery.record_engine_hint(engine, attempt_id='c' * 32, job_id=None, number=B_NUMBER, payload={})


def test_notify_reports_the_far_ends_address_without_its_passcode(tmp_path):
    from api.tests.test_hylafax_scripts import QFILE, ROOT, TOOLS, _stub, run
    import shutil
    if not all(shutil.which(tool) for tool in TOOLS):
        pytest.skip('POSIX tools are needed to run the engine scripts.')
    spool, state, tools = tmp_path / 'spool', tmp_path / 'state', tmp_path / 'tools'
    for folder in (spool / 'log', spool / 'etc', spool / 'doneq', state / 'results', tools):
        folder.mkdir(parents=True)
    (spool / 'etc' / 'faxbot.conf').write_text('url=http://api:8080\nsecret=synthetic-secret-value\n'
                                               'engine=0123456789abcdef\n')
    _stub(tools, 'curl', 'echo 503\n')
    _stub(tools, 'date', 'echo 1791180000\n')
    _stub(tools, 'flock', 'exit 0\n')
    for tool in TOOLS:
        (tools / tool).symlink_to(shutil.which(tool))
    environment = {'PATH': str(tools), 'FAXBOT_HYLAFAX_SPOOL': str(spool), 'FAXBOT_ENGINE_STATE': str(state)}
    assert ROOT.exists()
    for index, line in enumerate(('REMOTE CSA "ssl://(passcode hidden)@fax.b.example:10443", type 0x40',
                                  f'REMOTE CSA "ssl://{PASSCODE}@203.0.113.9:10443", type 0x40',
                                  'REMOTE CSI "+1 555 010 0002"')):
        commid = f'00000002{index}'
        (spool / 'log' / f'c{commid}').write_text(f'Oct 07 10:00:00.00: [  200]: {line}\n')
        (spool / 'doneq' / 'q12').write_text(QFILE.replace('commid:000000007', f'commid:{commid}'))
        result = run('notify', environment, 'doneq/q12', 'failed', '0:00:41', cwd=spool)
        assert result.returncode == 0, result.stderr
        kept = state / 'results' / '1791180000-job12-failed.report'
        report = json.loads(kept.read_text())
        kept.unlink()
        address = base64.b64decode(report['remote_address_b64']).decode()
        assert address == ['fax.b.example:10443', '203.0.113.9:10443', ''][index]
        assert PASSCODE not in json.dumps(report)
