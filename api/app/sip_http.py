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
from . import sip_trunk, stun
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


def _registration_text(registration, transport):
    if registration == 'registered' and transport:
        return f"The carrier accepted Faxbot's registration over {_TRANSPORT_NAMES[transport]}."
    if registration == 'not_used' and transport:
        return ('This trunk uses IP address authentication, so there is no registration; '
                f'calls use {_TRANSPORT_NAMES[transport]}.')
    return _REGISTRATION_TEXT[registration]


BEHIND_ROUTER = ('Your Faxbot runs behind a router, so sign in with a username and password; '
                 'server IP sign-in needs a public address.')
NO_PORTS = 'No ports need to be opened or forwarded.'
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


def _address_text(summary, network, carrier):
    """Faxbot's internet address in use and how the network treats it, in one sentence."""
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
        'configured': True, 'preset': preset.id, 'preset_label': preset.label, 'auth': values.sip_trunk_auth,
        'host': trunk.host if trunk else (values.sip_trunk_host or preset.host),
        'port': trunk.port if trunk else None, 'transport': trunk.transport if trunk else None,
        'missing': missing, 'caller_id_set': bool(values.sip_trunk_caller_id),
        'dids': list(values.sip_trunk_did_list), 't38': values.sip_t38_enabled,
        'fax_preference': values.sip_fax_preference_header,
        'public_address': values.sip_external_address or None,
        'public_address_source': 'typed' if values.sip_external_address else None,
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


def _reachability_text(asterisk):
    milliseconds = asterisk.get('round_trip_ms')
    if asterisk['reachability'] == 'reachable' and milliseconds:
        return f'The carrier answered Faxbot\'s check in {milliseconds} ms.'
    return _REACHABILITY_TEXT[asterisk['reachability']]


def _message(summary, asterisk, applied, ports_text=None, transport=None):
    if not summary.get('configured'):
        return 'No SIP trunk is set up. Choose your carrier to start.'
    if summary['missing']:
        return 'Some trunk settings are missing.'
    if ports_text == BEHIND_ROUTER:
        return BEHIND_ROUTER
    if not applied:
        return 'Apply these settings to Asterisk, then restart the Asterisk service.'
    if not asterisk['connected']:
        return asterisk.get('engine_message') or ENGINE_UNREACHABLE
    if not asterisk['permission']:
        return 'Asterisk does not let Faxbot read trunk status. Restart the Asterisk service to update its access.'
    if asterisk['registration'] == 'rejected':
        return _REGISTRATION_TEXT['rejected']
    if asterisk['registration'] == 'not_registered':
        if (transport or summary.get('transport')) == 'tls':
            return ('Faxbot is not registered with the carrier yet; if this lasts, the encrypted connection may be '
                    'failing, so switch Transport to TCP and apply again.')
        return _REGISTRATION_TEXT['not_registered']
    if asterisk['reachability'] == 'unreachable':
        return _REACHABILITY_TEXT['unreachable']
    if asterisk['reachability'] == 'reachable':
        return 'The trunk is ready.'
    return 'Faxbot is connected to Asterisk; the carrier has not answered a check yet.'


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
    network = await probe_network(values.sip_trunk_preset) if configured else None
    carrier = summary.get('preset_label') if configured and summary.get('preset') != 'custom' else 'the carrier'
    ports_text = _ports_text(values, network) if configured else None
    last = await run_lifecycle_step(lambda: _last_call(_records(request))) if configured else None
    # IP authentication has no registration; its calls use the trunk's transport.
    transport = asterisk['transport'] or (summary.get('transport') if asterisk['registration'] == 'not_used' else None)
    changed = configured and applied and await run_lifecycle_step(lambda: _address_changed(values, network))
    if configured:
        summary['advertised_address'] = await run_lifecycle_step(lambda: sip_trunk.applied_public_address(values)) or None
    message = _message(summary, asterisk, applied, ports_text, transport)
    if (changed and ports_text != BEHIND_ROUTER and asterisk['connected'] and asterisk['permission']
            and asterisk['registration'] != 'rejected'):
        message = ADDRESS_CHANGED
    if last and last['verdict'] == NOT_HANDED_OVER:
        # A received fax waits outside Faxbot; that matters more than any trunk detail.
        message = last['summary']
    return {
        **summary, 'applied': applied, 'asterisk_connected': asterisk['connected'],
        'registration': asterisk['registration'], 'registration_transport': transport,
        'registration_text': _registration_text(asterisk['registration'], transport),
        'reachability': asterisk['reachability'],
        'reachability_text': _reachability_text(asterisk),
        'round_trip_ms': asterisk.get('round_trip_ms'),
        'internet_address': network.public_ip if network else None,
        'behind_router': network.behind_nat if network else None,
        'port_numbers': network.ports if network else None,
        'public_address_text': _address_text(summary, network, carrier) if configured else None,
        'ports_text': ports_text,
        'last_call_text': last['summary'] if last else None,
        'last_call_at': last['started_at'] if last else None,
        'last_call_verdict': last['verdict'] if last else None,
        # After a T.38 call carried no fax data, audio fax is the next thing to try (the owner decides).
        'suggest_audio': bool(last and last['verdict'] == 'no_t38_data_back' and values.sip_t38_enabled),
        'address_changed': bool(changed),
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
    if not values.sip_external_address:
        network = await probe_network(values.sip_trunk_preset, fresh=True)
        # A carrier that signs in by address sends calls to a fixed public address, which a router does not pass on.
        if values.sip_trunk_auth == 'ip' and network and network.behind_nat:
            raise HTTPException(400, detail=BEHIND_ROUTER)
    try:
        # Received faxes reach Faxbot with this secret; Faxbot creates it when none is set.
        secret = await run_lifecycle_step(lambda: ensure_inbound_secret(_runtime(request).manager))
        await run_lifecycle_step(lambda: sip_trunk.write_asterisk_configuration(values, inbound_secret=secret))
        if not values.sip_external_address:
            # What Asterisk advertises at its next start (only on a network that keeps port numbers).
            await run_lifecycle_step(lambda: sip_trunk.write_public_address(values, network))
    except sip_trunk.TrunkConfigurationError as error:
        names = ', '.join(_FIELD_NAMES.get(field, 'trunk settings') for field in error.fields)
        raise HTTPException(400, detail=f'Fill in the {names} before applying.')
    except OSError:
        raise HTTPException(500, detail='Faxbot could not save the trunk settings for Asterisk.') from None
    except (AcquisitionError, ConfigurationStoreError):
        raise HTTPException(503, detail='Faxbot could not save an inbound secret for the fax engine. Try again.') from None
    return {'ok': True, 'message': 'Saved for Asterisk. Restart the Asterisk service to use these settings.'}


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


def _check_minutes():
    try:
        return max(0, int(os.environ.get('SIP_PUBLIC_ADDRESS_CHECK_MINUTES', '5')))
    except ValueError:
        return 5


async def watch_public_address(*, minutes=None, values_source=None):
    """Probe again every few minutes and record a changed internet address for Asterisk's next start.

    SIP_PUBLIC_ADDRESS_CHECK_MINUTES (default 5, 0 turns it off) sets the pace.
    A probe that finds no address leaves the record alone; Check trunk status
    says when Asterisk needs a restart to advertise the new address.
    """
    minutes = _check_minutes() if minutes is None else minutes
    if minutes <= 0:
        return
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            values = (values_source or configuration_values)()
            if not sip_trunk.configured(values) or values.sip_external_address:
                continue
            network = await probe_network(values.sip_trunk_preset, fresh=True)
            if network and network.public_ip:
                await run_lifecycle_step(lambda: sip_trunk.write_public_address(values, network))
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning('Faxbot could not check its internet address.')
