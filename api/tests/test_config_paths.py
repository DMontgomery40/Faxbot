"""Provider resources must keep their identity across cwd and runtime layouts."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from api.tests.test_outbound_store import installation
from api.tests.test_schema import database


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
    'registry_path': str(plugin_registry_path()),
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
    assert Path(data["registry_path"]) == ROOT / "config" / "plugin_registry.json"
    assert data["registry"] == json.loads((ROOT / "config" / "plugin_registry.json").read_text(encoding="utf-8"))


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
    assert Path(data["registry_path"]) == runtime / "config" / "plugin_registry.json"
    assert data["registry"] == json.loads((ROOT / "config" / "plugin_registry.json").read_text(encoding="utf-8"))


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
    assert Path(data["registry_path"]) == runtime / "config" / "plugin_registry.json"
    assert data["registry"] == json.loads((ROOT / "config" / "plugin_registry.json").read_text(encoding="utf-8"))


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


@pytest.fixture
def captured_provider_installation(installation, monkeypatch, tmp_path):
    """Capture a validated operator resource without starting an HTTP lifespan."""
    from api.app.config_paths import provider_traits_path
    from api.app.config_profiles import ProviderConfiguration
    from api.app.provider_catalog import ProviderCatalog
    configuration, store, snapshot = installation
    providers = tmp_path / "operator-providers"
    selected = providers / "synthetic-provider.v1" / "manifest.json"
    selected.parent.mkdir(parents=True)
    original = manifest()
    original["actions"]["get_status"]["method"] = "GET"
    selected.write_text(json.dumps(original))
    definition = ProviderCatalog.load(provider_traits_path(), providers).get(original["id"])
    values = snapshot.active.values.with_patch({
        "backend": original["id"], "outbound_backend": original["id"],
        "fax_data_dir": str(tmp_path / "artifacts"), "providers_dir": str(providers),
        "feature_v3_plugins": True,
    })
    snapshot = configuration.apply(snapshot, values, actor="test", restart_required=False,
        providers={"outbound": ProviderConfiguration(definition.id,
            traits=definition.traits.as_dict(), manifest=definition.manifest.as_dict())})
    monkeypatch.chdir(tmp_path)
    return configuration, store, snapshot, selected, original


async def accept_resource_document(installation):
    from datetime import datetime
    from io import BytesIO
    from uuid import uuid4
    from fastapi import UploadFile
    from api.app.documents import prepare_upload
    configuration, _, snapshot, _, _ = installation
    job_id = uuid4().hex
    prepared = await prepare_upload(
        UploadFile(file=BytesIO(b"Original resource path document"), filename="document.txt"),
        job_id=job_id, data_dir=snapshot.active.values.fax_data_dir, max_bytes=100,
        requires_tiff=False)
    now = datetime.utcnow()
    configuration.accept_outbound(snapshot.active, {
        "id": job_id, "to_number": "+12025550123", "file_name": prepared.original_name,
        "tiff_path": prepared.tiff_path or "", "status": "queued", "pages": prepared.pages,
        "created_at": now, "updated_at": now,
    })
    return job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dispatch", "historical_refresh"])
async def test_custom_manifest_dispatch_and_historical_refresh_use_accepted_snapshot(
    captured_provider_installation, monkeypatch, operation,
):
    from contextlib import contextmanager
    import sqlalchemy as sa
    from api.app.config import use_configuration
    from api.app.config_profiles import ProviderConfiguration
    from api.app.outbound_polling import OutboundPoller
    from api.app.outbound_transport import CapturedTransport
    from api.app.outbound_worker import OutboundWorker
    configuration, store, snapshot, selected, original = captured_provider_installation
    job_id = await accept_resource_document(captured_provider_installation)
    assert configuration.outbound_profile(job_id).configuration.manifest == original
    if operation == "historical_refresh":
        # A status lookup needs an acknowledged attempt, never a held upload.
        claim = store.claim("synthetic-original-worker")
        assert store.begin_submission(claim)
        store.record_receipt(claim, provider_sid="selected-resource-job", status="in_progress")
    configuration.apply(snapshot, snapshot.active.values.with_patch({
        "backend": "phaxio", "outbound_backend": "phaxio",
    }), actor="test", restart_required=False, providers={"outbound": ProviderConfiguration("phaxio")})
    edited = manifest()
    for action in edited["actions"].values():
        action["url"] = "https://synthetic.invalid/edited-install"
    selected.write_text(json.dumps(edited))
    requests, frames = [], []

    async def synthetic_http(client, method, url, **kwargs):
        import httpx
        requests.append((method, str(url)))
        return httpx.Response(200, json={"id": "selected-resource-job", "status": "success"},
            request=httpx.Request(method, url))

    class Runtime:
        @contextmanager
        def frame(self, revision):
            frames.append(revision.id)
            with use_configuration(revision.values):
                yield

    monkeypatch.setattr("api.app.plugins.http_provider.httpx.AsyncClient.request", synthetic_http)
    if operation == "dispatch":
        assert await OutboundWorker(store, CapturedTransport(store, Runtime())).step()
        assert frames == [snapshot.active.id]
        expected_request = ("POST", "https://synthetic.invalid/resource-marker/send")
    else:
        assert await OutboundPoller(store).refresh(job_id)
        expected_request = ("GET", "https://synthetic.invalid/resource-marker/status")
    with configuration.engine.connect() as connection:
        job = connection.execute(sa.select(configuration.jobs).where(
            configuration.jobs.c.id == job_id)).mappings().one()
        assert job["status"] == "success" and job["provider_sid"] == "selected-resource-job"
        assert connection.scalar(sa.select(sa.func.count()).select_from(store.attempts)) == 1
    assert store.get(job_id)["state"] == "success"
    assert requests == [expected_request]


@pytest.mark.asyncio
async def test_held_manifest_refresh_never_contacts_provider(captured_provider_installation, monkeypatch):
    from api.app.outbound_polling import OutboundPoller
    configuration, store, snapshot, selected, original = captured_provider_installation
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({"fax_disabled": True}),
        actor="test", restart_required=False)
    fixture = (configuration, store, snapshot, selected, original)
    job_id = await accept_resource_document(fixture)
    before, history = store.get(job_id), store.history(job_id)
    async def forbidden_request(*args, **kwargs):
        raise AssertionError("Held fax must not contact any provider")
    monkeypatch.setattr("api.app.plugins.http_provider.httpx.AsyncClient.request", forbidden_request)
    assert await OutboundPoller(store).refresh(job_id) is False
    assert store.get(job_id) == before and store.history(job_id) == history
    assert before["state"] == before["dispatch_mode"] == "held"
    assert before["attempt_id"] is None


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


def test_escaped_manifest_cannot_be_selected_for_a_fax(provider_client, monkeypatch, tmp_path):
    from app import main
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
    selection = client.put("/admin/settings", json={"backend": "escaped", "outbound_backend": "escaped"})
    assert selection.status_code == 400
    assert selection.json() == {"detail": "Invalid provider resource."}
    assert runtime.manager.store.read() == snapshot
    assert client.get("/admin/fax-jobs").json()["total"] == 0
    assert not list(Path(snapshot.active.values.fax_data_dir).glob("*.pdf"))
    assert not list(Path(snapshot.active.values.fax_data_dir).glob("*.tif*"))
    assert (outside / "manifest.json").read_bytes() == original
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["dispatch", "historical_refresh"])
async def test_legacy_escaped_manifest_is_never_used_for_a_fax(installation, monkeypatch, tmp_path, operation):
    from datetime import datetime
    from uuid import uuid4
    import sqlalchemy as sa
    from api.app.config_paths import provider_traits_path
    from api.app.config_store import UnboundProviderProfile
    from api.app.outbound_polling import OutboundPoller
    from api.app.outbound_store import DeliveryConflict
    from api.app.outbound_transport import CapturedTransport
    from api.app.outbound_worker import OutboundWorker
    from api.app.provider_catalog import ProviderCatalog, ProviderCatalogError
    configuration, store, snapshot = installation
    providers = tmp_path / "operator-providers"
    outside = tmp_path / "outside-provider"
    outside.mkdir()
    outside_manifest = outside / "manifest.json"
    outside_manifest.write_text(json.dumps(manifest("escaped")))
    providers.mkdir()
    (providers / "escaped").symlink_to(outside, target_is_directory=True)
    snapshot = configuration.apply(snapshot, snapshot.active.values.with_patch({
        "fax_data_dir": str(tmp_path / "artifacts"), "providers_dir": str(providers),
    }), actor="test", restart_required=False)
    original = outside_manifest.read_bytes()
    with pytest.raises(ProviderCatalogError, match="Invalid provider resource"):
        ProviderCatalog.load(provider_traits_path(), providers)
    calls = []
    async def forbidden_provider_request(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Unsafe or unbound provider must not be contacted")
    monkeypatch.setattr("api.app.plugins.http_provider.httpx.AsyncClient.request", forbidden_provider_request)
    # A legacy provider name has no verified account/manifest binding. Reconcile
    # it rather than reading today's installation or borrowing another profile.
    job_id, now = uuid4().hex, datetime.utcnow()
    with configuration.engine.begin() as connection:
        connection.execute(configuration.jobs.insert().values(id=job_id, to_number="+12025550123",
            file_name="document.txt", tiff_path="", status="queued", backend="escaped",
            created_at=now, updated_at=now))
        connection.execute(store.deliveries.insert().values(id=job_id, dispatch_mode="legacy",
            state="reconciliation_required", legacy_status="queued", version=1,
            created_at=now, updated_at=now))
        before_job = dict(connection.execute(sa.select(configuration.jobs)).mappings().one())
    before, history = store.get(job_id), store.history(job_id)
    with pytest.raises(UnboundProviderProfile):
        configuration.outbound_profile(job_id)
    if operation == "dispatch":
        class NoFrame:
            def frame(self, revision):
                raise AssertionError("Legacy job must not enter provider preparation")
        assert await OutboundWorker(store, CapturedTransport(store, NoFrame())).step() is False
    else:
        with pytest.raises(DeliveryConflict, match="No acknowledged provider identity"):
            await OutboundPoller(store).refresh(job_id)
    assert store.get(job_id) == before and store.history(job_id) == history
    with configuration.engine.connect() as connection:
        assert dict(connection.execute(sa.select(configuration.jobs)).mappings().one()) == before_job
        assert connection.scalar(sa.select(sa.func.count()).select_from(store.attempts)) == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(configuration.job_bindings)) == 0
    assert not calls
    assert outside_manifest.read_bytes() == original
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
