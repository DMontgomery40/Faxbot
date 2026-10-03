from sqlalchemy import Column, String, DateTime, Integer, Text, UniqueConstraint  # type: ignore
from sqlalchemy.orm import declarative_base, sessionmaker  # type: ignore
from datetime import datetime
from .config import settings
from .schema import create_database_engine, upgrade_schema
from sqlalchemy.engine import make_url
from threading import RLock


engine = create_database_engine(settings.database_url)
_binding_lock = RLock()
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
    backend = Column(String(255).with_variant(String(20), "sqlite"), nullable=False, default="sip")  # "sip" or cloud provider key
    outbound_backend = Column(String(255).with_variant(String(20), "sqlite"), nullable=True)  # effective outbound backend (hybrid)
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
    backend = Column(String(255).with_variant(String(20), "sqlite"), nullable=False)
    inbound_backend = Column(String(255).with_variant(String(20), "sqlite"), nullable=True)  # effective inbound backend (hybrid)
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


def init_db():
    """Publish a new session binding only after its versioned upgrade succeeds."""
    global engine
    with _binding_lock:
        target = make_url(settings.database_url)
        if engine.url == target:
            upgrade_schema(engine)
            return
        candidate = create_database_engine(target)
        try:
            upgrade_schema(candidate)
        except BaseException:
            candidate.dispose()
            raise
        previous = engine
        SessionLocal.configure(bind=candidate)
        engine = candidate
        previous.dispose()
