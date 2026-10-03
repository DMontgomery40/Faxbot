"""Canonical edits select complete profiles and stage lifespan-owned changes."""
from types import SimpleNamespace
import json

import pytest

from api.tests.test_schema import database
from api.app.schema import upgrade_schema
from api.app.config_store import ConfigurationStore, ConfigurationNotInitialized
from api.app.config_bootstrap import ConfigurationBootstrapError
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


def test_phaxio_callback_token_is_distinct_captured_and_redacted(database, tmp_path):
    from api.app.config_views import project_admin_settings
    control = manager(database, tmp_path)
    first = control.initialize({**environment(tmp_path), 'PHAXIO_CALLBACK_TOKEN': 'callback-only-secret'})
    original = control.store.read_profile(first.active.profile_id('outbound'))
    assert original.configuration.credentials['callback_token'] == 'callback-only-secret'
    assert original.configuration.credentials['api_secret'] == 'original-secret'
    assert project_admin_settings(first)['phaxio']['callback_token'] == '***'
    assert 'callback-only-secret' not in repr(first.active.values)
    changed = control.patch(first, {'phaxio_callback_token': ''}, actor='admin')
    assert project_admin_settings(changed)['phaxio']['callback_token'] == ''
    assert control.store.read_profile(changed.active.profile_id('outbound')).configuration.credentials['callback_token'] == ''
    assert control.store.read_profile(first.active.profile_id('outbound')).configuration.credentials['callback_token'] == 'callback-only-secret'


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


def native_override_catalog(tmp_path, identity):
    from api.app.config_paths import provider_traits_path
    from api.app.provider_catalog import ProviderCatalog
    providers = tmp_path / 'providers'
    destination = providers / identity / 'manifest.json'
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps({'id': identity, 'kind': 'cloud',
        'traits': {'requires_tiff': False, 'requires_ami': False, 'supports_inbound': False},
        'actions': {'send_fax': {'url': 'https://synthetic.invalid/fax'}}}))
    return ProviderCatalog.load(provider_traits_path(), providers)


@pytest.mark.parametrize('identity', ['sip', 'freeswitch'])
@pytest.mark.parametrize('plugins_enabled', [False, True])
def test_compilation_uses_native_definition_only_when_manifest_plugins_disabled(tmp_path, identity, plugins_enabled):
    from api.app.config_activation import compile_profiles
    from api.app.config_bootstrap import default_plugin_state
    from api.app.config_values import ConfigurationValues
    catalog = native_override_catalog(tmp_path, identity)
    values = ConfigurationValues.from_environment({'FAX_BACKEND': identity,
        'FEATURE_V3_PLUGINS': str(plugins_enabled).lower()})
    configuration = compile_profiles(values, catalog, default_plugin_state(values))['outbound']
    assert (configuration.manifest is not None) is plugins_enabled
    assert configuration.traits['requires_tiff'] is (not plugins_enabled)
    assert configuration.traits['requires_ami'] is (not plugins_enabled and identity == 'sip')
    if plugins_enabled:
        assert configuration.manifest == catalog.get(identity).manifest.as_dict()


def test_disabled_manifest_uses_native_inbound_capabilities_from_same_definition(tmp_path):
    from api.app.config_activation import compile_profiles
    from api.app.config_bootstrap import default_plugin_state
    from api.app.config_values import ConfigurationValues
    catalog = native_override_catalog(tmp_path, 'sip')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'INBOUND_ENABLED': 'true',
        'FEATURE_V3_PLUGINS': 'false'})
    configurations = compile_profiles(values, catalog, default_plugin_state(values))
    assert set(configurations) == {'outbound', 'inbound'}
    for configuration in configurations.values():
        assert configuration.manifest is None
        assert configuration.traits['supports_inbound'] is True
        assert configuration.traits['requires_ami'] is True
        assert configuration.traits['requires_tiff'] is True
    enabled = values.with_patch({'feature_v3_plugins': True})
    with pytest.raises(ConfigurationActivationError, match='does not support inbound'):
        compile_profiles(enabled, catalog, default_plugin_state(enabled))


def test_disabled_custom_manifest_does_not_borrow_a_native_definition(tmp_path):
    from api.app.config_activation import compile_profiles
    from api.app.config_bootstrap import default_plugin_state
    from api.app.config_values import ConfigurationValues
    catalog = native_override_catalog(tmp_path, 'custom-only')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'custom-only', 'FEATURE_V3_PLUGINS': 'false'})
    with pytest.raises(ConfigurationActivationError, match='requires plugins to be enabled'):
        compile_profiles(values, catalog, default_plugin_state(values))


def test_disabled_override_schema_does_not_validate_native_settings(tmp_path):
    from api.app.config_activation import compile_profiles
    from api.app.config_bootstrap import default_plugin_state
    from api.app.config_paths import provider_traits_path
    from api.app.config_values import ConfigurationValues
    from api.app.provider_catalog import ProviderCatalog
    native_override_catalog(tmp_path, 'sip')
    destination = tmp_path / 'providers' / 'sip' / 'manifest.json'
    document = json.loads(destination.read_text())
    document['config_schema'] = {'type': 'object', 'required': ['manifest-only-setting']}
    destination.write_text(json.dumps(document))
    catalog = ProviderCatalog.load(provider_traits_path(), tmp_path / 'providers')
    values = ConfigurationValues.from_environment({'FAX_BACKEND': 'sip', 'FEATURE_V3_PLUGINS': 'false'})
    state = default_plugin_state(values)
    state['settings']['sip'] = {'native-setting': 'literal'}
    native = compile_profiles(values, catalog, state)['outbound']
    assert native.manifest is None
    assert native.traits['requires_ami'] is True
    with pytest.raises(ConfigurationActivationError, match='configuration schema'):
        compile_profiles(values.with_patch({'feature_v3_plugins': True}), catalog, state)


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


class SchemaCatalog(ManifestCatalog):
    def __init__(self, schema):
        self.schema = schema

    def get(self, identity):
        definition = super().get(identity)
        if identity == 'custom':
            manifest = definition.manifest.as_dict()
            manifest['config_schema'] = self.schema
            definition = SimpleNamespace(**{**vars(definition), 'manifest': ConfigurationDocument(manifest)})
        return definition


SECRET_SCHEMAS = [
    {'$defs': {'secret': {'type': 'string', 'writeOnly': True}}, 'type': 'object',
     'properties': {'clientSecret': {'$ref': '#/$defs/secret'}}},
    {'type': 'object', 'allOf': [{'properties': {'clientSecret': {'type': 'string', 'writeOnly': True}}}]},
    {'type': 'object', 'anyOf': [{'properties': {'clientSecret': {'type': 'string', 'writeOnly': True}}}, {}]},
    {'type': 'object', 'oneOf': [{'required': ['clientSecret'],
      'properties': {'clientSecret': {'type': 'string', 'format': 'password'}}}, {'required': ['other']}]},
    {'type': 'object', 'if': {'required': ['clientSecret']},
     'then': {'properties': {'clientSecret': {'type': 'string', 'writeOnly': True}}}},
]


@pytest.mark.parametrize('schema', SECRET_SCHEMAS, ids=['ref', 'allOf', 'anyOf', 'oneOf', 'conditional'])
def test_schema_secret_masks_do_not_replace_saved_credentials(database, tmp_path, schema):
    control = manager(database, tmp_path)
    control.catalog_loader = lambda values: SchemaCatalog(schema)
    first = control.initialize({**environment(tmp_path), 'FEATURE_V3_PLUGINS': 'true'})
    saved = control.patch_plugin(first, 'custom', settings={'clientSecret': 'original'}, actor='admin')
    for mask in ('*2345', '**3456', '***', '***\nabc', '********last'):
        with pytest.raises(ConfigurationActivationError):
            control.patch_plugin(saved, 'custom', settings={'clientSecret': mask}, actor='admin')
        assert control.store.read() == saved
    changed = control.patch_plugin(saved, 'custom', settings={'clientSecret': 'replacement'}, actor='admin')
    assert changed.active.plugins.as_dict()['settings']['custom']['clientSecret'] == 'replacement'


@pytest.mark.parametrize('schema, settings', [
    (ManifestCatalog().get('custom').manifest.as_dict()['config_schema'],
     {'credentials': {'username': 'operator', 'password': '***'}}),
    (SECRET_SCHEMAS[0], {'clientSecret': '***'}),
], ids=['nested-credentials', 'referenced-secret'])
def test_custom_legacy_masks_leave_canonical_configuration_absent(database, tmp_path, schema, settings):
    control = manager(database, tmp_path)
    control.catalog_loader = lambda values: SchemaCatalog(schema)
    path = tmp_path / 'legacy.json'
    document = json.dumps({'version': 1, 'providers': {'outbound': {'plugin': 'custom', 'enabled': True,
        'settings': settings}}})
    path.write_text(document)
    with pytest.raises((ConfigurationBootstrapError, ConfigurationActivationError)):
        control.initialize({**environment(tmp_path), 'FAXBOT_CONFIG_PATH': str(path), 'FEATURE_V3_PLUGINS': 'true'})
    with pytest.raises(ConfigurationNotInitialized):
        control.store.read()
    assert path.read_text() == document


def test_unevaluated_secret_fields_preserve_nonsecret_fields(database, tmp_path):
    schema = {'allOf': [{'type': 'object', 'properties': {'label': {'type': 'string'}}}],
              'unevaluatedProperties': {'type': 'string', 'writeOnly': True}}
    control = manager(database, tmp_path)
    control.catalog_loader = lambda values: SchemaCatalog(schema)
    first = control.initialize({**environment(tmp_path), 'FEATURE_V3_PLUGINS': 'true'})
    saved = control.patch_plugin(first, 'custom', settings={'label': '***', 'clientSecret': 'original'}, actor='admin')
    with pytest.raises(ConfigurationActivationError):
        control.patch_plugin(saved, 'custom', settings={'clientSecret': '***'}, actor='admin')
    assert control.store.read() == saved


@pytest.mark.parametrize('schema, settings', [
    ({'$id': 'https://example.test/root', '$defs': {'nested': {'$id': 'child',
        '$defs': {'secret': {'type': 'string', 'writeOnly': True}}, 'type': 'object',
        'properties': {'clientSecret': {'$ref': '#/$defs/secret'}}}},
      'type': 'object', 'properties': {'nested': {'$ref': 'child'}}}, {'nested': {'clientSecret': '***'}}),
    ({'$defs': {'secret': {'$anchor': 'private', 'type': 'string', 'writeOnly': True}},
      'type': 'object', 'properties': {'keys': {'type': 'array', 'items': {'$ref': '#private'}}}}, {'keys': ['***']}),
    ({'$defs': {'node': {'type': 'object', 'properties': {'child': {'$ref': '#/$defs/node'},
        'clientSecret': {'type': 'string', 'writeOnly': True}}}}, '$ref': '#/$defs/node'},
     {'child': {'clientSecret': '***'}}),
], ids=['embedded-resource', 'anchor-array', 'recursive'])
def test_secret_annotations_in_local_resources_remain_offline_and_effective(schema, settings):
    from api.app.config_activation import _validate_manifest_settings
    with pytest.raises(ConfigurationActivationError, match='Masked credentials'):
        _validate_manifest_settings({'config_schema': schema}, settings)


def test_nonmatching_secret_branch_does_not_reject_nonsecret_mask_text():
    from api.app.config_activation import _validate_manifest_settings
    secret_branch = {'required': ['mode'], 'properties': {'mode': {'const': 'private'},
                     'clientSecret': {'type': 'string', 'writeOnly': True}}}
    public_branch = {'required': ['mode'], 'properties': {'mode': {'const': 'public'},
                     'clientSecret': {'type': 'string'}}}
    value = {'mode': 'public', 'clientSecret': '***'}
    _validate_manifest_settings({'config_schema': {'anyOf': [secret_branch, public_branch]}}, value)
    _validate_manifest_settings({'config_schema': {'if': {'properties': {'mode': {'const': 'private'}}},
        'then': secret_branch, 'else': public_branch}}, value)
