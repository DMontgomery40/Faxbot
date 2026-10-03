"""SIP trunk presets and the Asterisk PJSIP configuration rendered from them.

Faxbot's own fax engine reaches the telephone network through one carrier SIP
trunk. A preset records what each carrier documents (signaling host, transport,
authentication, signaling addresses, dialed-number format, T.38 notes) with the
page and date each fact was read. ``render_pjsip`` turns the active settings
into a complete ``pjsip.conf``; ``write_asterisk_configuration`` stores it where
the Asterisk container reads it at start (``<FAX_DATA_DIR>/asterisk/pjsip.conf``).

Rendering never logs. The SIP password appears only in the returned text and in
the private file written with mode 0600; errors name fields, never values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import tempfile


READ_ON = '2026-10-03'
ENDPOINT = 'trunk-endpoint'
INBOUND_CONTEXT = 'faxbot-inbound'
FAX_PREFERENCE = '*;+sip.fax="t38"'


class TrunkConfigurationError(ValueError):
    """The trunk cannot be rendered; ``fields`` names what to fix, never values."""

    def __init__(self, fields):
        self.fields = tuple(fields)
        super().__init__('SIP trunk settings are incomplete: ' + ', '.join(self.fields))


@dataclass(frozen=True)
class Source:
    url: str
    read_on: str = READ_ON


@dataclass(frozen=True)
class TrunkPreset:
    id: str
    label: str
    host: str
    port: int
    transport: str
    auth_modes: tuple[str, ...]
    codecs: tuple[str, ...]
    # 'e164' sends +<country><number>; 'digits' sends the same digits without
    # the plus (Flowroute documents 1NPANXXXXXX); 'entered' leaves the number
    # exactly as the sender typed it.
    dial_format: str
    # Carrier signaling addresses for the identify section. Empty means the
    # carrier publishes none we could verify, so Faxbot matches the host name.
    signaling_addresses: tuple[str, ...] = ()
    # Flowroute requires an account tech prefix before the number when the
    # trunk authenticates by IP address; Faxbot reads it from the username.
    ip_dial_prefix: bool = False
    t38: str = ''
    notes: tuple[str, ...] = ()
    sources: tuple[Source, ...] = field(default_factory=tuple)

    @property
    def needs_host(self):
        return not self.host


PRESETS: dict[str, TrunkPreset] = {preset.id: preset for preset in (
    TrunkPreset(
        id='telnyx', label='Telnyx', host='sip.telnyx.com', port=5060, transport='udp',
        auth_modes=('registration', 'ip'), codecs=('ulaw', 'alaw'), dial_format='e164',
        signaling_addresses=('192.76.120.10', '64.16.250.10'),
        t38=('T.38 is turned on per number with "Enable T.38 Fax Gateway". For calls you receive, '
             'Telnyx expects your server to switch the call to T.38, which Faxbot does.'),
        notes=('Telnyx accepts credentials (registration) or IP address authentication.',
               'The caller ID must be a number on your Telnyx account or one Telnyx has verified.',
               'The US signaling addresses are 192.76.120.10 and 64.16.250.10.',
               'Choose an outbound voice profile for the connection so it can place calls.'),
        sources=(Source('https://sip.telnyx.com/voice.json'),
                 Source('https://sip.telnyx.com/'),
                 Source('https://developers.telnyx.com/docs/voice/sip-trunking/get-started'),
                 Source('https://developers.telnyx.com/docs/voice/sip-trunking/authentication/credential-types'),
                 Source('https://developers.telnyx.com/docs/voice/sip-trunking/configuration/caller-id-policy'),
                 Source('https://support.telnyx.com/en/articles/1130672-fax-service-with-telnyx-via-t-38-or-g711'),
                 Source('https://developers.telnyx.com/docs/voice/sip-trunking/network-configuration/ip-whitelisting'),
                 Source('https://support.telnyx.com/en/articles/4404448-sip-connection-inbound-outbound-settings')),
    ),
    TrunkPreset(
        id='signalwire', label='SignalWire', host='', port=5060, transport='udp',
        auth_modes=('registration',), codecs=('ulaw', 'alaw'), dial_format='e164',
        t38='SignalWire does not document T.38 for SIP endpoints, so test a fax before relying on it.',
        notes=('Enter your space SIP domain, for example example.sip.signalwire.com.',
               'SignalWire does not publish fixed signaling addresses, so Faxbot uses SIP credentials.'),
        sources=(Source('https://signalwire.com/docs/platform/voice/sip/trunking'),
                 Source('https://signalwire.com/docs/platform/voice/sip/bring-your-own-carrier')),
    ),
    TrunkPreset(
        id='sinch', label='Sinch', host='', port=5060, transport='udp',
        auth_modes=('registration', 'ip'), codecs=('ulaw', 'alaw'), dial_format='e164',
        t38=('Sinch does not document T.38 or fax for Elastic SIP Trunking. Ask Sinch to confirm T.38 '
             'on your trunk and send test faxes before relying on it.'),
        notes=('Enter your trunk domain, for example example.pstn.sinch.com.',
               'Sinch asks every outgoing call for the trunk username and password.',
               'For receiving, use a registered SIP endpoint with the same username and password.',
               'Sinch expects called numbers and caller ID in E.164 format with a plus sign.'),
        sources=(Source('https://developers.sinch.com/docs/est/test-plan'),
                 Source('https://developers.sinch.com/docs/est'),
                 Source('https://developers.sinch.com/docs/est/integration-guides/livekit'),
                 Source('https://developers.sinch.com/docs/est/integration-guides/ribbon-sbc'),
                 Source('https://sinch.com/voice/sip-trunking/elastic/')),
    ),
    TrunkPreset(
        id='anveo', label='AnveoDirect', host='sbc.anveo.com', port=5060, transport='udp',
        auth_modes=('ip',), codecs=('ulaw', 'alaw'), dial_format='e164',
        signaling_addresses=('169.48.232.158', '204.216.109.55', '176.9.39.206', '72.9.149.25'),
        t38='AnveoDirect does not state T.38 support on its connection page, so test a fax before relying on it.',
        notes=('AnveoDirect authenticates by IP address and does not support registration.',
               'Add your server public IP address in the AnveoDirect portal.'),
        sources=(Source('https://www.anveodirect.com/about/faq'),),
    ),
    TrunkPreset(
        id='flowroute', label='Flowroute', host='us-west-or.sip.flowroute.com', port=5060, transport='udp',
        auth_modes=('registration', 'ip'), codecs=('ulaw', 'alaw'), dial_format='digits',
        signaling_addresses=('34.210.91.112/28', '34.226.36.32/28'), ip_dial_prefix=True,
        t38='Flowroute says it repairs T.38 incompatibilities between sender and receiver.',
        notes=('Use us-east-va.sip.flowroute.com instead if that location is closer.',
               'With IP authentication, enter your eight-digit tech prefix as the username.',
               'Flowroute expects North American numbers as 1 plus the ten-digit number.'),
        sources=(Source('https://developer.flowroute.com/docs/inbound-and-outbound-calling-with-flowroute-new-pops/'),
                 Source('https://support.bcmone.com/flowroute-support/docs/set-up-ip-based-authentication-for-outbound-calls'),
                 Source('https://flowroute.com/faxing/')),
    ),
    TrunkPreset(
        id='custom', label='Another carrier', host='', port=5060, transport='udp',
        auth_modes=('registration', 'ip'), codecs=('ulaw', 'alaw'), dial_format='entered',
        notes=('Use the host, port and credentials your carrier gave you.',),
    ),
)}

_DEFAULT_PORTS = {'udp': 5060, 'tcp': 5060, 'tls': 5061}
_DIGITS = re.compile(r'\+?[0-9]{3,20}', re.ASCII)


@dataclass(frozen=True)
class Trunk:
    """Effective trunk settings after applying preset defaults."""
    preset: TrunkPreset
    auth: str
    host: str
    port: int
    transport: str
    username: str
    password: str = field(repr=False)
    outbound_proxy: str
    caller_id: str
    dids: tuple[str, ...]
    t38: bool
    fax_preference: bool
    codecs: tuple[str, ...]


def configured(values) -> bool:
    return bool(getattr(values, 'sip_trunk_preset', ''))


def effective_trunk(values, *, for_calls=False) -> Trunk:
    """Apply preset defaults and check completeness; raises with field names only.

    ``for_calls`` also requires the carrier-authorized caller ID, which placing
    a call needs but the Asterisk configuration does not.
    """
    preset = PRESETS.get(values.sip_trunk_preset)
    if preset is None:
        raise TrunkConfigurationError(['sip_trunk_preset'])
    missing = []
    auth = values.sip_trunk_auth
    if auth not in preset.auth_modes:
        missing.append('sip_trunk_auth')
    host = values.sip_trunk_host or preset.host
    if not host:
        missing.append('sip_trunk_host')
    transport = values.sip_trunk_transport or preset.transport
    if auth == 'registration':
        if not values.sip_trunk_username:
            missing.append('sip_trunk_username')
        if not values.sip_trunk_password:
            missing.append('sip_trunk_password')
    elif preset.ip_dial_prefix and not re.fullmatch(r'[0-9]{4,16}', values.sip_trunk_username or ''):
        missing.append('sip_trunk_username')
    if for_calls and not values.sip_trunk_caller_id:
        missing.append('sip_trunk_caller_id')
    if missing:
        raise TrunkConfigurationError(missing)
    port = values.sip_trunk_port or (preset.port if transport == preset.transport else _DEFAULT_PORTS[transport])
    codecs = tuple(values.sip_trunk_codecs.split(',')) if values.sip_trunk_codecs else preset.codecs
    return Trunk(preset=preset, auth=auth, host=host, port=port, transport=transport,
                 username=values.sip_trunk_username, password=values.sip_trunk_password,
                 outbound_proxy=values.sip_trunk_outbound_proxy, caller_id=values.sip_trunk_caller_id,
                 dids=values.sip_trunk_did_list, t38=values.sip_t38_enabled,
                 fax_preference=values.sip_fax_preference_header, codecs=codecs)


def dial_number(trunk: Trunk, number: str) -> str:
    """The Request-URI user part for one destination, in the carrier's format.

    Ten-digit numbers are treated as US or Canadian numbers when a carrier needs
    the country code. Raises ValueError for anything but digits with an optional
    leading plus, before any call is placed.
    """
    if not isinstance(number, str) or _DIGITS.fullmatch(number) is None:
        raise ValueError('Unsupported destination number')
    fmt = trunk.preset.dial_format
    if fmt == 'entered':
        return number
    digits = number.lstrip('+')
    if not number.startswith('+') and len(digits) == 10:
        digits = '1' + digits
    if fmt == 'digits':
        result = digits
        if trunk.auth == 'ip' and trunk.preset.ip_dial_prefix:
            result = trunk.username + '*' + digits
        return result
    return '+' + digits


def _transport_section(trunk: Trunk):
    name = 'transport-' + trunk.transport
    lines = [f'[{name}]', 'type=transport', f'protocol={trunk.transport}']
    if trunk.transport == 'tls':
        lines += ['bind=0.0.0.0:5061', 'method=tlsv1_2',
                  'ca_list_file=/etc/ssl/certs/ca-certificates.crt', 'verify_server=yes']
    else:
        lines.append('bind=0.0.0.0:5060')
    return name, lines


def _uri(trunk: Trunk, user: str = ''):
    # A bare semicolon starts a comment in Asterisk configuration files.
    target = f'{trunk.host}:{trunk.port}'
    suffix = '' if trunk.transport == 'udp' else f'\\;transport={trunk.transport}'
    return f'sip:{user + "@" if user else ""}{target}{suffix}'


def render_pjsip(values) -> str:
    """Complete pjsip.conf text for the active settings; contains the SIP password."""
    trunk = effective_trunk(values)
    preset = trunk.preset
    transport, transport_lines = _transport_section(trunk)
    registration = trunk.auth == 'registration'
    lines = [
        f'; Faxbot SIP trunk for {preset.label}, written by Faxbot from its settings.',
        '; Change the trunk in Faxbot settings; edits to this file are replaced.',
        '[global]', 'type=global', 'user_agent=Faxbot-Asterisk', '',
        *transport_lines, '',
        '[trunk-aor]', 'type=aor', f'contact={_uri(trunk)}', 'qualify_frequency=60', '',
    ]
    if registration:
        lines += ['[trunk-auth]', 'type=auth', 'auth_type=userpass',
                  f'username={trunk.username}', f'password={trunk.password}', '']
    lines += [f'[{ENDPOINT}]', 'type=endpoint', f'transport={transport}', 'aors=trunk-aor']
    if registration:
        lines.append('outbound_auth=trunk-auth')
    lines += [f'context={INBOUND_CONTEXT}', 'disallow=all', 'allow=' + ','.join(trunk.codecs)]
    if trunk.t38:
        lines += ['t38_udptl=yes', 't38_udptl_ec=redundancy', 't38_udptl_maxdatagram=400', 't38_udptl_nat=yes']
    else:
        lines.append('t38_udptl=no')
    lines += ['rtp_symmetric=yes', 'force_rport=yes', 'rewrite_contact=yes', 'direct_media=no',
              'send_pai=yes', f'from_domain={trunk.host}']
    if trunk.outbound_proxy:
        lines.append(f'outbound_proxy=sip:{trunk.outbound_proxy}\\;lr')
    lines.append('')
    lines += ['[trunk-identify]', 'type=identify', f'endpoint={ENDPOINT}']
    lines += [f'match={address}' for address in (preset.signaling_addresses or (trunk.host,))]
    lines.append('')
    if registration:
        lines += ['[trunk-registration]', 'type=registration', f'transport={transport}',
                  'outbound_auth=trunk-auth', f'server_uri={_uri(trunk)}',
                  f'client_uri={_uri(trunk, trunk.username)}', f'contact_user={trunk.username}',
                  'retry_interval=60', 'forbidden_retry_interval=600', 'expiration=300',
                  'line=yes', f'endpoint={ENDPOINT}']
        if trunk.outbound_proxy:
            lines.append(f'outbound_proxy=sip:{trunk.outbound_proxy}\\;lr')
        lines.append('')
    return '\n'.join(lines)


def configuration_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'pjsip.conf'


def write_asterisk_configuration(values) -> Path:
    """Atomically write the private pjsip.conf the Asterisk container loads at start."""
    text = render_pjsip(values)
    target = configuration_path(values)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.pjsip.', dir=target.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target


def preset_catalog():
    """Public preset facts for the console; never includes settings or secrets."""
    return [{
        'id': preset.id, 'label': preset.label, 'host': preset.host, 'port': preset.port,
        'transport': preset.transport, 'auth_modes': list(preset.auth_modes),
        'codecs': list(preset.codecs), 'needs_host': preset.needs_host,
        'ip_dial_prefix': preset.ip_dial_prefix, 't38': preset.t38, 'notes': list(preset.notes),
        'sources': [{'url': source.url, 'read_on': source.read_on} for source in preset.sources],
    } for preset in PRESETS.values()]


def main(argv=None):
    """``python -m app.sip_trunk write``: render from this process's settings."""
    import sys
    from .config_values import ConfigurationValues, ConfigurationValueError
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments != ['write']:
        print('Usage: python -m app.sip_trunk write', file=sys.stderr)
        return 2
    try:
        values = ConfigurationValues.from_environment(os.environ)
        path = write_asterisk_configuration(values)
    except (TrunkConfigurationError, ConfigurationValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f'Wrote {path}. Restart the Asterisk service to use it.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
