from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, UniqueConstraint  # type: ignore
from sqlalchemy.orm import declarative_base, sessionmaker  # type: ignore
from datetime import datetime
from .config import settings


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
    expire_on_commit=False,
)
Base = declarative_base()


class FaxJob(Base):  # type: ignore
    __tablename__ = "fax_jobs"
    id = Column(String(40), primary_key=True, index=True)
    to_number = Column(String(64), index=True, nullable=False)
    file_name = Column(String(255), nullable=False)
    tiff_path = Column(String(512), nullable=False)
    status = Column(String(32), index=True, nullable=False, default="queued")
    error = Column(Text, nullable=True)
    pages = Column(Integer, nullable=True)
    backend = Column(String(20), nullable=False, default="sip")  # "sip" or cloud provider key
    outbound_backend = Column(String(20), nullable=True)  # effective outbound backend (hybrid)
    provider_sid = Column(String(100), nullable=True)  # Cloud provider fax ID
    pdf_url = Column(String(512), nullable=True)  # Public URL for PDF (for cloud backend)
    pdf_token = Column(String(128), nullable=True)  # Secure token for PDF fetch
    pdf_token_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class APIKey(Base):  # type: ignore
    __tablename__ = "api_keys"
    id = Column(String(40), primary_key=True, index=True)
    key_id = Column(String(32), unique=True, index=True, nullable=False)
    key_hash = Column(String(200), nullable=False)
    name = Column(String(100), nullable=True)
    owner = Column(String(100), nullable=True)
    scopes = Column(String(200), nullable=True)  # CSV list of scopes
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)


class InboundFax(Base):  # type: ignore
    __tablename__ = "inbound_faxes"
    id = Column(String(40), primary_key=True, index=True)
    from_number = Column(String(64), index=True, nullable=True)
    to_number = Column(String(64), index=True, nullable=True)
    status = Column(String(32), index=True, nullable=False, default="received")
    backend = Column(String(20), nullable=False)
    inbound_backend = Column(String(20), nullable=True)  # effective inbound backend (hybrid)
    provider_sid = Column(String(100), nullable=True)
    pages = Column(Integer, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    pdf_path = Column(String(512), nullable=True)
    tiff_path = Column(String(512), nullable=True)
    mailbox_label = Column(String(100), nullable=True)
    retention_until = Column(DateTime, nullable=True)
    pdf_token = Column(String(128), nullable=True)
    pdf_token_expires_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Mailbox(Base):  # type: ignore
    __tablename__ = "mailboxes"
    id = Column(String(40), primary_key=True, index=True)
    label = Column(String(100), unique=True, nullable=False)
    allowed_scopes = Column(String(200), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class InboundRule(Base):  # type: ignore
    __tablename__ = "inbound_rules"
    id = Column(String(40), primary_key=True, index=True)
    to_number = Column(String(64), index=True, nullable=False)
    mailbox_label = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class InboundEvent(Base):  # type: ignore
    __tablename__ = "inbound_events"
    id = Column(String(40), primary_key=True, index=True)
    provider_sid = Column(String(100), nullable=False)
    event_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    __table_args__ = (UniqueConstraint('provider_sid', 'event_type', name='uix_inbound_events_sid_type'),)


def _rebind_engine_if_needed() -> None:
    global engine, SessionLocal
    target_url = settings.database_url
    current_url = str(engine.url)
    if current_url != target_url:
        engine = create_engine(target_url, future=True)
        SessionLocal.configure(bind=engine)


def init_db():
    _rebind_engine_if_needed()
    Base.metadata.create_all(engine)
    _ensure_optional_columns()


def _ensure_optional_columns() -> None:
    """Ad‑hoc migration to add new optional columns when missing.
    - SQLite: use PRAGMA to inspect and ALTER TABLE without IF NOT EXISTS.
    - Postgres/MySQL: use ALTER TABLE ... ADD COLUMN IF NOT EXISTS (best effort).
    Idempotent and transactional where supported.
    """
    try:
        with engine.begin() as conn:
            dialect = engine.dialect.name
            if dialect == 'sqlite':
                cols = set()
                for row in conn.exec_driver_sql("PRAGMA table_info('fax_jobs')"):
                    cols.add(row[1])
                if "pdf_token" not in cols:
                    conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN pdf_token VARCHAR(128)")
                if "pdf_token_expires_at" not in cols:
                    conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN pdf_token_expires_at DATETIME")
                if "outbound_backend" not in cols:
                    conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN outbound_backend VARCHAR(20)")
                    conn.exec_driver_sql("UPDATE fax_jobs SET outbound_backend = backend WHERE outbound_backend IS NULL")

                inb_cols = set()
                for row in conn.exec_driver_sql("PRAGMA table_info('inbound_faxes')"):
                    inb_cols.add(row[1])
                if "inbound_backend" not in inb_cols:
                    conn.exec_driver_sql("ALTER TABLE inbound_faxes ADD COLUMN inbound_backend VARCHAR(20)")
                    conn.exec_driver_sql("UPDATE inbound_faxes SET inbound_backend = backend WHERE inbound_backend IS NULL")
            else:
                # Postgres/MySQL: best-effort IF NOT EXISTS
                try:
                    conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS pdf_token VARCHAR(128)")
                except Exception:
                    pass
                try:
                    conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS pdf_token_expires_at TIMESTAMP")
                except Exception:
                    pass
                try:
                    conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN IF NOT EXISTS outbound_backend VARCHAR(20)")
                except Exception:
                    pass
                try:
                    conn.exec_driver_sql("UPDATE fax_jobs SET outbound_backend = backend WHERE outbound_backend IS NULL")
                except Exception:
                    pass
                try:
                    conn.exec_driver_sql("ALTER TABLE inbound_faxes ADD COLUMN IF NOT EXISTS inbound_backend VARCHAR(20)")
                except Exception:
                    pass
                try:
                    conn.exec_driver_sql("UPDATE inbound_faxes SET inbound_backend = backend WHERE inbound_backend IS NULL")
                except Exception:
                    pass
    except Exception:
        # Do not block startup on migration best-effort failures
        pass
