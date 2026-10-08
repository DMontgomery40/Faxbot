"""faxbot sent uncertain, probe, settle, assign and uncertain-settings against the real application."""
from datetime import datetime

import pytest
import sqlalchemy as sa

import app.main as main_module
from app.work.certainty import CertaintyStore, CertaintyWorker, PartnerQuestion, resend_id
from api.tests.test_cli import Cli, _serve
from api.tests.test_cli_work import KEY, ready_user


NUMBER = '+12025550123'


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(CertaintyWorker, 'step', lambda self, now=None: False)

    async def idle(self):
        return False
    monkeypatch.setattr(PartnerQuestion, 'step', idle)
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def engine():
    return main_module.app.state.configuration_runtime.manager.store.engine


def uncertain(client):
    sent = client.post('/fax', headers=KEY, data={'to': NUMBER},
                       files={'file': ('note.txt', b'Synthetic referral body', 'text/plain')})
    assert sent.status_code == 202, sent.text
    job = sent.json()['id']
    metadata = sa.MetaData()
    attempts, deliveries = (sa.Table(name, metadata, autoload_with=engine())
                            for name in ('outbound_attempts', 'outbound_deliveries'))
    now = datetime.utcnow()
    with engine().begin() as connection:
        connection.execute(attempts.insert().values(id='attempt-' + job[:24], job_id=job, sequence=1,
                                                    phase='uncertain', error_category='worker_lost',
                                                    created_at=now, submitted_at=now))
        connection.execute(deliveries.update().where(deliveries.c.id == job).values(
            state='reconciliation_required', dispatch_mode='normal', attempt_id='attempt-' + job[:24]))
    assert CertaintyStore(engine()).feed(main_module.app.state.access_runtime.control) == 1
    return job


def test_list_probe_assign_and_settle_an_uncertain_fax(cli, tmp_path):
    job = uncertain(cli.client)
    listing = cli('sent', 'uncertain', '--ids')
    assert listing.exit_code == 0, listing.stdout + listing.stderr
    assert '1 sent fax needs settling; 0 overdue.' in listing.stdout and job in listing.stdout
    assert 'Faxbot stopped while the fax was being sent.' in listing.stdout
    probe = cli('sent', 'probe', job)
    assert probe.exit_code == 0, probe.stdout + probe.stderr
    for words in ('Ask the partner', 'Read the call record', 'Fax a receipt query', 'Phone the recipient',
                  'Phone script:', f'Call {NUMBER}.'):
        assert words in probe.stdout
    saved = tmp_path / 'query.pdf'
    drafted = cli('sent', 'probe', job, '--query-pdf', saved)
    assert drafted.exit_code == 0 and saved.read_bytes().startswith(b'%PDF')
    ready_user(cli.client, 'dana', 'Dana Example')
    assigned = cli('sent', 'assign', job, 'dana')
    assert assigned.exit_code == 0 and 'Assigned to Dana Example; settle by ' in assigned.stdout
    both = cli('sent', 'settle', job, '--delivered', '--not-delivered', '--reason', 'x')
    assert both.exit_code != 0 and 'Choose one of --delivered, --not-delivered or --cant-tell.' in both.stderr + both.stdout
    settled = cli('sent', 'settle', job, '--not-delivered', '--send-again', '--reason', 'Their front desk has no fax')
    assert settled.exit_code == 0, settled.stdout + settled.stderr
    assert 'Settled as not delivered by ' in settled.stdout and resend_id(cli.json('sent', 'probe', job)['id']) in settled.stdout
    history = cli('sent', 'probe', job, '--history')
    assert 'settled it as not delivered: Their front desk has no fax.' in history.stdout
    assert cli.json('sent', 'uncertain')['items'] == []
    assert len(cli.json('sent', 'uncertain', '--settled')['items']) == 1
    nothing = cli('sent', 'probe', resend_id(cli.json('sent', 'probe', job)['id']))
    assert nothing.exit_code != 0 and 'a new fax sent again for fax' in nothing.stderr + nothing.stdout


def test_uncertain_settings(cli):
    shown = cli('sent', 'uncertain-settings')
    assert shown.exit_code == 0 and 'within 24 hours' in shown.stdout
    ready_user(cli.client, 'fran', 'Fran Example', role='role_fax_viewer')
    changed = cli('sent', 'uncertain-settings', '--hours', '8', '--fallback', 'fran')
    assert changed.exit_code == 0, changed.stdout + changed.stderr
    assert 'within 8 hours' in changed.stdout and 'Fran Example settles it.' in changed.stdout
    cleared = cli.json('sent', 'uncertain-settings', '--no-fallback')
    assert cleared['fallback'] is None and cleared['settle_hours'] == 8
