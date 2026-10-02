"""Import operator data once without mutating the process environment."""
import json
import os

import pytest

from api.app.config_bootstrap import load_bootstrap_configuration, ConfigurationBootstrapError


def test_enabled_literal_file_overrides_environment_without_mutating_it(tmp_path):
    persisted = tmp_path / 'settings.env'
    persisted.write_text('PHAXIO_API_SECRET="literal\\nsecret"\nFAX_BACKEND=sinch\n')
    environment = {'ENABLE_PERSISTED_SETTINGS': 'true', 'PERSISTED_ENV_PATH': str(persisted),
                   'FAXBOT_CONFIG_PATH': str(tmp_path / 'missing.json'), 'PHAXIO_API_SECRET': 'environment'}
    before = dict(os.environ)
    imported = load_bootstrap_configuration(environment)
    assert imported.values.phaxio_api_secret == 'literal\nsecret'
    assert imported.values.effective_outbound == 'sinch'
    assert dict(os.environ) == before


def test_existing_legacy_selection_is_imported_but_missing_defaults_are_not(tmp_path):
    path = tmp_path / 'plugins.json'
    missing = load_bootstrap_configuration({'FAXBOT_CONFIG_PATH': str(path), 'FAX_BACKEND': 'sinch'})
    assert missing.values.effective_outbound == 'sinch'
    path.write_text(json.dumps({'version': 1, 'providers': {'outbound': {'plugin': 'phaxio', 'enabled': False,
        'settings': {'api_key': 'legacy-key', 'api_secret': 'legacy-secret'}}}}))
    imported = load_bootstrap_configuration({'FAXBOT_CONFIG_PATH': str(path)})
    assert imported.values.phaxio_api_key == 'legacy-key'
    assert imported.plugins.as_dict()['roles']['outbound']['enabled'] is False
    with pytest.raises(ConfigurationBootstrapError):
        load_bootstrap_configuration({'FAXBOT_CONFIG_PATH': str(path), 'FAX_BACKEND': 'sinch'})


def test_conflicting_legacy_credentials_require_reconciliation(tmp_path):
    path = tmp_path / 'plugins.json'
    path.write_text(json.dumps({'version': 1, 'providers': {'outbound': {'plugin': 'phaxio', 'enabled': True,
        'settings': {'api_key': 'legacy-key'}}}}))
    with pytest.raises(ConfigurationBootstrapError) as caught:
        load_bootstrap_configuration({'FAXBOT_CONFIG_PATH': str(path), 'PHAXIO_API_KEY': 'environment-key'})
    assert 'legacy-key' not in str(caught.value) and 'environment-key' not in str(caught.value)


@pytest.mark.parametrize('artifact', ['persisted', 'plugins'])
def test_existing_malformed_import_does_not_fall_back_to_environment(tmp_path, artifact):
    path = tmp_path / 'invalid'
    path.write_text('this is not valid configuration')
    environment = {'FAXBOT_CONFIG_PATH': str(path if artifact == 'plugins' else tmp_path / 'missing'),
                   'ENABLE_PERSISTED_SETTINGS': str(artifact == 'persisted').lower(), 'PERSISTED_ENV_PATH': str(path)}
    with pytest.raises(ConfigurationBootstrapError):
        load_bootstrap_configuration(environment)
