"""The portfolio's real authenticated POST and CLI; scenarios have no product side effects."""
import json

import sqlalchemy as sa

from tests.test_cli import BOOTSTRAP, Cli, restricted_key, server  # noqa: F401
from tests.test_portfolio import scenario

PATH = '/routing/portfolio/plan'
ADMIN = {'X-API-Key': BOOTSTRAP}


def test_portfolio_is_a_stateless_settings_read_post(server):
    raw = scenario()
    engine = server.app.state.configuration_runtime.manager.store.engine
    names = ('direct_peers', 'fax_jobs', 'outbound_deliveries', 'configuration_revisions', 'configuration_state')
    tables = {name: sa.Table(name, sa.MetaData(), autoload_with=engine) for name in names}

    def snapshot():
        with engine.connect() as connection:
            return {name: [dict(row) for row in connection.execute(sa.select(table)).mappings()]
                    for name, table in tables.items()}
    before = snapshot()
    assert server.post(PATH, json=raw).status_code in (401, 403)
    cli = Cli(server)
    cli.json('access', 'roles', 'add', 'Portfolio readers', '-p', 'settings:read')
    _, token = restricted_key(cli, name='Synthetic portfolio reader', role='Portfolio readers',
                              permissions=('settings:read',))
    reader = {'X-API-Key': token}
    answer = server.post(PATH, json=raw, headers=reader)
    assert answer.status_code == 200, answer.text
    assert answer.json()['plans']['expected']['new_ids'] == ['a', 'b', 'c', 'r']
    denied = server.post('/admin/api-keys', headers=ADMIN,
                         json={'name': 'Synthetic fax reader', 'scopes': ['fax:read']})
    assert denied.status_code == 200, denied.text
    assert server.post(PATH, json=raw, headers={'X-API-Key': denied.json()['token']}).status_code == 403
    assert server.get(PATH, headers=reader).status_code == 405
    assert snapshot() == before
    raw['extra_currency'] = 'EUR'
    assert server.post(PATH, json=raw, headers=reader).status_code == 422


def test_cli_posts_the_same_model_and_preserves_json_and_plain_results(server, tmp_path):
    cli = Cli(server); raw = scenario()
    source = tmp_path / 'scenario.json'; source.write_text(json.dumps(raw))
    direct = server.post(PATH, headers=ADMIN, json=raw)
    assert direct.status_code == 200, direct.text
    assert cli.json('costs', 'portfolio', '--file', source) == direct.json()
    plain = cli('costs', 'portfolio', '--file', source)
    assert plain.exit_code == 0, plain.stderr
    assert 'Expected plan' in plain.stdout and 'Cautious plan' in plain.stdout
    assert '6.00 USD' in plain.stdout and 'Inputs are not saved' in plain.stdout
    raw['budget'] = None
    missing = cli.json('costs', 'portfolio', '--file', '-', input=json.dumps(raw))
    assert missing['state'] == 'incomplete' and missing['plans'] is None
    plain = cli('costs', 'portfolio', '--file', '-', input=json.dumps(raw))
    assert plain.exit_code == 0 and 'spending limit' in plain.stdout


def test_cli_refuses_malformed_duplicate_or_unbounded_input(server, tmp_path):
    cli = Cli(server)
    for text in ('{', '{"budget":"1","budget":"2"}', '{"budget":NaN}', ' ' * 65537):
        source = tmp_path / 'invalid.json'; source.write_text(text)
        answer = cli('costs', 'portfolio', '--file', source)
        assert answer.exit_code == 9, (answer.exit_code, answer.stdout, answer.stderr)
    missing = cli('costs', 'portfolio', '--file', tmp_path / 'absent.json')
    assert missing.exit_code != 0 and 'Cannot read' in missing.stderr


def test_browser_calculation_uses_the_normal_session_csrf_boundary(server):
    server.headers['Origin'] = 'https://testserver'
    login = server.post('/auth/key-login', json={'api_key': BOOTSTRAP})
    assert login.status_code == 200, login.text
    assert server.post(PATH, json=scenario()).status_code == 403
    csrf = server.get('/auth/me').json()['csrf_token']
    answer = server.post(PATH, json=scenario(), headers={'X-CSRF-Token': csrf})
    assert answer.status_code == 200 and answer.json()['saved'] is False
