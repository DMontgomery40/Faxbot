"""Carrier presets render the exact Asterisk trunk configuration in the golden files."""
import os
from pathlib import Path
import stat

import pytest

from app import sip_trunk
from app.config_values import ConfigurationValues


FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'sip'
PASSWORD = 'synthetic-Trunk-Pass!42'

CASES = {
    'telnyx-registration': {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                            'SIP_TRUNK_PASSWORD': PASSWORD},
    'telnyx-ip': {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip'},
    'signalwire-registration': {'SIP_TRUNK_PRESET': 'signalwire', 'SIP_TRUNK_HOST': 'example.sip.signalwire.com',
                                'SIP_TRUNK_USERNAME': 'faxbot', 'SIP_TRUNK_PASSWORD': PASSWORD},
    'sinch-registration': {'SIP_TRUNK_PRESET': 'sinch', 'SIP_TRUNK_HOST': 'example.pstn.sinch.com',
                           'SIP_TRUNK_USERNAME': 'faxbot', 'SIP_TRUNK_PASSWORD': PASSWORD,
                           'SIP_TRUNK_TRANSPORT': 'tls'},
    'sinch-ip': {'SIP_TRUNK_PRESET': 'sinch', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'example.pstn.sinch.com'},
    'anveo-ip': {'SIP_TRUNK_PRESET': 'anveo', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_CODECS': 'ulaw'},
    'flowroute-registration': {'SIP_TRUNK_PRESET': 'flowroute', 'SIP_TRUNK_USERNAME': '12345678',
                               'SIP_TRUNK_PASSWORD': PASSWORD},
    'flowroute-ip': {'SIP_TRUNK_PRESET': 'flowroute', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_USERNAME': '12345678',
                     'SIP_TRUNK_HOST': 'us-east-va.sip.flowroute.com'},
    'custom-registration-proxy': {'SIP_TRUNK_PRESET': 'custom', 'SIP_TRUNK_HOST': 'sip.example.net',
                                  'SIP_TRUNK_PORT': '5080', 'SIP_TRUNK_TRANSPORT': 'tcp',
                                  'SIP_TRUNK_USERNAME': 'faxbot', 'SIP_TRUNK_PASSWORD': PASSWORD,
                                  'SIP_TRUNK_OUTBOUND_PROXY': 'proxy.example.net:5070',
                                  'SIP_T38_ENABLED': 'false', 'SIP_TRUNK_CODECS': 'alaw,ulaw'},
    'custom-ip': {'SIP_TRUNK_PRESET': 'custom', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': 'peer'},
}


def values(environment):
    return ConfigurationValues.from_environment({'SIP_TRUNK_CALLER_ID': '+15555550100', **environment})


@pytest.mark.parametrize('case', sorted(CASES))
def test_each_preset_renders_its_golden_configuration(case):
    rendered = sip_trunk.render_pjsip(values(CASES[case]))
    expected = (FIXTURES / f'{case}.conf').read_text()
    assert rendered == expected


def test_every_preset_has_a_golden_case_and_every_carrier_cites_dated_sources():
    covered = {CASES[case]['SIP_TRUNK_PRESET'] for case in CASES}
    assert covered == set(sip_trunk.PRESETS)
    for preset in sip_trunk.PRESETS.values():
        if preset.id != 'custom':
            assert preset.sources, preset.id
        for source in preset.sources:
            assert source.url.startswith('https://') and source.read_on == '2026-10-03'
        assert set(preset.codecs) <= {'ulaw', 'alaw'}
    assert {fixture.stem for fixture in FIXTURES.glob('*.conf')} == set(CASES)


def test_registration_needs_credentials_and_errors_never_echo_values():
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser'}))
    assert error.value.fields == ('sip_trunk_password',)
    assert 'faxbotuser' not in str(error.value)
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': 'anveo', 'SIP_TRUNK_PASSWORD': PASSWORD,
                                       'SIP_TRUNK_USERNAME': 'faxbot'}))
    assert error.value.fields == ('sip_trunk_auth',)
    assert PASSWORD not in str(error.value)
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': 'signalwire', 'SIP_TRUNK_USERNAME': 'faxbot',
                                       'SIP_TRUNK_PASSWORD': PASSWORD}))
    assert error.value.fields == ('sip_trunk_host',)
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': 'flowroute', 'SIP_TRUNK_AUTH': 'ip'}))
    assert error.value.fields == ('sip_trunk_username',)


def test_calls_need_a_carrier_authorized_caller_id_but_configuration_does_not():
    environment = {**CASES['telnyx-ip'], 'SIP_TRUNK_CALLER_ID': ''}
    plain = ConfigurationValues.from_environment(environment)
    assert sip_trunk.render_pjsip(plain)
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.effective_trunk(plain, for_calls=True)
    assert error.value.fields == ('sip_trunk_caller_id',)


def test_trunk_representation_and_catalog_never_contain_the_password():
    trunk = sip_trunk.effective_trunk(values(CASES['telnyx-registration']))
    assert PASSWORD not in repr(trunk)
    assert PASSWORD not in repr(values(CASES['telnyx-registration']))
    assert PASSWORD not in str(sip_trunk.preset_catalog())


@pytest.mark.parametrize('case,number,expected', [
    ('telnyx-ip', '+15555550123', '+15555550123'),
    ('telnyx-ip', '15555550123', '+15555550123'),
    ('telnyx-ip', '5555550123', '+15555550123'),
    ('telnyx-ip', '+442071838750', '+442071838750'),
    ('flowroute-registration', '+15555550123', '15555550123'),
    ('flowroute-registration', '5555550123', '15555550123'),
    ('flowroute-ip', '+15555550123', '12345678*15555550123'),
    ('custom-ip', '5555550123', '5555550123'),
    ('custom-ip', '+15555550123', '+15555550123'),
])
def test_dialed_number_uses_the_carrier_format(case, number, expected):
    assert sip_trunk.dial_number(sip_trunk.effective_trunk(values(CASES[case])), number) == expected


@pytest.mark.parametrize('number', ['', '12', '1555&x', '+1 555', '1555\r\nAction: Command', None])
def test_unusable_destination_is_refused_before_dialing(number):
    trunk = sip_trunk.effective_trunk(values(CASES['telnyx-ip']))
    with pytest.raises(ValueError):
        sip_trunk.dial_number(trunk, number)


def test_written_configuration_is_private_and_replaced_atomically(tmp_path):
    configured = values({**CASES['telnyx-registration'], 'FAX_DATA_DIR': str(tmp_path)})
    path = sip_trunk.write_asterisk_configuration(configured)
    assert path == tmp_path / 'asterisk' / 'pjsip.conf'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text() == (FIXTURES / 'telnyx-registration.conf').read_text()
    path.write_text('stale')
    sip_trunk.write_asterisk_configuration(configured)
    assert path.read_text() != 'stale'
    assert sorted(item.name for item in path.parent.iterdir()) == ['pjsip.conf']


def test_command_line_writes_from_the_environment_without_printing_secrets(tmp_path, monkeypatch, capsys):
    for key, value in {**CASES['telnyx-registration'], 'FAX_DATA_DIR': str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    assert sip_trunk.main(['write']) == 0
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert (tmp_path / 'asterisk' / 'pjsip.conf').exists()
    monkeypatch.setenv('SIP_TRUNK_PASSWORD', '')
    assert sip_trunk.main(['write']) == 1
    assert sip_trunk.main([]) == 2


def test_rate_card_seed_covers_every_carrier_preset_with_dated_sources():
    import json
    import re
    from datetime import date
    document = json.loads((Path(__file__).resolve().parents[2] / 'config' / 'rate_cards.json').read_text())
    cards = document['cards']
    carriers = {preset for preset in sip_trunk.PRESETS if preset != 'custom'}
    assert {(card['preset'], card['direction']) for card in cards} == {
        (preset, direction) for preset in carriers for direction in ('outbound', 'inbound')}
    money = re.compile(r'[0-9]+(?:\.[0-9]{1,6})?')
    for card in cards:
        assert card['provider_id'] == 'sip-' + card['preset'] and card['provider'] == 'sip'
        assert card['currency'] == 'USD' and date.fromisoformat(card['advertised_on']) == date(2026, 10, 3)
        assert card['source_url'].startswith('https://') and card['source_url'] in card['sources'] or \
            card['source_url'] == card['sources'][0]
        for field in ('per_minute', 'per_page', 'per_call', 'number_rental_monthly', 'number_setup'):
            assert card[field] is None or money.fullmatch(card[field]), (card['label'], field)
        assert card['billing_increment_seconds'] in (None, 1, 6, 60)
        assert card['rounding'] in {'whole_minute', 'per_second', '6_second', 'not_published'}
        assert (card['billing_increment_seconds'] is None) == (card['rounding'] == 'not_published')
        assert card['notes'].endswith('.')
