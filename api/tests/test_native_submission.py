"""Internal native acknowledgement contracts; no real telephony commands."""

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
import re
import subprocess

import pytest

from app import ami, freeswitch_service
from app.config import use_configuration
from app.config_values import ConfigurationValues


JOB = "0123456789abcdef0123456789abcdef"
ATTEMPT = "11111111-2222-4333-8444-555555555555"
ACK_UUID = "ABCDEF01-2345-4678-9ABC-DEF012345678"


def test_native_preparation_is_explicit_and_matches_the_issued_contract():
    """Preflight must validate/build the same operation without transport I/O or config reads."""
    prepare = getattr(ami, "prepare_originate_fields", None)
    build = getattr(freeswitch_service, "build_originate_command", None)
    assert callable(prepare) and callable(
        build
    ), "native preflight interface is missing"
    fields = prepare(
        JOB,
        "+15555550123",
        "/fax data/a.tif",
        caller_id="+15555550100",
        attempt_id=ATTEMPT,
    )
    assert fields == {
        "Action": "Originate",
        "ActionID": f"faxbot:{JOB}:{ATTEMPT}",
        "Channel": "PJSIP/+15555550123@trunk-endpoint",
        "Context": "faxbot-send",
        "Exten": "s",
        "Priority": "1",
        "Async": "true",
        "Variable": (
            f"JOBID={JOB},FAXFILE=/fax data/a.tif,FAXHEADER64=,"
            f"FAXSTATION64=KzE1NTU1NTUwMTAw,FAXATTEMPT={ATTEMPT}"
        ),
        "CallerID": "+15555550100",
    }
    assert build(
        "15555550123",
        "/fax/a.tif",
        JOB,
        gateway_name="my_gateway",
        caller_id_number="15555550100",
        t38_enable=False,
        attempt_id=ATTEMPT,
    ) == (
        "bgapi originate {origination_caller_id_number=15555550100,"
        f"faxbot_job_id={JOB},faxbot_attempt_id={ATTEMPT}"
        + "}sofia/gateway/my_gateway/15555550123 &txfax(/fax/a.tif)"
    )


@pytest.mark.parametrize(
    "header", ["", "Faxbot", "Captured, Ω ^ ${ENV(FAX_HEADER)}\r\nnot-an-AMI-header"]
)
def test_ami_captured_metadata_is_encoded_without_variable_or_header_injection(header):
    station = 'Synthetic, "Ω" ${ENV(FAX_LOCAL_STATION_ID)}'
    fields = ami.prepare_originate_fields(
        JOB,
        "15555550123",
        "/fax/a.tif",
        caller_id=station,
        header=header,
        attempt_id=ATTEMPT,
    )
    variables = dict(part.split("=", 1) for part in fields["Variable"].split(","))
    assert set(variables) == {
        "JOBID",
        "FAXFILE",
        "FAXHEADER64",
        "FAXSTATION64",
        "FAXATTEMPT",
    }
    assert base64.b64decode(variables["FAXHEADER64"], validate=True).decode() == header
    assert (
        base64.b64decode(variables["FAXSTATION64"], validate=True).decode() == station
    )
    assert fields["Channel"] == "PJSIP/15555550123@trunk-endpoint"
    assert fields["CallerID"] == station
    assert "\r" not in fields["Variable"] and "\n" not in fields["Variable"]


@pytest.mark.parametrize(
    "header", [None, 17, "synthetic\x00header", "synthetic\ud800header"]
)
def test_ami_unrepresentable_header_is_refused_during_preflight(header):
    with pytest.raises(ValueError) as error:
        ami.prepare_originate_fields(
            JOB,
            "15555550123",
            "/fax/a.tif",
            caller_id="15555550100",
            header=header,
        )
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize("field", ["header", "caller_id", "tiff_path"])
def test_ami_oversized_wire_line_is_refused_before_submission(field):
    kwargs = {"header": "Faxbot", "caller_id": "15555550100", "tiff_path": "/fax/a.tif"}
    kwargs[field] = "a" * 1000
    with pytest.raises(ValueError) as error:
        ami.prepare_originate_fields(JOB, "15555550123", **kwargs, attempt_id=ATTEMPT)
    assert kwargs[field] not in str(error.value)


def test_ami_line_boundary_counts_encoded_bytes_and_crlf():
    ami._validate_headers({"Variable": "a" * 1012})
    with pytest.raises(ValueError):
        ami._validate_headers({"Variable": "a" * 1013})
    with pytest.raises(ValueError):
        ami._validate_headers({"CallerID": "Ω" * 507})


def test_ami_dedicated_dialplan_has_one_send_and_one_terminal_hangup_observation():
    """The direct post-answer flow cannot enter a second Dial or emit success twice."""
    text = (
        Path(__file__).resolve().parents[2] / "asterisk/etc/asterisk/extensions.conf"
    ).read_text()
    contexts = {}
    context = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            context = line[1:-1]
            contexts[context] = []
        elif context and line and not line.startswith(";"):
            contexts[context].append(line)
    assert {
        "faxout",
        "faxsend",
        "fax-hangup",
    } <= contexts.keys(), "legacy contexts were removed"
    send, terminal = contexts["faxbot-send"], contexts["faxbot-result"]
    applications = []
    for line in send + terminal:
        match = re.fullmatch(
            r"(?:exten\s*=>\s*[^,]+,[^,]+|same\s*=>\s*[^,]+),([A-Za-z]+)\((.*)\)", line
        )
        assert match, f"unexpected dedicated dialplan syntax: {line}"
        applications.append(match.group(1))
    assert set(applications) <= {
        "Set",
        "GotoIf",
        "Goto",
        "SendFAX",
        "Hangup",
        "UserEvent",
        "Return",
    }
    assert applications.count("SendFAX") == 1
    assert sum("UserEvent(FaxResult," in line for line in send) == 0
    assert sum("UserEvent(FaxResult," in line for line in terminal) == 1
    assert (
        "CHANNEL(hangup_handler_push)=faxbot-result,s,1(${JOBID},${FAXATTEMPT})"
        in send[0]
    )
    assert sum("hangup_handler_push" in line for line in send + terminal) == 1
    assert any("FAXRESULT_EMITTED" in line and "GotoIf" in line for line in terminal)
    assert any("Set(FAXRESULT_EMITTED=1)" in line for line in terminal)
    assert terminal[-1].endswith("Return()")
    event = next(line for line in terminal if "UserEvent(FaxResult," in line)
    for field in (
        "JobID:${ARG1}",
        "AttemptID:${ARG2}",
        "Status:${FAXSTATUS}",
        "Error:${FAXERROR}",
        "Pages:${FAXPAGES}",
    ):
        assert field in event
    flow = "\n".join(send + terminal)
    assert "ENV(" not in flow and "Local/" not in flow
    assert "Set(FAXOPT(headerinfo)=${BASE64_DECODE(${FAXHEADER64})})" in flow
    assert "Set(FAXOPT(localstationid)=${BASE64_DECODE(${FAXSTATION64})})" in flow
    assert flow.index("BASE64_DECODE(ZmF4Ym90)") < flow.index("SendFAX(")
    assert "HEADER_CODEC_UNAVAILABLE" in flow


class StreamWriter:
    """Owned in-memory wire boundary; the production parser reads real frames."""

    def __init__(self):
        self.requests = asyncio.Queue()
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        self.requests.put_nowait(data)

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@asynccontextmanager
async def connected_stream(monkeypatch):
    monkeypatch.setattr(ami, "ORIGINATE_RESPONSE_TIMEOUT_SECONDS", 0.05, raising=False)
    client = ami.AMIClient()
    client.reader = asyncio.StreamReader()
    client.writer = writer = StreamWriter()
    client._connected.set()
    read_task = asyncio.create_task(client._read_loop())
    values = ConfigurationValues.from_environment(
        {"FAX_LOCAL_STATION_ID": "+15555550100"}
    )
    with use_configuration(values):
        try:
            yield client, writer
        finally:
            await client.close()
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ami_adapter_issues_captured_metadata_even_when_environment_changes(
    monkeypatch,
):
    async with connected_stream(monkeypatch) as (client, writer):
        header, station = "Captured, Ω ${unsafe}", "15555550177"
        values = ConfigurationValues.from_environment(
            {"FAX_HEADER": header, "FAX_LOCAL_STATION_ID": station}
        )
        monkeypatch.setenv("FAX_HEADER", "later-environment")
        monkeypatch.setenv("FAX_LOCAL_STATION_ID", "15555550999")
        with use_configuration(values):
            task = asyncio.create_task(
                client.originate_sendfax(
                    JOB, "15555550123", "/fax/a.tif", attempt_id=ATTEMPT
                )
            )
        try:
            raw = (await writer.requests.get()).decode()
            fields = dict(line.split(": ", 1) for line in raw.splitlines() if line)
            variables = dict(
                part.split("=", 1) for part in fields["Variable"].split(",")
            )
            assert base64.b64decode(variables["FAXHEADER64"]).decode() == header
            assert base64.b64decode(variables["FAXSTATION64"]).decode() == station
            assert fields["CallerID"] == station
            assert fields["Channel"] == "PJSIP/15555550123@trunk-endpoint"
            assert fields["Context"] == "faxbot-send"
            assert header not in raw and "later-environment" not in raw
            feed_response(client, fields["ActionID"])
            await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def feed_response(client, action_id, *, response="Success", event=None):
    fields = [f"Response: {response}", f"ActionID: {action_id}"]
    if event:
        fields.insert(0, f"Event: {event}")
    client.reader.feed_data(("\r\n".join(fields) + "\r\n\r\n").encode())


@pytest.mark.asyncio
async def test_ami_drain_is_not_acceptance_and_only_matching_response_acknowledges(
    monkeypatch,
):
    """Removing the pending response wait would accept an unacknowledged action."""
    async with connected_stream(monkeypatch) as (client, writer):
        task = asyncio.create_task(
            client.originate_sendfax(JOB, "+15555550123", "/fax data/fax.tif")
        )
        try:
            raw = (await writer.requests.get()).decode()
            await asyncio.sleep(0)
            assert not task.done(), "socket drain was incorrectly treated as acceptance"
            action_id = next(
                line.removeprefix("ActionID: ")
                for line in raw.splitlines()
                if line.startswith("ActionID: ")
            )
            feed_response(client, "unrelated-action")
            feed_response(client, action_id, event="OriginateResponse")
            await asyncio.sleep(0)
            assert (
                not task.done()
            ), "an event or another action acknowledged this request"
            feed_response(client, action_id)
            assert await task is None
            assert len(writer.writes) == 1
            assert not client._pending_actions
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ami_concurrent_responses_keep_attempt_identity_and_listener_separate(
    monkeypatch,
):
    """Swapping futures or treating an asynchronous event as the action reply is unsafe."""
    async with connected_stream(monkeypatch) as (client, writer):
        events, fax_events = [], []
        client.on_originate_response(events.append)
        client.on_fax_result(fax_events.append)
        second_attempt = "66666666-7777-4888-9999-000000000000"
        tasks = [
            asyncio.create_task(
                client.originate_sendfax(
                    JOB, "15555550123", "/fax/a.tif", attempt_id=value
                )
            )
            for value in (ATTEMPT, second_attempt)
        ]
        try:
            first, second = [(await writer.requests.get()).decode() for _ in range(2)]
            assert f"ActionID: faxbot:{JOB}:{ATTEMPT}\r\n" in first
            assert f"FAXATTEMPT={ATTEMPT}" in first
            assert f"ActionID: faxbot:{JOB}:{second_attempt}\r\n" in second
            feed_response(
                client,
                f"faxbot:{JOB}:{ATTEMPT}",
                response="Failure",
                event="OriginateResponse",
            )
            feed_response(client, f"faxbot:{JOB}:{second_attempt}")
            await tasks[1]
            assert not tasks[0].done()
            assert events == [
                {
                    "Event": "OriginateResponse",
                    "Response": "Failure",
                    "ActionID": f"faxbot:{JOB}:{ATTEMPT}",
                }
            ]
            client.reader.feed_data(
                b"Event: UserEvent\r\nUserEvent: FaxResult\r\nJobID: synthetic-job\r\n\r\n"
            )
            feed_response(client, f"faxbot:{JOB}:{ATTEMPT}")
            await tasks[0]
            assert fax_events == [
                {
                    "Event": "UserEvent",
                    "UserEvent": "FaxResult",
                    "JobID": "synthetic-job",
                }
            ]
            assert not client._pending_actions
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["timeout", "error", "disconnect", "close", "cancel"]
)
async def test_ami_uncertain_failure_cleans_pending_without_replaying(
    monkeypatch, failure
):
    """Each uncertain outcome must issue once and leave no stale future for reconnect."""
    async with connected_stream(monkeypatch) as (client, writer):
        task = asyncio.create_task(
            client.originate_sendfax(
                JOB, "15555550123", "/fax/a.tif", attempt_id=ATTEMPT
            )
        )
        await writer.requests.get()
        if failure == "error":
            feed_response(client, f"faxbot:{JOB}:{ATTEMPT}", response="Error")
        elif failure == "disconnect":
            client.reader.feed_eof()
        elif failure == "close":
            await client.close()
        elif failure == "cancel":
            task.cancel()
        expected = (
            asyncio.CancelledError
            if failure == "cancel"
            else (TimeoutError, ConnectionError)
        )
        with pytest.raises(expected):
            await asyncio.wait_for(task, 1)
        assert len(writer.writes) == 1
        assert not client._pending_actions


@pytest.mark.asyncio
async def test_ami_reconnect_never_reissues_the_unacknowledged_native_action():
    """The owned supervisor may reconnect/login, but must not replay an issued action."""
    logins, actions = asyncio.Queue(), []
    peers = set()
    writers = []

    async def peer(reader, writer):
        peers.add(asyncio.current_task())
        writers.append(writer)
        first = len(writers) == 1
        try:
            logins.put_nowait(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
            )
            await writer.drain()
            if first:
                actions.append(await reader.readuntil(b"\r\n\r\n"))
            else:
                replay = await reader.read()
                if replay:
                    actions.append(replay)
        finally:
            writer.close()
            await writer.wait_closed()
            peers.discard(asyncio.current_task())

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    values = ConfigurationValues.from_environment(
        {
            "ASTERISK_AMI_HOST": "127.0.0.1",
            "ASTERISK_AMI_PORT": str(server.sockets[0].getsockname()[1]),
            "ASTERISK_AMI_USERNAME": "synthetic-native-peer",
            "ASTERISK_AMI_PASSWORD": "synthetic-native-secret",
        }
    )
    client = ami.AMIClient()
    try:
        with use_configuration(values):
            await asyncio.wait_for(client.connect(), 2)
            await asyncio.wait_for(logins.get(), 1)
            with pytest.raises(ConnectionError):
                await client.originate_sendfax(
                    JOB, "15555550123", "/fax/a.tif", attempt_id=ATTEMPT
                )
        await asyncio.wait_for(logins.get(), 3)
        await client.close()
        await asyncio.gather(*peers)
        assert len(writers) == 2
        assert len(actions) == 1
        assert f"ActionID: faxbot:{JOB}:{ATTEMPT}\r\n".encode() in actions[0]
        assert not client._pending_actions
        assert client.writer is None
    finally:
        await client.close()
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*peers, return_exceptions=True)


@pytest.mark.asyncio
async def test_ami_duplicate_pending_attempt_does_not_replace_the_first_future(
    monkeypatch,
):
    async with connected_stream(monkeypatch) as (client, writer):
        first = asyncio.create_task(
            client.originate_sendfax(
                JOB, "15555550123", "/fax/a.tif", attempt_id=ATTEMPT
            )
        )
        try:
            await writer.requests.get()
            with pytest.raises(ConnectionError):
                await client.originate_sendfax(
                    JOB, "15555550123", "/fax/a.tif", attempt_id=ATTEMPT
                )
            feed_response(client, f"faxbot:{JOB}:{ATTEMPT}")
            await first
            assert len(writer.writes) == 1
            assert not client._pending_actions
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_ami_acknowledgement_can_arrive_before_drain_returns(monkeypatch):
    async with connected_stream(monkeypatch) as (client, writer):
        drained = asyncio.Event()

        async def delayed_drain():
            await drained.wait()

        writer.drain = delayed_drain
        task = asyncio.create_task(
            client.originate_sendfax(
                JOB, "15555550123", "/fax/a.tif", attempt_id=ATTEMPT
            )
        )
        try:
            await writer.requests.get()
            client.reader.feed_data(
                (
                    f"rEsPoNsE: Success\r\naCtIoNiD: faxbot:{JOB}:{ATTEMPT}\r\n\r\n"
                ).encode()
            )
            await asyncio.sleep(0)
            drained.set()
            await task
            assert len(writer.writes) == 1
            assert not client._pending_actions
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ami_drain_failure_is_sanitized_and_cleans_the_registered_future(
    monkeypatch,
):
    async with connected_stream(monkeypatch) as (client, writer):

        async def fail():
            raise OSError("synthetic-private-destination")

        writer.drain = fail
        with pytest.raises(ConnectionError) as error:
            await client.originate_sendfax(
                JOB, "15555550123", "/fax/a.tif", attempt_id=ATTEMPT
            )
        assert "private" not in str(error.value)
        assert error.value.__suppress_context__
        assert len(writer.writes) == 1
        assert not client._pending_actions


@pytest.mark.asyncio
async def test_ami_login_injection_is_rejected_before_credentials_are_written():
    writer = StreamWriter()
    with pytest.raises(ValueError) as error:
        await ami._login(
            asyncio.StreamReader(),
            writer,
            "synthetic-user",
            "private-secret\r\nAction: Command",
        )
    assert "secret" not in str(error.value)
    assert not writer.writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("dest", "1555\r\nAction: Command"),
        ("dest", "1555&other"),
        ("job_id", "job,DEST=other"),
        ("attempt_id", "attempt:other"),
        ("tiff_path", "/fax/file,DEST=other.tif"),
        ("tiff_path", "/fax/a^other.tif"),
        ("tiff_path", "/fax/a)\r\nAction: Command"),
        ("tiff_path", "/fax/${DANGEROUS}.tif"),
    ],
)
async def test_ami_rejects_injection_before_any_write(monkeypatch, field, value):
    """Input validation must fail before the native action crosses the wire."""
    async with connected_stream(monkeypatch) as (client, writer):
        kwargs = {
            "job_id": JOB,
            "dest": "15555550123",
            "tiff_path": "/fax/a.tif",
            "attempt_id": ATTEMPT,
        }
        kwargs[field] = value
        with pytest.raises(ValueError) as error:
            await client.originate_sendfax(**kwargs)
        assert value not in str(error.value)
        assert not writer.writes
        assert not client._pending_actions


@pytest.mark.asyncio
async def test_ami_rejects_header_injection_without_exposing_station_id(monkeypatch):
    async with connected_stream(monkeypatch) as (client, writer):
        bad = "synthetic-private\r\nAction: Command"
        with use_configuration(
            ConfigurationValues.from_environment({"FAX_LOCAL_STATION_ID": bad})
        ):
            with pytest.raises(ValueError) as error:
                await client.originate_sendfax(JOB, "15555550123", "/fax/a.tif")
        assert "synthetic-private" not in str(error.value)
        assert not writer.writes


@pytest.fixture
def fs_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(freeswitch_service, "fs_cli_available", lambda: True)

    def output(args, **kwargs):
        calls.append((args, kwargs))
        return f"+OK Job-UUID: {ACK_UUID}\n"

    monkeypatch.setattr(freeswitch_service.subprocess, "check_output", output)
    return calls


def test_freeswitch_returns_canonical_acceptance_uuid_with_bounded_single_command(
    fs_boundary,
):
    """Returning raw fs_cli output or removing its timeout breaks the acceptance seam."""
    values = ConfigurationValues.from_environment(
        {
            "FREESWITCH_GATEWAY_NAME": "gw_signalwire",
            "FREESWITCH_CALLER_ID_NUMBER": "+15555550100",
        }
    )
    with use_configuration(values):
        result = freeswitch_service.originate_txfax(
            "+15555550123", "/var/fax/a.tif", JOB, attempt_id=ATTEMPT
        )
    assert result == "abcdef01-2345-4678-9abc-def012345678"
    assert len(fs_boundary) == 1
    args, kwargs = fs_boundary[0]
    assert args == [
        "fs_cli",
        "-x",
        "bgapi originate {origination_caller_id_number=+15555550100,"
        f"faxbot_job_id={JOB},faxbot_attempt_id={ATTEMPT},fax_enable_t38_request=true,"
        "fax_enable_t38=true}sofia/gateway/gw_signalwire/+15555550123 &txfax(/var/fax/a.tif)",
    ]
    assert kwargs["timeout"] == 30
    assert kwargs["stderr"] == subprocess.PIPE
    assert kwargs.get("shell", False) is False


@pytest.mark.parametrize(
    "output",
    [
        "-ERR synthetic-private",
        "+OK",
        "+OK Job-UUID: invalid",
        "",
        "+OK Job-UUID: " + ACK_UUID + "\n-ERR leaked",
        "+OK " + ACK_UUID,
    ],
)
def test_freeswitch_unexpected_output_is_not_acceptance(
    monkeypatch, fs_boundary, output
):
    monkeypatch.setattr(
        freeswitch_service.subprocess, "check_output", lambda *a, **kw: output
    )
    with pytest.raises(RuntimeError) as error:
        freeswitch_service.originate_txfax("15555550123", "/fax/a.tif", JOB)
    assert "synthetic-private" not in str(error.value)
    assert "leaked" not in str(error.value)


@pytest.mark.parametrize("failure", ["timeout", "exit", "oserror"])
def test_freeswitch_subprocess_failure_is_sanitized_and_not_retried(
    monkeypatch, fs_boundary, failure
):
    issued = []

    def fail(args, **kwargs):
        issued.append(args)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                args, 30, output="synthetic-private", stderr="synthetic-stderr"
            )
        if failure == "exit":
            raise subprocess.CalledProcessError(
                1, args, output="synthetic-private", stderr="synthetic-stderr"
            )
        raise OSError("synthetic-private")

    monkeypatch.setattr(freeswitch_service.subprocess, "check_output", fail)
    with pytest.raises(RuntimeError) as error:
        freeswitch_service.originate_txfax(
            "15555550123", "/fax/private-artifact.tif", JOB
        )
    assert len(issued) == 1
    assert "private" not in str(error.value)
    assert "stderr" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "field,value",
    [
        ("to_number", "1555 &echo"),
        ("to_number", "1555\napi status"),
        ("job_id", "job,other=1"),
        ("attempt_id", "attempt}other"),
        ("tiff_path", "/fax/a.tif) &echo("),
        ("tiff_path", "/fax/space name.tif"),
        ("tiff_path", "/fax/${danger}.tif"),
        ("tiff_path", "/fax/a;other.tif"),
        ("tiff_path", "/fax/'quoted'.tif"),
        ("tiff_path", "/fax/a\nother.tif"),
    ],
)
def test_freeswitch_refuses_unsupported_command_syntax_before_subprocess(
    fs_boundary, field, value
):
    kwargs = {
        "to_number": "15555550123",
        "tiff_path": "/fax/a.tif",
        "job_id": JOB,
        "attempt_id": ATTEMPT,
    }
    kwargs[field] = value
    with pytest.raises(ValueError) as error:
        freeswitch_service.originate_txfax(**kwargs)
    assert value not in str(error.value)
    assert not fs_boundary


@pytest.mark.parametrize(
    "setting,value",
    [
        ("FREESWITCH_GATEWAY_NAME", "gw/other}bad"),
        ("FREESWITCH_CALLER_ID_NUMBER", "1555,other=1"),
    ],
)
def test_freeswitch_configuration_cannot_inject_native_command(
    fs_boundary, setting, value
):
    with use_configuration(ConfigurationValues.from_environment({setting: value})):
        with pytest.raises(ValueError) as error:
            freeswitch_service.originate_txfax("15555550123", "/fax/a.tif", JOB)
    assert value not in str(error.value)
    assert not fs_boundary


def _asterisk_variable_assignments(header_value):
    """Port of Asterisk 22 AMI Variable parsing (AST_STANDARD_APP_ARGS, then name=value)."""
    args, current, depth, quoted, index = [], [], 0, False, 0
    while index < len(header_value):
        char = header_value[index]
        if char == "\\":
            index += 1
            if index < len(header_value):
                current.append(header_value[index])
        elif char == '"':
            quoted = not quoted
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(depth - 1, 0)
            current.append(char)
        elif char == "," and not depth and not quoted:
            args.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    args.append("".join(current))
    return dict(item.split("=", 1) for item in args if "=" in item)


def _trunk(**extra):
    return ConfigurationValues.from_environment({
        "SIP_TRUNK_PRESET": "telnyx", "SIP_TRUNK_AUTH": "ip", "SIP_TRUNK_CALLER_ID": "+15555550100",
        "FAX_LOCAL_STATION_ID": "+15555550111", "FAX_HEADER": "Clinic", **extra})


def test_trunk_call_uses_carrier_caller_id_number_format_and_separate_station_id():
    fields = ami.originate_fields_for(_trunk(), JOB, "+15555550123", "/fax/a.tif", attempt_id=ATTEMPT)
    assert fields["Channel"] == "PJSIP/+15555550123@trunk-endpoint"
    uk = ami.originate_fields_for(_trunk(), JOB, "+441782684953", "/fax/a.tif", attempt_id=ATTEMPT)
    assert uk["Channel"] == "PJSIP/+441782684953@trunk-endpoint"
    with pytest.raises(ValueError):  # national digits are resolved at acceptance, never here
        ami.originate_fields_for(_trunk(), JOB, "5555550123", "/fax/a.tif", attempt_id=ATTEMPT)
    assert fields["CallerID"] == "+15555550100"
    variables = _asterisk_variable_assignments(fields["Variable"])
    assert base64.b64decode(variables["FAXSTATION64"]).decode() == "+15555550111"
    assert "PJSIP_HEADER(add,Accept-Contact)" not in variables
    flowroute = ami.originate_fields_for(
        _trunk(SIP_TRUNK_PRESET="flowroute", SIP_TRUNK_USERNAME="12345678"), JOB, "+15555550123",
        "/fax/a.tif", attempt_id=ATTEMPT)
    assert flowroute["Channel"] == "PJSIP/12345678*15555550123@trunk-endpoint"
    legacy = ami.originate_fields_for(
        ConfigurationValues.from_environment({"FAX_LOCAL_STATION_ID": "+15555550111"}),
        JOB, "+441782684953", "/fax/a.tif", attempt_id=ATTEMPT)
    assert legacy["Channel"] == "PJSIP/+441782684953@trunk-endpoint"
    assert legacy["CallerID"] == "+15555550111"


def test_fax_preference_reaches_asterisk_as_the_exact_rfc6913_header_value():
    fields = ami.originate_fields_for(_trunk(SIP_FAX_PREFERENCE_HEADER="true"), JOB, "+15555550123",
                                      "/fax/a.tif", attempt_id=ATTEMPT)
    variables = _asterisk_variable_assignments(fields["Variable"])
    assert variables["PJSIP_HEADER(add,Accept-Contact)"] == '*;+sip.fax="t38"'
    assert variables["JOBID"] == JOB and variables["FAXATTEMPT"] == ATTEMPT
    assert fields["ActionID"] == f"faxbot:{JOB}:{ATTEMPT}"


def test_trunk_without_authorized_caller_id_is_refused_before_preparation_or_write():
    with pytest.raises(ValueError) as error:
        ami.originate_fields_for(_trunk(SIP_TRUNK_CALLER_ID=""), JOB, "+15555550123", "/fax/a.tif")
    assert "sip_trunk_caller_id" in str(error.value)


@pytest.mark.asyncio
async def test_trunk_without_caller_id_never_writes_and_emits_no_submission(monkeypatch):
    async with connected_stream(monkeypatch) as (client, writer):
        submissions = []
        client.on_submission(submissions.append)
        with use_configuration(_trunk(SIP_TRUNK_CALLER_ID="")):
            with pytest.raises(ValueError):
                await client.originate_sendfax(JOB, "+15555550123", "/fax/a.tif", attempt_id=ATTEMPT)
        assert not writer.writes and not submissions


@pytest.mark.asyncio
async def test_every_listener_hears_each_event_and_a_failing_one_cannot_stop_the_rest(monkeypatch):
    async with connected_stream(monkeypatch) as (client, writer):
        delivery, records = [], []

        def broken(event):
            raise RuntimeError("synthetic listener failure")

        client.on_fax_result(delivery.append)
        client.on_fax_result(broken)
        client.on_fax_result(records.append)
        client.on_fax_result(delivery.append)  # registering twice does not double-deliver
        client.reader.feed_data(
            b"Event: UserEvent\r\nUserEvent: FaxResult\r\nJobID: synthetic-job\r\n\r\n")
        client.reader.feed_data(b"Event: OriginateResponse\r\nResponse: Failure\r\nActionID: x\r\n\r\n")
        await asyncio.sleep(0.01)
        assert [event["JobID"] for event in delivery] == ["synthetic-job"]
        assert [event["JobID"] for event in records] == ["synthetic-job"]
        assert client._connected.is_set()


@pytest.mark.asyncio
async def test_submission_is_announced_before_the_wire_even_when_never_acknowledged(monkeypatch):
    async with connected_stream(monkeypatch) as (client, writer):
        submissions = []
        client.on_submission(lambda event: submissions.append((event, len(writer.writes))))
        with use_configuration(_trunk(SIP_FAX_PREFERENCE_HEADER="true")):
            with pytest.raises((TimeoutError, ConnectionError)):
                await client.originate_sendfax(JOB, "+15555550123", "/fax/a.tif", attempt_id=ATTEMPT)
        assert len(submissions) == 1
        event, writes_before = submissions[0]
        assert writes_before == 0 and len(writer.writes) == 1
        assert event == {"JobID": JOB, "AttemptID": ATTEMPT, "Called": "+15555550123",
                         "CallerID": "+15555550100", "Preset": "telnyx", "FaxPreference": "yes"}


def _context_lines(name):
    text = (Path(__file__).resolve().parents[2] / "asterisk/etc/asterisk/extensions.conf").read_text()
    lines, current = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            current = line[1:-1]
            lines[current] = []
        elif current and line and not line.startswith(";"):
            lines[current].append(line)
    return lines[name]


def test_outbound_result_reports_answer_end_media_and_remote_station_without_new_applications():
    send, terminal = _context_lines("faxbot-send"), _context_lines("faxbot-result")
    assert send[1] == "same => n,Set(FAXBOT_ANSWERED=${EPOCH})"
    event = next(line for line in terminal if "UserEvent(FaxResult," in line)
    for field in ("Mode:${FAXMODE}", "Station64:${BASE64_ENCODE(${REMOTESTATIONID})}",
                  "Answered:${FAXBOT_ANSWERED}", "Ended:${EPOCH}", "Cause:${HANGUPCAUSE}"):
        assert field in event


def test_the_sip_call_id_is_captured_encoded_for_every_call_report():
    """The carrier bills each call under its SIP Call-ID; every call report carries it, base64 encoded."""
    capture = 'Set(FAXBOT_CALLID64=${BASE64_ENCODE(${CHANNEL(pjsip,call-id)})})'
    send, terminal = _context_lines("faxbot-send"), _context_lines("faxbot-result")
    receive, done = _context_lines("faxbot-inbound-receive"), _context_lines("faxbot-inbound-done")
    assert sum(capture in line for line in send) == 1 and sum(capture in line for line in receive) == 1
    answer = next(index for index, line in enumerate(receive) if "Answer()" in line)
    assert any(capture in line for line in receive[:answer])
    reports = [line for line in terminal + done if "UserEvent(" in line]
    assert len(reports) == 3 and all(",CallID64:${FAXBOT_CALLID64}" in line for line in reports)
    shell = next(line for line in done if "SHELL(" in line)
    assert "callid64=${FILTER(" in shell


def test_inbound_dialplan_only_passes_filtered_or_encoded_caller_values_to_the_shell():
    entry = _context_lines("faxbot-inbound")
    receive = _context_lines("faxbot-inbound-receive")
    done = _context_lines("faxbot-inbound-done")
    assert all("FILTER(0123456789+," in line for line in entry if "FAXBOT_DID=$" in line)
    assert any("FAXBOT_CALLER=${FILTER(0123456789+,${CALLERID(num)})}" in line for line in receive)
    assert any("hangup_handler_push)=faxbot-inbound-done" in line for line in receive)
    assert sum("ReceiveFAX(" in line for line in receive) == 1
    system = [line for line in done if "SHELL(" in line or "System(" in line]
    assert len(system) == 1 and "SHELL(/usr/local/bin/faxbot-inbound-notify " in system[0]
    command = system[0]
    assert "ENV(" not in "\n".join(entry + receive + done)
    assert "CALLERID" not in command and "REMOTESTATIONID" not in command and "EXTEN" not in command
    for variable in re.findall(r"\$\{([A-Z0-9_]+)\}", command):
        assert variable in {"FAXBOT_FILE", "FAXBOT_DID", "FAXBOT_CALLER", "FAXBOT_STARTED", "FAXBOT_ANSWERED",
                            "FAXBOT_ENDED", "FAXBOT_STATION64", "FAXBOT_CALLID64", "FAXSTATUS", "FAXPAGES",
                            "FAXMODE", "UNIQUEID"}, variable
    for raw in ("${FAXSTATUS}", "${FAXPAGES}", "${FAXMODE}", "${UNIQUEID}", "${FAXBOT_STATION64}",
                "${FAXBOT_CALLID64}"):
        assert command.count(raw) == command.count("," + raw + ")"), raw
    assert done[-1].endswith("Return()")


@pytest.mark.asyncio
async def test_status_query_keeps_allowlisted_fields_and_drops_auth_details(monkeypatch):
    """PJSIPShowRegistrationsOutbound also emits AuthDetail with the SIP password; it must vanish."""
    async with connected_stream(monkeypatch) as (client, writer):
        task = asyncio.create_task(client.status_query({"Action": "PJSIPShowRegistrationsOutbound"}, collect=True))
        raw = (await writer.requests.get()).decode()
        action_id = next(line.split(": ", 1)[1] for line in raw.splitlines() if line.startswith("ActionID: "))
        assert action_id.startswith("faxbot-status:")
        frames = [
            f"Response: Success\r\nActionID: {action_id}\r\nEventList: start\r\nMessage: Following\r\n\r\n",
            (f"Event: OutboundRegistrationDetail\r\nActionID: {action_id}\r\nObjectName: trunk-registration\r\n"
             "Status: Registered\r\nServerUri: sip:sip.telnyx.com:5060\r\nOutboundAuth: trunk-auth\r\n"
             "ClientUri: sip:faxbotuser@sip.telnyx.com:5060\r\n\r\n"),
            (f"Event: AuthDetail\r\nActionID: {action_id}\r\nObjectName: trunk-auth\r\nUsername: faxbotuser\r\n"
             "Password: synthetic-private-password\r\n\r\n"),
            "Event: UserEvent\r\nUserEvent: FaxResult\r\nJobID: unrelated\r\n\r\n",
            (f"Event: OutboundRegistrationDetailComplete\r\nActionID: {action_id}\r\nEventList: Complete\r\n"
             "ListItems: 1\r\n\r\n"),
        ]
        fax_events = []
        client.on_fax_result(fax_events.append)
        for frame in frames:
            client.reader.feed_data(frame.encode())
        response, events = await asyncio.wait_for(task, 1)
        assert response == {"response": "Success", "value": "", "message": "Following"}
        assert events == [{"ObjectName": "trunk-registration", "Status": "Registered",
                           "ServerUri": "sip:sip.telnyx.com:5060"}]
        assert "synthetic-private-password" not in repr((response, events))
        assert [event["JobID"] for event in fax_events] == ["unrelated"]
        assert not client._queries


@pytest.mark.asyncio
async def test_status_query_never_connects_and_cleans_up_on_disconnect(monkeypatch):
    client = ami.AMIClient()
    with pytest.raises(ConnectionError):
        await client.status_query({"Action": "Getvar", "Variable": "DEVICE_STATE(PJSIP/trunk-endpoint)"})
    assert not client._queries and client._connection_task is None
    async with connected_stream(monkeypatch) as (client, writer):
        task = asyncio.create_task(client.status_query({"Action": "Getvar", "Variable": "X"}))
        await writer.requests.get()
        client.reader.feed_eof()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, 1)
        assert not client._queries
