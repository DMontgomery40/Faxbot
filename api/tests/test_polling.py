"""Collecting a fax by polling (M21, routing/polling.py): opt-in per number, one call when a person asks, the
engine's poll job and its result, the collected fax handed over into Received, and the advice. Synthetic numbers
and stand-ins only; the loopback proof (tests/test_sslfax_loopback.py, case r) runs the real engines."""
import asyncio
import base64
from datetime import datetime, timedelta
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.app import schema
from api.tests.test_hylafax_engine import FakeEngine, trunk_values
from api.tests.test_hylafax_scripts import engine as scripts, run  # noqa: F401 - fixture
from api.tests.test_schema import database  # noqa: F401 - fixture
from app import hylafax_engine
from app.routing import polling

NUMBER = '+15555550199'
NOW = datetime(2026, 10, 8, 9, 0)
REQUEST = 'c' * 32


@pytest.fixture
def installation(database):  # noqa: F811
    schema.upgrade_schema(database)
    return database


@pytest.fixture
def fake_engine(tmp_path):
    values = trunk_values(tmp_path)
    server = FakeEngine(hylafax_engine.engine_secrets(values)['submit_password'])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield values, server
    server.shutdown()
    server.server_close()


# The engine's poll job ------------------------------------------------------------------------------------------

def test_a_poll_job_dials_once_carries_its_request_and_sends_no_document(fake_engine):
    values, server = fake_engine
    tag = hylafax_engine.new_tag()
    job = hylafax_engine.create_poll_job(values, tag=tag, request_id=REQUEST, selective='12 34',
                                         host='127.0.0.1', port=server.server_address[1])
    assert job.engine_job == '7' and server.uploads == [] and 'JSUBM' not in server.commands
    parms = [command for command in server.commands if command.startswith('JPARM')]
    assert f'JPARM DIALSTRING "{tag}"' in parms and f'JPARM JOBINFO "{REQUEST}.{REQUEST}"' in parms
    assert 'JPARM MAXDIALS 1' in parms and 'JPARM MAXTRIES 1' in parms
    assert f'JPARM NOTIFYADDR "poll-{REQUEST}@faxbot.invalid"' in parms and 'JPARM POLL "1234"' in parms
    assert not any(command.startswith('JPARM DOCUMENT') for command in parms)
    assert hylafax_engine.parse_tag(f'{REQUEST}.{REQUEST}') == (REQUEST, REQUEST)
    with pytest.raises(ValueError):
        hylafax_engine.create_poll_job(values, tag=tag, request_id='not-hex', host='127.0.0.1',
                                       port=server.server_address[1])


def b64(text):
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize('payload, outcome', [
    ({'why': 'done', 'dials': 1}, 'received'),
    ({'why': 'poll_no_document', 'dials': 1}, 'nothing_waiting'),
    # What HylaFAX+ 7.0.11 really reports for a machine whose DIS holds no document (faxd/FaxSend.c++ sendPoll).
    ({'why': 'done', 'dials': 1, 'status_b64': b64('remote has no document to poll')}, 'nothing_waiting'),
    ({'why': 'poll_rejected', 'dials': 1}, 'refused'),
    # A stock HylaFAX+ answering a poll: it ends the call on DTC (Class1Recv.c++, E107); faxsend says the remote
    # cannot be polled (E220/E266) or the job fails with the remote's own words.
    ({'why': 'poll_failed', 'dials': 1, 'status_b64': b64('In Polling Phase B, remote cannot be polled {E266}')},
     'refused'),
    ({'why': 'failed', 'dials': 0, 'status_b64': b64('Busy signal detected')}, 'failed'),
    ({'why': 'failed', 'dials': 1, 'status_b64': b64('Busy signal detected {E001}'), 'status_code': 'E001'},
     'failed'),
    ({'why': 'poll_failed', 'dials': 1, 'remote_station_b64': b64('5550199'), 'status_b64': b64('RSPREC error')},
     'uncertain'),
])
def test_each_poll_result_has_one_outcome_and_sentence(payload, outcome):
    found, sentence = hylafax_engine.poll_outcome(payload)
    assert found == outcome and sentence.endswith('.')


# Opt-in, and only when a person asks ------------------------------------------------------------------------------

class Ami:
    def __init__(self):
        self.plans, self.events = [], []

    async def db_put(self, family, key, value):
        self.plans.append((family, key, value))

    async def db_del(self, family, key):
        pass

    def _emit(self, name, message):
        self.events.append((name, message))


def test_collecting_needs_the_numbers_own_opt_in_and_places_nothing_without_it(installation, monkeypatch):
    ami = Ami()
    chosen = []

    async def choose(values, **_):
        chosen.append(values)
        return hylafax_engine.EngineChoice('hylafax')
    monkeypatch.setattr(hylafax_engine, 'choose', choose)
    with pytest.raises(polling.PollRefused, match='Turn on collecting faxes from this number first.'):
        asyncio.run(polling.collect(installation, SimpleNamespace(), ami, NUMBER))
    polling.save_source(installation, NUMBER, enabled=True, label='Denver office', now=NOW)
    polling.save_source(installation, NUMBER, enabled=False, now=NOW + timedelta(minutes=1))
    with pytest.raises(polling.PollRefused):
        asyncio.run(polling.collect(installation, SimpleNamespace(), ami, NUMBER))
    assert ami.plans == [] and chosen == [] and polling.requests(installation, NUMBER) == []
    # Every setting is a new row; the newest counts.
    assert polling.source(installation, NUMBER).enabled is False
    with installation.connect() as connection:
        assert connection.execute(sa.text('SELECT COUNT(*) FROM poll_sources')).scalar() == 2
    with pytest.raises(ValueError):
        polling.save_source(installation, NUMBER, enabled=True, selective='12ab')


def test_a_collection_is_recorded_before_its_one_call_and_its_result_once(installation, monkeypatch):
    ami = Ami()
    polling.save_source(installation, NUMBER, enabled=True, selective='77', now=NOW)

    async def choose(values, **_):
        return hylafax_engine.EngineChoice('hylafax')
    submitted = []

    class Job:
        engine_job, tag = '42', '1' * 16
        submission = {'JobID': 'x', 'AttemptID': 'x', 'Called': NUMBER}

        def submit(self):
            # The request is on record before the call.
            submitted.append(polling.requests(installation, NUMBER)[0]['id'])
            return '42'

        def close(self):
            pass

    async def prepare_poll(values, ami, *, request_id, number, selective='', trunk=None):
        assert (number, selective) == (NUMBER, '77')
        return Job()
    monkeypatch.setattr(hylafax_engine, 'choose', choose)
    monkeypatch.setattr(hylafax_engine, 'prepare_poll', prepare_poll)
    request_id = asyncio.run(polling.collect(installation, SimpleNamespace(), ami, NUMBER, actor_name='Ada'))
    assert submitted == [request_id] and ami.events[0][0] == 'Submission'
    shown = polling.view(installation, NUMBER, now=NOW)
    assert shown['enabled'] and shown['requests'][0]['state'] == 'Calling'
    assert shown['requests'][0]['sentence'] == polling.WAITING and shown['requests'][0]['requested_by'] == 'Ada'
    assert polling.is_request(installation, request_id) and not polling.is_request(installation, 'd' * 32)
    assert polling.record_result(installation, request_id, 'nothing_waiting', 'Nothing.', now=NOW) == 'nothing_waiting'
    # An engine report sent again keeps the first result.
    assert polling.record_result(installation, request_id, 'failed', 'Again.', now=NOW) == 'nothing_waiting'
    assert polling.view(installation, NUMBER, now=NOW)['requests'][0]['state'] == 'Nothing waiting'


def test_an_engine_that_cannot_take_the_call_dials_nothing_and_says_so(installation, monkeypatch):
    polling.save_source(installation, NUMBER, enabled=True, now=NOW)

    async def not_running(values, **_):
        return hylafax_engine.EngineChoice('builtin', hylafax_engine.NOT_RUNNING)
    monkeypatch.setattr(hylafax_engine, 'choose', not_running)
    with pytest.raises(polling.PollRefused, match="fast fax service, which cannot take the call now"):
        asyncio.run(polling.collect(installation, SimpleNamespace(), Ami(), NUMBER))

    async def running(values, **_):
        return hylafax_engine.EngineChoice('hylafax')

    async def refused(*args, **kwargs):
        raise hylafax_engine.EngineError('The fax engine refused the job settings.')
    monkeypatch.setattr(hylafax_engine, 'choose', running)
    monkeypatch.setattr(hylafax_engine, 'prepare_poll', refused)
    with pytest.raises(polling.PollRefused, match='nothing was dialed'):
        asyncio.run(polling.collect(installation, SimpleNamespace(), Ami(), NUMBER))
    assert polling.view(installation, NUMBER, now=NOW)['requests'][0]['state'] == 'Not dialed'


# The collected fax ------------------------------------------------------------------------------------------------

def test_the_collected_fax_carries_its_collection_through_the_engines_hand_over(scripts, tmp_path):  # noqa: F811
    spool, state, data, environment = scripts
    result = run('pollrcvd', environment, f'poll-{REQUEST}@faxbot.invalid', 'recvq/fax000000007.tif', 'ttyIAX1',
                 '000000007', '', cwd=spool)
    assert result.returncode == 0, result.stderr
    ticket = next((state / 'received').glob('*.ticket')).read_text()
    assert f'poll={REQUEST}' in ticket
    (tmp_path / 'answer').write_text('200')
    assert run('handover', environment).returncode == 0
    body = json.loads((tmp_path / 'body').read_text())
    assert body['poll'] == REQUEST and (tmp_path / 'url').read_text().endswith('/_internal/hylafax/inbound')
    # Anything else in the notify address is refused: only collections Faxbot asked for.
    refused = run('pollrcvd', environment, 'someone@example.com', 'recvq/fax000000007.tif', 'ttyIAX1', '7', '',
                  cwd=spool)
    assert refused.returncode != 0
    # An ordinary received fax names no collection.
    assert run('received', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '000000008', '', cwd=spool
               ).returncode in (0, 1)


def test_the_engine_reads_its_poll_command_from_each_lines_own_config():
    from api.tests.test_hylafax_engine import ROOT
    entry = (ROOT / 'hylafax' / 'entrypoint.sh').read_text()
    assert "printf 'PollRcvdCmd:\\t\\t/usr/local/lib/faxbot-engine/pollrcvd\\n'" in entry


# Advice ---------------------------------------------------------------------------------------------------------

def test_the_advice_counts_what_the_number_sent_you_and_prices_your_own_call(installation):
    table = sa.Table('sip_call_records', sa.MetaData(), autoload_with=installation)
    with installation.begin() as connection:
        for day, pages in ((1, 3), (2, 2), (40, 9)):
            when = NOW - timedelta(days=day)
            connection.execute(table.insert().values(
                id=uuid4().hex, direction='inbound', call_id=f'engine.{day}', caller='15555550199',
                called='15555550100', started_at=when, answered_at=when, ended_at=when + timedelta(seconds=90),
                disposition='answered', connected_seconds=90, t38='yes', pages=pages, fax_status='SUCCESS',
                fax_preference=0, created_at=when, updated_at=when))
    calls = []

    def predict(route, number, shape, now=None):
        from app.routing.costs import Money
        calls.append((route, number, shape.pages))
        return SimpleNamespace(cost=Money(5_000, 'USD'))
    sentence = polling.advice(installation, NUMBER, now=NOW, predict=predict)
    assert calls == [('sip', NUMBER, 5)]
    assert sentence == ('This number sent you 2 faxes (5 pages, about 3 minutes on the line) in the last 30 days, '
                        'and the other site paid for those calls. Collecting them would cost your own phone line '
                        'about $0.005.')
    assert polling.advice(installation, '+15555550111', now=NOW, predict=predict) is None

    def flat(route, number, shape, now=None):
        from app.routing.costs import Money
        return SimpleNamespace(cost=Money(0, 'USD'))
    assert polling.advice(installation, NUMBER, now=NOW, predict=flat).endswith(
        "Collecting them would add nothing to your phone line's bill.")


# The console's and the command line's routes -----------------------------------------------------------------------

@pytest.fixture
def polling_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, TZ='America/Denver'):
        yield Cli(client)


def test_cli_and_api_show_turn_on_and_refuse_a_collection_the_engine_cannot_take(polling_cli):
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = polling_cli, {'X-API-Key': BOOTSTRAP}
    route = '/routing/destinations/%2B15555550199/polling'
    shown = cli.client.get(route, headers=admin).json()
    assert shown['enabled'] is False and shown['requests'] == [] and shown['advice'] is None
    assert shown['note'] == polling.ADVICE_NOTE
    assert cli.client.post(route + '/collect', headers=admin).json()['detail'] == polling.NOT_ON
    saved = cli.json('recipients', 'polling', NUMBER, '--on', '--name', 'Denver office', '--selective-address', '12')
    assert (saved['enabled'], saved['label'], saved['selective']) == (True, 'Denver office', '12')
    printed = ' '.join(cli('recipients', 'polling', NUMBER).stdout.split())
    assert 'Collecting On' in printed and 'Other site Denver office' in printed
    # The fast fax service is not set up here: refused, nothing dialed, and said in one sentence.
    assert cli('recipients', 'collect', NUMBER).exit_code != 0
    answer = cli.client.post(route + '/collect', headers=admin)
    assert answer.status_code == 409 and answer.json()['detail'].startswith('Faxbot collects faxes with its fast')
    assert cli.json('recipients', 'polling', NUMBER, '--off')['enabled'] is False
    sender = cli.client.post('/admin/api-keys', headers=admin, json={'name': 'synthetic', 'scopes': ['fax:send']})
    key = {'X-API-Key': sender.json()['token']}
    assert cli.client.get(route, headers=key).status_code == 403
    assert cli.client.post(route + '/collect', headers=key).status_code == 403


def test_the_engines_report_on_a_collection_is_its_result_and_an_unknown_one_is_refused(polling_cli, tmp_path):
    from api.app.routing.background import installation_engine
    cli = polling_cli
    # The served installation's own data folder (test_cli._serve), where the engine's secret lives.
    secret = hylafax_engine.engine_secrets(SimpleNamespace(fax_data_dir=str(tmp_path / 'faxdata')),
                                           lines=1)['report_secret']
    report = {'tag': f'{REQUEST}.{REQUEST}', 'why': 'poll_no_document', 'dials': 1}
    route = '/_internal/hylafax/result'
    assert cli.client.post(route, json=report, headers={'X-Internal-Secret': secret}).status_code == 404
    database, _ = installation_engine(cli.client.app)
    polling.record_request(database, REQUEST, NUMBER, now=NOW)
    answer = cli.client.post(route, json=report, headers={'X-Internal-Secret': secret})
    assert answer.status_code == 200, answer.text
    assert polling.requests(database, NUMBER)[0]['outcome'] == 'nothing_waiting'
    # 'done' alone records nothing: the hand-over that carries the fax gives the result.
    other = 'e' * 32
    polling.record_request(database, other, NUMBER, now=NOW + timedelta(minutes=1))
    assert cli.client.post(route, json={**report, 'tag': f'{other}.{other}', 'why': 'done'},
                           headers={'X-Internal-Secret': secret}).status_code == 200
    assert [row['outcome'] for row in polling.requests(database, NUMBER) if row['id'] == other] == [None]


def test_a_collected_fax_reaches_received_from_the_number_faxbot_called(isolated_installation, monkeypatch, tmp_path):
    """The engine's hand-over of a collected fax (hylafax/bin/pollrcvd, then bin/handover) names its collection:
    the fax lands in Received from the number Faxbot called, and the collection's result is that fax. As the
    engine sends it: no caller and no called number (the engine placed the call)."""
    from fastapi.testclient import TestClient
    from PIL import Image
    from api.app.main import app
    from api.app.routing.background import installation_engine
    data = tmp_path / 'faxdata_engine'
    for name, value in (('INBOUND_ENABLED', 'true'), ('ASTERISK_INBOUND_SECRET', 'sekret'),
                        ('FAX_DATA_DIR', str(data)), ('REQUIRE_API_KEY', 'true'), ('API_KEY', 'bootstrap_admin_only'),
                        ('FAX_DEFAULT_COUNTRY', 'US')):
        monkeypatch.setenv(name, value)
    secret = hylafax_engine.engine_secrets(SimpleNamespace(fax_data_dir=str(data)), lines=1)['report_secret']
    folder = data / 'hylafax-out' / 'inbound'
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / 'engine-0123456789abcdef-000000009-1.tiff'
    Image.new('1', (1728, 400), 1).save(image, format='TIFF', compression='group4')
    with TestClient(app, base_url='http://testserver') as client:
        database, _ = installation_engine(client.app)
        polling.record_request(database, REQUEST, NUMBER, now=NOW)
        answer = client.post('/_internal/hylafax/inbound', headers={'X-Internal-Secret': secret}, json={
            'poll': REQUEST, 'tiff_path': str(image), 'to_number': None, 'from_number': None,
            'faxstatus': 'SUCCESS', 'faxpages': 1, 'uniqueid': 'hylafax.0123456789abcdef.000000009-1',
            'call': {'did': None, 'caller': None, 'pages': 1}})
        assert answer.status_code == 200, answer.text
        with database.connect() as connection:
            received = connection.execute(sa.text('SELECT id, from_number FROM inbound_faxes')).mappings().all()
        assert len(received) == 1 and received[0]['from_number'] == NUMBER
        found = polling.requests(database, NUMBER)[0]
        assert (found['outcome'], found['inbound_fax_id'], found['pages']) == ('received', received[0]['id'], 1)
        # The hand-over sent again (the engine keeps a ticket until Faxbot answers): one fax, one result.
        again = client.post('/_internal/hylafax/inbound', headers={'X-Internal-Secret': secret}, json={
            'poll': REQUEST, 'tiff_path': str(image), 'to_number': None, 'from_number': None,
            'faxstatus': 'SUCCESS', 'faxpages': 1, 'uniqueid': 'hylafax.0123456789abcdef.000000009-1',
            'call': {'did': None, 'caller': None, 'pages': 1}})
        assert again.status_code == 200, again.text
        with database.connect() as connection:
            assert connection.execute(sa.text('SELECT COUNT(*) FROM inbound_faxes')).scalar() == 1
            assert connection.execute(sa.text('SELECT COUNT(*) FROM poll_results')).scalar() == 1
