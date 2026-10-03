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


def test_trunk_call_details_add_one_call_record_without_changing_the_fax(isolated_installation, monkeypatch, tmp_path):
    monkeypatch.setenv("INBOUND_ENABLED", "true")
    monkeypatch.setenv("ASTERISK_INBOUND_SECRET", "sekret")
    monkeypatch.setenv("FAX_DATA_DIR", str(tmp_path / "faxdata_call"))
    monkeypatch.setenv("SIP_TRUNK_PRESET", "telnyx")
    tiff = tmp_path / "in.tiff"
    Image.new("1", (20, 10), 1).save(tiff, format="TIFF")
    call = {"did": "+15555550199", "caller": "+15555550100", "started_at": 1791049108,
            "answered_at": 1791049108, "ended_at": 1791049134, "pages": 2, "t38": True,
            "remote_station_id_b64": "KzE1NTU1NTUwMTAw"}
    with TestClient(app, base_url="http://testserver") as client:
        payload = {"tiff_path": str(tiff), "to_number": "+15555550199", "from_number": "+15555550100",
                   "faxstatus": "SUCCESS", "faxpages": 2, "uniqueid": "1791049108.4", "call": call}
        first = client.post("/_internal/asterisk/inbound", headers={"X-Internal-Secret": "sekret"}, json=payload)
        assert first.status_code == 200, first.text
        # A repeated report of the same Asterisk call stores the fax but not a second call.
        again = client.post("/_internal/asterisk/inbound", headers={"X-Internal-Secret": "sekret"}, json=payload)
        assert again.status_code == 200
        plain = _asterisk(client, tiff, uniqueid="no-call-details")
        engine = sa.create_engine(isolated_installation["DATABASE_URL"])
        try:
            with engine.connect() as c:
                rows = c.execute(sa.text("SELECT direction, call_id, job_id, did, caller, connected_seconds, t38, "
                                         "pages, trunk_preset, remote_station_id FROM sip_call_records")).all()
                fax = c.execute(sa.text("SELECT to_number, pages, status FROM inbound_faxes WHERE id = :id"),
                                {"id": first.json()["id"]}).one()
                plain_fax = c.execute(sa.text("SELECT to_number FROM inbound_faxes WHERE id = :id"),
                                      {"id": plain}).one()
        finally:
            engine.dispose()
        assert rows == [("inbound", "1791049108.4", first.json()["id"], "+15555550199", "+15555550100", 26,
                         "yes", 2, "telnyx", "+15555550100")]
        assert tuple(fax) == ("+15555550199", 2, "SUCCESS")
        assert tuple(plain_fax) == ("+15551234567",)
