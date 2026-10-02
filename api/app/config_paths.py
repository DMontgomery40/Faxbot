"""Locations for bundled resources and operator-configured provider files.

Source checkouts keep config beside api; the image keeps config beside app.
Locate that directory from this module, independent of import spelling or cwd.
Explicit relative overrides retain their operator meaning: they are relative to
the process working directory when resolved. Returned locations are absolute.
"""

from pathlib import Path
import os
import re


_APP_DIR = Path(__file__).resolve().parent
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)


class InvalidProviderPath(ValueError):
    """A provider identifier or existing symlink would escape its resource root."""

    def __init__(self):
        super().__init__("Invalid provider id or path")


def bundled_config_dir() -> Path:
    for app_root in (_APP_DIR.parent, _APP_DIR.parent.parent):
        config_dir = app_root / "config"
        # A leftover providers-only directory from the old cwd-based installer
        # is not the bundle. Traits are a required resource in both layouts.
        if (config_dir / "provider_traits.json").is_file():
            return config_dir
    raise FileNotFoundError("Bundled configuration directory unavailable")


def _configured_path(variable: str, filename: str) -> Path:
    override = os.getenv(variable)
    if override is not None:
        return Path(os.path.abspath(override))
    return bundled_config_dir() / filename


def provider_traits_path() -> Path:
    return bundled_config_dir() / "provider_traits.json"


def providers_dir() -> Path:
    return _configured_path("FAXBOT_PROVIDERS_DIR", "providers")


def faxbot_config_path() -> Path:
    return _configured_path("FAXBOT_CONFIG_PATH", "faxbot.config.json")


def plugin_registry_path() -> Path:
    return _configured_path("PLUGIN_REGISTRY_PATH", "plugin_registry.json")


def plugin_examples_path() -> Path:
    # This resource is copied with the API module in the flattened image too.
    return _APP_DIR / "api_plugins_list.md"


def provider_manifest_path(provider_id: str) -> Path:
    """Resolve one safe component beneath the configured, possibly symlinked root.

    Check both the provider directory and manifest's resolved identity before
    reading or writing. This refuses preexisting escaped symlinks; it does not
    claim to protect against concurrent filesystem changes by another process.
    """
    if not isinstance(provider_id, str) or _PROVIDER_ID.fullmatch(provider_id) is None:
        raise InvalidProviderPath()
    try:
        root = providers_dir().resolve()
        directory = (root / provider_id).resolve()
        directory.relative_to(root)
        path = (directory / "manifest.json").resolve()
        path.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        raise InvalidProviderPath() from None
    return path
