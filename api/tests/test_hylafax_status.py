"""The SSL Fax engine's state sentence (trunk page, System diagnostics, command line) and its diagnostics check."""
import json

import pytest

from app import hylafax_engine, sip_trunk
from app.config_values import ConfigurationValues

SECRET = 'synthetic-inbound-secret-0123456789'


def values(tmp_path, **extra):
    return ConfigurationValues.from_environment({
        'FAX_DATA_DIR': str(tmp_path), 'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
        'SIP_TRUNK_PASSWORD': 'Synthetic-Password-1', 'SIP_TRUNK_CALLER_ID': '+15555550100',
        'ASTERISK_INBOUND_SECRET': SECRET, **extra})


class Ami:
    def __init__(self, ready=2, fail=None):
        self.ready, self.fail = ready, fail

    async def iax_lines_ready(self, prefix):
        if self.fail:
            raise self.fail
        return self.ready


def engine_running(tmp_path, configured, *, state='running', reason='', listener='', loaded=True):
    sip_trunk.write_asterisk_configuration(configured)
    (tmp_path / 'hylafax-out').mkdir(exist_ok=True)
    (tmp_path / 'hylafax-out' / 'engine.status').write_text(json.dumps(
        {'state': state, 'reason': reason, 'lines': 2, 'listener': listener}))
    if loaded:
        (tmp_path / 'asterisk' / 'iax.conf.started').write_bytes((tmp_path / 'asterisk' / 'iax.conf').read_bytes())


@pytest.mark.asyncio
async def test_each_engine_state_has_one_sentence(tmp_path):
    configured = values(tmp_path)
    summary = hylafax_engine.engine_summary
    assert await summary(configured, Ami()) == ('not_set_up', hylafax_engine.NOT_SET_UP)
    engine_running(tmp_path, configured)
    state, sentence = await summary(configured, Ami())
    assert state == 'running' and sentence == ("Faxbot's fast fax service is running on 2 fax lines and sends "
                                               'pages faster when the other fax machine allows it.')
    assert (await summary(configured, Ami(ready=1)))[1].startswith("Faxbot's fast fax service is running on 1 fax line ")
    assert await summary(configured, Ami(ready=0)) == ('starting', hylafax_engine.STARTING)
    assert await summary(configured, Ami(fail=ConnectionError())) == ('starting', hylafax_engine.STARTING)
    engine_running(tmp_path, configured, listener='203.0.113.10:10443')
    assert (await summary(configured, Ami()))[1].endswith(
        'Fax machines that call Faxbot can also send their pages faster.')
    engine_running(tmp_path, configured, loaded=False)
    (tmp_path / 'asterisk' / 'iax.conf.started').write_text('older lines')
    assert await summary(configured, Ami()) == ('starting', hylafax_engine.WAITING_FOR_RESTART)
    engine_running(tmp_path, configured, state='failed', reason="Faxbot's fast fax service could not start; "
                                                                 'select Apply and connect to try again.')
    assert (await summary(configured, Ami()))[0] == 'stopped'
    (tmp_path / 'hylafax-out' / 'engine.status').unlink()
    assert await summary(configured, Ami()) == ('stopped', hylafax_engine.STOPPED)
    # Only the engine's own sentences reach the trunk page, and a link in its folder is not followed.
    engine_running(tmp_path, configured, state='failed', reason='Visit http://198.51.100.9 to fix this.')
    assert await summary(configured, Ami()) == ('stopped', hylafax_engine.STOPPED)
    elsewhere = tmp_path / 'elsewhere.json'
    elsewhere.write_text(json.dumps({'state': 'running', 'lines': 2}))
    (tmp_path / 'hylafax-out' / 'engine.status').unlink()
    (tmp_path / 'hylafax-out' / 'engine.status').symlink_to(elsewhere)
    assert hylafax_engine.read_status(configured).state == 'absent'
    (tmp_path / 'hylafax-out' / 'engine.status').unlink()
    off = values(tmp_path, SIP_SSLFAX_ENABLED='false')
    engine_running(tmp_path, off)
    assert (await summary(off, Ami()))[1] == ("Faxbot's fast fax service is running on 2 fax lines; "
                                              'faster pages are turned off.')
    # After its T.38 call heard no fax machine, the engine says it sends audio fax and how to try T.38 again.
    hylafax_engine.note_t38_failure(configured)
    state, sentence = await summary(configured, Ami())
    assert state == 'running' and sentence.endswith(' ' + hylafax_engine.ENGINE_AUDIO)


def test_the_engine_check_is_registered_for_system_diagnostics():
    from app import diagnostics_report
    assert 'ssl fax engine' in [name for name, _ in diagnostics_report._CHECKS]


class Out:
    def __init__(self):
        self.lines = []

    def line(self, text):
        self.lines.append(text)

    def fields(self, rows):
        pass


def test_the_command_line_names_its_own_command_to_try_t38_again():
    from app.cli.commands import trunk
    out = Out()
    trunk._status_lines(out, {'configured': True, 'message': 'The trunk is ready.', 'engine_audio': True,
                              'engine_text': "Faxbot's fast fax service is running on 2 fax lines. "
                                             + hylafax_engine.ENGINE_AUDIO})
    assert out.lines[-1] == 'To try T.38 again, run faxbot providers trunk apply.'
    assert not any('select Apply' in line for line in out.lines)
    quiet = Out()
    trunk._status_lines(quiet, {'configured': True, 'message': 'The trunk is ready.', 'engine_audio': False})
    assert not any('T.38' in line for line in quiet.lines)


@pytest.mark.asyncio
async def test_diagnostics_asks_for_attention_while_the_engine_is_on_audio_on_its_own(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app import config, diagnostics_report
    configured = values(tmp_path)
    engine_running(tmp_path, configured)
    monkeypatch.setattr(config, 'configuration_values', lambda: configured)
    monkeypatch.setattr(diagnostics_report, '_uses_trunk', lambda request: True)
    import app.ami as ami
    monkeypatch.setattr(ami, 'ami_client', Ami())
    context = SimpleNamespace(request=None, identity=None)
    [finding] = await diagnostics_report.ssl_fax_engine(context)
    assert finding.status == diagnostics_report.OK
    hylafax_engine.note_t38_failure(configured)
    [finding] = await diagnostics_report.ssl_fax_engine(context)
    assert finding.status == diagnostics_report.ATTENTION and hylafax_engine.ENGINE_AUDIO in finding.sentence



@pytest.mark.asyncio
async def test_diagnostics_says_when_the_engine_missed_a_call_and_restarted(tmp_path, monkeypatch):
    """System diagnostics shows the same one sentence as the trunk page after a fax call no free line answered."""
    import time
    from types import SimpleNamespace
    from app import config, diagnostics_report
    configured = values(tmp_path)
    engine_running(tmp_path, configured)
    monkeypatch.setattr(config, 'configuration_values', lambda: configured)
    monkeypatch.setattr(diagnostics_report, '_uses_trunk', lambda request: True)
    import app.ami as ami
    monkeypatch.setattr(ami, 'ami_client', Ami())
    assert hylafax_engine.request_restart(configured, reason='missed_call', at=int(time.time()) - 60)
    [finding] = await diagnostics_report.ssl_fax_engine(SimpleNamespace(request=None, identity=None))
    assert finding.sentence == hylafax_engine.missed_sentence(configured, hylafax_engine.read_status(configured))
    assert finding.sentence.startswith("Faxbot's fast fax service did not answer the ")
