"""Pure, redacted editor views of canonical configuration snapshots.

The editor uses the desired revision; runtime consumers use the active revision.
Presence flags describe local configuration only, not remote account validation.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from .routing.numbers import SUPPORTED_COUNTRIES, number_example

if TYPE_CHECKING:
    from .config_store import ConfigurationSnapshot


# Environment-only settings the console shows read-only (System → Security, Storage & retention,
# Diagnostics, Developer, Terminal and the trunk's advanced box). They are read from the process
# environment at start and are never changed from the console or the command line.
DEPLOYMENT_VARIABLES = (
    'FAXBOT_ALLOW_INSECURE_HTTP_SESSIONS', 'FAXBOT_CONSOLE_ORIGINS', 'ENABLE_LOCAL_ADMIN', 'ENABLE_ADMIN_EXEC',
    'FAXBOT_ALLOW_INSECURE_LOOPBACK', 'FAXBOT_INSTALLATION_KEY_PATH', 'FAXBOT_DIRECT_KEY_PATH',
    'FAXBOT_MEDIA_PORTS', 'FAXBOT_PHONE_SYSTEM_ADDRESS',
    'MCP_ALLOWED_HOSTS', 'MCP_ALLOWED_ORIGINS', 'MCP_OAUTH_SUBJECT_KEYS_FILE', 'MCP_RESOURCE_URL',
    'MCP_HTTP_PORT', 'TZ',
)
# Whether these are set is shown; their values never are.
SECRET_DEPLOYMENT_VARIABLES: frozenset[str] = frozenset()
_TRUE = {'1', 'true', 'yes'}


def deployment_view(environment: Mapping[str, str]) -> dict[str, Any]:
    """{variable: {set, value}} for the environment-only settings; a secret's value is never included."""
    view = {}
    for name in DEPLOYMENT_VARIABLES:
        raw = environment.get(name)
        present = isinstance(raw, str) and raw.strip() != ''
        view[name] = {'set': present,
                      'value': raw.strip()[:512] if present and name not in SECRET_DEPLOYMENT_VARIABLES else None}
    # The terminal is on when ENABLE_ADMIN_EXEC says so, or, when it is not set, when the console is served here.
    exec_value, local = environment.get('ENABLE_ADMIN_EXEC'), environment.get('ENABLE_LOCAL_ADMIN', 'false')
    view['ENABLE_ADMIN_EXEC']['effective'] = (exec_value.lower() in _TRUE if exec_value is not None
                                              else local.lower() in _TRUE)
    return view


def _owner_only() -> list[str]:
    from .access.configuration import owner_only_fields
    return sorted(owner_only_fields())


def _capacity_view(values) -> dict:
    """The trunk's calls at once and new calls a second in effect, and the carrier's published limits."""
    from .capacity import CARRIERS, calls_per_second, trunk_calls_at_once
    limits = CARRIERS.get(values.sip_trunk_preset or '')
    return {'max_calls_in_effect': trunk_calls_at_once(values), 'calls_per_second_in_effect': calls_per_second(values),
            'carrier_limits': None if limits is None else {
                'calls_per_second': limits.calls_per_second, 'calls_at_once': limits.calls_at_once,
                'note': limits.note, 'sources': list(limits.sources), 'read_on': limits.read_on}}


def _audio_reason(values) -> dict:
    from .sip_fax_mode import reason_for
    try:
        found = reason_for(values)
    except (TypeError, ValueError, OSError):
        found = None
    return {'t38_off_reason': found['reason'] if found else None, 't38_off_at': found['at'] if found else None}


def _engine_login_shared(values) -> bool:
    from .sip_trunk import manager_credentials_shared
    try:
        return manager_credentials_shared(values)
    except (TypeError, ValueError):
        return False


def _humblefax_numbers(values) -> tuple:
    # The only view value read from outside the snapshot: HumbleFax's cached answer, never the keys.
    from .humblefax_service import account_numbers
    return account_numbers(values.humblefax_access_key, values.humblefax_secret_key) or ()


def _freeswitch_caller_id_missing() -> str:
    from .freeswitch_service import CALLER_ID_MISSING
    return CALLER_ID_MISSING


def mask_secret(value: str) -> str:
    """Do not disclose a secret's suffix or length; preserve an explicit clear."""
    return '***' if value else ''


def _database_view(url: str) -> dict[str, Any]:
    # Only known scheme names may leave this boundary. Parsing/removing a URL's
    # password alone still exposes query credentials, paths, or private hosts.
    scheme = url.partition(':')[0].partition('+')[0].lower() if ':' in url else ''
    scheme = {'sqlite': 'sqlite', 'postgresql': 'postgresql', 'postgres': 'postgresql'}.get(scheme, 'unknown') if url else ''
    return {
        'url': mask_secret(url),
        'scheme': scheme,
        'persistent': scheme == 'sqlite' and url.partition(':')[2].startswith('////faxdata/'),
        'editable': False,
        'maintenance_required': True,
    }


def project_admin_settings(snapshot: ConfigurationSnapshot, pending_fields: Iterable[str] = (),
                           env_managed: Iterable[str] = (),
                           environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Retain the legacy nested shape while exposing desired/active identity.

    Callers can add existing change hints to the returned ``_meta`` dictionary.
    Neither process environment nor mutable runtime settings are consulted; the
    environment-only settings come from ``environment`` when the caller passes it.
    """
    values = snapshot.desired.values
    return {
        'backend': {'type': values.fax_backend, 'disabled': values.fax_disabled},
        'hybrid': {
            'outbound_backend': values.effective_outbound,
            'inbound_backend': values.effective_inbound,
            'outbound_override': values.outbound_backend,
            'inbound_override': values.inbound_backend,
            'outbound_explicit': bool(values.outbound_backend),
            'inbound_explicit': bool(values.inbound_backend),
        },
        'phaxio': {
            'api_key': mask_secret(values.phaxio_api_key),
            'api_secret': mask_secret(values.phaxio_api_secret),
            'callback_token': mask_secret(values.phaxio_callback_token),
            'callback_url': values.phaxio_status_callback_url,
            'verify_signature': values.phaxio_verify_signature,
            'configured': bool(values.phaxio_api_key and values.phaxio_api_secret),
        },
        'documo': {
            'api_key': mask_secret(values.documo_api_key),
            'base_url': values.documo_base_url,
            'sandbox': values.documo_use_sandbox,
            'configured': bool(values.documo_api_key),
        },
        'humblefax': {
            'access_key': mask_secret(values.humblefax_access_key),
            'secret_key': mask_secret(values.humblefax_secret_key),
            'from_number': values.humblefax_from_number,
            # Receiving: the switch, and how often Faxbot asks HumbleFax for received faxes.
            'receive': values.humblefax_receive_enabled,
            'poll_seconds': values.humblefax_poll_seconds,
            # The account's own numbers as HumbleFax reports them (cached read; empty until known).
            'account_numbers': list(_humblefax_numbers(values)),
            'configured': bool(values.humblefax_access_key and values.humblefax_secret_key),
        },
        'efax': {
            'app_id': mask_secret(values.efax_app_id),
            'api_key': mask_secret(values.efax_api_key),
            'user_id': mask_secret(values.efax_user_id),
            'caller_id': values.efax_caller_id,
            'csid': values.efax_csid,
            'poll_seconds': values.efax_poll_seconds,
            'delete_after_download': values.efax_delete_after_download,
            'webhook_secret': mask_secret(values.efax_webhook_secret),
            'webhook_secret_set': bool(values.efax_webhook_secret),
            'configured': bool(values.efax_app_id and values.efax_api_key and values.efax_user_id),
        },
        'sinch': {
            'project_id': values.sinch_project_id,
            'base_url': values.sinch_base_url,
            'api_key': mask_secret(values.sinch_api_key),
            'api_secret': mask_secret(values.sinch_api_secret),
            'configured': bool(values.sinch_project_id and values.sinch_api_key and values.sinch_api_secret),
            'webhook_base_url': values.sinch_webhook_base_url,
            'incoming_webhook_url': values.sinch_incoming_webhook_url,
            'incoming_webhook_login_url': values.sinch_incoming_webhook_login_url,
        },
        'signalwire': {
            'space_url': values.signalwire_space_url,
            'project_id': values.signalwire_project_id,
            'api_token': mask_secret(values.signalwire_api_token),
            'from_fax': values.signalwire_fax_from_e164,
            'from_sms': values.signalwire_sms_from_e164,
            'callback_url': values.signalwire_status_callback_url,
            'webhook_signing_key': mask_secret(values.signalwire_webhook_signing_key),
            'status_poll_seconds': values.signalwire_status_poll_seconds,
            'configured': bool(values.signalwire_space_url and values.signalwire_project_id and values.signalwire_api_token),
        },
        'fs': {
            'esl_host': values.fs_esl_host,
            'esl_port': values.fs_esl_port,
            'esl_password': mask_secret(values.fs_esl_password),
            'gateway_name': values.fs_gateway_name,
            'caller_id_number': values.fs_caller_id_number,
            'problem': None if values.fs_caller_id_number else _freeswitch_caller_id_missing(),
            't38_enable': values.fs_t38_enable,
        },
        'sip': {
            'ami_host': values.ami_host,
            'ami_port': values.ami_port,
            'ami_username': values.ami_username,
            'ami_password': mask_secret(values.ami_password),
            'ami_password_is_default': values.ami_password == 'changeme',
            # Faxbot has written this login where its own Asterisk reads it.
            'ami_password_shared': _engine_login_shared(values),
            # A fax number, not a secret: the person sees and edits the stored number.
            'station_id': values.fax_station_id,
            'configured': bool(values.ami_username and values.ami_password),
            'trunk': {
                'preset': values.sip_trunk_preset,
                'auth': values.sip_trunk_auth,
                'host': values.sip_trunk_host,
                'port': values.sip_trunk_port,
                'transport': values.sip_trunk_transport,
                'username': values.sip_trunk_username,
                'password': mask_secret(values.sip_trunk_password),
                'password_set': bool(values.sip_trunk_password),
                'outbound_proxy': values.sip_trunk_outbound_proxy,
                'caller_id': values.sip_trunk_caller_id,
                'dids': list(values.sip_trunk_did_list),
                't38_enabled': values.sip_t38_enabled,
                'fax_preference_header': values.sip_fax_preference_header,
                'codecs': values.sip_trunk_codecs,
                # Fax settings (collapsed on the trunk page): both fax engines use them.
                't38_error_correction': values.sip_t38_error_correction,
                't38_max_datagram': values.sip_t38_max_datagram,
                'fax_max_rate': values.sip_fax_max_rate,
                'fax_ecm': values.sip_fax_ecm,
                'fax_compression': values.sip_fax_compression,
                'fax_fine': values.sip_fax_fine,
                'sslfax_enabled': values.sip_sslfax_enabled,
                'fax_lines': values.sip_fax_lines,
                # Calls at once on the trunk and new calls a second, as set (0: the default) and as in effect.
                'max_calls': values.sip_trunk_max_calls,
                'calls_per_second': values.sip_trunk_calls_per_second,
                **_capacity_view(values),
                'sslfax_listener_port': values.sip_sslfax_listener_port,
                'dial_format': values.sip_trunk_dial_format,
                'dial_prefix': values.sip_trunk_dial_prefix,
                'external_address': values.sip_external_address,
                'public_address_check_minutes': values.sip_public_address_check_minutes,
                'router_ports': values.sip_router_ports,
                # Why Faxbot chose audio fax for new calls ('no_data_back' or 'network'), and when; else None.
                **_audio_reason(values),
            },
            # Lets Faxbot read what Telnyx charged for each call; never shown.
            'telnyx_api_key': mask_secret(values.telnyx_api_key),
            'telnyx_api_key_set': bool(values.telnyx_api_key),
        },
        'security': {
            'api_key': mask_secret(values.api_key),
            'require_api_key': values.require_api_key,
            'enforce_https': values.enforce_public_https,
            'audit_enabled': values.audit_log_enabled,
            'public_api_url': values.public_api_url,
        },
        'audit': {
            'enabled': values.audit_log_enabled,
            'format': values.audit_log_format,
            'file': values.audit_log_file,
            'syslog': values.audit_log_syslog,
            'syslog_address': values.audit_log_syslog_address,
        },
        'mcp': {
            'sse_enabled': values.enable_mcp_sse,
            'sse_path': values.mcp_sse_path,
            'http_enabled': values.enable_mcp_http,
            'http_path': values.mcp_http_path,
            'require_oauth': values.require_mcp_oauth,
            'oauth': {'issuer': values.oauth_issuer, 'audience': values.oauth_audience, 'jwks_url': values.oauth_jwks_url},
        },
        'persisted': {'enabled': values.enable_persisted_settings, 'path': values.persisted_env_path},
        # The older settings file, read once when a new installation first starts; shown read-only.
        'legacy_config': {'path': values.faxbot_config_path},
        'features': {
            'v3_plugins': values.feature_v3_plugins,
            'fax_disabled': values.fax_disabled,
            'inbound_enabled': values.inbound_enabled,
            'plugin_install': values.feature_plugin_install,
        },
        'storage': {
            'backend': values.storage_backend,
            's3_bucket': values.s3_bucket,
            's3_prefix': values.s3_prefix,
            's3_region': values.s3_region,
            's3_endpoint_url': values.s3_endpoint_url,
            's3_kms_key_id': values.s3_kms_key_id,
            's3_kms_enabled': bool(values.s3_kms_key_id),
            's3_diagnostics': values.enable_s3_diagnostics,
        },
        'database': _database_view(values.database_url),
        'installation': {'time_zone': values.time_zone},
        'mobile': {'local_base': values.mobile_local_base},
        'developer': {'docs_base_url': values.docs_base_url},
        # Whether the console may restart Faxbot (it exits and its service manager starts it again).
        'restart': {'allowed': values.admin_allow_restart},
        # Where provider plugin files are read from; shown read-only.
        'plugin_files': {'providers_dir': values.providers_dir},
        # Environment-only settings, shown read-only; never a secret's value.
        'deployment': deployment_view(environment or {}),
        # Settings only the owner may change; the console shows them disabled to everyone else.
        'owner_only': _owner_only(),
        'numbers': {
            'default_country': values.fax_default_country,
            'example': number_example(values.fax_default_country),
            'supported_countries': list(SUPPORTED_COUNTRIES),
        },
        'routing': {
            'outbound_routes': values.outbound_routes,
            'min_success_percent': values.route_min_success_percent,
            # Faxes to the installation's own numbers become received faxes here, with no call.
            'local_delivery': values.local_delivery_enabled,
            # Fax-friendly shading on documents you send (pages/friendly.py), and the opt-in to make light areas
            # white (off by default).
            'fax_friendly_documents': values.fax_friendly_documents,
            'fax_friendly_whiten': values.fax_friendly_whiten,
        },
        'intake': {
            'email_enabled': values.intake_email_enabled,
            'smtp_host': values.intake_smtp_host,
            'smtp_port': values.intake_smtp_port,
            'smtp_security': values.intake_smtp_security,
            'smtp_username': values.intake_smtp_username,
            'smtp_password': mask_secret(values.intake_smtp_password),
            'email_from': values.intake_email_from,
            'email_to': values.intake_email_to,
            'email_subject': values.intake_email_subject,
        },
        'work': {
            'acknowledge_hours': values.work_acknowledge_hours,
        },
        # Case checklists: whether Faxbot suggests possible matches for missing items (off by default).
        'cases': {'suggestions': values.case_suggestions_enabled},
        # What every sent fax carries: the header text and the station ID (your fax number).
        'sender': {'header': values.fax_header, 'station_id': values.fax_station_id},
        'direct': {
            'enabled': values.direct_delivery_enabled,
            'organization': values.direct_organization,
            'fax_number': values.direct_fax_number,
            'allow_private_peers': values.direct_allow_private_peers,
        },
        'inbound': {
            'enabled': values.inbound_enabled,
            'retention_days': values.inbound_retention_days,
            'token_ttl_minutes': values.inbound_token_ttl_minutes,
            'sip': {'asterisk_secret': mask_secret(values.asterisk_inbound_secret), 'configured': bool(values.asterisk_inbound_secret)},
            'phaxio': {'verify_signature': values.phaxio_inbound_verify_signature},
            'sinch': {
                'basic_user': values.sinch_inbound_basic_user,
                'basic_pass': mask_secret(values.sinch_inbound_basic_pass),
                'basic_auth_configured': values.sinch_inbound_basic_configured,
            },
        },
        'limits': {
            'max_file_size_mb': values.max_file_size_mb,
            'pdf_token_ttl_minutes': values.pdf_token_ttl_minutes,
            'rate_limit_rpm': values.max_requests_per_minute,
            'inbound_list_rpm': values.inbound_list_rpm,
            'inbound_get_rpm': values.inbound_get_rpm,
            'artifact_ttl_days': values.artifact_ttl_days,
            'cleanup_interval_minutes': values.cleanup_interval_minutes,
        },
        '_meta': {
            'active_revision_id': snapshot.active.id,
            'desired_revision_id': snapshot.desired.id,
            'generation': snapshot.generation,
            'apply_state': 'pending_restart' if snapshot.pending is not None else 'applied',
            'pending_fields': list(pending_fields),
            # Settings whose value comes from the environment (.env) at every start; names only.
            'env_managed': sorted(env_managed),
        },
    }
