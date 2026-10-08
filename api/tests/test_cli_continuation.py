"""faxbot sent continue: the pages a broken call left, their cost, and sending only those, on the real application."""
import pytest

import app.main as main_module
from app.routing.continuation import continuation_id
from app.work.certainty import CertaintyStore, CertaintyWorker, PartnerQuestion
from api.tests.test_cli import Cli, _serve
from api.tests.test_cli_work import KEY
from api.tests.test_continuation import broken, fax_ids


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(CertaintyWorker, 'step', lambda self, now=None: False)

    async def idle(self):
        return False
    monkeypatch.setattr(PartnerQuestion, 'step', idle)
    for client in _serve(monkeypatch, tmp_path):
        yield Cli(client)


def test_continue_shows_the_pages_left_and_sends_only_those_once(cli):
    job, attempt = broken(cli.client, headers=KEY)
    shown = cli('sent', 'continue', job)
    assert shown.exit_code == 0, shown.stdout + shown.stderr
    for words in ('pages 8–20', 'The call used error correction, and the receiving machine confirmed the first',
                  'Page 8 may already have arrived, so the recipient may get it twice.',
                  f'Send them with: faxbot sent continue {job} --send'):
        assert words in shown.stdout, shown.stdout
    before = fax_ids()
    sent = cli('sent', 'continue', job, '--send')
    assert sent.exit_code == 0, sent.stdout + sent.stderr
    new_fax = continuation_id(attempt)
    assert fax_ids() - before == {new_fax}
    assert f'Pages 8–20 went as a new fax: {new_fax}.' in sent.stdout
    linked = cli('sent', 'continue', new_fax)
    assert linked.exit_code == 0 and f'This fax carries pages 8–20 of fax {job}, whose call broke.' in linked.stdout
    again = cli('sent', 'continue', job, '--send')
    assert again.exit_code != 0 and fax_ids() - before == {new_fax}


def test_a_fax_waiting_to_be_settled_needs_a_reason_and_is_settled_by_it(cli):
    job, attempt = broken(cli.client, headers=KEY)
    assert CertaintyStore(main_module.app.state.configuration_runtime.manager.store.engine).feed(
        main_module.app.state.access_runtime.control) == 1
    before = fax_ids()
    missing = cli('sent', 'continue', job, '--send')
    assert missing.exit_code != 0 and 'Add --reason' in (missing.stdout + missing.stderr)
    assert fax_ids() == before
    sent = cli('sent', 'continue', job, '--send', '--reason', 'The machine confirmed pages 1 to 7')
    assert sent.exit_code == 0, sent.stdout + sent.stderr
    assert fax_ids() - before == {continuation_id(attempt)}
    settled = cli('sent', 'probe', job)
    assert 'Pages 8–20 were sent as a new fax.' in settled.stdout


def test_unknown_pages_say_why_and_send_nothing(cli):
    job, _ = broken(cli.client, headers=KEY, route='phaxio')
    shown = cli('sent', 'continue', job)
    assert shown.exit_code == 0 and shown.stdout.startswith('Phaxio does not report how many pages it sent')
    refused = cli('sent', 'continue', job, '--send')
    assert refused.exit_code != 0
