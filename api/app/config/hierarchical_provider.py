import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.app.database.async_db import AsyncSessionLocal  # type: ignore
from api.app.models.config import (
    ConfigGlobal,
    ConfigTenant,
    ConfigDepartment,
    ConfigGroup,
    ConfigUser,
    ConfigAudit,
)

from cryptography.fernet import Fernet  # type: ignore


ConfigLevel = Literal["global", "tenant", "department", "group", "user"]
ConfigSource = Literal["db", "env", "default", "cache"]


@dataclass
class UserContext:
    user_id: Optional[str]
    tenant_id: Optional[str] = None
    department: Optional[str] = None
    groups: List[str] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.groups is None:
            self.groups = []


@dataclass
class ConfigValue:
    value: Any
    source: ConfigSource
    level: Optional[ConfigLevel] = None
    level_id: Optional[str] = None
    encrypted: bool = False
    updated_at: Optional[datetime] = None


class ConfigEncryption:
    def __init__(self, master_key: str):
        if not master_key or len(master_key) != 44:
            raise ValueError("CONFIG_MASTER_KEY must be a 44-char base64 Fernet key")
        self.fernet = Fernet(master_key.encode())

    def encrypt(self, value: Any, encrypt_flag: bool) -> str:
        as_json = json.dumps(value)
        return (
            self.fernet.encrypt(as_json.encode()).decode() if encrypt_flag else as_json
        )

    def decrypt(self, stored: str, is_encrypted: bool) -> Any:
        try:
            if is_encrypted:
                dec = self.fernet.decrypt(stored.encode()).decode()
                return json.loads(dec)
            return json.loads(stored)
        except Exception:
            return stored


class HierarchicalConfigProvider:
    BUILT_IN_DEFAULTS: Dict[str, Any] = {
        "fax.timeout_seconds": 30,
        "fax.max_pages": 100,
        "fax.retry_attempts": 3,
        "api.rate_limit_rpm": 60,
        "notifications.enable_sse": True,
    }

    ALWAYS_ENCRYPT_KEYS = {
        "fax.provider.api_key",
        "fax.provider.secret",
        "oauth.client_secret",
    }

    SAFE_EDIT_KEYS: Dict[str, Dict[str, Any]] = {
        "fax.timeout_seconds": {"type": "integer", "min": 5, "max": 300},
        "fax.retry_attempts": {"type": "integer", "min": 0, "max": 10},
        "api.rate_limit_rpm": {"type": "integer", "min": 1, "max": 10000},
        "notifications.enable_sse": {"type": "boolean"},
    }

    def __init__(self, encryption_key: str, cache_manager=None):
        self.enc = ConfigEncryption(encryption_key)
        self.cache = cache_manager

    async def get_effective(
        self, key: str, user_ctx: UserContext, default: Any = None
    ) -> ConfigValue:
        # Cache first
        if self.cache:
            ckey = self._cache_key("eff", user_ctx, key)
            cached = await self.cache.get(ckey)
            if cached:
                return ConfigValue(**cached, source="cache")

        val = await self._resolve(key, user_ctx, default)

        if self.cache and val.source == "db":
            ckey = self._cache_key("eff", user_ctx, key)
            await self.cache.set(
                ckey,
                {
                    "value": val.value,
                    "source": "db",
                    "level": val.level,
                    "level_id": val.level_id,
                    "encrypted": val.encrypted,
                    "updated_at": val.updated_at.isoformat() if val.updated_at else None,
                },
                ttl=300,
            )

        return val

    async def _resolve(
        self, key: str, user_ctx: UserContext, default: Any = None
    ) -> ConfigValue:
        async with AsyncSessionLocal() as db:  # type: ignore
            # 1. User
            if user_ctx.user_id:
                q = await db.execute(
                    select(ConfigUser).where(
                        ConfigUser.user_id == user_ctx.user_id,
                        ConfigUser.key == key,
                    )
                )
                row = q.scalar_one_or_none()
                if row:
                    return ConfigValue(
                        value=self.enc.decrypt(row.value_encrypted, row.encrypted),
                        source="db",
                        level="user",
                        level_id=user_ctx.user_id,
                        encrypted=row.encrypted,
                        updated_at=row.updated_at,
                    )

            # 2. Group (first by priority desc)
            if user_ctx.groups:
                q = await db.execute(
                    select(ConfigGroup)
                    .where(ConfigGroup.group_id.in_(user_ctx.groups), ConfigGroup.key == key)
                    .order_by(ConfigGroup.priority.desc())
                )
                grp = q.first()
                if grp:
                    grp = grp[0]
                    return ConfigValue(
                        value=self.enc.decrypt(grp.value_encrypted, grp.encrypted),
                        source="db",
                        level="group",
                        level_id=grp.group_id,
                        encrypted=grp.encrypted,
                        updated_at=grp.updated_at,
                    )

            # 3. Department
            if user_ctx.tenant_id and user_ctx.department:
                q = await db.execute(
                    select(ConfigDepartment).where(
                        ConfigDepartment.tenant_id == user_ctx.tenant_id,
                        ConfigDepartment.department == user_ctx.department,
                        ConfigDepartment.key == key,
                    )
                )
                dep = q.scalar_one_or_none()
                if dep:
                    return ConfigValue(
                        value=self.enc.decrypt(dep.value_encrypted, dep.encrypted),
                        source="db",
                        level="department",
                        level_id=f"{user_ctx.tenant_id}:{user_ctx.department}",
                        encrypted=dep.encrypted,
                        updated_at=dep.updated_at,
                    )

            # 4. Tenant
            if user_ctx.tenant_id:
                q = await db.execute(
                    select(ConfigTenant).where(
                        ConfigTenant.tenant_id == user_ctx.tenant_id,
                        ConfigTenant.key == key,
                    )
                )
                ten = q.scalar_one_or_none()
                if ten:
                    return ConfigValue(
                        value=self.enc.decrypt(ten.value_encrypted, ten.encrypted),
                        source="db",
                        level="tenant",
                        level_id=user_ctx.tenant_id,
                        encrypted=ten.encrypted,
                        updated_at=ten.updated_at,
                    )

            # 5. Global
            q = await db.execute(select(ConfigGlobal).where(ConfigGlobal.key == key))
            glob = q.scalar_one_or_none()
            if glob:
                return ConfigValue(
                    value=self.enc.decrypt(glob.value_encrypted, glob.encrypted),
                    source="db",
                    level="global",
                    encrypted=glob.encrypted,
                    updated_at=glob.updated_at,
                )

        # 6. Built-in defaults
        if key in self.BUILT_IN_DEFAULTS:
            return ConfigValue(value=self.BUILT_IN_DEFAULTS[key], source="default")

        # 7. fallback (unset)
        if default is not None:
            return ConfigValue(value=default, source="default")
        raise KeyError(key)

    def _cache_key(self, prefix: str, u: UserContext, key: str) -> str:
        parts = [u.tenant_id or "null", u.department or "null", u.user_id or "null", ",".join(sorted(u.groups)) or "null"]
        return f"cfg:{prefix}:{':'.join(parts)}:{key}"

    async def get_hierarchy(self, key: str, user_ctx: UserContext) -> List[ConfigValue]:
        """Return layered values from highest to lowest priority present + default."""
        layers: List[ConfigValue] = []
        try:
            # user
            if user_ctx.user_id:
                async with AsyncSessionLocal() as db:  # type: ignore
                    q = await db.execute(select(ConfigUser).where(ConfigUser.user_id == user_ctx.user_id, ConfigUser.key == key))
                    row = q.scalar_one_or_none()
                    if row:
                        layers.append(
                            ConfigValue(
                                value=self.enc.decrypt(row.value_encrypted, row.encrypted),
                                source="db",
                                level="user",
                                level_id=user_ctx.user_id,
                                encrypted=row.encrypted,
                                updated_at=row.updated_at,
                            )
                        )
            # group (first only reported)
            if user_ctx.groups:
                async with AsyncSessionLocal() as db:  # type: ignore
                    q = await db.execute(
                        select(ConfigGroup)
                        .where(ConfigGroup.group_id.in_(user_ctx.groups), ConfigGroup.key == key)
                        .order_by(ConfigGroup.priority.desc())
                    )
                    grp = q.first()
                    if grp:
                        grp = grp[0]
                        layers.append(
                            ConfigValue(
                                value=self.enc.decrypt(grp.value_encrypted, grp.encrypted),
                                source="db",
                                level="group",
                                level_id=grp.group_id,
                                encrypted=grp.encrypted,
                                updated_at=grp.updated_at,
                            )
                        )
            # department
            if user_ctx.tenant_id and user_ctx.department:
                async with AsyncSessionLocal() as db:  # type: ignore
                    q = await db.execute(
                        select(ConfigDepartment).where(
                            ConfigDepartment.tenant_id == user_ctx.tenant_id,
                            ConfigDepartment.department == user_ctx.department,
                            ConfigDepartment.key == key,
                        )
                    )
                    dep = q.scalar_one_or_none()
                    if dep:
                        layers.append(
                            ConfigValue(
                                value=self.enc.decrypt(dep.value_encrypted, dep.encrypted),
                                source="db",
                                level="department",
                                level_id=f"{user_ctx.tenant_id}:{user_ctx.department}",
                                encrypted=dep.encrypted,
                                updated_at=dep.updated_at,
                            )
                        )
            # tenant
            if user_ctx.tenant_id:
                async with AsyncSessionLocal() as db:  # type: ignore
                    q = await db.execute(select(ConfigTenant).where(ConfigTenant.tenant_id == user_ctx.tenant_id, ConfigTenant.key == key))
                    ten = q.scalar_one_or_none()
                    if ten:
                        layers.append(
                            ConfigValue(
                                value=self.enc.decrypt(ten.value_encrypted, ten.encrypted),
                                source="db",
                                level="tenant",
                                level_id=user_ctx.tenant_id,
                                encrypted=ten.encrypted,
                                updated_at=ten.updated_at,
                            )
                        )
            # global
            async with AsyncSessionLocal() as db:  # type: ignore
                q = await db.execute(select(ConfigGlobal).where(ConfigGlobal.key == key))
                glob = q.scalar_one_or_none()
                if glob:
                    layers.append(
                        ConfigValue(
                            value=self.enc.decrypt(glob.value_encrypted, glob.encrypted),
                            source="db",
                            level="global",
                            encrypted=glob.encrypted,
                            updated_at=glob.updated_at,
                        )
                    )
        except Exception:
            pass
        if key in self.BUILT_IN_DEFAULTS:
            layers.append(ConfigValue(value=self.BUILT_IN_DEFAULTS[key], source="default"))
        return layers

    async def get_safe_edit_keys(self) -> Dict[str, Dict[str, Any]]:
        return dict(self.SAFE_EDIT_KEYS)

    async def set_config(
        self,
        key: str,
        value: Any,
        level: ConfigLevel,
        level_id: Optional[str] = None,
        reason: str = "Admin update",
        encrypt: Optional[bool] = None
    ) -> bool:
        """Set a configuration value at the specified level."""
        # Determine if we should encrypt this value
        should_encrypt = encrypt if encrypt is not None else (key in self.ALWAYS_ENCRYPT_KEYS)

        # Validate the key is safe to edit (optional validation)
        if key not in self.SAFE_EDIT_KEYS and key not in self.ALWAYS_ENCRYPT_KEYS:
            # Allow admin to set any key, but warn about unsafe keys
            pass

        try:
            async with AsyncSessionLocal() as db:  # type: ignore
                encrypted_value = self.enc.encrypt(value, should_encrypt)
                now = datetime.utcnow()

                if level == "global":
                    # Check if exists
                    existing = await db.execute(
                        select(ConfigGlobal).where(ConfigGlobal.key == key)
                    )
                    existing_row = existing.scalar_one_or_none()

                    if existing_row:
                        existing_row.value_encrypted = encrypted_value
                        existing_row.encrypted = should_encrypt
                        existing_row.updated_at = now
                    else:
                        new_row = ConfigGlobal(
                            id=str(uuid.uuid4()),
                            key=key,
                            value_encrypted=encrypted_value,
                            encrypted=should_encrypt,
                            updated_at=now
                        )
                        db.add(new_row)

                elif level == "tenant":
                    if not level_id:
                        raise ValueError("tenant level requires level_id")

                    existing = await db.execute(
                        select(ConfigTenant).where(
                            ConfigTenant.tenant_id == level_id,
                            ConfigTenant.key == key
                        )
                    )
                    existing_row = existing.scalar_one_or_none()

                    if existing_row:
                        existing_row.value_encrypted = encrypted_value
                        existing_row.encrypted = should_encrypt
                        existing_row.updated_at = now
                    else:
                        new_row = ConfigTenant(
                            id=str(uuid.uuid4()),
                            tenant_id=level_id,
                            key=key,
                            value_encrypted=encrypted_value,
                            encrypted=should_encrypt,
                            updated_at=now
                        )
                        db.add(new_row)

                elif level == "user":
                    if not level_id:
                        raise ValueError("user level requires level_id")

                    existing = await db.execute(
                        select(ConfigUser).where(
                            ConfigUser.user_id == level_id,
                            ConfigUser.key == key
                        )
                    )
                    existing_row = existing.scalar_one_or_none()

                    if existing_row:
                        existing_row.value_encrypted = encrypted_value
                        existing_row.encrypted = should_encrypt
                        existing_row.updated_at = now
                    else:
                        new_row = ConfigUser(
                            id=str(uuid.uuid4()),
                            user_id=level_id,
                            key=key,
                            value_encrypted=encrypted_value,
                            encrypted=should_encrypt,
                            updated_at=now
                        )
                        db.add(new_row)

                # TODO: Add department and group levels when needed

                # Create audit entry
                audit_entry = ConfigAudit(
                    id=str(uuid.uuid4()),
                    key=key,
                    level=level,
                    level_id=level_id,
                    old_value=None,  # Could get from existing_row if needed
                    new_value=encrypted_value,
                    encrypted=should_encrypt,
                    reason=reason,
                    changed_at=now,
                    changed_by="admin"  # Could extract from auth context
                )
                db.add(audit_entry)

                await db.commit()

                # Invalidate cache
                if self.cache:
                    await self._invalidate_cache_for_key(key)

                return True

        except Exception:
            return False

    async def _invalidate_cache_for_key(self, key: str) -> None:
        """Invalidate all cached entries for a specific key."""
        if not self.cache:
            return

        try:
            # For now, just flush all cache entries
            # In production, we'd want more targeted invalidation
            await self.cache.flush_pattern(f"cfg:eff:*:{key}")
        except Exception:
            # If targeted invalidation fails, flush all
            try:
                await self.cache.flush_all()
            except Exception:
                pass
