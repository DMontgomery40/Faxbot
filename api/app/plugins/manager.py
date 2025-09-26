from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Root of the plugins tree (…/api/app/plugins)
_PLUGINS_ROOT = Path(__file__).resolve().parent
# Supported plugin types for discovery
# Phase 1: transport, storage; Phase 2 adds identity discovery (no behavior change if none present)
_SUPPORTED_TYPES = ("transport", "storage", "identity")

# Optional: jsonschema validation (if library is available)
try:
    import jsonschema  # type: ignore
    _HAS_JSONSCHEMA = True
except Exception:
    _HAS_JSONSCHEMA = False


@dataclass
class PluginRecord:
    plugin_type: str           # e.g., "transport"
    plugin_id: str             # e.g., "phaxio"
    instance: Any              # instantiated shim/plugin object
    manifest: Dict[str, Any]   # raw manifest json


class PluginManager:
    """
    Manifest-first plugin manager with lazy shim import.

    - Discovers manifests under: api/app/plugins/<type>/<id>/manifest.json
    - Validates manifests against manifest.schema.json (if present / jsonschema available)
    - Imports shim module at: app.plugins.<type>.shims.<id>.plugin (with fallback to api.app.plugins...)
      * expects either:
        - callable get_plugin_class() -> type, or
        - a class named 'Plugin' exported in module
    - Registers instances addressable via:
        get_by_type_and_id("transport","phaxio")
        get_active_by_type("transport")  # see selection rules below
    """

    def __init__(self) -> None:
        # registry: { "transport": { "phaxio": PluginRecord, ... }, ... }
        self._registry: Dict[str, Dict[str, PluginRecord]] = {}
        # optionally preselect active plugin per type (can be set by config later)
        self._active_by_type: Dict[str, str] = {}

    # -------------------------
    # Public accessors
    # -------------------------
    def get_by_type_and_id(self, plugin_type: str, plugin_id: str) -> Any:
        bucket = self._registry.get(plugin_type, {})
        if plugin_id not in bucket:
            raise KeyError(f"Plugin not loaded: type={plugin_type!r}, id={plugin_id!r}")
        return bucket[plugin_id].instance

    def set_active_by_type(self, plugin_type: str, plugin_id: str) -> None:
        # sanity check it exists
        self.get_by_type_and_id(plugin_type, plugin_id)
        self._active_by_type[plugin_type] = plugin_id

    def get_active_by_type(self, plugin_type: str) -> Any:
        """
        Selection rules (in order):
          1) if set_active_by_type() was called -> return that
          2) if exactly one plugin loaded for that type -> return it
          3) if env FAXBOT_ACTIVE_<TYPE> set -> use that id (e.g., FAXBOT_ACTIVE_TRANSPORT=phaxio)
          4) else raise KeyError
        """
        # explicit selection
        if plugin_type in self._active_by_type:
            return self.get_by_type_and_id(plugin_type, self._active_by_type[plugin_type])

        bucket = self._registry.get(plugin_type, {})
        if len(bucket) == 1:
            return next(iter(bucket.values())).instance

        env_key = f"FAXBOT_ACTIVE_{plugin_type.upper()}"
        env_id = os.getenv(env_key)
        if env_id:
            return self.get_by_type_and_id(plugin_type, env_id)

        raise KeyError(
            f"No active plugin selected for type={plugin_type!r}; loaded={list(bucket.keys())}, "
            f"set env {env_key}=<id> or call set_active_by_type()."
        )

    @property
    def plugins(self) -> Dict[str, Dict[str, PluginRecord]]:
        # read-only view
        return self._registry

    # -------------------------
    # Discovery / load
    # -------------------------
    def load_all(self) -> None:
        """
        Discover manifests and register shim instances for supported types.
        """
        schema_path = _PLUGINS_ROOT / "manifest.schema.json"
        schema: Optional[Dict[str, Any]] = None
        if schema_path.exists():
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception as ex:
                logger.warning("manifest.schema.json could not be parsed: %s", ex)

        for type_dir in _SUPPORTED_TYPES:
            type_root = _PLUGINS_ROOT / type_dir
            if not type_root.exists():
                continue

            for id_dir in sorted([p for p in type_root.iterdir() if p.is_dir()]):
                manifest_file = id_dir / "manifest.json"
                if not manifest_file.exists():
                    # some subdirs may be "shims" or non-manifest folders; skip quietly
                    continue

                try:
                    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                except Exception as ex:
                    logger.error("Failed to read manifest: %s (%s)", manifest_file, ex)
                    continue

                # Validate manifest (jsonschema if available; else strict fallback)
                if schema and _HAS_JSONSCHEMA:
                    try:
                        jsonschema.validate(manifest, schema)  # type: ignore
                    except Exception as ex:
                        logger.error("Manifest schema validation failed for %s: %s", manifest_file, ex)
                        continue
                else:
                    if not _fallback_manifest_check(manifest):
                        logger.error("Manifest missing required fields: %s", manifest_file)
                        continue

                plugin_type, plugin_id = _read_ids_from_manifest(manifest)
                if plugin_type not in _SUPPORTED_TYPES:
                    logger.info("Skipping unsupported plugin type %r at %s", plugin_type, manifest_file)
                    continue

                # Import the shim module and resolve the plugin class
                try:
                    cls = _import_shim_class(plugin_type, plugin_id)
                except Exception as ex:
                    logger.error("Shim import failed for %s/%s: %s", plugin_type, plugin_id, ex)
                    continue

                # Validate type/scope and instantiate the plugin
                try:
                    if not _validate_plugin_class(plugin_type, cls, manifest):
                        logger.error("Plugin %s/%s rejected by validation", plugin_type, plugin_id)
                        continue
                    instance = cls()
                except Exception as ex:
                    logger.error("Failed to instantiate plugin %s/%s: %s", plugin_type, plugin_id, ex)
                    continue

                self.register(plugin_type, plugin_id, instance, manifest)

        logger.info("Plugin load complete. Loaded types: %s",
                    {t: list(self._registry.get(t, {}).keys()) for t in _SUPPORTED_TYPES})

    async def initialize_all(self) -> None:
        """
        Optionally call initialize(config) on each plugin if it exists.
        This function is async-safe: if initialize is a coroutine, await it;
        if it's sync, run it in a thread to avoid blocking the loop.
        """
        for ptype, bucket in self._registry.items():
            for pid, rec in bucket.items():
                init_fn = getattr(rec.instance, "initialize", None)
                if init_fn is None:
                    continue
                try:
                    if inspect.iscoroutinefunction(init_fn):
                        await init_fn(_config_for(rec))  # provide a dict if needed
                    else:
                        # offload sync init to thread so we don't block the loop
                        await asyncio.to_thread(init_fn, _config_for(rec))
                    logger.info("Initialized plugin %s/%s", ptype, pid)
                except Exception as ex:
                    logger.error("Initialization failed for %s/%s: %s", ptype, pid, ex)

    def register(self, plugin_type: str, plugin_id: str, instance: Any, manifest: Dict[str, Any]) -> None:
        bucket = self._registry.setdefault(plugin_type, {})
        bucket[plugin_id] = PluginRecord(plugin_type=plugin_type, plugin_id=plugin_id,
                                         instance=instance, manifest=manifest)


# -------------------------
# Helpers (module-internal)
# -------------------------
def _read_ids_from_manifest(manifest: Dict[str, Any]) -> Tuple[str, str]:
    plugin_type = str(manifest.get("type", "")).strip()
    plugin_id = str(manifest.get("id", "")).strip()
    if not plugin_type or not plugin_id:
        raise ValueError("Manifest missing 'type' or 'id'")
    return plugin_type, plugin_id


def _fallback_manifest_check(m: Dict[str, Any]) -> bool:
    """
    Strict fallback when jsonschema isn't available.
    Requires: id:str, name:str, version:str, type:str, traits:dict.
    """
    required = ("id", "name", "version", "type", "traits")
    for k in required:
        if k not in m:
            return False
    if not isinstance(m["traits"], dict):
        return False
    return True


def _import_shim_class(plugin_type: str, plugin_id: str):
    """
    Resolve the shim class in:
      app.plugins.<type>.shims.<id>.plugin  (preferred in runtime)
      or api.app.plugins.<type>.shims.<id>.plugin (repo-root fallback)

    Expected:
      - module exports `get_plugin_class()` -> Type
        OR
      - module exports a class named `Plugin`
    """
    module_path_candidates = [
        f"app.plugins.{plugin_type}.shims.{plugin_id}.plugin",
        f"api.app.plugins.{plugin_type}.shims.{plugin_id}.plugin",
    ]
    last_err: Optional[Exception] = None
    for module_path in module_path_candidates:
        try:
            module = importlib.import_module(module_path)
            # Preferred explicit factory
            factory = getattr(module, "get_plugin_class", None)
            if callable(factory):
                cls = factory()
                if inspect.isclass(cls):
                    return cls
            # Fallback: find a class named 'Plugin'
            if hasattr(module, "Plugin") and inspect.isclass(getattr(module, "Plugin")):
                return getattr(module, "Plugin")
            # Last resort: first class in module that looks like a plugin
            for name, obj in vars(module).items():
                if inspect.isclass(obj) and hasattr(obj, "__name__"):
                    if getattr(obj, "plugin_type", None) == plugin_type or hasattr(obj, "send_fax"):
                        return obj
        except Exception as ex:  # try next path
            last_err = ex
            continue
    raise ImportError(f"No plugin class found for {plugin_type}/{plugin_id}: {last_err}")


def _config_for(record: PluginRecord) -> Dict[str, Any]:
    """
    Provide a minimal config dict to initialize() if needed.
    In v4 this will be replaced by Hybrid/Hierarchical config lookups.
    For now we pass the manifest as context; plugins are expected to read
    real credentials from env or the future config provider.
    """
    return {"manifest": record.manifest}


def _validate_plugin_class(plugin_type: str, cls: type, manifest: Dict[str, Any]) -> bool:
    """Validate that the imported class conforms to expected base/type/scope.

    - transport → subclass of TransportPlugin (if available)
    - identity  → subclass of IdentityPlugin (if available)
    - scope → must be 'global' in Phase 1 (reject tenant/user to avoid mis-scoping)
    """
    try:
        # Type enforcement
        if plugin_type == "transport":
            try:
                from .base import TransportPlugin  # type: ignore
                if not issubclass(cls, TransportPlugin):
                    logger.warning("Class %s is not a TransportPlugin; continuing (compat)", getattr(cls, "__name__", str(cls)))
            except Exception:
                # Base not available; skip strictness
                pass
        elif plugin_type == "identity":
            try:
                from .identity.base import IdentityPlugin  # type: ignore
                if not issubclass(cls, IdentityPlugin):
                    logger.warning("Class %s is not an IdentityPlugin; continuing (compat)", getattr(cls, "__name__", str(cls)))
            except Exception:
                # Base not available; skip strictness
                pass

        # Scope enforcement (Phase 1: global only)
        scope = str(getattr(cls, "scope", manifest.get("scope", "global"))).strip().lower()
        if scope not in {"global", ""}:
            logger.error("Plugin scope %r not allowed in Phase 1; only 'global' accepted", scope)
            return False
        return True
    except Exception:
        return False


# Singleton accessor (optional convenience)
_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    global _manager
    if _manager is None:
        _manager = PluginManager()
    return _manager
