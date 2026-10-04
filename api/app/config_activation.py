"""Build complete operation profiles and apply validated canonical edits."""
from pathlib import Path
from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from .config_bootstrap import load_bootstrap_configuration, default_plugin_state
from .config_plugin_fields import PLUGIN_FIELDS
from .config_plugin_secrets import SECRET_PLUGIN_FIELDS, ConfigurationPluginSecretError, reject_masked_plugin_secrets
from .config_profiles import ConfigurationDocument, ProviderConfiguration
from .config_store import ConfigurationNotInitialized
from .config_values import ConfigurationValues


class ConfigurationActivationError(ValueError):
    """Safe operator-facing candidate rejection."""


_MAINTENANCE_FIELDS = frozenset({'database_url', 'fax_data_dir', 'providers_dir',
                                 'plugin_registry_path', 'faxbot_config_path', 'persisted_env_path'})
_RESTART_FIELDS = frozenset({'enable_mcp_sse', 'mcp_sse_path', 'enable_mcp_http', 'mcp_http_path',
    'require_mcp_oauth', 'oauth_issuer', 'oauth_audience', 'oauth_jwks_url',
    'audit_log_enabled', 'audit_log_format', 'audit_log_file', 'audit_log_syslog', 'audit_log_syslog_address',
    'artifact_ttl_days', 'cleanup_interval_minutes', 'fax_disabled'})
_AMI_FIELDS = frozenset({'ami_host', 'ami_port', 'ami_username', 'ami_password'})


def _validate_manifest_settings(manifest, settings):
    if manifest is None or 'config_schema' not in manifest:
        try:
            reject_masked_plugin_secrets(settings)
        except ConfigurationPluginSecretError:
            raise ConfigurationActivationError('Masked credentials cannot be saved as secrets.') from None
        return
    schema = manifest['config_schema']
    try:
        if not isinstance(schema, (dict, bool)):
            raise ValueError
        validator = validators.validator_for(schema, default=None) if isinstance(schema, dict) and '$schema' in schema else Draft202012Validator
        if validator is None:
            raise ValueError
        validator.check_schema(schema)
        # No retrieve callback: external refs cannot turn settings validation
        # into filesystem or network access. Local $defs remain supported.
        instance_validator = validator(schema, registry=Registry(), format_checker=validator.FORMAT_CHECKER)
        instance_validator.validate(settings)
        reject_masked_plugin_secrets(settings, schema=schema, validator=instance_validator)
    except ConfigurationPluginSecretError:
        raise ConfigurationActivationError('Masked credentials cannot be saved as secrets.') from None
    except (SchemaError, ValidationError, Unresolvable, ValueError, RecursionError):
        raise ConfigurationActivationError('Plugin settings do not satisfy the installed configuration schema.') from None


def _merge_plugin_settings(previous, patch):
    merged = dict(previous) if patch else {}
    for key, value in patch.items():
        if value is None:
            continue
        if isinstance(value, dict):
            prior = merged.get(key, {})
            merged[key] = _merge_plugin_settings(prior if isinstance(prior, dict) else {}, value)
        else:
            merged[key] = value
    return merged


def _catalog(values):
    from .provider_catalog import ProviderCatalog
    from .config_paths import provider_traits_path
    return ProviderCatalog.load(provider_traits_path(), Path(values.providers_dir))


def _plugin_state(values, state):
    if not isinstance(state, dict) or set(state) != {'roles', 'settings'} or not isinstance(state['settings'], dict):
        raise ConfigurationActivationError('Invalid plugin state.')
    roles = state['roles']
    if (not isinstance(roles, dict) or set(roles) != set(default_plugin_state(values)['roles'])
            or any(not isinstance(entry, dict) or set(entry) != {'enabled'}
                   or type(entry['enabled']) is not bool for entry in roles.values())
            or any(not isinstance(settings, dict) for settings in state['settings'].values())):
        raise ConfigurationActivationError('Invalid plugin role or settings.')
    return ConfigurationDocument(state).as_dict()


def _configuration_for(values, definition, plugin_settings):
    pid = definition.id
    credentials, settings = {}, {}
    if pid in PLUGIN_FIELDS:
        for public_name, field_name in PLUGIN_FIELDS[pid].items():
            field = ConfigurationValues.model_fields[field_name]
            target = credentials if (field.json_schema_extra or {}).get('secret') else settings
            target[public_name] = getattr(values, field_name)
    if definition.manifest is not None and values.feature_v3_plugins:
        manifest = definition.manifest.as_dict()
        data = dict(plugin_settings)
        _validate_manifest_settings(manifest, {**settings, **credentials, **data})
        explicit_credentials = data.pop('credentials', {})
        if not isinstance(explicit_credentials, dict):
            raise ConfigurationActivationError('Invalid manifest credentials.')
        credentials.update(explicit_credentials)
        for key in list(data):
            if key in SECRET_PLUGIN_FIELDS or key == 'username':
                credentials[key] = data.pop(key)
        settings.update(data)
    else:
        manifest = None
        if pid not in PLUGIN_FIELDS:
            raise ConfigurationActivationError('Manifest provider activation requires plugins to be enabled.')
    settings.update(public_api_url=values.public_api_url, pdf_token_ttl_minutes=values.pdf_token_ttl_minutes,
                    enforce_public_https=values.enforce_public_https, fax_header=values.fax_header,
                    fax_station_id=values.fax_station_id)
    return ProviderConfiguration(pid, credentials=credentials, settings=settings,
                                 traits=definition.traits.as_dict(), manifest=manifest)


def _effective_definition(values, definition):
    """Disabled HTTP plugins restore the complete retained native definition."""
    if not values.feature_v3_plugins:
        return getattr(definition, 'native_definition', None) or definition
    return definition


def compile_profiles(values, catalog, state):
    values.validate_provider_selection({identity: True for identity in catalog.provider_ids})
    if values.storage_backend not in {'local', 's3'}:
        raise ConfigurationActivationError('Unknown storage provider.')
    state = _plugin_state(values, state)
    if set(state['settings']) - set(catalog.provider_ids):
        raise ConfigurationActivationError('Plugin settings refer to an unavailable provider.')
    for identity, settings in state['settings'].items():
        definition = _effective_definition(values, catalog.get(identity))
        _validate_manifest_settings(definition.manifest.as_dict() if definition.manifest is not None else None, settings)
    profiles = {}
    for role, identity in [('outbound', values.effective_outbound), ('inbound', values.effective_inbound)]:
        # No provider set up for this role yet: it has no profile, as when the role is turned off.
        if not identity or not state['roles'][role]['enabled'] or (role == 'inbound' and not values.inbound_enabled):
            continue
        definition = _effective_definition(values, catalog.get(identity))
        traits = definition.traits.as_dict()
        if role == 'inbound' and not traits.get('supports_inbound', False):
            raise ConfigurationActivationError('Selected provider does not support inbound fax.')
        if role == 'inbound' and traits.get('needs_storage', True) and not state['roles']['storage']['enabled']:
            raise ConfigurationActivationError('Inbound fax requires enabled storage.')
        profile = _configuration_for(values, definition, state['settings'].get(identity, {}))
        if role == 'outbound' and profile.manifest is not None and 'send_fax' not in profile.manifest.get('actions', {}):
            raise ConfigurationActivationError('Selected manifest cannot send faxes.')
        profiles[role] = profile
    return profiles


class ConfigurationManager:
    def __init__(self, store, *, catalog_loader=_catalog):
        self.store = store
        self.catalog_loader = catalog_loader

    def initialize(self, environment):
        try:
            return self.store.read()
        except ConfigurationNotInitialized:
            imported = load_bootstrap_configuration(environment)
            catalog = self.catalog_loader(imported.values)
            state = imported.plugins.as_dict()
            return self.store.initialize(imported.values, actor='bootstrap', plugins=state,
                providers=compile_profiles(imported.values, catalog, state))

    def pending_fields(self, snapshot):
        if snapshot.pending is None:
            return ()
        fields = [name for name in ConfigurationValues.model_fields
                  if getattr(snapshot.active.values, name) != getattr(snapshot.desired.values, name)]
        if snapshot.active.profiles != snapshot.desired.profiles:
            fields.append('provider_profiles')
        if snapshot.active.plugins != snapshot.desired.plugins:
            fields.append('plugins')
        return tuple(sorted(fields))

    def _validate_maintenance(self, expected, values):
        if any(getattr(values, name) != getattr(expected.active.values, name) for name in _MAINTENANCE_FIELDS):
            raise ConfigurationActivationError('Changing installation storage or resource paths requires the maintenance transfer workflow.')

    def _catalog_for(self, expected, values):
        self._validate_maintenance(expected, values)
        return self.catalog_loader(values)

    def _prepare_apply(self, expected, values, state, catalog):
        self._validate_maintenance(expected, values)
        profiles = compile_profiles(values, catalog, state)
        restart = any(getattr(values, name) != getattr(expected.active.values, name) for name in _RESTART_FIELDS)
        old_ami = any(self.store.read_profile(identity).configuration.traits.get('requires_ami', False)
                      for _, identity in expected.active.profiles)
        new_ami = any(profile.traits.get('requires_ami', False) for profile in profiles.values())
        restart = restart or old_ami != new_ami or ((old_ami or new_ami)
            and any(getattr(values, name) != getattr(expected.active.values, name) for name in _AMI_FIELDS))
        return profiles, restart

    def _apply(self, expected, values, state, actor, *, catalog):
        profiles, restart = self._prepare_apply(expected, values, state, catalog)
        return self.store.apply(expected, values, restart_required=restart, actor=actor, providers=profiles, plugins=state)

    def _apply_authorized(self, expected, values, state, *, principal, control, operation, catalog):
        profiles, restart = self._prepare_apply(expected, values, state, catalog)
        baseline = compile_profiles(expected.desired.values, catalog, expected.desired.plugins.as_dict())
        return self.store.apply_authorized(expected, values, principal=principal, control=control,
            operation=operation, restart_required=restart, providers=profiles, plugins=state,
            baseline_providers=baseline)

    def _patch_values(self, expected, changes):
        values = expected.desired.values.with_patch(changes)
        state = expected.desired.plugins.as_dict()
        if changes.get('inbound_enabled') is not None:
            state['roles']['inbound']['enabled'] = values.inbound_enabled
        return values, state

    def patch(self, expected, changes, *, actor):
        """Trusted internal edit; human adapters use patch_authorized."""
        values, state = self._patch_values(expected, changes)
        return self._apply(expected, values, state, actor, catalog=self._catalog_for(expected, values))

    def patch_authorized(self, expected, changes, *, principal, control):
        values, state = self._patch_values(expected, changes)
        return self._apply_authorized(expected, values, state, principal=principal, control=control,
            operation='settings.update', catalog=self._catalog_for(expected, values))

    def _patch_plugin_values(self, expected, provider_id, *, settings, enabled, role, catalog):
        values = expected.desired.values
        state = expected.desired.plugins.as_dict()
        if role is None:
            role = 'storage' if provider_id in {'local', 's3'} else 'outbound'
        if provider_id not in set(catalog.provider_ids) | {'local', 's3'}:
            raise ConfigurationActivationError('Unknown plugin.')
        if role not in {'outbound', 'inbound', 'storage'} or (enabled is not None and type(enabled) is not bool):
            raise ConfigurationActivationError('Invalid plugin role or enabled state.')
        if (role == 'storage') != (provider_id in {'local', 's3'}):
            raise ConfigurationActivationError('Plugin does not support the selected role.')
        if settings is not None:
            if not isinstance(settings, dict):
                raise ConfigurationActivationError('Plugin settings must be an object.')
            settings = ConfigurationDocument(settings).as_dict()
            if provider_id in PLUGIN_FIELDS:
                mapping = PLUGIN_FIELDS[provider_id]
                if set(settings) - set(mapping):
                    raise ConfigurationActivationError('Unknown plugin setting.')
                if settings:
                    patch = {mapping[key]: value for key, value in settings.items()}
                else:
                    defaults = ConfigurationValues.from_environment({})
                    patch = {name: '' if isinstance(getattr(defaults, name), str) else getattr(defaults, name)
                             for name in mapping.values()}
                values = values.with_patch(patch)
            else:
                previous = state['settings'].get(provider_id, {})
                merged = _merge_plugin_settings(previous, settings)
                state['settings'][provider_id] = ConfigurationDocument(merged).as_dict()
        if enabled is not None:
            selected = values.storage_backend if role == 'storage' else getattr(values, 'effective_' + role)
            if enabled:
                field = 'storage_backend' if role == 'storage' else role + '_backend'
                values = values.with_patch({field: provider_id})
                state['roles'][role]['enabled'] = True
                if role == 'inbound':
                    values = values.with_patch({'inbound_enabled': True})
            elif selected == provider_id:
                state['roles'][role]['enabled'] = False
                if role == 'inbound':
                    values = values.with_patch({'inbound_enabled': False})
        return values, state

    def patch_plugin(self, expected, provider_id, *, settings=None, enabled=None, role=None, actor):
        """Trusted internal edit; human adapters use patch_plugin_authorized."""
        catalog = self.catalog_loader(expected.desired.values)
        values, state = self._patch_plugin_values(expected, provider_id, settings=settings,
            enabled=enabled, role=role, catalog=catalog)
        return self._apply(expected, values, state, actor, catalog=catalog)

    def patch_plugin_authorized(self, expected, provider_id, *, principal, control,
                                settings=None, enabled=None, role=None):
        catalog = self.catalog_loader(expected.desired.values)
        values, state = self._patch_plugin_values(expected, provider_id, settings=settings,
            enabled=enabled, role=role, catalog=catalog)
        return self._apply_authorized(expected, values, state, principal=principal, control=control,
            operation='providers.configure', catalog=catalog)
