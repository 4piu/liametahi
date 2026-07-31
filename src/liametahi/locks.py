"""Advisory per-task run lock (spec §10).

One task has at most one active `run` process. The lock file name is the
SHA-256 of the symlink-resolved config path plus the task name, so the
same task under two different configs never collides and two paths to
one config file never produce two locks.

The default is non-blocking: fail immediately if the lock is held.
`wait_seconds` opts into bounded polling, never unbounded — this module
refuses an infinite or negative wait outright. OS advisory locks
(`flock`) are released automatically when the holding process exits,
including on a crash, so there is deliberately no stale-lock detection
or deletion here.
"""

import fcntl
import hashlib
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_POLL_INTERVAL_SECONDS = 0.2


class LockTimeout(Exception):
    """The task lock could not be acquired before the deadline (or at all,
    for the non-blocking default). Callers map this to exit code 5."""

    def __init__(self, task: str, holder_pid: int | None):
        self.task = task
        self.holder_pid = holder_pid
        detail = f" (held by pid {holder_pid})" if holder_pid is not None else ""
        super().__init__(f"task {task!r} is already running{detail}")


def lock_path(lock_dir: Path, config_path: Path, task: str) -> Path:
    """The deterministic lock file path for one (config, task) pair."""
    resolved_config = config_path.resolve()
    digest = hashlib.sha256(f"{resolved_config}\0{task}".encode()).hexdigest()
    return lock_dir / f"{digest}.lock"


def _read_holder_pid(fd: int) -> int | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 64)
        return int(data.decode("ascii").strip())
    except OSError, ValueError, UnicodeDecodeError:
        return None


@contextmanager
def task_lock(
    lock_dir: Path,
    config_path: Path,
    task: str,
    *,
    wait_seconds: float = 0.0,
) -> Iterator[Path]:
    """Acquire the exclusive advisory lock for `task` under `config_path`.

    Non-blocking by default (`wait_seconds=0`): raises `LockTimeout`
    immediately if the lock is held. With `wait_seconds > 0`, polls until
    acquired or the deadline passes, then raises `LockTimeout` exactly as
    if it had not waited. Never blocks unboundedly: a negative or
    non-finite `wait_seconds` is rejected outright.

    While waiting, no lock is held, no file is written — only polling.
    Yields the resolved lock file path.
    """
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise ValueError(
            "wait_seconds must be a finite, non-negative number of seconds "
            "(spec §10: the wait is always bounded)"
        )
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_path(lock_dir, config_path, task)
    deadline = time.monotonic() + wait_seconds
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(task, _read_holder_pid(fd)) from None
                remaining = deadline - time.monotonic()
                time.sleep(min(_POLL_INTERVAL_SECONDS, max(0.0, remaining)))
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.fsync(fd)
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
