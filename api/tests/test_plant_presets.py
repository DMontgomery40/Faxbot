"""Faxbot as the fax annex behind a Teams Direct Routing SBC (N21), copiers that fax over the network and copiers
that send by direct SMTP (N22). Synthetic addresses and numbers; not yet run against a real SBC, tenant or copier.
"""
import pytest

from app import copiers, sip_trunk
from app.intake.sources import mail
from app.intake.sources.settings import SourceInputError, copier_senders, validate

TEAMS = ('teams-sbc-audiocodes', 'teams-sbc-ribbon', 'teams-sbc-oracle', 'teams-sbc-anynode')


def test_each_teams_sbc_sends_the_fax_numbers_to_faxbot_before_teams_and_its_faxes_to_the_carrier():
    for preset_id in TEAMS:
        preset = sip_trunk.PRESETS[preset_id]
        assert preset.kind == sip_trunk.PHONE_SYSTEM and preset.auth_modes == ('ip',)
        steps = ' '.join(preset.admin_steps)
        assert 'your fax numbers' in steps and "Faxbot's address" in steps
        assert "so Faxbot's faxes go out to the carrier" in steps
        assert 'not yet run against a real' in ' '.join(preset.notes)
        assert preset.sources[-1].url == 'https://learn.microsoft.com/en-us/microsoftteams/direct-routing-analog-devices'
        assert all(source.read_on == '2026-10-10' for source in preset.sources)
    assert 'Move it above the row that sends calls from your carrier to Teams: the first matching row wins.' in \
        ' '.join(sip_trunk.PRESETS['teams-sbc-audiocodes'].admin_steps)
    assert 'longest matching To address' in ' '.join(sip_trunk.PRESETS['teams-sbc-oracle'].notes)
    assert len(sip_trunk.TEAMS_PORT_CHECKLIST) == 5
    assert 'Take the fax numbers out of the port order' in sip_trunk.TEAMS_PORT_CHECKLIST[1]


def test_the_copier_list_says_which_makers_document_sip_t38_and_never_assumes_the_rest():
    found = {item['id']: item for item in copiers.catalog()}
    assert {key for key, item in found.items() if item['sip_t38']} == {'ricoh-im', 'konica-minolta-bizhub',
                                                                       'xerox-workcentre-foip'}
    assert not found['canon-imagerunner']['sip_t38'] and 'T.37' in found['canon-imagerunner']['summary']
    assert found['ricoh-im']['steps'] and found['ricoh-im']['sources'][0]['read_on'] == '2026-10-10'
    assert all(item['sources'] or not item['sip_t38'] for item in found.values())


def _message(received, sender='scanner@example.com'):
    raw = (f'Received: {received}\r\nFrom: Scanner <{sender}>\r\nTo: fax@example.com\r\nSubject: +13035550150\r\n'
           'Message-ID: <copier-1@example.com>\r\n\r\nbody\r\n').encode()
    return mail.parse(raw)


SETTINGS = {'checked_by': 'mx.example.com', 'copier_senders': 'scanner@example.com 192.168.1.40/32'}


def test_a_copier_you_listed_is_admitted_only_from_its_own_address_through_your_mail_server():
    good = 'from scanner.local ([192.168.1.40]) by mx.example.com (Postfix) with ESMTP id 1; Sat, 10 Oct 2026'
    assert mail.copier_sender(_message(good), 'scanner@example.com', SETTINGS)
    assert not mail.confirmed(_message(good), 'scanner@example.com', 'mx.example.com')  # no DKIM or SPF at all
    # Another address on the network, another sender, another server, or a Microsoft 365 connector: refused.
    assert not mail.copier_sender(_message(good.replace('192.168.1.40', '192.168.1.41')), 'scanner@example.com',
                                  SETTINGS)
    assert not mail.copier_sender(_message(good, 'boss@example.com'), 'boss@example.com', SETTINGS)
    assert not mail.copier_sender(_message(good.replace('by mx.example.com', 'by evil.example.net')),
                                  'scanner@example.com', SETTINGS)
    assert not mail.copier_sender(_message(good), 'scanner@example.com', {**SETTINGS, 'checked_by': 'microsoft365'})
    assert not mail.copier_sender(_message(good), 'scanner@example.com', {'checked_by': 'mx.example.com'})


def test_copier_senders_are_written_as_address_and_private_network():
    assert copier_senders('Scanner@Example.com 192.168.1.40, b@example.com 10.0.0.0/8') == \
        'scanner@example.com 192.168.1.40/32, b@example.com 10.0.0.0/8'
    assert copier_senders('') == '' and copier_senders(None) == ''
    for wrong in ('scanner@example.com', 'scanner 192.168.1.40', 'scanner@example.com 8.8.8.8',
                  'scanner@example.com not-an-ip'):
        with pytest.raises(SourceInputError):
            copier_senders(wrong)
    base = {'provider': 'other', 'address': 'fax@example.com', 'imap_host': 'mail.example.com',
            'smtp_host': 'mail.example.com', 'checked_by': 'mx.example.com', 'sign_in': 'password'}
    kept = validate('email', 'send', {**base, 'copier_senders': 'scanner@example.com 192.168.1.40'})
    assert kept['copier_senders'] == 'scanner@example.com 192.168.1.40/32'


# -- the console's and the command line's paths ----------------------------------------------------------------------

@pytest.fixture
def sbc_client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from api.tests.test_access_management_http import ORIGIN, _environment
    from app.main import app
    _environment(monkeypatch, tmp_path)
    for name, value in {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'teams-sbc-audiocodes', 'SIP_TRUNK_AUTH': 'ip',
                        'SIP_TRUNK_HOST': '10.30.0.5', 'SIP_TRUNK_CALLER_ID': '+13035550100',
                        'SIP_TRUNK_DIDS': '+13035550100', 'AMI_PASSWORD': 'synthetic-ami-pass'}.items():
        monkeypatch.setenv(name, value)
    with TestClient(app, base_url=ORIGIN, headers={'Origin': ORIGIN}) as test_client:
        yield test_client


def test_the_command_line_prints_the_sbc_steps_with_faxbots_address_and_the_copier_list(sbc_client):
    from typer.testing import CliRunner
    from app.cli.main import app as cli_app
    from api.tests.test_access_management_http import B, BOOTSTRAP

    def run(*args):
        return CliRunner().invoke(cli_app, ['--url', 'https://testserver', '--key', BOOTSTRAP, *args],
                                  obj={'client_factory': lambda address, timeout: (sbc_client, False)},
                                  env={'COLUMNS': '220', 'TZ': 'UTC'})
    annex = run('providers', 'trunk', 'teams-annex', '--vendor', 'audiocodes', '--address', '192.168.1.20',
                '--numbers', '+1303555010x')
    assert annex.exit_code == 0, (annex.stdout, annex.stderr)
    text = ' '.join(annex.stdout.split())
    assert "What you set in AudioCodes Mediant:" in text and "Faxbot's address (192.168.1.20)" in text
    assert 'your fax numbers (+1303555010x)' in text and 'Before the Teams port order:' in text
    assert 'read 10 October 2026' in text
    assert run('providers', 'trunk', 'teams-annex', '--vendor', 'cisco').exit_code != 0
    listed = run('providers', 'trunk', 'copiers')
    assert listed.exit_code == 0 and 'ricoh-im' in listed.stdout and 'Not found' in listed.stdout
    one = run('providers', 'trunk', 'copiers', 'ricoh-im')
    assert one.exit_code == 0 and 'Register/Change/Delete Gateway' in one.stdout
    assert sbc_client.get('/admin/sip/copiers', headers=B).status_code == 200
    presets = {item['id']: item for item in sbc_client.get('/admin/sip/presets', headers=B).json()['presets']}
    assert presets['teams-sbc-audiocodes']['port_checklist'] == list(sip_trunk.TEAMS_PORT_CHECKLIST)
