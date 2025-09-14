"""Atomic JSON config store for v3 plugin system (feature-gated).

This provides safe read/write helpers for the resolved Faxbot config file.
It is not wired into runtime settings yet; endpoints are gated by
FEATURE_V3_PLUGINS.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ConfigResult:
    path: str
    ok: bool
    error: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


DEFAULT_CONFIG: Dict[str, Any] = {
    "version": 1,
    "providers": {
        "outbound": {"plugin": "phaxio", "enabled": True, "settings": {}},
        "inbound": {"plugin": None, "enabled": False, "settings": {}},
        "auth": {"plugin": None, "enabled": False, "settings": {}},
        "storage": {"plugin": "local", "enabled": True, "settings": {}},
    },
}


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def read_config(path: str) -> ConfigResult:
    try:
        if not os.path.exists(path):
            return ConfigResult(path=path, ok=True, data=DEFAULT_CONFIG.copy())
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Minimal validation
        if not isinstance(data, dict) or "providers" not in data:
            return ConfigResult(path=path, ok=False, error="Invalid config structure")
        return ConfigResult(path=path, ok=True, data=data)
    except Exception as e:
        return ConfigResult(path=path, ok=False, error=str(e))


def write_config(path: str, data: Dict[str, Any]) -> ConfigResult:
    try:
        # Minimal validation before write
        if not isinstance(data, dict) or "providers" not in data:
            return ConfigResult(path=path, ok=False, error="Invalid config structure")
        _ensure_dir(path)
        # Atomic write via temp file + rename
        dir_name = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(prefix="faxbot_cfg_", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                json.dump(data, tmp, indent=2, sort_keys=True)
                tmp.flush()
                os.fsync(tmp.fileno())
            # Backup prior version
            if os.path.exists(path):
                backup = f"{path}.bak"
                try:
                    if os.path.exists(backup):
                        os.remove(backup)
                except Exception:
                    pass
                try:
                    os.replace(path, backup)
                except Exception:
                    pass
            os.replace(tmp_path, path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
        return ConfigResult(path=path, ok=True, data=data)
    except Exception as e:
        return ConfigResult(path=path, ok=False, error=str(e))

