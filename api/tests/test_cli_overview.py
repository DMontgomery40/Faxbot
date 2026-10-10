"""faxbot overview: the console's Overview, from the same answers the console's tests use.

overviewBlocks.json gives each installation's reads (status and body) and the lines the command prints;
overviewAttention.json does the same for Needs attention, with the items both surfaces compute.
dashboard.test.tsx and overviewBlocks.test.tsx render the console from the same files. The command runs
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
TESTS = Path(__file__).resolve().parents[1] / 'admin_ui/src/__tests__'
ATTENTION = json.loads((TESTS / 'overviewAttention.json').read_text(encoding='utf-8'))['scenarios']
BLOCKS = json.loads((TESTS / 'overviewBlocks.json').read_text(encoding='utf-8'))
FIELDS = ('key', 'group', 'label', 'count', 'detail', 'serious', 'command')
VALUE_READS = ('/routing/capabilities', '/routing/savings', '/routing/recommendations/sending',
               '/routing/recommendations/facts')


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


def with_value(responses):
    """An attention scenario's reads, plus the value blocks' reads of a healthy installation."""
    return {**{path: BLOCKS['base'][path] for path in VALUE_READS}, **responses}


def attention_section(stdout):
    """The Needs attention block of the command's output, as overviewAttention.json writes it (not indented)."""
    lines = stdout.splitlines()
    start = lines.index('Needs attention')
    section = ['Needs attention']
    for line in lines[start + 1:]:
        if not line.startswith('  '):
            break
        section.append(line[2:])
    return section


@pytest.mark.parametrize('scenario', BLOCKS['scenarios'], ids=[scenario['name'] for scenario in BLOCKS['scenarios']])
def test_faxbot_overview_prints_the_same_blocks_as_the_console(scenario):
    responses = {**BLOCKS['base'], **scenario['responses']}
    result, asked = faxbot(responses, 'overview')
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == scenario['cli']
    assert sorted(set(asked)) == sorted(responses)
    order = json.loads(faxbot(responses, 'overview', '--json')[0].stdout)['order']
    assert order == scenario['order']


@pytest.mark.parametrize('scenario', ATTENTION, ids=[scenario['name'] for scenario in ATTENTION])
def test_faxbot_overview_prints_the_same_attention_items_as_the_console(scenario):
    result, _ = faxbot(with_value(scenario['responses']), 'overview')
    assert result.exit_code == 0, result.stdout + result.stderr
    assert attention_section(result.stdout) == scenario['cli']
    # A serious problem puts Needs attention first.
    assert (result.stdout.splitlines()[0] == 'Needs attention') is scenario['serious']


@pytest.mark.parametrize('scenario', ATTENTION, ids=[scenario['name'] for scenario in ATTENTION])
def test_faxbot_overview_json_has_each_item_with_its_command(scenario):
    result, _ = faxbot(with_value(scenario['responses']), 'overview', '--json')
    assert result.exit_code == 0, result.stdout + result.stderr
    attention = json.loads(result.stdout)['attention']
    assert [{name: item[name] for name in FIELDS} for item in attention['items']] == [
        {name: item[name] for name in FIELDS} for item in scenario['items']]
    assert attention['serious'] is scenario['serious']
    # Every source answered exactly when the command names none it could not check.
    assert attention['complete'] is not any(line.startswith(('Not available', 'Could not check'))
                                            for line in scenario['cli'])


def test_every_command_it_names_exists():
    lines = [line.strip() for scenario in ATTENTION + BLOCKS['scenarios'] for line in scenario['cli']]
    named = {line[line.index('faxbot '):] for line in lines if 'faxbot ' in line} | {'faxbot providers list'}
    assert len(named) > 10
    for command in sorted(named):
        words = command.split()[1:]
        result = CliRunner().invoke(cli_app, [*words, '--help'])
        assert result.exit_code == 0, f'{command}: {result.stdout}'


def test_a_key_faxbot_does_not_accept_or_a_server_it_cannot_reach_stops_the_command():
    refused = {path: {'status': 401, 'body': {'detail': 'Authentication required or credentials no longer valid.'}}
               for path in BLOCKS['base']}
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
    clear = ATTENTION[1]['responses']
    responses = {**clear, '/admin/health-status': {'status': 200, 'body': {
        **clear['/admin/health-status']['body'], 'backend': '', 'backend_healthy': False,
        'receiving_backend': '', 'receiving_ready': False}}}
    result, _ = faxbot(with_value(responses), 'overview')
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == 'What Faxbot is doing'
    assert attention_section(result.stdout) == ['Needs attention', 'To check', '  No fax provider is set up yet',
                                                '    faxbot providers list']


def test_the_full_capabilities_answer_puts_every_capability_ready_to_turn_on_among_the_next_improvements():
    """Against UX-B's whole fixture, not the trimmed copy: nothing ready to turn on is dropped (up to the cap)."""
    from app.cli.commands.overview import IMPROVEMENTS_SHOWN, next_improvements
    full = json.loads((TESTS / 'capabilities.json').read_text(encoding='utf-8'))['response']
    ready = [c['name'] for outcome in full['outcomes'] for c in outcome['capabilities'] if c['ready']]
    assert ready
    listed = [item['title'] for item in next_improvements(full, None, None)]
    assert sorted(listed) == sorted(ready[:IMPROVEMENTS_SHOWN])
