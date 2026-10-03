from fastapi.testclient import TestClient
from app.main import app
from app.config_file import parse_environment
from app.config_values import ConfigurationValues


def _admin_headers():
    return {"X-API-Key": "bootstrap_admin_only", "Origin": "https://testserver"}


def test_admin_config_hybrid_fields(isolated_installation, monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("FAX_INBOUND_BACKEND", "sip")

    with TestClient(app, base_url="https://testserver") as client:
        r = client.get("/admin/config", headers=_admin_headers())
        assert r.status_code == 200
        data = r.json()
        assert data["hybrid"]["outbound"] == "phaxio"
        assert data["hybrid"]["inbound"] == "sip"
        assert data["hybrid"]["inbound_explicit"] is True


def test_admin_settings_update_hybrid(isolated_installation, monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")

    with TestClient(app, base_url="https://testserver") as client:
        # Update outbound/inbound separately
        payload = {"outbound_backend": "sinch", "inbound_backend": "sip", "inbound_enabled": True}
        r = client.put("/admin/settings", headers=_admin_headers(), json=payload)
        assert r.status_code == 200
        # Reload reads desired state without promoting resource changes.
        reloaded = client.post("/admin/settings/reload", headers=_admin_headers())
        assert reloaded.status_code == 200
        still_active = client.get("/admin/config", headers=_admin_headers()).json()
        assert still_active["hybrid"]["outbound"] == "phaxio"
    # A stopped installation prepares and promotes the complete pending revision.
    with TestClient(app, base_url="https://testserver") as client:
        r2 = client.get("/admin/config", headers=_admin_headers())
        assert r2.status_code == 200
        cfg = r2.json()
        assert cfg["hybrid"]["outbound"] == "sinch"
        assert cfg["hybrid"]["inbound"] == "sip"


def test_admin_inbound_callbacks_reflect_inbound_backend(isolated_installation, monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("INBOUND_ENABLED", "true")

    with TestClient(app, base_url="https://testserver") as client:
        # Default: inbound follows outbound → phaxio
        r = client.get("/admin/inbound/callbacks", headers=_admin_headers())
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] in {"phaxio", "sinch", "sip"}

        # Explicit SIP inbound should switch callbacks
        client.put("/admin/settings", headers=_admin_headers(), json={"inbound_backend": "sip", "inbound_enabled": True})
        client.post("/admin/settings/reload", headers=_admin_headers())
    with TestClient(app, base_url="https://testserver") as client:
        r2 = client.get("/admin/inbound/callbacks", headers=_admin_headers())
        assert r2.status_code == 200
        data2 = r2.json()
    assert data2["backend"] == "sip"


def test_export_env_preserves_explicit_and_inherited_directional_values(isolated_installation, monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    # Single-provider mode (no explicit dual env)
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.delenv("FAX_OUTBOUND_BACKEND", raising=False)
    monkeypatch.delenv("FAX_INBOUND_BACKEND", raising=False)

    with TestClient(app, base_url="https://testserver") as client:
        r = client.get("/admin/settings/export", headers={"X-API-Key": "bootstrap_admin_only", "Origin": "https://testserver"})
        assert r.status_code == 200
        assert r.json()["env"] == r.json()["env_content"]
        content = parse_environment(r.json()["env_content"], allowed_keys=ConfigurationValues.environment_keys())
        assert content["FAX_BACKEND"] == "phaxio"
        assert "FAX_OUTBOUND_BACKEND" not in content
        assert "FAX_INBOUND_BACKEND" not in content

        # Explicit outbound only
        client.put("/admin/settings", headers={"X-API-Key": "bootstrap_admin_only", "Origin": "https://testserver"}, json={"outbound_backend": "sinch"})
        r2 = client.get("/admin/settings/export", headers={"X-API-Key": "bootstrap_admin_only", "Origin": "https://testserver"})
        content2 = parse_environment(r2.json()["env_content"], allowed_keys=ConfigurationValues.environment_keys())
        assert content2["FAX_OUTBOUND_BACKEND"] == "sinch"
        assert "FAX_INBOUND_BACKEND" not in content2

        # Explicit inbound
        client.put("/admin/settings", headers={"X-API-Key": "bootstrap_admin_only", "Origin": "https://testserver"}, json={"inbound_backend": "sip", "inbound_enabled": True})
        r3 = client.get("/admin/settings/export", headers={"X-API-Key": "bootstrap_admin_only", "Origin": "https://testserver"})
        content3 = parse_environment(r3.json()["env_content"], allowed_keys=ConfigurationValues.environment_keys())
        assert content3["FAX_OUTBOUND_BACKEND"] == "sinch"
        assert content3["FAX_INBOUND_BACKEND"] == "sip"


def test_humblefax_is_reported_configured_and_listed_like_other_cloud_providers(isolated_installation, monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("FEATURE_V3_PLUGINS", "true")
    monkeypatch.setenv("FAX_BACKEND", "humblefax")
    monkeypatch.setenv("HUMBLEFAX_ACCESS_KEY", "synthetic-humblefax-access")
    monkeypatch.setenv("HUMBLEFAX_SECRET_KEY", "synthetic-humblefax-secret")

    with TestClient(app, base_url="https://testserver") as client:
        config = client.get("/admin/config", headers=_admin_headers())
        assert config.status_code == 200
        assert config.json()["backend_configured"]["humblefax"] is True
        assert config.json()["backend_configured"]["documo"] is False
        assert "synthetic-humblefax" not in config.text

        plugins = client.get("/plugins", headers=_admin_headers())
        assert plugins.status_code == 200
        items = {item["id"]: item for item in plugins.json()["items"]}
        assert items["humblefax"]["categories"] == items["documo"]["categories"] == ["outbound"]
        assert items["humblefax"]["capabilities"] == items["documo"]["capabilities"] == ["send", "get_status"]
        assert items["humblefax"]["enabled"] is True
        assert items["documo"]["enabled"] is False


def test_humblefax_needs_both_keys_to_count_as_configured(isolated_installation, monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("PUBLIC_API_URL", "https://testserver")
    monkeypatch.setenv("FAXBOT_CONSOLE_ORIGINS", "https://testserver")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("HUMBLEFAX_ACCESS_KEY", "synthetic-humblefax-access")

    with TestClient(app, base_url="https://testserver") as client:
        config = client.get("/admin/config", headers=_admin_headers())
        assert config.status_code == 200
        assert config.json()["backend_configured"]["humblefax"] is False
