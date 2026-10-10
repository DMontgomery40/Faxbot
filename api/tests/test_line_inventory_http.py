"""The line inventory and carrier lists through the real server and the real command line (N19).

Synthetic files only: the workbook copies the structure of AT&T's published workbook with invented wire centers,
and the numbers are 555-01xx numbers. Not yet run against a real customer's inventory.
"""
import pytest
from fastapi.testclient import TestClient

from app import main
from api.tests.test_line_inventory import FAX_A, INVENTORY, att_workbook

BOOTSTRAP = 'synthetic-line-inventory-bootstrap'
ADMIN = {'X-API-Key': BOOTSTRAP}


@pytest.fixture
def client(isolated_installation, monkeypatch):
    for name, value in {'REQUIRE_API_KEY': 'true', 'API_KEY': BOOTSTRAP, 'PUBLIC_API_URL': 'https://testserver',
                        'FAX_BACKEND': 'phaxio', 'FAX_DEFAULT_COUNTRY': 'US', 'MAX_REQUESTS_PER_MINUTE': '0'}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('FAXBOT_CONSOLE_ORIGINS', 'https://testserver')
    with TestClient(main.app, base_url='https://testserver', headers={'Origin': 'https://testserver'}) as client:
        yield client


def test_import_an_inventory_and_atts_workbook_then_read_the_matches(client):
    empty = client.get('/routing/line-inventory', headers=ADMIN)
    assert empty.status_code == 200 and empty.json()['lines'] == [] and empty.json()['lists'] == []
    assert empty.json()['sentence'].startswith('No line inventory yet.')
    imported = client.post('/routing/line-inventory/files', headers=ADMIN, data={'date_order': 'mdy'},
                           files={'file': ('lines.csv', INVENTORY.encode(), 'text/csv')})
    assert imported.status_code == 200, imported.text
    assert imported.json()['imported'] == 7 and imported.json()['skipped_count'] == 4
    listed = client.post('/routing/carrier-lists/files', headers=ADMIN, data={'file_date': '2026-08-17'},
                         files={'file': ('PrimeAccess.xlsx', att_workbook(), 'application/octet-stream')})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body['layout'] == 'att_workbook' and body['skipped_count'] == 1
    assert body['lists'][0]['file_date'] == '2026-08-17' and body['lists'][0]['source_url'].startswith(
        'https://clec.att.com/')
    first = body['lines'][0]
    assert first['number'] == FAX_A and first['match']['state'] == 'listed'
    assert first['dates'][0]['source'] == "AT&T's Discontinued TDM Service Areas workbook"
    # The plan reads the same dates.
    plan = client.get('/routing/recommendations/lines', headers=ADMIN).json()
    assert plan['numbers'][0]['number'] == FAX_A and plan['numbers'][0]['verdict'] == 'not_in_faxbot'
    # The contract end shows in the line closures beside carrier letters.
    closures = client.get('/routing/closures', headers=ADMIN).json()
    assert any('Its contract with AT&T Illinois ends on 31 December 2026.' in ' '.join(line['sentences'])
               for line in closures['lines'])


def test_files_faxbot_cannot_read_are_refused_in_one_sentence(client):
    no_number = client.post('/routing/line-inventory/files', headers=ADMIN,
                            files={'file': ('lines.csv', b'Address\n1 Test St\n', 'text/csv')})
    assert no_number.status_code == 400 and 'column named number' in no_number.json()['detail']
    damaged = client.post('/routing/carrier-lists/files', headers=ADMIN,
                          files={'file': ('list.xlsx', b'PK\x03\x04damaged', 'application/octet-stream')})
    assert damaged.status_code == 400 and 'Excel' in damaged.json()['detail']
    wrong = client.post('/routing/carrier-lists/files', headers=ADMIN, data={'kind': 'rumoured'},
                        files={'file': ('list.csv', b'Carrier,Wire Center,Effective Date\nLumen,ZZTSCOAA,2026-11-01\n',
                                        'text/csv')})
    assert wrong.status_code == 400 and 'discontinued or grandfathered' in wrong.json()['detail']
    bad_date = client.post('/routing/carrier-lists/files', headers=ADMIN, data={'file_date': '17/08/2026'},
                           files={'file': ('list.csv', b'x', 'text/csv')})
    assert bad_date.status_code == 400 and '2026-11-04' in bad_date.json()['detail']


def test_reading_and_importing_need_a_signed_in_administrator(client):
    assert client.get('/routing/line-inventory').status_code in (401, 403)
    assert client.post('/routing/line-inventory/files',
                       files={'file': ('lines.csv', INVENTORY.encode(), 'text/csv')}).status_code in (401, 403)
    assert client.post('/routing/carrier-lists/files',
                       files={'file': ('list.xlsx', att_workbook(), 'text/csv')}).status_code in (401, 403)


@pytest.fixture
def inventory_cli(monkeypatch, tmp_path):
    from api.tests.test_cli import Cli, _serve
    for client in _serve(monkeypatch, tmp_path, FAX_DEFAULT_COUNTRY='US'):
        yield Cli(client)


def test_the_command_line_imports_both_files_and_shows_dated_lines_first(inventory_cli, tmp_path):
    lines = tmp_path / 'lines.csv'
    lines.write_text(INVENTORY, encoding='utf-8')
    workbook = tmp_path / 'PrimeAccess_Model-Discontinued_Service_Areas.xlsx'
    workbook.write_bytes(att_workbook())
    imported = inventory_cli('numbers', 'move', 'import-inventory', lines)
    assert imported.exit_code == 0, imported.stdout + imported.stderr
    assert 'Imported 7 lines.' in imported.stdout and '4 rows were not read:' in imported.stdout
    assert "No carrier list yet. Import AT&T's workbook" in imported.stdout
    listed = inventory_cli('numbers', 'move', 'import-carrier-list', workbook, '--file-date', '2026-08-17')
    assert listed.exit_code == 0, listed.stdout + listed.stderr
    assert 'Imported 5 areas.' in listed.stdout and '1 row was not read:' in listed.stdout
    assert "AT&T's Discontinued TDM Service Areas workbook: 5 areas in 4 wire centers" in listed.stdout
    shown = inventory_cli.json('numbers', 'move', 'inventory')
    assert shown['lines'][0]['number'] == FAX_A
    assert inventory_cli('numbers', 'move', 'import-inventory', lines, '--date-order', 'ymd').exit_code != 0
    assert inventory_cli('numbers', 'move', 'import-carrier-list', workbook, '--kind', 'maybe').exit_code != 0
