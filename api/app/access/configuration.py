"""Classify fully normalized configuration changes without returning secrets.

Call after candidate validation, alias resolution and preserved-secret merging,
before persisting the candidate. The entry route still requires its operation
permission even for a no-op. This result adds field-sensitive requirements; it
does not authenticate or grant authority itself.
"""
from dataclasses import dataclass
import json

from pydantic import AliasChoices

from ..config_values import ConfigurationValues
from ..config_profiles import ConfigurationDocument


# Everything not explicitly ordinary is Owner-protected, including future fields.
# These sets classify canonical values, never submitted aliases or secret masks.
_ORDINARY_FIELDS = frozenset({
    'route_min_success_percent', 'intake_email_subject', 'work_acknowledge_hours',
    'max_file_size_mb', 'fax_disabled', 'fax_header', 'fax_station_id', 'fax_default_country',
    'artifact_ttl_days', 'cleanup_interval_minutes', 'inbound_retention_days', 'time_zone',
})
_PROVIDER_FIELDS = frozenset({
    'fax_backend', 'outbound_backend', 'inbound_backend',
    'ami_host', 'ami_port', 'ami_username', 'ami_password',
    'fs_esl_host', 'fs_esl_port', 'fs_esl_password', 'fs_gateway_name',
    'fs_caller_id_number', 'fs_t38_enable',
    'sip_trunk_preset', 'sip_trunk_auth', 'sip_trunk_host', 'sip_trunk_port', 'sip_trunk_transport',
    'sip_trunk_username', 'sip_trunk_password', 'sip_trunk_outbound_proxy', 'sip_trunk_caller_id',
    'sip_trunk_dids', 'sip_t38_enabled', 'sip_fax_preference_header', 'sip_trunk_codecs',
    'sip_t38_error_correction', 'sip_t38_max_datagram', 'sip_fax_max_rate', 'sip_fax_ecm', 'sip_fax_compression',
    'sip_fax_fine', 'sip_sslfax_enabled', 'sip_fax_lines', 'sip_sslfax_listener_port',
    'sip_trunk_dial_format', 'sip_trunk_dial_prefix',
    'sip_external_address', 'sip_public_address_check_minutes', 'sip_router_ports', 'telnyx_api_key',
    'phaxio_api_key', 'phaxio_api_secret', 'phaxio_callback_token',
    'phaxio_status_callback_url',
    'sinch_base_url', 'sinch_project_id', 'sinch_api_key', 'sinch_api_secret',
    'signalwire_space_url', 'signalwire_project_id', 'signalwire_api_token',
    'signalwire_fax_from_e164', 'signalwire_sms_from_e164',
    'signalwire_status_callback_url', 'signalwire_webhook_signing_key',
    'signalwire_status_poll_seconds',
    'documo_api_key', 'documo_base_url', 'documo_use_sandbox',
    'humblefax_access_key', 'humblefax_secret_key', 'humblefax_from_number',
    'efax_app_id', 'efax_api_key', 'efax_user_id', 'efax_caller_id', 'efax_csid', 'efax_poll_seconds',
    'efax_delete_after_download', 'efax_webhook_secret',
    'outbound_routes', 'direct_delivery_enabled', 'direct_organization', 'direct_fax_number',
    'intake_email_enabled', 'intake_smtp_host', 'intake_smtp_port', 'intake_smtp_security',
    'intake_smtp_username', 'intake_smtp_password', 'intake_email_from', 'intake_email_to',
    'inbound_enabled', 'asterisk_inbound_secret', 'sinch_inbound_basic_user',
    'sinch_inbound_basic_pass',
    'storage_backend', 's3_bucket', 's3_prefix', 's3_region', 's3_endpoint_url',
    's3_kms_key_id', 'enable_s3_diagnostics',
})
# Owner-protected like public_api_url: mobile_local_base is where paired phones send
# their keys, and docs_base_url is where the console's help links send people.


@dataclass(frozen=True)
class ConfigurationRequirements:
    changed_fields: tuple[str, ...]
    permissions: frozenset[str]
    requires_complete_owner: bool
    plugin_categories: tuple[str, ...] = ()
    profile_drift: bool = False


def _policy_values(values: ConfigurationValues) -> dict:
    environment = values.to_environment()
    result = {}
    for name, value in values.model_dump().items():
        alias = type(values).model_fields[name].validation_alias
        key = alias.choices[0] if isinstance(alias, AliasChoices) else alias
        # Equal effective credentials can still change whether future edits
        # inherit a fallback. Compare canonical presence as well as the value.
        result[name] = (value, key in environment)
    return result


def owner_only_fields() -> frozenset[str]:
    """Settings only the installation's owner may change, by the names a settings change sends."""
    names = set()
    for name, field in ConfigurationValues.model_fields.items():
        if name in _ORDINARY_FIELDS or name in _PROVIDER_FIELDS:
            continue
        extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
        names.add(extra.get('patch_name', name))
    return frozenset(names)


def configuration_requirements(
    before: ConfigurationValues, after: ConfigurationValues,
) -> ConfigurationRequirements:
    if not isinstance(before, ConfigurationValues) or not isinstance(after, ConfigurationValues):
        raise ValueError('Configuration policy requires validated configuration values.')
    old, new = _policy_values(before), _policy_values(after)
    missing = object()
    changed = tuple(sorted(name for name in old.keys() | new.keys()
                           if old.get(name, missing) != new.get(name, missing)))
    permissions = frozenset(
        'settings:write' if name in _ORDINARY_FIELDS
        else 'providers:write' if name in _PROVIDER_FIELDS
        else 'owner:recover'
        for name in changed
    )
    return ConfigurationRequirements(changed, permissions, 'owner:recover' in permissions)


def configuration_candidate_requirements(before, after, before_plugins, after_plugins, *, profile_drift):
    """Union canonical scalar, document and captured-declaration requirements.

    Provider settings are private arbitrary JSON. Results expose only closed
    categories, never their keys, values or value-derived fingerprints.
    """
    if (type(before_plugins) is not ConfigurationDocument or type(after_plugins) is not ConfigurationDocument
            or type(profile_drift) is not bool):
        raise ValueError('Configuration policy requires validated configuration documents.')
    scalar = configuration_requirements(before, after)
    old, new = before_plugins.as_dict(), after_plugins.as_dict()
    categories, permissions = set(), set(scalar.permissions)
    for name in old.keys() | new.keys():
        if _same_json_member(old, new, name):
            continue
        if name == 'settings' and isinstance(old.get(name), dict) and isinstance(new.get(name), dict):
            categories.add('providers')
            permissions.add('providers:write')
        elif name == 'roles' and isinstance(old.get(name), dict) and isinstance(new.get(name), dict):
            previous, candidate = old[name], new[name]
            for role in previous.keys() | candidate.keys():
                if _same_json_member(previous, candidate, role):
                    continue
                if role in {'outbound', 'inbound', 'storage'}:
                    categories.add('providers')
                    permissions.add('providers:write')
                else:
                    categories.add('authentication' if role == 'auth' else 'installation')
                    permissions.add('owner:recover')
        else:
            categories.add('installation')
            permissions.add('owner:recover')
    if profile_drift:
        permissions.add('providers:write')
    return ConfigurationRequirements(scalar.changed_fields, frozenset(permissions),
        'owner:recover' in permissions, tuple(sorted(categories)), profile_drift)


def _same_json_member(before, after, name):
    # Python equates True with 1 and False with 0, including inside dictionaries.
    # Canonical persisted JSON retains those types, so policy must retain them.
    return name in before and name in after and json.dumps(before[name], sort_keys=True,
        ensure_ascii=False, allow_nan=False, separators=(',', ':')) == json.dumps(after[name],
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
