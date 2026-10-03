"""Internal operation isolation; GUI acceptance is recorded separately."""
import asyncio

import pytest

from app.config_values import ConfigurationValues


@pytest.mark.asyncio
async def test_concurrent_operations_keep_their_complete_values():
    from app.config import settings, use_configuration
    ready = asyncio.Event()
    async def operation(header):
        with use_configuration(ConfigurationValues.from_environment({'FAX_HEADER': header})):
            ready.set()
            await asyncio.sleep(0)
            return settings.fax_header
    assert await asyncio.gather(operation('first'), operation('second')) == ['first', 'second']


def test_bound_operation_cannot_be_overwritten_by_environment_reload(monkeypatch):
    from app.config import settings, reload_settings, use_configuration
    with use_configuration(ConfigurationValues.from_environment({'FAX_HEADER': 'captured'})):
        monkeypatch.setenv('FAX_HEADER', 'changed')
        reload_settings()
        assert settings.fax_header == 'captured'


def test_configuration_facade_rejects_per_field_mutation():
    from app.config import settings
    with pytest.raises(AttributeError):
        settings.fax_header = 'partial mutation'


def test_operation_resource_paths_ignore_changed_process_environment(monkeypatch, tmp_path):
    from app.config import use_configuration
    from app.config_paths import providers_dir, faxbot_config_path, plugin_registry_path
    root = tmp_path / 'captured'
    values = ConfigurationValues.from_environment({'FAXBOT_PROVIDERS_DIR': str(root / 'providers'),
        'FAXBOT_CONFIG_PATH': str(root / 'legacy.json'), 'PLUGIN_REGISTRY_PATH': str(root / 'registry.json')})
    with use_configuration(values):
        monkeypatch.setenv('FAXBOT_PROVIDERS_DIR', '/changed/providers')
        monkeypatch.setenv('FAXBOT_CONFIG_PATH', '/changed/legacy.json')
        monkeypatch.setenv('PLUGIN_REGISTRY_PATH', '/changed/registry.json')
        assert providers_dir() == root / 'providers'
        assert faxbot_config_path() == root / 'legacy.json'
        assert plugin_registry_path() == root / 'registry.json'
