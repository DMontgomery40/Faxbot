"""Per-worker ownership for one local Faxbot installation.

All starters hold the startup gate through initialization and serving-lock
downgrade. This closes the possible flock EX-to-SH unlock window. Serving workers
keep SH ownership until close; only a stopped installation can acquire serving EX.
The caller owns the existing trusted parent directory and runtime activation.
Lockfiles are private regular files and are never unlinked by this module.
"""
import fcntl
import math
import os
from pathlib import Path
import stat
import time


class InstallationLifecycleError(RuntimeError):
    """A lifecycle ownership failure without paths or raw OS diagnostics."""


class InstallationLifecycle:
    """Acquire inside the actual worker lifespan, then mark ready resources serving.

Construction has no file/lock side effects. A context manager closes ownership
on initialization failure or worker exit. An acquired object inherited by fork
is refused; the child must construct a fresh object for its own descriptors.
"""

    def __init__(self, lock_directory: Path, *, startup_timeout_seconds: float = 10.0):
        if (not isinstance(startup_timeout_seconds, (int, float))
                or not math.isfinite(startup_timeout_seconds) or startup_timeout_seconds < 0):
            raise InstallationLifecycleError("Startup timeout must be finite and nonnegative.")
        self._directory = Path(lock_directory).absolute()
        self._startup_timeout_seconds = startup_timeout_seconds
        self._can_promote = False
        self._startup_fd: int | None = None
        self._serving_fd: int | None = None
        self._owner_pid: int | None = None
        self._inherited = False

    @property
    def can_promote(self) -> bool:
        self._check_process()
        return self._can_promote

    def acquire(self) -> "InstallationLifecycle":
        """Hold the startup gate and select EX or SH installation ownership."""
        self._check_process()
        if self._serving_fd is not None:
            return self
        self._owner_pid = os.getpid()
        try:
            self._startup_fd = self._open_lock(".faxbot-startup.lock")
            self._wait_startup()
            self._serving_fd = self._open_lock(".faxbot-serving.lock")
            try:
                fcntl.flock(self._serving_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._can_promote = True
            except BlockingIOError:
                # A conforming initializer keeps the startup gate while it owns
                # EX. Failure of SH here is an error, never an unbounded wait.
                fcntl.flock(self._serving_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except InstallationLifecycleError:
            self._release()
            raise
        except (OSError, ValueError):
            self._release()
            raise InstallationLifecycleError("Cannot acquire installation lifecycle ownership.") from None
        except BaseException:
            self._release()
            raise
        return self

    def mark_serving(self) -> None:
        """Retain serving SH before releasing the startup gate."""
        self._check_process()
        if self._serving_fd is None:
            raise InstallationLifecycleError("Installation lifecycle ownership has not been acquired.")
        if self._startup_fd is None:
            return
        try:
            if self._can_promote:
                fcntl.flock(self._serving_fd, fcntl.LOCK_SH)
            self._can_promote = False
            descriptor = self._startup_fd
            self._startup_fd = None
            os.close(descriptor)
        except OSError:
            self._release()
            raise InstallationLifecycleError("Cannot transition installation lifecycle ownership.") from None

    def close(self) -> None:
        """Release owned descriptors; repeated normal close is a no-op."""
        self._check_process()
        if self._release():
            raise InstallationLifecycleError("Cannot release installation lifecycle ownership.") from None

    def __enter__(self) -> "InstallationLifecycle":
        return self.acquire()

    def __exit__(self, exception_type, _exception, _traceback) -> None:
        try:
            self.close()
        except InstallationLifecycleError:
            if exception_type is None:
                raise

    def _open_lock(self, name: str) -> int:
        descriptor = os.open(
            self._directory / name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise InstallationLifecycleError("Installation lifecycle locks must be regular files.")
            os.fchmod(descriptor, 0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        return descriptor

    def _wait_startup(self) -> None:
        deadline = time.monotonic() + self._startup_timeout_seconds
        while True:
            try:
                fcntl.flock(self._startup_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (BlockingIOError, InterruptedError):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InstallationLifecycleError("Timed out waiting for installation startup ownership.") from None
                time.sleep(min(0.025, remaining))

    def _release(self) -> bool:
        failed = False
        for name in ("_serving_fd", "_startup_fd"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                setattr(self, name, None)
                try:
                    os.close(descriptor)
                except OSError:
                    failed = True
        self._can_promote = False
        self._owner_pid = None
        return failed

    def _check_process(self) -> None:
        if self._inherited or (self._owner_pid is not None and self._owner_pid != os.getpid()):
            # flock belongs to the shared open-file description. Closing only
            # this child's copies preserves the parent; LOCK_UN would not.
            self._release()
            self._inherited = True
            raise InstallationLifecycleError("Inherited lifecycle ownership requires a fresh worker object.") from None
