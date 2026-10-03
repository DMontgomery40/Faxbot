"""Administrative HTTP surface for the SIP trunk: presets, status, apply and recent calls."""
import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .access.route_policy import require_permission
from .config import configuration_values
from .config_runtime import run_lifecycle_step
from . import sip_trunk
from .sip_calls import SipCallRecordError, SipCallRecords


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


def _engine(request):
    runtime = getattr(request.app.state, 'configuration_runtime', None)
    if runtime is None or not runtime.serving:
        raise HTTPException(503, detail='Installation configuration is not ready.')
    return runtime.manager.store.engine


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
              'reachability': 'unknown', 'permission': True}
    if not result['connected']:
        return result
    try:
        if values.sip_trunk_auth == 'registration':
            response, events = await ami_client.status_query(
                {'Action': 'PJSIPShowRegistrationsOutbound'}, collect=True)
            if response['response'].lower() == 'success':
                details = [event for event in events if event.get('ObjectName') == 'trunk-registration']
                status = (details[0].get('Status', '') if details else '').strip().lower()
                result['registration'] = _REGISTRATION_STATES.get(status, 'unknown')
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
    except (ConnectionError, TimeoutError):
        result['connected'] = False
    return result


def _message(summary, asterisk, applied):
    if not summary.get('configured'):
        return 'No SIP trunk is set up. Choose your carrier to start.'
    if summary['missing']:
        return 'Some trunk settings are missing.'
    if not applied:
        return 'Apply these settings to Asterisk, then restart the Asterisk service.'
    if not asterisk['connected']:
        return 'Faxbot is not connected to Asterisk.'
    if not asterisk['permission']:
        return 'Asterisk does not let Faxbot read trunk status. Restart the Asterisk service to update its access.'
    if asterisk['registration'] == 'rejected':
        return _REGISTRATION_TEXT['rejected']
    if asterisk['registration'] == 'not_registered':
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
    applied = await run_lifecycle_step(lambda: _applied(values)) if summary.get('configured') else False
    asterisk = (await _asterisk_status(values) if summary.get('configured')
                else {'connected': False, 'registration': 'unknown', 'reachability': 'unknown', 'permission': True})
    return {
        **summary, 'applied': applied, 'asterisk_connected': asterisk['connected'],
        'registration': asterisk['registration'],
        'registration_text': _REGISTRATION_TEXT[asterisk['registration']],
        'reachability': asterisk['reachability'],
        'reachability_text': _REACHABILITY_TEXT[asterisk['reachability']],
        'message': _message(summary, asterisk, applied),
    }


@router.post('/apply')
async def apply(identity=Depends(require_permission('providers:write'))):
    """Write the trunk configuration Asterisk loads when it starts."""
    values = configuration_values()
    if not sip_trunk.configured(values):
        raise HTTPException(400, detail='Choose a carrier before applying trunk settings.')
    try:
        await run_lifecycle_step(lambda: sip_trunk.write_asterisk_configuration(values))
    except sip_trunk.TrunkConfigurationError as error:
        names = ', '.join(_FIELD_NAMES.get(field, 'trunk settings') for field in error.fields)
        raise HTTPException(400, detail=f'Fill in the {names} before applying.')
    except OSError:
        raise HTTPException(500, detail='Faxbot could not save the trunk settings for Asterisk.') from None
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
