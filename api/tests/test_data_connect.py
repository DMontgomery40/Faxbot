"""NTT Hikari Denwa and Data Connect, from NTT's own published documents (brief 92, RF item 4)."""
from decimal import Decimal

import pytest

from app import sip_trunk
from app.config_values import ConfigurationValues
from app.routing import data_connect
from app.routing.seed import load_cards


@pytest.mark.parametrize('seconds, kbps, yen', [
    (0, 64, '0'), (1, 64, '1.1'), (30, 64, '1.1'), (30.001, 64, '2.2'), (31, 64, '2.2'), (60, 64, '2.2'),
    (61, 64, '3.3'),
    (30, 65, '1.65'), (31, 512, '3.30'), (90, 512, '4.95'),
    (30, 513, '2.2'), (31, 1000, '4.4'),
    (180, 1001, '16.5'), (181, 2600, '33.0'), (180, 2601, '110'), (360, 9000, '220'),
])
def test_data_connect_counts_each_started_unit_at_its_bandwidth_tier(seconds, kbps, yen):
    assert data_connect.charge(seconds, kbps) == Decimal(yen)


def test_the_tiers_are_ntts_published_prices_and_a_bandwidth_must_be_given():
    assert [(tier.up_to_kbps, str(tier.yen), tier.seconds) for tier in data_connect.TIERS] == [
        (64, '1.1', 30), (512, '1.65', 30), (1000, '2.2', 30)]
    assert [(tier.up_to_kbps, str(tier.yen), tier.seconds) for tier in data_connect.TOGETHER] == [
        (2600, '16.5', 180), (None, '110', 180)]
    with pytest.raises(ValueError):
        data_connect.tier(0)
    with pytest.raises(ValueError):
        data_connect.charge(-1, 64)
    # A faster tier can cost less for the same transfer (NTT's own prices; the research's illustration).
    assert data_connect.charge(62.5, 64) > data_connect.charge(7.8125, 512)


def test_an_ordinary_call_is_a_voice_call_and_the_rate_card_never_prices_it_under_ntts_rate():
    assert data_connect.voice_charge(1) == Decimal('8.8') and data_connect.voice_charge(181) == Decimal('17.6')
    cards = {(card.provider_id, card.direction): card for card in load_cards()}
    out = cards[('sip-ntt-hikari', 'outbound')]
    assert out.currency == 'JPY' and out.billing_increment_seconds == 180 and out.minimum_seconds == 180
    # Three minutes on the card cost at least NTT's ¥8.8, and less than a yen-thousandth more.
    three_minutes = out.per_minute_micros * 3
    assert 8_800_000 <= three_minutes < 8_800_010
    assert cards[('sip-ntt-hikari', 'inbound')].per_minute_micros == 0


def test_the_preset_says_what_ntt_documents_and_offers_t38_as_the_standard_describes():
    preset = sip_trunk.PRESETS['ntt-hikari']
    assert preset.codecs == ('ulaw',) and preset.auth_modes == ('registration',) and preset.host == ''
    assert {source.url for source in preset.sources} >= {data_connect.EAST_INTERFACE, data_connect.SERVICE_PAGE,
                                                        data_connect.EAST_PRICES, data_connect.WEST_MANUAL}
    assert 'confirm the T.38 settings with NTT before use' in preset.t38
    assert preset.notes[-1] == 'Faxbot has not yet run against NTT.'
    assert all(note.endswith('.') for note in preset.notes)
    assert dict(data_connect.T38_SDP) == {'m=image': 'udptl t38', 'T38FaxVersion': '0', 'T38MaxBitRate': '14400',
                                          'T38FaxRateManagement': 'transferredTCF', 'T38FaxUdpEC': 't38UDPRedundancy'}
    rendered = sip_trunk.render_pjsip(ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'ntt-hikari', 'SIP_TRUNK_HOST': '192.0.2.60', 'SIP_TRUNK_USERNAME': '0312345678',
        'SIP_TRUNK_PASSWORD': 'synthetic-Trunk-Pass!42', 'FAX_DEFAULT_COUNTRY': 'JP'}))
    assert 't38_udptl=yes' in rendered and 't38_udptl_ec=redundancy' in rendered and 'allow=ulaw' in rendered


def test_numbers_are_dialed_in_their_national_form():
    values = ConfigurationValues.from_environment({
        'SIP_TRUNK_PRESET': 'ntt-hikari', 'SIP_TRUNK_HOST': '192.0.2.60', 'SIP_TRUNK_USERNAME': '0312345678',
        'SIP_TRUNK_PASSWORD': 'synthetic-Trunk-Pass!42', 'FAX_DEFAULT_COUNTRY': 'JP'})
    trunk = sip_trunk.effective_trunk(values)
    assert sip_trunk.dial_number(trunk, '+81312345678') == '0312345678'
