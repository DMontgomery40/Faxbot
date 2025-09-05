from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text  # type: ignore
from sqlalchemy import text  # type: ignore
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
    provider_sid = Column(String(100), nullable=True)  # Cloud provider fax ID
    pdf_url = Column(String(512), nullable=True)  # Public URL for PDF (for cloud backend)
    pdf_token = Column(String(128), nullable=True)  # Secure token for PDF fetch
    pdf_token_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def init_db():
    Base.metadata.create_all(engine)
    _ensure_optional_columns()


def _ensure_optional_columns() -> None:
    """Ad‑hoc migration to add new optional columns when missing (SQLite)."""
    try:
        with engine.begin() as conn:
            # Inspect existing columns
            cols = set()
            for row in conn.exec_driver_sql("PRAGMA table_info('fax_jobs')"):
                # row: cid, name, type, notnull, dflt_value, pk
                cols.add(row[1])

            if "pdf_token" not in cols:
                conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN pdf_token VARCHAR(128)")
            if "pdf_token_expires_at" not in cols:
                conn.exec_driver_sql("ALTER TABLE fax_jobs ADD COLUMN pdf_token_expires_at DATETIME")
    except Exception:
        # Best effort; do not block startup if inspection fails on non-SQLite
        pass
