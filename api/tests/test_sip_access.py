"""Encrypted audio fax trunks (N18): Swisscom Smart Business Connect and Telekom CompanyFlex.

Asterisk's file is checked as rendered (TLS, media_encryption=sdes, no T.38); the CompanyFlex access rule runs
through the real network check with the STUN probe and host discovery given synthetic answers. Not yet run against a
live Swisscom or Telekom trunk; the real Asterisk image loading this file is the native proof below.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from app import sip_access, sip_fax_mode, sip_network, sip_trunk
from app.config_profiles import ConfigurationDocument
from app.config_values import ConfigurationValues
from api.tests.test_sip_network import (ADMIN, COLIMA_BRIDGED, _check, _client, _t38, _values, network,  # noqa: F401
                                        probe)


SWISSCOM = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'swisscom-sbc', 'SIP_TRUNK_AUTH': 'registration',
            'SIP_TRUNK_HOST': 'zhheapp-asbc01.join.swisscom.ch', 'SIP_TRUNK_USERNAME': 'synthetic41',
            'SIP_TRUNK_PASSWORD': 'synthetic-Pass!41', 'SIP_TRUNK_CALLER_ID': '+41445550100',
            'FAX_DEFAULT_COUNTRY': 'CH'}
COMPANYFLEX = {'FAX_BACKEND': 'sip', 'SIP_TRUNK_PRESET': 'telekom-companyflex', 'SIP_TRUNK_AUTH': 'registration',
               'SIP_TRUNK_USERNAME': 'synthetic49', 'SIP_TRUNK_PASSWORD': 'synthetic-Pass!49',
               'SIP_TRUNK_OUTBOUND_PROXY': 'k0001.primary.companyflex.de', 'SIP_TRUNK_CALLER_ID': '+493055500100',
               'SIP_TRUNK_TRANSPORT': 'tcp', 'FAX_DEFAULT_COUNTRY': 'DE'}
TELEKOM_LINE, OTHER_LINE = '198.51.100.7', '203.0.113.50'


def values(base, tmp_path, **extra):
    return ConfigurationValues.from_environment({**base, 'FAX_DATA_DIR': str(tmp_path), **extra})


def endpoint(text):
    section = text.split('[trunk-endpoint]', 1)[1].split('\n\n', 1)[0]
    return [line for line in section.splitlines() if line]


def test_swisscom_is_tls_with_encrypted_audio_g711_only_and_t38_off_whatever_the_switch(tmp_path):
    settings = values(SWISSCOM, tmp_path, SIP_T38_ENABLED='true', SIP_FAX_ECM='false')
    trunk = sip_trunk.effective_trunk(settings)
    assert (trunk.transport, trunk.port, trunk.media_encryption, trunk.t38) == ('tls', 5061, 'sdes', False)
    text = sip_trunk.render_pjsip(settings)
    lines = endpoint(text)
    assert 'media_encryption=sdes' in lines and 'media_encryption_optimistic=no' in lines
    assert 't38_udptl=no' in lines and 'allow=alaw,ulaw' in lines and 'transport=transport-tls' in lines
    assert 'direct_media=no' in lines  # the audio path never moves during a call, as Swisscom requires
    assert 'protocol=tls' in text and 'verify_server=yes' in text
    # Error correction is on for encrypted audio fax, and audio fax never goes above 9,600 bit/s.
    options = sip_trunk.fax_options(settings)
    assert options.ecm is True and options.rate_for(t38=False) == 9600
    preset = sip_trunk.PRESETS['swisscom-sbc']
    assert preset.transports == ('tls',) and preset.audio_by_default and preset.single_registration
    assert sip_access.sentence(settings).startswith('Swisscom Smart Business Connect accepts only encrypted sign-in')
    assert sip_fax_mode.off_sentence(sip_fax_mode.ENCRYPTED, carrier='Swisscom Smart Business Connect') == (
        'Off: calls over Swisscom Smart Business Connect are encrypted here, and fax over IP (T.38) cannot be '
        'encrypted, so Faxbot sends encrypted audio fax.')
    # The network never turns T.38 on for an encrypted trunk.
    assert sip_fax_mode.network_decision(values(SWISSCOM, tmp_path, SIP_T38_ENABLED='false'), 'open') is None


def test_a_second_trunk_on_the_same_swisscom_account_is_not_loaded(tmp_path):
    settings = values(SWISSCOM, tmp_path).with_provider_accounts(ConfigurationDocument({
        'swisscom-2': {'provider': 'sip', 'label': 'Second Swisscom trunk',
                       'settings': {'preset': 'swisscom-sbc', 'auth': 'registration',
                                    'host': 'zhheapp-asbc01.join.swisscom.ch', 'username': 'synthetic41'},
                       'credentials': {'password': 'synthetic-Pass!41'}},
        'swisscom-3': {'provider': 'sip', 'label': 'Its own Swisscom account',
                       'settings': {'preset': 'swisscom-sbc', 'auth': 'registration',
                                    'host': 'zhheapp-asbc01.join.swisscom.ch', 'username': 'synthetic42'},
                       'credentials': {'password': 'synthetic-Pass!42'}}}))
    problems = sip_trunk.trunk_problems(settings)
    assert problems == {'swisscom-2': 'Second Swisscom trunk is not loaded: Swisscom Smart Business Connect allows '
                                      'one registration per account, and another trunk already signs in with it.'}
    assert sip_trunk.rendered_endpoints(settings) == ('trunk-endpoint', 'trunk-swisscom-3-endpoint')


def _check_record(settings, address):
    sip_network.write_check(settings, {'t38': 'open', 'internet_address': address, 'checked_at': 0})


def test_companyflex_is_encrypted_on_any_access_but_the_telekom_line_you_listed(tmp_path):
    unknown = values(COMPANYFLEX, tmp_path)
    assert sip_access.access(unknown) == sip_access.UNKNOWN and sip_access.encryption_required(unknown)
    trunk = sip_trunk.effective_trunk(unknown)
    assert (trunk.transport, trunk.port, trunk.media_encryption, trunk.t38) == ('tls', 5061, 'sdes', False)
    assert 'has not seen your Telekom line yet' in sip_access.sentence(unknown)
    listed = values(COMPANYFLEX, tmp_path, SIP_TRUNK_OWN_ACCESS=f'{TELEKOM_LINE}, 2001:db8::/32')
    _check_record(listed, OTHER_LINE)
    assert sip_access.access(listed) == sip_access.OTHER
    assert 'media_encryption=sdes' in endpoint(sip_trunk.render_pjsip(listed))
    _check_record(listed, TELEKOM_LINE)
    assert sip_access.access(listed) == sip_access.OWN and not sip_access.encryption_required(listed)
    lines = endpoint(sip_trunk.render_pjsip(listed))
    assert 'transport=transport-tcp' in lines and not any(line.startswith('media_encryption') for line in lines)
    assert 't38_udptl=yes' in lines
    assert sip_access.sentence(listed).startswith('Faxbot is on your Telekom line, so CompanyFlex allows')
    # On the Telekom line over TLS, Telekom asks for SRTP: still encrypted audio, and the sentence says how to get T.38.
    over_tls = values(COMPANYFLEX, tmp_path, SIP_TRUNK_OWN_ACCESS=TELEKOM_LINE, SIP_TRUNK_TRANSPORT='tls')
    assert sip_trunk.effective_trunk(over_tls).media_encryption == 'sdes'
    assert 'choose TCP to allow fax over IP' in sip_access.sentence(over_tls)
    # A range and an IPv6 line count; unreadable entries are left out, never matched.
    assert [str(item) for item in sip_access.own_networks(values(COMPANYFLEX, tmp_path,
                                                                  SIP_TRUNK_OWN_ACCESS='198.51.100.0/24, 2001:db8::1, 999.1.1.1'))] == [
        '198.51.100.0/24', '2001:db8::1/128']


def test_the_access_rule_records_each_change_and_decides_t38_only_for_its_own_decision(tmp_path):
    settings = values(COMPANYFLEX, tmp_path, SIP_TRUNK_OWN_ACCESS=TELEKOM_LINE)
    first = sip_access.requalify(settings, {'internet_address': TELEKOM_LINE})
    assert first == {'from': None, 'to': sip_access.OWN, 'required': False}
    assert sip_access.requalify(settings, {'internet_address': TELEKOM_LINE}) is None  # nothing changed
    moved = sip_access.requalify(settings, {'internet_address': OTHER_LINE})
    assert moved == {'from': sip_access.OWN, 'to': sip_access.OTHER, 'required': True}
    assert sip_access.read_record(settings)['address'] == OTHER_LINE
    assert sip_fax_mode.access_decision(settings, moved) == 'audio'
    off = settings.with_patch({'sip_t38_enabled': False})
    back = {'from': sip_access.OTHER, 'to': sip_access.OWN, 'required': False}
    assert sip_fax_mode.access_decision(off, back) is None  # a person's choice of audio fax is left alone
    sip_fax_mode.write(off, 'audio', sip_fax_mode.ENCRYPTED)
    assert sip_fax_mode.access_decision(off, back) == 't38'
    assert sip_fax_mode.access_decision(off.with_patch({'sip_trunk_transport': 'tls'}), back) is None
    assert sip_access.requalify(values(SWISSCOM, tmp_path), {'internet_address': OTHER_LINE}) is None


def test_the_network_check_requalifies_companyflex_when_the_access_changes(isolated_installation, monkeypatch,
                                                                            network):  # noqa: F811
    network['row'] = (COLIMA_BRIDGED[0], probe(4002, 4002, 4002, public=TELEKOM_LINE))
    with _client(monkeypatch, {**COMPANYFLEX, 'SIP_TRUNK_OWN_ACCESS': TELEKOM_LINE}) as client:
        assert _check(client)['access']['to'] == sip_access.OWN and _t38(client) is True
        trunk = client.get('/admin/settings', headers=ADMIN).json()['sip']['trunk']
        assert (trunk['access'], trunk['media_encryption'], trunk['own_access']) == ('own', None, TELEKOM_LINE)
        # The router fails over to another provider's line: encrypted calls, audio fax, with the reason.
        network['row'] = (COLIMA_BRIDGED[0], probe(4002, 4002, 4002, public=OTHER_LINE))
        moved = _check(client)
        assert moved['access']['required'] is True and moved['switched'] == 'audio' and _t38(client) is False
        assert sip_fax_mode.read(_values(client))['reason'] == sip_fax_mode.ENCRYPTED
        written = Path(_values(client).fax_data_dir, 'asterisk', 'pjsip.conf').read_text()
        assert 'media_encryption=sdes' in written and 'transport=transport-tls' in endpoint(written)
        trunk = client.get('/admin/settings', headers=ADMIN).json()['sip']['trunk']
        assert (trunk['t38_off_reason'], trunk['media_encryption'], trunk['access']) == ('encrypted', 'sdes', 'other')
        # Back on the Telekom line: T.38 again, because turning it off was the rule's own decision.
        network['row'] = (COLIMA_BRIDGED[0], probe(4002, 4002, 4002, public=TELEKOM_LINE))
        back = _check(client)
        assert back['switched'] == 't38' and _t38(client) is True
        written = Path(_values(client).fax_data_dir, 'asterisk', 'pjsip.conf').read_text()
        assert 'media_encryption=sdes' not in written and 'transport=transport-tcp' in endpoint(written)


NATIVE_IMAGE = os.environ.get('FAXBOT_SRTP_IMAGE')


@pytest.mark.native
@pytest.mark.skipif(not NATIVE_IMAGE, reason='Set FAXBOT_SRTP_IMAGE to an Asterisk image built from asterisk/.')
def test_the_real_asterisk_loads_the_encrypted_trunk_with_srtp(tmp_path):
    """The rendered file in the real Asterisk 22 image: res_srtp runs, the endpoint has SDES and no T.38, and the
    log has no error for it. Nothing is dialed; the carrier host is never reached."""
    # A typed internet address: the file needs none of the placeholders the container's start script fills in.
    settings = values(SWISSCOM, tmp_path, SIP_EXTERNAL_ADDRESS='198.51.100.7')
    folder = tmp_path / 'etc'
    folder.mkdir()
    (folder / 'pjsip.conf').write_text(sip_trunk.render_pjsip(settings))
    context = ['docker', '--context', os.environ.get('FAXBOT_DOCKER_CONTEXT', 'colima-faxbot-refresh')]
    name = f'faxbot-co-srtp-{uuid.uuid4().hex[:8]}'
    # Copied in, not mounted: the Docker host (a Colima virtual machine) may not see this computer's temp folder.
    subprocess.run(context + ['create', '--name', name, '--entrypoint', 'asterisk', NATIVE_IMAGE, '-f', '-vvv'],
                   check=True, capture_output=True, text=True)
    try:
        subprocess.run(context + ['cp', str(folder / 'pjsip.conf'), f'{name}:/etc/asterisk/pjsip.conf'], check=True,
                       capture_output=True, text=True)
        subprocess.run(context + ['start', name], check=True, capture_output=True, text=True)
        def cli(command):
            return subprocess.run(context + ['exec', name, 'asterisk', '-rx', command], capture_output=True,
                                  text=True).stdout
        import time
        deadline = time.monotonic() + 60
        while 'res_srtp' not in cli('module show like res_srtp') and time.monotonic() < deadline:
            time.sleep(1)
        assert 'Running' in cli('module show like res_srtp')
        shown = cli('pjsip show endpoint trunk-endpoint')
        assert 'media_encryption' in shown and 'sdes' in shown
        assert 't38_udptl' in shown and 'false' in shown.split('t38_udptl', 1)[1].splitlines()[0]
        logs = subprocess.run(context + ['logs', name], capture_output=True, text=True)
        assert 'Error parsing' not in logs.stdout + logs.stderr and 'trunk-endpoint' not in [
            line for line in (logs.stdout + logs.stderr).splitlines() if 'ERROR' in line]
    finally:
        subprocess.run(context + ['rm', '-f', name], capture_output=True, text=True)
