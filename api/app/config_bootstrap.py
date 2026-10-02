"""Explicit first-import reconciliation; no process environment mutation."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import json

from .config_file import ConfigurationFileError, read_configuration_text, read_environment
from .config_plugin_fields import PLUGIN_FIELDS
from .config_profiles import ConfigurationDocument, ConfigurationRecordError
from .config_values import ConfigurationValues, ConfigurationValueError


class ConfigurationBootstrapError(ValueError):
    """An operator can reconcile artifacts without exposing their values."""


@dataclass(frozen=True)
class BootstrapConfiguration:
    values: ConfigurationValues = field(repr=False)
    plugins: ConfigurationDocument = field(repr=False)


def default_plugin_state(values):
    return {'roles': {'outbound': {'enabled': True}, 'inbound': {'enabled': values.inbound_enabled},
                      'storage': {'enabled': True}, 'auth': {'enabled': False}}, 'settings': {}}


def _conflict():
    raise ConfigurationBootstrapError('Legacy plugin settings conflict with deployment settings; reconcile the original artifacts before starting.')


def _import_plugins(values, environment, legacy):
    state = default_plugin_state(values)
    if legacy is None:
        return values, state
    if (not isinstance(legacy, dict) or type(legacy.get('version', 1)) is not int
            or legacy.get('version', 1) != 1 or not isinstance(legacy.get('providers'), dict)
            or set(legacy['providers']) - set(state['roles'])):
        raise ConfigurationBootstrapError('Unsupported legacy plugin configuration format.')
    imported_fields = {}
    for role, entry in legacy['providers'].items():
        if not isinstance(entry, dict) or set(entry) - {'plugin', 'enabled', 'settings'}:
            raise ConfigurationBootstrapError('Invalid legacy plugin role.')
        enabled = entry.get('enabled', state['roles'][role]['enabled'])
        if type(enabled) is not bool or not isinstance(entry.get('settings', {}), dict):
            raise ConfigurationBootstrapError('Invalid legacy plugin settings or enabled state.')
        state['roles'][role]['enabled'] = enabled
        provider = entry.get('plugin')
        if provider is None:
            if enabled or entry.get('settings'):
                raise ConfigurationBootstrapError('An enabled legacy role requires an explicit provider.')
            continue
        if not isinstance(provider, str) or not provider.strip():
            raise ConfigurationBootstrapError('Invalid legacy provider selection.')
        provider = provider.strip().lower()
        if role in {'outbound', 'inbound'}:
            explicit = environment.get('FAX_' + role.upper() + '_BACKEND') or environment.get('FAX_BACKEND')
            if explicit and explicit.strip().lower() != provider:
                _conflict()
            values = values.with_patch({role + '_backend': provider})
            if role == 'inbound':
                if 'INBOUND_ENABLED' in environment and values.inbound_enabled != enabled:
                    _conflict()
                values = values.with_patch({'inbound_enabled': enabled})
        elif role == 'storage':
            if provider not in {'local', 's3'}:
                raise ConfigurationBootstrapError('Unsupported legacy storage provider.')
            if 'STORAGE_BACKEND' in environment and values.storage_backend != provider:
                _conflict()
            values = values.with_patch({'storage_backend': provider})
        else:
            raise ConfigurationBootstrapError('Legacy authentication selection requires explicit reconciliation.')
        settings = entry.get('settings', {})
        if provider in PLUGIN_FIELDS:
            fields = PLUGIN_FIELDS[provider]
            if set(settings) - set(fields):
                raise ConfigurationBootstrapError('Unsupported legacy plugin setting.')
            patch = {fields[key]: value for key, value in settings.items()}
            candidate = values.with_patch(patch)
            for name in patch:
                alias = ConfigurationValues.model_fields[name].validation_alias
                choices = alias.choices if hasattr(alias, 'choices') else [alias]
                # Sinch's optional Phaxio fallback is not an explicit Sinch account.
                explicit_keys = choices[:1] if name in {'sinch_api_key', 'sinch_api_secret'} else choices
                if ((any(key in environment for key in explicit_keys) or name in imported_fields)
                        and getattr(values, name) != getattr(candidate, name)):
                    _conflict()
                imported_fields[name] = True
            values = candidate
        else:
            if provider in state['settings'] and state['settings'][provider] != settings:
                _conflict()
            state['settings'][provider] = settings
    return values, state


def load_bootstrap_configuration(environment: Mapping[str, str]) -> BootstrapConfiguration:
    """Only call when canonical state is absent, or for an explicit import preview."""
    try:
        # Read the switch/path before validating values that the enabled artifact
        # may intentionally override. Never update os.environ.
        switches = ConfigurationValues.from_environment({key: environment[key] for key in
            ('ENABLE_PERSISTED_SETTINGS', 'PERSISTED_ENV_PATH') if key in environment})
        merged = {key: value for key, value in environment.items() if key in ConfigurationValues.environment_keys()}
        if switches.enable_persisted_settings:
            merged.update(read_environment(switches.persisted_env_path, allowed_keys=ConfigurationValues.environment_keys()))
        values = ConfigurationValues.from_environment(merged)
        text = read_configuration_text(values.faxbot_config_path, missing_ok=True)
        legacy = json.loads(text) if text is not None else None
        values, plugins = _import_plugins(values, merged, legacy)
        return BootstrapConfiguration(values, ConfigurationDocument(plugins))
    except ConfigurationBootstrapError:
        raise
    except (ConfigurationFileError, ConfigurationValueError, ConfigurationRecordError, ValueError, TypeError, RecursionError):
        raise ConfigurationBootstrapError('Cannot import configuration; check the enabled artifacts and field formats.') from None
