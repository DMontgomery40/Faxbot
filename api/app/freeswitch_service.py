import re
import shutil
import subprocess
from typing import Optional
from uuid import UUID

from .config import settings


CALLER_ID_MISSING = 'Enter the caller ID number your carrier gave you for FreeSWITCH.'


class CallerIdMissing(ValueError):
    """FreeSWITCH never places a call with a made-up caller ID."""

    def __init__(self) -> None:
        super().__init__(CALLER_ID_MISSING)


def fs_cli_available() -> bool:
    return shutil.which("fs_cli") is not None


def build_originate_command(
    to_number: str,
    tiff_path: str,
    job_id: str,
    *,
    gateway_name: str,
    caller_id_number: str,
    t38_enable: bool,
    attempt_id: Optional[str] = None,
) -> str:
    """Prepare one command without I/O; refuse unsupported quoting/delimiters.

    Only plain POSIX paths without whitespace or FreeSWITCH expansion syntax
    are supported. Do not apply shell quoting to FreeSWITCH's command grammar.
    """
    if caller_id_number in ("", None):
        raise CallerIdMissing()
    for number in (to_number, caller_id_number):
        if not isinstance(number, str) or not re.fullmatch(r"\+?[0-9]+", number):
            raise ValueError("Unsupported FreeSWITCH phone number")
    for identity in (job_id,) if attempt_id is None else (job_id, attempt_id):
        if not isinstance(identity, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identity
        ):
            raise ValueError("Unsupported FreeSWITCH submission identity")
    if not isinstance(gateway_name, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", gateway_name
    ):
        raise ValueError("Unsupported FreeSWITCH gateway name")
    if not isinstance(tiff_path, str) or not re.fullmatch(
        r"[A-Za-z0-9_./-]+", tiff_path
    ):
        raise ValueError("Unsupported FreeSWITCH artifact path syntax")
    vars_list = [
        f"origination_caller_id_number={caller_id_number}",
        f"faxbot_job_id={job_id}",
    ]
    if attempt_id is not None:
        vars_list.append(f"faxbot_attempt_id={attempt_id}")
    if t38_enable:
        vars_list += ["fax_enable_t38_request=true", "fax_enable_t38=true"]
    var_str = ",".join(vars_list)
    dest = f"sofia/gateway/{gateway_name}/{to_number}"
    return f"bgapi originate {{{var_str}}}{dest} &txfax({tiff_path})"


def originate_txfax(
    to_number: str, tiff_path: str, job_id: str, *, attempt_id: Optional[str] = None
) -> str:
    """Return the bgapi acceptance Job-UUID, never a fax-delivery result."""
    cmd = build_originate_command(
        to_number,
        tiff_path,
        job_id,
        gateway_name=settings.fs_gateway_name,
        caller_id_number=settings.fs_caller_id_number,
        t38_enable=settings.fs_t38_enable,
        attempt_id=attempt_id,
    )
    if not fs_cli_available():
        raise RuntimeError("FreeSWITCH client unavailable")
    try:
        out = subprocess.check_output(
            ["fs_cli", "-x", cmd], text=True, timeout=30, stderr=subprocess.PIPE
        )
    except Exception:
        raise RuntimeError("FreeSWITCH submission was not acknowledged") from None
    match = (
        re.fullmatch(
            r"\+OK Job-UUID: ([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
            out.strip(),
        )
        if isinstance(out, str)
        else None
    )
    if match is None:
        raise RuntimeError("FreeSWITCH submission was not acknowledged")
    return str(UUID(match.group(1)))
