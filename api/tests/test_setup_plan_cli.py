"""faxbot system setup plan|show|apply, through the real faxbot command against the real application."""
import pytest

from api.tests.test_cli import BOOTSTRAP, Cli, _serve
from api.tests.test_setup_plan_http import CLINIC, _sent


@pytest.fixture
def cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path, FAX_OUTBOUND_ROUTES='sip, signalwire'):
        yield Cli(client)


def _failing_first_route(cli):
    for _ in range(4):
        _sent(CLINIC, 'phaxio', 'failed')
    for _ in range(3):
        _sent(CLINIC, 'signalwire', 'success')
    chosen = cli.client.patch(f'/routing/destinations/{CLINIC}', headers={'X-API-Key': BOOTSTRAP},
                              json={'preferred_route': 'phaxio'})
    assert chosen.status_code == 200, chosen.text


def test_plan_show_and_apply_from_the_command_line(cli):
    _failing_first_route(cli)
    previewed = cli('system', 'setup', 'plan', '--name', 'Synthetic Clinic', '--country', 'us')
    assert previewed.exit_code == 0, (previewed.stdout, previewed.stderr)
    text = previewed.stdout
    assert 'Setup plan 1, made ' in text and 'Faxes to +12025550123 try SignalWire first' in text
    assert 'Print your business name at the top of each page' in text
    assert "What's missing" in text and 'Your choice' in text and 'Not in Faxbot yet' in text
    assert 'Apply the chosen suggestions with: faxbot system setup apply 1' in text
    shown = cli.json('system', 'setup', 'show')
    assert shown['number'] == 1 and shown['context'] == {'organization_name': 'Synthetic Clinic', 'country': 'US',
                                                         'mailboxes': {}}
    keys = [item['key'] for pack in shown['packs'] for item in pack['items']]
    header = keys.index('compliance.header') + 1
    first = cli('system', 'setup', 'apply', '1', '--only', str(header))
    assert first.exit_code == 0, (first.stdout, first.stderr)
    assert 'Applied 1 suggestion.' in first.stdout and 'Settings: Settings saved and in use.' in first.stdout
    rest = cli.json('system', 'setup', 'apply', '1')
    assert rest['outcome'] == 'applied' and rest['items'] == [f'reliability.fallback.{CLINIC}']
    assert rest['steps'][0]['sentence'].startswith('Rules published as version 1')
    assert len(cli.json('system', 'setup', 'show', '1')['applications']) == 2


def test_the_command_line_says_what_to_fix(cli):
    empty = cli('system', 'setup', 'show')
    assert empty.exit_code != 0 and 'faxbot system setup plan' in (empty.stdout + empty.stderr)
    assert cli('system', 'setup', 'plan').exit_code == 0
    wrong = cli('system', 'setup', 'apply', '1', '--only', '99')
    assert wrong.exit_code != 0 and 'Plan 1 has suggestions 1 to' in (wrong.stdout + wrong.stderr)
    words = cli('system', 'setup', 'apply', '1', '--only', 'first')
    assert words.exit_code != 0 and "such as '1,4'" in (words.stdout + words.stderr)
    shape = cli('system', 'setup', 'plan', '--mailbox-country', 'Leeds')
    assert shape.exit_code != 0 and 'NAME=COUNTRY' in (shape.stdout + shape.stderr)


def test_mailbox_countries_are_given_by_mailbox_name(cli):
    version = cli.client.get('/auth/me', headers={'X-API-Key': BOOTSTRAP}).json()['policy_version']
    created = cli.client.post('/access/mailboxes', headers={'X-API-Key': BOOTSTRAP},
                              json={'label': 'Leeds', 'enabled': True, 'expected_policy_version': version})
    assert created.status_code == 200, created.text
    plan = cli.json('system', 'setup', 'plan', '--mailbox-country', 'Leeds=gb')
    leeds = plan['mailboxes'][0]
    assert leeds['name'] == 'Leeds' and leeds['country'] == 'GB' and leeds['missing'] == ['reviewed-rules.GB']


def test_lists_saved_plans_and_opens_an_earlier_one(cli):
    assert cli.json('system', 'setup', 'list') == {'plans': []}
    first = cli.json('system', 'setup', 'plan', '--name', 'First synthetic organization')
    second = cli.json('system', 'setup', 'plan', '--name', 'Second synthetic organization')
    saved = cli.json('system', 'setup', 'list')['plans']
    assert [row['number'] for row in saved] == [second['number'], first['number']]
    shown = cli.json('system', 'setup', 'show', str(first['number']))
    assert shown['context']['organization_name'] == 'First synthetic organization'
    assert shown['applications'] == []
    human = cli('system', 'setup', 'list')
    assert human.exit_code == 0 and 'faxbot system setup show NUMBER' in human.stdout
