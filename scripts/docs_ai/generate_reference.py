#!/usr/bin/env python3
"""Generate public references from source without loading operator settings."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
COVERAGE = "source declarations; no live installation state or provider verification"
CALLBACK_SOURCES = {
    "api/app/provider_signatures.py": ("_pairs", "verify_phaxio_signature"),
    "api/app/callback_locator.py": ("callback_base_url", "callback_url_with_locators"),
    "api/app/outbound_callbacks.py": ("CapturedCallbacks.receive",),
}


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True).strip()


def configuration_schema():
    from api.app.config_values import ConfigurationValues
    schema = ConfigurationValues.model_json_schema(by_alias=True)
    for name, value in schema["properties"].items():
        if value.get("secret"):
            value.pop("default", None)
            value.pop("examples", None)
            value["writeOnly"] = True
        # Factory-backed paths are intentionally not evaluated on the build host.
        if name in {"FAXBOT_PROVIDERS_DIR", "FAXBOT_CONFIG_PATH"}:
            value.pop("default", None)
    return schema


def provider_reference():
    from api.app.provider_catalog import ProviderCatalog
    catalog = ProviderCatalog.load(ROOT / "config/provider_traits.json", ROOT / "config/providers")
    result = {}
    for identity in sorted(catalog.provider_ids):
        definition = catalog.get(identity)
        manifest = definition.manifest.as_dict() if definition.manifest else None
        result[identity] = {
            "kind": definition.kind, "traits": definition.traits.as_dict(),
            "manifest_digest": definition.manifest.digest if definition.manifest else None,
            "declared_actions": sorted(manifest.get("actions", {})) if manifest else [],
        }
    return result


def openapi_reference():
    """Import route declarations in a child with a fresh, credential-free environment."""
    with tempfile.TemporaryDirectory(prefix="faxbot-docs-openapi-") as temporary:
        target = Path(temporary) / "openapi.json"
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT),
            "FAXBOT_TEST_MODE": "true", "FAX_DISABLED": "true",
            "FAX_DATA_DIR": str(Path(temporary) / "data"),
            "DATABASE_URL": "sqlite:///" + str(Path(temporary) / "docs.db"),
            "ENABLE_PERSISTED_SETTINGS": "false", "REQUIRE_API_KEY": "false",
            "ENFORCE_PUBLIC_HTTPS": "false", "FEATURE_V3_PLUGINS": "false",
        }
        code = "from api.app.main import app; import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(app.openapi()))"
        subprocess.run([sys.executable, "-c", code, str(target)], cwd=ROOT, env=environment, check=True)
        return json.loads(target.read_text())


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;").replace(">", "&gt;")


def _header(title, provenance):
    dirty = " The build includes uncommitted local changes." if provenance["source_tree_dirty"] else ""
    return [f"# {title}", "", f"Source commit: [`{provenance['source_sha']}`](https://github.com/DMontgomery40/Faxbot/commit/{provenance['source_sha']}).{dirty}", "",
        "Generated from this revision's source declarations. These defaults and traits do not report a running installation's active settings, prove provider delivery, or establish that newly added components are integrated.", ""]


def source_excerpt(path, symbol):
    """Read an exact declaration without importing its module or dependencies."""
    source = (ROOT / path).read_text(encoding="utf-8")
    node = ast.parse(source)
    for name in symbol.split("."):
        declarations = [
            entry for entry in node.body
            if isinstance(entry, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and entry.name == name
        ]
        if len(declarations) != 1:
            raise ValueError(f"Missing source declaration: {path}:{symbol}")
        node = declarations[0]
    start = min([node.lineno] + [entry.lineno for entry in node.decorator_list])
    return textwrap.dedent("\n".join(source.splitlines()[start - 1:node.end_lineno]))


def callback_reference(provenance):
    lines = _header("Outbound callback source reference", provenance)
    lines += ["These excerpts are read directly from source with Python's AST; callback runtime modules are not imported. The build provenance hashes each listed module. The verifier, URL construction and captured-attempt acceptance code below are regenerated on every source build.", "",
        "[Maintained Phaxio setup and verification guidance](../setup/webhooks.md#outbound-status-phaxio). This reference covers outbound observations; it does not establish inbound verification readiness.", ""]
    for path, symbols in CALLBACK_SOURCES.items():
        for symbol in symbols:
            lines += [f"## {symbol}", "", f"Source: `{path}`.", "", "```python", source_excerpt(path, symbol), "```", ""]
    return "\n".join(lines) + "\n"


def generate(output_directory, *, source_sha=None, source_ref=None, require_clean=False):
    revision = git("rev-parse", "HEAD")
    if source_sha is not None and source_sha != revision:
        raise ValueError("Requested source SHA does not match the checked-out source SHA.")
    dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    if require_clean and dirty:
        raise ValueError("Published documentation requires a clean source tree.")
    schema = configuration_schema()
    providers = provider_reference()
    spec = openapi_reference()
    inputs = ["api/app/main.py", "api/app/config_values.py", "api/app/provider_catalog.py", "config/provider_traits.json"]
    inputs.extend(CALLBACK_SOURCES)
    inputs.extend(str(path.relative_to(ROOT)) for path in sorted((ROOT / "config/providers").glob("*/manifest.json")))
    provenance = {
        "source_sha": revision, "source_ref": source_ref or git("rev-parse", "--abbrev-ref", "HEAD"),
        "source_tree_dirty": dirty, "coverage": COVERAGE,
        "source_files": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in inputs},
    }
    spec["x-faxbot-source"] = provenance
    # Preserve FastAPI's declared servers. Do not invent a hosted fax API.
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    _json(output / "openapi.json", spec)
    _json(output / "configuration.schema.json", schema)
    _json(output / "providers.json", providers)
    _json(output / "provenance.json", provenance)
    from api.app.config_values import ConfigurationValues
    lines = _header("Configuration source reference", provenance)
    lines += ["Source: `api/app/config_values.py`. Secret defaults are omitted; no environment values, persisted operator files, database records, or installation keys are read.", "",
        "Directional overrides inherit `FAX_BACKEND` when empty. Omitted and null patches preserve values; secret masks are not stored credentials. The source activation model may require restart or maintenance for some edits; this table is not an activation result.", "",
        "[Public JSON schema](configuration.schema.json)", "", "| Environment key | Patch field | Type | Source default |", "| --- | --- | --- | --- |"]
    for name, field in ConfigurationValues.model_fields.items():
        alias = field.validation_alias
        key = alias.choices[0] if hasattr(alias, "choices") else alias
        property_schema = schema["properties"][key]
        secret = (field.json_schema_extra or {}).get("secret", False)
        default = "secret (omitted)" if secret else json.dumps(property_schema["default"]) if "default" in property_schema else "packaged resource path"
        patch = (field.json_schema_extra or {}).get("patch_name", name)
        lines.append(f"| `{key}` | `{patch}` | {_cell(property_schema.get('type', 'value'))} | {_cell(default)} |")
    (output / "configuration.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    lines = _header("Provider source reference", provenance)
    lines += ["Source: the bundled `config/provider_traits.json` and any bundled `config/providers/*/manifest.json`, validated by `ProviderCatalog`. A declaration here is not an account, active selection, installed operator plugin, or remote validation result.", "", "[Machine-readable provider declarations](providers.json)", ""]
    for identity, definition in providers.items():
        lines += [f"## {identity}", "", f"Declared kind: `{definition['kind']}`.", "", "| Trait | Declared value |", "| --- | --- |"]
        lines += [f"| `{key}` | `{json.dumps(value)}` |" for key, value in sorted(definition["traits"].items())]
        lines += ["", "Bundled manifest: " + (f"`{definition['manifest_digest']}`" if definition["manifest_digest"] else "none; built-in traits only") + ".", ""]
    (output / "providers.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "outbound-callbacks.md").write_text(callback_reference(provenance), encoding="utf-8")
    lines = _header("Generated source reference", provenance)
    lines += ["- [API route and model reference (Redocly)](api.html)", "- [OpenAPI JSON](openapi.json)", "- [Typed configuration reference](configuration.md)", "- [Provider declarations](providers.md)", "- [Outbound callback source reference](outbound-callbacks.md)", "- [Build provenance](provenance.json)", "",
        "Instructional pages remain maintained prose. These generated references supplement those pages and make their source revision inspectable. The legacy `faxbot.net/api` site has a separate compatibility deployment and is not refreshed by this build."]
    (output / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return provenance


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/generated")
    parser.add_argument("--source-sha")
    parser.add_argument("--source-ref")
    parser.add_argument("--require-clean", action="store_true")
    options = parser.parse_args()
    result = generate(options.output, source_sha=options.source_sha, source_ref=options.source_ref, require_clean=options.require_clean)
    print("Generated source reference for " + result["source_sha"])
