"""Power-aware admission and the NUT reader (brief 92, RF item 3).

The UPS is a fake ``upsd`` on localhost that answers NUT's network protocol line by line. No real UPS is read.
"""
import socketserver
import threading

import pytest
from fastapi.testclient import TestClient

from app import main, nut, power
from app.power import Admission, PowerState, admission, power_allows
from app.schema import upgrade_schema
from api.tests.test_schema import database  # noqa: F401 - fixture

BOOTSTRAP = 'synthetic-power-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


class FakeUpsd:
    """A NUT server: ``variables`` per UPS name; a value that is an ``ERR ...`` line is answered as that error."""

    def __init__(self, variables, *, list_error=None):
        self.variables = variables
        self.list_error = list_error
        self.commands = []
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for raw in self.rfile:
                    line = raw.decode().strip()
                    fake.commands.append(line)
                    if line == 'LOGOUT':
                        self.wfile.write(b'OK Goodbye\n')
                        return
                    if line == 'LIST UPS':
                        if fake.list_error:
                            self.wfile.write(f'ERR {fake.list_error}\n'.encode())
                            continue
                        body = ['BEGIN LIST UPS'] + [f'UPS {name} "Office \\"rack\\" UPS"' for name in fake.variables]
                        self.wfile.write(('\n'.join(body + ['END LIST UPS']) + '\n').encode())
                        continue
                    parts = line.split(' ')
                    if parts[:2] == ['GET', 'VAR'] and len(parts) == 4:
                        ups, name = parts[2], parts[3]
                        if ups not in fake.variables:
                            self.wfile.write(b'ERR UNKNOWN-UPS\n')
                            continue
                        value = fake.variables[ups].get(name)
                        if value is None:
                            self.wfile.write(b'ERR VAR-NOT-SUPPORTED\n')
                        elif value.startswith('ERR '):
                            self.wfile.write((value + '\n').encode())
                        else:
                            escaped = value.replace('\\', '\\\\').replace('"', '\\"')
                            self.wfile.write(f'VAR {ups} {name} "{escaped}"\n'.encode())
                        continue
                    self.wfile.write(b'ERR UNKNOWN-COMMAND\n')

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def fresh_cache():
    power._cache.clear()
    yield
    power._cache.clear()


def test_the_reader_reads_status_runtime_and_charge_like_upsc():
    with FakeUpsd({'office': {'ups.status': 'OB DISCHRG', 'battery.runtime': '300', 'battery.charge': '45.5'}}) as ups:
        reading = nut.read('127.0.0.1', port=ups.port)
    assert reading == nut.Reading('office', frozenset({'OB', 'DISCHRG'}), 300, 45.5)
    assert reading.on_battery and not reading.low_battery
    assert ups.commands == ['LIST UPS', 'GET VAR office ups.status', 'GET VAR office battery.runtime',
                            'GET VAR office battery.charge', 'LOGOUT']


def test_a_variable_the_ups_does_not_report_stays_unknown_and_a_named_ups_is_asked_directly():
    with FakeUpsd({'office': {'ups.status': 'OL CHRG'}, 'lab': {'ups.status': 'OL'}}) as ups:
        reading = nut.read('127.0.0.1', port=ups.port, ups='lab')
    assert reading.ups == 'lab' and reading.runtime_seconds is None and reading.charge_percent is None
    assert not reading.on_battery and ups.commands[0] == 'GET VAR lab ups.status'


@pytest.mark.parametrize('variables, list_error, sentence', [
    ({'office': {'ups.status': 'ERR DATA-STALE'}}, None, 'no fresh reading from the UPS'),
    ({'office': {'ups.status': 'ERR DRIVER-NOT-CONNECTED'}}, None, 'driver is not connected'),
    ({}, 'ACCESS-DENIED', 'does not let this computer read it'),
    ({}, None, 'lists no UPS'),
    ({'office': {}}, None, 'does not report whether it is on mains or battery'),
])
def test_refusals_read_as_one_plain_sentence(variables, list_error, sentence):
    with FakeUpsd(variables, list_error=list_error) as ups:
        with pytest.raises(nut.NutError, match=sentence):
            nut.read('127.0.0.1', port=ups.port)


def test_an_unknown_ups_name_and_a_closed_port_are_plain_refusals():
    with FakeUpsd({'office': {'ups.status': 'OL'}}) as ups:
        with pytest.raises(nut.NutError, match='does not know a UPS by that name'):
            nut.read('127.0.0.1', port=ups.port, ups='garage')
        port = ups.port
    with pytest.raises(nut.NutError, match='could not connect to the UPS server'):
        nut.read('127.0.0.1', port=port, timeout=0.5)
    assert nut.unquote(r'"a \"quoted\" \\ value"') == 'a "quoted" \\ value'


@pytest.mark.parametrize('runtime, need, reserve, allowed', [
    (300, 180, 120, True), (300, 181, 120, False), (None, 999, 120, True), (300, None, 120, True), (60, 60, 0, True),
])
def test_power_allows_compares_the_call_plus_its_reserve_with_the_runtime(runtime, need, reserve, allowed):
    assert power_allows(runtime, need, reserve) is allowed


def _battery(runtime, reserve=120):
    return PowerState(True, nut.Reading('office', frozenset({'OB'}), runtime, 40.0), reserve_seconds=reserve)


def test_a_call_that_does_not_fit_on_battery_goes_by_another_power_domain_or_waits_unsent():
    on_mains = PowerState(True, nut.Reading('office', frozenset({'OL'}), 60, 100.0))
    assert admission(on_mains, provider='sip', p90_seconds=900) == Admission('go')
    assert admission(_battery(600), provider='sip', p90_seconds=400) == Admission('go')
    other = admission(_battery(300), provider='sip', p90_seconds=400,
                      others=[('sip-2', 'Second trunk', 'sip', 200), ('sinch', 'Sinch', 'sinch', 400)])
    assert other.action == 'other' and other.account == 'sinch'
    assert other.sentence == ('This office is on battery with about 5 minutes left and this fax\'s call could take up '
                              'to 7 minutes, so Faxbot sends it by Sinch, whose call does not depend on this office\'s '
                              'power.')
    held = admission(_battery(300), provider='sip', p90_seconds=400)
    assert held.action == 'hold' and held.sentence.endswith('so Faxbot holds it, unsent, until the power is back or '
                                                            'the battery can cover the call.')
    urgent = admission(_battery(300), provider='sip', p90_seconds=400, urgent=True)
    assert urgent.action == 'hold' and urgent.sentence.endswith('It is urgent: if you have another way to send it, '
                                                                'use it now.')
    # A fax service only needs Faxbot to hand the fax over: its call runs on the service's power.
    assert admission(_battery(200), provider='sinch', p90_seconds=900) == Admission('go')
    # An unknown runtime or duration never holds a fax.
    assert admission(_battery(None), provider='sip', p90_seconds=900) == Admission('go')
    assert admission(_battery(100), provider='sip', p90_seconds=None).action == 'go'


def test_the_ups_turns_on_by_itself_once_set_and_holds_through_the_real_settings(database):  # noqa: F811
    upgrade_schema(database)
    assert power.state(database) == PowerState(False)
    with FakeUpsd({'office': {'ups.status': 'OB', 'battery.runtime': '240'}}) as ups:
        power.save(database, '127.0.0.1', port=ups.port, reserve_seconds=60)
        found = power.state(database)
        assert found.on_battery and found.runtime_seconds == 240
        assert power.state_view(found)['sentence'] == ('On battery, with about 4 minutes left. Faxbot starts a call '
                                                       'on this server only when it can finish with 1 minute to '
                                                       'spare.')
        from app.config_values import ConfigurationValues
        decision = power.admission_for(database, ConfigurationValues.from_environment({}), 'sip', 300)
        assert decision.action == 'hold'
    power.save(database, '')
    assert power.state(database) == PowerState(False)
    with pytest.raises(ValueError, match='port from 1 to 65535'):
        power.save(database, 'ups.local', port=70000)


def test_an_unreadable_ups_is_attention_and_never_holds(database):  # noqa: F811
    upgrade_schema(database)
    power.save(database, '127.0.0.1', port=9)
    found = power.state(database, reader=lambda *args, **kwargs: (_ for _ in ()).throw(
        nut.NutError('Faxbot could not connect to the UPS server at 127.0.0.1 port 9.')))
    assert power.state_view(found) == {'status': 'attention', 'sentence': 'Faxbot could not read the UPS: Faxbot could '
                                       'not connect to the UPS server at 127.0.0.1 port 9. Faxes are sent as usual '
                                       'until it can.'}
    assert admission(found, provider='sip', p90_seconds=900) == Admission('go')


def test_the_power_settings_through_the_real_server(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(main.app, base_url='https://testserver') as client:
        empty = client.get('/power', headers=ADMIN).json()
        assert empty['configured'] is False and empty['status'] == 'off'
        with FakeUpsd({'office': {'ups.status': 'OL', 'battery.runtime': '1800'}}) as ups:
            saved = client.put('/power', headers=ADMIN, json={'host': '127.0.0.1', 'port': ups.port,
                                                              'reserve_minutes': 3})
            assert saved.status_code == 200, saved.text
            assert saved.json()['sentence'] == 'On mains power. The battery would last about 30 minutes.'
            assert saved.json()['reserve_minutes'] == 3 and saved.json()['runtime_minutes'] == 30
        off = client.put('/power', headers=ADMIN, json={'host': ''})
        assert off.json()['configured'] is False
        assert client.put('/power', headers=ADMIN, json={'host': 'ups local'}).status_code == 400
        assert client.get('/power').status_code in (401, 403)


def test_the_command_line_sets_shows_and_turns_off_the_ups(isolated_installation, monkeypatch):
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(main.app, base_url='https://testserver') as client:
        def run(*args):
            return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                      obj={'client_factory': lambda address, timeout: (client, False)},
                                      env={'COLUMNS': '220', 'TZ': 'UTC'})
        assert 'No UPS is set.' in run('system', 'diagnostics', 'power', 'show').stdout
        with FakeUpsd({'office': {'ups.status': 'OB', 'battery.runtime': '600'}}) as ups:
            saved = run('system', 'diagnostics', 'power', 'set', '--address', '127.0.0.1', '--port', str(ups.port))
            assert saved.exit_code == 0, saved.stdout
            flat = ' '.join(saved.stdout.split())
            assert flat.startswith('Needs attention. On battery, with about 10 minutes left.')
            assert f'UPS server: 127.0.0.1 port {ups.port}. Reserve: 2 minutes.' in flat
        off = run('system', 'diagnostics', 'power', 'off')
        assert off.exit_code == 0 and 'No UPS is set.' in off.stdout
