"""SIP trunk presets and the Asterisk PJSIP configuration rendered from them.

Faxbot's own fax engine reaches the telephone network through one carrier SIP
trunk. A preset records what each carrier documents (signaling host, transport,
authentication, signaling addresses, dialed-number format, T.38 notes) with the
page and date each fact was read. ``render_pjsip`` turns the active settings
into a complete ``pjsip.conf``; ``write_asterisk_configuration`` stores it where
the Asterisk container reads it at start (``<FAX_DATA_DIR>/asterisk/pjsip.conf``).

A preset is either a carrier or a phone system (an office PBX such as Avaya IP
Office or Aura). With a phone system, Faxbot's one trunk goes to the PBX on the
local network, which keeps its own carrier lines; the PBX and Faxbot recognise
each other by address, and Asterisk tells the PBX the address Faxbot is
published on (``docker-compose.phone-system.yml``) instead of its internet
address.

Rendering never logs. The SIP password appears only in the returned text and in
the private file written with mode 0600; errors name fields, never values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import re
import tempfile


READ_ON = '2026-10-03'
ENDPOINT = 'trunk-endpoint'
INBOUND_CONTEXT = 'faxbot-inbound'
FAX_PREFERENCE = '*;+sip.fax="t38"'
CARRIER = 'carrier'
PHONE_SYSTEM = 'phone_system'


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
    # CARRIER, or PHONE_SYSTEM for a PBX on the local network that keeps the carrier lines.
    kind: str = CARRIER
    # Signaling transports a person may choose; ``transport`` is the default.
    transports: tuple[str, ...] = ('tls', 'tcp', 'udp')
    # G.711 order from the installation country: A-law first, mu-law first in North America and Japan.
    codecs_by_country: bool = False
    # Number formats a person may choose (SIP_TRUNK_DIAL_FORMAT); empty means no choice.
    dial_formats: tuple[str, ...] = ()
    # New trunks start with audio fax because the carrier turns T.38 into audio itself (sip_fax_mode.CARRIER).
    audio_by_default: bool = False
    # What the phone system's administrator sets, in order (shown as a checklist with the sources).
    admin_steps: tuple[str, ...] = ()
    # Encrypted audio fax (sip_access.py, N18): 'sdes' encrypts the audio (SRTP, keys in the TLS-protected SDP)
    # whenever the trunk signs in over TLS.
    media_encryption: str = ''
    # The carrier carries fax only as encrypted audio: TLS and SRTP always, G.711, T.38 off with the reason.
    encrypted_audio_only: bool = False
    # The carrier allows one registration per account: a second trunk on the same account is not loaded.
    single_registration: bool = False
    # The trunk's media depends on the internet access it is reached over ('telekom': CompanyFlex).
    access_rule: str = ''

    @property
    def needs_host(self):
        return not self.host

    @property
    def phone_system(self):
        return self.kind == PHONE_SYSTEM


PRESETS: dict[str, TrunkPreset] = {preset.id: preset for preset in (
    TrunkPreset(
        # Encrypted signaling by default: one outbound connection that home-router
        # SIP helpers cannot rewrite, and Telnyx sends incoming calls down it.
        id='telnyx', label='Telnyx', host='sip.telnyx.com', port=5061, transport='tls',
        auth_modes=('registration', 'ip'), codecs=('ulaw', 'alaw'), dial_format='e164',
        signaling_addresses=('192.76.120.10', '64.16.250.10'),
        t38=('In the Telnyx portal, turn on "Enable T.38 Fax Gateway" for each number and set '
             '"T.38 fax re-invite initiated by" to Telnyx.'),
        notes=('Use a credential connection, and enter the SIP connection\'s password (connection → Authentication '
               'and routing), not your Telnyx account password.',
               'Telnyx sends incoming calls down the encrypted connection Faxbot registers over; there is no '
               'inbound transport to set for a credential connection.',
               'The caller ID must be a number on your Telnyx account or one Telnyx has verified.',
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
        # Encrypted signaling by default, as for Telnyx: SignalWire's trunking page (the first source, read
        # again 2026-10-05) gives devices that register "SIP Server Port: 5061" and "Transport Protocol: TLS".
        # Not yet tried with a live SignalWire trunk.
        id='signalwire', label='SignalWire', host='', port=5061, transport='tls',
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
        # UK. Gamma is sold through resellers; it recognises the PBX or fax server by its public address.
        id='gamma', label='Gamma', host='', port=5060, transport='udp', transports=('udp', 'tcp'),
        auth_modes=('ip',), codecs=('alaw', 'ulaw'), dial_format='e164', dial_formats=('e164', 'local'),
        t38='Gamma lists T.38 for fax, and a phone system maker tested T.38 fax over Gamma in June 2024.',
        notes=('Gamma recognises Faxbot by your static public IP address; give it to your Gamma reseller.',
               'Enter the SIP server address and number format your reseller gives you.',
               'Keep only G.711 on the trunk: G.729 breaks fax.',
               'Faxbot tested these settings against its own Asterisk standing in for the carrier; '
               'a live Gamma trunk has not been tested yet.'),
        sources=(Source('https://service.swyx.net/hc/en-gb/articles/360010513919-SIP-Provider-Gamma-Telecom-UK'),
                 Source('https://www.yeastar.com/itsp-partners/united-kingdom/')),
    ),
    TrunkPreset(
        # UK, also sold in Australia and the US. IP peering; BT transcodes T.38 to G.711 pass-through.
        id='bt-one-voice', label='BT One Voice', host='', port=5060, transport='udp', transports=('udp', 'tcp'),
        auth_modes=('ip',), codecs=('alaw', 'ulaw'), codecs_by_country=True, dial_format='e164',
        dial_formats=('e164', 'local'), audio_by_default=True,
        t38='BT accepts T.38 but turns it into audio fax inside its network, so Faxbot starts with audio fax.',
        notes=('BT recognises Faxbot by its static public IP address and port; give them to BT at turn-up.',
               'Enter the BT SIP server address and number format from your turn-up sheet.',
               'For BT Cloud Voice SIP-T, choose Another carrier and sign in with the username and password '
               'BT gives you.',
               'Faxbot tested these settings against its own Asterisk standing in for the carrier; '
               'a live BT One Voice trunk has not been tested yet.'),
        sources=(Source('https://www.globalservices.bt.com/static/assets/pdf/products/one_voice_sip_trunking/'
                        'One_Voice_SIP_trunking_technical_Outline.pdf'),
                 Source('https://www.globalservices.bt.com/static/assets/pdf/data_sheets/Product/one_voice_sip/'
                        'bt_one_voice_sip_trunk_uk_datasheet.pdf')),
    ),
    TrunkPreset(
        # Australia. Registration over TCP to Telstra's SBC; Telstra states nothing about T.38.
        id='telstra-sip-connect', label='Telstra SIP Connect', host='', port=5060, transport='tcp',
        transports=('tcp', 'udp'), auth_modes=('registration',), codecs=('alaw', 'ulaw'), dial_format='e164',
        dial_formats=('e164', 'local'),
        t38=('Telstra does not state T.38 support. Faxbot tries T.38 and uses audio fax for new calls if T.38 '
             'fax data does not come back.'),
        notes=('Enter the SIP domain from your Telstra order as the server, and Telstra\'s SBC address as the '
               'outbound proxy.',
               'Sign in with the authentication user ID and password Telstra gives you.',
               'Faxbot tested these settings against its own Asterisk standing in for the carrier; '
               'a live Telstra SIP Connect trunk has not been tested yet.'),
        sources=(Source('https://www.3cx.com/docs/sip-trunk/telstra-sip-connect-australia/'),
                 Source('https://www.telstra.com.au/content/dam/tcom/personal/consumer-advice/pdf/business-a-full/'
                        'sip-connect.pdf')),
    ),
    TrunkPreset(
        # A phone system: Faxbot is a SIP line of IP Office on the local network (Avaya DevConnect notes).
        id='avaya-ipoffice', label='Avaya IP Office', host='', port=5060, transport='udp', transports=('udp', 'tcp'),
        auth_modes=('ip',), codecs=('alaw', 'ulaw'), codecs_by_country=True, dial_format='e164',
        dial_formats=('e164', 'local'), kind=PHONE_SYSTEM,
        t38='T.38 with G.711 fallback: on the Faxbot line, set Fax Transport Support to T38 Fallback.',
        notes=('Faxbot connects to IP Office as a SIP line on your local network; IP Office keeps its own '
               'carrier lines.',
               'IP Office and Faxbot recognise each other by address, so there is no username or password.',
               'Keep only G.711 on every line a fax passes through: G.729 breaks fax.',
               'Faxbot tested this setup against a second Asterisk standing in for the phone system, with T.38 '
               'and audio fax both ways; a real IP Office has not been tested yet.'),
        admin_steps=(
            'System, LAN1 (or LAN2), VoIP: tick SIP Trunks Enable.',
            'Line, New, SIP Line. On the SIP Line tab, set ITSP Domain Name to Faxbot\'s address.',
            'Transport tab: ITSP Proxy Address is Faxbot\'s address, Layer 4 Protocol UDP, Send Port and '
            'Listen Port 5060.',
            'SIP URI tab: add one URI with its own Incoming Group and Outgoing Group; Local URI and Contact can be '
            '* so the fax numbers pass through.',
            'VoIP tab: Codec Selection Custom with only G.711 ALAW and G.711 ULAW, tick Re-invite Supported, '
            'Fax Transport Support T38 Fallback, DTMF Support RFC2833/RFC4733, Media Security Disabled.',
            'T38 Fax tab: keep Use Default Values.',
            'Incoming Call Route on the carrier line: send each fax number to the Faxbot line.',
            'Route calls from the Faxbot line to the carrier line like calls from a phone; if that needs an '
            'outside-line prefix such as 9, enter it in Faxbot too.',
            'Carrier line, VoIP tab: Fax Transport Support T38 Fallback (G.711 if the carrier has no T.38), '
            'and only G.711 codecs.',
            'If T38 is not offered on your IP Office (some Linux-based systems), choose G.711 there and turn off '
            'T.38 in Faxbot.',
        ),
        sources=(Source('https://support.avaya.com/css/public/documents/101065243'),
                 Source('https://support.avaya.com/css/public/documents/100172137')),
    ),
    TrunkPreset(
        # A phone system: Faxbot is a trusted SIP entity of Session Manager (Avaya Aura CM7/SM7 notes).
        id='avaya-aura', label='Avaya Aura', host='', port=5060, transport='udp', transports=('udp', 'tcp'),
        auth_modes=('ip',), codecs=('alaw', 'ulaw'), codecs_by_country=True, dial_format='e164',
        dial_formats=('e164', 'local'), kind=PHONE_SYSTEM,
        t38=('T.38 with G.711 fallback: set FAX Mode to t.38-standard in the codec set Communication Manager '
             'uses toward Faxbot.'),
        notes=('Faxbot connects to Session Manager as a trusted SIP entity on your local network; enter Session '
               'Manager\'s address.',
               'Session Manager and Faxbot recognise each other by address, so there is no username or password.',
               'Keep only G.711 on every trunk a fax passes through: G.729 breaks fax.',
               'Faxbot tested this setup against a second Asterisk standing in for the phone system, with T.38 '
               'and audio fax both ways; a real Aura system has not been tested yet.'),
        admin_steps=(
            'Communication Manager, change ip-codec-set: G.711A first (UK and Australia) or G.711MU first (US), '
            'no G.729; on page 2, FAX Mode t.38-standard, Redundancy 0, ECM y, Modem off.',
            'change ip-network-region: use that codec set.',
            'add signaling-group: Group Type sip, to Session Manager over TCP or TLS, Peer Detection Enabled y '
            'with Peer Server SM, DTMF over IP rtp-payload.',
            'add trunk-group: Group Type sip, Service Type tie, with enough members for the faxes sent at once.',
            'Route the fax numbers to that trunk group.',
            'Session Manager (System Manager, Elements, Routing): add a SIP Entity for Faxbot with Faxbot\'s '
            'address, an Entity Link to it on UDP or TCP port 5060 with Trust State Trusted, a Routing Policy to '
            'it, and Dial Patterns for the fax numbers.',
            'If a Session Border Controller sits in between, tick T.38 Support in its interworking profile.',
            'If T.38 calls are refused with 488 Not Acceptable Here, set ECM to n in the codec set.',
        ),
        sources=(Source('https://www.virginmediabusiness.co.uk/help/s/WSIPT_Avaya_CM7_SM7_ASBCE7.pdf'),
                 Source('https://support.avaya.com/css/public/documents/100172137'),
                 Source('https://support.avaya.com/kb/public/SOLN268281')),
    ),
    TrunkPreset(
        # Switzerland. TLS only, SRTP, G.711 audio fax; T.38 failed in the newest published test (sip_access.py).
        id='swisscom-sbc', label='Swisscom Smart Business Connect', host='', port=5061, transport='tls',
        transports=('tls',), auth_modes=('registration',), codecs=('alaw', 'ulaw'), dial_format='e164',
        audio_by_default=True, media_encryption='sdes', encrypted_audio_only=True, single_registration=True,
        t38=('T.38 failed to the phone network and to other Swisscom numbers in the newest published test '
             '(Innovaphone, December 2024), so Faxbot sends encrypted audio fax.'),
        notes=('Enter the SIP server and the username and password from your Smart Business Connect order. '
               "Innovaphone's test reached Swisscom's server zhheapp-asbc01.join.swisscom.ch.",
               'Swisscom accepts encrypted sign-in only (TLS), and Faxbot encrypts the audio as well (SRTP).',
               'Swisscom allows one registration per account: do not sign in to the same account from a second '
               'trunk, phone system or standby server.',
               'Faxbot keeps the audio path fixed during a call, as Swisscom requires, and caps audio fax at 9,600 '
               'bit/s with error correction on.',
               'Faxbot tested the encrypted settings against its own Asterisk; a live Swisscom trunk has not been '
               'tested yet.'),
        sources=(Source('https://wiki.innovaphone.com/?i=14782', '2026-10-09'),
                 Source('https://wiki.innovaphone.com/?i=12001', '2026-10-09'),
                 Source('https://service-de.enreach.com/hc/en-gb/articles/16339395197980', '2026-10-09'),
                 Source('https://www.swisscom.ch/en/residential/help/fixed-network/fax.html', '2026-10-08')),
    ),
    TrunkPreset(
        # Germany. Encrypted calls required on another provider's internet access (sip_access.py).
        id='telekom-companyflex', label='Telekom CompanyFlex', host='tel.t-online.de', port=5061, transport='tls',
        transports=('tls', 'tcp'), auth_modes=('registration',), codecs=('alaw', 'ulaw'), dial_format='e164',
        media_encryption='sdes', access_rule='telekom',
        t38=('On your Telekom line Telekom recommends T.38; encrypted calls carry fax only as audio (T.30), so on any '
             'other internet access Faxbot sends encrypted audio fax.'),
        notes=('Sign in with the registration number and password from your CompanyFlex portal, and enter its '
               'outbound proxy (it ends in .primary.companyflex.de).',
               "List your Telekom line's internet address under \"Your own line's internet address\": on any other "
               'access Faxbot encrypts the calls by itself, as CompanyFlex requires.',
               'Telekom recommends 9,600 bit/s and error correction (ECM) for fax; Faxbot uses both for audio fax.',
               'Faxbot tested these settings against its own Asterisk; a live CompanyFlex trunk has not been tested '
               'yet.'),
        sources=(Source('https://hilfe.companyflex.de/de/einrichtung/anschalteszenarien/'
                        'nutzung-eines-ip-anschlusses-eines-anderen-anbieters-am-companyflex', '2026-10-09'),
                 Source('https://hilfe.companyflex.de/de/einrichtung/anschalteszenarien/fax?mode=user', '2026-10-09'),
                 Source('https://backstage.telekom.de/hilfe/downloads/1tr119.pdf', '2026-10-09'),
                 Source('https://support.yeastar.com/hc/en-us/articles/10874343612441', '2026-10-09')),
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
# The address docker-compose.phone-system.yml publishes Asterisk on; asterisk/bin/faxbot-lan-address
# fills it in at every start, or removes the lines when Faxbot is not published on the local network.
LAN_ADDRESS = '@FAXBOT_LAN_ADDRESS@'
_DIGITS = re.compile(r'\+?[0-9]{3,20}', re.ASCII)


def country_codecs(country) -> tuple[str, ...]:
    """G.711 order for the installation country: mu-law first in North America and Japan, else A-law first."""
    import phonenumbers
    country = str(country or '').strip().upper()
    mu_law = country == 'JP' or phonenumbers.country_code_for_region(country) == 1
    return ('ulaw', 'alaw') if mu_law else ('alaw', 'ulaw')


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
    dial_format: str = 'e164'
    dial_prefix: str = ''
    country: str = 'US'
    # 'sdes' when the trunk's audio is encrypted (sip_access.py); T.38 is then off, whatever the switch says.
    media_encryption: str = ''


def configured(values) -> bool:
    return bool(getattr(values, 'sip_trunk_preset', ''))


# Fax settings shared by both fax engines (the built-in one and the SSL Fax engine).
FAX_RATES = (14400, 9600, 7200, 4800)
AUDIO_MAX_RATE = 9600
COMPRESSIONS = ('mh', 'mr', 'mmr', 'jbig')


@dataclass(frozen=True)
class FaxOptions:
    t38_error_correction: str = 'redundancy'
    t38_max_datagram: int = 400
    max_rate: int = 14400
    ecm: bool = True
    compression: str = 'jbig'
    fine: bool = True
    sslfax: bool = True
    lines: int = 2
    listener_port: int = 10443

    def rate_for(self, *, t38: bool, override=None) -> int:
        """The highest speed for one call: the setting, a recipient's own limit, and 9600 on audio calls."""
        rate = self.max_rate if override is None else min(self.max_rate, override)
        return rate if t38 else min(rate, AUDIO_MAX_RATE)


def fax_options(values) -> FaxOptions:
    """The fax settings in effect; anything missing or out of range falls back to the recommended value."""
    defaults = FaxOptions()

    def pick(name, allowed, default):
        value = getattr(values, name, default)
        return value if value in allowed else default
    ec = pick('sip_t38_error_correction', ('redundancy', 'fec', 'none'), defaults.t38_error_correction)
    datagram = getattr(values, 'sip_t38_max_datagram', defaults.t38_max_datagram)
    lines = getattr(values, 'sip_fax_lines', defaults.lines)
    port = getattr(values, 'sip_sslfax_listener_port', defaults.listener_port)
    return FaxOptions(
        t38_error_correction=ec,
        t38_max_datagram=datagram if isinstance(datagram, int) and 100 <= datagram <= 1400 else defaults.t38_max_datagram,
        max_rate=pick('sip_fax_max_rate', FAX_RATES, defaults.max_rate),
        # Encrypted audio fax runs with error correction on (Telekom's fax guidance; sip_access.py).
        ecm=bool(getattr(values, 'sip_fax_ecm', True)) or bool(
            getattr(PRESETS.get(getattr(values, 'sip_trunk_preset', '')), 'encrypted_audio_only', False)),
        compression=pick('sip_fax_compression', COMPRESSIONS, defaults.compression),
        fine=bool(getattr(values, 'sip_fax_fine', True)),
        sslfax=bool(getattr(values, 'sip_sslfax_enabled', True)),
        lines=lines if isinstance(lines, int) and 1 <= lines <= 8 else defaults.lines,
        listener_port=port if isinstance(port, int) and 1024 <= port <= 65535 else defaults.listener_port)


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
    # Required encryption is never dropped: on an access where the carrier requires it, the trunk signs in over
    # TLS whatever its connection setting says (sip_access.py, N18).
    from . import sip_access
    forced = sip_access.encryption_required(values, preset) and transport != 'tls'
    if forced:
        transport = 'tls'
    if transport not in preset.transports:
        missing.append('sip_trunk_transport')
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
    port = (0 if forced else values.sip_trunk_port) or (preset.port if transport == preset.transport
                                                         else _DEFAULT_PORTS[transport])
    media_encryption = preset.media_encryption if transport == 'tls' else ''
    if values.sip_trunk_codecs:
        codecs = tuple(values.sip_trunk_codecs.split(','))
    else:
        codecs = country_codecs(values.fax_default_country) if preset.codecs_by_country else preset.codecs
    # A chosen number format applies only where the preset offers the choice; the
    # outside-line prefix only to numbers written the way a phone here dials them.
    dial_format = values.sip_trunk_dial_format if values.sip_trunk_dial_format in preset.dial_formats else ''
    dial_format = dial_format or preset.dial_format
    return Trunk(preset=preset, auth=auth, host=host, port=port, transport=transport,
                 username=values.sip_trunk_username, password=values.sip_trunk_password,
                 outbound_proxy='' if preset.phone_system else values.sip_trunk_outbound_proxy,
                 caller_id=values.sip_trunk_caller_id,
                 dids=values.sip_trunk_did_list, t38=values.sip_t38_enabled and not media_encryption,
                 fax_preference=values.sip_fax_preference_header, codecs=codecs,
                 # A phone system is reached on the local network, never at the internet address.
                 external_address='' if preset.phone_system else values.sip_external_address,
                 dial_format=dial_format, dial_prefix=values.sip_trunk_dial_prefix if dial_format == 'local' else '',
                 country=values.fax_default_country, media_encryption=media_encryption)


def dial_number(trunk: Trunk, number: str) -> str:
    """The Request-URI user part for one canonical E.164 destination, in the carrier's format.

    Destinations reach this point already resolved for the installation country,
    so nothing here guesses a country. Raises ValueError for anything but a
    canonical number, before any call is placed.
    """
    from .routing.numbers import canonical_number
    canonical = canonical_number(number)
    if trunk.dial_format == 'local':
        dialled = trunk.dial_prefix + local_digits(canonical, trunk.country)
        # The fax engine takes at most 20 digits; a longer number is refused before any call.
        if not re.fullmatch(r'[0-9]{3,20}', dialled):
            raise ValueError('The number is too long to dial through this phone system.')
        return dialled
    if trunk.dial_format != 'digits':
        # 'e164' and 'entered' both send the canonical number with its plus sign.
        return canonical
    digits = canonical[1:]
    if trunk.auth == 'ip' and trunk.preset.ip_dial_prefix:
        return trunk.username + '*' + digits
    return digits


def local_digits(canonical: str, country: str) -> str:
    """The digits a phone in ``country`` dials for this number: national form at home, else with the
    country's international prefix (GB: 02079460000 and 0016502530000; US: 16502530000 and 011442079460000)."""
    import phonenumbers
    formatted = phonenumbers.format_out_of_country_calling_number(phonenumbers.parse(canonical),
                                                                  str(country or 'US').upper())
    return re.sub(r'[^0-9]', '', formatted)


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
    if trunk.preset.phone_system:
        # The phone system sends calls and media to the address Faxbot is published on in the local
        # network; Asterisk fills it in at start (or drops these lines). No local_net: the phone system
        # is itself on a private network and is the only SIP peer, so every message names that address
        # and the Docker network's own address never appears in SIP or SDP.
        lines += [f'external_media_address={LAN_ADDRESS}', f'external_signaling_address={LAN_ADDRESS}']
    elif trunk.external_address:
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


# -- several trunks (provider-rules design §3.6) -------------------------------------------------------------
#
# Each trunk is a provider account with provider ``sip``. The first trunk (key ``sip``) is read from the flat
# ``sip_trunk_*`` settings and keeps the section names it always had, so an installation with one trunk renders
# exactly the file it rendered before. Every other trunk gets ``trunk-<key>-endpoint``, ``-aor``, ``-auth``,
# ``-identify`` and ``-reg``, and its endpoint sets FAXBOT_TRUNK=<key> on every call it carries (``set_var``), so
# the dialplan and the hand-over know which trunk a call came in on. All trunks share Asterisk's transports,
# media ports and published address.

PRIMARY = 'sip'
TRUNK_KEY = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
# Endpoint identifiers Asterisk tries, in order, once a second trunk signs in: a call that came down one
# registration is matched to that registration's trunk first, before the carrier's address is.
IDENTIFIER_ORDER = 'line,ip,username,anonymous'


def endpoint_name(key=None) -> str:
    """The PJSIP endpoint a trunk's calls use: ``trunk-endpoint`` for the first trunk, else ``trunk-<key>-endpoint``."""
    if not key or key == PRIMARY:
        return ENDPOINT
    if TRUNK_KEY.fullmatch(key) is None:
        raise ValueError('Unsupported trunk key')
    return f'trunk-{key}-endpoint'


def _section_names(key):
    if not key or key == PRIMARY:
        return {'aor': 'trunk-aor', 'auth': 'trunk-auth', 'endpoint': ENDPOINT, 'identify': 'trunk-identify',
                'registration': 'trunk-registration'}
    base = f'trunk-{key}'
    return {'aor': f'{base}-aor', 'auth': f'{base}-auth', 'endpoint': f'{base}-endpoint',
            'identify': f'{base}-identify', 'registration': f'{base}-reg'}


@dataclass(frozen=True)
class TrunkAccount:
    """One trunk Faxbot renders: its account key, its name, and the settings as that trunk sees them."""
    key: str
    label: str
    values: object = field(repr=False)
    primary: bool = False

    @property
    def endpoint(self):
        return endpoint_name(self.key)


def extra_trunks(values, *, unfinished=False) -> list:
    """The trunk accounts after the first that are on and filled in, in account order; ``unfinished`` also lists
    those still missing their carrier (so Faxbot can say why they are not loaded)."""
    from . import accounts
    found = []
    try:
        listed = accounts.extra_accounts(values)
    except Exception:
        return found
    for account in listed:
        if account.provider != 'sip' or not account.enabled or TRUNK_KEY.fullmatch(account.key) is None:
            continue
        try:
            own = accounts.account_values(values, account.key)
        except accounts.AccountsError:
            continue
        if configured(own) or unfinished:
            found.append(TrunkAccount(account.key, account.label, own))
    return found


def trunk_accounts(values) -> list:
    """Every trunk account Faxbot may render, the first trunk first (when it is set up)."""
    found = []
    if configured(values):
        from .provider_labels import trunk_name
        found.append(TrunkAccount(PRIMARY, trunk_name(values.sip_trunk_preset or None), values, primary=True))
    return found + extra_trunks(values)


def trunk_for(values, key=None) -> TrunkAccount | None:
    """The trunk account ``key`` (None or ``sip``: the first trunk), or None when it is not set up."""
    key = key or PRIMARY
    return next((trunk for trunk in trunk_accounts(values) if trunk.key == key), None)


def _identify_matches(trunk: Trunk):
    return tuple(trunk.preset.signaling_addresses or (trunk.host,))


def _rendered(values):
    """[(TrunkAccount, Trunk, identify matches)] for every trunk in the file, and {key: why not} for the rest.

    The first trunk raises as it always has; a trunk after it that cannot be rendered is left out, with a
    sentence, so a half-finished second trunk never stops the first one. A carrier's addresses already
    matched by an earlier trunk are not matched again: Asterisk could not tell the two apart, so Faxbot
    decides those calls by the number they called (``shared_addresses``)."""
    rendered, problems, claimed = [], {}, set()
    if configured(values):
        trunk = effective_trunk(values)
        first = TrunkAccount(PRIMARY, trunk.preset.label, values, primary=True)
        rendered.append((first, trunk, _identify_matches(trunk)))
        claimed.update(_identify_matches(trunk))
    kinds = {}
    for account, trunk, _ in rendered:
        kinds[trunk.transport] = trunk.preset.phone_system
    for account in extra_trunks(values, unfinished=True):
        try:
            trunk = effective_trunk(account.values)
        except TrunkConfigurationError:
            problems[account.key] = f'{account.label} is not loaded yet: fill in its settings.'
            continue
        if trunk.transport in kinds and kinds[trunk.transport] != trunk.preset.phone_system:
            problems[account.key] = (f'{account.label} is not loaded: a phone system and a carrier cannot share one '
                                     f'{trunk.transport.upper()} connection. Choose another connection type for it.')
            continue
        kinds.setdefault(trunk.transport, trunk.preset.phone_system)
        if trunk.preset.single_registration and any(
                other.preset.id == trunk.preset.id and other.host == trunk.host and other.username == trunk.username
                for _, other, _ in rendered):
            problems[account.key] = (f'{account.label} is not loaded: {trunk.preset.label} allows one registration '
                                     'per account, and another trunk already signs in with it.')
            continue
        matches = tuple(address for address in _identify_matches(trunk) if address not in claimed)
        claimed.update(matches)
        rendered.append((account, trunk, matches))
    return rendered, problems


def trunk_problems(values) -> dict:
    """{trunk key: one sentence} for trunk accounts that are on but not in Asterisk's file, and why."""
    try:
        return _rendered(values)[1]
    except TrunkConfigurationError:
        return {}


def rendered_endpoints(values) -> tuple:
    """The endpoint of every trunk in Asterisk's file, the first trunk's first: the only ones Faxbot dials."""
    try:
        rendered, _ = _rendered(values)
    except TrunkConfigurationError:
        return ()
    return tuple(account.endpoint for account, _, _ in rendered)


def trunk_loaded(values, key=None) -> bool:
    """Whether calls can go over trunk account ``key`` now: Faxbot wrote it into Asterisk's file, and the running
    Asterisk loaded that file (when it shares Faxbot's data folder and says what it loaded). A trunk that is not
    loaded is skipped like an account that is not ready, never bound to a fax."""
    try:
        endpoint = endpoint_name(key)
    except ValueError:
        return False
    if endpoint not in rendered_endpoints(values):
        return False
    try:
        started = started_configuration_path(values).read_text()
    except OSError:
        return True  # an Asterisk Faxbot does not manage: what it loaded cannot be read here
    return f'[{endpoint}]' in started


def shared_addresses(values) -> list:
    """Groups of trunk keys whose carrier addresses overlap: Asterisk matches such a call to the first of them,
    so the number it called decides the trunk (each number belongs to one trunk account)."""
    try:
        rendered, _ = _rendered(values)
    except TrunkConfigurationError:
        return []
    groups = []
    for account, trunk, _ in rendered:
        addresses = set(_identify_matches(trunk))
        joined = [group for group in groups if group[1] & addresses]
        keys, found = {account.key}, set(addresses)
        for group in joined:
            keys |= group[0]
            found |= group[1]
            groups.remove(group)
        groups.append((keys, found))
    return [sorted(keys) for keys, _ in groups if len(keys) > 1]


def trunk_numbers(values) -> dict:
    """{trunk key: the numbers the carrier sends to that trunk}, E.164 as stored."""
    found = {}
    for trunk in trunk_accounts(values):
        found[trunk.key] = tuple(trunk.values.sip_trunk_did_list)
    return found


def _trunk_lines(trunk: Trunk, values, names, transport, matches, key=None):
    """One trunk's sections: AOR, credentials, endpoint, identify and registration."""
    registration = trunk.auth == 'registration'
    udp = trunk.transport == 'udp'
    lines = [f'[{names["aor"]}]', 'type=aor', f'contact={_uri(trunk)}', f'qualify_frequency={25 if udp else 30}',
             'qualify_timeout=3.0', '']
    if registration:
        lines += [f'[{names["auth"]}]', 'type=auth', 'auth_type=userpass',
                  f'username={trunk.username}', f'password={trunk.password}', '']
    lines += [f'[{names["endpoint"]}]', 'type=endpoint', f'transport={transport}', f'aors={names["aor"]}']
    if registration:
        lines.append(f'outbound_auth={names["auth"]}')
    lines += [f'context={INBOUND_CONTEXT}', 'disallow=all', 'allow=' + ','.join(trunk.codecs)]
    if trunk.media_encryption:
        if trunk.transport != 'tls':
            # SDES keys travel in the SDP: never written over a connection that is not encrypted.
            raise TrunkConfigurationError(['sip_trunk_transport'])
        # Encrypted audio (SRTP, SDES keys in the TLS-protected SDP); a call that cannot be encrypted fails.
        lines += [f'media_encryption={trunk.media_encryption}', 'media_encryption_optimistic=no']
    if key is not None:
        # Every call over this trunk, in and out, carries the trunk's account key to the dialplan.
        lines.append(f'set_var=FAXBOT_TRUNK={key}')
    if trunk.t38:
        # Fax settings: T.38 error correction and the largest packet the carrier accepts.
        options = fax_options(values)
        lines += ['t38_udptl=yes', f't38_udptl_ec={options.t38_error_correction}',
                  f't38_udptl_maxdatagram={options.t38_max_datagram}', 't38_udptl_nat=yes']
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
    if matches:
        lines += [f'[{names["identify"]}]', 'type=identify', f'endpoint={names["endpoint"]}']
        lines += [f'match={address}' for address in matches]
        lines.append('')
    if registration:
        lines += [f'[{names["registration"]}]', 'type=registration', f'transport={transport}',
                  f'outbound_auth={names["auth"]}', f'server_uri={_uri(trunk)}',
                  f'client_uri={_uri(trunk, trunk.username)}', f'contact_user={trunk.username}',
                  # Re-register often enough to refresh a UDP mapping; never give up
                  # on an unattended fax server (max_retries=0 would mean no retries).
                  f'expiration={120 if udp else 300}', 'retry_interval=60', 'forbidden_retry_interval=600',
                  'fatal_retry_interval=120', 'max_retries=10000', 'auth_rejection_permanent=no',
                  'line=yes', f'endpoint={names["endpoint"]}']
        if trunk.outbound_proxy:
            lines.append(f'outbound_proxy=sip:{trunk.outbound_proxy}\\;lr')
        lines.append('')
    return lines


def render_pjsip(values) -> str:
    """Complete pjsip.conf text for the active settings; contains the SIP passwords.

    One section set per trunk account; with one trunk the file is exactly what it has always been.
    """
    rendered, _ = _rendered(values)
    if not rendered:
        raise TrunkConfigurationError(['sip_trunk_preset'])
    first_account, first, _ = rendered[0]
    transport, transport_lines = _transport_section(first)
    transports = {first.transport: transport}
    extra_transports = []
    for _, trunk, _ in rendered[1:]:
        if trunk.transport not in transports:
            name, section = _transport_section(trunk)
            transports[trunk.transport] = name
            extra_transports += ['', *section]
    registrations = any(trunk.auth == 'registration' for _, trunk, _ in rendered[1:])
    lines = [
        f'; Faxbot SIP trunk for {first.preset.label}, written by Faxbot from its settings.',
        '; Change the trunk in Faxbot settings; edits to this file are replaced.',
        # Every flow starts from Faxbot's side and is kept alive from it, so no
        # router port has to be opened: keepalives on the TCP/TLS connection,
        # carrier checks every 25 s on UDP (under common 30 s NAT timeouts).
        '[global]', 'type=global', 'user_agent=Faxbot-Asterisk', 'keep_alive_interval=30',
        *([f'endpoint_identifier_order={IDENTIFIER_ORDER}'] if registrations else []), '',
        *transport_lines, *extra_transports, '',
    ]
    for account, trunk, matches in rendered:
        primary = account.primary
        if not primary:
            lines += [f'; Trunk {account.key}: {trunk.preset.label}.']
        lines += _trunk_lines(trunk, account.values, _section_names(None if primary else account.key),
                              transports[trunk.transport], matches, key=None if primary else account.key)
    return '\n'.join(lines)


def configuration_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'pjsip.conf'


def secret_path(values) -> Path:
    return Path(values.fax_data_dir) / 'asterisk' / 'inbound.secret'


def public_address_path(values) -> Path:
    """What Faxbot's STUN probe found, read by the Asterisk container at start."""
    return Path(values.fax_data_dir) / 'asterisk' / 'public-address'


def write_public_address(values, probe, *, exact=None) -> bool:
    """Record the probe for the next Asterisk start; True when the advertised address would change.

    Asterisk advertises the address only when the network keeps port numbers,
    so a probe without that is recorded with ``ports_preserved`` false.
    ``exact`` True says the address and port numbers are exact anyway, because
    the router opened Faxbot's fax ports to the same numbers (sip_network).
    """
    import json
    path = public_address_path(values)
    preserved = bool(probe and probe.public_ip and probe.ports == 'preserved')
    record = {'ip': probe.public_ip if probe else None,
              'ports_preserved': bool(probe and probe.public_ip and (preserved or exact)),
              'probed_at': int(probe.probed_at) if probe else None}
    if exact and not preserved and record['ports_preserved']:
        record['router_ports'] = True
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


def lan_address_path(values) -> Path:
    """Written by the Asterisk container at start when docker-compose.phone-system.yml publishes it."""
    return Path(values.fax_data_dir) / 'asterisk' / 'lan-address'


_OCTET = r'(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
_IPV4 = re.compile(rf'{_OCTET}(?:\.{_OCTET}){{3}}', re.ASCII)


def read_lan_address(values):
    """Where a phone system reaches Faxbot ({address, sip_port, media_ports, faxes_at_once}), or None."""
    import json
    try:
        record = json.loads(lan_address_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not _IPV4.fullmatch(str(record.get('address', ''))):
        return None
    ports = re.fullmatch(r'([0-9]{4,5})-([0-9]{4,5})', str(record.get('media_ports', '')), re.ASCII)
    if not ports or int(ports.group(1)) > int(ports.group(2)):
        return None
    first, last = int(ports.group(1)), int(ports.group(2))
    return {'address': record['address'], 'sip_port': 5060, 'media_ports': f'{first}-{last}',
            'media_first': first, 'media_last': last, 'faxes_at_once': faxes_at_once(first, last)}


def faxes_at_once(first: int, last: int) -> int:
    """Faxes a media range carries at once: its first third is T.38 (one port per fax), the rest
    audio (two ports per fax), as asterisk/start.sh divides it."""
    count = last - first + 1
    udptl = count // 3
    return max(0, min(udptl, (count - udptl) // 2))


def manager_credentials_path(values) -> Path:
    """The manager login Asterisk's start script reads when the environment sets none."""
    return Path(values.fax_data_dir) / 'asterisk' / 'manager.credentials'


_MANAGER_USERNAME = re.compile(r'[A-Za-z0-9_-]{1,64}')


def _manager_login_usable(username, password) -> bool:
    """What asterisk/start.sh accepts: a plain username and a password manager.conf can hold."""
    return bool(_MANAGER_USERNAME.fullmatch(username or '') and username.lower() != 'general' and password
                and password == password.strip() and not re.search(r'[\x00-\x1f\x7f;\\]', password))


def write_manager_credentials(values):
    """Write Faxbot's manager username and password for Asterisk (mode 0600); None when unusable.

    Asterisk's start script turns its manager port on with this login, and
    restarts itself when the file changes, so the two containers never need a
    password typed twice. ASTERISK_AMI_PASSWORD in the environment still wins
    in both containers.
    """
    if not _manager_login_usable(values.ami_username, values.ami_password):
        return None
    path = manager_credentials_path(values)
    text = f'{values.ami_username}\n{values.ami_password}\n'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if path.read_text(encoding='utf-8') == text:
            return path
    except OSError:
        pass
    _write_private(path, text)
    return path


def manager_credentials_shared(values) -> bool:
    """Whether the file Asterisk reads holds exactly the login Faxbot uses."""
    try:
        return manager_credentials_path(values).read_text(encoding='utf-8') == \
            f'{values.ami_username}\n{values.ami_password}\n'
    except OSError:
        return False


def engine_marker_path(values) -> Path:
    """Written by the Asterisk container at every start (asterisk/start.sh)."""
    return Path(values.fax_data_dir) / 'asterisk' / 'engine-started'


def started_configuration_path(values) -> Path:
    """The trunk file exactly as Asterisk loaded it at its last start, before addresses were filled in."""
    return Path(values.fax_data_dir) / 'asterisk' / 'pjsip.conf.started'


def engine_managed(values) -> bool:
    """Whether Asterisk shares Faxbot's data folder, as in the Docker Compose install.

    Such an Asterisk reads what Faxbot writes at every start, and Docker starts
    it again after it stops, so Faxbot can restart it to load new settings.
    """
    return engine_marker_path(values).is_file()


def engine_uses_current(values) -> bool:
    """Whether the running Asterisk loaded exactly these trunk settings and internet address."""
    try:
        started = started_configuration_path(values).read_bytes()
        expected = rendered_configuration(values).encode()
    except (OSError, TrunkConfigurationError):
        return False
    if started != expected:
        return False
    from . import hylafax_engine
    if not hylafax_engine.iax_current(values):
        return False
    if PUBLIC_ADDRESS not in expected.decode():
        return True
    record = read_public_address(values) or {}
    wanted = record.get('ip') if record.get('ports_preserved') and record.get('ip') else ''
    return applied_public_address(values) == wanted


def applied_public_address(values):
    """The address Asterisk advertised when it last started: an address, '' for none, None if unknown."""
    try:
        return (public_address_path(values).with_name('public-address.applied')).read_text().strip()
    except OSError:
        return None


def peer_sections(values, peers=None) -> str:
    """The partners' peer fax call sections for pjsip.conf (direct/peer_call.py), '' for none.

    ``peers`` None reads the enrolled partners from Faxbot's database; when it cannot be read the file is written
    without them (logged), and peer fax calls wait for the next write.
    """
    from .direct import peer_call
    from .routing.database import DeliveryStoreError
    if peers is None:
        from .ami import _database
        try:
            peers = peer_call.installation_peers(_database())
        except DeliveryStoreError as error:
            logging.getLogger(__name__).warning('Partners could not be read for peer fax calls (%s).', error)
            peers = []
    return peer_call.render_peers(peers)


def rendered_configuration(values, peers=None) -> str:
    """pjsip.conf exactly as Faxbot writes it: the trunks, then any partners' peer fax calls. Whether Asterisk runs
    the current settings is decided against this same text (``engine_uses_current``, sip_http's applied check)."""
    text = render_pjsip(values)
    peered = peer_sections(values, peers)
    return text + '\n\n' + peered if peered else text


def write_asterisk_configuration(values, *, inbound_secret=None, peers=None) -> Path:
    """Atomically write the private files the Asterisk container reads.

    ``pjsip.conf`` is loaded when Asterisk starts: the trunks, then any partners' peer fax calls
    (``peer_sections``). ``inbound.secret`` holds the
    shared secret the inbound dialplan sends with each received fax; the
    console's Apply always passes one (Faxbot creates it when none is set). It
    is removed only when no secret is known at all.
    """
    text = rendered_configuration(values, peers)
    target = configuration_path(values)
    target.parent.mkdir(parents=True, exist_ok=True, mode=SHARED_FOLDER_MODE)
    _write_private(target, text)
    secret = inbound_secret or values.asterisk_inbound_secret
    if secret:
        write_inbound_secret(values, secret)
    else:
        try:
            secret_path(values).unlink()
        except FileNotFoundError:
            pass
    # The SSL Fax engine's settings and its fax lines for Asterisk (iax.conf).
    from . import hylafax_engine
    hylafax_engine.write_engine_files(values, secret)
    return target


# Asterisk runs as its own user, in its own group, which the shared folder gives every file made in it
# (asterisk/start.sh). The folder and the inbound secret are readable by that group; the trunk settings and
# the manager login stay root's alone (start.sh reads them as root).
SHARED_FOLDER_MODE = 0o750
SECRET_MODE = 0o640


def write_inbound_secret(values, secret: str) -> Path:
    """Write the inbound secret the Asterisk notify script reads with each received fax (mode 0640)."""
    path = secret_path(values)
    path.parent.mkdir(parents=True, exist_ok=True, mode=SHARED_FOLDER_MODE)
    try:
        if path.read_text(encoding='utf-8') == secret:
            return path
    except OSError:
        pass
    _write_private(path, secret, mode=SECRET_MODE)
    return path


def _write_private(target: Path, text: str, *, mode: int = 0o600):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
    try:
        os.fchmod(descriptor, mode)
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
        'kind': preset.kind, 'transports': list(preset.transports), 'codecs_by_country': preset.codecs_by_country,
        'dial_formats': list(preset.dial_formats), 'audio_by_default': preset.audio_by_default,
        'admin_steps': list(preset.admin_steps),
        # Encrypted audio fax (sip_access.py, N18).
        'media_encryption': preset.media_encryption or None, 'encrypted_audio_only': preset.encrypted_audio_only,
        'single_registration': preset.single_registration, 'access_rule': preset.access_rule or None,
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
