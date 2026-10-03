"""Pure, redacted editor views of canonical configuration snapshots.

The editor uses the desired revision; runtime consumers use the active revision.
Presence flags describe local configuration only, not remote account validation.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config_store import ConfigurationSnapshot


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


def project_admin_settings(snapshot: ConfigurationSnapshot, pending_fields: Iterable[str] = ()) -> dict[str, Any]:
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
            'from_fax': mask_secret(values.signalwire_fax_from_e164),
            'from_sms': mask_secret(values.signalwire_sms_from_e164),
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
            'station_id': mask_secret(values.fax_station_id),
            'configured': bool(values.ami_username and values.ami_password),
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
        },
    }
