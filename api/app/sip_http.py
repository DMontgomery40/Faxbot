"""Administrative HTTP surface for the SIP trunk: presets, status, apply and recent calls."""
import asyncio
import hashlib
import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .access.route_policy import require_permission
from .config import configuration_values
from .config_runtime import run_lifecycle_step
from . import sip_fax_mode, sip_trunk, stun
from .ami import ENGINE_UNREACHABLE
from .config_store import ConfigurationStoreError
from .inbound.acquisition import AcquisitionError
from .inbound.sip_handover import ensure_inbound_secret
from .sip_calls import NOT_HANDED_OVER, SipCallRecordError, SipCallRecords


router = APIRouter(prefix='/admin/sip', tags=['SIP trunk'])

_REGISTRATION_TEXT = {
    'registered': 'The carrier accepted Faxbot\'s registration.',
    'not_registered': 'Faxbot is not registered with the carrier yet.',
    'rejected': 'The carrier rejected the username or password.',
    'not_used': 'This trunk uses IP address authentication, so there is no registration.',
    'unknown': 'Registration status is not available.',
}
_REACHABILITY_TEXT = {
    'reachable': 'The carrier answers Faxbot\'s checks.',
    'unreachable': 'The carrier does not answer Faxbot\'s checks.',
    'unknown': 'Faxbot cannot tell yet whether the carrier answers.',
}
_FIELD_NAMES = {'sip_trunk_preset': 'carrier', 'sip_trunk_auth': 'sign-in method', 'sip_trunk_host': 'server',
                'sip_trunk_username': 'username', 'sip_trunk_password': 'password',
                'sip_trunk_caller_id': 'caller ID'}
_REGISTRATION_STATES = {'registered': 'registered', 'rejected': 'rejected', 'unregistered': 'not_registered',
                        'stopped': 'not_registered', 'failed': 'rejected'}
_DEVICE_STATES = {'not_inuse': 'reachable', 'inuse': 'reachable', 'busy': 'reachable', 'ringing': 'reachable',
                  'ringinuse': 'reachable', 'onhold': 'reachable', 'unavailable': 'unreachable'}
_TRANSPORT_NAMES = {'udp': 'UDP', 'tcp': 'TCP', 'tls': 'TLS'}


def _transport_from_section(name):
    """``transport-tcp`` (the section Faxbot renders) to ``tcp``; anything else is unknown."""
    name = str(name or '').strip().lower()
    transport = name.removeprefix('transport-')
    return transport if name.startswith('transport-') and transport in _TRANSPORT_NAMES else None


# A rejected registration after authentication (401/403): say which password the carrier wants.
_REJECTED_BY_CARRIER = {
    'telnyx': ('Telnyx refused the username or password. Use the SIP connection\'s password, '
               'not your Telnyx account password.'),
}
REJECTED_GENERIC = ('The carrier refused the username or password. Use the SIP credentials the carrier gave for '
                    'this trunk, not your account login.')


def _rejected_text(preset):
    return _REJECTED_BY_CARRIER.get(preset, REJECTED_GENERIC)


def _phone_system(preset):
    """Whether this preset is a phone system on the local network rather than a carrier."""
    found = sip_trunk.PRESETS.get(preset or '')
    return bool(found and found.phone_system)


def _registration_text(registration, transport, preset=None):
    if registration == 'not_used' and _phone_system(preset):
        return ('Faxbot and your phone system recognise each other by address, so there is no registration'
                + (f'; calls use {_TRANSPORT_NAMES[transport]}.' if transport else '.'))
    if registration == 'rejected':
        return _rejected_text(preset)
    if registration == 'registered' and transport:
        return f"The carrier accepted Faxbot's registration over {_TRANSPORT_NAMES[transport]}."
    if registration == 'not_used' and transport:
        return ('This trunk uses IP address authentication, so there is no registration; '
                f'calls use {_TRANSPORT_NAMES[transport]}.')
    return _REGISTRATION_TEXT[registration]


BEHIND_ROUTER = ('Your Faxbot runs behind a router, so sign in with a username and password; '
                 'server IP sign-in needs a public address.')
NO_PORTS = 'No ports need to be opened or forwarded.'

# A phone system on the local network reaches Faxbot where docker-compose.phone-system.yml publishes it.
PHONE_SYSTEM_COMMAND = 'docker compose -f docker-compose.yml -f docker-compose.phone-system.yml up -d'
PHONE_SYSTEM_SETTING = 'FAXBOT_LAN_ADDRESS'
LAN_NOT_STARTED = 'Your phone system cannot reach Faxbot yet, because Faxbot is not published on your local network.'
LAN_HIDDEN = ('Faxbot runs in Docker Desktop or Colima here, which hide your phone system\'s address from Faxbot, '
              'so the phone system cannot connect; run Faxbot on a Linux computer to connect a phone system.')
# Docker Desktop and Colima give containers these names; Docker Engine on Linux does not.
DESKTOP_HOST_NAMES = ('host.docker.internal', 'host.lima.internal')
_desktop = {}


def lan_text(record):
    """What to give the phone system's administrator, in one sentence."""
    return (f'Give your phone system administrator this address: {record["address"]}, port 5060 (UDP or TCP), '
            f'and media ports {record["media_first"]}\u2013{record["media_last"]}, enough for '
            f'{record["faxes_at_once"]} {"fax" if record["faxes_at_once"] == 1 else "faxes"} at once.')


def _desktop_docker():
    import socket
    for name in DESKTOP_HOST_NAMES:
        try:
            socket.getaddrinfo(name, None)
            return True
        except OSError:
            continue
    return False


async def address_hidden():
    """True when Faxbot runs in Docker Desktop or Colima, whose published ports replace the sender's address."""
    if 'value' not in _desktop:
        try:
            _desktop['value'] = bool(await asyncio.wait_for(asyncio.to_thread(_desktop_docker), 3))
        except Exception:
            return False
    return _desktop['value']


def _phone_system_reach(values, managed, hidden):
    """(record, sentence) for how a phone system reaches Faxbot; (None, None) when Faxbot cannot tell."""
    record = sip_trunk.read_lan_address(values)
    if hidden:
        return record, LAN_HIDDEN
    if record:
        return record, lan_text(record)
    return None, (LAN_NOT_STARTED if managed else None)
# One STUN probe serves status checks for a minute; Apply always probes again.
_PROBE_SECONDS = 60
_probes = {}


async def probe_network(preset, *, fresh=False):
    """The STUN result for this carrier preset, run off the event loop; never raises."""
    cached = _probes.get(preset)
    if cached and not fresh and time.monotonic() - cached[0] < _PROBE_SECONDS:
        return cached[1]
    try:
        result = await asyncio.to_thread(stun.probe, stun.servers_for(preset))
    except Exception:
        result = None
    _probes[preset] = (time.monotonic(), result)
    return result


_UNTESTED = 'the first test fax shows whether it does.'


def _observed(records):
    """What recent calls have shown on this network, or None before any fax went through or failed.

    ``t38_failed``: the newest T.38 call got no fax data back; ``t38_ok``: it
    went through; ``audio_ok``: a fax sent or received as audio went through.
    """
    try:
        items = records.page(limit=50)['items'] if records else []
    except (SipCallRecordError, ValueError):
        return None
    found = {'t38_failed': False, 't38_ok': False, 'audio_ok': False}
    newest_t38 = True
    for item in items:
        went_through = item.get('verdict') in ('sent', 'received')
        if item.get('t38') == 'yes':
            if newest_t38:
                found['t38_failed'] = (item.get('verdict') == 'no_t38_data_back'
                                       and sip_fax_mode.t38_timeout(item.get('error_cause')))
                found['t38_ok'] = went_through
            newest_t38 = False
        elif went_through:
            found['audio_ok'] = True
    return found if any(found.values()) else None


def _address_text(summary, network, carrier, observed=None, audio=False):
    """Faxbot's internet address in use and how the network treats it, in one sentence.

    Once calls have shown whether the carrier follows Faxbot's packets, the
    sentence says what they showed instead of waiting for the first test fax:
    audio and T.38 can differ (a carrier may follow audio packets but not
    T.38 ones), so both are named when both were seen.
    """
    text = _network_text(summary, network, carrier)
    if not text.endswith(_UNTESTED) or observed is None:
        return text
    lead = text[:-len(_UNTESTED)].rstrip(' ,;').removesuffix(', and')
    bare = lead.removesuffix(f", so {carrier} has to follow Faxbot's packets")
    if observed['t38_failed'] and observed['audio_ok']:
        return (f"{bare}, and {carrier} follows Faxbot's audio packets (a fax went through) but not its T.38 packets"
                f"{', so Faxbot uses audio fax' if audio else ''}.")
    if observed['t38_failed']:
        return (f'{bare}, and the last T.38 fax got no fax data back, so {carrier} does not follow Faxbot\'s T.38 '
                f'packets on this network{"; audio fax is in use" if audio else ""}.')
    if observed['t38_ok']:
        return f'{lead}, and a T.38 fax that went through shows it does.'
    return f'{lead}, and a fax that went through shows it does.'


def _network_text(summary, network, carrier):
    typed = summary.get('public_address')
    if typed:
        if network and network.public_ip and network.public_ip != typed:
            return ('The address you entered differs from the one Faxbot sees from the internet '
                    f'({network.public_ip}).')
        return f'Faxbot tells {carrier} to send calls and fax data to {typed}, the address you entered.'
    if (network and network.public_ip and network.ports == 'preserved' and network.behind_nat
            and summary.get('advertised_address') == network.public_ip):
        return (f'Faxbot\'s internet address is {network.public_ip}, and your network keeps port numbers, '
                f'so {carrier} is told exactly where to send fax data.')
    return stun.address_sentence(network, carrier=carrier)


ADDRESS_CHANGED = 'Your internet address changed. Restart the Asterisk service so the carrier gets the new address.'
ADDRESS_CHANGED_MANAGED = 'Your internet address changed. Select Apply and connect so the carrier gets the new address.'
APPLY_MANUAL = 'Apply these settings to Asterisk, then restart the Asterisk service.'
APPLY_MANAGED = 'Select Apply and connect so Asterisk uses these settings.'
NOT_LOADED = 'Asterisk still uses earlier trunk settings; select Apply and connect to load these.'
RESTARTING = 'Asterisk is restarting to use the new settings.'
SAVED_MANUAL = 'Saved for Asterisk. Restart the Asterisk service to use these settings.'
SAVED_CURRENT = 'Saved. Asterisk already uses these settings.'
SAVED_BUSY = 'Saved. A call is in progress, so Asterisk keeps its current settings until you apply again after it ends.'
SAVED_NOT_ALLOWED = ('Saved. Asterisk does not let Faxbot restart it yet; restart the Asterisk service once, '
                     'and Apply and connect restarts it from then on.')
# Faxbot asked Asterisk to restart at this time.monotonic(); a restart that takes
# longer than this is no longer reported as in progress.
_RESTART_SECONDS = 120
_restart = {'at': None}


HANDOVER_READY = 'Received faxes reach Faxbot: ready.'
HANDOVER_MANAGED = 'Received faxes cannot reach Faxbot yet; select Apply and connect to connect them.'
HANDOVER_MANUAL = ('Received faxes cannot reach Faxbot yet; apply these settings to Asterisk, '
                   'then restart the Asterisk service.')


def _handover(values, managed, last):
    """Whether a fax received over the trunk can reach Faxbot, in one sentence; None when the trunk does not receive.

    Asterisk reads the inbound secret from the shared folder for each received
    fax, so the secret Faxbot keeps and the written file must agree.
    """
    if not (values.inbound_enabled and values.effective_inbound == 'sip'):
        return None
    if last and last['verdict'] == NOT_HANDED_OVER:
        return {'ready': False, 'text': last['summary']}
    secret = values.asterisk_inbound_secret
    try:
        written = bool(secret) and sip_trunk.secret_path(values).read_text(encoding='utf-8') == secret
    except OSError:
        written = False
    if written:
        return {'ready': True, 'text': HANDOVER_READY}
    return {'ready': False, 'text': HANDOVER_MANAGED if managed else HANDOVER_MANUAL}


NO_TRUNK = 'No SIP trunk is set up. Choose your carrier to start.'
TRUNK_INCOMPLETE = 'Some trunk settings are missing.'


def sip_trunk_message(values):
    """Readiness: the sentence when a direction uses the SIP trunk and the trunk is not set up; else None.

    An older install whose Asterisk container carries the trunk itself
    (SIP_USERNAME/SIP_SERVER in its environment) counts as set up.
    """
    uses = values.effective_outbound == 'sip' or (values.inbound_enabled and values.effective_inbound == 'sip')
    if not uses or os.environ.get('SIP_SERVER') or os.environ.get('SIP_USERNAME'):
        return None
    if not sip_trunk.configured(values):
        return NO_TRUNK
    try:
        sip_trunk.effective_trunk(values, for_calls=values.effective_outbound == 'sip')
    except sip_trunk.TrunkConfigurationError:
        return TRUNK_INCOMPLETE
    return None


def _restarting():
    """True from Faxbot's restart request until Faxbot has logged in to the restarted Asterisk."""
    from .ami import ami_client
    at = _restart['at']
    if at is None or time.monotonic() - at > _RESTART_SECONDS:
        return False
    return not (ami_client._connected.is_set() and ami_client.connected_at is not None
                and ami_client.connected_at > at)


def _address_changed(values, network):
    """True when Asterisk advertises an address that is no longer Faxbot's, or should now advertise one."""
    if values.sip_external_address or network is None or network.public_ip is None:
        return False
    applied = sip_trunk.applied_public_address(values)
    if applied is None:
        return False
    wanted = network.public_ip if network.ports == 'preserved' else ''
    return applied != wanted


def _ports_text(values, network):
    """Whether this sign-in method can work from here; None when Faxbot cannot tell."""
    if values.sip_trunk_auth == 'ip':
        if values.sip_external_address:
            return None
        return BEHIND_ROUTER if network and network.behind_nat else None
    return NO_PORTS


def _last_call(records):
    """The newest call's plain sentence, or None when there are no calls yet."""
    try:
        latest = records.latest() if records else None
    except (SipCallRecordError, ValueError):
        return None
    return latest


def _runtime(request):
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return runtime


def _engine(request):
    return _runtime(request).manager.store.engine


def _summary(values):
    """Plain settings summary without the password."""
    preset = sip_trunk.PRESETS.get(values.sip_trunk_preset)
    if preset is None:
        return {'configured': False}
    try:
        trunk = sip_trunk.effective_trunk(values)
        missing = []
    except sip_trunk.TrunkConfigurationError as error:
        trunk, missing = None, list(error.fields)
    return {
        'configured': True, 'preset': preset.id, 'preset_label': preset.label, 'kind': preset.kind,
        'auth': values.sip_trunk_auth,
        'host': trunk.host if trunk else (values.sip_trunk_host or preset.host),
        'port': trunk.port if trunk else None, 'transport': trunk.transport if trunk else None,
        'missing': missing, 'caller_id_set': bool(values.sip_trunk_caller_id),
        'dids': list(values.sip_trunk_did_list), 't38': values.sip_t38_enabled,
        'fax_preference': values.sip_fax_preference_header,
        'public_address': (values.sip_external_address or None) if not preset.phone_system else None,
        'public_address_source': 'typed' if values.sip_external_address and not preset.phone_system else None,
        'codecs': list(trunk.codecs) if trunk else None,
        'dial_format': trunk.dial_format if trunk else None,
        'dial_prefix': trunk.dial_prefix if trunk else '',
    }


def _applied(values):
    """Whether the file Asterisk loads at start matches the current settings."""
    path = sip_trunk.configuration_path(values)
    try:
        current = path.read_bytes()
        expected = sip_trunk.render_pjsip(values).encode()
    except (OSError, sip_trunk.TrunkConfigurationError):
        return False
    return hashlib.sha256(current).digest() == hashlib.sha256(expected).digest()


async def _asterisk_status(values):
    from .ami import ami_client
    result = {'connected': bool(ami_client._connected.is_set()), 'registration': 'unknown',
              'reachability': 'unknown', 'permission': True, 'transport': None}
    if not result['connected']:
        result['engine_message'] = ami_client.engine_message()
        return result
    try:
        if values.sip_trunk_auth == 'registration':
            response, events = await ami_client.status_query(
                {'Action': 'PJSIPShowRegistrationsOutbound'}, collect=True)
            if response['response'].lower() == 'success':
                details = [event for event in events if event.get('ObjectName') == 'trunk-registration']
                status = (details[0].get('Status', '') if details else '').strip().lower()
                result['registration'] = _REGISTRATION_STATES.get(status, 'unknown')
                # The transport Asterisk itself registers over, not the one in settings.
                result['transport'] = _transport_from_section(details[0].get('Transport')) if details else None
            elif 'permission' in response['message'].lower():
                result['permission'] = False
        else:
            result['registration'] = 'not_used'
        response, _ = await ami_client.status_query(
            {'Action': 'Getvar', 'Variable': f'DEVICE_STATE(PJSIP/{sip_trunk.ENDPOINT})'})
        if response['response'].lower() == 'success':
            result['reachability'] = _DEVICE_STATES.get(response['value'].strip().lower(), 'unknown')
        elif 'permission' in response['message'].lower():
            result['permission'] = False
        # The carrier check (OPTIONS) round trip, over the same connection as the registration.
        response, events = await ami_client.status_query({'Action': 'PJSIPShowContacts'}, collect=True)
        if response['response'].lower() == 'success':
            for event in events:
                microseconds = str(event.get('RoundtripUsec', '')).strip()
                if str(event.get('ObjectName', '')).startswith('trunk-aor@@') and microseconds.isdigit():
                    result['round_trip_ms'] = max(1, round(int(microseconds) / 1000))
    except (ConnectionError, TimeoutError):
        result['connected'] = False
    return result


_PHONE_REACHABILITY_TEXT = {
    'reachable': 'The phone system answers Faxbot\'s checks.',
    'unreachable': 'The phone system does not answer Faxbot\'s checks.',
    'unknown': 'Faxbot cannot tell yet whether the phone system answers.',
}


def _reachability_text(asterisk, phone=False):
    milliseconds = asterisk.get('round_trip_ms')
    if asterisk['reachability'] == 'reachable' and milliseconds:
        return f'The {"phone system" if phone else "carrier"} answered Faxbot\'s check in {milliseconds} ms.'
    return (_PHONE_REACHABILITY_TEXT if phone else _REACHABILITY_TEXT)[asterisk['reachability']]


def _message(summary, asterisk, applied, ports_text=None, transport=None, *, managed=False, in_use=True,
             restarting=False):
    if not summary.get('configured'):
        return NO_TRUNK
    if summary['missing']:
        return TRUNK_INCOMPLETE
    if ports_text in (BEHIND_ROUTER, LAN_HIDDEN, LAN_NOT_STARTED):
        return ports_text
    phone = summary.get('kind') == sip_trunk.PHONE_SYSTEM
    if restarting:
        return RESTARTING
    if not applied:
        return APPLY_MANAGED if managed else APPLY_MANUAL
    if not asterisk['connected']:
        return asterisk.get('engine_message') or ENGINE_UNREACHABLE
    if not asterisk['permission']:
        return 'Asterisk does not let Faxbot read trunk status. Restart the Asterisk service to update its access.'
    if managed and not in_use:
        return NOT_LOADED
    if asterisk['registration'] == 'rejected':
        return _rejected_text(summary.get('preset'))
    if asterisk['registration'] == 'not_registered':
        if (transport or summary.get('transport')) == 'tls':
            return ('Faxbot is not registered with the carrier yet; if this lasts, the encrypted connection may be '
                    'failing, so switch Transport to TCP and apply again.')
        return _REGISTRATION_TEXT['not_registered']
    if asterisk['reachability'] == 'unreachable':
        return (_PHONE_REACHABILITY_TEXT if phone else _REACHABILITY_TEXT)['unreachable']
    if asterisk['reachability'] == 'reachable':
        return 'The trunk is ready.'
    return f'Faxbot is connected to Asterisk; the {"phone system" if phone else "carrier"} has not answered a check yet.'


@router.get('/presets')
def presets(identity=Depends(require_permission('providers:read'))):
    """Carrier presets with their documented settings and sources."""
    return {'presets': sip_trunk.preset_catalog()}


@router.get('/status')
async def status(request: Request, identity=Depends(require_permission('providers:read'))):
    """Trunk registration and carrier reachability as Asterisk reports them; never includes secrets."""
    values = configuration_values()
    summary = _summary(values)
    configured = summary.get('configured')
    applied = await run_lifecycle_step(lambda: _applied(values)) if configured else False
    asterisk = (await _asterisk_status(values) if configured
                else {'connected': False, 'registration': 'unknown', 'reachability': 'unknown', 'permission': True,
                      'transport': None})
    phone = configured and summary.get('kind') == sip_trunk.PHONE_SYSTEM
    # A phone system is reached on the local network: no internet address to look up.
    network = await probe_network(values.sip_trunk_preset) if configured and not phone else None
    carrier = summary.get('preset_label') if configured and summary.get('preset') != 'custom' else 'the carrier'
    ports_text = _ports_text(values, network) if configured and not phone else None
    last = await run_lifecycle_step(lambda: _last_call(_records(request))) if configured else None
    # IP authentication has no registration; its calls use the trunk's transport.
    transport = asterisk['transport'] or (summary.get('transport') if asterisk['registration'] == 'not_used' else None)
    changed = configured and applied and await run_lifecycle_step(lambda: _address_changed(values, network))
    managed = configured and await run_lifecycle_step(lambda: sip_trunk.engine_managed(values))
    lan = None
    if phone:
        hidden = bool(managed and await address_hidden())
        lan, ports_text = await run_lifecycle_step(lambda: _phone_system_reach(values, managed, hidden))
    in_use = bool(managed and await run_lifecycle_step(lambda: sip_trunk.engine_uses_current(values)))
    restarting = bool(configured and _restarting())
    handover = await run_lifecycle_step(lambda: _handover(values, managed, last)) if configured else None
    observed = await run_lifecycle_step(lambda: _observed(_records(request))) if configured else None
    off = await run_lifecycle_step(lambda: sip_fax_mode.reason_for(values, _records(request))) if configured else None
    if configured and not phone:
        summary['advertised_address'] = await run_lifecycle_step(lambda: sip_trunk.applied_public_address(values)) or None
    message = _message(summary, asterisk, applied, ports_text, transport, managed=managed, in_use=in_use,
                       restarting=restarting)
    if (changed and not restarting and ports_text != BEHIND_ROUTER and asterisk['connected'] and asterisk['permission']
            and asterisk['registration'] != 'rejected'):
        message = ADDRESS_CHANGED_MANAGED if managed else ADDRESS_CHANGED
    if last and last['verdict'] == NOT_HANDED_OVER:
        # A received fax waits outside Faxbot; that matters more than any trunk detail.
        message = last['summary']
    return {
        **summary, 'applied': applied, 'asterisk_connected': asterisk['connected'],
        'registration': asterisk['registration'], 'registration_transport': transport,
        'registration_text': _registration_text(asterisk['registration'], transport, summary.get('preset')),
        'reachability': asterisk['reachability'],
        'reachability_text': _reachability_text(asterisk, phone),
        'round_trip_ms': asterisk.get('round_trip_ms'),
        'internet_address': network.public_ip if network else None,
        'behind_router': network.behind_nat if network else None,
        'port_numbers': network.ports if network else None,
        'public_address_text': (_address_text(summary, network, carrier, observed, not values.sip_t38_enabled)
                                if configured and not phone else None),
        # A phone system: where it reaches Faxbot on the local network ({address, sip_port, media_ports,
        # faxes_at_once}), or None while Faxbot is not published there (then the command that publishes it).
        'phone_system': ({key: lan[key] for key in ('address', 'sip_port', 'media_ports', 'faxes_at_once')}
                         if lan else None),
        'phone_system_command': PHONE_SYSTEM_COMMAND if phone and ports_text == LAN_NOT_STARTED else None,
        'phone_system_setting': PHONE_SYSTEM_SETTING if phone and ports_text == LAN_NOT_STARTED else None,
        # Docker Desktop or Colima replaces the phone system's address, so it cannot connect from this host.
        'phone_system_hidden': bool(phone and ports_text == LAN_HIDDEN),
        # Why new calls use audio fax when Faxbot chose it ({reason, at}); None when T.38 is on or a person chose.
        't38_off_reason': off['reason'] if off else None,
        't38_off_at': off['at'] if off else None,
        'ports_text': ports_text,
        'last_call_text': last['summary'] if last else None,
        'last_call_at': last['started_at'] if last else None,
        'last_call_verdict': last['verdict'] if last else None,
        # After a T.38 call carried no fax data, audio fax is the next thing to try (the owner decides).
        'suggest_audio': bool(last and last['verdict'] == 'no_t38_data_back' and values.sip_t38_enabled),
        'address_changed': bool(changed),
        # Asterisk shares Faxbot's data folder (the Compose install), so Apply and connect restarts it.
        'engine_managed': bool(managed),
        'engine_restarting': restarting,
        'in_use': in_use,
        # Received faxes over the trunk: ready, or what keeps them from Faxbot (None when the trunk does not receive).
        'handover_ready': handover['ready'] if handover else None,
        'handover_text': handover['text'] if handover else None,
        'message': message,
    }


def _records(request):
    try:
        return SipCallRecords(_engine(request))
    except HTTPException:
        return None


@router.post('/apply')
async def apply(request: Request, identity=Depends(require_permission('providers:write'))):
    """Write the trunk configuration Asterisk loads when it starts."""
    values = configuration_values()
    if not sip_trunk.configured(values):
        raise HTTPException(400, detail='Choose a carrier before applying trunk settings.')
    network = None
    phone = _phone_system(values.sip_trunk_preset)
    if not values.sip_external_address and not phone:
        network = await probe_network(values.sip_trunk_preset, fresh=True)
        # A carrier that signs in by address sends calls to a fixed public address, which a router does not pass on.
        if values.sip_trunk_auth == 'ip' and network and network.behind_nat:
            raise HTTPException(400, detail=BEHIND_ROUTER)
    runtime = _runtime(request)
    # T.38 already off after a call that got no fax data back: that call is the reason, not a person's choice.
    await run_lifecycle_step(lambda: sip_fax_mode.derive(values, _records(request)))
    has_calls = await run_lifecycle_step(lambda: _last_call(_records(request)) is not None)
    if sip_fax_mode.network_prefers_audio(values, network, has_calls=has_calls):
        # A new Telnyx trunk on a network that changes port numbers: T.38 data was seen not to come back there.
        values = await run_lifecycle_step(lambda: _audio_for_network(runtime))
    elif sip_fax_mode.carrier_prefers_audio(values, has_calls=has_calls):
        # A new trunk with a carrier that turns T.38 into audio fax inside its own network.
        values = await run_lifecycle_step(lambda: _audio_for_network(runtime, sip_fax_mode.CARRIER))
    else:
        await run_lifecycle_step(lambda: sip_fax_mode.reconcile(values))
    try:
        # Received faxes reach Faxbot with this secret; Faxbot creates it when none is set.
        secret = await run_lifecycle_step(lambda: ensure_inbound_secret(_runtime(request).manager))
        await run_lifecycle_step(lambda: sip_trunk.write_asterisk_configuration(values, inbound_secret=secret))
        if values.ami_password not in ('', 'changeme'):
            # The manager login Faxbot uses now, for an Asterisk that has none yet or an older one.
            await run_lifecycle_step(lambda: sip_trunk.write_manager_credentials(values))
        if not values.sip_external_address and not phone:
            # What Asterisk advertises at its next start (only on a network that keeps port numbers).
            await run_lifecycle_step(lambda: sip_trunk.write_public_address(values, network))
    except sip_trunk.TrunkConfigurationError as error:
        names = ', '.join(_FIELD_NAMES.get(field, 'trunk settings') for field in error.fields)
        raise HTTPException(400, detail=f'Fill in the {names} before applying.')
    except OSError:
        raise HTTPException(500, detail='Faxbot could not save the trunk settings for Asterisk.') from None
    except (AcquisitionError, ConfigurationStoreError):
        raise HTTPException(503, detail='Faxbot could not save an inbound secret for the fax engine. Try again.') from None
    return await _load_into_engine(values)


def _audio_for_network(runtime, reason=sip_fax_mode.NETWORK):
    snapshot = runtime.manager.store.read()
    runtime.manager.patch(snapshot, {'sip_t38_enabled': False}, actor='system')
    values = runtime.manager.store.read().active.values
    sip_fax_mode.write(values, 'audio', reason)
    return values


async def _load_into_engine(values):
    """Have Asterisk use the files just written: restart it when it shares Faxbot's data folder.

    Transports (protocol, bind, external addresses) never reload in a running
    Asterisk, so a restart is the one way that always loads every setting. The
    restart waits until no call is up and happens only when Asterisk is not
    already running exactly these settings.
    """
    from .ami import ami_client
    if not await run_lifecycle_step(lambda: sip_trunk.engine_managed(values)):
        return {'ok': True, 'engine': 'manual', 'message': SAVED_MANUAL}
    if await run_lifecycle_step(lambda: sip_trunk.engine_uses_current(values)) and not _restarting():
        return {'ok': True, 'engine': 'current', 'message': SAVED_CURRENT}
    if not ami_client._connected.is_set():
        return {'ok': True, 'engine': 'not_connected',
                'message': 'Saved. ' + (ami_client.engine_message() or ENGINE_UNREACHABLE)}
    try:
        if await ami_client.active_calls():
            return {'ok': True, 'engine': 'busy', 'message': SAVED_BUSY}
        if not await ami_client.stop_gracefully():
            return {'ok': True, 'engine': 'not_allowed', 'message': SAVED_NOT_ALLOWED}
    except PermissionError:
        return {'ok': True, 'engine': 'not_allowed', 'message': SAVED_NOT_ALLOWED}
    except (ConnectionError, TimeoutError):
        return {'ok': True, 'engine': 'not_connected', 'message': 'Saved. ' + ENGINE_UNREACHABLE}
    _restart['at'] = time.monotonic()
    try:
        from .audit import audit_event
        audit_event('sip_engine_restart', backend='sip')
    except Exception:
        pass
    return {'ok': True, 'engine': 'restarting', 'message': RESTARTING}


@router.get('/calls')
async def calls(request: Request, cursor: str | None = Query(default=None, max_length=200),
                limit: int = Query(default=25, ge=1, le=200),
                direction: str | None = Query(default=None, pattern='^(outbound|inbound)$'),
                identity=Depends(require_permission('diagnostics:read'))):
    """Recent trunk calls, newest first; pass next_cursor to read further back."""
    records = SipCallRecords(_engine(request))
    try:
        return await run_lifecycle_step(lambda: records.page(cursor=cursor, limit=limit, direction=direction))
    except ValueError:
        raise HTTPException(400, detail='That page of calls is not available.') from None
    except SipCallRecordError:
        raise HTTPException(503, detail='Call records are not available right now.') from None


# With the check turned off, look again this often for the setting to change.
_IDLE_CHECK_MINUTES = 1


async def _current_values(values_source):
    """The installation's current values; a store read runs off the event loop."""
    if values_source is None:
        return configuration_values()
    return await run_lifecycle_step(values_source)


async def watch_public_address(*, minutes=None, values_source=None):
    """Probe again every few minutes and record a changed internet address for Asterisk's next start.

    The trunk setting sip_public_address_check_minutes (5 by default, 0 turns the
    check off) sets the pace and is read again before every wait, so a change
    applies from the next check. ``minutes`` fixes the pace instead (tests).
    A probe that finds no address leaves the record alone; Check trunk status
    says when Asterisk needs a restart to advertise the new address.
    """
    if minutes is not None and minutes <= 0:
        return
    while True:
        pace = minutes
        if pace is None:
            try:
                pace = (await _current_values(values_source)).sip_public_address_check_minutes
            except asyncio.CancelledError:
                raise
            except Exception:
                pace = 0
        await asyncio.sleep((pace if pace > 0 else _IDLE_CHECK_MINUTES) * 60)
        if pace <= 0:
            continue
        try:
            values = await _current_values(values_source)
            if minutes is None and values.sip_public_address_check_minutes <= 0:
                continue
            if (not sip_trunk.configured(values) or values.sip_external_address
                    or _phone_system(values.sip_trunk_preset)):
                continue
            network = await probe_network(values.sip_trunk_preset, fresh=True)
            if network and network.public_ip:
                await run_lifecycle_step(lambda: sip_trunk.write_public_address(values, network))
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning('Faxbot could not check its internet address.')
