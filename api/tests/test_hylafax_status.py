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
    (tmp_path / 'hylafax' / 'engine.status').write_text(json.dumps(
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
    (tmp_path / 'hylafax' / 'engine.status').unlink()
    assert await summary(configured, Ami()) == ('stopped', hylafax_engine.STOPPED)
    off = values(tmp_path, SIP_SSLFAX_ENABLED='false')
    engine_running(tmp_path, off)
    assert (await summary(off, Ami()))[1] == ("Faxbot's fast fax service is running on 2 fax lines; "
                                              'faster pages are turned off.')


def test_the_engine_check_is_registered_for_system_diagnostics():
    from app import diagnostics_report
    assert 'ssl fax engine' in [name for name, _ in diagnostics_report._CHECKS]
