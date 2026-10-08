"""faxbot expected against the real application, in process over HTTPS."""
from datetime import datetime, timedelta
import json
import uuid
import zipfile

import pytest

import app.main as main_module
from app.access.receiving_rules import ReceivedFacts
from app.work.expectations import ExpectationStore
from app.work.store import WorkStore
from api.tests.test_cli import Cli, _serve
from api.tests.test_cli_work import KEY, policy


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr('app.work.worker.WorkWorker.step', lambda self, now=None: False)
    monkeypatch.setattr('app.work.expectations.ExpectationWorker.step', lambda self, now=None: False)
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def engine():
    return main_module.app.state.configuration_runtime.manager.store.engine


def arrive(sub=None, sender='+15559990000'):
    identity = uuid.uuid4().hex
    moment = datetime.utcnow()
    main_module.app.state.access_runtime.inbound.accept(dict(
        id=identity, from_number=sender, to_number='+15550100001', status='received', backend='sip', pages=1,
        size_bytes=10, pdf_path='/synthetic/' + identity + '.pdf', created_at=moment, received_at=moment,
        updated_at=moment), country='US', facts=ReceivedFacts(to_number=None, subaddress=sub, received_at=moment))
    WorkStore(engine()).feed()
    store = ExpectationStore(engine())
    store.examine()
    store.look_back()
    return identity


def front_desk(cli):
    created = cli.client.post('/access/mailboxes', headers=KEY, json={
        'label': 'Front Desk', 'enabled': True, 'expected_policy_version': policy(cli.client)})
    assert created.status_code == 200, created.text
    rule = cli.client.post('/access/inbound-rules', headers=KEY, json={
        'to_number': '+15550100001', 'mailbox_id': created.json()['mailbox']['id'],
        'expected_policy_version': policy(cli.client)})
    assert rule.status_code == 200, rule.text


def test_expect_a_fax_confirm_a_proposal_and_export(cli, tmp_path):
    front_desk(cli)
    added = cli('expected', 'add', 'PO 483', '--expecting', 'Signed acknowledgement', '--mailbox', 'front desk',
                '--from-name', 'Acme Supply', '--fax-number', '+1 555 010 4444', '--subaddress', '483',
                '--must-include', 'Signature page', '--due-hours', '48')
    assert added.exit_code == 0, added.stdout + added.stderr
    assert 'Expecting PO 483 (code ' in added.stdout and 'Waiting; expected by ' in added.stdout
    (item,) = cli.json('expected', 'list')['expected']
    code = item['code']
    assert item['id'] not in cli('expected', 'list').stdout
    refused = cli('expected', 'add', 'PO 483', '--expecting', 'Reply', '--mailbox', 'Nowhere')
    assert refused.exit_code != 0 and 'You cannot add expected faxes to Nowhere.' in refused.stdout + refused.stderr
    arrive(sub='483')
    shown = cli('expected', 'show', code)
    assert 'A received fax may be the one; confirm or reject it.' in shown.stdout
    assert 'check that it includes Signature page.' in shown.stdout
    confirmed = cli('expected', 'confirm', code)
    assert confirmed.exit_code == 0 and 'Matched by ' in confirmed.stdout
    history = cli('expected', 'show', code, '--history')
    assert 'confirmed a received fax answers it.' in history.stdout
    target = tmp_path / 'evidence.zip'
    exported = cli('expected', 'export', code, '-o', target)
    assert exported.exit_code == 0 and target.exists()
    with zipfile.ZipFile(target) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    assert manifest['expected']['reference'] == 'PO 483' and manifest['links'][0]['state'] == 'confirmed'
    report = cli('expected', 'report')
    assert report.exit_code == 0 and '1 of 1 expected faxes arrived and were matched' in report.stdout


def test_import_through_a_saved_source_then_an_outage_and_its_lists(cli, tmp_path):
    front_desk(cli)
    saved = cli('expected', 'sources', 'save', 'Open purchase orders', '--format', 'csv', '--column', 'reference=PO',
                '--column', 'counterparty=Supplier', '--mailbox', 'Front Desk', '--subject-pattern', 'PO {reference}')
    assert saved.exit_code == 0, saved.stdout + saved.stderr
    assert 'reference=PO' in cli('expected', 'sources', 'list').stdout
    export = tmp_path / 'open.csv'
    export.write_text('PO,Supplier\n701,Acme\n702,Beta\n')
    imported = cli('expected', 'import', export, '--source', 'Open purchase orders', '--full')
    assert imported.exit_code == 0, imported.stdout + imported.stderr
    assert '2 rows: 2 new.' in imported.stdout
    again = cli('expected', 'import', export, '--source', 'Open purchase orders', '--full')
    assert 'This file was imported before; nothing was added twice.' in again.stdout
    started = cli('expected', 'outage', 'start', 'Open purchase orders', '--note', 'ERP maintenance')
    assert started.exit_code == 0, started.stdout + started.stderr
    (outage,) = cli.json('expected', 'outage', 'list')['outages']
    recorded = cli('expected', 'outage', 'record', outage['code'], '--id', '701', '--did', 'Order faxed to Acme',
                   '--by', 'fax')
    assert recorded.exit_code == 0 and 'Recorded for 701.' in recorded.stdout
    assert cli('expected', 'outage', 'end', outage['code']).exit_code == 0
    export.write_text('PO,Supplier\n701,Acme\n702,Beta\n703,Gamma\n')
    sorted_lists = cli('expected', 'import', export, '--source', 'Open purchase orders', '--full')
    assert f"Sorted against outage {outage['code']}" in sorted_lists.stdout
    shown = cli('expected', 'outage', 'show', outage['code'])
    assert '1 already done (record them; do not submit them again), 2 new (submit them normally), 0 held' in shown.stdout
    again = cli('expected', 'outage', 'reconcile', outage['code'])
    assert again.exit_code == 0, again.stdout + again.stderr
    missing = cli('expected', 'list', '--show', 'missing')
    assert missing.exit_code == 0 and 'No expected faxes.' in missing.stdout
    timed = cli('expected', 'add', 'PO 9', '--expecting', 'Reply', '--mailbox', 'Front Desk', '--due', 'tomorrow')
    assert timed.exit_code != 0 and '--due must be a time like' in timed.stdout + timed.stderr
    later = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M')
    assert cli('expected', 'add', 'PO 9', '--expecting', 'Reply', '--mailbox', 'Front Desk', '--due', later
               ).exit_code == 0
