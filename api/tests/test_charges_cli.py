"""faxbot costs charges and faxbot costs invoices through the real command line and API (B2, M27).

The Phaxio listing is answered by an ``httpx.MockTransport`` shaped like Phaxio's documented "List faxes" answer
(API v2.1, read 2026-10-08); any request but a read fails the test. Keys, numbers and invoices are synthetic.
"""
from datetime import datetime, timedelta
import json

import httpx
import pytest

from api.tests.test_cli import Cli, _serve


PDF = b'%PDF-1.4\n% synthetic invoice\n%%EOF\n'


@pytest.fixture
def cli(monkeypatch, tmp_path):
    for client in _serve(monkeypatch, tmp_path, PHAXIO_API_KEY='synthetic-phaxio-key',
                         PHAXIO_API_SECRET='synthetic-phaxio-secret'):
        yield Cli(client)


def test_an_invoice_is_entered_with_its_file_listed_and_its_file_saved(cli, tmp_path):
    invoice = tmp_path / 'september.pdf'
    invoice.write_bytes(PDF)
    added = cli('costs', 'invoices', 'add', '--account', 'phaxio', '--month', '2026-09', '--total', '13.20',
                '--note', 'INV-0042', '--file', invoice)
    assert added.exit_code == 0, (added.stdout, added.stderr)
    assert 'Phaxio, September 2026: invoice $13.20.' in added.stdout
    assert "$13.20 of your Phaxio invoice for September 2026 isn't explained by your faxes." in added.stdout
    listed = cli.json('costs', 'invoices', 'list')
    [entry] = listed['invoices']
    assert (entry['file']['name'], entry['residual']['amount'], entry['note']) == ('september.pdf', '13.20', 'INV-0042')
    human = cli('costs', 'invoices', 'list')
    assert human.exit_code == 0 and entry['id'] in human.stdout and 'September 2026' in human.stdout
    saved = tmp_path / 'copy.pdf'
    shown = cli('costs', 'invoices', 'show', entry['id'], '--save-file', saved)
    assert shown.exit_code == 0, (shown.stdout, shown.stderr)
    assert saved.read_bytes() == PDF and f'saved to {saved}' in shown.stdout
    again = cli('costs', 'invoices', 'show', entry['id'], '--save-file', saved)
    assert again.exit_code != 0 and 'already exists' in again.stdout + again.stderr
    refused = cli('costs', 'invoices', 'add', '--account', 'phaxio', '--total', '13.20')
    assert refused.exit_code != 0 and '--month' in refused.stdout + refused.stderr


def test_charges_show_each_account_and_a_sweep_lists_only(cli, monkeypatch):
    from app.routing import provider_sweep
    seen = []
    # A day ago, so it is inside the three days listed whenever the test runs.
    sent_at = (datetime.utcnow() - timedelta(days=1)).replace(microsecond=0)

    def answer(request):
        assert request.method == 'GET'
        seen.append(request.url)
        return httpx.Response(200, json={'success': True, 'message': 'Retrieved faxes', 'paging': {
            'total': 1, 'per_page': 100, 'page': 1}, 'data': [{
                'id': 31337, 'direction': 'sent', 'status': 'success', 'num_pages': 1, 'cost': 7, 'is_test': False,
                'created_at': sent_at.isoformat() + '.000Z', 'recipients': [{'phone_number': '+12025550177'}]}]})
    monkeypatch.setattr(provider_sweep, '_TRANSPORT', httpx.MockTransport(answer))
    shown = cli('costs', 'charges')
    assert shown.exit_code == 0, (shown.stdout, shown.stderr)
    assert 'Phaxio reports what each sent and received fax cost' in ' '.join(shown.stdout.split())
    assert 'Your providers listed no fax that Faxbot has no record of.' in shown.stdout
    swept = cli('costs', 'charges', 'sweep', '--account', 'phaxio', '--days', '3')
    assert swept.exit_code == 0, (swept.stdout, swept.stderr)
    assert 'Phaxio listed 1 fax, and Faxbot has no record of it.' in swept.stdout
    assert [url.host for url in seen] == ['api.phaxio.com']
    listed = json.loads(cli('--json', 'costs', 'charges').stdout)
    [fax] = listed['unrecorded']
    assert (fax['to_number'], fax['cost']) == ('+12025550177', {'currency': 'USD', 'amount': '0.07'})
    assert fax['summary'] == (f"Phaxio billed a fax to +12025550177 on {sent_at.day} {sent_at:%b} ($0.07) that Faxbot "
                              "didn't send.")


def test_costs_spending_has_a_received_faxes_line_per_provider(cli):
    import os
    import sqlalchemy as sa
    from api.tests.test_invoices import received
    engine = sa.create_engine(os.environ['DATABASE_URL'])
    try:
        when = datetime.utcnow() - timedelta(days=1)
        received(engine, 'phaxio', when, account_key='phaxio', charge=70_000)  # Phaxio reported $0.07
        received(engine, 'phaxio', when, account_key='phaxio', pages=2)  # Phaxio's shipped price: 2 pages estimated
        received(engine, 'documo', when, account_key='documo')  # no price for Documo here: unknown
    finally:
        engine.dispose()
    shown = cli('costs', 'spending')
    assert shown.exit_code == 0, (shown.stdout, shown.stderr)
    text = ' '.join(shown.stdout.split())
    phaxio = [line for line in shown.stdout.splitlines() if line.startswith('Received faxes: Phaxio')]
    assert len(phaxio) == 1 and phaxio[0].endswith(' for 2 faxes.')
    assert 'Received faxes: Documo 1 fax, not priced yet.' in text
    assert '1 fax is not priced yet, so it is not in the total.' in text
