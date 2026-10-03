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
