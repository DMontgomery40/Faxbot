"""Pure, redacted editor views of canonical configuration snapshots.

The editor uses the desired revision; runtime consumers use the active revision.
Presence flags describe local configuration only, not remote account validation.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .routing.numbers import SUPPORTED_COUNTRIES, number_example

if TYPE_CHECKING:
    from .config_store import ConfigurationSnapshot


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
                           env_managed: Iterable[str] = ()) -> dict[str, Any]:
    """Retain the legacy nested shape while exposing desired/active identity.

    Callers can add existing change hints to the returned ``_meta`` dictionary.
    Neither process environment nor mutable runtime settings are consulted.
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
            'configured': bool(values.efax_app_id and values.efax_api_key and values.efax_user_id),
        },
        'sinch': {
            'project_id': values.sinch_project_id,
            'base_url': values.sinch_base_url,
            'api_key': mask_secret(values.sinch_api_key),
            'api_secret': mask_secret(values.sinch_api_secret),
            'configured': bool(values.sinch_project_id and values.sinch_api_key and values.sinch_api_secret),
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
                'external_address': values.sip_external_address,
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
        },
        'database': _database_view(values.database_url),
        'numbers': {
            'default_country': values.fax_default_country,
            'example': number_example(values.fax_default_country),
            'supported_countries': list(SUPPORTED_COUNTRIES),
        },
        'routing': {
            'outbound_routes': values.outbound_routes,
            'min_success_percent': values.route_min_success_percent,
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
                'verify_signature': values.sinch_inbound_verify_signature,
                'basic_user': values.sinch_inbound_basic_user,
                'basic_pass': mask_secret(values.sinch_inbound_basic_pass),
                'hmac_secret': mask_secret(values.sinch_inbound_hmac_secret),
                'basic_auth_configured': bool(values.sinch_inbound_basic_user),
                'hmac_configured': bool(values.sinch_inbound_hmac_secret),
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
