"""Analysis commands keep keys out of command arguments and preserve server freshness state."""
import json
import httpx
from typer.testing import CliRunner
from app.cli.main import app


def invoke(*args, handler, input=None):
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url='https://faxbot.example')
    try:
        return CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', *args],
                                  input=input, obj={'client_factory': lambda *_: (client, False)})
    finally:
        client.close()


def test_analysis_displays_stale_success_and_failed_refresh():
    result = invoke('system', 'analysis', 'status', handler=lambda request: httpx.Response(200, json={
        'state': 'failed', 'message': 'The model provider could not be reached.', 'stale': True,
        'last_run': {'summary': 'Check unpriced calls before comparing plans.', 'finished_at': '2026-10-01T12:00:00',
                     'model': 'chosen-model', 'evidence': [], 'usage': {}}, 'next_run_at': None}))
    assert result.exit_code == 0, result.output
    assert 'Check unpriced calls' in result.stdout
    assert 'out of date' in result.stdout
    assert 'could not be reached' in result.stdout


def test_manual_refresh_queues_once_and_reports_server_result():
    requests = []
    def handle(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={'state': 'queued', 'message': 'Analysis queued.', 'last_run': None})
    result = invoke('--json', 'system', 'analysis', 'run', handler=handle)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['state'] == 'queued'
    assert requests == [('POST', '/analysis/run')]


def test_connection_test_failure_is_nonzero_and_safe():
    result = invoke('system', 'analysis', 'test', handler=lambda request:
                    httpx.Response(200, json={'ok': False, 'message': 'Check the API key.'}))
    assert result.exit_code != 0
    assert 'Check the API key.' in result.output


def test_configure_reads_key_from_stdin_and_uses_revision():
    written = []
    def handle(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'_meta': {'desired_revision_id': 'revision-1'}, 'analysis': {}})
        written.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True, 'changed': True})
    result = invoke('system', 'analysis', 'configure', '--provider', 'openrouter', '--model', 'vendor/model',
                    '--key-stdin', '--enable', handler=handle, input='synthetic-private-key\n')
    assert result.exit_code == 0, result.output
    assert written == [{'analysis_provider': 'openrouter', 'analysis_base_url': 'https://openrouter.ai/api/v1',
                        'analysis_model': 'vendor/model', 'analysis_api_key': 'synthetic-private-key',
                        'analysis_enabled': True, 'expected_revision_id': 'revision-1'}]
    assert 'synthetic-private-key' not in result.output


def test_costs_analysis_reads_same_saved_result():
    requests = []
    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={'state': 'not_configured', 'message': 'Set up AI analysis.', 'last_run': None})
    result = invoke('costs', 'analysis', handler=handle)
    assert result.exit_code == 0, result.output
    assert requests == ['/analysis']
