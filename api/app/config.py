"""Stable compatibility facade over immutable operation configuration.

An ASGI request or owned background operation binds one complete revision.
Bootstrap loading never writes process environment; managed reloads read the
canonical installation store and cannot replace an already captured operation.
"""
import os
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional
from .config_values import ConfigurationValues
from .config_paths import (
    InvalidProviderPath, faxbot_config_path, provider_manifest_path,
    provider_traits_path, providers_dir,
)

_BOUND_VALUES = ContextVar('faxbot_configuration_values', default=None)
_BOUND_PROFILES = ContextVar('faxbot_configuration_profiles', default=None)
_source = None
_bootstrap_values = None


def bootstrap_locations(environment):
    return ConfigurationValues.from_environment({key: environment[key] for key in
        ('DATABASE_URL', 'FAX_DATA_DIR', 'FAXBOT_PROVIDERS_DIR',
         'FAXBOT_CONFIG_PATH') if key in environment})


def Settings():
    """Explicit deployment-value parsing for compatibility with embedding clients."""
    return ConfigurationValues.from_environment(os.environ)


def configuration_values():
    captured = _BOUND_VALUES.get()
    if captured is not None:
        return captured
    if _source is not None:
        return _source.read().active.values
    global _bootstrap_values
    if _bootstrap_values is None:
        # Module import needs only datastore/resource locations. Ordinary values
        # are validated at first import, after checking for canonical state.
        _bootstrap_values = bootstrap_locations(os.environ)
    return _bootstrap_values


def managed_configuration_values():
    """Resource paths follow the operation/canonical values after activation."""
    if _BOUND_VALUES.get() is not None or _source is not None:
        return configuration_values()
    return None


class _SettingsFacade:
    __slots__ = ()

    def __getattr__(self, name):
        return getattr(configuration_values(), name)


settings = _SettingsFacade()


@contextmanager
def use_configuration(values, profiles=None):
    """Pin values and selected profiles for the full operation, including awaits."""
    if not isinstance(values, ConfigurationValues):
        raise TypeError('Operation configuration must be validated immutable values.')
    token = _BOUND_VALUES.set(values)
    profile_token = _BOUND_PROFILES.set(profiles)
    try:
        yield
    finally:
        _BOUND_PROFILES.reset(profile_token)
        _BOUND_VALUES.reset(token)


def install_configuration_source(store):
    global _source
    if _source is not None and _source is not store:
        raise RuntimeError('Configuration runtime is already installed.')
    _source = store


def release_configuration_source(store):
    global _source
    if _source is store:
        _source = None


def reload_settings():
    """Refresh canonical state without promoting pending settings or changing a frame."""
    if _source is not None:
        return _source.read()
    if _BOUND_VALUES.get() is None:
        global _bootstrap_values
        _bootstrap_values = Settings()
        _refresh_traits_cache()
    return None

# ===== Provider traits registry (declarative) =====
_TRAITS_CACHE: Dict[str, Any] = {"registry": {}, "loaded_mtime": 0.0, "schema_issues": {}}

# Canonical trait keys — schema contract
CANONICAL_TRAIT_KEYS: set[str] = {
    "requires_ghostscript",
    "requires_ami",
    "requires_tiff",
    "supports_inbound",
    "inbound_verification",
    "needs_storage",
    "outbound_status_only",
}


def _traits_file_path() -> str:
    return str(provider_traits_path())


def _providers_dir() -> str:
    return str(providers_dir())


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _load_base_traits() -> Dict[str, Dict[str, Any]]:
    base = _read_json(_traits_file_path())
    reg: Dict[str, Dict[str, Any]] = {}
    if not base:
        return reg
    # Accept either an object keyed by id, or a list of providers
    if isinstance(base, dict):
        for pid, obj in base.items():
            if isinstance(obj, dict):
                obj.setdefault("id", pid)
                reg[pid] = obj
    elif isinstance(base, list):
        for obj in base:
            if isinstance(obj, dict) and obj.get("id"):
                reg[str(obj["id"])]= obj
    return reg


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a or {})
    for k, v in (b or {}).items():
        if k == "traits" and isinstance(v, dict):
            tv = dict(out.get("traits") or {})
            # Filter unknown trait keys and record issues
            unknown = [kk for kk in v.keys() if kk not in CANONICAL_TRAIT_KEYS]
            if unknown:
                issues = _TRAITS_CACHE.get("schema_issues") or {}
                pid = (a.get("id") or b.get("id") or "unknown")
                issues.setdefault("unknown_trait_keys", {})[str(pid)] = sorted(set(unknown))
                _TRAITS_CACHE["schema_issues"] = issues
            # Merge only canonical keys
            for kk, vv in v.items():
                if kk in CANONICAL_TRAIT_KEYS:
                    tv[kk] = vv
            out["traits"] = tv
        else:
            out[k] = v
    return out


def _scan_manifest_traits() -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    pdir = _providers_dir()
    if not os.path.isdir(pdir):
        return results
    for pid in os.listdir(pdir):
        try:
            mpath = provider_manifest_path(pid)
        except InvalidProviderPath:
            # Unsafe entries are not installed providers and must never be read.
            continue
        if not os.path.exists(mpath):
            continue
        data = _read_json(mpath)
        if not isinstance(data, dict):
            continue
        # Traits are optional in manifests; use if present
        traits = data.get("traits") if isinstance(data.get("traits"), dict) else None
        kind = data.get("kind") if isinstance(data.get("kind"), str) else None
        obj: Dict[str, Any] = {"id": pid}
        if kind:
            obj["kind"] = kind
        if traits:
            obj["traits"] = traits
        if obj:
            results[pid] = obj
    return results


def _build_provider_registry() -> Dict[str, Dict[str, Any]]:
    reg = _load_base_traits()
    # Merge in manifests (override base)
    man = _scan_manifest_traits()
    for pid, obj in man.items():
        base = reg.get(pid, {"id": pid, "traits": {}})
        reg[pid] = _merge(base, obj)
    return reg


def _refresh_traits_cache() -> None:
    try:
        mtime = 0.0
        tf = _traits_file_path()
        if os.path.exists(tf):
            mtime = os.stat(tf).st_mtime
        if _TRAITS_CACHE.get("loaded_mtime") != mtime:
            _TRAITS_CACHE["schema_issues"] = {}
            _TRAITS_CACHE["registry"] = _build_provider_registry()
            _TRAITS_CACHE["loaded_mtime"] = mtime
        else:
            # Always rescan manifests since they can change without touching the traits file
            _TRAITS_CACHE["schema_issues"] = {}
            _TRAITS_CACHE["registry"] = _build_provider_registry()
    except Exception:
        _TRAITS_CACHE["registry"] = {}
        _TRAITS_CACHE["loaded_mtime"] = 0.0


def get_provider_registry() -> Dict[str, Dict[str, Any]]:
    if not _TRAITS_CACHE.get("registry"):
        _refresh_traits_cache()
    return _TRAITS_CACHE.get("registry") or {}


def get_provider_traits(provider_id: Optional[str]) -> Dict[str, Any]:
    if not provider_id:
        return {}
    return get_provider_registry().get(provider_id, {})


def valid_backends() -> set[str]:
    """Return provider ids known to the system.
    Falls back to a safe built-in set when the traits registry is unavailable.
    """
    reg = get_provider_registry() or {}
    keys = set(reg.keys())
    # Drop schema metadata if present
    keys.discard("_schema")
    if not keys:
        # Fallback to known providers to avoid treating everything as legacy
        return {"phaxio", "sinch", "sip", "signalwire", "documo", "humblefax", "efax", "freeswitch"}
    return keys


# Valid backend identifiers supported by the core (dynamic)
VALID_BACKENDS = valid_backends()


def active_outbound() -> str:
    """Return the effective outbound backend, normalizing and validating.
    Falls back to legacy fax_backend when dual env is not set or invalid.
    """
    return settings.effective_outbound


def active_inbound() -> str:
    """Return the effective inbound backend, normalizing and validating.
    Falls back to legacy fax_backend when dual env is not set or invalid.
    """
    return settings.effective_inbound


def providerHasTrait(direction: str, trait_name: str) -> bool:
    try:
        pid = active_outbound() if direction == "outbound" else active_inbound()
        if direction == "any":
            return providerHasTrait("outbound", trait_name) or providerHasTrait("inbound", trait_name)
        profiles = _BOUND_PROFILES.get()
        if profiles is not None:
            profile = profiles.get(direction)
            tr = profile.configuration.traits if profile is not None else {}
        else:
            tr = (get_provider_traits(pid).get("traits") or {})
        val = tr.get(trait_name)
        return val is True
    except Exception:
        return False


def providerTraitValue(direction: str, trait_name: str):
    try:
        pid = active_outbound() if direction == "outbound" else active_inbound()
        if direction == "any":
            # Prefer outbound's value, else inbound
            v = providerTraitValue("outbound", trait_name)
            return v if v is not None else providerTraitValue("inbound", trait_name)
        profiles = _BOUND_PROFILES.get()
        if profiles is not None:
            profile = profiles.get(direction)
            tr = profile.configuration.traits if profile is not None else {}
        else:
            tr = (get_provider_traits(pid).get("traits") or {})
        return tr.get(trait_name)
    except Exception:
        return None
