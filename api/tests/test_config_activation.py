"""Canonical edits select complete profiles and stage lifespan-owned changes."""
from types import SimpleNamespace

import pytest

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore
from api.app.config_profiles import ConfigurationDocument
from api.app.config_activation import ConfigurationManager, ConfigurationActivationError


class Catalog:
    provider_ids = frozenset({'phaxio', 'sinch', 'sip'})

    def get(self, identity):
        if identity not in self.provider_ids:
            raise ValueError('unknown provider')
        return SimpleNamespace(id=identity, manifest=None, kind='cloud', traits=ConfigurationDocument({
            'requires_tiff': identity == 'sip', 'requires_ami': identity == 'sip', 'supports_inbound': True}))


def manager(database, tmp_path):
    upgrade_schema(database)
    store = ConfigurationStore(database, tmp_path / 'configuration.key')
    return ConfigurationManager(store, catalog_loader=lambda values: Catalog())


def environment(tmp_path):
    return {'FAXBOT_CONFIG_PATH': str(tmp_path / 'no-legacy.json'), 'PHAXIO_API_KEY': 'original-key',
            'PHAXIO_API_SECRET': 'original-secret'}


def test_manager_ignores_changed_bootstrap_after_initialization_and_rotates_live_profile(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    assert control.initialize({'FAX_BACKEND': 'unknown', 'MAX_FILE_SIZE_MB': 'invalid'}) == first
    changed = control.patch(first, {'phaxio_api_key': 'new-key'}, actor='admin')
    assert changed.pending is None
    old = control.store.read_profile(first.active.profile_id('outbound'))
    new = control.store.read_profile(changed.active.profile_id('outbound'))
    assert old.configuration.credentials['api_key'] == 'original-key'
    assert new.configuration.credentials['api_key'] == 'new-key'
    assert old.id != new.id


def test_manager_stages_entire_restart_patch_and_rejects_datastore_transfer(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    staged = control.patch(first, {'enable_mcp_http': True, 'fax_header': 'desired'}, actor='admin')
    assert staged.active == first.active
    assert staged.desired.values.fax_header == 'desired'
    assert 'enable_mcp_http' in control.pending_fields(staged)
    with pytest.raises(ConfigurationActivationError):
        control.patch(staged, {'database_url': 'sqlite:///different.db'}, actor='admin')
    assert control.store.read() == staged
    active = control.patch(staged, {'enable_mcp_http': False}, actor='admin')
    assert active.pending is None and active.active.values.fax_header == 'desired'


def test_inactive_plugin_settings_do_not_activate_and_disabling_outbound_preserves_inbound(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize({**environment(tmp_path), 'INBOUND_ENABLED': 'true', 'FAX_INBOUND_BACKEND': 'sinch'})
    changed = control.patch_plugin(first, 'sinch', settings={'api_key': 'sinch-key'}, actor='admin')
    assert changed.active.values.effective_outbound == 'phaxio'
    assert changed.active.profile_id('outbound') == first.active.profile_id('outbound')
    disabled = control.patch_plugin(changed, 'phaxio', enabled=False, actor='admin')
    assert disabled.active.profile_id('outbound') is None
    assert disabled.active.profile_id('inbound') is not None
    reenabled = control.patch_plugin(disabled, 'phaxio', enabled=True, actor='admin')
    assert reenabled.active.profile_id('outbound') is not None


class ManifestCatalog(Catalog):
    provider_ids = Catalog.provider_ids | {'custom'}

    def get(self, identity):
        if identity != 'custom':
            return super().get(identity)
        return SimpleNamespace(id='custom', kind='cloud', traits=ConfigurationDocument({'requires_tiff': False}),
            manifest=ConfigurationDocument({'id': 'custom', 'actions': {'send_fax': {'url': 'https://example.test/fax'}},
                'config_schema': {'type': 'object', 'properties': {'credentials': {'type': 'object',
                    'properties': {'username': {'type': 'string'}, 'password': {'type': 'string', 'writeOnly': True}},
                    'required': ['username', 'password']}, 'attempts': {'type': 'integer', 'minimum': 1}}}}))


def test_manifest_credentials_merge_without_saving_masks_or_invalid_schema(database, tmp_path):
    control = manager(database, tmp_path)
    control.catalog_loader = lambda values: ManifestCatalog()
    first = control.initialize({**environment(tmp_path), 'FEATURE_V3_PLUGINS': 'true'})
    saved = control.patch_plugin(first, 'custom', settings={'credentials': {'username': 'operator', 'password': 'original'}}, actor='admin')
    edited = control.patch_plugin(saved, 'custom', settings={'credentials': {'password': 'changed'}}, actor='admin')
    assert edited.active.plugins.as_dict()['settings']['custom']['credentials'] == {'username': 'operator', 'password': 'changed'}
    for bad in ({'credentials': {'password': '***'}}, {'attempts': 'not-an-integer'}):
        with pytest.raises(ConfigurationActivationError):
            control.patch_plugin(edited, 'custom', settings=bad, actor='admin')
        assert control.store.read() == edited
    activated = control.patch_plugin(edited, 'custom', enabled=True, actor='admin')
    assert control.store.read_profile(activated.active.profile_id('outbound')).configuration.credentials == {'username': 'operator', 'password': 'changed'}


def test_provider_cannot_be_selected_as_storage_and_unknown_storage_has_no_fallback(database, tmp_path):
    control = manager(database, tmp_path)
    first = control.initialize(environment(tmp_path))
    with pytest.raises(ConfigurationActivationError):
        control.patch_plugin(first, 'phaxio', enabled=True, role='storage', actor='admin')
    with pytest.raises(ConfigurationActivationError):
        control.patch(first, {'storage_backend': 'unknown-storage'}, actor='admin')
    assert control.store.read() == first


def test_manifest_schema_validation_never_fetches_external_references(monkeypatch):
    import urllib.request
    from api.app.config_activation import _validate_manifest_settings
    def refuse_network(*args, **kwargs):
        raise AssertionError('Schema validation attempted external access')
    monkeypatch.setattr(urllib.request, 'urlopen', refuse_network)
    with pytest.raises(ConfigurationActivationError):
        _validate_manifest_settings({'config_schema': {'$ref': 'https://example.test/schema'}}, {})
    _validate_manifest_settings({'config_schema': {'$defs': {'key': {'type': 'string'}},
        'type': 'object', 'properties': {'key': {'$ref': '#/$defs/key'}}}}, {'key': 'literal'})
