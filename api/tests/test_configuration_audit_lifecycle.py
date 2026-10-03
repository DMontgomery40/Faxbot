"""Internal lifecycle resource probes; no HTTP routes or provider requests."""
import logging
import os
from pathlib import Path
import pytest

@pytest.fixture
def installation(tmp_path, monkeypatch):
    from app.config_values import ConfigurationValues
    for key in ConfigurationValues.environment_keys():
        monkeypatch.delenv(key, raising=False)
    environment = {'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'installation.db'),
        'FAX_DATA_DIR': str(tmp_path / 'data'), 'FAX_DISABLED': 'true',
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'missing.json'),
        'FAXBOT_PROVIDERS_DIR': str(tmp_path / 'providers'),
        'API_KEY': 'synthetic-audit-key', 'AUDIT_LOG_ENABLED': 'true',
        'AUDIT_LOG_FILE': str(tmp_path / 'original-audit.log')}
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    logger = logging.getLogger('audit')
    original_handlers, original_disabled = list(logger.handlers), logger.disabled
    logger.handlers.clear()
    logger.disabled = False
    try:
        yield environment
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.handlers[:] = original_handlers
        logger.disabled = original_disabled

@pytest.mark.asyncio
async def test_failed_lifespan_closes_owned_audit_sink(installation, monkeypatch):
    from app import main
    def fail_mount(application, mounts):
        raise RuntimeError('synthetic mount initialization failure')
    monkeypatch.setattr(main, '_mount_enabled_mcp', fail_mount)
    with pytest.raises(RuntimeError, match='synthetic mount'):
        async with main.lifespan(main.app):
            pytest.fail('Failed initialization became ready')
    logger = logging.getLogger('audit')
    assert not logger.handlers or all(h.stream is None or h.stream.closed for h in logger.handlers), 'Failed lifespan left its audit file open'

@pytest.mark.asyncio
async def test_new_lifespan_uses_the_committed_audit_sink(installation, tmp_path, monkeypatch):
    from app import main
    from app.config_runtime import ConfigurationRuntime
    from app.config import settings
    async with main.lifespan(main.app):
        assert settings.audit_log_file == installation['AUDIT_LOG_FILE']
    second_sink = str(tmp_path / 'new-audit.log')
    runtime = ConfigurationRuntime(installation).prepare()
    try:
        staged = runtime.manager.patch(runtime.snapshot, {'audit_log_file': second_sink}, actor='internal-review')
        assert staged.pending is not None
    finally:
        runtime.close()
    async with main.lifespan(main.app):
        assert settings.audit_log_file == second_sink
        handlers = logging.getLogger('audit').handlers
        assert len(handlers) == 1
        assert handlers[0].baseFilename == second_sink, 'Ready candidate retained the previous audit sink'
