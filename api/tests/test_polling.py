"""Collecting a fax by polling (M21, routing/polling.py): opt-in per number, one call when a person asks, the
engine's poll job and its result, the collected fax handed over into Received, and the advice. Synthetic numbers
and stand-ins only; the loopback proof (tests/test_sslfax_loopback.py, case r) runs the real engines."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
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

    async def prepare_poll(values, ami, *, request_id, number, selective='', password='', trunk=None):
        assert (number, selective, password) == (NUMBER, '77', '')
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
    # And the command a polled call runs (hylafax/patches/0002), with pollq kept for faxgetty's user.
    assert "printf 'PolledCmd:\\t\\t/usr/local/lib/faxbot-engine/polled\\n'" in entry
    assert 'chown uucp:uucp "$spool/pollq"' in entry


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


# Polled transmission (hylafax/patches/0002): directions, passwords, timetables, held faxes ---------------------

class Seal:
    """A stand-in for polling.PollSeal: a reversible envelope, so tests see that only the envelope is stored."""
    def seal(self, password, number):
        return f'sealed:{number}:{password}'

    def open(self, envelope, number):
        prefix = f'sealed:{number}:'
        assert envelope.startswith(prefix)
        return envelope[len(prefix):]


def test_collecting_and_holding_are_separate_settings_and_the_password_is_kept_sealed(installation):
    seal = Seal()
    polling.save_source(installation, NUMBER, enabled=True, label='Denver office', selective='77', password='2468',
                        seal=seal, now=NOW)
    polling.save_source(installation, NUMBER, enabled=True, label='Denver office', direction='hold', password='1357',
                        seal=seal, now=NOW + timedelta(minutes=1))
    collecting, holding = polling.source(installation, NUMBER), polling.hold_setting(installation, NUMBER)
    assert collecting.direction == 'collect' and collecting.has_password and collecting.selective == '77'
    assert holding.direction == 'hold' and holding.has_password and holding.selective is None
    assert seal.open(collecting.password_sealed, NUMBER) == '2468' and seal.open(holding.password_sealed, NUMBER) == '1357'
    with installation.connect() as connection:
        stored = connection.execute(sa.text('SELECT password_sealed FROM poll_sources')).scalars().all()
    assert all(value.startswith('sealed:') for value in stored)
    # None keeps the password, '' clears it; the views say only whether one is set.
    kept = polling.save_source(installation, NUMBER, enabled=False, now=NOW + timedelta(minutes=2))
    assert kept.has_password and polling.view(installation, NUMBER, now=NOW)['has_password'] is True
    cleared = polling.save_source(installation, NUMBER, enabled=True, password='', now=NOW + timedelta(minutes=3))
    assert not cleared.has_password and polling.hold_setting(installation, NUMBER).has_password
    assert polling.hold_view(installation, NUMBER)['has_password'] is True
    with pytest.raises(ValueError):
        polling.save_source(installation, NUMBER, enabled=True, password='12ab', seal=seal)
    with pytest.raises(polling.PollStoreError):
        polling.save_source(installation, NUMBER, enabled=True, password='1234')  # no seal: never stored plain


def test_the_password_goes_with_the_collection_and_the_real_poll_job_carries_it(installation, fake_engine,
                                                                                 monkeypatch):
    seal = Seal()
    polling.save_source(installation, NUMBER, enabled=True, password='2468', seal=seal, now=NOW)

    async def choose(values, **_):
        return hylafax_engine.EngineChoice('hylafax')
    seen = []

    class Job:
        engine_job, tag, submission = '42', '1' * 16, {}

        def submit(self):
            return '42'

        def close(self):
            pass

    async def prepare_poll(values, ami, *, request_id, number, selective='', password='', trunk=None):
        seen.append((selective, password))
        return Job()
    monkeypatch.setattr(hylafax_engine, 'choose', choose)
    monkeypatch.setattr(hylafax_engine, 'prepare_poll', prepare_poll)
    asyncio.run(polling.collect(installation, SimpleNamespace(), Ami(), NUMBER, seal=seal))
    assert seen == [('', '2468')]
    # The real engine job: the password is the second POLL argument, and appears nowhere else.
    values, server = fake_engine
    hylafax_engine.create_poll_job(values, tag=hylafax_engine.new_tag(), request_id=REQUEST, selective='77',
                                   password='24 68', host='127.0.0.1', port=server.server_address[1])
    assert 'JPARM POLL "77" "2468"' in server.commands
    assert sum('2468' in command for command in server.commands) == 1


def test_a_refusal_right_after_dtc_is_recorded_as_refused_with_what_to_check():
    outcome, sentence = hylafax_engine.poll_outcome(
        {'why': 'failed', 'dials': 1, 'pages': 0, 'remote_station_b64': b64('5550199'),
         'status_b64': b64('RSPREC error/got DCN (sender abort) {E103}')})
    assert outcome == 'refused' and 'selective polling address' in sentence and 'password' in sentence
    # The same words after a page arrived mean the call broke off: uncertain, as before.
    outcome, _ = hylafax_engine.poll_outcome(
        {'why': 'failed', 'dials': 1, 'pages': 1, 'remote_station_b64': b64('5550199'),
         'status_b64': b64('RSPREC error/got DCN (sender abort) {E103}')})
    assert outcome == 'uncertain'


def test_the_timetable_names_this_minute_once_in_the_numbers_time_zone_and_skips_an_uncertain_result(installation):
    polling.save_source(installation, NUMBER, enabled=True, collect_times='08:00,16:00',
                        collect_days='mon,tue,wed,thu,fri', time_zone='America/Denver', now=NOW)
    other = '+15555550111'
    polling.save_source(installation, other, enabled=True, collect_times='09:00', collect_days='sat', now=NOW)
    # Thursday 8 October 2026, 08:00 in Denver is 14:00 UTC.
    at_eight = datetime(2026, 10, 8, 14, 0)
    assert polling.due(installation, at_eight) == [NUMBER]
    assert polling.due(installation, at_eight + timedelta(minutes=1)) == []
    assert polling.due(installation, datetime(2026, 10, 10, 9, 0)) == [other]  # Saturday 09:00 UTC
    assert polling.due(installation, datetime(2026, 10, 10, 14, 0)) == []  # Saturday: not a weekday in Denver
    # One slot collects once: a request made in the last minutes holds it off.
    polling.record_request(installation, 'a' * 32, NUMBER, now=at_eight - timedelta(minutes=1))
    assert polling.due(installation, at_eight) == []
    # An uncertain result stops the timetable until a person collects by hand.
    polling.record_result(installation, 'a' * 32, 'uncertain', 'Check Received.', now=at_eight)
    assert polling.due(installation, datetime(2026, 10, 8, 22, 0)) == []
    polling.record_request(installation, 'b' * 32, NUMBER, now=datetime(2026, 10, 8, 20, 0))
    polling.record_result(installation, 'b' * 32, 'received', 'Collected.', now=datetime(2026, 10, 8, 20, 1))
    assert polling.due(installation, datetime(2026, 10, 8, 22, 0)) == [NUMBER]
    view = polling.view(installation, NUMBER, now=NOW)
    assert view['timetable'] == 'Faxbot collects at 08:00 and 16:00 on weekdays (America/Denver).'
    assert polling.save_source(installation, NUMBER, enabled=True, collect_times='', now=NOW).collect_times is None
    for bad in ({'collect_times': '25:00'}, {'collect_days': 'funday'}, {'time_zone': 'Mars/Olympus'}):
        with pytest.raises(ValueError):
            polling.save_source(installation, NUMBER, enabled=True, **bad)
    # Off: a timetable on a number whose collecting is off collects nothing.
    polling.save_source(installation, other, enabled=False, now=NOW + timedelta(minutes=5))
    assert polling.due(installation, datetime(2026, 10, 10, 9, 0)) == []


def test_collect_due_collects_each_due_number_and_skips_one_the_engine_cannot_take(installation, monkeypatch):
    polling.save_source(installation, NUMBER, enabled=True, collect_times='14:00', collect_days='thu', now=NOW)
    collected = []

    async def collect(engine, values, ami, number, **kwargs):
        collected.append((number, kwargs.get('actor_name')))
        if len(collected) == 1:
            raise polling.PollRefused('not now')
        return 'r' * 32
    monkeypatch.setattr(polling, 'collect', collect)
    at = datetime(2026, 10, 8, 14, 0)
    assert asyncio.run(polling.collect_due(installation, SimpleNamespace(), Ami(), now=at)) == []
    assert asyncio.run(polling.collect_due(installation, SimpleNamespace(), Ami(), now=at)) == ['r' * 32]
    assert collected == [(NUMBER, polling.TIMETABLE_BY)] * 2


def test_a_fax_is_held_only_for_a_number_that_may_collect_and_its_collections_are_kept_once(installation, tmp_path,
                                                                                               monkeypatch):
    from PIL import Image
    image = tmp_path / 'held.tiff'
    Image.new('1', (1728, 400), 1).save(image, format='TIFF', compression='group4', dpi=(204, 196))
    with pytest.raises(polling.PollRefused, match='Turn on holding'):
        asyncio.run(polling.hold(installation, SimpleNamespace(), NUMBER, path=image))
    seal = Seal()
    polling.save_source(installation, NUMBER, enabled=True, direction='hold', selective='77', password='2468',
                        seal=seal, now=NOW)

    async def choose(values, **_):
        return hylafax_engine.EngineChoice('hylafax')
    held = []

    def hold_document(values, *, hold_id, tiff_path, sidecar):
        held.append((hold_id, sidecar))
        return f'pollq/faxhold-{hold_id}.tif'
    monkeypatch.setattr(hylafax_engine, 'choose', choose)
    monkeypatch.setattr(hylafax_engine, 'hold_document', hold_document)
    hold_id = asyncio.run(polling.hold(installation, SimpleNamespace(), NUMBER, path=image, source_name='notice.pdf',
                                       actor_name='Ada', seal=seal, now=NOW, tsi='+1 555 555 0100'))
    assert held[0][0] == hold_id
    sidecar = dict(line.split('=', 1) for line in held[0][1].splitlines())
    assert sidecar == {'number': '15555550199', 'selective': '77', 'password': '2468', 'job': hold_id,
                       'tsi': '+1 555 555 0100', 'tagline': '',
                       'held': str(int(NOW.replace(tzinfo=timezone.utc).timestamp()))}
    shown = polling.hold_view(installation, NUMBER)
    assert shown['enabled'] and shown['held'][0]['state'] == 'Held' and shown['held'][0]['pages'] == 1
    assert shown['held'][0]['sentence'] == polling.HELD_WAITING and shown['held'][0]['has_password']
    # The engine's reports: a refusal keeps the fax held; the collection ends it; a report sent again changes nothing.
    report = {'engine_id': '0123456789abcdef', 'key': '9-1700000000', 'job': hold_id, 'outcome': 'refused',
              'reason_b64': b64('Polling refused: the polling password (PWD) does not match the held document'),
              'pages': 0, 'caller': '+15555550199', 'cig': '5550199', 'sep': '77'}
    assert polling.record_polled(installation, report, now=NOW)[0] == 'refused'
    assert polling.record_polled(installation, report, now=NOW)[0] == 'refused'
    shown = polling.hold_view(installation, NUMBER)['held'][0]
    assert shown['state'] == 'Still held'
    assert shown['sentence'].startswith('+15555550199 asked for it with the wrong polling password')
    sent = {**report, 'key': '10-1700000100', 'outcome': 'sent', 'reason_b64': '', 'pages': 1, 'seconds': 31}
    assert polling.record_polled(installation, sent, now=NOW + timedelta(minutes=1)) == (
        'sent', 'Collected by +15555550199 (1 page).')
    shown = polling.hold_view(installation, NUMBER)['held'][0]
    assert (shown['state'], shown['gone']) == ('Collected', True)
    with installation.connect() as connection:
        assert connection.execute(sa.text('SELECT COUNT(*) FROM poll_collections')).scalar() == 2
    assert polling.record_polled(installation, {**sent, 'job': 'f' * 32}, now=NOW) is None
    # A collected fax cannot be withdrawn; nothing goes to the engine for it.
    assert asyncio.run(polling.withdraw(installation, SimpleNamespace(), hold_id, actor_name='Ada')) == 'sent'


def test_withdrawing_a_held_fax_takes_it_out_of_the_engine_once(installation, monkeypatch):
    seal = Seal()
    polling.save_source(installation, NUMBER, enabled=True, direction='hold', seal=seal, now=NOW)
    polling.record_held(installation, 'c' * 32, NUMBER, document='pollq/faxhold-c.tif', pages=2, now=NOW)
    withdrawn = []
    monkeypatch.setattr(hylafax_engine, 'withdraw_held',
                        lambda values, *, hold_id: withdrawn.append(hold_id) or True)
    assert asyncio.run(polling.withdraw(installation, SimpleNamespace(), 'c' * 32, actor_name='Ada')) == 'withdrawn'
    assert asyncio.run(polling.withdraw(installation, SimpleNamespace(), 'c' * 32, actor_name='Ada')) == 'withdrawn'
    assert withdrawn == ['c' * 32]
    shown = polling.hold_view(installation, NUMBER)['held'][0]
    assert (shown['state'], shown['sentence'], shown['gone']) == ('Withdrawn', 'Withdrawn by Ada.', True)
    with pytest.raises(LookupError):
        asyncio.run(polling.withdraw(installation, SimpleNamespace(), 'd' * 32))


def test_the_real_engine_takes_a_held_fax_with_its_sidecar_and_gives_it_back(fake_engine, tmp_path):
    """The real hylafax_engine.hold_document and withdraw_held against the stand-in hfaxd: the image first, the
    sidecar last, both named for the hold, and both deleted on withdrawal."""
    values, server = fake_engine
    image = tmp_path / 'held.tiff'
    image.write_bytes(b'II*\x00held')
    sidecar = hylafax_engine.held_sidecar(number=NUMBER, selective='77', password='2468', job='a' * 32,
                                         tsi='+1 555 555 0100', tagline='x|%c|Page %%P of %%T', held_at=1700000000)
    port = server.server_address[1]
    document = hylafax_engine.hold_document(values, hold_id='a' * 32, tiff_path=str(image), sidecar=sidecar,
                                            host='127.0.0.1', port=port)
    assert document == 'pollq/faxhold-' + 'a' * 32 + '.tif'
    assert server.uploads == [(document, b'II*\x00held'), ('pollq/faxhold-' + 'a' * 32 + '.poll', sidecar.encode())]
    assert sidecar == ('number=15555550199\nselective=77\npassword=2468\njob=' + 'a' * 32
                       + '\ntsi=+1 555 555 0100\ntagline=x|%c|Page %%P of %%T\nheld=1700000000\n')
    assert hylafax_engine.withdraw_held(values, hold_id='a' * 32, host='127.0.0.1', port=port) is True
    assert server.uploads == []
    assert hylafax_engine.withdraw_held(values, hold_id='a' * 32, host='127.0.0.1', port=port) is False
    with pytest.raises(ValueError):
        hylafax_engine.held_sidecar(number=NUMBER, job='not-hex')


def test_the_polled_script_reports_the_collection_and_removes_only_a_sent_document(scripts, tmp_path):  # noqa: F811
    spool, state, data, environment = scripts
    (spool / 'pollq').mkdir()
    for name in ('faxhold-' + 'a' * 32 + '.tif', 'faxhold-' + 'a' * 32 + '.poll'):
        (spool / 'pollq' / name).write_bytes(b'x')
    (spool / 'log' / 'c000000009').write_text(
        'Oct 08 09:00:00.00: [ 1]: POLLED FAX: pollq/faxhold-' + 'a' * 32
        + '.tif sent to "+15555550199", 2 pages in 0:00:31\n')
    (tmp_path / 'answer').write_text('000')
    document = 'pollq/faxhold-' + 'a' * 32 + '.tif'
    # A refusal: reported, nothing removed.
    refused = run('polled', environment, '', 'ttyIAX1', '000000008',
                  'Polling refused: no document is held for that selective polling address (SEP)', '0',
                  '+15555550199', '5550199', '42', '', '+15555550199', '179117219142.15555550100', '', cwd=spool)
    assert refused.returncode == 0, refused.stderr
    reports = sorted((state / 'results').glob('*-polled.report'))
    body = json.loads(reports[0].read_text())
    assert body['outcome'] == 'refused' and body['job'] is None and body['document'] is None
    assert base64.b64decode(body['reason_b64']).decode().startswith('Polling refused')
    assert (body['caller'], body['cig'], body['sep'], body['token']) == ('+15555550199', '5550199', '42',
                                                                        '179117219142')
    assert (spool / 'pollq' / ('faxhold-' + 'a' * 32 + '.tif')).exists()
    # The collection: reported with the pages and seconds, and the document and sidecar are gone.
    sent = run('polled', environment, document, 'ttyIAX1', '000000009', '', '2', '+15555550199', '5550199', '',
               'a' * 32, '+15555550199', '179117219142.15555550100', '', cwd=spool)
    assert sent.returncode == 0, sent.stderr
    reports = sorted((state / 'results').glob('*-polled.report'))
    body = json.loads(reports[-1].read_text())
    assert (body['outcome'], body['job'], body['document'], body['pages'], body['seconds']) == (
        'sent', 'a' * 32, document, 2, 31)
    assert body['key'].startswith('000000009-') and body['engine_id'] == '0123456789abcdef'
    assert list((spool / 'pollq').iterdir()) == []
    # The report goes to Faxbot's polled route; one Faxbot cannot take stays for the next round.
    assert (tmp_path / 'url').read_text().endswith('/_internal/hylafax/polled')
    assert len(sorted((state / 'results').glob('*-polled.report'))) == 2
    # A document outside pollq is refused.
    assert run('polled', environment, 'recvq/fax000000007.tif', 'ttyIAX1', '9', '', '1', '', '', '', '',
               cwd=spool).returncode != 0


def test_the_engines_polled_report_reaches_the_held_fax_and_needs_its_secret(polling_cli, tmp_path):
    from api.app.routing.background import installation_engine
    cli = polling_cli
    secret = hylafax_engine.engine_secrets(SimpleNamespace(fax_data_dir=str(tmp_path / 'faxdata')),
                                           lines=1)['report_secret']
    database, _ = installation_engine(cli.client.app)
    polling.record_held(database, 'c' * 32, NUMBER, document='pollq/faxhold-c.tif', pages=2, now=NOW)
    report = {'engine_id': '0123456789abcdef', 'key': '9-1700000000', 'job': 'c' * 32, 'outcome': 'sent',
              'reason_b64': '', 'pages': 2, 'caller': '+15555550199'}
    route = '/_internal/hylafax/polled'
    assert cli.client.post(route, json=report).status_code in (401, 403)
    assert cli.client.post(route, json={**report, 'job': 'd' * 32},
                           headers={'X-Internal-Secret': secret}).status_code == 404
    answer = cli.client.post(route, json=report, headers={'X-Internal-Secret': secret})
    assert answer.status_code == 200 and answer.json()['outcome'] == 'sent', answer.text
    assert polling.hold_view(database, NUMBER)['held'][0]['state'] == 'Collected'


def test_cli_and_api_hold_settings_and_a_held_document_need_the_engine(polling_cli, tmp_path):
    from api.tests.test_cli import BOOTSTRAP
    cli, admin = polling_cli, {'X-API-Key': BOOTSTRAP}
    route = '/routing/destinations/%2B15555550199/polling'
    shown = cli.json('recipients', 'hold', NUMBER)
    assert shown['enabled'] is False and shown['held'] == [] and shown['note'] == polling.HOLD_NOTE
    saved = cli.json('recipients', 'hold', NUMBER, '--on', '--name', 'Denver office', '--selective-address', '77',
                     '--password', '2468')
    assert (saved['enabled'], saved['label'], saved['selective'], saved['has_password']) == (
        True, 'Denver office', '77', True)
    printed = ' '.join(cli('recipients', 'hold', NUMBER).stdout.split())
    assert 'Collects from Faxbot On' in printed and 'Polling password it must give Set' in printed
    assert '2468' not in printed
    # The collecting side: a password and a timetable, shown as set, never shown as digits.
    timed = cli.json('recipients', 'polling', NUMBER, '--on', '--password', '1357', '--collect-at', '08:00,16:00',
                     '--collect-days', 'mon,tue,wed,thu,fri', '--time-zone', 'America/Denver')
    assert timed['has_password'] is True
    assert timed['timetable'] == 'Faxbot collects at 08:00 and 16:00 on weekdays (America/Denver).'
    assert '1357' not in cli('recipients', 'polling', NUMBER).stdout
    assert cli.json('recipients', 'polling', NUMBER, '--no-timetable')['timetable'] is None
    # Holding a document: the fast fax service is not set up here, so nothing is held and the answer says so.
    document = tmp_path / 'notice.pdf'
    document.write_bytes(b'%PDF-1.4\n%%EOF\n')
    held = cli('recipients', 'hold-fax', NUMBER, str(document))
    assert held.exit_code != 0 and 'fast fax service' in (held.stdout + held.stderr)
    with document.open('rb') as handle:
        answer = cli.client.post(route + '/hold/faxes', headers=admin,
                                 files={'file': ('notice.pdf', handle, 'application/pdf')})
    assert answer.status_code == 409 and 'fast fax service' in answer.json()['detail']
    assert cli.client.delete(route + '/hold/faxes/' + 'e' * 32, headers=admin).status_code == 404
    sender = cli.client.post('/admin/api-keys', headers=admin, json={'name': 'synthetic', 'scopes': ['fax:send']})
    key = {'X-API-Key': sender.json()['token']}
    assert cli.client.get(route + '/hold', headers=key).status_code == 403
