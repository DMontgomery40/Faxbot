"""Number advice on the command line against the real API: your NPI record, the check before a first fax (which
never stops `faxbot send`), where each number should live, and US prices by where a call starts.

The NPPES registry is never called: its network read is replaced by fixtures shaped like the registry API.
"""
import json
from pathlib import Path

import pytest

from app.routing import nppes
from tests.test_cli import BOOTSTRAP, Cli, server  # noqa: F401 (fixture)


FIXTURES = Path(__file__).parent / 'fixtures' / 'nppes'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cli(server):  # noqa: F811
    return Cli(server)


@pytest.fixture
def registry(monkeypatch):
    asked = []
    answers = {'own': fixture('own_record.json'), 'search': fixture('search_organization.json')}

    def fetch(params, *, timeout=10.0):
        asked.append(dict(params))
        return answers['own'] if 'number' in params else answers['search']
    monkeypatch.setattr(nppes, '_fetch', fetch)
    return asked


def test_npi_commands_add_read_and_remove_your_record(cli, registry):
    added = cli('numbers', 'npi', 'add', '1234567893', '--label', 'Denver office')
    assert added.exit_code == 0, added.stderr
    assert 'NPI 1234567893 (Denver office): OUR SYNTHETIC PRACTICE' in added.stdout
    assert '+1 720-555-0199' in added.stdout
    assert cli.json('numbers', 'npi', 'list')['npis'][0]['npi'] == '1234567893'
    assert cli('numbers', 'npi', 'check').exit_code == 0 and len(registry) == 2
    refused = cli('numbers', 'npi', 'add', '1234567890')
    assert refused.exit_code != 0 and 'ten-digit NPI' in refused.stderr
    assert cli.json('numbers', 'npi', 'remove', '1234567893')['npis'] == []


def test_send_warns_before_a_first_fax_to_another_providers_number_and_still_sends(cli, registry, tmp_path):
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic\n')
    checked = cli('recipients', 'check', '+13035550199', '--name', 'Synthetic Health Clinic')
    assert checked.exit_code == 0
    assert "NPPES lists +1 303-555-0111 as SYNTHETIC HEALTH CLINIC's fax number, not this one." in checked.stdout
    # The search stored both organisations, so the imaging centre's number is now known without a question.
    sent = cli('send', '+13035550121', note, '--queue', '--recipient', 'Synthetic Health Clinic')
    assert sent.exit_code == 0, sent.stderr
    assert 'This number is listed for SYNTHETIC HEALTH IMAGING LLC in NPPES, not Synthetic Health Clinic.' in sent.stdout
    assert 'Fax accepted.' in sent.stdout and len(registry) == 1
    again = cli.json('send', '+13035550121', note, '--queue', '--recipient', 'Synthetic Health Clinic')
    assert 'recipient_warning' not in again   # faxed before: no check


def test_costs_commands_show_placement_sites_and_import_state_prices(cli, tmp_path):
    placed = cli('costs', 'recommendations', 'numbers')
    assert placed.exit_code == 0 and 'Faxbot only advises' in placed.stdout
    sites = cli('costs', 'recommendations', 'sites')
    assert sites.exit_code == 0 and 'Faxbot never changes caller ID to lower call charges' in sites.stdout
    deck = tmp_path / 'deck.csv'
    deck.write_text('destination,prefix,rate_inter,rate_intra,billing\nUSA,1303555,0.002,0.01,1-1\n')
    imported = cli('costs', 'state-prices', 'anveo', deck, '--read-on', '2026-10-08')
    assert imported.exit_code == 0, imported.stderr
    assert 'AnveoDirect: 1 number prefixes, 1 priced differently within one state.' in imported.stdout
    every = cli.json('costs', 'recommendations')
    assert {'numbers', 'sites'} <= set(every)



def test_sent_show_says_what_nppes_listed_when_the_fax_was_accepted(cli, registry, tmp_path):
    note = tmp_path / 'note.txt'
    note.write_text('Synthetic\n')
    assert cli('recipients', 'check', '+13035550199', '--name', 'Synthetic Health Clinic').exit_code == 0
    named = cli.client.patch('/routing/destinations/+13035550121', headers={'X-API-Key': BOOTSTRAP},
                             json={'display_name': 'Synthetic Health Clinic'})
    assert named.status_code == 200, named.text
    sent = cli.json('send', '+13035550121', note, '--queue')
    shown = ' '.join(cli('sent', 'show', sent['id']).stdout.split())
    assert ('NPPES This number is listed for SYNTHETIC HEALTH IMAGING LLC in NPPES, not Synthetic Health Clinic.'
            in shown)
