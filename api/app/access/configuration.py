"""Classify fully normalized configuration changes without returning secrets.

Call after candidate validation, alias resolution and preserved-secret merging,
before persisting the candidate. The entry route still requires its operation
permission even for a no-op. This result adds field-sensitive requirements; it
does not authenticate or grant authority itself.
"""
from dataclasses import dataclass

from pydantic import AliasChoices

from ..config_values import ConfigurationValues


# Everything not explicitly ordinary is Owner-protected, including future fields.
# These sets classify canonical values, never submitted aliases or secret masks.
_ORDINARY_FIELDS = frozenset({
    'max_file_size_mb', 'fax_disabled', 'fax_header', 'fax_station_id',
    'artifact_ttl_days', 'cleanup_interval_minutes', 'inbound_retention_days',
})
_PROVIDER_FIELDS = frozenset({
    'fax_backend', 'outbound_backend', 'inbound_backend',
    'ami_host', 'ami_port', 'ami_username', 'ami_password',
    'fs_esl_host', 'fs_esl_port', 'fs_esl_password', 'fs_gateway_name',
    'fs_caller_id_number', 'fs_t38_enable',
    'phaxio_api_key', 'phaxio_api_secret', 'phaxio_callback_token',
    'phaxio_status_callback_url',
    'sinch_base_url', 'sinch_project_id', 'sinch_api_key', 'sinch_api_secret',
    'signalwire_space_url', 'signalwire_project_id', 'signalwire_api_token',
    'signalwire_fax_from_e164', 'signalwire_sms_from_e164',
    'signalwire_status_callback_url', 'signalwire_webhook_signing_key',
    'signalwire_status_poll_seconds',
    'documo_api_key', 'documo_base_url', 'documo_use_sandbox',
    'inbound_enabled', 'asterisk_inbound_secret', 'sinch_inbound_basic_user',
    'sinch_inbound_basic_pass', 'sinch_inbound_hmac_secret',
    'storage_backend', 's3_bucket', 's3_prefix', 's3_region', 's3_endpoint_url',
    's3_kms_key_id',
})


@dataclass(frozen=True)
class ConfigurationRequirements:
    changed_fields: tuple[str, ...]
    permissions: frozenset[str]
    requires_complete_owner: bool


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
