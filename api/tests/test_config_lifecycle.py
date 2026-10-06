"""Installation ownership is exercised across real POSIX processes."""
from importlib import import_module
import fcntl
import errno
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import time

import pytest

# A hang guard, never a timing assumption: a wait that uses it ends as soon as the worker answers, and a
# loaded machine can take seconds to start a Python worker. Waits that prove a bound keep their own short limits.
CONDITION_TIMEOUT = 30.0


def lifecycle_module():
    try:
        return import_module("api.app.config_lifecycle")
    except ModuleNotFoundError:
        pytest.fail("Installation lifecycle dependency is not implemented")


def test_construction_and_unused_close_do_not_acquire_or_create_resources(tmp_path):
    module = lifecycle_module()
    directory = tmp_path / "not-created"
    lifecycle = module.InstallationLifecycle(directory)
    assert lifecycle.can_promote is False
    lifecycle.close()
    lifecycle.close()
    assert not directory.exists()


_WORKER = """
import json
from pathlib import Path
import sys
from api.app.config_lifecycle import InstallationLifecycle, InstallationLifecycleError
lock = InstallationLifecycle(Path(sys.argv[1]), startup_timeout_seconds=float(sys.argv[2]))
if len(sys.argv) > 3 and sys.argv[3] == 'interrupt':
    import fcntl
    def interrupted_flock(*args):
        raise InterruptedError('synthetic interrupted syscall')
    fcntl.flock = interrupted_flock
print(json.dumps({'state': 'starting'}), flush=True)
try:
    lock.acquire()
    print(json.dumps({'state': 'acquired', 'can_promote': lock.can_promote}), flush=True)
    for command in sys.stdin:
        command = command.strip()
        if command == 'serve':
            lock.mark_serving()
            print(json.dumps({'state': 'serving', 'can_promote': lock.can_promote}), flush=True)
        elif command == 'close':
            lock.close()
            print(json.dumps({'state': 'closed'}), flush=True)
        elif command == 'exit':
            break
except InstallationLifecycleError as error:
    print(json.dumps({'state': 'error', 'message': str(error)}), flush=True)
finally:
    lock.close()
"""


def message(process, *, timeout=CONDITION_TIMEOUT):
    if not select.select([process.stdout], [], [], timeout)[0]:
        return None
    payload = process.stdout.readline()
    if not payload:
        pytest.fail("Lifecycle worker exited: " + process.stderr.read().decode(errors="replace"))
    return json.loads(payload)


def command(process, text):
    process.stdin.write(text.encode() + b"\n")
    process.stdin.flush()
    return message(process)


@pytest.fixture
def worker():
    processes = []

    def start(directory, *, timeout=2, interrupt=False):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", _WORKER, str(directory), str(timeout)]
            + (["interrupt"] if interrupt else []),
            env={**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (
                str(Path(__file__).resolve().parents[2]), os.environ.get("PYTHONPATH")
            )))},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0,
        )
        processes.append(process)
        assert message(process) == {"state": "starting"}
        return process

    yield start
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_startup_serializes_workers_and_only_stopped_installation_can_promote(tmp_path, worker):
    first = worker(tmp_path)
    assert message(first) == {"state": "acquired", "can_promote": True}
    second = worker(tmp_path, timeout=CONDITION_TIMEOUT)  # waits for the first to serve, however slow
    assert message(second, timeout=0.1) is None
    assert command(first, "serve") == {"state": "serving", "can_promote": False}
    assert message(second) == {"state": "acquired", "can_promote": False}
    assert command(second, "serve") == {"state": "serving", "can_promote": False}
    assert command(first, "close") == {"state": "closed"}
    rolling = worker(tmp_path)
    assert message(rolling) == {"state": "acquired", "can_promote": False}
    assert command(rolling, "serve") == {"state": "serving", "can_promote": False}
    assert command(second, "close") == {"state": "closed"}
    assert command(rolling, "close") == {"state": "closed"}
    stopped = worker(tmp_path)
    assert message(stopped) == {"state": "acquired", "can_promote": True}


def test_repeated_acquire_mark_and_close_preserve_stable_lockfiles(tmp_path, worker):
    module = lifecycle_module()
    lifecycle = module.InstallationLifecycle(tmp_path)
    assert lifecycle.acquire() is lifecycle
    assert lifecycle.acquire() is lifecycle
    assert lifecycle.can_promote is True
    identities = {path.name: path.stat().st_ino for path in tmp_path.iterdir()}
    lifecycle.mark_serving()
    lifecycle.mark_serving()
    assert lifecycle.can_promote is False
    lifecycle.close()
    lifecycle.close()
    assert lifecycle.can_promote is False
    assert {path.name: path.stat().st_ino for path in tmp_path.iterdir()} == identities
    next_worker = worker(tmp_path)
    assert message(next_worker) == {"state": "acquired", "can_promote": True}


def test_startup_timeout_is_bounded_and_does_not_release_existing_owner(tmp_path, worker):
    first = worker(tmp_path)
    assert message(first) == {"state": "acquired", "can_promote": True}
    started = time.monotonic()
    blocked = worker(tmp_path, timeout=0.12)
    result = message(blocked, timeout=0.8)
    assert result is not None and result["state"] == "error"
    assert time.monotonic() - started < 1
    assert str(tmp_path) not in result["message"]
    assert command(first, "serve") == {"state": "serving", "can_promote": False}
    next_worker = worker(tmp_path)
    assert message(next_worker) == {"state": "acquired", "can_promote": False}


@pytest.mark.parametrize("name", [".faxbot-startup.lock", ".faxbot-serving.lock"])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "fifo", "directory"])
def test_unsafe_lock_artifact_fails_safely_without_following_or_waiting(tmp_path, worker, name, kind):
    directory = tmp_path / "private-parent-path-sentinel"
    directory.mkdir()
    artifact = directory / name
    target = tmp_path / "private-target-path-sentinel"
    if kind in {"symlink", "dangling"}:
        if kind == "symlink":
            target.write_bytes(b"synthetic-untouched")
        artifact.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(artifact, 0o600)
    else:
        artifact.mkdir()
    candidate = worker(directory)
    result = message(candidate, timeout=0.8)
    assert result is not None and result["state"] == "error"
    for marker in (str(directory), "private-parent-path-sentinel", "private-target-path-sentinel"):
        assert marker not in result["message"]
    if kind == "symlink":
        assert target.read_bytes() == b"synthetic-untouched"
    if kind == "dangling":
        assert not target.exists()
    if kind in {"symlink", "dangling"}:
        assert artifact.is_symlink()
    elif kind == "fifo":
        assert stat.S_ISFIFO(artifact.stat().st_mode)
    else:
        assert artifact.is_dir()
    artifact.unlink() if kind != "directory" else artifact.rmdir()
    retry = worker(directory)
    assert message(retry) == {"state": "acquired", "can_promote": True}


def test_missing_parent_error_is_safe_and_does_not_create_bootstrap_directory(tmp_path):
    module = lifecycle_module()
    directory = tmp_path / "private-parent-path-sentinel"
    lifecycle = module.InstallationLifecycle(directory)
    with pytest.raises(module.InstallationLifecycleError) as caught:
        lifecycle.acquire()
    assert str(directory) not in str(caught.value)
    assert "private-parent-path-sentinel" not in repr(caught.value)
    assert caught.value.__suppress_context__
    lifecycle.close()
    assert not directory.exists()


def test_existing_lockfiles_are_private_before_startup_ownership_is_returned(tmp_path):
    module = lifecycle_module()
    for name in (".faxbot-startup.lock", ".faxbot-serving.lock"):
        path = tmp_path / name
        path.write_bytes(b"")
        path.chmod(0o644)
    lifecycle = module.InstallationLifecycle(tmp_path)
    try:
        lifecycle.acquire()
        assert {stat.S_IMODE(path.stat().st_mode) for path in tmp_path.iterdir()} == {0o600}
    finally:
        lifecycle.close()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), None, "synthetic-private-timeout"])
def test_invalid_timeout_is_rejected_without_acquiring_or_exposing_input(tmp_path, timeout):
    module = lifecycle_module()
    with pytest.raises(module.InstallationLifecycleError) as caught:
        module.InstallationLifecycle(tmp_path, startup_timeout_seconds=timeout)
    assert "synthetic-private-timeout" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_mark_serving_requires_acquired_ownership(tmp_path):
    module = lifecycle_module()
    lifecycle = module.InstallationLifecycle(tmp_path)
    with pytest.raises(module.InstallationLifecycleError):
        lifecycle.mark_serving()
    assert list(tmp_path.iterdir()) == []


def test_initialization_exception_releases_both_locks_before_serving(tmp_path, worker):
    module = lifecycle_module()
    lifecycle = module.InstallationLifecycle(tmp_path)
    with pytest.raises(ValueError, match="synthetic initialization failure"):
        with lifecycle as acquired:
            assert acquired is lifecycle
            assert acquired.can_promote is True
            raise ValueError("synthetic initialization failure")
    lifecycle.close()
    assert lifecycle.can_promote is False
    next_worker = worker(tmp_path)
    assert message(next_worker) == {"state": "acquired", "can_promote": True}


@pytest.mark.parametrize("operation", ["acquire", "mark_serving", "close", "can_promote"])
def test_inherited_acquired_object_is_refused_without_releasing_parent_ownership(tmp_path, worker, operation):
    module = lifecycle_module()
    lifecycle = module.InstallationLifecycle(tmp_path).acquire()
    read_end, write_end = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_end)
        try:
            try:
                if operation == "can_promote":
                    lifecycle.can_promote
                else:
                    getattr(lifecycle, operation)()
            except module.InstallationLifecycleError as error:
                payload = {"rejected": True, "message": str(error)}
            else:
                payload = {"rejected": False}
            os.write(write_end, json.dumps(payload).encode())
        finally:
            os.close(write_end)
            os._exit(0)
    os.close(write_end)
    try:
        assert select.select([read_end], [], [], 2)[0]
        result = json.loads(os.read(read_end, 4096))
        assert result["rejected"] is True
        assert str(tmp_path) not in result["message"]
        assert lifecycle.can_promote is True
        contender = worker(tmp_path, timeout=0.1)
        assert message(contender)["state"] == "error"
    finally:
        os.close(read_end)
        os.waitpid(child, 0)
        lifecycle.close()
    fresh = worker(tmp_path)
    assert message(fresh) == {"state": "acquired", "can_promote": True}


def test_startup_gate_is_held_during_real_downgrade_unlock_window(tmp_path, worker, monkeypatch):
    module = lifecycle_module()
    lifecycle = module.InstallationLifecycle(tmp_path).acquire()
    real_flock = fcntl.flock
    contenders = []

    def downgrade_with_window(descriptor, operation):
        if operation == fcntl.LOCK_SH:
            # flock conversions may unlock before relocking. Expose that OS
            # window with a real release and an independent competing starter.
            real_flock(descriptor, fcntl.LOCK_UN)
            contender = worker(tmp_path, timeout=CONDITION_TIMEOUT)
            contenders.append(contender)
            assert message(contender, timeout=0.1) is None
        return real_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", downgrade_with_window)
    try:
        lifecycle.mark_serving()
        assert lifecycle.can_promote is False
        assert message(contenders[0]) == {"state": "acquired", "can_promote": False}
    finally:
        lifecycle.close()


def test_repeated_interrupted_syscalls_cannot_make_startup_wait_unbounded(tmp_path, worker):
    interrupted = worker(tmp_path, timeout=0.05, interrupt=True)
    started = time.monotonic()
    result = message(interrupted, timeout=0.8)
    assert result is not None and result["state"] == "error"
    assert time.monotonic() - started < 1
    retry = worker(tmp_path)
    assert message(retry) == {"state": "acquired", "can_promote": True}


@pytest.mark.parametrize("failure_at", [1, 2])
def test_permission_failure_releases_partial_ownership_and_reports_safe_error(tmp_path, worker, monkeypatch, failure_at):
    module = lifecycle_module()
    real_fchmod = os.fchmod
    calls = 0

    def fail_permission_update(descriptor, mode):
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise OSError(errno.EACCES, "synthetic-private-diagnostic", str(tmp_path))
        return real_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", fail_permission_update)
    lifecycle = module.InstallationLifecycle(tmp_path)
    with pytest.raises(module.InstallationLifecycleError) as caught:
        lifecycle.acquire()
    assert caught.value.__suppress_context__
    assert str(tmp_path) not in str(caught.value)
    assert "synthetic-private-diagnostic" not in repr(caught.value)
    assert lifecycle.can_promote is False
    lifecycle.close()
    retry = worker(tmp_path)
    assert message(retry) == {"state": "acquired", "can_promote": True}


def test_close_errors_do_not_hide_initialization_error_or_leave_locks_owned(tmp_path, worker, monkeypatch):
    module = lifecycle_module()
    real_close = os.close

    def close_then_error(descriptor):
        real_close(descriptor)
        raise OSError(errno.EIO, "synthetic-private-diagnostic", str(tmp_path))

    lifecycle = module.InstallationLifecycle(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close_then_error)
        with pytest.raises(ValueError, match="synthetic initialization failure"):
            with lifecycle:
                raise ValueError("synthetic initialization failure")
    lifecycle.close()
    retry = worker(tmp_path)
    assert message(retry) == {"state": "acquired", "can_promote": True}
