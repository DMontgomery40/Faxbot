"""Configuration values must be complete and literal before activation."""
import os

from app.config_values import ConfigurationValues


def test_explicit_environment_frame_preserves_hybrid_aliases_and_clear_without_process_mutation():
    before = dict(os.environ)
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio',
        'FAX_OUTBOUND_BACKEND': 'sinch',
        'FAX_INBOUND_BACKEND': 'signalwire',
        'PHAXIO_API_KEY': 'synthetic-phaxio-key',
        'PHAXIO_API_SECRET': 'synthetic phaxio\nsecret # with "quotes"',
        'PHAXIO_CALLBACK_URL': 'https://example.invalid/legacy-callback',
        'SINCH_API_KEY': '',
        'SIGNALWIRE_API_TOKEN': 'synthetic-$literal-token',
        'DOCUMO_API_KEY': 'synthetic-inactive-documo',
        'FAX_DISABLED': 'false',
        'MAX_REQUESTS_PER_MINUTE': '0',
    })
    assert values.effective_outbound == 'sinch'
    assert values.effective_inbound == 'signalwire'
    assert values.sinch_api_key == ''  # present empty suppresses legacy fallback
    assert values.sinch_api_secret == 'synthetic phaxio\nsecret # with "quotes"'
    assert values.phaxio_status_callback_url == 'https://example.invalid/legacy-callback'
    assert values.signalwire_api_token == 'synthetic-$literal-token'
    assert values.documo_api_key == 'synthetic-inactive-documo'
    assert values.fax_disabled is False
    assert values.max_requests_per_minute == 0
    assert os.environ == before


def test_invalid_environment_frame_reports_field_without_echoing_secret_values():
    import pytest
    from app.config_values import ConfigurationValueError

    before = dict(os.environ)
    with pytest.raises(ConfigurationValueError) as error:
        ConfigurationValues.from_environment({
            'FAX_DISABLED': 'synthetic-private-invalid-boolean',
            'PHAXIO_API_SECRET': 'synthetic-do-not-expose',
            'ASTERISK_AMI_PORT': '-1',
            'MAX_FILE_SIZE_MB': '0',
        })
    assert {item['field'] for item in error.value.issues} == {
        'FAX_DISABLED', 'ASTERISK_AMI_PORT', 'MAX_FILE_SIZE_MB',
    }
    assert 'synthetic-private' not in str(error.value)
    assert 'synthetic-do-not-expose' not in repr(error.value)
    assert os.environ == before


def test_complete_environment_projection_retains_inactive_values_and_inheritance(monkeypatch):
    from app.config_paths import bundled_config_dir
    monkeypatch.setenv('FAXBOT_CONFIG_PATH', '/unrelated-process-value.json')
    values = ConfigurationValues.from_environment({
        'FAX_BACKEND': ' SIP ',
        'FAX_OUTBOUND_BACKEND': '',
        'PHAXIO_STATUS_CALLBACK_URL': 'https://example.invalid/preferred',
        'PHAXIO_CALLBACK_URL': 'https://example.invalid/legacy',
        'PHAXIO_API_KEY': 'synthetic-fallback-key',
        'SINCH_API_SECRET': '',
        'DOCUMO_API_KEY': 'synthetic-inactive-key',
        'FREESWITCH_ESL_PASSWORD': 'synthetic-telephony-password',
        'SIGNALWIRE_API_TOKEN': 'synthetic-unused-token',
        'ENABLE_MCP_HTTP': 'true',
        'DATABASE_URL': 'postgresql://synthetic:synthetic-password@db.invalid/faxbot',
    })
    exported = values.to_environment()
    assert exported['FAX_BACKEND'] == 'sip'
    assert 'FAX_OUTBOUND_BACKEND' not in exported
    assert 'FAX_INBOUND_BACKEND' not in exported
    assert 'SINCH_API_KEY' not in exported  # preserve inherited fallback on later edits
    assert exported['SINCH_API_SECRET'] == ''  # preserve explicit clear
    assert exported['DOCUMO_API_KEY'] == 'synthetic-inactive-key'
    assert exported['FREESWITCH_ESL_PASSWORD'] == 'synthetic-telephony-password'
    assert exported['SIGNALWIRE_API_TOKEN'] == 'synthetic-unused-token'
    assert exported['ENABLE_MCP_HTTP'] == 'true'
    assert exported['PHAXIO_STATUS_CALLBACK_URL'] == 'https://example.invalid/preferred'
    assert values.faxbot_config_path == str(bundled_config_dir() / 'faxbot.config.json')
    assert values.effective_outbound == 'sip'
    assert values.sinch_api_key == 'synthetic-fallback-key'
    assert 'synthetic-password' not in repr(values)
    assert 'synthetic-unused-token' not in repr(values)


def test_candidate_patch_preserves_omission_resets_inheritance_and_rejects_mask_atomically():
    import pytest
    from app.config_values import ConfigurationValueError
    original = ConfigurationValues.from_environment({
        'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_BACKEND': 'sinch',
        'PHAXIO_API_KEY': 'synthetic-original-key', 'DOCUMO_API_KEY': 'synthetic-documo-key',
        'FAX_DISABLED': 'true', 'MAX_REQUESTS_PER_MINUTE': '12',
    })
    updated = original.with_patch({
        'backend': 'signalwire', 'outbound_backend': '', 'phaxio_api_key': None,
        'documo_api_key': '', 'fax_disabled': False, 'max_requests_per_minute': 0,
    })
    assert updated.effective_outbound == 'signalwire'
    assert updated.phaxio_api_key == 'synthetic-original-key'
    assert updated.documo_api_key == ''
    assert updated.fax_disabled is False
    assert updated.max_requests_per_minute == 0
    assert original.effective_outbound == 'sinch'
    assert original.fax_disabled is True
    with pytest.raises(ConfigurationValueError):
        original.with_patch({'backend': 'sip', 'phaxio_api_key': '********-key'})
    assert original.effective_outbound == 'sinch'
    with pytest.raises(ConfigurationValueError):
        original.with_patch({'max_requests_per_minute': -1})
    with pytest.raises(ConfigurationValueError):
        original.with_patch({'unrecognized_field': 'synthetic-private-value'})


def test_bootstrap_and_provider_endpoint_fields_share_complete_redacted_projection():
    values = ConfigurationValues.from_environment({
        'ENABLE_PERSISTED_SETTINGS': 'true',
        'PERSISTED_ENV_PATH': '/private/faxbot.env',
        'SINCH_BASE_URL': 'https://provider.example.invalid/v3',
        'FAXBOT_PROVIDERS_DIR': '/private/providers',
        'PLUGIN_REGISTRY_PATH': '/private/registry.json',
        'PHAXIO_API_KEY': 'synthetic-secret-key',
        'DATABASE_URL': 'postgresql://operator:synthetic-password@db.invalid/faxbot',
    })
    assert values.enable_persisted_settings is True
    assert values.persisted_env_path == '/private/faxbot.env'
    assert values.sinch_base_url == 'https://provider.example.invalid/v3'
    exported = values.to_environment(redact_secrets=True)
    assert exported['PHAXIO_API_KEY'] == '***'
    assert exported['DATABASE_URL'] == '***'
    assert exported['ENABLE_PERSISTED_SETTINGS'] == 'true'
    assert exported['FAXBOT_PROVIDERS_DIR'] == '/private/providers'
    assert exported['PLUGIN_REGISTRY_PATH'] == '/private/registry.json'
    assert 'synthetic-secret-key' not in str(exported)
    assert 'synthetic-password' not in str(exported)
    assert {'ENABLE_PERSISTED_SETTINGS', 'PHAXIO_CALLBACK_URL', 'SINCH_BASE_URL'} <= values.environment_keys()


def test_unknown_provider_selection_cannot_fall_back_to_legacy_or_schema_metadata():
    import pytest
    from app.config_values import ConfigurationValueError
    registry = {'_schema': {'version': 1}, 'phaxio': {'id': 'phaxio'}, 'sip': {'id': 'sip'}}
    known = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', 'FAX_OUTBOUND_BACKEND': 'sip'})
    known.validate_provider_selection(registry)
    for selector in ('misspelled-provider', '_schema', ''):
        field = 'FAX_OUTBOUND_BACKEND' if selector else 'FAX_BACKEND'
        unknown = ConfigurationValues.from_environment({'FAX_BACKEND': 'phaxio', field: selector})
        with pytest.raises(ConfigurationValueError) as error:
            unknown.validate_provider_selection(registry)
        assert field in {item['field'] for item in error.value.issues}
    assert known.effective_outbound == 'sip'


def test_masks_returned_for_short_or_newline_ending_secrets_cannot_be_saved_as_credentials():
    import pytest
    from app.config_values import ConfigurationValueError
    current = ConfigurationValues.from_environment({'PHAXIO_API_SECRET': 'synthetic-original'})
    for mask in ('*2345', '**3456', '***', '***\nabc', '********last'):
        with pytest.raises(ConfigurationValueError):
            current.with_patch({'phaxio_api_secret': mask})
    assert current.phaxio_api_secret == 'synthetic-original'


def test_installation_country_defaults_to_us_and_takes_iso_codes():
    import pytest
    from app.config_values import ConfigurationValueError
    assert ConfigurationValues.from_environment({}).fax_default_country == 'US'
    assert ConfigurationValues.from_environment({'FAX_DEFAULT_COUNTRY': ' gb '}).fax_default_country == 'GB'
    for invalid in ('', 'XX', 'UK', 'GBR'):
        with pytest.raises(ConfigurationValueError) as error:
            ConfigurationValues.from_environment({'FAX_DEFAULT_COUNTRY': invalid})
        assert error.value.issues == ({'field': 'FAX_DEFAULT_COUNTRY', 'reason': 'value_error'},)


def test_fax_numbers_in_settings_are_saved_in_e164_for_the_installation_country():
    import pytest
    from app.config_values import ConfigurationValueError
    uk = ConfigurationValues.from_environment({'FAX_DEFAULT_COUNTRY': 'GB'})
    saved = uk.with_patch({'direct_fax_number': '01782 684953', 'sip_trunk_caller_id': '01782 684953',
                           'sip_trunk_dids': '01782 684953, +1 303 555 0123',
                           'signalwire_fax_from_e164': '01782 684954'})
    assert (saved.direct_fax_number, saved.sip_trunk_caller_id, saved.signalwire_fax_from_e164) == (
        '+441782684953', '+441782684953', '+441782684954')
    assert saved.sip_trunk_did_list == ('+441782684953', '+13035550123')
    # A change of country in the same save applies to the numbers in it.
    switched = ConfigurationValues.from_environment({}).with_patch(
        {'fax_default_country': 'gb', 'direct_fax_number': '01782 684953'})
    assert switched.direct_fax_number == '+441782684953'
    assert ConfigurationValues.from_environment({}).with_patch(
        {'direct_fax_number': '303 555 0123'}).direct_fax_number == '+13035550123'
    with pytest.raises(ConfigurationValueError) as error:
        uk.with_patch({'sip_trunk_caller_id': 'front desk'})
    assert error.value.issues == ({'field': 'SIP_TRUNK_CALLER_ID', 'reason': 'string_pattern_mismatch'},)
    # Saved revisions load unchanged; only new saves are rewritten.
    assert ConfigurationValues.from_environment({'DIRECT_FAX_NUMBER': '3035550123'}).direct_fax_number == '3035550123'
