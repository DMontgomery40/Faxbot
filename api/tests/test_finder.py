"""The page and settings finder (#53): one catalogue for the console search and `faxbot find`.

The catalogue in app/finder.py mirrors the console's navigation table (admin_ui/src/navigation.tsx)
and gives every ConfigurationValues field its one console home (or says where it is changed instead).
These tests hold the mirror to the navigation source, prove that forbidden pages and fields never
appear, and keep the shared fixture (admin_ui/src/__tests__/finder.json) that the console's finder
test renders and the command prints from. Rewrite the fixture by running this file with
FAXBOT_WRITE_FIXTURES=1. All names are synthetic.
"""
import json
import os
import re
from pathlib import Path

import httpx
from typer.testing import CliRunner

from app import finder
from app.cli.commands.find import find_lines
from app.cli.main import app as cli_app
from app.config_values import ConfigurationValues
from api.tests.test_cli import BOOTSTRAP, Cli, _serve  # noqa: F401  (Cli/_serve for the route test)
from api.tests.test_settings_coverage import READ_ONLY_OR_ENV
import pytest

API_ROOT = Path(__file__).resolve().parents[1]
NAVIGATION = API_ROOT / 'admin_ui' / 'src' / 'navigation.tsx'
FIXTURE = API_ROOT / 'admin_ui' / 'src' / '__tests__' / 'finder.json'
WRITE = os.environ.get('FAXBOT_WRITE_FIXTURES') == '1'

ALL_NAVIGATION = {'jobs': True, 'inbox': True, 'send': True, 'work': True}
NO_NAVIGATION = {'jobs': False, 'inbox': False, 'send': False, 'work': False}
EVERY_PERMISSION = frozenset({
    'diagnostics:read', 'settings:read', 'settings:write', 'work:read', 'work:import', 'fax:send',
    'mailboxes:read', 'mailboxes:manage', 'users:read', 'users:manage', 'groups:read', 'groups:manage',
    'roles:read', 'roles:manage', 'grants:read', 'grants:manage', 'keys:manage', 'audit:read',
    'logs:read', 'host:terminal', 'providers:read', 'providers:write',
})


def search(words, permissions=EVERY_PERMISSION, navigation=ALL_NAVIGATION):
    return finder.search(words, permissions=permissions, navigation=navigation, plugins_enabled=True)


# -- the catalogue mirrors the console -------------------------------------------------

def _console_pages():
    """(area, page, label) from navigation.tsx, in menu order, provider pages included."""
    text = NAVIGATION.read_text(encoding='utf-8')
    body = text[text.index('export const NAVIGATION'):]
    pages, area = [], None
    for match in re.finditer(
            r"id: '(overview|savings|faxes|delivery|recipients|admin)', label: '([^']+)'"
            r"|providerPage\('([a-z-]+)', '([^']+)'"
            r"|\{ id: '([a-z-]+)', label: '([^']+)'", body):
        if match.group(1):
            area = match.group(1)
        elif match.group(3):
            pages.append(('delivery', match.group(3), match.group(4)))
        else:
            pages.append((area, match.group(5), match.group(6)))
    return pages


def test_every_navigation_page_is_in_the_catalogue_once():
    console = _console_pages()
    assert len(console) >= 52, console
    catalogue = [(page.area, page.page, page.label) for page in finder.PAGES]
    assert catalogue == console


def test_every_page_is_findable_by_its_label():
    for page in finder.PAGES:
        found = search(page.label)
        assert any(hit['address'] == page.address for hit in found), page.label


def test_every_setting_has_one_home_and_every_home_is_a_real_page():
    addresses = {page.address for page in finder.PAGES}
    entries = finder.setting_entries()
    assert {entry.name for entry in entries} == set(ConfigurationValues.model_fields)
    for entry in entries:
        assert entry.address is None or entry.address in addresses, entry.name
        if entry.address is None:
            # No console home: the entry still says the one place it is changed.
            assert entry.place and entry.command, entry.name
        assert entry.scope.startswith('Installation'), entry.name


def test_fields_without_a_console_home_are_exactly_the_listed_environment_ones():
    no_home = {entry.name for entry in finder.setting_entries() if entry.address is None}
    assert no_home == set(READ_ONLY_OR_ENV) | {'fs_caller_id_number', 'fs_gateway_name'}


def test_every_setting_is_findable_by_its_technical_name():
    for name in ConfigurationValues.model_fields:
        found = search(name.replace('_', ' '))
        assert any(hit.get('name') == name for hit in found), name


# -- searching --------------------------------------------------------------------------

def test_technical_and_task_synonyms_reach_the_canonical_editor():
    t38 = search('T.38')
    assert t38 and t38[0]['address'] == '#/delivery/trunk', t38
    retention = search('retention')
    assert any(hit.get('name') == 'inbound_retention_days' and hit['address'] == '#/admin/retention'
               for hit in retention), retention
    caller = search('caller ID')
    assert any(hit.get('name') == 'sip_trunk_caller_id' for hit in caller), caller
    senders = search('who can send')
    assert any(hit['address'] in ('#/delivery/mailboxes', '#/admin/who') for hit in senders), senders


def test_forbidden_pages_and_fields_never_appear():
    # A fax operator: may send and read faxes, may not read settings, users or the terminal.
    found = search('settings', permissions=frozenset({'fax:send'}), navigation={**NO_NAVIGATION, 'send': True, 'jobs': True})
    addresses = {hit['address'] for hit in found if hit['address']}
    assert '#/admin/terminal' not in addresses and '#/admin/users' not in addresses
    assert all(hit.get('name') is None for hit in found), 'a settings field leaked to someone without settings:read'
    trunk = search('T.38', permissions=frozenset(), navigation=NO_NAVIGATION)
    assert trunk == [], trunk
    everything = search('')
    limited = search('', permissions=frozenset(), navigation=NO_NAVIGATION)
    # Only the ungated Sessions page is left for someone with no permissions at all.
    assert [hit['address'] for hit in limited] == ['#/admin/sessions'], limited
    assert len(everything) > len(limited)


def test_no_result_ever_carries_a_setting_value_or_a_secret():
    blob = json.dumps(search('password') + search('api key') + search(''))
    assert 'value' not in blob and '***' not in blob
    for hit in search('password'):
        assert set(hit) <= {'kind', 'label', 'name', 'address', 'place', 'scope', 'command'}


# -- the shared fixture -------------------------------------------------------------------

QUERIES = ('T.38', 'retention', 'caller ID', 'who can send')


def _fixture():
    return {
        'about': ("Finder answers in words. The console's search (components/shell/Finder.tsx) and "
                  "`faxbot find` (cli/commands/find.py) show the same results from GET /auth/finder; "
                  "each query holds the response for someone who may see every page and the lines the "
                  "command prints from it. api/tests/test_finder.py regenerates this file with "
                  "FAXBOT_WRITE_FIXTURES=1."),
        'queries': [
            {'q': words, 'response': {'q': words, 'results': search(words)},
             'cli': find_lines(words, search(words))}
            for words in QUERIES
        ],
    }


def test_the_fixture_is_the_catalogues_own_answers():
    expected = _fixture()
    if WRITE:
        FIXTURE.write_text(json.dumps(expected, indent=2) + '\n', encoding='utf-8')
    assert json.loads(FIXTURE.read_text(encoding='utf-8')) == expected, (
        f'rewrite it: FAXBOT_WRITE_FIXTURES=1 with tests/test_finder.py ({FIXTURE.name})')


# -- the command --------------------------------------------------------------------------

def faxbot(responses, *args):
    asked = []

    def handler(request):
        asked.append((request.url.path, request.url.params.get('q')))
        reply = responses.get(request.url.path)
        return httpx.Response(404, json={'detail': 'Not Found'}) if reply is None else httpx.Response(200, json=reply)
    client = httpx.Client(base_url='https://testserver', transport=httpx.MockTransport(handler))
    result = CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', 'synthetic-key', *args],
                                obj={'client_factory': lambda address, timeout: (client, False)},
                                env={'COLUMNS': '220'})
    return result, asked


@pytest.mark.parametrize('case', _fixture()['queries'], ids=[case['q'] for case in _fixture()['queries']])
def test_faxbot_find_prints_the_fixtures_lines(case):
    result, asked = faxbot({'/auth/finder': case['response']}, 'find', *case['q'].split())
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == case['cli']
    assert asked == [('/auth/finder', case['q'])]


def test_faxbot_find_json_is_the_response():
    case = _fixture()['queries'][0]
    result, _ = faxbot({'/auth/finder': case['response']}, '--json', 'find', case['q'])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == case['response']


# -- the route ----------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def test_the_route_answers_the_owner_and_refuses_no_key(cli):
    client = cli.client
    found = client.get('/auth/finder', params={'q': 'T.38'}, headers={'X-API-Key': BOOTSTRAP})
    assert found.status_code == 200, found.text
    assert found.json()['results'][0]['address'] == '#/delivery/trunk'
    assert client.get('/auth/finder', params={'q': 'T.38'}).status_code == 401
    result = cli('find', 'retention')
    assert result.exit_code == 0, result.stdout + result.stderr
    assert 'inbound_retention_days' in result.stdout
