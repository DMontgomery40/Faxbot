import logging
import json
from typing import Any, Dict, Optional, List
from logging.handlers import SysLogHandler
from collections import deque
from datetime import datetime


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
    handler: logging.Handler
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
    payload: Dict[str, Any] = {"event": event, "ts": datetime.utcnow().isoformat()}
    # Mask numbers for PHI safety on common keys
    masked: Dict[str, Any] = {}
    for k, v in fields.items():
        if k in {"to", "to_number", "from", "from_number", "fr"} and isinstance(v, str):
            masked[k] = mask_number(v)
        else:
            masked[k] = v
    payload.update(masked)
    try:
        _ring_append(payload)
    except Exception:
        pass
    if logger.disabled:
        return
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


# ===== In-process ring buffer for recent audit events =====
_RING_MAX = 1000
_ring = deque(maxlen=_RING_MAX)


def _ring_append(payload: Dict[str, Any]) -> None:
    _ring.append(payload)


def query_recent_logs(q: Optional[str] = None, event: Optional[str] = None, since: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    """Return recent audit events matching simple filters.
    - q: case-insensitive substring search on JSON string
    - event: exact match on event field
    - since: ISO8601 timestamp; filters out older entries
    - limit: max number of records (newest first)
    """
    results: List[Dict[str, Any]] = []
    q_norm = (q or "").strip().lower()
    since_dt: Optional[datetime] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except Exception:
            since_dt = None
    for item in reversed(list(_ring)):
        if event and str(item.get("event")) != event:
            continue
        if since_dt:
            try:
                ts = item.get("ts")
                if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")) < since_dt:
                    continue
            except Exception:
                pass
        if q_norm:
            s = json.dumps(item, separators=(",", ":")).lower()
            if q_norm not in s:
                continue
        results.append(item)
        if len(results) >= max(1, min(limit, _RING_MAX)):
            break
    return results
