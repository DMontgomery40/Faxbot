import asyncio
import contextlib
import logging
from typing import Dict, Optional, Callable
from .config import settings


LOGIN_TIMEOUT_SECONDS = 10.0


async def _login(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, username: str, password: str):
    """Authenticate before advertising a connection; ignore the AMI greeting."""
    writer.write((f"Action: Login\r\nUsername: {username}\r\nSecret: {password}\r\n\r\n").encode())
    await writer.drain()

    async def response():
        fields: Dict[str, str] = {}
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("AMI closed before login response")
            line = line.decode().rstrip("\r\n")
            if not line:
                if "Response" in fields:
                    if fields["Response"].lower() != "success":
                        raise ConnectionError("AMI login rejected")
                    return
                fields = {}
            elif ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip()] = value.strip()

    async with asyncio.timeout(LOGIN_TIMEOUT_SECONDS):
        await response()


class AMIClient:
    def __init__(self):
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._connected = asyncio.Event()
        self._listeners: Dict[str, Callable[[Dict[str, str]], None]] = {}
        self._conn_lock = asyncio.Lock()
        self._connection_task: Optional[asyncio.Task] = None

    async def connect(self):
        async with self._conn_lock:
            if self._connection_task is None or self._connection_task.done():
                self._connection_task = asyncio.create_task(self._connection_loop(), name="faxbot-ami-supervisor")
            supervisor = self._connection_task
        connected = asyncio.create_task(self._connected.wait())
        try:
            done, _ = await asyncio.wait((supervisor, connected), return_when=asyncio.FIRST_COMPLETED)
            if supervisor in done:
                await supervisor
                raise ConnectionError("AMI connection closed")
        finally:
            connected.cancel()
            await asyncio.gather(connected, return_exceptions=True)

    async def _connection_loop(self):
        delay = 1.0
        while True:
            try:
                async with asyncio.timeout(LOGIN_TIMEOUT_SECONDS):
                    self.reader, self.writer = await asyncio.open_connection(settings.ami_host, settings.ami_port)
                await _login(self.reader, self.writer, settings.ami_username, settings.ami_password)
                self._connected.set()
                delay = 1.0
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning("AMI connection unavailable; retrying")
            finally:
                self._connected.clear()
                await self._close_writer()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _close_writer(self):
        writer, self.writer = self.writer, None
        self.reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def close(self):
        """Stop reads/reconnects and release the current socket before returning."""
        supervisor, self._connection_task = self._connection_task, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        self._connected.clear()
        await self._close_writer()

    async def _read_loop(self):
        buf: Dict[str, str] = {}
        assert self.reader is not None
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
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=LOGIN_TIMEOUT_SECONDS)
        await _login(reader, writer, username, password)
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
