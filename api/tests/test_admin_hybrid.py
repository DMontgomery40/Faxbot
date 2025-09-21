from fastapi.testclient import TestClient
from app.main import app


def _admin_headers():
    return {"X-API-Key": "bootstrap_admin_only"}


def test_admin_config_hybrid_fields(monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("FAX_INBOUND_BACKEND", "sip")

    with TestClient(app) as client:
        r = client.get("/admin/config", headers=_admin_headers())
        assert r.status_code == 200
        data = r.json()
        assert data["hybrid"]["outbound"] == "phaxio"
        assert data["hybrid"]["inbound"] == "sip"
        assert data["hybrid"]["inbound_explicit"] is True


def test_admin_settings_update_hybrid(monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")

    with TestClient(app) as client:
        # Update outbound/inbound separately
        payload = {"outbound_backend": "sinch", "inbound_backend": "sip", "inbound_enabled": True}
        r = client.put("/admin/settings", headers=_admin_headers(), json=payload)
        assert r.status_code == 200
        # Reload and verify
        client.post("/admin/settings/reload", headers=_admin_headers())
        r2 = client.get("/admin/config", headers=_admin_headers())
        assert r2.status_code == 200
        cfg = r2.json()
        assert cfg["hybrid"]["outbound"] == "sinch"
        assert cfg["hybrid"]["inbound"] == "sip"


def test_admin_inbound_callbacks_reflect_inbound_backend(monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.setenv("INBOUND_ENABLED", "true")

    with TestClient(app) as client:
        # Default: inbound follows outbound → phaxio
        r = client.get("/admin/inbound/callbacks", headers=_admin_headers())
        assert r.status_code == 200
        data = r.json()
        assert data["backend"] in {"phaxio", "sinch", "sip"}

        # Explicit SIP inbound should switch callbacks
        client.put("/admin/settings", headers=_admin_headers(), json={"inbound_backend": "sip", "inbound_enabled": True})
        client.post("/admin/settings/reload", headers=_admin_headers())
        r2 = client.get("/admin/inbound/callbacks", headers=_admin_headers())
        assert r2.status_code == 200
        data2 = r2.json()
    assert data2["backend"] == "sip"


def test_export_env_includes_dual_only_when_explicit(monkeypatch):
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    # Single-provider mode (no explicit dual env)
    monkeypatch.setenv("FAX_BACKEND", "phaxio")
    monkeypatch.delenv("FAX_OUTBOUND_BACKEND", raising=False)
    monkeypatch.delenv("FAX_INBOUND_BACKEND", raising=False)

    with TestClient(app) as client:
        r = client.get("/admin/settings/export", headers={"X-API-Key": "bootstrap_admin_only"})
        assert r.status_code == 200
        content = r.json().get("env_content") or ""
        assert "FAX_BACKEND=phaxio" in content
        assert "FAX_OUTBOUND_BACKEND=" not in content
        assert "FAX_INBOUND_BACKEND=" not in content

        # Explicit outbound only
        client.put("/admin/settings", headers={"X-API-Key": "bootstrap_admin_only"}, json={"outbound_backend": "sinch"})
        r2 = client.get("/admin/settings/export", headers={"X-API-Key": "bootstrap_admin_only"})
        content2 = r2.json().get("env_content") or ""
        assert "FAX_OUTBOUND_BACKEND=sinch" in content2
        assert "FAX_INBOUND_BACKEND=" not in content2

        # Explicit inbound
        client.put("/admin/settings", headers={"X-API-Key": "bootstrap_admin_only"}, json={"inbound_backend": "sip", "inbound_enabled": True})
        r3 = client.get("/admin/settings/export", headers={"X-API-Key": "bootstrap_admin_only"})
        content3 = r3.json().get("env_content") or ""
        assert "FAX_OUTBOUND_BACKEND=sinch" in content3
        assert "FAX_INBOUND_BACKEND=sip" in content3
