"""faxbot status: the live outbound board from GET /outbound/now, in plain words and local times, and --watch.

The command runs through the real faxbot command against an in-memory server that answers only its reads.
"""
import json

import httpx
import pytest
from typer.testing import CliRunner

from app.cli.commands import status as status_command
from app.cli.main import app as cli_app

ORIGIN = 'https://testserver'
KEY = 'synthetic-status-key'
FAX = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'

BOARD = {
    'as_of': '2026-10-13T15:20:05Z',
    'not_checked': [],
    'sending': {'count': 1, 'items': [{
        'id': FAX, 'to': '********5512', 'recipient': 'Riverside Clinic', 'pages': 4, 'page': None,
        'route': 'Telnyx', 'engine': 'SSL Fax engine', 'started_at': '2026-10-13T15:18:00Z'}]},
    'waiting': {'count': 32, 'more': 1, 'reasons': [
        {'key': 'lines_busy', 'count': 30, 'sentence': 'Waiting for a free line: all 2 lines are in use.'},
        {'key': 'number_busy', 'count': 1, 'sentence': 'Waiting: another fax is calling the same number.'},
        {'key': 'next', 'count': 0, 'sentence': 'Next to go.'}],
        'items': [{'id': 'b' * 32, 'to': '********0123', 'recipient': None, 'pages': 1, 'reason': 'lines_busy',
                   'sentence': 'Waiting for a free line: all 2 lines are in use.',
                   'since': '2026-10-13T15:02:00Z', 'until': None}]},
    'needs_you': {'count': 2, 'items': [
        {'id': 'c' * 32, 'kind': 'no_route', 'to': '********0124', 'recipient': None, 'pages': 2,
         'since': '2026-10-13T15:00:00Z', 'sentence': 'No account is allowed for this fax. It waits for you in Sent.',
         'hold': {'id': 'h' * 32, 'version': 3}, 'actions': ['send_by_account', 'check_again', 'refuse'],
         'until': None},
        {'id': 'd' * 32, 'kind': 'uncertain', 'to': '********0125', 'recipient': None, 'pages': 1,
         'since': '2026-10-13T14:00:00Z', 'sentence': 'Faxbot could not confirm delivery.', 'hold': None,
         'actions': ['settle'], 'until': None}]},
    'today': {'since': '2026-10-13T06:00:00Z', 'accepted': 260, 'delivered': 231, 'failed': 12,
              'failures': [{'key': 'busy', 'label': 'The line was busy', 'count': 5},
                           {'key': 'other', 'label': 'Another problem (see Sent)', 'count': 7}]},
    'lines': [{'key': 'sip', 'label': 'Telnyx', 'in_use': 2, 'limit': 2, 'incoming': 1,
               'sentence': '2 of 2 calls in use (1 incoming). Your carrier allows 2 at once.'}],
    'engine': {'audio': True, 'since': '2026-10-13T03:00:00Z',
               'sentence': 'Your network changes port numbers, so fax over IP (T.38) cannot work; Faxbot sends '
                           'audio fax until the network is fixed.',
               'fix': 'Faxbot switches back by itself once your network lets fax over IP (T.38) data come back. '
                      'Carrier trunk shows the network check.'},
    'groups': [{'who': 'EHR outbound (API key)', 'total': 240, 'delivered': 180, 'sending': 2, 'waiting': 40,
                'needs_you': 3, 'failed': 15}],
}


def faxbot(responses, *args):
    """Run faxbot with a server that answers only the given reads; every path it asked is returned too."""
    asked = []

    def handler(request):
        asked.append(request.url.path)
        reply = responses.get(request.url.path)
        if reply is None:
            return httpx.Response(404, json={'detail': 'Not Found'})
        return httpx.Response(200, json=reply)
    client = httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handler))
    result = CliRunner().invoke(cli_app, ['--url', ORIGIN, '--key', KEY, *args],
                                obj={'client_factory': lambda address, timeout: (client, False)},
                                env={'COLUMNS': '220', 'TZ': 'UTC'})
    return result, asked


def test_faxbot_status_prints_the_board_in_the_contracts_order_with_local_times():
    result, asked = faxbot({'/outbound/now': BOARD}, 'status')
    assert result.exit_code == 0, result.output
    assert asked == ['/outbound/now']
    lines = result.stdout.splitlines()
    assert lines == [
        'As of 2026-10-13 15:20 UTC',
        'Sending now: 1',
        '  ********5512 (Riverside Clinic), 4 pages: by Telnyx with the SSL Fax engine, started 2026-10-13 15:18 UTC',
        'Waiting: 32',
        '  30  Waiting for a free line: all 2 lines are in use.',
        '  1  Waiting: another fax is calling the same number.',
        '  1  Further back in the queue; Faxbot works out why once they move up.',
        '  First in the queue:',
        '    ********0123, 1 page: Waiting for a free line: all 2 lines are in use. Waiting since 2026-10-13 15:02 '
        'UTC.',
        'Needs you: 2',
        '  ********0124, 2 pages: No account is allowed for this fax. It waits for you in Sent. Since 2026-10-13 '
        '15:00 UTC.',
        f"    faxbot faxes sent approve {'c' * 32} --account KEY",
        f"    faxbot faxes sent check-again {'c' * 32}",
        f"    faxbot faxes sent refuse {'c' * 32} --reason \"...\"",
        '  ********0125, 1 page: Faxbot could not confirm delivery. Since 2026-10-13 14:00 UTC.',
        f"    faxbot faxes sent settle {'d' * 32} --delivered | --not-delivered | --cant-tell --reason \"...\"",
        'Today, since 2026-10-13 06:00 UTC: 260 accepted, 231 delivered, 12 failed',
        '  The line was busy: 5',
        '  Another problem (see Sent): 7',
        'Lines',
        '  Telnyx: 2 of 2 calls in use (1 incoming). Your carrier allows 2 at once.',
        'Fax engine',
        '  ' + BOARD['engine']['sentence'],
        '  ' + BOARD['engine']['fix'],
        'Big sends today',
        '  EHR outbound (API key): 240 faxes: 180 delivered, 2 sending, 40 waiting, 3 need you, 15 failed',
    ]


def test_faxbot_status_names_sections_it_could_not_check_and_leaves_out_empty_ones():
    board = {**BOARD, 'not_checked': ['lines', 'engine'], 'lines': [], 'engine': None, 'groups': [],
             'needs_you': {'count': 0, 'items': []}}
    result, _ = faxbot({'/outbound/now': board}, 'status')
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert 'Lines' not in lines and 'Fax engine' not in lines and 'Big sends today' not in lines
    assert 'Needs you: 0' in lines
    assert result.stdout.splitlines()[-1] == ('Not checked just now: lines, fax engine. Faxbot tries again at the '
                                              'next look.')


def test_faxbot_status_json_prints_the_response_as_it_came():
    result, _ = faxbot({'/outbound/now': BOARD}, 'status', '--json')
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == BOARD


def test_faxbot_status_watch_shows_the_board_again_until_ctrl_c(monkeypatch):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(status_command.time, 'sleep', sleep)
    result, asked = faxbot({'/outbound/now': BOARD}, 'status', '--watch')
    assert result.exit_code == 0, result.output
    assert asked == ['/outbound/now', '/outbound/now'] and sleeps == [5, 5]
    assert result.stdout.count('Sending now: 1') == 2


def test_faxbot_status_with_a_fax_id_still_shows_that_fax():
    job = {'id': FAX, 'to': '********5512', 'status': 'in_progress', 'backend': 'phaxio'}
    result, asked = faxbot({f'/fax/{FAX}': job}, 'status', FAX)
    assert result.exit_code == 0, result.output
    assert asked[0] == f'/fax/{FAX}' and FAX in result.stdout
    refused, _ = faxbot({}, 'status', FAX, '--watch')
    assert refused.exit_code == 1 and '--watch shows every fax' in refused.output


@pytest.mark.parametrize('count,word', [(1, 'needs'), (2, 'need')])
def test_a_big_send_says_how_many_need_you(count, word):
    board = {**BOARD, 'groups': [{**BOARD['groups'][0], 'needs_you': count}]}
    result, _ = faxbot({'/outbound/now': board}, 'status')
    assert f'{count} {word} you' in result.stdout


@pytest.mark.parametrize('state', ['stopped', 'low'])
def test_faxbot_status_says_first_when_the_carrier_account_is_low_or_turned_off(state):
    sentence = ('Telnyx has turned your account off (your balance is -$1.38), so faxes cannot be sent or received '
                'through Telnyx. Faxbot sends by your other routes until you add funds in the Telnyx portal under '
                'Billing.')
    board = {**BOARD, 'carrier_balance': {'state': state, 'sentence': sentence,
                                          'available': {'amount': '-1.38', 'currency': 'USD'}}}
    result, _ = faxbot({'/outbound/now': board}, 'status')
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[1] == sentence
    assert lines[0].startswith('As of ') and lines[2].startswith('Sending now:')
    unchanged, _ = faxbot({'/outbound/now': {**BOARD, 'carrier_balance': None}}, 'status')
    assert sentence not in unchanged.stdout
