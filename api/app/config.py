import os
from pydantic import BaseModel


class Settings(BaseModel):
    # App
    fax_data_dir: str = os.getenv("FAX_DATA_DIR", "/faxdata")
    max_file_size_mb: int = int(os.getenv("MAX_FILE_SIZE_MB", "10"))
    fax_disabled: bool = os.getenv("FAX_DISABLED", "false").lower() in {"1", "true", "yes"}
    api_key: str = os.getenv("API_KEY", "")

    # Asterisk AMI
    ami_host: str = os.getenv("ASTERISK_AMI_HOST", "asterisk")
    ami_port: int = int(os.getenv("ASTERISK_AMI_PORT", "5038"))
    ami_username: str = os.getenv("ASTERISK_AMI_USERNAME", "api")
    ami_password: str = os.getenv("ASTERISK_AMI_PASSWORD", "changeme")

    # Fax presentation
    fax_header: str = os.getenv("FAX_HEADER", "OpenFax")
    fax_station_id: str = os.getenv("FAX_LOCAL_STATION_ID", "+10000000000")

    # DB
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./openfax.db")


settings = Settings()
