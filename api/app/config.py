import os
from pydantic import BaseModel, Field


class Settings(BaseModel):
    # App
    fax_data_dir: str = Field(default_factory=lambda: os.getenv("FAX_DATA_DIR", "./faxdata"))
    max_file_size_mb: int = Field(default_factory=lambda: int(os.getenv("MAX_FILE_SIZE_MB", "10")))
    fax_disabled: bool = Field(default_factory=lambda: os.getenv("FAX_DISABLED", "false").lower() in {"1", "true", "yes"})
    api_key: str = Field(default_factory=lambda: os.getenv("API_KEY", ""))

    # Fax Backend Selection
    fax_backend: str = Field(default_factory=lambda: os.getenv("FAX_BACKEND", "sip").lower())  # "sip" or a cloud provider key like "phaxio"

    # Asterisk AMI (for SIP backend)
    ami_host: str = Field(default_factory=lambda: os.getenv("ASTERISK_AMI_HOST", "asterisk"))
    ami_port: int = Field(default_factory=lambda: int(os.getenv("ASTERISK_AMI_PORT", "5038")))
    ami_username: str = Field(default_factory=lambda: os.getenv("ASTERISK_AMI_USERNAME", "api"))
    ami_password: str = Field(default_factory=lambda: os.getenv("ASTERISK_AMI_PASSWORD", "changeme"))

    # Phaxio Configuration (for cloud backend)
    phaxio_api_key: str = Field(default_factory=lambda: os.getenv("PHAXIO_API_KEY", ""))
    phaxio_api_secret: str = Field(default_factory=lambda: os.getenv("PHAXIO_API_SECRET", ""))
    # Support both PHAXIO_STATUS_CALLBACK_URL and PHAXIO_CALLBACK_URL per AGENTS.md
    phaxio_status_callback_url: str = Field(
        default_factory=lambda: os.getenv("PHAXIO_STATUS_CALLBACK_URL", os.getenv("PHAXIO_CALLBACK_URL", ""))
    )
    # Default off for dev/tests; enable in production via env
    phaxio_verify_signature: bool = Field(default_factory=lambda: os.getenv("PHAXIO_VERIFY_SIGNATURE", "false").lower() in {"1", "true", "yes"})

    # Public API URL (needed for cloud backend to fetch PDFs, e.g., Phaxio)
    public_api_url: str = Field(default_factory=lambda: os.getenv("PUBLIC_API_URL", "http://localhost:8080"))

    # Fax presentation
    fax_header: str = Field(default_factory=lambda: os.getenv("FAX_HEADER", "Faxbot"))
    fax_station_id: str = Field(default_factory=lambda: os.getenv("FAX_LOCAL_STATION_ID", "+10000000000"))

    # DB
    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./faxbot.db"))

    # Security
    pdf_token_ttl_minutes: int = Field(default_factory=lambda: int(os.getenv("PDF_TOKEN_TTL_MINUTES", "60")))
    enforce_public_https: bool = Field(default_factory=lambda: os.getenv("ENFORCE_PUBLIC_HTTPS", "false").lower() in {"1", "true", "yes"})

    # Retention / cleanup
    artifact_ttl_days: int = Field(default_factory=lambda: int(os.getenv("ARTIFACT_TTL_DAYS", "0")))  # 0=disabled
    cleanup_interval_minutes: int = Field(default_factory=lambda: int(os.getenv("CLEANUP_INTERVAL_MINUTES", "1440")))

    # Audit logging
    audit_log_enabled: bool = Field(default_factory=lambda: os.getenv("AUDIT_LOG_ENABLED", "false").lower() in {"1", "true", "yes"})
    audit_log_format: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_FORMAT", "json"))
    audit_log_file: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_FILE", ""))
    audit_log_syslog: bool = Field(default_factory=lambda: os.getenv("AUDIT_LOG_SYSLOG", "false").lower() in {"1", "true", "yes"})
    audit_log_syslog_address: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_SYSLOG_ADDRESS", "/dev/log"))


settings = Settings()


def reload_settings() -> None:
    """Reload settings from current environment into the existing instance.
    Keeps references stable across modules that imported `settings`.
    """
    new = Settings()
    for name in new.model_fields.keys():  # type: ignore[attr-defined]
        setattr(settings, name, getattr(new, name))
