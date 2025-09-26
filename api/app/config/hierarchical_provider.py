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
class ConfigEncryption:
    """Handles configuration value encryption/decryption"""

    def __init__(self, master_key: Optional[str] = None):
        # Use provided key or get from environment
        key = master_key or os.getenv('CONFIG_MASTER_KEY')

        # P0: require 44-char base64 Fernet key; fail fast if missing/invalid
        if not key:
            # For development, generate a key if not provided
            if os.getenv('FAXBOT_ENV') == 'development':
                key = Fernet.generate_key().decode()
                print(f"[WARN] Generated dev CONFIG_MASTER_KEY: {key}")
            else:
                raise ValueError("CONFIG_MASTER_KEY must be set")

        if len(key) != 44:
            raise ValueError("CONFIG_MASTER_KEY must be a 44-char base64 Fernet key")

        self.fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt_value(self, value: Any, should_encrypt: bool = True) -> str:
        """Encrypt configuration value"""
        json_value = json.dumps(value) if not isinstance(value, str) else value
        if should_encrypt:
            return self.fernet.encrypt(json_value.encode()).decode()
        return json_value

    def decrypt_value(self, encrypted_value: str, is_encrypted: bool = True) -> Any:
        """Decrypt configuration value"""
        try:
            if is_encrypted:
                decrypted = self.fernet.decrypt(encrypted_value.encode()).decode()
                try:
                    return json.loads(decrypted)
                except json.JSONDecodeError:
                    return decrypted
            else:
                try:
                    return json.loads(encrypted_value)
                except json.JSONDecodeError:
                    return encrypted_value
        except Exception:
            # Fallback for non-JSON values
            return encrypted_value


class HierarchicalConfigProvider:
    """Phase 3 hierarchical configuration provider with encryption.

    Provides database-first configuration with hierarchical resolution:
    User → Group → Department → Tenant → Global → Environment → Default
    """

    # Built-in defaults for essential configurations
    BUILT_IN_DEFAULTS = {
        'system.public_api_url': 'http://localhost:8080',
        'api.rate_limit_rpm': 60,
        'api.session_timeout_hours': 8,
        'security.enforce_public_https': False,
        'security.require_mfa': False,
        'security.password_min_length': 12,
        'storage.s3.bucket': None,
        'storage.s3.region': 'us-east-1',
        'storage.s3.endpoint_url': None,
        'fax.timeout_seconds': 30,
        'fax.max_pages': 100,
        'fax.retry_attempts': 3,
        'webhook.verify_signatures': True,
        'provider.health_check_interval': 300,
        'provider.circuit_breaker_threshold': 5,
        'provider.circuit_breaker_timeout': 60,
        'audit.retention_days': 365,
        'hipaa.enforce_compliance': False,
    }

    # Configuration keys that should always be encrypted
    ALWAYS_ENCRYPT_KEYS = {
        'api_key', 'secret', 'password', 'token',
        'encryption.master_key', 'session.pepper'
    }

    # Safe keys that can be edited in Admin Console (Phase 3 scope)
    SAFE_EDIT_KEYS = {
        'fax.timeout_seconds': {'type': 'integer', 'min': 10, 'max': 300},
        'fax.max_pages': {'type': 'integer', 'min': 1, 'max': 1000},
        'fax.retry_attempts': {'type': 'integer', 'min': 0, 'max': 10},
        'api.rate_limit_rpm': {'type': 'integer', 'min': 1, 'max': 10000},
        'api.session_timeout_hours': {'type': 'integer', 'min': 1, 'max': 168},
        'provider.health_check_interval': {'type': 'integer', 'min': 30, 'max': 3600},
        'provider.circuit_breaker_threshold': {'type': 'integer', 'min': 1, 'max': 100},
        'provider.circuit_breaker_timeout': {'type': 'integer', 'min': 10, 'max': 600},
        'webhook.verify_signatures': {'type': 'boolean'},
        'security.require_mfa': {'type': 'boolean'},
        'hipaa.enforce_compliance': {'type': 'boolean'},
    }

    def __init__(self, cache: Optional[CacheManager] = None) -> None:
        self.cache_manager = cache or CacheManager()
        self.encryption = ConfigEncryption()
        self._env_mapping = self._build_env_mapping()

    def _build_env_mapping(self) -> Dict[str, str]:
        """Build mapping of config keys to environment variables"""
        return {
            "system.public_api_url": "PUBLIC_API_URL",
            "api.rate_limit_rpm": "API_RATE_LIMIT_RPM",
            "api.session_timeout_hours": "API_SESSION_TIMEOUT_HOURS",
            "security.enforce_public_https": "ENFORCE_PUBLIC_HTTPS",
            "security.require_mfa": "REQUIRE_MFA",
            "storage.s3.bucket": "S3_BUCKET",
            "storage.s3.region": "S3_REGION",
            "storage.s3.endpoint_url": "S3_ENDPOINT_URL",
            "fax.timeout_seconds": "FAX_TIMEOUT_SECONDS",
            "fax.max_pages": "FAX_MAX_PAGES",
            "fax.retry_attempts": "FAX_RETRY_ATTEMPTS",
            "webhook.verify_signatures": "WEBHOOK_VERIFY_SIGNATURES",
            "hipaa.enforce_compliance": "HIPAA_ENFORCE_COMPLIANCE",
        }

    def _should_encrypt(self, key: str) -> bool:
        """Check if a configuration key should be encrypted"""
        return any(sensitive in key.lower() for sensitive in self.ALWAYS_ENCRYPT_KEYS)

    def _mask_value(self, value: Any, key: str) -> str:
        """Mask sensitive values for audit/display"""
        if value is None:
            return "Not set"

        str_value = str(value)
        if self._should_encrypt(key):
            if len(str_value) <= 4:
                return "*" * len(str_value)
            return str_value[:4] + "*" * (len(str_value) - 4)

        return str_value

    async def get_effective(self, key: str, ctx: Optional[UserContext] = None) -> Dict[str, Any]:
        """Get effective configuration value with hierarchical resolution.

        Resolution order:
        1. User level (if ctx.user_id)
        2. Group level (if ctx.groups)
        3. Department level (if ctx.department)
        4. Tenant level (if ctx.tenant_id)
        5. Global level (database)
        6. Environment variable
        7. Built-in default
        """

        # Try cache first
        if self.cache_manager and ctx:
            cache_key = self._build_cache_key('effective', ctx, key)
            cached = await self.cache_manager.get(cache_key)
            if cached:
                return cached

        # Check database levels if available
        if DB_AVAILABLE and ctx:
            # Try each level in order
            async with AsyncSessionLocal() as db:
                # User level
                if ctx.user_id:
                    result = await db.execute(
                        select(ConfigUser).where(
                            ConfigUser.user_id == ctx.user_id,
                            ConfigUser.key == key
                        )
                    )
                    config = result.scalar_one_or_none()
                    if config:
                        value = self.encryption.decrypt_value(
                            config.value_encrypted, config.encrypted
                        )
                        result = {"key": key, "value": value, "source": "db", "level": "user"}
                        if self.cache_manager:
                            await self.cache_manager.set(cache_key, result, ttl=300)
                        return result

                # Group level
                if ctx.groups:
                    for group_id in ctx.groups:
                        result = await db.execute(
                            select(ConfigGroup).where(
                                ConfigGroup.group_id == group_id,
                                ConfigGroup.key == key
                            ).order_by(ConfigGroup.priority.desc())
                        )
                        config = result.scalar_one_or_none()
                        if config:
                            value = self.encryption.decrypt_value(
                                config.value_encrypted, config.encrypted
                            )
                            result = {"key": key, "value": value, "source": "db", "level": "group"}
                            if self.cache_manager:
                                await self.cache_manager.set(cache_key, result, ttl=300)
                            return result

                # Department level
                if ctx.tenant_id and ctx.department:
                    result = await db.execute(
                        select(ConfigDepartment).where(
                            ConfigDepartment.tenant_id == ctx.tenant_id,
                            ConfigDepartment.department == ctx.department,
                            ConfigDepartment.key == key
                        )
                    )
                    config = result.scalar_one_or_none()
                    if config:
                        value = self.encryption.decrypt_value(
                            config.value_encrypted, config.encrypted
                        )
                        result = {"key": key, "value": value, "source": "db", "level": "department"}
                        if self.cache_manager:
                            await self.cache_manager.set(cache_key, result, ttl=300)
                        return result

                # Tenant level
                if ctx.tenant_id:
                    result = await db.execute(
                        select(ConfigTenant).where(
                            ConfigTenant.tenant_id == ctx.tenant_id,
                            ConfigTenant.key == key
                        )
                    )
                    config = result.scalar_one_or_none()
                    if config:
                        value = self.encryption.decrypt_value(
                            config.value_encrypted, config.encrypted
                        )
                        result = {"key": key, "value": value, "source": "db", "level": "tenant"}
                        if self.cache_manager:
                            await self.cache_manager.set(cache_key, result, ttl=300)
                        return result

                # Global level
                result = await db.execute(
                    select(ConfigGlobal).where(ConfigGlobal.key == key)
                )
                config = result.scalar_one_or_none()
                if config:
                    value = self.encryption.decrypt_value(
                        config.value_encrypted, config.encrypted
                    )
                    result = {"key": key, "value": value, "source": "db", "level": "global"}
                    if self.cache_manager:
                        await self.cache_manager.set(cache_key, result, ttl=300)
                    return result

        # Check environment variable
        env_key = self._env_mapping.get(key, key.upper().replace(".", "_"))
        env_value = os.getenv(env_key)
        if env_value is not None:
            # Parse boolean strings
            if env_value.lower() in ('true', 'false'):
                value = env_value.lower() == 'true'
            # Parse numeric strings
            elif env_value.isdigit():
                value = int(env_value)
            else:
                value = env_value

            return {"key": key, "value": value, "source": "env"}

        # Check built-in defaults
        if key in self.BUILT_IN_DEFAULTS:
            return {"key": key, "value": self.BUILT_IN_DEFAULTS[key], "source": "default"}

        # Unknown key
        return {"key": key, "value": None, "source": None}

    async def get_hierarchy(self, key: str, ctx: Optional[UserContext] = None) -> Dict[str, Any]:
        """Get configuration hierarchy for a key showing values at each level."""

        hierarchy = {
            "key": key,
            "levels": {
                "user": None,
                "group": None,
                "department": None,
                "tenant": None,
                "global": None,
                "env": None,
                "default": None,
            },
            "effective": await self.get_effective(key, ctx),
        }

        # Check database levels if available
        if DB_AVAILABLE and ctx:
            async with AsyncSessionLocal() as db:
                # User level
                if ctx.user_id:
                    result = await db.execute(
                        select(ConfigUser).where(
                            ConfigUser.user_id == ctx.user_id,
                            ConfigUser.key == key
                        )
                    )
                    config = result.scalar_one_or_none()
                    if config:
                        hierarchy["levels"]["user"] = self.encryption.decrypt_value(
                            config.value_encrypted, config.encrypted
                        )

                # Group level
                if ctx.groups:
                    for group_id in ctx.groups:
                        result = await db.execute(
                            select(ConfigGroup).where(
                                ConfigGroup.group_id == group_id,
                                ConfigGroup.key == key
                            ).order_by(ConfigGroup.priority.desc())
                        )
                        config = result.scalar_one_or_none()
                        if config:
                            hierarchy["levels"]["group"] = self.encryption.decrypt_value(
                                config.value_encrypted, config.encrypted
                            )
                            break

                # Department level
                if ctx.tenant_id and ctx.department:
                    result = await db.execute(
                        select(ConfigDepartment).where(
                            ConfigDepartment.tenant_id == ctx.tenant_id,
                            ConfigDepartment.department == ctx.department,
                            ConfigDepartment.key == key
                        )
                    )
                    config = result.scalar_one_or_none()
                    if config:
                        hierarchy["levels"]["department"] = self.encryption.decrypt_value(
                            config.value_encrypted, config.encrypted
                        )

                # Tenant level
                if ctx.tenant_id:
                    result = await db.execute(
                        select(ConfigTenant).where(
                            ConfigTenant.tenant_id == ctx.tenant_id,
                            ConfigTenant.key == key
                        )
                    )
                    config = result.scalar_one_or_none()
                    if config:
                        hierarchy["levels"]["tenant"] = self.encryption.decrypt_value(
                            config.value_encrypted, config.encrypted
                        )

                # Global level
                result = await db.execute(
                    select(ConfigGlobal).where(ConfigGlobal.key == key)
                )
                config = result.scalar_one_or_none()
                if config:
                    hierarchy["levels"]["global"] = self.encryption.decrypt_value(
                        config.value_encrypted, config.encrypted
                    )

        # Check environment
        env_key = self._env_mapping.get(key, key.upper().replace(".", "_"))
        env_value = os.getenv(env_key)
        if env_value:
            hierarchy["levels"]["env"] = env_value

        # Check default
        if key in self.BUILT_IN_DEFAULTS:
            hierarchy["levels"]["default"] = self.BUILT_IN_DEFAULTS[key]

        return hierarchy

    async def get_safe_edit_keys(self) -> Dict[str, Dict[str, Any]]:
        """Get configuration keys that are safe to edit via Admin Console."""
        return self.SAFE_EDIT_KEYS.copy()

    async def flush_cache(self, scope: Optional[str] = None) -> Dict[str, Any]:
        if not self.cache_manager:
            return {"ok": True, "deleted": 0, "scope": scope or "all", "backend": "none"}
        if scope and scope != "*":
            deleted = await self.cache_manager.delete_pattern(scope)
            return {"ok": True, "deleted": deleted, "scope": scope, "backend": "memory"}
        await self.cache_manager.flush_all()
        return {"ok": True, "deleted": None, "scope": "all", "backend": "memory"}

    def _build_cache_key(self, prefix: str, ctx: UserContext, key: str) -> str:
        """Build cache key for hierarchical config"""
        context_parts = [
            ctx.tenant_id or 'null',
            ctx.department or 'null',
            ctx.user_id or 'null',
            ','.join(sorted(ctx.groups)) if ctx.groups else 'null'
        ]
        return f"cfg:{prefix}:{':'.join(context_parts)}:{key}"

    async def validate_config_value(self, key: str, value: Any) -> bool:
        """Validate a configuration value against its constraints."""
        if key not in self.SAFE_EDIT_KEYS:
            return False

        constraints = self.SAFE_EDIT_KEYS[key]
        value_type = constraints.get('type')

        if value_type == 'integer':
            if not isinstance(value, int):
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    return False

            if 'min' in constraints and value < constraints['min']:
                return False
            if 'max' in constraints and value > constraints['max']:
                return False

        elif value_type == 'boolean':
            if not isinstance(value, bool):
                if isinstance(value, str):
                    if value.lower() not in ('true', 'false'):
                        return False
                else:
                    return False

        elif value_type == 'string':
            if not isinstance(value, str):
                return False

            if 'min_length' in constraints and len(value) < constraints['min_length']:
                return False
            if 'max_length' in constraints and len(value) > constraints['max_length']:
                return False

        return True

=======
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
