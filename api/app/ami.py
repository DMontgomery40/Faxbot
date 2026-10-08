import asyncio
import base64
import contextlib
import logging
import re
import time
from typing import Dict, List, Optional, Callable
from uuid import uuid4
from .config import settings


LOGIN_TIMEOUT_SECONDS = 10.0
# How long a peer fax call's route check inside Asterisk may take (peer_route).
PEER_ROUTE_TIMEOUT_SECONDS = 5.0
PEER_ADDRESS = re.compile(r"[0-9A-Fa-f.:]{2,45}")
# A partner's peer fax call endpoint (direct/peer_call.py); never a trunk's, so neither path can reach the other.
PEER_ENDPOINT = re.compile(r"peer-[a-f0-9]{32}-endpoint")
ORIGINATE_RESPONSE_TIMEOUT_SECONDS = 10.0
STATUS_TIMEOUT_SECONDS = 5.0
AMI_MAX_LINE_BYTES = 1024
# Read-only status answers keep only these event fields. Everything else in a
# status reply (notably AuthDetail events, which carry the SIP password) is
# dropped as it is read and never stored, returned or logged.
STATUS_EVENT_FIELDS = {
    "outboundregistrationdetail": ("ObjectName", "Status", "ServerUri", "NextReg", "Transport"),
    "contactlist": ("ObjectName", "Status", "RoundtripUsec"),
    # PJSIPShowEndpoint: only the contact's status. Its EndpointDetail and AuthDetail events are dropped.
    "contactstatusdetail": ("URI", "Status", "RoundtripUsec"),
    # Counted before Faxbot restarts Asterisk, and listed under System → Developer → Scripts & checks.
    "coreshowchannel": ("Uniqueid", "Channel", "ChannelStateDesc", "CallerIDNum", "Exten", "Duration"),
    "faxsessionsentry": ("Channel", "Technology", "SessionType", "Operation", "State"),
    # The SSL Fax engine's IAX lines and whether each answers Asterisk's checks.
    "peerentry": ("ObjectName", "Status"),
    # Faxbot's own families in Asterisk's database: blocked callers and the calls turned away (inbound/screening.py).
    "dbgettreeresponse": ("Key", "Val"),
}
# One plain sentence for each state of Faxbot's connection to its fax engine
# (Asterisk). Readiness, the dashboard, diagnostics, trunk status and a refused
# send all show the same sentence.
ENGINE_LOGIN_REJECTED = "Faxbot can't sign in to its fax engine. Check that the Asterisk manager password matches."
ENGINE_UNREACHABLE = "Faxbot can't reach its fax engine. Check that the Asterisk service is running."
ENGINE_CONNECTING = "Faxbot is still connecting to its fax engine."
ENGINE_NOT_IN_USE = "Faxbot connects to its fax engine when the SIP trunk is the provider in use."


class AMILoginRejected(ConnectionError):
    """Asterisk answered the login with an error: the manager username or password does not match."""


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

# A T.33 subaddress as the built-in engine sends it (patch 0005's faxbot_sub_clean): digits and +, # and *, at
# most 20, spaces dropped. The same characters a receiving rule accepts (access/receiving_rules.py).
SUBADDRESS = re.compile(r"[0-9#*+]{1,20}")
SUBADDRESS_VARIABLE = re.compile(r"(?:^|,)FAXBOT_TX_SUB=([0-9#*+]{1,20})(?=,|$)")


def subaddress_text(value) -> Optional[str]:
    """``value`` as the engine sends it, or None when a fax subaddress cannot hold it."""
    if not isinstance(value, str):
        return None
    text = value.replace(" ", "")
    return text if SUBADDRESS.fullmatch(text) else None


def peer_route_fields(token: str, address: str) -> Dict[str, str]:
    """The Originate that runs one peer fax call route check in Asterisk ([faxbot-peer-route]); nothing is dialed."""
    if not isinstance(address, str) or not PEER_ADDRESS.fullmatch(address):
        raise ValueError("Unsupported peer address")
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise ValueError("Unsupported check")
    return {"Action": "Originate", "ActionID": "faxbot-route:" + token, "Channel": "Local/s@faxbot-peer-route",
            "Application": "Wait", "Data": "1", "Async": "true",
            "Variable": f"FAXBOT_CHECK={token},FAXBOT_PEER_ADDRESS={address}"}


def requested_subaddress(fields: Dict[str, str]) -> Optional[str]:
    """The subaddress an Originate's fields ask for, or None."""
    found = SUBADDRESS_VARIABLE.search(fields.get("Variable", ""))
    return found.group(1) if found else None


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
    max_rate: Optional[int] = None,
    ecm: Optional[bool] = None,
    t38_now: bool = False,
    iaf: Optional[str] = None,
    audio: bool = False,
    endpoint: str = "trunk-endpoint",
    subaddress: Optional[str] = None,
    peer: Optional[str] = None,
) -> Dict[str, str]:
    """Prepare one direct PJSIP call before a durable marker or any I/O.

    Spaces are literal path characters. Dialplan/variable separators and
    expansion/quoting syntax in paths are refused. Captured UTF-8 metadata is
    base64 encoded so commas and expansion syntax remain literal data.

    ``dial`` is the carrier-formatted Request-URI user (defaults to ``dest``);
    ``station_id`` is the fax station identifier (defaults to ``caller_id``).
    ``fax_preference`` adds the RFC 6913 Accept-Contact header to this call's
    initial INVITE: a property of the route, never a reason to call again.
    ``endpoint`` is the trunk the call goes over (``sip_trunk.endpoint_name``);
    the caller checks it is one Faxbot rendered.
    ``subaddress`` is the T.33 subaddress (SUB) this fax asks the far end's
    machine for (patch 0005, ``FAXBOT_TX_SUB``): digits and +, # and *, at most
    20. It is requested, never promised: the engine sends it only when the far
    end's machine says it takes one.
    ``peer`` is a partner's peer fax call endpoint (``peer-<id>-endpoint``,
    direct/peer_call.py): the call goes there, inside the tunnel, instead of
    over ``endpoint``. The caller checked the tunnel first.
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
    if not isinstance(endpoint, str) or not re.fullmatch(r"trunk-(?:[a-z0-9][a-z0-9_-]{0,31}-)?endpoint", endpoint):
        raise ValueError("Unsupported AMI trunk")
    if peer is not None and (not isinstance(peer, str) or not PEER_ENDPOINT.fullmatch(peer)):
        raise ValueError("Unsupported AMI partner")
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
    # This call's highest speed and error correction (fax settings and the recipient's own limits).
    if max_rate is not None:
        if max_rate not in (14400, 9600, 7200, 4800):
            raise ValueError("Unsupported AMI fax speed")
        variables["FAXBOT_MAXRATE"] = str(max_rate)
    if ecm is not None:
        variables["FAXBOT_ECM"] = "yes" if ecm else "no"
    # Patch 0004: ask for T.38 at once (a number that never asks itself), and Internet Aware Fax (an approved
    # fax server or enrolled partner only). Both are learned or approved per number (engine_frames.py).
    if t38_now:
        variables["FAXBOT_T38_NOW"] = "yes"
    if iaf is not None:
        if iaf not in ("peer", "endpoint"):
            raise ValueError("Unsupported AMI fax mode")
        variables["FAXBOT_IAF"] = iaf
    # Audio fax for this call only: fax over IP (T.38) failed to this number and audio has not (engine_learning).
    if not isinstance(audio, bool):
        raise ValueError("Unsupported AMI fax mode")
    if audio:
        variables["FAXBOT_AUDIO"] = "yes"
    # Patch 0005: the subaddress this fax asks for (a notice fax's notice ID, or one your sending rules chose).
    if subaddress is not None:
        clean = subaddress_text(subaddress)
        if clean is None:
            raise ValueError("Unsupported AMI subaddress")
        variables["FAXBOT_TX_SUB"] = clean
    assignments = [f"{key}={value}" for key, value in variables.items()]
    if fax_preference:
        assignments.append(FAX_PREFERENCE_VARIABLE)
    fields = {
        "Action": "Originate",
        "ActionID": (
            f"faxbot:{job_id}:{attempt_id}" if attempt_id is not None else str(uuid4())
        ),
        "Channel": f"PJSIP/{dial}@{peer or endpoint}",
        "Context": "faxbot-send",
        "Exten": "s",
        "Priority": "1",
        "Async": "true",
        "Variable": ",".join(assignments),
        "CallerID": caller_id,
    }
    _validate_headers(fields)
    return fields


def _database():
    """The installation's database engine, or None before it is ready (reply numbers fall back then)."""
    try:
        from . import db
        return db.engine
    except Exception:
        return None


def reply_choice(values, *, mailbox_id=None):
    """The reply number this fax shows in its header line and station ID (routing/reply_number.py).

    Never raises: when nothing can be read the fax shows the line's own number, as before.
    """
    from .routing import reply_number
    try:
        store = None
        engine = _database()
        if engine is not None:
            try:
                from .routing.store import RouteStore
                store = RouteStore(engine)
            except Exception:
                store = None
        return reply_number.choose(values, engine=engine, store=store, mailbox_id=mailbox_id)
    except Exception:
        return reply_number.Choice(None, 'line', "Faxes show the number of the line they leave on.")


def sender_identity(job_id):
    """(header text, station ID) for a fax Faxbot relays for a partner (``direct.relay``), or None for every
    other fax. Never raises: without the relay, or when nothing can be read, the fax carries its own."""
    try:
        from .direct.relay import sender_identity_for
    except Exception:
        return None
    try:
        found = sender_identity_for(_database(), job_id)
    except Exception:
        return None
    if not isinstance(found, (tuple, list)) or len(found) != 2:
        return None
    header, station = found
    if not isinstance(header, str) or not isinstance(station, str):
        return None
    return header, station


def notice_subaddress(job_id) -> Optional[str]:
    """The 20-digit notice ID a notice fax to an enrolled partner asks for as its subaddress (``direct/notice.py``,
    ``subaddress_for``), or None for every other fax."""
    from .direct.notice import subaddress_for
    engine = _database()
    return subaddress_for(engine, job_id) if engine is not None else None


def rule_subaddress(job_id) -> Optional[str]:
    """The subaddress the fax's sending rules chose (``routing/envelope.py``, the envelope's ``subaddress``), or
    None. A fax accepted before rules, or whose decision can no longer be read, asks for none."""
    engine = _database()
    if engine is None:
        return None
    from .routing import envelope as envelopes
    try:
        pinned = envelopes.load(engine, job_id)
    except envelopes.UnreadableDecision:
        return None
    return getattr(pinned.envelope, "subaddress", None) if pinned is not None else None


def fax_subaddress(job_id) -> Optional[str]:
    """The subaddress this fax asks for: a notice fax's notice ID first, then one its sending rules chose."""
    return notice_subaddress(job_id) or rule_subaddress(job_id)


def learned_options(learned):
    """Originate keyword arguments for what Faxbot learned about the number (engine_learning.Decision):
    audio fax, T.38 at once and Internet Aware Fax. Speed and error correction come with the call."""
    found = {}
    if getattr(learned, "audio", False):
        found["audio"] = True
    if getattr(learned, "t38_now", False):
        found["t38_now"] = True
    if getattr(learned, "iaf", None):
        found["iaf"] = learned.iaf
    return found


def frame_options(values, dest, max_rate=None):
    """What Faxbot learned or was told about ``dest`` (engine_learning.py), as Originate keyword arguments.

    Audio fax, T.38 at once and a learned starting speed come from Faxbot's own calls to the number; a learned
    speed only ever lowers this call's speed. Internet Aware Fax only for a number you approved or an enrolled
    partner marked IAF capable. Never raises: nothing known changes nothing.
    """
    from . import engine_learning
    from .hylafax_engine import try_t38
    database = _database()
    if database is None:
        return {}
    try:
        learned = engine_learning.decide(values, dest, engine="builtin", t38=try_t38(values), base_rate=max_rate,
                                         db=database)
    except Exception:
        return {}
    found = learned_options(learned)
    if learned.max_rate and (max_rate is None or learned.max_rate < max_rate):
        found["max_rate"] = learned.max_rate
    return found


class UnknownTrunk(ValueError):
    """The fax was given a trunk account that is not in Asterisk's file (not set up, turned off or removed)."""


def trunk_values(values, trunk=None):
    """(settings as the trunk sees them, its endpoint) for trunk account ``trunk``; None is the first trunk.

    Only a trunk Faxbot rendered into Asterisk's file is dialed (``sip_trunk.rendered_endpoints``); any other
    raises UnknownTrunk before anything is sent.
    """
    from . import sip_trunk
    if not trunk or trunk == sip_trunk.PRIMARY:
        return values, sip_trunk.ENDPOINT
    found = sip_trunk.trunk_for(values, trunk)
    if found is None or found.endpoint not in sip_trunk.rendered_endpoints(values):
        raise UnknownTrunk("This fax's trunk is not set up in the fax engine.")
    return found.values, found.endpoint


def originate_fields_for(values, job_id, dest, tiff_path, *, attempt_id=None, call=None, mailbox_id=None,
                         choice=None, trunk=None, peer=None):
    """The exact Originate fields for these settings; preflight and submission share it.

    ``trunk`` is the trunk account the fax goes over (its key); None or ``sip`` is the first trunk. The call
    then uses that trunk's caller ID, number format and endpoint. ``peer`` (direct/peer_call.PeerCall) sends it
    to an enrolled partner's Asterisk inside the tunnel instead, with Internet Aware Fax.

    With a configured SIP trunk the call carries the carrier-authorized caller
    ID, the carrier's number format and the optional fax preference; refused
    (ValueError naming fields only) when the trunk cannot place calls. Without
    one, the call carries the station ID as before; an empty station ID sends none.
    ``call`` (hylafax_engine.CallSettings) adds this call's speed and error correction.

    The station ID (TSI) and the number in each page's header line are the
    reply number (``reply_choice``: the mailbox's, the organization's, the
    station ID setting, or the cheapest number that reaches a mailbox); with
    none, a trunk call shows the trunk's caller ID. The caller ID becomes the
    reply number only when it is one of the same trunk's numbers.
    """
    from . import sip_trunk
    from .routing.reply_number import caller_id_for
    values, endpoint = trunk_values(values, trunk)
    limits = {} if call is None else {"max_rate": call.max_rate, "ecm": call.ecm}
    choice = choice if choice is not None else reply_choice(values, mailbox_id=mailbox_id)
    learned = getattr(call, "learned", None)
    if learned is not None:
        limits.update(learned_options(learned))  # the speed and error correction it chose are in the call
    else:
        limits.update(frame_options(values, dest, limits.get("max_rate")))
    # A fax relayed for a partner shows the partner's header text and station ID; caller ID is unchanged.
    identity = sender_identity(job_id)
    header = identity[0] if identity is not None else values.fax_header
    # The subaddress this fax asks for (patch 0005): a notice fax's notice ID, or one its sending rules chose.
    subaddress = fax_subaddress(job_id)
    if subaddress:
        limits["subaddress"] = subaddress
    if peer is not None:
        # A peer fax call (direct/peer_call.py): to the partner's Asterisk inside the tunnel, never a carrier. Its
        # number as you know it reaches the partner's own receiving rules; both ends are Faxbot, so IAF is on.
        limits.update(peer=peer.endpoint, iaf="peer")
    if not sip_trunk.configured(values):
        return prepare_originate_fields(job_id, dest, tiff_path, caller_id=choice.number or values.fax_station_id,
                                        header=header, attempt_id=attempt_id,
                                        station_id=identity[1] if identity is not None else None, **limits)
    trunk = sip_trunk.effective_trunk(values, for_calls=True)
    return prepare_originate_fields(
        job_id, dest, tiff_path, caller_id=caller_id_for(values, choice.number) or trunk.caller_id,
        header=header, attempt_id=attempt_id,
        station_id=identity[1] if identity is not None else (choice.number or None),
        dial=dest if peer is not None else sip_trunk.dial_number(trunk, dest),
        fax_preference=trunk.fax_preference and peer is None, endpoint=endpoint, **limits)


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
                        raise AMILoginRejected("AMI login rejected")
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
        self._queries: Dict[str, Dict[str, object]] = {}
        # Peer fax call route checks waiting for their FaxPeerRoute event, by check token (peer_route).
        self._route_waiters: Dict[str, asyncio.Future] = {}
        # Why the last connection attempt failed ("login_rejected" or
        # "unreachable"); None after a successful login or before any attempt.
        self.problem: Optional[str] = None
        # When the current connection logged in (time.monotonic), so a restart
        # Faxbot asked for can tell the new connection from the old one.
        self.connected_at: Optional[float] = None

    def engine_message(self) -> Optional[str]:
        """The plain sentence for a missing connection, or None while connected."""
        if self._connected.is_set():
            return None
        if self._connection_task is None:
            return ENGINE_NOT_IN_USE
        return {"login_rejected": ENGINE_LOGIN_REJECTED, "unreachable": ENGINE_UNREACHABLE}.get(
            self.problem, ENGINE_CONNECTING)

    async def settle(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a connection; stop early when the login is refused.

        Startup uses this so a quick connection is in place before the worker
        runs, without ever blocking startup on Asterisk.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._connected.is_set() and self.problem != "login_rejected" and loop.time() < deadline:
            await asyncio.sleep(0.05)
        return self._connected.is_set()

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
                self.problem = None
                self.connected_at = time.monotonic()
                self._connected.set()
                delay = 1.0
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except AMILoginRejected:
                self.problem = "login_rejected"
                logging.getLogger(__name__).warning(
                    "Asterisk refused Faxbot's manager login; check that the Asterisk manager password matches. Retrying"
                )
            except Exception:
                self.problem = "unreachable"
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
        self.problem = None
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
        queries, self._queries = self._queries, {}
        for query in queries.values():
            for key in ("response", "done"):
                future = query[key]
                if not future.done():
                    future.set_exception(ConnectionError("AMI connection closed"))
                    future.exception()

    def _dispatch(self, msg: Dict[str, str]):
        fields = {key.lower(): value for key, value in msg.items()}
        query = self._queries.get(fields.get("actionid", "")) if fields.get("actionid") else None
        if query is not None:
            self._collect(query, msg, fields)
            return
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
        elif event == "userevent" and fields.get("userevent", "").lower() == "faxinboundcall":
            self._emit("FaxInboundCall", msg)
        elif event == "userevent" and fields.get("userevent", "").lower() == "faxenginecall":
            self._emit("FaxEngineCall", msg)
        elif event == "userevent" and fields.get("userevent", "").lower() == "faxenginemissed":
            self._emit("FaxEngineMissed", msg)
        elif event == "userevent" and fields.get("userevent", "").lower() == "faxscreened":
            self._emit("FaxScreened", msg)
        elif event == "userevent" and fields.get("userevent", "").lower() == "faxframes":
            self._emit("FaxFrames", msg)
        elif event == "userevent" and fields.get("userevent", "").lower() == "faxpeerroute":
            waiter = self._route_waiters.get(fields.get("check", ""))
            if waiter is not None and not waiter.done():
                waiter.set_result(fields.get("route", ""))

    @staticmethod
    def _collect(query, msg: Dict[str, str], fields: Dict[str, str]):
        if "event" not in fields:
            if not query["response"].done():
                query["response"].set_result(
                    {"response": fields.get("response", ""), "value": fields.get("value", ""),
                     "message": fields.get("message", "")})
            return
        allowed = STATUS_EVENT_FIELDS.get(fields["event"].lower())
        if allowed:
            query["events"].append({key: msg[key] for key in allowed if key in msg})
        if fields.get("eventlist", "").lower() == "complete" and not query["done"].done():
            query["done"].set_result(True)

    async def status_query(self, fields: Dict[str, str], *, collect: bool = False):
        """One read-only status action on the existing connection; never connects or retries.

        Returns the reply (response, value, message) and, for list actions, the
        allowlisted events. Raises ConnectionError or TimeoutError.
        """
        action_id = "faxbot-status:" + uuid4().hex
        fields = {**fields, "ActionID": action_id}
        _validate_headers(fields)
        loop = asyncio.get_running_loop()
        query = {"response": loop.create_future(), "done": loop.create_future(), "events": []}
        self._queries[action_id] = query
        try:
            async with asyncio.timeout(STATUS_TIMEOUT_SECONDS):
                writer = self.writer
                if not self._connected.is_set() or writer is None:
                    raise ConnectionError("AMI connection unavailable")
                writer.write(("".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n").encode())
                await writer.drain()
                response = await query["response"]
                if collect and response["response"].lower() == "success":
                    await query["done"]
                return response, list(query["events"])
        except TimeoutError:
            raise TimeoutError("AMI status timed out") from None
        except ConnectionError:
            raise
        except OSError:
            raise ConnectionError("AMI status unavailable") from None
        finally:
            self._queries.pop(action_id, None)
            for key in ("response", "done"):
                if not query[key].done():
                    query[key].cancel()

    async def active_calls(self) -> int:
        """How many channels (calls) Asterisk has up now; raises ConnectionError or TimeoutError."""
        response, events = await self.status_query({"Action": "CoreShowChannels"}, collect=True)
        if response["response"].lower() != "success":
            raise PermissionError("AMI channel list refused")
        return len(events)

    async def iax_lines_ready(self, prefix: str) -> int:
        """How many IAX peers named ``prefix``* are registered and answer Asterisk's checks."""
        response, events = await self.status_query({"Action": "IAXpeerlist"}, collect=True)
        if response["response"].lower() != "success":
            raise PermissionError("AMI peer list refused")
        return sum(1 for event in events if str(event.get("ObjectName", "")).startswith(prefix)
                   and str(event.get("Status", "")).upper().startswith("OK"))

    async def peer_route(self, address: str):
        """(interface, kind) the route from Asterisk's own network to ``address`` leaves through, or None.

        Asked inside the Asterisk container, where the fax call's packets start (direct/peer_call.py): a Local
        channel runs ``[faxbot-peer-route]``, which runs ``faxbot-peer-route`` and reports a FaxPeerRoute event,
        then hangs up. Nothing is dialed. None when Asterisk cannot say within PEER_ROUTE_TIMEOUT_SECONDS; raises
        ConnectionError or TimeoutError when the manager connection refuses the check.
        """
        token = uuid4().hex
        fields = peer_route_fields(token, address)
        waiter = asyncio.get_running_loop().create_future()
        self._route_waiters[token] = waiter
        try:
            await self._send_action(fields)
            try:
                async with asyncio.timeout(PEER_ROUTE_TIMEOUT_SECONDS):
                    route = await waiter
            except TimeoutError:
                return None
        finally:
            self._route_waiters.pop(token, None)
            if not waiter.done():
                waiter.cancel()
        parts = str(route).strip().split("/")
        return (parts[0], parts[1]) if len(parts) == 2 and parts[0] and parts[0] != "none" else None

    async def db_put(self, family: str, key: str, value: str):
        """Store one value in Asterisk's database (the SSL Fax engine's call plans); raises when not stored."""
        await self._send_action({"Action": "DBPut", "ActionID": "faxbot-db:" + uuid4().hex,
                                 "Family": family, "Key": key, "Val": value})

    async def db_del(self, family: str, key: str):
        """Remove one value from Asterisk's database; raises when Asterisk did not confirm."""
        await self._send_action({"Action": "DBDel", "ActionID": "faxbot-db:" + uuid4().hex,
                                 "Family": family, "Key": key})

    async def stop_gracefully(self) -> bool:
        """Ask Asterisk to stop once no call is up; Docker starts it again and it reloads its files.

        True when Asterisk accepted or began stopping (the connection may close
        before any reply); False when the manager account may not run commands.
        """
        try:
            response, _ = await self.status_query({"Action": "Command", "Command": "core stop gracefully"})
        except (ConnectionError, TimeoutError):
            return True
        if response["response"].lower() == "success":
            return True
        if "permission" in response["message"].lower():
            return False
        raise ConnectionError("AMI command failed")

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
        call=None,
        trunk: Optional[str] = None,
    ):
        """Await acceptance of one Originate action; acceptance is not delivery.

        Submission listeners hear about the call after validation and before
        the action is written, so an unacknowledged call still leaves a record.
        ``trunk`` is the trunk account the fax goes over (None: the first trunk).
        """
        # A fax to an enrolled partner whose Faxbot takes peer fax calls goes inside the encrypted tunnel when the
        # tunnel is up (checked in Asterisk's own network just now); otherwise by the carrier, as before.
        from .direct import peer_call
        peer = await peer_call.call_for(self, settings, _database(), dest)
        fields = originate_fields_for(settings, job_id, dest, tiff_path, attempt_id=attempt_id, call=call,
                                      trunk=trunk, peer=peer)
        own, _ = trunk_values(settings, trunk)
        submission = {
            "JobID": job_id, "AttemptID": attempt_id or "", "Called": dest,
            "CallerID": fields["CallerID"], "Preset": own.sip_trunk_preset or "",
            "FaxPreference": "yes" if FAX_PREFERENCE_VARIABLE in fields["Variable"] else "no",
        }
        # The subaddress this call asks for, recorded as requested (carried only if the far end takes one).
        subaddress = requested_subaddress(fields)
        if subaddress:
            submission["Subaddress"] = subaddress
        if peer is not None:
            submission["Peer"] = peer.peer_id
        if trunk and trunk != "sip":
            submission["Trunk"] = trunk
        learned = getattr(call, "learned", None)
        if learned is not None and learned.changed():
            submission["Learned"] = learned.payload()  # what this call used and why (fax_call_choices)
        self._emit("Submission", submission)
        await self._send_action(fields)

    def on_originate_response(self, cb: Callable[[Dict[str, str]], None]):
        self._listen("OriginateResponse", cb)

    def on_fax_result(self, cb: Callable[[Dict[str, str]], None]):
        self._listen("FaxResult", cb)

    def on_submission(self, cb: Callable[[Dict[str, str]], None]):
        self._listen("Submission", cb)

    def on_engine_call(self, cb: Callable[[Dict[str, str]], None]):
        """A trunk call the SSL Fax engine placed or answered (the dialplan's FaxEngineCall event)."""
        self._listen("FaxEngineCall", cb)

    def on_engine_missed(self, cb: Callable[[Dict[str, str]], None]):
        """A received call none of the SSL Fax engine's free lines answered (the built-in engine took it)."""
        self._listen("FaxEngineMissed", cb)

    def on_frames(self, cb: Callable[[Dict[str, str]], None]):
        """What the far end's fax machine said on a built-in engine call (patch 0004's FaxFrames event)."""
        self._listen("FaxFrames", cb)

    def on_screened(self, cb: Callable[[Dict[str, str]], None]):
        """A call from a blocked sender, turned away before it was answered (the dialplan's FaxScreened event)."""
        self._listen("FaxScreened", cb)

    def on_inbound_call(self, cb: Callable[[Dict[str, str]], None]):
        """A received call that left no fax image (the dialplan's FaxInboundCall event)."""
        self._listen("FaxInboundCall", cb)


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
