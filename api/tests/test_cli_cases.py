"""`faxbot faxes cases`: acknowledgements, reuse periods, repairs, kept originals and checklist packets."""
import json

import sqlalchemy as sa

from app import main as main_module
from api.tests.test_cli import BOOTSTRAP, cli, pdf, server  # noqa: F401 - fixtures


TO = '+15551230009'


def finish(job_id):
    engine = main_module.app.state.configuration_runtime.manager.store.engine
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE outbound_deliveries SET state='success' WHERE id=:id"), {'id': job_id})


def test_accept_invalidate_reuse_and_repair(cli, tmp_path):
    record, letter = pdf(tmp_path / 'record.pdf', 'Record'), pdf(tmp_path / 'letter.pdf', 'Letter')
    sent = cli.json('recipients', 'cases', 'send', 'CASE-1', TO, record, letter, '--title', 'Record', '--title',
                    'Cover letter', '--purpose', 'Appeal', '--source', 'Valley EHR', '--version', 'final')
    assert sent['purpose'] == 'Appeal'
    early = cli('recipients', 'cases', 'accept', 'CASE-1', '--to', TO, '--note', 'They have it.')
    assert early.exit_code != 0 and 'Name the documents' in early.stdout + early.stderr
    finish(sent['fax_id'])
    human = cli('recipients', 'cases', 'documents', 'CASE-1', '--to', TO)
    assert human.exit_code == 0 and 'Delivered, not acknowledged' in human.stdout
    assert 'version final, from Valley EHR' in human.stdout and 'Appeal' in human.stdout
    accepted = cli.json('recipients', 'cases', 'accept', 'CASE-1', '--to', TO, '--note',
                        'Their intake desk confirmed by phone.')
    assert {item['state'] for item in accepted['documents']} == {'accepted'}
    assert 'Acknowledged (confirmed by a person' in cli('recipients', 'cases', 'documents', 'CASE-1', '--to', TO).stdout
    missed = cli.json('recipients', 'cases', 'invalidate', 'CASE-1', '--to', TO, '--document', 'cover letter',
                      '--note', 'Not in their system.')
    assert [(item['title'], item['state']) for item in missed['documents']] == [
        ('Record', 'accepted'), ('Cover letter', 'invalidated')]
    unknown = cli('recipients', 'cases', 'invalidate', 'CASE-1', '--to', TO, '--document', 'Nothing')
    assert unknown.exit_code != 0 and "No document 'Nothing'" in unknown.stdout + unknown.stderr

    assert cli.json('recipients', 'cases', 'reuse', TO)['reuse_days'] == 90
    assert cli.json('recipients', 'cases', 'reuse', TO, '--days', '30')['reuse_days'] == 30
    limitless = cli('recipients', 'cases', 'reuse', TO, '--no-limit')
    assert limitless.exit_code == 0 and 'no time limit' in limitless.stdout
    assert "(Faxbot's default)" in cli('recipients', 'cases', 'reuse', TO, '--default').stdout

    refused = cli('recipients', 'cases', 'repair', 'CASE-1', '--to', TO)
    assert refused.exit_code != 0 and '--reason' in refused.stdout + refused.stderr
    preview = cli('recipients', 'cases', 'repair', 'CASE-1', '--to', TO, '--preview')
    assert preview.exit_code == 0 and 'Preview only; nothing was sent.' in preview.stdout
    repaired = cli.json('recipients', 'cases', 'repair', 'CASE-1', '--to', TO, '--reason', 'They lost the file.')
    assert repaired['fax_id'] and repaired['fax_id'] != sent['fax_id'] and repaired['reason'] == 'They lost the file.'


def test_originals_checklists_and_a_built_packet(cli, tmp_path):
    summary, card = pdf(tmp_path / 'summary.pdf', 'Summary'), pdf(tmp_path / 'card.pdf', 'Card')
    added = cli('recipients', 'cases', 'add', 'CASE-2', summary, card, '--title', 'Discharge summary', '--title',
                'Insurance card', '--type', 'Discharge summary', '--type', 'Insurance card', '--date', '2026-10-01',
                '--version', 'final')
    assert added.exit_code == 0 and '2 documents are kept for case CASE-2.' in added.stdout
    originals = cli('recipients', 'cases', 'originals', 'CASE-2').stdout
    assert 'Discharge summary' in originals and '2026-10-01' in originals
    assert 'Kept documents stay until you set how long sent fax files are kept.' in originals
    assert 'No checklists yet' in cli('recipients', 'cases', 'checklist', 'list').stdout
    example = cli('recipients', 'cases', 'checklist', 'add', 'Discharge follow-up', '--example', '--to', TO)
    assert example.exit_code == 0 and 'Discharge follow-up, version 1' in example.stdout
    items = tmp_path / 'items.json'
    items.write_text(json.dumps({'items': [{'type': 'Discharge summary', 'version': 'final', 'within_days': 30},
                                           {'type': 'Insurance card'}]}))
    second = cli.json('recipients', 'cases', 'checklist', 'add', 'Discharge follow-up', items)
    assert second['version'] == 2 and len(second['items']) == 2
    shown = cli('recipients', 'cases', 'checklist', 'show', 'Discharge follow-up', '--version', '1').stdout
    assert 'Medication list' in shown and 'not used yet' in shown
    preview = cli('recipients', 'cases', 'build', 'CASE-2', '--to', TO, '--checklist', 'Discharge follow-up',
                  '--version', '1', '--as-of', '2026-10-07', '--preview')
    assert preview.exit_code == 0, preview.stdout
    assert "Matches 'Discharge summary': version 'final', dated 1 October 2026" in preview.stdout
    assert "Missing: item 2, Medication list. No 'Medication list' is in the case." in preview.stdout
    refused = cli('recipients', 'cases', 'build', 'CASE-2', '--to', TO, '--checklist', 'Discharge follow-up',
                  '--version', '1', '--as-of', '2026-10-07')
    assert refused.exit_code != 0 and "'Medication list'" in refused.stdout + refused.stderr
    built = cli.json('recipients', 'cases', 'build', 'CASE-2', '--to', TO, '--checklist', 'Discharge follow-up',
                     '--as-of', '2026-10-07')
    assert built['fax_id'] and built['checklist']['version'] == 2
    assert [item['title'] for item in built['selected']] == ['Discharge summary', 'Insurance card']
    listed = cli('recipients', 'cases', 'checklist', 'list').stdout
    assert 'Discharge follow-up' in listed


def test_suggestions_switch(cli):
    assert cli.json('recipients', 'cases', 'suggestions', 'on') == {'suggestions': True}
    settings = cli.client.get('/admin/settings', headers={'X-API-Key': BOOTSTRAP}).json()
    assert settings['cases'] == {'suggestions': True}
    off = cli('recipients', 'cases', 'suggestions', 'off')
    assert off.exit_code == 0 and 'no longer suggest' in off.stdout
    assert cli('recipients', 'cases', 'suggestions', 'maybe').exit_code != 0
