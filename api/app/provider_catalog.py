"""Immutable provider resources read from explicit operator paths.

Each load captures a fresh snapshot. Resolution checks implement the operator
root containment policy, without promising safety against concurrent hostile
filesystem changes. Cataloguing a manifest does not activate its capabilities.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import stat
from types import MappingProxyType
from urllib.parse import urlparse

from .config_file import ConfigurationFileError, read_configuration_text
from .config_profiles import ConfigurationDocument, ConfigurationRecordError


_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
_BOOLEAN_TRAITS = frozenset(
    {
        "requires_ghostscript", "requires_ami", "requires_tiff",
        "supports_inbound", "needs_storage", "outbound_status_only",
    }
)
_VERIFICATION = frozenset({"hmac", "basic", "internal_secret", "none"})
_ACTIONS = frozenset({"send_fax", "get_status", "cancel_fax"})
_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)
_HTTP_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", re.ASCII)
_AUTH_SCHEMES = frozenset({"none", "basic", "bearer", "api_key_header", "api_key_query"})


class ProviderCatalogError(ValueError):
    """A safe provider resource or selector failure."""


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    traits: ConfigurationDocument = field(repr=False)
    manifest: ConfigurationDocument | None = field(repr=False)
    kind: str


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError
    return document


def _read_json(path: Path) -> dict:
    try:
        document = json.loads(
            read_configuration_text(path), object_pairs_hook=_json_object
        )
        return ConfigurationDocument(document).as_dict()
    except (
        ConfigurationFileError, ConfigurationRecordError, ValueError,
        TypeError, RecursionError,
    ):
        raise ProviderCatalogError("Invalid provider resource.") from None


def _identity(provider_id: str) -> None:
    if (
        not isinstance(provider_id, str) or len(provider_id) > 255
        or _PROVIDER_ID.fullmatch(provider_id) is None
    ):
        raise ValueError


def _traits(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError
    for key, item in value.items():
        if item is not None and type(item) not in (str, bool, int, float):
            raise ValueError
        if key in _BOOLEAN_TRAITS and type(item) is not bool:
            raise ValueError
        if key == "inbound_verification" and item not in _VERIFICATION:
            raise ValueError
    return value


def _kind(entry: dict, default: str = "cloud") -> str:
    kind = entry.get("kind", default)
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError
    return kind


def _string(value: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError


def _mapping(value: dict) -> None:
    if not isinstance(value, dict):
        raise ValueError


def _http_action(action_id: str, action: dict) -> None:
    """Check shapes consumed by HttpManifest and HttpProviderRuntime."""
    _mapping(action)
    url = action.get("url")
    _string(url)
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError
    # Accessing port also validates malformed/out-of-range port declarations.
    parsed.port
    method = action.get("method", "POST")
    _string(method)
    if method.upper() not in _METHODS:
        raise ValueError
    headers = action.get("headers", {})
    _mapping(headers)
    for name, value in headers.items():
        if _HTTP_TOKEN.fullmatch(name) is None:
            raise ValueError
        _string(value, empty=True)
        if "\r" in value or "\n" in value:
            raise ValueError
    body = action.get("body", {})
    _mapping(body)
    kind = body.get("kind", "none")
    if not isinstance(kind, str) or kind not in {"none", "json", "form", "multipart"}:
        raise ValueError
    if action_id == "get_status" and kind == "multipart":
        raise ValueError
    _string(body.get("template", ""), empty=True)
    parameters = action.get("path_params", [])
    if not isinstance(parameters, list):
        raise ValueError
    for parameter in parameters:
        _mapping(parameter)
        _string(parameter.get("name"))
        if "source" in parameter:
            _string(parameter["source"])
    response = action.get("response", {})
    _mapping(response)
    for key in ("job_id", "status", "error"):
        if key in response:
            _string(response[key])
    if "status_map" in response:
        _mapping(response["status_map"])
        for value in response["status_map"].values():
            _string(value)


def _http_manifest(document: dict) -> None:
    actions = document.get("actions")
    _mapping(actions)
    if not _ACTIONS.intersection(actions):
        raise ValueError
    for action_id in _ACTIONS.intersection(actions):
        _http_action(action_id, actions[action_id])
    if "name" in document:
        _string(document["name"])
    auth = document.get("auth", {})
    _mapping(auth)
    scheme = auth.get("scheme", "none")
    _string(scheme)
    if scheme.lower() not in _AUTH_SCHEMES:
        raise ValueError
    if "header_name" in auth:
        _string(auth["header_name"])
        if _HTTP_TOKEN.fullmatch(auth["header_name"]) is None:
            raise ValueError
    if "query_name" in auth:
        _string(auth["query_name"])
    domains = document.get("allowed_domains", [])
    if not isinstance(domains, list):
        raise ValueError
    for domain in domains:
        _string(domain)
    timeout = document.get("timeout_ms", 15000)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError


def _manifest_paths(providers_directory: Path):
    root = Path(providers_directory)
    try:
        root.lstat()
    except FileNotFoundError:
        return
    root = root.resolve(strict=True)
    if not stat.S_ISDIR(root.stat().st_mode):
        raise ValueError
    for directory in sorted(root.iterdir()):
        mode = directory.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
            continue
        resolved_directory = directory.resolve(strict=True)
        resolved_directory.relative_to(root)
        if not stat.S_ISDIR(resolved_directory.stat().st_mode):
            continue
        path = resolved_directory / "manifest.json"
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        resolved_manifest = path.resolve(strict=True)
        resolved_manifest.relative_to(root)
        _identity(directory.name)
        yield directory.name, resolved_manifest


@dataclass(frozen=True, init=False)
class ProviderCatalog:
    """A fresh immutable registry of built-ins and installed HTTP manifests."""

    _definitions: Mapping[str, ProviderDefinition] = field(repr=False)

    def __init__(self, definitions: Mapping[str, ProviderDefinition]) -> None:
        object.__setattr__(self, "_definitions", MappingProxyType(dict(definitions)))

    @property
    def provider_ids(self) -> frozenset[str]:
        return frozenset(self._definitions)

    @classmethod
    def load(cls, traits_path: Path, providers_directory: Path) -> "ProviderCatalog":
        """Capture resources without global configuration or environment access."""
        definitions = {}
        try:
            for provider_id, entry in _read_json(traits_path).items():
                if provider_id == "_schema":
                    continue
                _identity(provider_id)
                if (
                    not isinstance(entry, dict)
                    or entry.get("id", provider_id) != provider_id
                ):
                    raise ValueError
                definitions[provider_id] = ProviderDefinition(
                    provider_id,
                    ConfigurationDocument(_traits(entry.get("traits", {}))),
                    None,
                    _kind(entry),
                )
            for provider_id, path in _manifest_paths(providers_directory):
                manifest = _read_json(path)
                if manifest.get("id") != provider_id:
                    raise ValueError
                _http_manifest(manifest)
                previous = definitions.get(provider_id)
                traits = (
                    previous.traits.as_dict() if previous is not None else {
                        "requires_tiff": False, "requires_ami": False,
                        "supports_inbound": False,
                    }
                )
                traits.update(_traits(manifest.get("traits", {})))
                definitions[provider_id] = ProviderDefinition(
                    provider_id,
                    ConfigurationDocument(traits),
                    ConfigurationDocument(manifest),
                    _kind(manifest, previous.kind if previous is not None else "cloud"),
                )
        except (ValueError, TypeError, OSError, RuntimeError):
            raise ProviderCatalogError("Invalid provider resource.") from None
        return cls(definitions)

    def get(self, provider_id: str) -> ProviderDefinition:
        """Select exactly one identity; unknown selectors never use a fallback."""
        try:
            return self._definitions[provider_id]
        except (KeyError, TypeError):
            raise ProviderCatalogError("Unknown provider identity.") from None
