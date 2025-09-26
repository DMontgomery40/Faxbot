from __future__ import annotations

from enum import Enum


class PluginType(str, Enum):
    transport = "transport"
    storage = "storage"
    identity = "identity"


class PluginScope(str, Enum):
    global_ = "global"
    tenant = "tenant"
    user = "user"

    @classmethod
    def normalize(cls, value: str | None) -> "PluginScope":
        v = (value or "global").strip().lower()
        if v == "global":
            return cls.global_
        if v == "tenant":
            return cls.tenant
        if v == "user":
            return cls.user
        return cls.global_

