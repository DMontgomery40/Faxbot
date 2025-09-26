"""
Hierarchical configuration database models for Phase 3.

Implements multi-level configuration with encryption at rest
and comprehensive audit trail.
"""

from datetime import datetime
from sqlalchemy import Column, String, Text, Boolean, DateTime, Integer, Index
from sqlalchemy.sql import func

from api.app.db import Base


class ConfigGlobal(Base):
    """Global configuration table - system-wide defaults"""
    __tablename__ = "config_global"

    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default='string')
    encrypted = Column(Boolean(), nullable=False, default=True)
    description = Column(Text(), nullable=True)
    category = Column(String(50), nullable=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_global_key', 'key'),
        Index('idx_global_category', 'category'),
    )


class ConfigTenant(Base):
    """Tenant-level configuration overrides"""
    __tablename__ = "config_tenant"

    tenant_id = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default='string')
    encrypted = Column(Boolean(), nullable=False, default=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_tenant_key', 'tenant_id', 'key'),
    )


class ConfigDepartment(Base):
    """Department-level configuration within a tenant"""
    __tablename__ = "config_department"

    tenant_id = Column(String(100), primary_key=True, nullable=False)
    department = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default='string')
    encrypted = Column(Boolean(), nullable=False, default=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_dept_key', 'tenant_id', 'department', 'key'),
    )


class ConfigGroup(Base):
    """Group-level configuration overrides"""
    __tablename__ = "config_group"

    group_id = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default='string')
    encrypted = Column(Boolean(), nullable=False, default=True)
    priority = Column(Integer(), nullable=False, default=0)  # For group ordering
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_group_key', 'group_id', 'key'),
        Index('idx_group_priority', 'group_id', 'priority'),
    )


class ConfigUser(Base):
    """User-specific configuration overrides"""
    __tablename__ = "config_user"

    user_id = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default='string')
    encrypted = Column(Boolean(), nullable=False, default=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_user_key', 'user_id', 'key'),
    )


class ConfigAudit(Base):
    """Configuration change audit trail"""
    __tablename__ = "config_audit"

    id = Column(String(40), primary_key=True, nullable=False)
    level = Column(String(20), nullable=False)  # global|tenant|department|group|user
    level_id = Column(String(200), nullable=True)  # tenant_id, group_id, user_id, etc.
    key = Column(String(200), nullable=False)

    # Store masked snapshots only; never decrypted secrets
    old_value_masked = Column(Text(), nullable=True)
    new_value_masked = Column(Text(), nullable=False)

    # Integrity fingerprint for audit diffs (HMAC with server-side AUDIT_PEPPER)
    value_hmac = Column(String(64), nullable=False)  # hex sha256

    value_type = Column(String(20), nullable=False)
    changed_by = Column(String(100), nullable=False)  # user_id or api_key_id
    changed_at = Column(DateTime(), nullable=False, server_default=func.now())
    reason = Column(Text(), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text(), nullable=True)

    __table_args__ = (
        Index('idx_audit_level', 'level', 'level_id'),
        Index('idx_audit_key', 'key'),
        Index('idx_audit_time', 'changed_at'),
        Index('idx_audit_user', 'changed_by'),
    )