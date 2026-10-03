"""Provider resources must keep their identity across cwd and runtime layouts."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]


def run_resource_probe(root, cwd, package):
    """Import real resources in a fresh, credential-free interpreter."""
    script = f"""
import json
from {package}.config_paths import provider_traits_path, providers_dir, faxbot_config_path, plugin_registry_path
from {package}.provider_catalog import ProviderCatalog
catalog = ProviderCatalog.load(provider_traits_path(), providers_dir())
print(json.dumps({{
    'sip': {{'kind': catalog.get('sip').kind, 'traits': catalog.get('sip').traits.as_dict()}},
    'documo': {{'kind': catalog.get('documo').kind, 'traits': catalog.get('documo').traits.as_dict()}},
    'config_path': str(faxbot_config_path()),
    'registry': json.loads(plugin_registry_path().read_text(encoding='utf-8')),
}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=cwd, capture_output=True,
        text=True, timeout=20, env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": os.pathsep.join((str(root), str(root / "api"))),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("package", ["app", "api.app"])
@pytest.mark.parametrize("working_directory", ["root", "api", "unrelated"])
def test_bundled_traits_config_and_registry_ignore_source_cwd(tmp_path, package, working_directory):
    cwd = {"root": ROOT, "api": ROOT / "api", "unrelated": tmp_path}[working_directory]
    data = run_resource_probe(ROOT, cwd, package)
    assert data["sip"]["kind"] == "self_hosted"
    assert data["sip"]["traits"]["requires_ami"] is True
    assert data["documo"]["kind"] == "cloud"
    assert data["documo"]["traits"]["requires_tiff"] is False
    assert Path(data["config_path"]) == ROOT / "config" / "faxbot.config.json"
    assert data["registry"]["items"][0]["description"] == "Recommended for most users; HIPAA-ready with BAA."


def test_flattened_image_layout_loads_bundled_resources_from_unrelated_cwd(tmp_path):
    runtime = tmp_path / "image-app"
    shutil.copytree(ROOT / "api" / "app", runtime / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "config", runtime / "config")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    data = run_resource_probe(runtime, cwd, "app")
    assert data["sip"]["traits"]["requires_ami"] is True
    assert data["documo"]["kind"] == "cloud"
    assert Path(data["config_path"]) == runtime / "config" / "faxbot.config.json"
    assert data["registry"]["items"][0]["description"] == "Recommended for most users; HIPAA-ready with BAA."


def test_old_api_provider_directory_cannot_shadow_bundled_source_resources(tmp_path):
    runtime = tmp_path / "source-checkout"
    shutil.copytree(ROOT / "api" / "app", runtime / "api" / "app", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "config", runtime / "config")
    # The previous installer created this from cwd=api. It is not the bundle.
    (runtime / "api" / "config" / "providers").mkdir(parents=True)
    data = run_resource_probe(runtime, tmp_path, "app")
    assert data["sip"]["traits"]["requires_ami"] is True
    assert data["documo"]["kind"] == "cloud"
    assert Path(data["config_path"]) == runtime / "config" / "faxbot.config.json"
    assert data["registry"]["items"][0]["description"] == "Recommended for most users; HIPAA-ready with BAA."


def manifest(provider_id="synthetic-provider.v1"):
    return {
        "id": provider_id, "name": "Synthetic resource marker", "kind": "cloud",
        "traits": {"supports_inbound": True, "requires_tiff": False},
        "allowed_domains": ["synthetic.invalid"],
        "actions": {
            "send_fax": {"url": "https://synthetic.invalid/resource-marker/send"},
            "get_status": {"url": "https://synthetic.invalid/resource-marker/status"},
        },
    }


@pytest.fixture
def provider_client(isolated_installation, monkeypatch, tmp_path):
    from app import config, main
    providers = tmp_path / "operator-providers"
    for name, value in {
        "FAXBOT_PROVIDERS_DIR": str(providers),
        "FAXBOT_CONFIG_PATH": str(tmp_path / "operator-config.json"),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'paths.db'}",
        "FAX_DATA_DIR": str(tmp_path / "faxdata"), "FAX_DISABLED": "true",
        # A selected custom provider must exist before canonical activation.
        "FAX_BACKEND": "phaxio", "FAX_OUTBOUND_BACKEND": "phaxio",
        "FAX_INBOUND_BACKEND": "phaxio", "INBOUND_ENABLED": "false",
        "REQUIRE_API_KEY": "true", "API_KEY": "synthetic-paths-key",
        "ENABLE_PERSISTED_SETTINGS": "false", "FEATURE_V3_PLUGINS": "true",
        "MAX_REQUESTS_PER_MINUTE": "0", "ENFORCE_PUBLIC_HTTPS": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    with TestClient(main.app, headers={"X-API-Key": "synthetic-paths-key"}) as client:
        yield client, providers
    # Client lifespan loads a provider cache; don't leave a removed test root cached.
    config._TRAITS_CACHE["registry"] = {}


def install(client, provider_id="synthetic-provider.v1"):
    return client.post("/admin/plugins/http/install", json={"manifest": manifest(provider_id)})


def activate(client, provider_id="synthetic-provider.v1"):
    response = client.put("/admin/settings", json={
        "backend": provider_id, "outbound_backend": provider_id,
    })
    assert response.status_code == 200, response.text
    return response


def test_explicit_root_install_is_visible_to_traits_discovery_config_and_diagnostics(provider_client):
    from app import main
    client, providers = provider_client
    response = install(client)
    assert response.status_code == 200
    selected = providers / "synthetic-provider.v1" / "manifest.json"
    assert Path(response.json()["path"]) == selected
    assert json.loads(selected.read_text())["name"] == "Synthetic resource marker"
    before = client.get("/plugins/synthetic-provider.v1/config").json()
    assert before["enabled"] is False  # Installation is distinct from activation.
    activate(client)
    runtime = main.app.state.configuration_runtime
    snapshot = runtime.manager.store.read()
    catalog = runtime.manager.catalog_loader(snapshot.active.values)
    assert catalog.get("synthetic-provider.v1").kind == "cloud"
    assert catalog.get("synthetic-provider.v1").traits.as_dict()["supports_inbound"] is True
    assert "synthetic-provider.v1" in catalog.provider_ids
    assert any(item["id"] == "synthetic-provider.v1" for item in client.get("/plugins").json()["items"])
    view = client.get("/plugins/synthetic-provider.v1/config").json()
    assert view["enabled"] is True
    assert view["settings"] == {}
    assert view["role"] == "outbound"
    diagnostics = client.post("/admin/diagnostics/run").json()["checks"]["plugins"]
    assert diagnostics["installed"] == 1
    assert diagnostics["manifests"][0]["name"] == "Synthetic resource marker"
    update = client.put("/plugins/synthetic-provider.v1/config", json={"settings": {"resource_marker": "operator-config"}})
    assert update.status_code == 200
    desired = runtime.manager.store.read().desired
    assert desired.plugins.as_dict()["settings"]["synthetic-provider.v1"] == {"resource_marker": "operator-config"}
    assert Path(update.json()["path"]) == Path(desired.values.faxbot_config_path)
    assert not Path(update.json()["path"]).exists()  # Recovery export is explicit.
    assert update.json()["settings"] == {"resource_marker": "***"}


def test_custom_manifest_send_preparation_uses_captured_traits(provider_client):
    from app import main
    client, _ = provider_client
    assert install(client).status_code == 200
    # Give the manifest a telephony id to prove its presence suppresses TIFF preparation.
    assert install(client, "freeswitch").status_code == 200
    activate(client, "freeswitch")
    response = client.post("/fax", data={"to": "+15551230001"},
        files={"file": ("document.txt", b"Resource path original document", "text/plain")})
    assert response.status_code == 202, response.text
    with main.SessionLocal() as db:
        job = db.get(main.FaxJob, response.json()["id"])
        assert job.tiff_path == ""
    bound = main.app.state.configuration_runtime.manager.store.outbound_profile(response.json()["id"])
    assert bound.configuration.manifest["id"] == "freeswitch"
    assert bound.configuration.traits["requires_tiff"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dispatch", "historical_refresh"])
async def test_custom_manifest_dispatch_and_historical_refresh_use_accepted_snapshot(provider_client, monkeypatch, operation):
    from app import main
    client, providers = provider_client
    assert install(client).status_code == 200
    activate(client)
    runtime = main.app.state.configuration_runtime
    accepted_revision = runtime.manager.store.read().active
    response = client.post("/fax", data={"to": "+15551230001"}, files={"file": ("document.txt", b"Original resource path document", "text/plain")})
    assert response.status_code == 202
    job_id = response.json()["id"]
    bound = runtime.manager.store.outbound_profile(job_id)
    assert bound.configuration.manifest == manifest()
    # Rotation and installation edits cannot rewrite an accepted provider frame.
    activate(client, "phaxio")
    edited = manifest()
    for action in edited["actions"].values():
        action["url"] = "https://synthetic.invalid/edited-install"
    (providers / "synthetic-provider.v1" / "manifest.json").write_text(json.dumps(edited))
    # Only the HTTP transport is synthetic; parsing, templates, DB updates and paths stay real.
    requests = []

    async def synthetic_http(client, method, url, **kwargs):
        requests.append(str(url))
        import httpx
        return httpx.Response(200, json={"id": "selected-resource-job", "status": "sent"}, request=httpx.Request(method, url))

    monkeypatch.setattr("app.plugins.http_provider.httpx.AsyncClient.request", synthetic_http)
    if operation == "dispatch":
        await main._dispatch_accepted_job(accepted_revision, job_id, "+15551230001",
            str(Path(accepted_revision.values.fax_data_dir) / f"{job_id}.pdf"), "")
        expected_url = "https://synthetic.invalid/resource-marker/send"
    else:
        # Historical binding uses the job's provider after current selection changes.
        with runtime.frame():
            refreshed = await main.admin_refresh_job(job_id)
        assert refreshed.status == "sent"
        expected_url = "https://synthetic.invalid/resource-marker/status"
    with main.SessionLocal() as db:
        job = db.get(main.FaxJob, job_id)
        assert job.status == "sent", job.error
        assert job.provider_sid == "selected-resource-job"
    assert requests == [expected_url]


@pytest.mark.parametrize("provider_id", ["../escaped", "..", ".", "/absolute", r"..\escaped", "a/b", "a%2Fb", "%2e%2e", "a%252fb", "bad\x00id", " spaced ", "C:escape"])
def test_invalid_provider_ids_refuse_install_and_bulk_import_without_mutation(provider_client, provider_id):
    client, providers = provider_client
    if provider_id == "/absolute":
        provider_id = str(providers.parent / "escaped-absolute")
    response = install(client, provider_id)
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid provider id or path"}
    bulk = client.post("/admin/plugins/http/import-manifests", json={"items": [manifest(provider_id)]})
    assert bulk.status_code == 200
    assert bulk.json()["imported"] == []
    assert bulk.json()["errors"][0]["error"] == "Invalid provider id or path"
    assert not providers.exists()


@pytest.mark.parametrize("escape", ["directory", "manifest", "directory_backlink"])
def test_escaped_symlink_manifest_is_refused_by_install_scans_and_reads(provider_client, tmp_path, escape):
    from app import main
    from app.config_paths import InvalidProviderPath, provider_manifest_path
    from app.provider_catalog import ProviderCatalogError
    client, providers = provider_client
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_manifest = outside / "manifest.json"
    outside_manifest.write_text(json.dumps(manifest("escaped")))
    providers.mkdir()
    if escape == "directory_backlink":
        inside = providers / "inside"
        inside.mkdir()
        (inside / "manifest.json").write_text(json.dumps(manifest("inside")))
        outside_manifest.unlink()
        outside_manifest.symlink_to(inside / "manifest.json")
        (providers / "escaped").symlink_to(outside, target_is_directory=True)
    elif escape == "directory":
        (providers / "escaped").symlink_to(outside, target_is_directory=True)
    else:
        (providers / "escaped").mkdir()
        (providers / "escaped" / "manifest.json").symlink_to(outside_manifest)
    original = outside_manifest.read_bytes()
    response = install(client, "escaped")
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid provider id or path"}
    assert outside_manifest.read_bytes() == original
    runtime = main.app.state.configuration_runtime
    snapshot = runtime.manager.store.read()
    with runtime.frame(), pytest.raises(InvalidProviderPath):
        provider_manifest_path("escaped")
    # Present invalid resources fail catalog validation instead of disappearing.
    with pytest.raises(ProviderCatalogError, match="Invalid provider resource"):
        runtime.manager.catalog_loader(snapshot.active.values)
    read = client.get("/plugins/escaped/config")
    assert read.status_code == 400
    assert read.json() == {"detail": "Invalid provider resource."}
    update = client.put("/plugins/escaped/config", json={"enabled": True})
    assert update.status_code == 400
    assert update.json() == {"detail": "Invalid provider resource."}
    assert runtime.manager.store.read() == snapshot
    assert outside_manifest.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["prepare", "dispatch", "historical_refresh"])
async def test_escaped_manifest_is_never_used_for_a_fax(provider_client, monkeypatch, tmp_path, operation):
    from app import main
    from app.config_store import UnboundProviderProfile
    from fastapi import HTTPException
    client, providers = provider_client
    outside = tmp_path / "outside-provider"
    outside.mkdir()
    (outside / "manifest.json").write_text(json.dumps(manifest("escaped")))
    providers.mkdir()
    (providers / "escaped").symlink_to(outside, target_is_directory=True)
    runtime = main.app.state.configuration_runtime
    snapshot = runtime.manager.store.read()
    original = (outside / "manifest.json").read_bytes()
    calls = []
    async def forbidden_provider_request(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unsafe or unbound provider must not be contacted")
    monkeypatch.setattr("app.plugins.http_provider.httpx.AsyncClient.request", forbidden_provider_request)
    if operation == "prepare":
        selection = client.put("/admin/settings", json={"backend": "escaped", "outbound_backend": "escaped"})
        assert selection.status_code == 400
        assert selection.json() == {"detail": "Invalid provider resource."}
        assert runtime.manager.store.read() == snapshot
        assert client.get("/admin/fax-jobs").json()["total"] == 0
        assert not list(Path(snapshot.active.values.fax_data_dir).glob("*.pdf"))
        assert not list(Path(snapshot.active.values.fax_data_dir).glob("*.tif*"))
        assert (outside / "manifest.json").read_bytes() == original
        assert not calls
        return
    # A legacy provider name has no verified account/manifest binding. Reconcile
    # it rather than reading today's installation or borrowing another profile.
    with main.SessionLocal() as db:
        job = main.FaxJob(id="escaped-provider-job", to_number="+15551230001", file_name="document.txt", tiff_path="", status="queued", backend="escaped")
        db.add(job)
        db.commit()
    with pytest.raises(UnboundProviderProfile):
        runtime.manager.store.outbound_profile("escaped-provider-job")
    if operation == "dispatch":
        with runtime.frame():
            await main._send_via_manifest("escaped-provider-job", "+15551230001", "unused.pdf")
        with main.SessionLocal() as db:
            job = db.get(main.FaxJob, "escaped-provider-job")
            assert job.status == "failed"
            assert job.error == "Provider operation failed; reconcile delivery before retrying."
    else:
        with runtime.frame(), pytest.raises(HTTPException) as error:
            await main.admin_refresh_job("escaped-provider-job")
        assert error.value.status_code == 409
        assert "reconciliation" in error.value.detail
    assert not calls
    assert (outside / "manifest.json").read_bytes() == original
    assert not list(Path(snapshot.active.values.fax_data_dir).glob("*.pdf"))
    assert not list(Path(snapshot.active.values.fax_data_dir).glob("*.tif*"))


def test_operator_root_symlink_and_relative_path_overrides_remain_supported(monkeypatch, tmp_path):
    from app.config import use_configuration
    from app.config_values import ConfigurationValues
    from app.config_paths import provider_manifest_path, faxbot_config_path, plugin_registry_path, provider_traits_path
    from app.provider_catalog import ProviderCatalog
    monkeypatch.chdir(tmp_path)
    providers = tmp_path / "operator-providers"
    providers.mkdir()
    alias = tmp_path / "providers-alias"
    alias.symlink_to(providers, target_is_directory=True)
    registry = tmp_path / "relative-registry.json"
    registry.write_text(json.dumps({"items": [{"id": "operator-registry-marker"}]}))
    values = ConfigurationValues.from_environment({
        "FAXBOT_PROVIDERS_DIR": "providers-alias", "FAXBOT_CONFIG_PATH": "relative-settings.json",
        "PLUGIN_REGISTRY_PATH": registry.name,
    })
    with use_configuration(values):
        selected = provider_manifest_path("safe-name_1.v2")
        selected.parent.mkdir()
        selected.write_text(json.dumps(manifest("safe-name_1.v2")))
        assert selected == providers / "safe-name_1.v2" / "manifest.json"
        assert faxbot_config_path() == tmp_path / "relative-settings.json"
        assert json.loads(plugin_registry_path().read_text()) == {"items": [{"id": "operator-registry-marker"}]}
        catalog = ProviderCatalog.load(provider_traits_path(), Path(values.providers_dir))
        assert catalog.get("safe-name_1.v2").manifest.as_dict() == manifest("safe-name_1.v2")


def test_repo_scrape_reads_packaged_examples_instead_of_conflicting_cwd_file(provider_client, tmp_path):
    client, providers = provider_client
    (tmp_path / "api_plugins_list.md").write_text("```json\n" + json.dumps(manifest("wrong-cwd-provider")) + "\n```")
    response = client.post("/admin/plugins/http/import-manifests", json={"source": "repo_scrape"})
    # Bundled examples contain plain JSON, while the existing parser accepts
    # fences only. The path repair must load that real resource, never the cwd
    # decoy. Parsing those examples is a separate provider-installation repair.
    assert response.status_code == 400
    assert response.json() == {"detail": "No manifest candidates provided"}
    assert not providers.exists()
