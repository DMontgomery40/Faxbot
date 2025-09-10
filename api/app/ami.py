import asyncio
import contextlib
from typing import Dict, Optional, Callable
from .config import settings


class AMIClient:
    def __init__(self):
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._connected = asyncio.Event()
        self._listeners: Dict[str, Callable[[Dict[str, str]], None]] = {}
        self._conn_lock = asyncio.Lock()

    async def connect(self):
        async with self._conn_lock:
            if self._connected.is_set():
                return
            delay = 1.0
            while not self._connected.is_set():
                try:
                    self.reader, self.writer = await asyncio.open_connection(settings.ami_host, settings.ami_port)
                    await self._login()
                    self._connected.set()
                    asyncio.create_task(self._read_loop())
                    return
                except Exception:
                    # Exponential backoff with cap
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30.0)

    async def _login(self):
        await self._send_action({
            "Action": "Login",
            "Username": settings.ami_username,
            "Secret": settings.ami_password,
        })

    async def _read_loop(self):
        buf: Dict[str, str] = {}
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    raise ConnectionError("AMI connection closed")
                line = line.decode().rstrip("\r\n")
                if line == "":
                    if buf:
                        self._dispatch(buf)
                        buf = {}
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    buf[k.strip()] = v.strip()
        except Exception:
            self._connected.clear()
            # Reconnect in background; avoid raising into caller
            asyncio.create_task(self.connect())

    def _dispatch(self, msg: Dict[str, str]):
        event = msg.get("Event")
        if event == "UserEvent" and msg.get("UserEvent") == "FaxResult":
            cb = self._listeners.get("FaxResult")
            if cb:
                cb(msg)

    async def _send_action(self, fields: Dict[str, str]):
        # Ensure connected
        if not self._connected.is_set():
            await self.connect()
        raw = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        assert self.writer is not None
        self.writer.write(raw.encode())
        await self.writer.drain()

    async def originate_sendfax(self, job_id: str, dest: str, tiff_path: str):
        # Originate to Local channel which enters faxout context
        variables = {
            "JOBID": job_id,
            "DEST": dest,
            "FAXFILE": tiff_path,
        }
        var_lines = ",".join(f"{k}={v}" for k, v in variables.items())
        await self._send_action({
            "Action": "Originate",
            "Channel": "Local/s@faxout",
            "Context": "faxout",
            "Exten": "s",
            "Priority": "1",
            "Async": "true",
            "Variable": var_lines,
            "CallerID": settings.fax_station_id,
        })

    def on_fax_result(self, cb: Callable[[Dict[str, str]], None]):
        self._listeners["FaxResult"] = cb


ami_client = AMIClient()


async def test_ami_connection(host: str, port: int, username: str, password: str) -> bool:
    """One-off AMI TCP connect + login probe. Does not mutate global client."""
    try:
        reader, writer = await asyncio.open_connection(host, port)
        login = (
            f"Action: Login\r\nUsername: {username}\r\nSecret: {password}\r\n\r\n"
        )
        writer.write(login.encode())
        await writer.drain()
        # Read a line to confirm response (best-effort)
        try:
            await asyncio.wait_for(reader.readline(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except Exception:
        return False
