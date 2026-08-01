"""Tests for `liametahi.cli` (spec §9)."""

import os
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from liametahi import runner as runner_mod
from liametahi import state
from liametahi.cli import _handle_termination, app
from liametahi.config import load_config
from liametahi.locks import task_lock
from tests.conftest import make_config_dict, write_config

runner = CliRunner()


def _config_with_state(tmp_path: Path) -> Path:
    """A valid config whose state DB, backups, and locks live under
    `tmp_path`, for commands that actually open the database."""
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": str(tmp_path / "state.sqlite3"),
        "backup_dir": str(tmp_path / "backups"),
        "task_lock_dir": str(tmp_path / "locks"),
    }
    return write_config(tmp_path / "cfg.yaml", data)


def _unwritable_path(tmp_path: Path, name: str) -> str:
    """A path no `mkdir(parents=True)` can ever create: a plain file
    sits where a directory component needs to be, so this reproduces the
    failure portably (unlike a permission-denied path under `/`, which
    depends on not running as root)."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    return str(blocker / "sub" / name)


def test_config_check_valid_config_exits_0(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    result = runner.invoke(app, ["config", "check", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "config OK" in result.output
    assert "inbox-cleanup" in result.output


def test_config_check_connect_surfaces_auth_failure_as_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--connect` verifies IMAP auth; an auth failure is exit 4, not the
    generic runtime-failure exit 1 (spec §9)."""
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())

    def fake_connect_check(
        config: object, **_: object
    ) -> runner_mod.ConnectCheckResult:
        return runner_mod.ConnectCheckResult(
            checked_accounts=(),
            issues=(
                runner_mod.ConnectCheckIssue(
                    severity="error", account="personal", message="login rejected"
                ),
            ),
            auth_failed=True,
        )

    monkeypatch.setattr(runner_mod, "connect_check", fake_connect_check)
    result = runner.invoke(app, ["config", "check", "--config", str(path), "--connect"])
    assert result.exit_code == 4, result.output


def test_config_check_connect_not_called_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain `config check` contacts nothing (spec §9)."""
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("config check must not connect without --connect")

    monkeypatch.setattr(runner_mod, "connect_check", explode)
    result = runner.invoke(app, ["config", "check", "--config", str(path)])
    assert result.exit_code == 0, result.output


def test_config_check_missing_file_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["config", "check", "--config", str(tmp_path / "nope.yaml")]
    )
    assert result.exit_code == 2


def test_config_check_unwritable_log_file_exits_2_not_a_traceback(
    tmp_path: Path,
) -> None:
    """`configure_logging` creates `log_file`'s parent directory; when it
    can't (a blocking file in the way, permission denied, ...) this must
    be a clean exit-2 config error, not an unhandled OSError."""
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "log_file": _unwritable_path(tmp_path, "liametahi.log"),
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    result = runner.invoke(app, ["config", "check", "--config", str(path)])
    assert result.exit_code == 2, result.output
    assert "config error" in result.output
    assert "settings.log_file" in result.output


def test_acceptance_15_config_check_world_readable_exits_2(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    path.chmod(0o644)
    result = runner.invoke(app, ["config", "check", "--config", str(path)])
    assert result.exit_code == 2
    assert "config error" in result.output


def test_acceptance_14_invalid_rule_configs_exit_2(tmp_path: Path) -> None:
    variants = {
        "two-llm-atoms": [{"llm": "a"}, {"llm": "b"}],
        "llm-under-not": {"not": {"llm": "a"}},
    }
    for name, when in variants.items():
        data = make_config_dict()
        data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = when
        data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["move_to:Archive"]
        path = write_config(tmp_path / f"{name}.yaml", data)
        result = runner.invoke(app, ["config", "check", "--config", str(path)])
        assert result.exit_code == 2, f"{name}: {result.output}"

    # trash on an llm-only rule
    data = make_config_dict()
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = {"llm": "discard freely"}
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["trash"]
    path = write_config(tmp_path / "trash-llm-only.yaml", data)
    result = runner.invoke(app, ["config", "check", "--config", str(path)])
    assert result.exit_code == 2, result.output


def test_run_unknown_task_exits_2(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    result = runner.invoke(app, ["run", "no-such-task", "--config", str(path)])
    assert result.exit_code == 2, result.output


def test_run_rejects_bad_format(tmp_path: Path) -> None:
    path = write_config(tmp_path / "cfg.yaml", make_config_dict())
    result = runner.invoke(
        app, ["run", "inbox-cleanup", "--config", str(path), "--format", "yaml"]
    )
    assert result.exit_code == 2, result.output


def test_run_unwritable_task_lock_dir_exits_2_not_a_traceback(tmp_path: Path) -> None:
    """`task_lock` creates `task_lock_dir`; when it can't, this must be a
    clean exit-2 config error, not an unhandled OSError -- and it must
    happen before any mailbox/model factory is ever called, since lock
    acquisition comes first (spec §10)."""
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": str(tmp_path / "state.sqlite3"),
        "task_lock_dir": _unwritable_path(tmp_path, "locks"),
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    result = runner.invoke(app, ["run", "inbox-cleanup", "--config", str(path)])
    assert result.exit_code == 2, result.output
    assert "settings.task_lock_dir" in result.output


def test_run_unwritable_state_db_exits_2_not_a_traceback(tmp_path: Path) -> None:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": _unwritable_path(tmp_path, "state.sqlite3"),
        "task_lock_dir": str(tmp_path / "locks"),
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    result = runner.invoke(app, ["run", "inbox-cleanup", "--config", str(path)])
    assert result.exit_code == 2, result.output
    assert "settings.state_db" in result.output


def test_report_with_no_stored_runs_exits_1(tmp_path: Path) -> None:
    path = _config_with_state(tmp_path)
    result = runner.invoke(app, ["report", "--config", str(path)])
    assert result.exit_code == 1, result.output


def test_report_list_with_no_runs_exits_0(tmp_path: Path) -> None:
    """`--list` on an empty database is not an error — there is simply
    nothing to list."""
    path = _config_with_state(tmp_path)
    result = runner.invoke(app, ["report", "--config", str(path), "--list"])
    assert result.exit_code == 0, result.output


def test_report_unwritable_state_db_exits_2_not_a_traceback(tmp_path: Path) -> None:
    """`open_database` creates `state_db`'s parent directory; when it
    can't, this must be a clean exit-2 config error, not an unhandled
    OSError."""
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": _unwritable_path(tmp_path, "state.sqlite3"),
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    result = runner.invoke(app, ["report", "--config", str(path), "--list"])
    assert result.exit_code == 2, result.output
    assert "config error" in result.output
    assert "settings.state_db" in result.output


def test_report_never_contacts_mailbox_or_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §9: `report` reads stored state only."""

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("report must not open a mailbox or a classifier")

    monkeypatch.setattr(runner_mod, "default_mailbox_factory", explode)
    monkeypatch.setattr(runner_mod, "default_classifier_factory", explode)
    path = _config_with_state(tmp_path)
    result = runner.invoke(app, ["report", "--config", str(path), "--list"])
    assert result.exit_code == 0, result.output


def test_restore_unknown_backup_exits_1(tmp_path: Path) -> None:
    path = _config_with_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "restore",
            "bkp_NOPE",
            "--mailbox",
            "INBOX",
            "--config",
            str(path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 1, result.output


def test_restore_unwritable_state_db_exits_2_not_a_traceback(tmp_path: Path) -> None:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": _unwritable_path(tmp_path, "state.sqlite3"),
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    result = runner.invoke(
        app,
        [
            "restore",
            "bkp_NOPE",
            "--mailbox",
            "INBOX",
            "--config",
            str(path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "config error" in result.output
    assert "settings.state_db" in result.output


def test_restore_dry_run_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §4.4: a dry run verifies the checksum and reports what it
    would append, without connecting."""

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("restore --dry-run must not connect")

    monkeypatch.setattr(runner_mod, "default_mailbox_factory", explode)
    path = _config_with_state(tmp_path)
    result = runner.invoke(
        app,
        [
            "restore",
            "bkp_NOPE",
            "--mailbox",
            "INBOX",
            "--config",
            str(path),
            "--dry-run",
        ],
    )
    # Fails because the backup id is unknown, not because it connected.
    assert result.exit_code == 1, result.output
    assert "restore error" in result.output


def test_config_path_resolution_prefers_env_over_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--config` wins; else `$LIAMETAHI_CONFIG`; else the default (spec §9)."""
    path = write_config(tmp_path / "from-env.yaml", make_config_dict())
    monkeypatch.setenv("LIAMETAHI_CONFIG", str(path))
    result = runner.invoke(app, ["config", "check"])
    assert result.exit_code == 0, result.output
    assert "from-env.yaml" in result.output


def test_missing_config_exits_2_and_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "config.yaml"
    result = runner.invoke(app, ["config", "check", "--config", str(missing)])
    assert result.exit_code == 2
    assert str(missing) in result.output


def test_acceptance_09_second_run_while_locked_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §14.9 / §10: a second invocation of the same task while one is
    active exits 5 and performs no mailbox, model, or SQLite run work.
    """
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("a lock-contended run must do no mailbox/model work")

    monkeypatch.setattr(runner_mod, "default_mailbox_factory", explode)
    monkeypatch.setattr(runner_mod, "default_classifier_factory", explode)

    # Hold the lock exactly as a live run would, then invoke again.
    with task_lock(cfg.settings.task_lock_dir, path, "inbox-cleanup"):
        result = runner.invoke(app, ["run", "inbox-cleanup", "--config", str(path)])

    assert result.exit_code == 5, result.output
    # No run report was created (spec §10).
    assert not Path(cfg.settings.state_db).exists() or _run_count(cfg) == 0


def test_acceptance_09_wait_times_out_and_still_exits_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `--wait SECONDS`, a run that never acquires the lock still
    exits 5 once the deadline passes (spec §10)."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    def explode(*_: object, **__: object) -> None:
        raise AssertionError("a lock-contended run must do no mailbox/model work")

    monkeypatch.setattr(runner_mod, "default_mailbox_factory", explode)
    monkeypatch.setattr(runner_mod, "default_classifier_factory", explode)

    with task_lock(cfg.settings.task_lock_dir, path, "inbox-cleanup"):
        result = runner.invoke(
            app, ["run", "inbox-cleanup", "--config", str(path), "--wait", "0.2"]
        )

    assert result.exit_code == 5, result.output


def test_handle_termination_turns_sigterm_into_keyboard_interrupt() -> None:
    """spec §10: SIGTERM must reach `runner.py` as the same
    `KeyboardInterrupt` Python already raises for SIGINT (Ctrl+C)."""
    with pytest.raises(KeyboardInterrupt), _handle_termination():
        os.kill(os.getpid(), signal.SIGTERM)


def test_handle_termination_restores_the_previous_sigterm_handler() -> None:
    original = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(KeyboardInterrupt), _handle_termination():
            os.kill(os.getpid(), signal.SIGTERM)
        assert signal.getsignal(signal.SIGTERM) is original
    finally:
        signal.signal(signal.SIGTERM, original)


def _run_count(cfg: object) -> int:
    db = Path(cfg.settings.state_db)  # type: ignore[attr-defined]
    if not db.exists():
        return 0
    conn = state.open_database(db)
    try:
        return len(state.list_runs(conn))
    finally:
        state.close_database(conn)
