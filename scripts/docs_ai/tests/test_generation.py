"""Public documentation is generated from source, never operator configuration."""
from importlib import import_module
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]


def generator():
    try:
        return import_module("scripts.docs_ai.generate_reference")
    except ModuleNotFoundError:
        pytest.fail("Deterministic source documentation generator is missing")


def test_fresh_reference_records_source_and_ignores_operator_secrets(tmp_path, monkeypatch):
    module = generator()
    marker = "synthetic-private-operator-secret"
    monkeypatch.setenv("PHAXIO_API_SECRET", marker)
    monkeypatch.setenv("PHAXIO_CALLBACK_TOKEN", marker)
    monkeypatch.setenv("FAXBOT_PROVIDERS_DIR", str(tmp_path / "private-operator-providers"))
    provenance = module.generate(tmp_path / "generated")
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert provenance["source_sha"] == expected
    assert provenance["coverage"] == "source declarations; no live installation state or provider verification"
    output = tmp_path / "generated"
    spec = json.loads((output / "openapi.json").read_text())
    assert "/fax" in spec["paths"]
    assert spec["x-faxbot-source"]["source_sha"] == expected
    assert all(server["url"] != "https://api.faxbot.net" for server in spec.get("servers", []))
    schema = json.loads((output / "configuration.schema.json").read_text())
    assert "FAX_OUTBOUND_BACKEND" in schema["properties"]
    assert "default" not in schema["properties"]["PHAXIO_API_SECRET"]
    assert schema["properties"]["PHAXIO_API_SECRET"]["writeOnly"] is True
    assert "default" not in schema["properties"]["PHAXIO_CALLBACK_TOKEN"]
    assert schema["properties"]["PHAXIO_CALLBACK_TOKEN"]["writeOnly"] is True
    references = (output / "configuration.md").read_text()
    assert "PHAXIO_API_SECRET" in references and "secret" in references
    providers = json.loads((output / "providers.json").read_text())
    assert {"phaxio", "sinch", "sip"} <= set(providers)
    assert providers["sip"]["traits"]["requires_tiff"] is True
    callbacks = (output / "outbound-callbacks.md").read_text()
    assert "verify_phaxio_signature" in callbacks
    assert "callback_url_with_locators" in callbacks
    assert "CapturedCallbacks.receive" in callbacks
    assert "outbound-callbacks.md" in (output / "index.md").read_text()
    for name in ("provider_signatures", "callback_locator", "outbound_callbacks"):
        source = "api/app/" + name + ".py"
        assert provenance["source_files"][source] == hashlib.sha256((ROOT / source).read_bytes()).hexdigest()
    assert not any(marker in path.read_text() for path in output.iterdir())


def test_callback_reference_reads_exact_source_without_importing_runtime(monkeypatch):
    module = generator()
    monkeypatch.setitem(sys.modules, "api.app.outbound_callbacks", None)
    provenance = {"source_sha": "1" * 40, "source_tree_dirty": False}
    result = module.callback_reference(provenance)
    assert result == module.callback_reference(provenance)
    for path, symbols in module.CALLBACK_SOURCES.items():
        for symbol in symbols:
            assert "```python\n" + module.source_excerpt(path, symbol) + "\n```" in result
    assert "configuration.credentials" in result
    assert "Disabled verification means callbacks are disabled" in result


def test_source_excerpt_tracks_edits_and_fails_for_missing_declarations(tmp_path, monkeypatch):
    module = generator()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    source = tmp_path / "synthetic.py"
    source.write_text("class Captured:\n    def receive(self):\n        return 'before'\n")
    assert module.source_excerpt("synthetic.py", "Captured.receive") == "def receive(self):\n    return 'before'"
    source.write_text("class Captured:\n    def receive(self):\n        return 'after'\n")
    assert module.source_excerpt("synthetic.py", "Captured.receive") == "def receive(self):\n    return 'after'"
    with pytest.raises(ValueError, match="Missing source declaration"):
        module.source_excerpt("synthetic.py", "Captured.missing")


def test_source_sha_mismatch_is_rejected_before_generation(tmp_path):
    module = generator()
    with pytest.raises(ValueError, match="source SHA"):
        module.generate(tmp_path / "generated", source_sha="1" * 40)
    assert not (tmp_path / "generated").exists()


def test_public_configuration_schema_never_materializes_factory_host_paths():
    schema = generator().configuration_schema()
    assert "default" not in schema["properties"]["FAXBOT_PROVIDERS_DIR"]
    for value in schema["properties"].values():
        if value.get("secret"):
            assert "default" not in value


def test_diff_plan_defaults_to_actual_previous_commit(tmp_path, monkeypatch):
    module = import_module("scripts.docs_ai.generate_docs_from_diff")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.name", "Synthetic Docs Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "docs-test@example.invalid"], cwd=tmp_path, check=True)
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "app.py").write_text("before\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "before"], cwd=tmp_path, check=True)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    (tmp_path / "api" / "app.py").write_text("after\n")
    subprocess.run(["git", "commit", "-qam", "after"], cwd=tmp_path, check=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setenv("DOCS_BASE_SHA", before)
    plan = module.build_plan()
    assert "api/app.py" in plan
    assert f"Base: {before}" in plan


def test_diff_plan_handles_first_push_without_self_diff(tmp_path, monkeypatch):
    module = import_module("scripts.docs_ai.generate_docs_from_diff")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "config", "user.name", "Synthetic Docs Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "docs-test@example.invalid"], cwd=tmp_path, check=True)
    (tmp_path / "new.md").write_text("new\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=tmp_path, check=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setenv("DOCS_BASE_SHA", "0" * 40)
    assert "new.md" in module.build_plan()
