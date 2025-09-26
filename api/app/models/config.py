from datetime import datetime
from sqlalchemy import Column, String, Text, Boolean, DateTime, Integer
from sqlalchemy.sql import func

# Use the existing Base from the sync DB module so Alembic can import models
from api.app.db import Base  # type: ignore


class ConfigGlobal(Base):  # type: ignore
    """Global configuration table (system-wide defaults)."""
    __tablename__ = "config_global"

    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default="string")
    encrypted = Column(Boolean(), nullable=False, default=True)
    description = Column(Text(), nullable=True)
    category = Column(String(50), nullable=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now())


class ConfigTenant(Base):  # type: ignore
    """Tenant-level configuration."""
    __tablename__ = "config_tenant"

    tenant_id = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default="string")
    encrypted = Column(Boolean(), nullable=False, default=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now())


class ConfigDepartment(Base):  # type: ignore
    """Department-level configuration."""
    __tablename__ = "config_department"

    tenant_id = Column(String(100), primary_key=True, nullable=False)
    department = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default="string")
    encrypted = Column(Boolean(), nullable=False, default=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now())


class ConfigGroup(Base):  # type: ignore
    """Group-level configuration with priority to decide first-match order."""
    __tablename__ = "config_group"

    group_id = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default="string")
    encrypted = Column(Boolean(), nullable=False, default=True)
    priority = Column(Integer(), nullable=False, default=0)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now())


class ConfigUser(Base):  # type: ignore
    """User-level configuration."""
    __tablename__ = "config_user"

    user_id = Column(String(100), primary_key=True, nullable=False)
    key = Column(String(200), primary_key=True, nullable=False)
    value_encrypted = Column(Text(), nullable=False)
    value_type = Column(String(20), nullable=False, default="string")
    encrypted = Column(Boolean(), nullable=False, default=True)
    created_at = Column(DateTime(), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(), nullable=False, server_default=func.now())


class ConfigAudit(Base):  # type: ignore
    """Configuration audit trail (masked values; integrity fingerprint only)."""
    __tablename__ = "config_audit"

    id = Column(String(40), primary_key=True, nullable=False)
    level = Column(String(20), nullable=False)  # global|tenant|department|group|user
    level_id = Column(String(200), nullable=True)
    key = Column(String(200), nullable=False)
    old_value_masked = Column(Text(), nullable=True)
    new_value_masked = Column(Text(), nullable=False)
    value_hmac = Column(String(64), nullable=False)
    value_type = Column(String(20), nullable=False)
    changed_by = Column(String(100), nullable=False)
    changed_at = Column(DateTime(), nullable=False, server_default=func.now())
    reason = Column(Text(), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text(), nullable=True)

