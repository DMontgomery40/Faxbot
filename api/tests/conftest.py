import os
import sys
import pytest

# Ensure project root is on sys.path for `import api.*`
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Ensure test mode flag for Ghostscript-less CI/unit tests
os.environ.setdefault("FAXBOT_TEST_MODE", "true")


@pytest.fixture(autouse=True)
def _restore_installation_database():
    """The process's installation database binding, back after every test.

    An app lifespan whose DATABASE_URL differs moves ``db.engine`` and ``db.SessionLocal`` to that database
    (``db.init_db``) and never moves them back. A test on a scoped PostgreSQL schema then left every later test
    reading a schema it dropped, so a trunk call later in the run failed before its Originate was written. The
    module is loaded both as ``app.db`` and as ``api.app.db``; each loaded copy is restored.
    """
    saved = {name: sys.modules[name].engine for name in ('app.db', 'api.app.db') if name in sys.modules}
    yield
    for name, engine in saved.items():
        module = sys.modules.get(name)
        if module is None or module.engine is engine:
            continue
        moved = module.engine
        module.SessionLocal.configure(bind=engine)
        module.engine = engine
        moved.dispose()


@pytest.fixture
def isolated_installation(monkeypatch, tmp_path):
    """Opt-in real installation boundary for legacy lifespan/HTTP fixtures.

    Canonical configuration belongs to a database and its key, not to the
    current process environment. Each test gets a new installation; sequential
    lifespans within that test deliberately retain the same canonical state.
    """
    from app.config_values import ConfigurationValues
    for name in ConfigurationValues.environment_keys():
        monkeypatch.delenv(name, raising=False)
    environment = {
        'DATABASE_URL': 'sqlite:///' + str(tmp_path / 'installation.db'),
        'FAX_DATA_DIR': str(tmp_path / 'faxdata'),
        'FAXBOT_INSTALLATION_KEY_PATH': str(tmp_path / 'installation.key'),
        'FAXBOT_CONFIG_PATH': str(tmp_path / 'absent-legacy.json'),
        'FAXBOT_PROVIDERS_DIR': str(tmp_path / 'providers'),
        'FAX_DISABLED': 'true',
        # A new installation has no fax provider; these legacy fixtures exercise a Phaxio one.
        'FAX_BACKEND': 'phaxio',
        'REQUIRE_API_KEY': 'false',
        'ENABLE_PERSISTED_SETTINGS': 'false',
        'ENABLE_MCP_SSE': 'false',
        'ENABLE_MCP_HTTP': 'false',
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    return environment


def pytest_configure(config):
    config.addinivalue_line('markers', 'native: needs Docker and the Faxbot Asterisk image; runs only with FAXBOT_NATIVE_PROOF=1')
