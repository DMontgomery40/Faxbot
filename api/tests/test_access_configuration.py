"""Internal policy classification; GUI/route acceptance is separate."""
import pytest

from api.app.config_values import ConfigurationValues
from api.app.access.configuration import configuration_requirements


def test_bootstrap_secret_replacement_requires_owner_and_never_returns_values():
    before = ConfigurationValues.from_environment({'API_KEY': 'old-synthetic-secret'})
    after = ConfigurationValues.from_environment({'API_KEY': 'new-synthetic-secret'})

    result = configuration_requirements(before, after)

    assert result.changed_fields == ('api_key',)
    assert result.permissions == frozenset({'owner:recover'})
    assert result.requires_complete_owner is True
    assert 'synthetic-secret' not in repr(result)


def test_combined_upload_and_provider_change_requires_both_categories():
    before = ConfigurationValues.from_environment({})
    after = ConfigurationValues.from_environment({
        'MAX_FILE_SIZE_MB': '12', 'PHAXIO_CALLBACK_TOKEN': 'provider-synthetic-token',
    })

    result = configuration_requirements(before, after)

    assert result.changed_fields == ('max_file_size_mb', 'phaxio_callback_token')
    assert result.permissions == frozenset({'settings:write', 'providers:write'})
    assert result.requires_complete_owner is False
    assert 'provider-synthetic-token' not in repr(result)


@pytest.mark.parametrize('change', [
    {'API_KEY': ''},
    {'FAX_DATA_DIR': '/private/recovery-export'},
    {'PERSISTED_ENV_PATH': '/private/recovery.env'},
    {'AUDIT_LOG_FILE': '/private/security.log'},
    {'PHAXIO_VERIFY_SIGNATURE': 'false'},
    {'PUBLIC_API_URL': 'https://changed-origin.example'},
    {'ADMIN_ALLOW_RESTART': 'true'},
    {'FEATURE_PLUGIN_INSTALL': 'true'},
    {'OAUTH_JWKS_URL': 'https://changed-identity.example/keys'},
    {'FAXBOT_PROVIDERS_DIR': '/private/replacement-providers'},
])
def test_security_and_host_changes_require_owner_after_alias_resolution(change):
    environment = {'API_KEY': 'synthetic-bootstrap'}
    before = ConfigurationValues.from_environment(environment)
    after = ConfigurationValues.from_environment({**environment, **change})

    result = configuration_requirements(before, after)

    assert result.changed_fields
    assert result.permissions == frozenset({'owner:recover'})
    assert result.requires_complete_owner is True


def test_unchanged_normalized_values_do_not_require_owner_again():
    before = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'API_KEY': 'synthetic'})
    after = ConfigurationValues.from_environment({'FAX_BACKEND': ' PHAXIO ', 'API_KEY': 'synthetic'})

    result = configuration_requirements(before, after)

    assert result.changed_fields == ()
    assert result.permissions == frozenset()
    assert result.requires_complete_owner is False


def test_future_fields_fail_closed_until_deliberately_classified():
    class FutureConfiguration(ConfigurationValues):
        remote_administration: bool = False

    result = configuration_requirements(FutureConfiguration(), FutureConfiguration(remote_administration=True))

    assert result.changed_fields == ('remote_administration',)
    assert result.permissions == frozenset({'owner:recover'})
    assert result.requires_complete_owner is True


def test_every_sip_trunk_setting_is_a_provider_setting():
    from api.app.access.configuration import _PROVIDER_FIELDS
    trunk = {name for name in ConfigurationValues.model_fields if name.startswith('sip_')}
    assert trunk == {'sip_trunk_preset', 'sip_trunk_auth', 'sip_trunk_host', 'sip_trunk_port',
                     'sip_trunk_transport', 'sip_trunk_username', 'sip_trunk_password',
                     'sip_trunk_outbound_proxy', 'sip_trunk_caller_id', 'sip_trunk_dids', 'sip_t38_enabled',
                     'sip_fax_preference_header', 'sip_trunk_codecs', 'sip_trunk_dial_format',
                     'sip_trunk_dial_prefix', 'sip_external_address', 'sip_public_address_check_minutes',
                     # Fax settings (both fax engines) and the SSL Fax engine.
                     'sip_t38_error_correction', 'sip_t38_max_datagram', 'sip_fax_max_rate', 'sip_fax_ecm',
                     'sip_fax_compression', 'sip_fax_fine', 'sip_sslfax_enabled', 'sip_fax_lines',
                     'sip_sslfax_listener_port'}
    assert trunk <= _PROVIDER_FIELDS


def test_raw_patch_cannot_be_mistaken_for_normalized_configuration():
    with pytest.raises(ValueError, match='validated configuration values'):
        configuration_requirements(ConfigurationValues(), {'API_KEY': 'synthetic'})


@pytest.mark.parametrize('change', [
    {'DOCUMO_API_KEY': 'synthetic-documo-key'},
    {'DOCUMO_SANDBOX': 'true'},
    {'HUMBLEFAX_ACCESS_KEY': 'synthetic-humblefax-access'},
    {'HUMBLEFAX_SECRET_KEY': 'synthetic-humblefax-secret'},
    {'HUMBLEFAX_FROM_NUMBER': '13035550199'},
    {'HUMBLEFAX_ACCESS_KEY': 'synthetic-humblefax-access', 'HUMBLEFAX_SECRET_KEY': 'synthetic-humblefax-secret'},
    {'SIP_TRUNK_PRESET': 'telnyx', 'SIP_TRUNK_AUTH': 'registration', 'SIP_TRUNK_HOST': 'sip.telnyx.com',
     'SIP_TRUNK_PORT': '5060', 'SIP_TRUNK_TRANSPORT': 'udp', 'SIP_TRUNK_USERNAME': 'faxbotuser',
     'SIP_TRUNK_PASSWORD': 'synthetic-humblefax-trunk', 'SIP_TRUNK_OUTBOUND_PROXY': 'proxy.example.net',
     'SIP_TRUNK_CALLER_ID': '+15555550100', 'SIP_TRUNK_DIDS': '+15555550100', 'SIP_T38_ENABLED': 'false',
     'SIP_FAX_PREFERENCE_HEADER': 'true', 'SIP_TRUNK_CODECS': 'ulaw', 'SIP_EXTERNAL_ADDRESS': '203.0.113.10'},
    {'SIP_TRUNK_PASSWORD': 'synthetic-humblefax-trunk'},
])
def test_cloud_provider_credentials_need_provider_permission_not_owner(change):
    before = ConfigurationValues.from_environment({'API_KEY': 'synthetic-bootstrap'})
    after = ConfigurationValues.from_environment({'API_KEY': 'synthetic-bootstrap', **change})

    result = configuration_requirements(before, after)

    assert result.changed_fields
    assert result.permissions == frozenset({'providers:write'})
    assert result.requires_complete_owner is False
    assert 'synthetic-humblefax' not in repr(result) and 'synthetic-documo' not in repr(result)


@pytest.mark.parametrize(('field', 'environment'), [
    ('sinch_api_key', {}),
    ('sinch_api_key', {'PHAXIO_API_KEY': 'synthetic-inherited'}),
    ('sinch_api_secret', {'PHAXIO_API_SECRET': 'synthetic-inherited'}),
])
def test_same_value_explicit_override_still_changes_provider_inheritance(field, environment):
    before = ConfigurationValues.from_environment(environment)
    after = before.with_patch({field: getattr(before, field)})
    assert before.model_dump() == after.model_dump()

    result = configuration_requirements(before, after)

    assert result.changed_fields == (field,)
    assert result.permissions == frozenset({'providers:write'})
    assert result.requires_complete_owner is False
    assert configuration_requirements(after, before).changed_fields == (field,)


def test_installation_country_is_an_ordinary_setting():
    before = ConfigurationValues.from_environment({})
    after = ConfigurationValues.from_environment({'FAX_DEFAULT_COUNTRY': 'GB'})
    result = configuration_requirements(before, after)
    assert result.changed_fields == ('fax_default_country',)
    assert result.permissions == frozenset({'settings:write'})
    assert result.requires_complete_owner is False
