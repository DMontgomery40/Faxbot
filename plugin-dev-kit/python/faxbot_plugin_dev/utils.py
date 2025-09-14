import re
import hashlib
import secrets
from datetime import datetime


def mask_phone_number(phone: str) -> str:
    if not phone:
        return "[EMPTY]"
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return "***"
    return f"***-***-{digits[-4:]}"


def hash_document(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def redact_text(text: str) -> str:
    if not text:
        return text
    out = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "***-**-****", text)
    out = re.sub(r"\d{10,}", "***", out)
    return out


def timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

