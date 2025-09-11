import os
from pydantic import BaseModel, Field


class Settings(BaseModel):
    # App
    fax_data_dir: str = Field(default_factory=lambda: os.getenv("FAX_DATA_DIR", "./faxdata"))
    max_file_size_mb: int = Field(default_factory=lambda: int(os.getenv("MAX_FILE_SIZE_MB", "10")))
    fax_disabled: bool = Field(default_factory=lambda: os.getenv("FAX_DISABLED", "false").lower() in {"1", "true", "yes"})
    api_key: str = Field(default_factory=lambda: os.getenv("API_KEY", ""))
    # Require API key on requests regardless of env API_KEY. Useful for HIPAA prod.
    require_api_key: bool = Field(default_factory=lambda: os.getenv("REQUIRE_API_KEY", "false").lower() in {"1", "true", "yes"})

    # Fax Backend Selection
    fax_backend: str = Field(default_factory=lambda: os.getenv("FAX_BACKEND", "phaxio").lower())  # default to cloud; require explicit 'sip' for telephony

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
    # Verify Phaxio webhook signatures (HMAC-SHA256) — default on; allow explicit dev opt-out
    phaxio_verify_signature: bool = Field(default_factory=lambda: os.getenv("PHAXIO_VERIFY_SIGNATURE", "true").lower() in {"1", "true", "yes"})

    # Public API URL (needed for cloud backend to fetch PDFs, e.g., Phaxio)
    public_api_url: str = Field(default_factory=lambda: os.getenv("PUBLIC_API_URL", "http://localhost:8080"))

    # Sinch Fax (Phaxio by Sinch) — direct upload flow
    sinch_project_id: str = Field(default_factory=lambda: os.getenv("SINCH_PROJECT_ID", ""))
    sinch_api_key: str = Field(default_factory=lambda: os.getenv("SINCH_API_KEY", os.getenv("PHAXIO_API_KEY", "")))
    sinch_api_secret: str = Field(default_factory=lambda: os.getenv("SINCH_API_SECRET", os.getenv("PHAXIO_API_SECRET", "")))

    # Fax presentation
    fax_header: str = Field(default_factory=lambda: os.getenv("FAX_HEADER", "Faxbot"))
    fax_station_id: str = Field(default_factory=lambda: os.getenv("FAX_LOCAL_STATION_ID", "+10000000000"))

    # DB
    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./faxbot.db"))

    # Security
    pdf_token_ttl_minutes: int = Field(default_factory=lambda: int(os.getenv("PDF_TOKEN_TTL_MINUTES", "60")))
    enforce_public_https: bool = Field(default_factory=lambda: os.getenv("ENFORCE_PUBLIC_HTTPS", "true").lower() in {"1", "true", "yes"})

    # Retention / cleanup
    artifact_ttl_days: int = Field(default_factory=lambda: int(os.getenv("ARTIFACT_TTL_DAYS", "0")))  # 0=disabled
    cleanup_interval_minutes: int = Field(default_factory=lambda: int(os.getenv("CLEANUP_INTERVAL_MINUTES", "1440")))

    # Rate limiting (per key) — disabled by default; implemented in Phase 2
    max_requests_per_minute: int = Field(default_factory=lambda: int(os.getenv("MAX_REQUESTS_PER_MINUTE", "0")))

    # Audit logging
    audit_log_enabled: bool = Field(default_factory=lambda: os.getenv("AUDIT_LOG_ENABLED", "false").lower() in {"1", "true", "yes"})
    audit_log_format: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_FORMAT", "json"))
    audit_log_file: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_FILE", ""))
    audit_log_syslog: bool = Field(default_factory=lambda: os.getenv("AUDIT_LOG_SYSLOG", "false").lower() in {"1", "true", "yes"})
    audit_log_syslog_address: str = Field(default_factory=lambda: os.getenv("AUDIT_LOG_SYSLOG_ADDRESS", "/dev/log"))

    # Inbound receiving (Phase Receive)
    inbound_enabled: bool = Field(default_factory=lambda: os.getenv("INBOUND_ENABLED", "false").lower() in {"1", "true", "yes"})
    inbound_retention_days: int = Field(default_factory=lambda: int(os.getenv("INBOUND_RETENTION_DAYS", "30")))
    inbound_token_ttl_minutes: int = Field(default_factory=lambda: int(os.getenv("INBOUND_TOKEN_TTL_MINUTES", "60")))
    asterisk_inbound_secret: str = Field(default_factory=lambda: os.getenv("ASTERISK_INBOUND_SECRET", ""))
    phaxio_inbound_verify_signature: bool = Field(default_factory=lambda: os.getenv("PHAXIO_INBOUND_VERIFY_SIGNATURE", "true").lower() in {"1", "true", "yes"})
    sinch_inbound_verify_signature: bool = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_VERIFY_SIGNATURE", "true").lower() in {"1", "true", "yes"})
    sinch_inbound_basic_user: str = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_BASIC_USER", ""))
    sinch_inbound_basic_pass: str = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_BASIC_PASS", ""))
    sinch_inbound_hmac_secret: str = Field(default_factory=lambda: os.getenv("SINCH_INBOUND_HMAC_SECRET", ""))

    # Storage backend for inbound artifacts
    storage_backend: str = Field(default_factory=lambda: os.getenv("STORAGE_BACKEND", "local"))  # local | s3
    s3_bucket: str = Field(default_factory=lambda: os.getenv("S3_BUCKET", ""))
    s3_prefix: str = Field(default_factory=lambda: os.getenv("S3_PREFIX", "inbound/"))
    s3_region: str = Field(default_factory=lambda: os.getenv("S3_REGION", ""))
    s3_endpoint_url: str = Field(default_factory=lambda: os.getenv("S3_ENDPOINT_URL", ""))  # allow S3-compatible (MinIO)
    s3_kms_key_id: str = Field(default_factory=lambda: os.getenv("S3_KMS_KEY_ID", ""))

    # Inbound rate limits (per key)
    inbound_list_rpm: int = Field(default_factory=lambda: int(os.getenv("INBOUND_LIST_RPM", "30")))
    inbound_get_rpm: int = Field(default_factory=lambda: int(os.getenv("INBOUND_GET_RPM", "60")))

    # Admin console options
    admin_allow_restart: bool = Field(default_factory=lambda: os.getenv("ADMIN_ALLOW_RESTART", "false").lower() in {"1","true","yes"})


settings = Settings()


def reload_settings() -> None:
    """Reload settings from current environment into the existing instance.
    Keeps references stable across modules that imported `settings`.
    """
    new = Settings()
    for name in new.model_fields.keys():  # type: ignore[attr-defined]
        setattr(settings, name, getattr(new, name))
