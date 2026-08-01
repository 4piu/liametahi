"""`run_task` must turn an unwritable `settings.task_lock_dir` or
`settings.state_db` into a clean `RunOutcome` (exit code 2, no run row,
no report) rather than letting the underlying `OSError` escape -- see
`tests/test_cli.py` for the equivalent end-to-end CLI-level assertions.
"""

from pathlib import Path
from typing import NoReturn

from liametahi import runner as runner_mod
from liametahi.config import load_config
from tests.conftest import make_config_dict, write_config


def _unwritable_path(tmp_path: Path, name: str) -> str:
    """A path no `mkdir(parents=True)` can ever create: a plain file
    sits where a directory component needs to be."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    return str(blocker / "sub" / name)


def _explode(*_: object, **__: object) -> NoReturn:
    raise AssertionError("must not reach a mailbox/model factory")


def test_unwritable_task_lock_dir_exits_bad_config_with_no_report(
    tmp_path: Path,
) -> None:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": str(tmp_path / "state.sqlite3"),
        "task_lock_dir": _unwritable_path(tmp_path, "locks"),
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)

    outcome = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=_explode,
        classifier_factory=_explode,
    )
    assert outcome.exit_code == runner_mod.EXIT_BAD_CONFIG
    assert outcome.run_id is None
    assert outcome.report_data is None
    assert outcome.diagnostic is not None
    assert "settings.task_lock_dir" in outcome.diagnostic
    assert not Path(cfg.settings.state_db).exists(), "must not have opened the DB"


def test_unwritable_state_db_exits_bad_config_with_no_report(tmp_path: Path) -> None:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": _unwritable_path(tmp_path, "state.sqlite3"),
        "task_lock_dir": str(tmp_path / "locks"),
    }
    path = write_config(tmp_path / "cfg.yaml", data)
    cfg = load_config(path)

    outcome = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=_explode,
        classifier_factory=_explode,
    )
    assert outcome.exit_code == runner_mod.EXIT_BAD_CONFIG
    assert outcome.run_id is None
    assert outcome.report_data is None
    assert outcome.diagnostic is not None
    assert "settings.state_db" in outcome.diagnostic

    # The lock must still have been released, not left held.
    second = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=_explode,
        classifier_factory=_explode,
    )
    assert second.exit_code != runner_mod.EXIT_TASK_RUNNING
