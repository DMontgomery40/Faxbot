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
    'telnyx-registration-tcp': {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                                'SIP_TRUNK_PASSWORD': PASSWORD, 'SIP_TRUNK_TRANSPORT': 'tcp'},
    'telnyx-registration-udp': {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                                'SIP_TRUNK_PASSWORD': PASSWORD, 'SIP_TRUNK_TRANSPORT': 'udp'},
    'telnyx-ip': {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'ip'},
    'signalwire-registration': {'SIP_TRUNK_PRESET': 'signalwire', 'SIP_TRUNK_HOST': 'example.sip.signalwire.com',
                                'SIP_TRUNK_USERNAME': 'faxbot', 'SIP_TRUNK_PASSWORD': PASSWORD},
    'sinch-registration': {'SIP_TRUNK_PRESET': 'sinch', 'SIP_TRUNK_HOST': 'example.pstn.sinch.com',
                           'SIP_TRUNK_USERNAME': 'faxbot', 'SIP_TRUNK_PASSWORD': PASSWORD,
                           'SIP_TRUNK_TRANSPORT': 'tls'},
    'telnyx-registration-nat': {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_USERNAME': 'faxbotuser',
                                'SIP_TRUNK_PASSWORD': PASSWORD, 'SIP_EXTERNAL_ADDRESS': '203.0.113.10'},
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
    # UK and Australian carriers.
    'gamma-ip': {'SIP_TRUNK_PRESET': 'gamma', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': '192.0.2.40',
                 'FAX_DEFAULT_COUNTRY': 'GB'},
    'bt-one-voice-ip': {'SIP_TRUNK_PRESET': 'bt-one-voice', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': '192.0.2.50',
                        'FAX_DEFAULT_COUNTRY': 'GB', 'SIP_T38_ENABLED': 'false'},
    'telstra-sip-connect-registration': {'SIP_TRUNK_PRESET': 'telstra-sip-connect',
                                         'SIP_TRUNK_HOST': 'sipconnect.example.net', 'SIP_TRUNK_USERNAME': 'faxbot',
                                         'SIP_TRUNK_PASSWORD': PASSWORD,
                                         'SIP_TRUNK_OUTBOUND_PROXY': 'sbc.example.net:5060',
                                         'FAX_DEFAULT_COUNTRY': 'AU'},
    # Phone systems on the local network: identified by address, no sign-in, A-law first outside North America.
    'avaya-ipoffice-uk': {'SIP_TRUNK_PRESET': 'avaya-ipoffice', 'SIP_TRUNK_AUTH': 'ip',
                          'SIP_TRUNK_HOST': '192.168.10.5', 'FAX_DEFAULT_COUNTRY': 'GB',
                          'SIP_TRUNK_DIAL_FORMAT': 'local', 'SIP_TRUNK_DIAL_PREFIX': '9'},
    'avaya-aura-us-tcp': {'SIP_TRUNK_PRESET': 'avaya-aura', 'SIP_TRUNK_AUTH': 'ip', 'SIP_TRUNK_HOST': '10.20.0.15',
                          'SIP_TRUNK_TRANSPORT': 'tcp', 'FAX_DEFAULT_COUNTRY': 'US'},
}


def values(environment):
    return ConfigurationValues.from_environment({'SIP_TRUNK_CALLER_ID': '+15555550100', **environment})


@pytest.mark.parametrize('case', sorted(CASES))
def test_each_preset_renders_its_golden_configuration(case):
    rendered = sip_trunk.render_pjsip(values(CASES[case]))
    expected = (FIXTURES / f'{case}.conf').read_text()
    assert rendered == expected


def test_every_preset_has_a_golden_case_and_every_preset_cites_dated_sources():
    covered = {CASES[case]['SIP_TRUNK_PRESET'] for case in CASES}
    assert covered == set(sip_trunk.PRESETS)
    for preset in sip_trunk.PRESETS.values():
        if preset.id != 'custom':
            assert preset.sources, preset.id
            # The console links the first source as the carrier's documentation page.
            assert not preset.sources[0].url.endswith('.json'), preset.id
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
    # Sinch receiving addresses are unpublished, so IP sign-in could send but never receive.
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': 'sinch', 'SIP_TRUNK_AUTH': 'ip',
                                       'SIP_TRUNK_HOST': 'example.pstn.sinch.com'}))
    assert error.value.fields == ('sip_trunk_auth',)


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
    ('telnyx-ip', '+442071838750', '+442071838750'),
    ('telnyx-ip', '+441782684953', '+441782684953'),
    ('sinch-registration', '+441782684953', '+441782684953'),
    ('signalwire-registration', '+441782684953', '+441782684953'),
    ('anveo-ip', '+441782684953', '+441782684953'),
    ('flowroute-registration', '+15555550123', '15555550123'),
    ('flowroute-registration', '+441782684953', '441782684953'),
    ('flowroute-ip', '+15555550123', '12345678*15555550123'),
    ('custom-ip', '+15555550123', '+15555550123'),
    ('custom-ip', '+441782684953', '+441782684953'),
    ('gamma-ip', '+441782684953', '+441782684953'),
    ('bt-one-voice-ip', '+442079460000', '+442079460000'),
    ('telstra-sip-connect-registration', '+61291234567', '+61291234567'),
    # A phone here dials the national number at home and the international prefix abroad, after the prefix.
    ('avaya-ipoffice-uk', '+441782684953', '901782684953'),
    ('avaya-ipoffice-uk', '+16502530000', '90016502530000'),
    ('avaya-aura-us-tcp', '+16502530000', '+16502530000'),
])
def test_dialed_number_uses_the_carrier_format(case, number, expected):
    assert sip_trunk.dial_number(sip_trunk.effective_trunk(values(CASES[case])), number) == expected


# National or unprefixed digits never reach a carrier: they are resolved to E.164
# for the installation country when the fax is accepted, not guessed here.
@pytest.mark.parametrize('number', ['', '12', '1555&x', '+1 555', '1555\r\nAction: Command', None,
                                    '5555550123', '15555550123', '01782 684953', '441782684953'])
def test_unusable_destination_is_refused_before_dialing(number):
    trunk = sip_trunk.effective_trunk(values(CASES['telnyx-ip']))
    with pytest.raises(ValueError):
        sip_trunk.dial_number(trunk, number)


def test_written_configuration_is_private_and_replaced_atomically(tmp_path):
    configured = values({**CASES['telnyx-registration'], 'FAX_DATA_DIR': str(tmp_path),
                         'ASTERISK_INBOUND_SECRET': 'synthetic-inbound-secret'})
    path = sip_trunk.write_asterisk_configuration(configured)
    assert path == tmp_path / 'asterisk' / 'pjsip.conf'
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text() == (FIXTURES / 'telnyx-registration.conf').read_text()
    secret = tmp_path / 'asterisk' / 'inbound.secret'
    assert secret.read_text() == 'synthetic-inbound-secret' and stat.S_IMODE(os.stat(secret).st_mode) == 0o600
    path.write_text('stale')
    sip_trunk.write_asterisk_configuration(configured)
    assert path.read_text() != 'stale'
    assert sorted(item.name for item in path.parent.iterdir()) == ['inbound.secret', 'pjsip.conf']
    # Clearing the inbound secret in Faxbot removes the copy Asterisk reads.
    sip_trunk.write_asterisk_configuration(values({**CASES['telnyx-registration'], 'FAX_DATA_DIR': str(tmp_path)}))
    assert sorted(item.name for item in path.parent.iterdir()) == ['pjsip.conf']


def test_public_address_is_advertised_only_outside_private_networks():
    rendered = sip_trunk.render_pjsip(values(CASES['telnyx-registration-nat']))
    assert 'external_media_address=203.0.113.10\nexternal_signaling_address=203.0.113.10\n' in rendered
    assert 'local_net=172.16.0.0/12' in rendered and 'local_net=10.0.0.0/8' in rendered
    # Nobody typed an address: Asterisk fills in what STUN found at start, or drops the lines.
    automatic = sip_trunk.render_pjsip(values(CASES['telnyx-registration']))
    assert ('external_media_address=@FAXBOT_PUBLIC_ADDRESS@\nexternal_signaling_address=@FAXBOT_PUBLIC_ADDRESS@\n'
            'local_net=@FAXBOT_LOCAL_NET@\n') in automatic
    assert '203.0.113' not in automatic and 'local_net=172.16.0.0/12' not in automatic
    with pytest.raises(Exception):
        values({'SIP_EXTERNAL_ADDRESS': '203.0.113.10;evil'})


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
    # A phone system has no card: the carrier behind it bills the calls.
    carriers = {preset for preset, item in sip_trunk.PRESETS.items() if preset != 'custom' and not item.phone_system}
    assert {(card['preset'], card['direction']) for card in cards} == {
        (preset, direction) for preset in carriers for direction in ('outbound', 'inbound')}
    money = re.compile(r'[0-9]+(?:\.[0-9]{1,6})?')
    for card in cards:
        assert card['provider_id'] == 'sip-' + card['preset'] and card['provider'] == 'sip'
        # US cards are in dollars; a UK or Australian carrier's card is in its own currency and, with no
        # published price, carries none.
        assert date.fromisoformat(card['advertised_on']) == date(2026, 10, 3)
        assert card['currency'] == 'USD' or (card['currency'] in ('GBP', 'AUD') and card['per_minute'] is None)
        assert card['source_url'].startswith('https://') and card['source_url'] in card['sources'] or \
            card['source_url'] == card['sources'][0]
        for field in ('per_minute', 'per_page', 'per_call', 'number_rental_monthly', 'number_setup'):
            assert card[field] is None or money.fullmatch(card[field]), (card['label'], field)
        assert card['billing_increment_seconds'] in (None, 1, 6, 60)
        assert card['rounding'] in {'whole_minute', 'per_second', '6_second', 'not_published'}
        assert (card['billing_increment_seconds'] is None) == (card['rounding'] == 'not_published')
        assert card['notes'].endswith('.')


# Phone systems and the UK/AU carriers ---------------------------------------------------------------


def test_a_phone_system_is_identified_by_its_address_with_no_sign_in_or_prefix():
    """The PBX's own INVITEs are accepted because identify matches the address entered, nothing else."""
    for case, host in (('avaya-ipoffice-uk', '192.168.10.5'), ('avaya-aura-us-tcp', '10.20.0.15')):
        rendered = sip_trunk.render_pjsip(values(CASES[case]))
        identify = rendered.split('[trunk-identify]')[1]
        assert [line for line in identify.splitlines() if line.startswith('match=')] == [f'match={host}']
        assert f'contact=sip:{host}:5060' in rendered
        assert 'type=auth' not in rendered and 'type=registration' not in rendered
        assert 'outbound_auth' not in rendered and 'username=' not in rendered
        trunk = sip_trunk.effective_trunk(values(CASES[case]), for_calls=True)
        assert trunk.auth == 'ip' and trunk.username == '' and not trunk.preset.ip_dial_prefix
    # A phone system signs in by address only; registration is refused by field name.
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': 'avaya-ipoffice', 'SIP_TRUNK_HOST': '192.168.10.5',
                                       'SIP_TRUNK_USERNAME': 'fax', 'SIP_TRUNK_PASSWORD': PASSWORD}))
    assert error.value.fields == ('sip_trunk_auth',)
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': 'avaya-aura', 'SIP_TRUNK_AUTH': 'ip'}))
    assert error.value.fields == ('sip_trunk_host',)
    # No certificate path to a PBX: encrypted signaling is not offered.
    with pytest.raises(sip_trunk.TrunkConfigurationError) as error:
        sip_trunk.render_pjsip(values({**CASES['avaya-aura-us-tcp'], 'SIP_TRUNK_TRANSPORT': 'tls'}))
    assert error.value.fields == ('sip_trunk_transport',)


def test_a_phone_system_hears_the_local_network_address_never_the_internet_one():
    rendered = sip_trunk.render_pjsip(values({**CASES['avaya-ipoffice-uk'], 'SIP_EXTERNAL_ADDRESS': '203.0.113.10',
                                              'SIP_TRUNK_OUTBOUND_PROXY': 'proxy.example.net'}))
    assert ('external_media_address=@FAXBOT_LAN_ADDRESS@\nexternal_signaling_address=@FAXBOT_LAN_ADDRESS@\n'
            in rendered)
    # No local_net: the PBX is on a private network itself, so a private-range local_net would keep
    # Asterisk from naming the published address and leak the Docker network's own address.
    assert 'local_net' not in rendered and '@FAXBOT_PUBLIC_ADDRESS@' not in rendered
    assert '203.0.113.10' not in rendered and 'outbound_proxy' not in rendered
    # Carriers keep the internet address path unchanged.
    carrier = sip_trunk.render_pjsip(values(CASES['gamma-ip']))
    assert '@FAXBOT_LAN_ADDRESS@' not in carrier and 'local_net=@FAXBOT_LOCAL_NET@' in carrier


@pytest.mark.parametrize('country,expected', [('GB', 'alaw,ulaw'), ('AU', 'alaw,ulaw'), ('IE', 'alaw,ulaw'),
                                              ('US', 'ulaw,alaw'), ('CA', 'ulaw,alaw'), ('JP', 'ulaw,alaw')])
def test_codec_order_follows_the_installation_country(country, expected):
    for preset in ('avaya-ipoffice', 'avaya-aura', 'bt-one-voice'):
        rendered = sip_trunk.render_pjsip(values({'SIP_TRUNK_PRESET': preset, 'SIP_TRUNK_AUTH': 'ip',
                                                  'SIP_TRUNK_HOST': '192.168.10.5', 'FAX_DEFAULT_COUNTRY': country}))
        assert f'disallow=all\nallow={expected}\n' in rendered, (preset, country)
    # A person's own order wins; UK and Australian carriers keep A-law first wherever Faxbot runs.
    chosen = values({**CASES['avaya-ipoffice-uk'], 'FAX_DEFAULT_COUNTRY': country, 'SIP_TRUNK_CODECS': 'ulaw'})
    assert 'allow=ulaw\n' in sip_trunk.render_pjsip(chosen)
    gamma = values({**CASES['gamma-ip'], 'FAX_DEFAULT_COUNTRY': country})
    assert 'allow=alaw,ulaw\n' in sip_trunk.render_pjsip(gamma)


@pytest.mark.parametrize('country,number,expected', [
    ('GB', '+442079460000', '902079460000'),
    ('US', '+16502530000', '916502530000'),
    ('US', '+442079460000', '9011442079460000'),
    ('AU', '+61291234567', '90291234567'),
    ('AU', '+442079460000', '90011442079460000'),
])
def test_numbers_are_dialled_the_way_a_phone_at_the_installation_dials_them(country, number, expected):
    trunk = sip_trunk.effective_trunk(values({**CASES['avaya-ipoffice-uk'], 'FAX_DEFAULT_COUNTRY': country}))
    assert sip_trunk.dial_number(trunk, number) == expected


def test_the_outside_line_prefix_applies_only_to_numbers_dialled_as_a_phone_here_dials_them():
    e164 = sip_trunk.effective_trunk(values({**CASES['avaya-ipoffice-uk'], 'SIP_TRUNK_DIAL_FORMAT': 'e164'}))
    assert e164.dial_prefix == '' and sip_trunk.dial_number(e164, '+441782684953') == '+441782684953'
    # Carriers without a choice keep their documented format whatever is saved.
    telnyx = sip_trunk.effective_trunk(values({**CASES['telnyx-ip'], 'SIP_TRUNK_DIAL_FORMAT': 'local',
                                               'SIP_TRUNK_DIAL_PREFIX': '9'}))
    assert sip_trunk.dial_number(telnyx, '+15555550123') == '+15555550123'
    with pytest.raises(Exception):
        values({'SIP_TRUNK_DIAL_PREFIX': '9#'})
    with pytest.raises(Exception):
        values({'SIP_TRUNK_DIAL_PREFIX': '12345'})


def test_a_dialled_number_too_long_for_the_fax_engine_is_refused_before_any_call():
    from app import ami
    trunk = sip_trunk.effective_trunk(values({**CASES['avaya-ipoffice-uk'], 'FAX_DEFAULT_COUNTRY': 'US',
                                              'SIP_TRUNK_DIAL_PREFIX': '9999'}), for_calls=True)
    # 9999 + 011 + a 15-digit international number is 22 digits.
    with pytest.raises(ValueError):
        sip_trunk.dial_number(trunk, '+491234567890123')
    # Every number the phone-system path produces is one the fax engine accepts.
    fields = ami.prepare_originate_fields('job', '+442079460000', '/faxdata/outbound/a.tiff', caller_id='+15555550100',
                                          dial=sip_trunk.dial_number(trunk, '+442079460000'))
    assert fields


def test_lan_address_record_gives_the_address_ports_and_faxes_at_once(tmp_path):
    configured = values({**CASES['avaya-ipoffice-uk'], 'FAX_DATA_DIR': str(tmp_path)})
    assert sip_trunk.read_lan_address(configured) is None
    record = tmp_path / 'asterisk' / 'lan-address'
    record.parent.mkdir()
    record.write_text('{"address": "192.168.10.20", "sip_port": 5060, "media_ports": "4000-4019"}\n')
    assert sip_trunk.read_lan_address(configured) == {
        'address': '192.168.10.20', 'sip_port': 5060, 'media_ports': '4000-4019', 'media_first': 4000,
        'media_last': 4019, 'faxes_at_once': 6}
    # As start.sh divides a range: 32 ports carry ten faxes, 100 ports 33.
    assert sip_trunk.faxes_at_once(4000, 4031) == 10 and sip_trunk.faxes_at_once(4000, 4099) == 33
    for broken in ('{"address": "192.168.10.20;x", "media_ports": "4000-4019"}', 'not json',
                   '{"address": "192.168.10.20", "media_ports": "4019-4000"}', '[]'):
        record.write_text(broken)
        assert sip_trunk.read_lan_address(configured) is None


def test_catalog_says_which_presets_are_phone_systems_and_lists_the_administrator_steps():
    catalog = {item['id']: item for item in sip_trunk.preset_catalog()}
    assert {item for item in catalog if catalog[item]['kind'] == 'phone_system'} == {'avaya-ipoffice', 'avaya-aura'}
    for preset in ('avaya-ipoffice', 'avaya-aura'):
        item = catalog[preset]
        assert item['auth_modes'] == ['ip'] and item['transports'] == ['udp', 'tcp']
        assert item['admin_steps'] and item['codecs_by_country'] and item['dial_formats'] == ['e164', 'local']
        assert any('has not been tested' in note for note in item['notes'])
    assert catalog['bt-one-voice']['audio_by_default'] is True
    assert catalog['telstra-sip-connect']['auth_modes'] == ['registration']
    assert catalog['telstra-sip-connect']['transport'] == 'tcp'
    for preset in ('gamma', 'bt-one-voice', 'telstra-sip-connect'):
        assert any('has not been tested' in note for note in catalog[preset]['notes'])
        assert catalog[preset]['kind'] == 'carrier' and not catalog[preset]['admin_steps']


def test_presets_without_a_published_price_get_no_card_so_spending_asks_for_your_rate():
    """Null prices are never seeded, so Spending says "No published price; add your rate" for these trunks."""
    from app.routing.carriers import carrier_label
    from app.routing.seed import cards_in_use, load_cards
    shipped = load_cards()
    for preset in ('gamma', 'bt-one-voice', 'telstra-sip-connect', 'avaya-ipoffice', 'avaya-aura'):
        installation = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': preset})
        assert [card for card in cards_in_use(installation, shipped) if card.provider_id.startswith('sip-')] == []
    telnyx = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telnyx'})
    assert {card.provider_id for card in cards_in_use(telnyx, shipped)} == {'sip-telnyx'}
    # Spending names the trunk's carrier or phone system in plain words, never by its preset id.
    assert [carrier_label(preset) for preset in ('avaya-ipoffice', 'bt-one-voice', 'telstra-sip-connect',
                                                 'flowroute', 'telnyx')] == [
        'Avaya IP Office', 'BT One Voice', 'Telstra SIP Connect', 'Flowroute', 'Telnyx']
