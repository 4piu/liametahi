"""Tests for `liametahi.locks` (spec §10)."""

import multiprocessing
import time
from pathlib import Path

import pytest

from liametahi.locks import LockTimeout, lock_path, task_lock


def test_lock_path_is_deterministic_and_symlink_resolved(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    config = tmp_path / "config.yaml"
    config.write_text("x", encoding="utf-8")
    link = tmp_path / "config-link.yaml"
    link.symlink_to(config)

    path_a = lock_path(lock_dir, config, "task-1")
    path_b = lock_path(lock_dir, link, "task-1")
    assert path_a == path_b  # same underlying file, two paths -> one lock


def test_lock_path_differs_by_task_and_config(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    config_a = tmp_path / "a.yaml"
    config_b = tmp_path / "b.yaml"
    config_a.write_text("x", encoding="utf-8")
    config_b.write_text("y", encoding="utf-8")

    assert lock_path(lock_dir, config_a, "t") != lock_path(lock_dir, config_b, "t")
    assert lock_path(lock_dir, config_a, "t1") != lock_path(lock_dir, config_a, "t2")


def test_acquire_and_release(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    config = tmp_path / "config.yaml"
    config.write_text("x", encoding="utf-8")

    with task_lock(lock_dir, config, "task-1") as path:
        assert path.exists()

    # Released on exit: a second non-blocking acquire succeeds immediately.
    with task_lock(lock_dir, config, "task-1"):
        pass


def test_non_blocking_default_fails_fast_when_held(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    config = tmp_path / "config.yaml"
    config.write_text("x", encoding="utf-8")

    with task_lock(lock_dir, config, "task-1"):
        start = time.monotonic()
        with pytest.raises(LockTimeout), task_lock(lock_dir, config, "task-1"):
            pass
        elapsed = time.monotonic() - start
        assert elapsed < 0.5  # non-blocking: fails essentially immediately


def test_wait_seconds_bounded_and_times_out(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    config = tmp_path / "config.yaml"
    config.write_text("x", encoding="utf-8")

    with task_lock(lock_dir, config, "task-1"):
        start = time.monotonic()
        with (
            pytest.raises(LockTimeout),
            task_lock(lock_dir, config, "task-1", wait_seconds=0.5),
        ):
            pass
        elapsed = time.monotonic() - start
        assert 0.4 <= elapsed < 2.0


@pytest.mark.parametrize("wait_seconds", [-1.0, float("inf"), float("nan")])
def test_wait_seconds_never_unbounded(tmp_path: Path, wait_seconds: float) -> None:
    lock_dir = tmp_path / "locks"
    config = tmp_path / "config.yaml"
    config.write_text("x", encoding="utf-8")

    with (
        pytest.raises(ValueError),
        task_lock(lock_dir, config, "task-1", wait_seconds=wait_seconds),
    ):
        pass


def _hold_lock_for(lock_dir: Path, config: Path, task: str, seconds: float) -> None:
    with task_lock(lock_dir, config, task):
        time.sleep(seconds)


def test_wait_acquires_after_holder_releases(tmp_path: Path) -> None:
    lock_dir = tmp_path / "locks"
    config = tmp_path / "config.yaml"
    config.write_text("x", encoding="utf-8")

    holder = multiprocessing.Process(
        target=_hold_lock_for, args=(lock_dir, config, "task-1", 1.0)
    )
    holder.start()
    try:
        time.sleep(0.2)  # let the holder acquire first
        start = time.monotonic()
        with task_lock(lock_dir, config, "task-1", wait_seconds=5.0):
            elapsed = time.monotonic() - start
            assert elapsed >= 0.5  # actually waited for the holder
    finally:
        holder.join(timeout=5)


def test_lock_released_on_holder_crash(tmp_path: Path) -> None:
    """OS advisory locks release automatically on process exit, including
    a crash — no stale-lock cleanup is implemented or needed (spec §10)."""
    lock_dir = tmp_path / "locks"
    config = tmp_path / "config.yaml"
    config.write_text("x", encoding="utf-8")

    holder = multiprocessing.Process(
        target=_hold_lock_for, args=(lock_dir, config, "task-1", 30.0)
    )
    holder.start()
    time.sleep(0.2)
    holder.kill()  # SIGKILL: no cleanup code in the child runs at all
    holder.join(timeout=5)

    # The lock is available immediately, non-blocking, once the process
    # is gone — no stale-lock detection was required.
    deadline = time.monotonic() + 5
    acquired = False
    while time.monotonic() < deadline and not acquired:
        try:
            with task_lock(lock_dir, config, "task-1"):
                acquired = True
        except LockTimeout:
            time.sleep(0.1)
    assert acquired
