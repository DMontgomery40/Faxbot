"""faxbot overview: the Overview's Needs attention list, from the same answers the console's tests use.

overviewAttention.json gives each scenario's reads (status and body), the items both surfaces compute and
the lines the command prints; dashboard.test.tsx renders the console from the same file. The command runs
through the real faxbot command against an in-memory server that answers only those reads.
"""
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from app.cli.main import app as cli_app

ORIGIN = 'https://testserver'
KEY = 'synthetic-overview-key'
FIXTURE = Path(__file__).resolve().parents[1] / 'admin_ui/src/__tests__/overviewAttention.json'
SCENARIOS = json.loads(FIXTURE.read_text(encoding='utf-8'))['scenarios']
FIELDS = ('key', 'group', 'label', 'count', 'detail', 'serious', 'command')


def faxbot(responses, *args):
    """Run faxbot with a server that answers only the given reads; every request it made is returned too."""
    asked = []

    def handler(request):
        asked.append(request.url.path)
        reply = responses.get(request.url.path)
        if reply is None:
            return httpx.Response(404, json={'detail': 'Not Found'})
        return httpx.Response(reply['status'], json=reply['body'])
    client = httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handler))
    result = CliRunner().invoke(cli_app, ['--url', ORIGIN, '--key', KEY, *args],
                                obj={'client_factory': lambda address, timeout: (client, False)},
                                env={'COLUMNS': '220', 'TZ': 'UTC'})
    return result, asked


@pytest.mark.parametrize('scenario', SCENARIOS, ids=[scenario['name'] for scenario in SCENARIOS])
def test_faxbot_overview_prints_the_same_attention_items_as_the_console(scenario):
    result, asked = faxbot(scenario['responses'], 'overview')
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == scenario['cli']
    assert sorted(set(asked)) == sorted(scenario['responses'])


@pytest.mark.parametrize('scenario', SCENARIOS, ids=[scenario['name'] for scenario in SCENARIOS])
def test_faxbot_overview_json_has_each_item_with_its_command(scenario):
    result, _ = faxbot(scenario['responses'], 'overview', '--json')
    assert result.exit_code == 0, result.stdout + result.stderr
    attention = json.loads(result.stdout)['attention']
    assert [{name: item[name] for name in FIELDS} for item in attention['items']] == [
        {name: item[name] for name in FIELDS} for item in scenario['items']]
    assert attention['serious'] is scenario['serious']
    # Every source answered exactly when the command names none it could not check.
    assert attention['complete'] is not any(line.startswith(('Not available', 'Could not check'))
                                            for line in scenario['cli'])


def test_every_command_it_names_exists():
    named = {item['command'] for scenario in SCENARIOS for item in scenario['items']} | {'faxbot providers list'}
    for command in sorted(named):
        words = command.split()[1:]
        result = CliRunner().invoke(cli_app, [*words, '--help'])
        assert result.exit_code == 0, f'{command}: {result.stdout}'


def test_a_key_faxbot_does_not_accept_or_a_server_it_cannot_reach_stops_the_command():
    refused = {path: {'status': 401, 'body': {'detail': 'Authentication required or credentials no longer valid.'}}
               for path in SCENARIOS[0]['responses']}
    result, _ = faxbot(refused, 'overview')
    assert result.exit_code == 3
    assert 'Faxbot did not accept the API key.' in result.stderr

    def unreachable(request):
        raise httpx.ConnectError('synthetic: nothing listening')
    client = httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(unreachable))
    result = CliRunner().invoke(cli_app, ['--url', ORIGIN, '--key', KEY, 'overview'],
                                obj={'client_factory': lambda address, timeout: (client, False)})
    assert result.exit_code == 8
    assert 'Could not reach Faxbot at https://testserver.' in result.stderr


def test_no_provider_names_the_providers_command_and_is_not_serious():
    responses = {**SCENARIOS[1]['responses'], '/admin/health-status': {'status': 200, 'body': {
        **SCENARIOS[1]['responses']['/admin/health-status']['body'], 'backend': '', 'backend_healthy': False,
        'receiving_backend': '', 'receiving_ready': False}}}
    result, _ = faxbot(responses, 'overview')
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ['Needs attention', 'To check', '  No fax provider is set up yet',
                                          '    faxbot providers list']
