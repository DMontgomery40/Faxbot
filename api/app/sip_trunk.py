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
    # the plus (Flowroute documents 1NPANXXXXXX); 'entered' sends the number
    # the sender entered, which Faxbot has resolved to E.164 at acceptance.
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
        # Encrypted signaling by default: one outbound connection that home-router
        # SIP helpers cannot rewrite, and Telnyx sends incoming calls down it.
        id='telnyx', label='Telnyx', host='sip.telnyx.com', port=5061, transport='tls',
        auth_modes=('registration', 'ip'), codecs=('ulaw', 'alaw'), dial_format='e164',
        signaling_addresses=('192.76.120.10', '64.16.250.10'),
        t38=('In the Telnyx portal, turn on "Enable T.38 Fax Gateway" for each number, and set the connection '
             'option "T.38 fax re-invite initiated by" to Telnyx. For faxes you send, Telnyx then switches the '
             'call to T.38 as soon as the receiving machine answers. Faxbot also works with Customer, but then '
             'Faxbot waits about ten seconds before switching the call itself. For faxes you receive, Faxbot '
             'switches the call to T.38 whichever option you choose.'),
        notes=('Telnyx accepts credentials (registration) or IP address authentication.',
               'Faxbot connects to Telnyx over an encrypted connection by default. In the Telnyx portal, set the '
               'connection\'s inbound SIP transport to TLS as well.',
               'The caller ID must be a number on your Telnyx account or one Telnyx has verified.',
               'The US signaling addresses are 192.76.120.10 and 64.16.250.10.',
               'Choose an outbound voice profile for the connection so it can place calls.',
               'Keep only the G.711 U and G.711 A codecs on the connection.'),
        # The first source is the page the console links as the carrier's documentation.
        sources=(Source('https://developers.telnyx.com/docs/voice/sip-trunking/get-started'),
                 Source('https://sip.telnyx.com/voice.json'),
                 Source('https://sip.telnyx.com/'),
                 Source('https://developers.telnyx.com/docs/voice/sip-trunking/authentication/credential-types'),
                 Source('https://developers.telnyx.com/docs/voice/sip-trunking/configuration/caller-id-policy'),
                 Source('https://support.telnyx.com/en/articles/1130672-fax-service-with-telnyx-via-t-38-or-g711'),
                 Source('https://developers.telnyx.com/docs/voice/sip-trunking/network-configuration/ip-whitelisting'),
                 Source('https://support.telnyx.com/en/articles/4404448-sip-connection-inbound-outbound-settings')),
    ),
    TrunkPreset(
        id='signalwire', label='SignalWire', host='', port=5060, transport='udp',
        auth_modes=('registration',), codecs=('ulaw', 'alaw'), dial_format='e164',
        t38='T.38 is not documented by the carrier. Confirm it with SignalWire support and send test faxes first.',
        notes=('Enter your space SIP domain, for example example.sip.signalwire.com.',
               'SignalWire does not publish fixed signaling addresses, so Faxbot uses SIP credentials.'),
        sources=(Source('https://signalwire.com/docs/platform/voice/sip/trunking'),
                 Source('https://signalwire.com/docs/platform/voice/sip/bring-your-own-carrier')),
    ),
    TrunkPreset(
        id='sinch', label='Sinch', host='', port=5060, transport='udp',
        auth_modes=('registration',), codecs=('ulaw', 'alaw'), dial_format='e164',
        t38='T.38 is not documented by the carrier. Confirm it with Sinch support and send test faxes first.',
        notes=('Enter your trunk domain, for example example.pstn.sinch.com.',
               'Sinch asks every outgoing call for the trunk username and password.',
               'For receiving, use a registered SIP endpoint with the same username and password.',
               'Faxbot signs in to Sinch with a username and password, because Sinch does not publish '
               'the addresses it sends calls from on a page Faxbot could verify.',
               'Sinch expects called numbers and caller ID in E.164 format with a plus sign.'),
        sources=(Source('https://developers.sinch.com/docs/est'),
                 Source('https://developers.sinch.com/docs/est/test-plan'),
                 Source('https://developers.sinch.com/docs/est/integration-guides/livekit'),
                 Source('https://developers.sinch.com/docs/est/integration-guides/ribbon-sbc'),
                 Source('https://sinch.com/voice/sip-trunking/elastic/')),
    ),
    TrunkPreset(
        id='anveo', label='AnveoDirect', host='sbc.anveo.com', port=5060, transport='udp',
        auth_modes=('ip',), codecs=('ulaw', 'alaw'), dial_format='e164',
        signaling_addresses=('169.48.232.158', '204.216.109.55', '176.9.39.206', '72.9.149.25'),
        t38='T.38 is not documented on the carrier\'s connection page. Confirm it with AnveoDirect and send test faxes first.',
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
# Placeholders asterisk/bin/faxbot-public-address replaces at every Asterisk start.
PUBLIC_ADDRESS = '@FAXBOT_PUBLIC_ADDRESS@'
LOCAL_NET = '@FAXBOT_LOCAL_NET@'
PRIVATE_NETWORKS = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8')
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
    external_address: str = ''


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
                 fax_preference=values.sip_fax_preference_header, codecs=codecs,
                 external_address=values.sip_external_address)


def dial_number(trunk: Trunk, number: str) -> str:
    """The Request-URI user part for one canonical E.164 destination, in the carrier's format.

    Destinations reach this point already resolved for the installation country,
    so nothing here guesses a country. Raises ValueError for anything but a
    canonical number, before any call is placed.
    """
    from .routing.numbers import canonical_number
    canonical = canonical_number(number)
    if trunk.preset.dial_format != 'digits':
        # 'e164' and 'entered' both send the canonical number with its plus sign.
        return canonical
    digits = canonical[1:]
    if trunk.auth == 'ip' and trunk.preset.ip_dial_prefix:
        return trunk.username + '*' + digits
    return digits


def _transport_section(trunk: Trunk):
    name = 'transport-' + trunk.transport
    lines = [f'[{name}]', 'type=transport', f'protocol={trunk.transport}']
    if trunk.transport == 'tls':
        lines += ['bind=0.0.0.0:5061', 'method=tlsv1_2',
                  'ca_list_file=/etc/ssl/certs/ca-certificates.crt', 'verify_server=yes']
    else:
        lines.append('bind=0.0.0.0:5060')
    if trunk.transport != 'udp':
        # Keep the one outbound connection (and every NAT mapping on its way)
        # alive, and notice quickly when a router drops it silently.
        lines += ['tcp_keepalive_enable=yes', 'tcp_keepalive_idle_time=30',
                  'tcp_keepalive_interval_time=10', 'tcp_keepalive_probe_count=3']
    if trunk.external_address:
        # Behind NAT, advertise the public address to the carrier; private
        # networks (including Docker's) keep their own addresses.
        lines += [f'external_media_address={trunk.external_address}',
                  f'external_signaling_address={trunk.external_address}',
                  *(f'local_net={network}' for network in PRIVATE_NETWORKS)]
    else:
        # Nobody typed an address: the Asterisk container fills these in at
        # start from what Faxbot's STUN probe found (only on a network that
        # keeps port numbers, with its own subnet as local_net), or removes them.
        lines += [f'external_media_address={PUBLIC_ADDRESS}', f'external_signaling_address={PUBLIC_ADDRESS}',
                  f'local_net={LOCAL_NET}']
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
    udp = trunk.transport == 'udp'
    lines = [
        f'; Faxbot SIP trunk for {preset.label}, written by Faxbot from its settings.',
        '; Change the trunk in Faxbot settings; edits to this file are replaced.',
        # Every flow starts from Faxbot's side and is kept alive from it, so no
        # router port has to be opened: keepalives on the TCP/TLS connection,
        # carrier checks every 25 s on UDP (under common 30 s NAT timeouts).
        '[global]', 'type=global', 'user_agent=Faxbot-Asterisk', 'keep_alive_interval=30', '',
        *transport_lines, '',
        '[trunk-aor]', 'type=aor', f'contact={_uri(trunk)}', f'qualify_frequency={25 if udp else 30}',
        'qualify_timeout=3.0', '',
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
    # Answer media where the carrier's packets come from, reuse the signaling
    # connection for requests within a call, and send a packet every two
    # seconds when no audio flows so the router keeps the media path open.
    lines += ['rtp_symmetric=yes', 'force_rport=yes', 'rewrite_contact=yes', 'direct_media=no',
              'rtp_keepalive=2', 'send_pai=yes', f'from_domain={trunk.host}']
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
                  # Re-register often enough to refresh a UDP mapping; never give up
                  # on an unattended fax server (max_retries=0 would mean no retries).
                  f'expiration={120 if udp else 300}', 'retry_interval=60', 'forbidden_retry_interval=600',
                  'fatal_retry_interval=120', 'max_retries=10000', 'auth_rejection_permanent=no',
                  'line=yes', f'endpoint={ENDPOINT}']
        if trunk.outbound_proxy:
            lines.append(f'outbound_proxy=sip:{trunk.outbound_proxy}\\;lr')
        lines.append('')
    return '\n'.join(lines)


def configuration_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'pjsip.conf'


def secret_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'inbound.secret'


def public_address_path(values) -> Path:
    """What Faxbot's STUN probe found, read by the Asterisk container at start."""
    return Path(values.fax_data_dir) / 'asterisk' / 'public-address'


def write_public_address(values, probe) -> bool:
    """Record the probe for the next Asterisk start; True when the advertised address would change.

    Asterisk advertises the address only when the network keeps port numbers,
    so a probe without that is recorded with ``ports_preserved`` false.
    """
    import json
    path = public_address_path(values)
    record = {'ip': probe.public_ip if probe else None,
              'ports_preserved': bool(probe and probe.public_ip and probe.ports == 'preserved'),
              'probed_at': int(probe.probed_at) if probe else None}
    before = read_public_address(values)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(path, json.dumps(record) + '\n')
    advertised = lambda item: item.get('ip') if item and item.get('ports_preserved') else None  # noqa: E731
    return advertised(before) != advertised(record)


def read_public_address(values):
    """The recorded probe, or None."""
    import json
    try:
        return json.loads(public_address_path(values).read_text())
    except (OSError, ValueError):
        return None


def applied_public_address(values):
    """The address Asterisk advertised when it last started: an address, '' for none, None if unknown."""
    try:
        return (public_address_path(values).with_name('public-address.applied')).read_text().strip()
    except OSError:
        return None


def write_asterisk_configuration(values) -> Path:
    """Atomically write the private files the Asterisk container reads.

    ``pjsip.conf`` is loaded when Asterisk starts. ``inbound.secret`` holds the
    shared secret the inbound dialplan sends with each received fax, so a
    secret set in the console reaches Asterisk too; it is removed when unset.
    """
    text = render_pjsip(values)
    target = configuration_path(values)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(target, text)
    secret = secret_path(values)
    if values.asterisk_inbound_secret:
        _write_private(secret, values.asterisk_inbound_secret)
    else:
        try:
            secret.unlink()
        except FileNotFoundError:
            pass
    return target


def _write_private(target: Path, text: str):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
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
    """``python -m app.sip_trunk write`` renders from this process's settings;
    ``probe`` prints what STUN shows about this network as JSON and one sentence."""
    import json
    import sys
    from .config_values import ConfigurationValues, ConfigurationValueError
    from . import stun
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ['probe']:
        preset = os.environ.get('SIP_TRUNK_PRESET', '')
        result = stun.probe(stun.servers_for(preset))
        label = PRESETS[preset].label if preset in PRESETS and preset != 'custom' else 'the carrier'
        print(json.dumps({**result.as_dict(), 'text': stun.address_sentence(result, carrier=label)}, indent=2))
        return 0
    if arguments != ['write']:
        print('Usage: python -m app.sip_trunk write|probe', file=sys.stderr)
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
