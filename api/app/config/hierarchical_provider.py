import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database.async_db import AsyncSessionLocal  # type: ignore
from ..models.config import (
    ConfigGlobal,
    ConfigTenant,
    ConfigDepartment,
    ConfigGroup,
    ConfigUser,
    ConfigAudit,
)
from ..services.cache_manager import CacheManager

from cryptography.fernet import Fernet  # type: ignore

# Check if database is available
try:
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False


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

    async def set_config_value(self, key: str, value: Any, level: ConfigLevel, level_id: Optional[str] = None, reason: Optional[str] = None, changed_by: Optional[str] = None) -> Dict[str, Any]:
        """Set configuration value at the specified level.

        Args:
            key: Configuration key
            value: Configuration value
            level: Configuration level (global, tenant, department, group, user)
            level_id: ID for the level (e.g., user_id, tenant_id)
            reason: Reason for the change (for audit)
            changed_by: Who made the change (user_id or api_key_id)

        Returns:
            Dict with status and details
        """
        if not DB_AVAILABLE:
            return {"success": False, "error": "Database not available"}

        # Validate key is allowed for editing
        if not await self.validate_config_value(key, value):
            return {"success": False, "error": f"Invalid value for key {key}"}

        should_encrypt = self._should_encrypt(key)
        encrypted_value = self.encryption.encrypt_value(value, should_encrypt)

        try:
            async with AsyncSessionLocal() as db:
                old_value = None
                config_obj = None

                # Get or create config object based on level
                if level == "global":
                    result = await db.execute(select(ConfigGlobal).where(ConfigGlobal.key == key))
                    config_obj = result.scalar_one_or_none()
                    if not config_obj:
                        config_obj = ConfigGlobal(key=key, value_encrypted=encrypted_value, encrypted=should_encrypt)
                        db.add(config_obj)
                    else:
                        old_value = self.encryption.decrypt_value(config_obj.value_encrypted, config_obj.encrypted)
                        config_obj.value_encrypted = encrypted_value
                        config_obj.encrypted = should_encrypt

                elif level == "tenant" and level_id:
                    result = await db.execute(
                        select(ConfigTenant).where(
                            ConfigTenant.tenant_id == level_id,
                            ConfigTenant.key == key
                        )
                    )
                    config_obj = result.scalar_one_or_none()
                    if not config_obj:
                        config_obj = ConfigTenant(
                            tenant_id=level_id, key=key,
                            value_encrypted=encrypted_value, encrypted=should_encrypt
                        )
                        db.add(config_obj)
                    else:
                        old_value = self.encryption.decrypt_value(config_obj.value_encrypted, config_obj.encrypted)
                        config_obj.value_encrypted = encrypted_value
                        config_obj.encrypted = should_encrypt

                elif level == "user" and level_id:
                    result = await db.execute(
                        select(ConfigUser).where(
                            ConfigUser.user_id == level_id,
                            ConfigUser.key == key
                        )
                    )
                    config_obj = result.scalar_one_or_none()
                    if not config_obj:
                        config_obj = ConfigUser(
                            user_id=level_id, key=key,
                            value_encrypted=encrypted_value, encrypted=should_encrypt
                        )
                        db.add(config_obj)
                    else:
                        old_value = self.encryption.decrypt_value(config_obj.value_encrypted, config_obj.encrypted)
                        config_obj.value_encrypted = encrypted_value
                        config_obj.encrypted = should_encrypt

                else:
                    return {"success": False, "error": f"Unsupported level: {level}"}

                await db.commit()

                # Create audit entry
                if changed_by:
                    audit_entry = ConfigAudit(
                        level=level,
                        level_id=level_id,
                        key=key,
                        old_value=self._mask_value(old_value, key) if old_value else None,
                        new_value=self._mask_value(value, key),
                        encrypted=should_encrypt,
                        reason=reason,
                        changed_by=changed_by
                    )
                    db.add(audit_entry)
                    await db.commit()

                # Clear cache for this key
                if self.cache_manager:
                    await self.cache_manager.flush_pattern(f"cfg:*:{key}")

                return {
                    "success": True,
                    "key": key,
                    "value": value,
                    "level": level,
                    "encrypted": should_encrypt,
                    "old_value": old_value
                }

        except Exception as e:
            return {"success": False, "error": str(e)}
