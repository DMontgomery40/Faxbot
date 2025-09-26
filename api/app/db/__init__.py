"""
Compatibility shim for synchronous DB symbols.

Historically the project exposed sync SQLAlchemy helpers and models from
`api.app.db` (the module file `api/app/db.py`). During the async transition,
this package (`api/app/db/`) was introduced which shadowed the legacy module
path and broke imports like `from app.db import init_db`.

This shim re-exports the legacy sync symbols by importing the original
`api/app/db.py` file under a private module name and exposing the expected
attributes. Async helpers live in `api.app.db.async`.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Iterable

_ROOT = Path(__file__).resolve().parents[1]  # api/app
_LEGACY_DB_PATH = _ROOT / "db.py"


def _load_legacy() -> ModuleType:
    spec = spec_from_file_location("app._db_legacy", str(_LEGACY_DB_PATH))
    if spec is None or spec.loader is None:  # pragma: no cover - safety
        raise ImportError("Failed to locate legacy db.py for compatibility shim")
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[assignment]
    return mod


_legacy = _load_legacy()


def _export(names: Iterable[str]) -> None:
    g = globals()
    exported = []
    for name in names:
        if hasattr(_legacy, name):
            g[name] = getattr(_legacy, name)
            exported.append(name)
    g["__all__"] = tuple(exported)


# Re-export commonly used sync symbols
_export(
    (
        "engine",
        "SessionLocal",
        "Base",
        "FaxJob",
        "APIKey",
        "InboundFax",
        "Mailbox",
        "InboundRule",
        "InboundEvent",
        "init_db",
    )
)


# Lazy fallback for any future attributes looked up from legacy module
def __getattr__(name: str):  # pragma: no cover - defensive
    try:
        return getattr(_legacy, name)
    except AttributeError:
        raise
