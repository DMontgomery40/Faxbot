"""Missing-fact advice, line advice and a number's move over the real API, its access policy and the command line.

The server is the real application (``tests.test_cli.server``) on a fresh installation; nothing is stood in for the
advice code. Advice only: nothing here ports, enrolls, approves or changes a provider account.
"""
import pytest

from tests.test_cli import BOOTSTRAP, Cli, server  # noqa: F401 (fixture)


@pytest.fixture
def cli(server):  # noqa: F811
    return Cli(server)


def test_costs_advice_reads_the_facts_and_says_it_is_not_savings(cli):
    shown = cli('costs', 'advice')
    assert shown.exit_code == 0, shown.stderr
    assert 'You sent no faxes by a phone call in the last 90 days' in shown.stdout
    assert 'not savings' in shown.stdout and 'Faxbot only advises' in shown.stdout
    result = cli.json('costs', 'advice', '--days', '30')
    assert result['days'] == 30 and result['state'] == 'nothing_sent'
    assert [item['fact'] for item in result['catalogue']][:2] == ['partner', 'digital_address']


def test_the_advice_is_a_settings_read(server):  # noqa: F811
    assert server.get('/routing/recommendations/facts').status_code in (401, 403)
    answer = server.get('/routing/recommendations/facts', headers={'X-API-Key': BOOTSTRAP})
    assert answer.status_code == 200, answer.text
    assert server.get('/routing/recommendations/facts', params={'days': 3},
                      headers={'X-API-Key': BOOTSTRAP}).status_code == 422
