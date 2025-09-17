import os
import shlex
import shutil
import subprocess
from typing import Optional

from .config import settings


def fs_cli_available() -> bool:
    return shutil.which("fs_cli") is not None


def originate_txfax(to_number: str, tiff_path: str, job_id: str) -> str:
    """Originate a fax call via FreeSWITCH &txfax using fs_cli.
    Returns fs_cli output (may include UUID). Raises on failure.
    """
    if not fs_cli_available():
        raise RuntimeError("fs_cli not found; install FreeSWITCH client tools or use ESL")
    # Build originate arguments
    vars_list = [
        f"origination_caller_id_number={settings.fs_caller_id_number}",
        f"faxbot_job_id={job_id}",
    ]
    if settings.fs_t38_enable:
        vars_list += ["fax_enable_t38_request=true", "fax_enable_t38=true"]
    var_str = ",".join(vars_list)
    dest = f"sofia/gateway/{settings.fs_gateway_name}/{to_number}"
    # Use bgapi originate to avoid blocking
    cmd = f"bgapi originate {{{var_str}}}{dest} &txfax({tiff_path})"
    out = subprocess.check_output(["fs_cli", "-x", cmd], text=True)
    return out.strip()

