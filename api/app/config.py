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
    phaxio_status_callback_url: str = Field(default_factory=lambda: os.getenv("PHAXIO_STATUS_CALLBACK_URL", ""))
    phaxio_verify_signature: bool = Field(default_factory=lambda: os.getenv("PHAXIO_VERIFY_SIGNATURE", "true").lower() in {"1", "true", "yes"})

    # Public API URL (needed for cloud backend to fetch PDFs, e.g., Phaxio)
    public_api_url: str = Field(default_factory=lambda: os.getenv("PUBLIC_API_URL", "http://localhost:8080"))

    # Fax presentation
    fax_header: str = Field(default_factory=lambda: os.getenv("FAX_HEADER", "Faxbot"))
    fax_station_id: str = Field(default_factory=lambda: os.getenv("FAX_LOCAL_STATION_ID", "+10000000000"))

    # DB
    database_url: str = Field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./faxbot.db"))

    # Security
    pdf_token_ttl_minutes: int = Field(default_factory=lambda: int(os.getenv("PDF_TOKEN_TTL_MINUTES", "60")))


settings = Settings()
