"""The checked number-move workflow is usable without the console."""
import json
import httpx
from typer.testing import CliRunner
from app.cli.main import app


def invoke(*args):
    requests = []
    def handle(request):
        requests.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={'sentence': 'Synthetic checked move.', 'state': 'open', 'steps': [], 'numbers': []})
    with httpx.Client(transport=httpx.MockTransport(handle), base_url='https://faxbot.example') as client:
        result = CliRunner().invoke(app, ['--url', 'https://faxbot.example', '--key', 'synthetic', '--json',
                                         'numbers', *args], obj={'client_factory': lambda *_: (client, False)})
    assert result.exit_code == 0, result.output
    return requests


def test_number_advice_and_dependency_answers():
    assert invoke('advice')[0][:2] == ('GET', '/routing/recommendations/lines')
    request = invoke('dependencies', '+15550001001', 'broadband', 'no', '--note', 'Separate line')[0]
    assert request == ('POST', '/routing/numbers/+15550001001/dependencies',
                       {'question': 'broadband', 'answer': 'no', 'note': 'Separate line'})


def test_move_reads_and_records_without_sending_a_fax():
    assert invoke('move', 'show', '+15550001001')[0][:2] == ('GET', '/routing/numbers/+15550001001/move')
    assert invoke('move', 'start', '+15550001001', '--to-account', 'new')[0][2] == {'to_account': 'new'}
    assert invoke('move', 'record', '+15550001001', 'cutover', 'done')[0][1:] == (
        '/routing/numbers/+15550001001/move/steps/cutover', {'state': 'done', 'note': ''})
    assert invoke('move', 'test', '+15550001001', '--origin', 'old')[0][1:] == (
        '/routing/numbers/+15550001001/move/tests', {'origin': 'old'})
    assert invoke('move', 'forget', '+15550001001')[0][:2] == ('POST', '/routing/numbers/+15550001001/move/forget')
