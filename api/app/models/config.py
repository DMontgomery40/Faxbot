"""
Database models for hierarchical configuration system.

Supports User → Group → Department → Tenant → Global → Default resolution
with encryption, audit trail, and cache invalidation.
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Boolean, Text, Integer
from sqlalchemy.sql import func

from ..db import Base


class ConfigGlobal(Base):  # type: ignore
    """Global configuration values - lowest priority in hierarchy."""
    __tablename__ = "config_global"

    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String(200), unique=True, nullable=False, index=True)
    value_encrypted = Column(Text, nullable=False)
    encrypted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ConfigTenant(Base):  # type: ignore
    """Tenant-level configuration values."""
    __tablename__ = "config_tenant"

    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(100), nullable=False, index=True)
    key = Column(String(200), nullable=False, index=True)
    value_encrypted = Column(Text, nullable=False)
    encrypted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        {'sqlite_autoincrement': True},
    )


class ConfigDepartment(Base):  # type: ignore
    """Department-level configuration values."""
    __tablename__ = "config_department"

    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(100), nullable=False, index=True)
    department = Column(String(100), nullable=False, index=True)
    key = Column(String(200), nullable=False, index=True)
    value_encrypted = Column(Text, nullable=False)
    encrypted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ConfigGroup(Base):  # type: ignore
    """Group-level configuration values with priority ordering."""
    __tablename__ = "config_group"

    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String(100), nullable=False, index=True)
    key = Column(String(200), nullable=False, index=True)
    value_encrypted = Column(Text, nullable=False)
    encrypted = Column(Boolean, nullable=False, default=False)
    priority = Column(Integer, nullable=False, default=100)  # Higher = higher priority
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ConfigUser(Base):  # type: ignore
    """User-level configuration values - highest priority in hierarchy."""
    __tablename__ = "config_user"

    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(100), nullable=False, index=True)
    key = Column(String(200), nullable=False, index=True)
    value_encrypted = Column(Text, nullable=False)
    encrypted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ConfigAudit(Base):  # type: ignore
    """Audit trail for configuration changes."""
    __tablename__ = "config_audit"

    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String(200), nullable=False, index=True)
    level = Column(String(20), nullable=False)  # global, tenant, department, group, user
    level_id = Column(String(100), nullable=True)  # ID for the level (tenant_id, user_id, etc)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=False)
    encrypted = Column(Boolean, nullable=False, default=False)
    reason = Column(String(500), nullable=True)
    changed_by = Column(String(100), nullable=True)
    changed_at = Column(DateTime, nullable=False, server_default=func.now())
