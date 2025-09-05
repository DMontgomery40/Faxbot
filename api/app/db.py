from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from .config import settings


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class FaxJob(Base):
    __tablename__ = "fax_jobs"
    id = Column(String(40), primary_key=True, index=True)
    to_number = Column(String(64), index=True, nullable=False)
    file_name = Column(String(255), nullable=False)
    tiff_path = Column(String(512), nullable=False)
    status = Column(String(32), index=True, nullable=False, default="queued")
    error = Column(Text, nullable=True)
    pages = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


def init_db():
    Base.metadata.create_all(engine)

