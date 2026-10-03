import asyncio
import contextlib
import logging
import re
from typing import Dict, Optional, Callable
from uuid import uuid4
from .config import settings


LOGIN_TIMEOUT_SECONDS = 10.0
ORIGINATE_RESPONSE_TIMEOUT_SECONDS = 10.0


def _validate_headers(fields: Dict[str, str]):
    for key, value in fields.items():
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key)
            or not isinstance(value, str)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Unsupported AMI header value")


def prepare_originate_fields(
    job_id: str,
    dest: str,
    tiff_path: str,
    *,
    caller_id: str,
    attempt_id: Optional[str] = None,
) -> Dict[str, str]:
    """Validate native arguments before a durable submission marker or any I/O.

    Spaces are literal path characters. Dialplan/variable separators and
    expansion/quoting syntax are unsupported rather than silently escaped.
    """
    for identity in (job_id,) if attempt_id is None else (job_id, attempt_id):
        if not isinstance(identity, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identity
        ):
            raise ValueError("Unsupported AMI submission identity")
    if not isinstance(dest, str) or not re.fullmatch(r"\+?[0-9]+", dest):
        raise ValueError("Unsupported AMI destination")
    if not isinstance(tiff_path, str) or not re.fullmatch(
        r"[A-Za-z0-9_./ -]+", tiff_path
    ):
        raise ValueError("Unsupported AMI artifact path syntax")
    variables = {"JOBID": job_id, "DEST": dest, "FAXFILE": tiff_path}
    if attempt_id is not None:
        variables["FAXATTEMPT"] = attempt_id
    fields = {
        "Action": "Originate",
        "ActionID": (
            f"faxbot:{job_id}:{attempt_id}" if attempt_id is not None else str(uuid4())
        ),
        "Channel": "Local/s@faxout",
        "Context": "faxout",
        "Exten": "s",
        "Priority": "1",
        "Async": "true",
        "Variable": ",".join(f"{key}={value}" for key, value in variables.items()),
        "CallerID": caller_id,
    }
    _validate_headers(fields)
    return fields


async def _login(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    username: str,
    password: str,
):
    """Authenticate before advertising a connection; ignore the AMI greeting."""
    _validate_headers({"Username": username, "Secret": password})
    writer.write(
        (
            f"Action: Login\r\nUsername: {username}\r\nSecret: {password}\r\n\r\n"
        ).encode()
    )
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
        self._pending_actions: Dict[str, asyncio.Future] = {}

    async def connect(self):
        async with self._conn_lock:
            if self._connection_task is None or self._connection_task.done():
                self._connection_task = asyncio.create_task(
                    self._connection_loop(), name="faxbot-ami-supervisor"
                )
            supervisor = self._connection_task
        connected = asyncio.create_task(self._connected.wait())
        try:
            done, _ = await asyncio.wait(
                (supervisor, connected), return_when=asyncio.FIRST_COMPLETED
            )
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
                    self.reader, self.writer = await asyncio.open_connection(
                        settings.ami_host, settings.ami_port
                    )
                await _login(
                    self.reader,
                    self.writer,
                    settings.ami_username,
                    settings.ami_password,
                )
                self._connected.set()
                delay = 1.0
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning(
                    "AMI connection unavailable; retrying"
                )
            finally:
                self._connected.clear()
                await self._close_writer()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _close_writer(self):
        self._fail_pending_actions()
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
        finally:
            self._connected.clear()
            self._fail_pending_actions()

    def _fail_pending_actions(self):
        pending, self._pending_actions = self._pending_actions, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(
                    ConnectionError("AMI connection closed before acknowledgement")
                )

    def _dispatch(self, msg: Dict[str, str]):
        fields = {key.lower(): value for key, value in msg.items()}
        if "event" not in fields and "response" in fields:
            future = self._pending_actions.get(fields.get("actionid"))
            if future is not None and not future.done():
                future.set_result(fields)
            return
        event = fields.get("event", "").lower()
        if event == "originateresponse":
            cb = self._listeners.get("OriginateResponse")
            if cb:
                cb(msg)
        elif (
            event == "userevent" and fields.get("userevent", "").lower() == "faxresult"
        ):
            cb = self._listeners.get("FaxResult")
            if cb:
                cb(msg)

    async def _send_action(self, fields: Dict[str, str]):
        _validate_headers(fields)
        action_id = fields["ActionID"]
        raw = "".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n"
        future = None
        try:
            async with asyncio.timeout(ORIGINATE_RESPONSE_TIMEOUT_SECONDS):
                if not self._connected.is_set():
                    await self.connect()
                writer = self.writer
                if writer is None:
                    raise ConnectionError("AMI connection unavailable")
                if action_id in self._pending_actions:
                    raise ConnectionError("AMI action already pending")
                future = asyncio.get_running_loop().create_future()
                self._pending_actions[action_id] = future
                writer.write(raw.encode())
                await writer.drain()
                response = await future
                if response["response"].lower() != "success":
                    raise ConnectionError("AMI originate rejected")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise TimeoutError("AMI acknowledgement timed out") from None
        except Exception:
            raise ConnectionError("AMI submission was not acknowledged") from None
        finally:
            if future is not None:
                if self._pending_actions.get(action_id) is future:
                    self._pending_actions.pop(action_id)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    async def originate_sendfax(
        self,
        job_id: str,
        dest: str,
        tiff_path: str,
        *,
        attempt_id: Optional[str] = None,
    ):
        """Await acceptance of one Originate action; acceptance is not delivery."""
        fields = prepare_originate_fields(
            job_id,
            dest,
            tiff_path,
            caller_id=settings.fax_station_id,
            attempt_id=attempt_id,
        )
        await self._send_action(fields)

    def on_originate_response(self, cb: Callable[[Dict[str, str]], None]):
        self._listeners["OriginateResponse"] = cb

    def on_fax_result(self, cb: Callable[[Dict[str, str]], None]):
        self._listeners["FaxResult"] = cb


ami_client = AMIClient()


async def test_ami_connection(
    host: str, port: int, username: str, password: str
) -> bool:
    """One-off AMI TCP connect + login probe. Does not mutate global client."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=LOGIN_TIMEOUT_SECONDS
        )
        await _login(reader, writer, username, password)
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
