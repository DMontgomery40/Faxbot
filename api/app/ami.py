import asyncio
import base64
import contextlib
import logging
import re
from typing import Dict, List, Optional, Callable
from uuid import uuid4
from .config import settings


LOGIN_TIMEOUT_SECONDS = 10.0
ORIGINATE_RESPONSE_TIMEOUT_SECONDS = 10.0
AMI_MAX_LINE_BYTES = 1024


def _validate_headers(fields: Dict[str, str]):
    for key, value in fields.items():
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", key)
            or not isinstance(value, str)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Unsupported AMI header value")
        try:
            line_bytes = len(f"{key}: {value}\r\n".encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("Unsupported AMI header value") from None
        if line_bytes > AMI_MAX_LINE_BYTES:
            raise ValueError("AMI header exceeds the supported wire limit")


# RFC 6913 fax preference for the initial INVITE. Asterisk's AMI Variable
# parser removes bare double quotes and keeps backslash-escaped ones, so the
# escaped form arrives as the literal header value *;+sip.fax="t38".
FAX_PREFERENCE_VARIABLE = 'PJSIP_HEADER(add,Accept-Contact)=*;+sip.fax=\\"t38\\"'


def prepare_originate_fields(
    job_id: str,
    dest: str,
    tiff_path: str,
    *,
    caller_id: str,
    header: str = "",
    attempt_id: Optional[str] = None,
    station_id: Optional[str] = None,
    dial: Optional[str] = None,
    fax_preference: bool = False,
) -> Dict[str, str]:
    """Prepare one direct PJSIP call before a durable marker or any I/O.

    Spaces are literal path characters. Dialplan/variable separators and
    expansion/quoting syntax in paths are refused. Captured UTF-8 metadata is
    base64 encoded so commas and expansion syntax remain literal data.

    ``dial`` is the carrier-formatted Request-URI user (defaults to ``dest``);
    ``station_id`` is the fax station identifier (defaults to ``caller_id``).
    ``fax_preference`` adds the RFC 6913 Accept-Contact header to this call's
    initial INVITE: a property of the route, never a reason to call again.
    Async Originate ignores PreDialGoSub in Asterisk 22, so the header is set
    as an Originate variable, which Asterisk applies to the new channel before
    the INVITE is sent.
    """
    for identity in (job_id,) if attempt_id is None else (job_id, attempt_id):
        if not isinstance(identity, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", identity
        ):
            raise ValueError("Unsupported AMI submission identity")
    if not isinstance(dest, str) or not re.fullmatch(r"\+?[0-9]+", dest):
        raise ValueError("Unsupported AMI destination")
    if dial is None:
        dial = dest
    elif not isinstance(dial, str) or not re.fullmatch(r"(?:[0-9]{4,16}\*)?\+?[0-9]{3,20}", dial):
        raise ValueError("Unsupported AMI destination")
    if station_id is None:
        station_id = caller_id
    if not isinstance(fax_preference, bool):
        raise ValueError("Unsupported AMI fax preference")
    if not isinstance(tiff_path, str) or not re.fullmatch(
        r"[A-Za-z0-9_./ -]+", tiff_path
    ):
        raise ValueError("Unsupported AMI artifact path syntax")
    _validate_headers({"CallerID": caller_id})
    metadata = {}
    for key, value in (("FAXHEADER64", header), ("FAXSTATION64", station_id)):
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("Unsupported AMI fax metadata")
        try:
            metadata[key] = base64.b64encode(value.encode("utf-8")).decode("ascii")
        except UnicodeEncodeError:
            raise ValueError("Unsupported AMI fax metadata") from None
    variables = {"JOBID": job_id, "FAXFILE": tiff_path, **metadata}
    if attempt_id is not None:
        variables["FAXATTEMPT"] = attempt_id
    assignments = [f"{key}={value}" for key, value in variables.items()]
    if fax_preference:
        assignments.append(FAX_PREFERENCE_VARIABLE)
    fields = {
        "Action": "Originate",
        "ActionID": (
            f"faxbot:{job_id}:{attempt_id}" if attempt_id is not None else str(uuid4())
        ),
        "Channel": f"PJSIP/{dial}@trunk-endpoint",
        "Context": "faxbot-send",
        "Exten": "s",
        "Priority": "1",
        "Async": "true",
        "Variable": ",".join(assignments),
        "CallerID": caller_id,
    }
    _validate_headers(fields)
    return fields


def originate_fields_for(values, job_id, dest, tiff_path, *, attempt_id=None):
    """The exact Originate fields for these settings; preflight and submission share it.

    With a configured SIP trunk the call carries the carrier-authorized caller
    ID, the carrier's number format and the optional fax preference; refused
    (ValueError naming fields only) when the trunk cannot place calls. Without
    one, the original station-ID behavior is unchanged.
    """
    from . import sip_trunk
    if not sip_trunk.configured(values):
        return prepare_originate_fields(job_id, dest, tiff_path, caller_id=values.fax_station_id,
                                        header=values.fax_header, attempt_id=attempt_id)
    trunk = sip_trunk.effective_trunk(values, for_calls=True)
    return prepare_originate_fields(
        job_id, dest, tiff_path, caller_id=trunk.caller_id, header=values.fax_header,
        attempt_id=attempt_id, station_id=values.fax_station_id,
        dial=sip_trunk.dial_number(trunk, dest), fax_preference=trunk.fax_preference)


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
        # Several owners observe the same events (delivery state and call
        # records); each listener runs independently of the others.
        self._listeners: Dict[str, List[Callable[[Dict[str, str]], None]]] = {}
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
            self._emit("OriginateResponse", msg)
        elif (
            event == "userevent" and fields.get("userevent", "").lower() == "faxresult"
        ):
            self._emit("FaxResult", msg)

    def _emit(self, name: str, msg: Dict[str, str]):
        for cb in list(self._listeners.get(name, ())):
            try:
                cb(msg)
            except Exception:
                logging.getLogger(__name__).warning("AMI event listener failed")

    def _listen(self, name: str, cb: Callable[[Dict[str, str]], None]):
        listeners = self._listeners.setdefault(name, [])
        if cb not in listeners:
            listeners.append(cb)

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
        """Await acceptance of one Originate action; acceptance is not delivery.

        Submission listeners hear about the call after validation and before
        the action is written, so an unacknowledged call still leaves a record.
        """
        fields = originate_fields_for(settings, job_id, dest, tiff_path, attempt_id=attempt_id)
        self._emit("Submission", {
            "JobID": job_id, "AttemptID": attempt_id or "", "Called": dest,
            "CallerID": fields["CallerID"], "Preset": settings.sip_trunk_preset or "",
            "FaxPreference": "yes" if FAX_PREFERENCE_VARIABLE in fields["Variable"] else "no",
        })
        await self._send_action(fields)

    def on_originate_response(self, cb: Callable[[Dict[str, str]], None]):
        self._listen("OriginateResponse", cb)

    def on_fax_result(self, cb: Callable[[Dict[str, str]], None]):
        self._listen("FaxResult", cb)

    def on_submission(self, cb: Callable[[Dict[str, str]], None]):
        self._listen("Submission", cb)


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
