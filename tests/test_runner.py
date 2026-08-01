"""Tests for `liametahi.runner`'s signal handling: a `KeyboardInterrupt`
raised mid-run (spec §10) -- what `cli.py` turns both SIGINT and SIGTERM
into -- must finish the run with a proper stored report and exit code 3,
release the task lock normally, and record an audit event, rather than
leaving the run row looking perpetually in-progress.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from liametahi import runner as runner_mod
from liametahi import state
from liametahi.classifier import CandidatePayload, ClassifyOutcome, OfferedRule
from liametahi.config import load_config
from tests.conftest import make_config_dict, write_config
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<msg1@example.com>"


class _InterruptingClassifier:
    """Raises `KeyboardInterrupt` on the first `classify()` call, exactly
    as a real classifier would if SIGINT/SIGTERM landed mid-HTTP-call."""

    def classify(
        self, candidates: Sequence[CandidatePayload], rules: Sequence[OfferedRule]
    ) -> ClassifyOutcome:
        raise KeyboardInterrupt


def _config_with_state(tmp_path: Path) -> Path:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": str(tmp_path / "state.sqlite3"),
        "backup_dir": str(tmp_path / "backups"),
        "task_lock_dir": str(tmp_path / "locks"),
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"older-than": "1h"},
        {"llm": "always trash"},
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["backup", "trash"]
    return write_config(tmp_path / "cfg.yaml", data)


def _mailbox() -> FakeMailbox:
    raw = (
        "From: sender@example.com\r\n"
        "To: me@example.com\r\n"
        "Subject: Weekly Digest\r\n"
        f"Message-Id: {MESSAGE_ID}\r\n"
        "Date: Mon, 1 Jun 2026 08:00:00 +0000\r\n"
        "\r\n"
        "body text\r\n"
    ).encode()
    return FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1,
                raw=raw,
                flags=set(),
                internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                mailbox="INBOX",
            )
        ],
        uidvalidity={"INBOX": 1000, "Trash": 1000},
    )


def test_keyboard_interrupt_finishes_run_with_exit_code_3(tmp_path: Path) -> None:
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    outcome = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(),
        classifier_factory=lambda model_cfg: _InterruptingClassifier(),
    )

    assert outcome.exit_code == runner_mod.EXIT_INTERRUPTED == 3
    assert outcome.report_data is not None
    assert outcome.run_id is not None


def test_keyboard_interrupt_stores_a_finished_run_row(tmp_path: Path) -> None:
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    outcome = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(),
        classifier_factory=lambda model_cfg: _InterruptingClassifier(),
    )
    assert outcome.run_id is not None

    conn = state.open_database(cfg.settings.state_db)
    try:
        run = state.get_run(conn, outcome.run_id)
        assert run is not None
        assert run.ended_at is not None
        assert run.exit_code == runner_mod.EXIT_INTERRUPTED

        rows = conn.execute(
            "SELECT kind FROM audit_events "
            "WHERE run_id = ? AND kind = 'run_interrupted'",
            (outcome.run_id,),
        ).fetchall()
        assert len(rows) == 1
    finally:
        state.close_database(conn)


def test_keyboard_interrupt_releases_the_task_lock(tmp_path: Path) -> None:
    """A run interrupted mid-flight must not leave the next invocation of
    the same task lock-contended (spec §10)."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    first = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(),
        classifier_factory=lambda model_cfg: _InterruptingClassifier(),
    )
    assert first.exit_code == runner_mod.EXIT_INTERRUPTED

    second = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(),
        classifier_factory=lambda model_cfg: _InterruptingClassifier(),
    )
    assert second.exit_code != runner_mod.EXIT_TASK_RUNNING
