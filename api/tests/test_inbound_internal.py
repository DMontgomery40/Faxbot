from datetime import datetime, timedelta

import sqlalchemy as sa
from fastapi.testclient import TestClient  # type: ignore
from PIL import Image
from api.app.main import app


def _asterisk(client, tiff, to_number="+15551234567", uniqueid="abc123"):
    r = client.post(
        "/_internal/asterisk/inbound",
        headers={"X-Internal-Secret": "sekret"},
        json={
            "tiff_path": str(tiff),
            "to_number": to_number,
            "from_number": "+15550001111",
            "faxstatus": "received",
            "faxpages": 1,
            "uniqueid": uniqueid,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _key(client, scopes):
    r = client.post(
        "/admin/api-keys",
        headers={"X-API-Key": "bootstrap_admin_only"},
        json={"name": "in-" + "-".join(scopes), "owner": "tester", "scopes": scopes},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_internal_asterisk_inbound_flow(isolated_installation, monkeypatch, tmp_path):
    # Enable inbound and set secret
    monkeypatch.setenv("INBOUND_ENABLED", "true")
    monkeypatch.setenv("ASTERISK_INBOUND_SECRET", "sekret")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_inb"))
    monkeypatch.setenv("REQUIRE_API_KEY", "true")
    monkeypatch.setenv("API_KEY", "bootstrap_admin_only")

    # Use a real bounded TIFF that the converter can validate and preserve.
    tiff = tmp_path / "in.tiff"
    Image.new("1", (20, 10), 1).save(tiff, format="TIFF")

    # API keys authenticate over any transport; this mirrors a LAN client over plain HTTP.
    with TestClient(app, base_url="http://testserver") as client:
        inbound_id = _asterisk(client, tiff)

        # Metadata scopes list and read, but never download the document.
        token = _key(client, ["inbound:list", "inbound:read"])
        r3 = client.get("/inbound", headers={"X-API-Key": token})
        assert r3.status_code == 200
        assert any(item["id"] == inbound_id for item in r3.json())
        assert r3.json()[0]["backend"] == "sip"

        r4 = client.get(f"/inbound/{inbound_id}", headers={"X-API-Key": token})
        assert r4.status_code == 200
        assert r4.json()["to"] == "+15551234567"

        assert client.get(f"/inbound/{inbound_id}/pdf", headers={"X-API-Key": token}).status_code == 403
        assert client.get("/inbound/" + "0" * 32, headers={"X-API-Key": token}).status_code == 404

        # Download PDF with an explicit document scope (the iOS contract).
        reader = _key(client, ["inbound:list", "inbound:read", "inbound:document"])
        r5 = client.get(f"/inbound/{inbound_id}/pdf", headers={"X-API-Key": reader})
        assert r5.status_code == 200
        assert r5.headers.get("content-type", "").startswith("application/pdf")

        # A key with neither scope sees nothing, not even existence.
        sender = _key(client, ["fax:send"])
        assert client.get("/inbound", headers={"X-API-Key": sender}).json() == []
        assert client.get(f"/inbound/{inbound_id}", headers={"X-API-Key": sender}).status_code == 404
        assert client.get(f"/inbound/{inbound_id}/pdf", headers={"X-API-Key": sender}).status_code == 404
        assert client.get("/inbound").status_code == 401


def test_inbound_download_token_is_bounded_and_never_anonymous_otherwise(isolated_installation, monkeypatch, tmp_path):
    monkeypatch.setenv("INBOUND_ENABLED", "true")
    monkeypatch.setenv("ASTERISK_INBOUND_SECRET", "sekret")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_tok"))
    monkeypatch.setenv("REQUIRE_API_KEY", "false")
    tiff = tmp_path / "in.tiff"
    Image.new("1", (20, 10), 1).save(tiff, format="TIFF")
    with TestClient(app, base_url="http://testserver") as client:
        inbound_id = _asterisk(client, tiff, uniqueid="tok")
        engine = sa.create_engine(isolated_installation["DATABASE_URL"])
        try:
            with engine.connect() as c:
                pdf_token = c.execute(sa.text("SELECT pdf_token FROM inbound_faxes WHERE id = :id"),
                                      {"id": inbound_id}).scalar_one()
            # REQUIRE_API_KEY=false never means anonymous access to faxes.
            assert client.get("/inbound").status_code == 401
            assert client.get(f"/inbound/{inbound_id}/pdf").status_code == 401
            assert client.get(f"/inbound/{inbound_id}/pdf", params={"token": "wrong"}).status_code == 401
            good = client.get(f"/inbound/{inbound_id}/pdf", params={"token": pdf_token})
            assert good.status_code == 200 and good.headers["content-type"].startswith("application/pdf")
            with engine.begin() as c:
                c.execute(sa.text("UPDATE inbound_faxes SET pdf_token_expires_at = :past WHERE id = :id"),
                          {"past": datetime.utcnow() - timedelta(minutes=1), "id": inbound_id})
            assert client.get(f"/inbound/{inbound_id}/pdf", params={"token": pdf_token}).status_code == 401
        finally:
            engine.dispose()
