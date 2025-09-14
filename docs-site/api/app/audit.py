import logging
import json
from typing import Any, Dict, Optional
from logging.handlers import SysLogHandler


def init_audit_logger(
    enabled: bool,
    fmt: str = "json",
    filepath: Optional[str] = None,
    use_syslog: bool = False,
    syslog_address: Optional[str] = None,
) -> None:
    logger = logging.getLogger("audit")
    if not enabled:
        logger.disabled = True
        return
    logger.setLevel(logging.INFO)
    # Avoid duplicate handlers on reload
    if logger.handlers:
        return
    if filepath:
        handler = logging.FileHandler(filepath)
    elif use_syslog:
        address = syslog_address or "/dev/log"
        handler = SysLogHandler(address=address)
    else:
        handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)

    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def audit_event(event: str, **fields: Any) -> None:
    logger = logging.getLogger("audit")
    if logger.disabled:
        return
    payload: Dict[str, Any] = {"event": event}
    payload.update(fields)
    logger.info(payload)


def mask_number(num: Optional[str]) -> Optional[str]:
    if not num:
        return num
    digits = [c for c in num if c.isdigit()]
    if len(digits) <= 4:
        return "****"
    masked = "*" * (len(digits) - 4) + "".join(digits[-4:])
    return masked


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, dict):
            return json.dumps(record.msg, separators=(",", ":"))
        try:
            return json.dumps({"message": str(record.getMessage())})
        except Exception:
            return str(record.getMessage())

