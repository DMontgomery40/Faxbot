"""Explicit provider catalog contracts, with real operator resource files."""

from importlib import import_module
import json
import os
from pathlib import Path

import h11
import httpx
import pytest


def catalog_module():
    return import_module("api.app.provider_catalog")


def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_explicit_base_paths_ignore_cwd_environment_and_schema(tmp_path, monkeypatch):
    traits = write_json(tmp_path / "operator" / "traits.json", {
        "_schema": {"version": 1},
        "sip": {"id": "sip", "kind": "self_hosted", "traits": {
            "requires_ami": True, "requires_tiff": True,
            "supports_inbound": False, "inbound_verification": "internal_secret",
            "future_trait": "retained",
        }},
    })
    unrelated = tmp_path / "cwd"
    unrelated.mkdir()
    write_json(unrelated / "config" / "provider_traits.json", {"wrong": {}})
    monkeypatch.chdir(unrelated)
    monkeypatch.setenv("FAXBOT_PROVIDERS_DIR", str(unrelated / "wrong"))
    monkeypatch.setenv("FAX_BACKEND", "wrong")
    before = dict(os.environ)

    catalog = catalog_module().ProviderCatalog.load(traits, tmp_path / "missing")

    assert catalog.provider_ids == frozenset({"sip"})
    sip = catalog.get("sip")
    assert sip.id == "sip"
    assert sip.kind == "self_hosted"
    assert sip.manifest is None
    assert sip.traits.as_dict() == {
        "requires_ami": True, "requires_tiff": True,
        "supports_inbound": False, "inbound_verification": "internal_secret",
        "future_trait": "retained",
    }
    assert dict(os.environ) == before


@pytest.mark.parametrize("key", [
    "requires_ghostscript", "requires_ami", "requires_tiff", "supports_inbound",
    "needs_storage", "outbound_status_only",
])
@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_known_traits_require_exact_booleans(tmp_path, key, value):
    path = write_json(tmp_path / "traits.json", {"provider": {"traits": {key: value}}})
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog_module().ProviderCatalog.load(path, tmp_path / "providers")


@pytest.mark.parametrize("document", [
    [], "private-source-marker", None,
    {"provider": []}, {"provider": None},
    {"provider": {"id": "private-source-marker"}},
    {"provider": {"kind": None}}, {"provider": {"kind": ""}},
    {"provider": {"traits": []}}, {"provider": {"traits": None}},
    {"provider": {"traits": {"inbound_verification": "private-source-marker"}}},
    {"provider": {"traits": {"inbound_verification": False}}},
    {"provider": {"traits": {"future": []}}},
    {"provider": {"traits": {"future": {"private-source-marker": True}}}},
    {"provider": {"traits": {"future": float("nan")}}},
    {"provider": {"traits": {"future": "\ud800"}}},
])
def test_malformed_base_definitions_fail_safely(tmp_path, document):
    path = write_json(tmp_path / "private-path-marker.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(path, tmp_path / "providers")
    assert "private" not in str(failure.value)
    assert "private" not in repr(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("provider_id", [
    "../escaped", "..", ".", "/absolute", "a/b", "a\\b", "a%2Fb", " spaced ",
    "C:escape", "bad\0id", "unicode-é", "a" * 256,
])
def test_base_identity_is_a_bounded_ascii_component(tmp_path, provider_id):
    path = write_json(tmp_path / "traits.json", {provider_id: {}})
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog_module().ProviderCatalog.load(path, tmp_path / "providers")


def test_valid_traits_and_identity_are_preserved(tmp_path):
    expected = {"requires_tiff": False, "future_bool": True, "future_integer": 2,
                "future_number": 1.5, "future_null": None, "future_text": "hello"}
    path = write_json(tmp_path / "traits.json", {"safe-name_1.v2": {"traits": expected}})
    catalog = catalog_module().ProviderCatalog.load(path, tmp_path / "providers")
    assert catalog.get("safe-name_1.v2").traits.as_dict() == expected
    assert catalog.get("safe-name_1.v2").kind == "cloud"


@pytest.mark.parametrize("verification", ["hmac", "basic", "internal_secret", "none"])
def test_supported_inbound_verification_values(tmp_path, verification):
    path = write_json(tmp_path / "traits.json", {"provider": {"traits": {
        "inbound_verification": verification,
    }}})
    catalog = catalog_module().ProviderCatalog.load(path, tmp_path / "providers")
    assert catalog.get("provider").traits.as_dict() == {"inbound_verification": verification}


def manifest(provider_id="installed", action="send_fax"):
    return {"id": provider_id, "name": "Installed operator provider",
            "actions": {action: {"url": "https://synthetic.invalid/fax"}},
            "extension_metadata": {"nested": [1, True, {"marker": "captured"}]}}


def test_manifests_capture_complete_contents_and_override_base_traits(tmp_path):
    base = write_json(tmp_path / "traits.json", {
        "sip": {"kind": "self_hosted", "traits": {
            "requires_ami": True, "requires_tiff": True, "supports_inbound": True,
            "future": "base", "needs_storage": True,
        }},
        "builtin": {"traits": {"requires_tiff": False}},
    })
    installed = manifest("sip")
    installed["traits"] = {"requires_tiff": False, "future": "manifest"}
    write_json(tmp_path / "providers" / "sip" / "manifest.json", installed)
    new = manifest("new-provider")
    new["traits"] = {"supports_inbound": True, "future_count": 4}
    write_json(tmp_path / "providers" / "new-provider" / "manifest.json", new)
    (tmp_path / "providers" / "empty-directory").mkdir()
    (tmp_path / "providers" / "readme.txt").write_text("not a provider")

    catalog = catalog_module().ProviderCatalog.load(base, tmp_path / "providers")

    assert catalog.provider_ids == frozenset({"sip", "builtin", "new-provider"})
    assert catalog.get("sip").kind == "self_hosted"
    assert catalog.get("sip").manifest.as_dict() == installed
    assert catalog.get("sip").traits.as_dict() == {
        "requires_ami": True, "requires_tiff": False, "supports_inbound": True,
        "future": "manifest", "needs_storage": True,
    }
    native = catalog.get("sip").native_definition
    assert native.id == "sip"
    assert native.manifest is None
    assert native.native_definition is None
    assert native.traits.as_dict() == {
        "requires_ami": True, "requires_tiff": True, "supports_inbound": True,
        "future": "base", "needs_storage": True,
    }
    detached_native_traits = native.traits.as_dict()
    detached_native_traits["requires_tiff"] = False
    assert native.traits.as_dict()["requires_tiff"] is True
    assert catalog.get("builtin").manifest is None
    assert catalog.get("builtin").native_definition is None
    assert catalog.get("new-provider").kind == "cloud"
    assert catalog.get("new-provider").traits.as_dict() == {
        "requires_ami": False, "requires_tiff": False, "supports_inbound": True,
        "future_count": 4,
    }
    assert catalog.get("new-provider").manifest.as_dict() == new
    assert catalog.get("new-provider").native_definition is None


@pytest.mark.parametrize("identity", ["local", "s3"])
def test_http_manifest_cannot_replace_reserved_storage_identity(tmp_path, identity):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest(identity)
    installed = write_json(tmp_path / "providers" / identity / "manifest.json", document)
    original = installed.read_bytes()
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert installed.read_bytes() == original


@pytest.mark.parametrize("identity", ["local", "s3", "LOCAL", "S3"])
def test_shared_http_document_validator_reserves_storage_identity(identity):
    module = catalog_module()
    with pytest.raises(module.ProviderCatalogError, match="reserved for storage"):
        module.validate_http_provider_document(manifest(identity))


def test_shared_http_document_validator_preserves_supported_recipe_and_rejects_bad_traits():
    module = catalog_module()
    document = manifest("sip", action="get_status")
    document["actions"]["get_status"]["headers"] = {"Authorization": "Bearer {{credentials.api_key}}"}
    original = json.dumps(document)
    assert module.validate_http_provider_document(document) is None
    assert json.dumps(document) == original
    document["traits"] = {"requires_tiff": "private-invalid-false"}
    with pytest.raises(module.ProviderCatalogError) as failure:
        module.validate_http_provider_document(document)
    assert "private" not in str(failure.value)
    assert failure.value.__suppress_context__


def test_new_load_sees_new_installs_while_prior_snapshots_are_immutable(tmp_path):
    base = write_json(tmp_path / "traits.json", {"builtin": {"traits": {"future": "before"}}})
    providers = tmp_path / "providers"
    before = catalog_module().ProviderCatalog.load(base, providers)
    document = manifest()
    write_json(providers / "installed" / "manifest.json", document)
    after = catalog_module().ProviderCatalog.load(base, providers)
    document["extension_metadata"]["nested"].append("edited on disk")
    write_json(providers / "installed" / "manifest.json", document)
    latest = catalog_module().ProviderCatalog.load(base, providers)

    assert before.provider_ids == frozenset({"builtin"})
    assert after.provider_ids == frozenset({"builtin", "installed"})
    assert after.get("installed").manifest.as_dict() == manifest()
    assert latest.get("installed").manifest.as_dict() == document
    detached = after.get("installed").manifest.as_dict()
    detached["extension_metadata"]["nested"].clear()
    after.get("builtin").traits.as_dict()["future"] = "changed"
    assert after.get("installed").manifest.as_dict() == manifest()
    assert after.get("builtin").traits.as_dict() == {"future": "before"}
    with pytest.raises(AttributeError):
        after.get("installed").kind = "modified"
    with pytest.raises(AttributeError):
        after.provider_ids = frozenset()
    with pytest.raises(TypeError):
        after._definitions["invented"] = after.get("installed")


@pytest.mark.parametrize("selector", ["_schema", "unknown-private-marker", "../escaped", "", None, []])
def test_unknown_selectors_fail_safely_without_builtin_fallback(tmp_path, selector):
    base = write_json(tmp_path / "traits.json", {"phaxio": {}})
    catalog = catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog.get(selector)
    assert "private" not in str(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("field,value", [
    ("actions", None), ("actions", []), ("actions", {}),
    ("actions", {"future_action": {"url": "https://synthetic.invalid/fax"}}),
    ("actions", {"send_fax": None}), ("actions", {"send_fax": []}),
    ("actions", {"send_fax": {}}),
    ("name", []), ("kind", False), ("traits", []),
    ("auth", []), ("auth", None), ("auth", {"scheme": False}),
    ("auth", {"scheme": "private-marker-unsupported"}),
    ("auth", {"scheme": "api_key_header", "header_name": []}),
    ("auth", {"scheme": "api_key_query", "query_name": False}),
    ("allowed_domains", "private-marker.invalid"),
    ("allowed_domains", [False]), ("allowed_domains", [""]),
    ("timeout_ms", True), ("timeout_ms", "15000"),
    ("timeout_ms", 0), ("timeout_ms", -1),
])
def test_manifest_known_top_level_shape_is_validated(tmp_path, field, value):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest()
    document[field] = value
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("field,value", [
    ("url", None), ("url", []), ("url", ""), ("url", "http://"),
    ("url", "file:///private-marker"), ("url", "ftp://synthetic.invalid/fax"),
    ("url", "https://synthetic.invalid:bad/fax"),
    ("method", []), ("method", ""), ("method", "private-marker"),
    ("headers", []), ("headers", {"Authorization": False}),
    ("headers", {"bad\nname": "private-marker"}),
    ("headers", {"Authorization": "private-marker\ninvalid"}),
    ("body", None), ("body", []), ("body", {"kind": "xml"}),
    ("body", {"kind": False}), ("body", {"template": []}),
    ("path_params", {}), ("path_params", [False]),
    ("path_params", [{"source": "job_id"}]),
    ("path_params", [{"name": "job_id", "source": []}]),
    ("response", []), ("response", {"job_id": []}),
    ("response", {"status": False}), ("response", {"error": {}}),
    ("response", {"status_map": []}), ("response", {"status_map": {"queued": []}}),
])
def test_known_action_fields_cannot_reach_runtime_with_malformed_shapes(tmp_path, field, value):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest()
    document["actions"]["send_fax"][field] = value
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer résumé"},
    {"Authorization": "Bearer \x00private-marker"},
    {"Authorization": "Bearer \x0bprivate-marker"},
    {"Authorization": " private-marker"},
    {"Authorization": "private-marker\t"},
], ids=["non_ascii", "nul", "vertical_tab", "leading_space", "trailing_tab"])
def test_literal_headers_rejected_by_runtime_fail_during_catalog_load(tmp_path, headers):
    # These literal headers fail before sending a request in the actual runtime.
    with pytest.raises((UnicodeEncodeError, h11.LocalProtocolError)):
        request = httpx.Request("POST", "https://synthetic.invalid/fax", headers=headers)
        h11.Request(method=request.method, target=request.url.raw_path,
                    headers=request.headers.raw)
    base = write_json(tmp_path / "traits.json", {})
    document = manifest()
    document["actions"]["send_fax"]["headers"] = headers
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert "private" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)


@pytest.mark.parametrize("value", [
    "", "Bearer synthetic", "Bearer\tsynthetic", "Bearer {{creds.api_key}}",
], ids=["empty", "ordinary", "internal_tab", "template"])
def test_supported_literal_header_values_remain_captured_and_serializable(tmp_path, value):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest()
    document["actions"]["send_fax"]["headers"] = {"Authorization": value}
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    catalog = catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    captured = catalog.get("installed").manifest.as_dict()
    assert captured == document
    request = httpx.Request("POST", "https://synthetic.invalid/fax",
                            headers=captured["actions"]["send_fax"]["headers"])
    h11.Request(method=request.method, target=request.url.raw_path,
                headers=request.headers.raw)
    assert request.headers["Authorization"] == value


def test_framing_header_templates_remain_recipe_data_in_the_catalog(tmp_path):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest()
    document["actions"]["send_fax"]["headers"] = {
        "Content-Length": "{{settings.content_length}}",
        "X-Provider": "{{settings.provider_name}}",
    }
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    catalog = catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert catalog.get("installed").manifest.as_dict() == document


@pytest.mark.parametrize("action", ["send_fax", "get_status", "cancel_fax"])
def test_any_recognized_action_can_be_catalogued(tmp_path, action):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest(action=action)
    document.update({"auth": {"scheme": "bearer", "extension": {"safe": True}},
                     "allowed_domains": ["synthetic.invalid"], "timeout_ms": 1200})
    document["actions"][action].update({
        "method": "get", "url": "https://synthetic.invalid/fax/{job_id}?to={{to}}",
        "headers": {"Accept": "application/json"},
        "path_params": [{"name": "job_id", "source": "job_id"}],
        "body": {"kind": "json", "template": '{"to":"{{to}}"}'},
        "response": {"job_id": "data.id", "status": "state", "error": "error",
                     "status_map": {"pending": "queued"}, "faxId": "unknown.field"},
    })
    document["actions"]["future_action"] = {"future": [1, {"preserved": True}]}
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    catalog = catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert catalog.get("installed").manifest.as_dict() == document


def test_status_action_rejects_body_kind_unsupported_by_existing_runtime(tmp_path):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest(action="get_status")
    document["actions"]["get_status"]["body"] = {"kind": "multipart", "template": ""}
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")


@pytest.mark.parametrize("layout", [
    "escaped_directory", "escaped_manifest", "directory_backlink",
    "dangling_root", "dangling_directory", "dangling_manifest", "manifest_directory",
    "manifest_fifo", "root_file",
])
def test_present_or_escaped_provider_artifacts_fail_explicitly(tmp_path, layout):
    base = write_json(tmp_path / "traits.json", {"builtin": {}})
    providers = tmp_path / "providers"
    outside = tmp_path / "outside"
    providers.mkdir()
    outside.mkdir()
    if layout == "escaped_directory":
        write_json(outside / "manifest.json", manifest())
        (providers / "installed").symlink_to(outside, target_is_directory=True)
    elif layout == "directory_backlink":
        inside = write_json(providers / "captured.json", manifest())
        (outside / "manifest.json").symlink_to(inside)
        (providers / "installed").symlink_to(outside, target_is_directory=True)
    elif layout == "escaped_manifest":
        destination = write_json(outside / "manifest.json", manifest())
        (providers / "installed").mkdir()
        (providers / "installed" / "manifest.json").symlink_to(destination)
    elif layout == "dangling_root":
        providers.rmdir()
        providers.symlink_to(outside / "missing", target_is_directory=True)
    elif layout == "dangling_directory":
        (providers / "installed").symlink_to(outside / "missing", target_is_directory=True)
    elif layout == "root_file":
        providers.rmdir()
        providers.write_text("private-source-marker")
    else:
        (providers / "installed").mkdir()
        destination = providers / "installed" / "manifest.json"
        if layout == "dangling_manifest":
            destination.symlink_to(providers / "missing.json")
        elif layout == "manifest_directory":
            destination.mkdir()
        elif layout == "manifest_fifo":
            os.mkfifo(destination)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, providers)
    assert "private" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)


def test_operator_root_and_contained_directory_and_manifest_symlinks_are_supported(tmp_path):
    base = write_json(tmp_path / "traits.json", {})
    real_root = tmp_path / "resources"
    manifest_path = write_json(real_root / "storage" / "logical.json", manifest("logical"))
    (real_root / "logical").mkdir()
    (real_root / "logical" / "manifest.json").symlink_to(manifest_path)
    storage = real_root / "storage" / "alias-store"
    write_json(storage / "manifest.json", manifest("alias"))
    (real_root / "alias").symlink_to(storage, target_is_directory=True)
    operator_root = tmp_path / "operator-root"
    operator_root.symlink_to(real_root, target_is_directory=True)

    catalog = catalog_module().ProviderCatalog.load(base, operator_root)

    assert catalog.provider_ids == frozenset({"logical", "alias"})
    assert catalog.get("logical").manifest.as_dict() == manifest("logical")
    assert catalog.get("alias").manifest.as_dict() == manifest("alias")


@pytest.mark.parametrize("artifact", ["traits", "manifest"])
@pytest.mark.parametrize("payload", [
    b"\xffprivate-source-marker", b"{private-source-marker", b"[]", b"null",
    b'{"private-source-marker":NaN}',
    b'{"private-source-marker":Infinity}',
    b'{"id":"installed","id":"installed","actions":{"send_fax":{"url":"https://synthetic.invalid"}}}',
    b'{"provider":{"traits":{"future":"before","future":"after"}}}',
    b'{"padding":"' + b"a" * (1024 * 1024) + b'"}',
    b'{"nested":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}",
], ids=["invalid_utf8", "invalid_json", "array", "null", "nan", "infinity",
        "duplicate_id", "duplicate_trait", "oversized", "excessive_nesting"])
def test_invalid_bounded_json_is_not_silently_ignored(tmp_path, artifact, payload):
    base = write_json(tmp_path / "private-path-marker.json", {"builtin": {}})
    installed = write_json(tmp_path / "providers" / "installed" / "manifest.json", manifest())
    (base if artifact == "traits" else installed).write_bytes(payload)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert "private" not in str(failure.value)
    assert str(tmp_path) not in repr(failure.value)
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("shape", ["missing", "directory", "fifo", "final_symlink"])
def test_required_traits_file_is_a_present_regular_artifact(tmp_path, shape):
    path = tmp_path / "traits.json"
    if shape == "directory":
        path.mkdir()
    elif shape == "fifo":
        os.mkfifo(path)
    elif shape == "final_symlink":
        destination = write_json(tmp_path / "actual.json", {"builtin": {}})
        path.symlink_to(destination)
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog_module().ProviderCatalog.load(path, tmp_path / "providers")


@pytest.mark.parametrize("id_value", [None, "wrong-private-marker", False])
def test_manifest_identity_must_match_logical_directory(tmp_path, id_value):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest()
    if id_value is None:
        document.pop("id")
    else:
        document["id"] = id_value
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")


@pytest.mark.parametrize("traits", [{"supports_inbound": "false"}, {"requires_tiff": 0},
                                    {"future": []}, {"inbound_verification": "unknown"}])
def test_manifest_trait_overrides_are_strict(tmp_path, traits):
    base = write_json(tmp_path / "traits.json", {"installed": {"traits": {"supports_inbound": True}}})
    document = manifest()
    document["traits"] = traits
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")


def test_empty_base_has_no_implicit_builtin_fallback(tmp_path):
    base = write_json(tmp_path / "traits.json", {"_schema": "metadata"})
    catalog = catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert catalog.provider_ids == frozenset()
    with pytest.raises(catalog_module().ProviderCatalogError):
        catalog.get("phaxio")


def test_repository_base_keeps_known_builtin_definitions(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    catalog = catalog_module().ProviderCatalog.load(
        repository / "config" / "provider_traits.json", tmp_path / "providers")
    assert catalog.provider_ids == frozenset({"phaxio", "sinch", "signalwire", "documo", "sip", "freeswitch"})
    assert catalog.get("sip").traits.as_dict()["requires_tiff"] is True
    assert catalog.get("sip").kind == "self_hosted"
    assert catalog.get("phaxio").traits.as_dict()["inbound_verification"] == "hmac"
    assert catalog.get("signalwire").traits.as_dict()["supports_inbound"] is False


@pytest.mark.parametrize("control", ["\0", "\n", "\r", "\t", "\x7f"])
def test_action_urls_reject_control_characters(tmp_path, control):
    base = write_json(tmp_path / "traits.json", {})
    document = manifest()
    document["actions"]["send_fax"]["url"] += control + "private-marker"
    write_json(tmp_path / "providers" / "installed" / "manifest.json", document)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("operation", ["open", "read", "fstat"])
def test_real_file_read_failures_are_safe_and_close_open_descriptors(tmp_path, monkeypatch, operation):
    base = write_json(tmp_path / "traits.json", {"builtin": {}})
    installed = write_json(tmp_path / "providers" / "installed" / "manifest.json", manifest())
    descriptors = set()
    original_open, original_read, original_fstat = os.open, os.read, os.fstat

    def tracked_open(path, *args, **kwargs):
        if Path(path) == installed and operation == "open":
            raise PermissionError("private-source-marker")
        descriptor = original_open(path, *args, **kwargs)
        if Path(path) == installed:
            descriptors.add(descriptor)
        return descriptor

    def failed_read(descriptor, *args):
        if descriptor in descriptors and operation == "read":
            raise OSError("private-source-marker")
        return original_read(descriptor, *args)

    def failed_fstat(descriptor):
        if descriptor in descriptors and operation == "fstat":
            raise OSError("private-source-marker")
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "read", failed_read)
    monkeypatch.setattr(os, "fstat", failed_fstat)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert "private" not in str(failure.value)
    assert str(tmp_path) not in repr(failure.value)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            original_fstat(descriptor)


@pytest.mark.parametrize("operation", ["root_inspection", "root_listing", "manifest_inspection"])
def test_directory_and_present_manifest_os_failures_are_explicit(tmp_path, monkeypatch, operation):
    base = write_json(tmp_path / "traits.json", {"builtin": {}})
    root = tmp_path / "providers"
    installed = write_json(root / "installed" / "manifest.json", manifest())
    original_stat, original_listdir = os.stat, os.listdir

    def failed_stat(path, *args, **kwargs):
        target = root if operation == "root_inspection" else installed
        if operation != "root_listing" and Path(path) == target:
            raise PermissionError("private-source-marker")
        return original_stat(path, *args, **kwargs)

    def failed_listdir(path):
        if operation == "root_listing" and Path(path) == root:
            raise PermissionError("private-source-marker")
        return original_listdir(path)

    monkeypatch.setattr(os, "stat", failed_stat)
    monkeypatch.setattr(os, "listdir", failed_listdir)
    with pytest.raises(catalog_module().ProviderCatalogError) as failure:
        catalog_module().ProviderCatalog.load(base, root)
    assert "private" not in str(failure.value)


def test_loading_preserves_resource_contents_and_modes(tmp_path):
    base = write_json(tmp_path / "traits.json", {"builtin": {}})
    installed = write_json(tmp_path / "providers" / "installed" / "manifest.json", manifest())
    base.chmod(0o640)
    installed.chmod(0o600)
    before = [(path.read_bytes(), path.stat().st_mode) for path in (base, installed)]
    catalog_module().ProviderCatalog.load(base, tmp_path / "providers")
    assert [(path.read_bytes(), path.stat().st_mode) for path in (base, installed)] == before
