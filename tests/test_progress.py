"""Tests for `liametahi.progress` and its wiring through a real run.

The load-bearing property is the negative one: a non-interactive run must
be byte-for-byte what it was before progress reporting existed, because
cron mails that output and carriage returns in it would be actively
harmful. `NullProgress` is therefore the default everywhere, and
`cli.py` only substitutes `TtyProgress` when stderr is a terminal.
"""

import io
from datetime import UTC, datetime
from pathlib import Path

from liametahi import runner as runner_mod
from liametahi.config import load_config
from liametahi.progress import NullProgress, TtyProgress
from tests.conftest import make_config_dict, write_config
from tests.fakes.fake_classifier import FakeClassifier, outcome_with_matches
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<p1@example.com>"


class RecordingProgress:
    """Captures the phase labels and per-phase counts a run drives."""

    def __init__(self) -> None:
        self.phases: list[tuple[str, int | None]] = []
        self.counts: dict[str, int] = {}
        self._current: str | None = None

    def start(self, label: str, total: int | None = None) -> None:
        self.phases.append((label, total))
        self._current = label
        self.counts.setdefault(label, 0)

    def advance(self, n: int = 1) -> None:
        if self._current is not None:
            self.counts[self._current] += n

    def stop(self) -> None:
        self._current = None


# --- TtyProgress rendering ------------------------------------------------


def test_tty_progress_renders_a_bar_and_erases_itself() -> None:
    buf = io.StringIO()
    reporter = TtyProgress(buf)
    reporter.start("classifying", total=3)
    for _ in range(3):
        reporter.advance()
    reporter.stop()
    reporter.close()

    out = buf.getvalue()
    frames = [f for f in out.split("\r") if "classifying" in f]
    assert frames, out
    assert "3/3" in frames[-1]
    assert "#" in frames[-1]
    # Leaves the line clean for whatever prints next.
    assert out.endswith("\r\x1b[K")


def test_tty_progress_clear_is_idempotent_and_safe_after_close() -> None:
    buf = io.StringIO()
    reporter = TtyProgress(buf)
    reporter.start("scanning")
    reporter.clear()
    reporter.clear()  # must not double-write or raise
    reporter.close()
    reporter.clear()  # after close, still safe
    assert "scanning" in buf.getvalue()


def test_null_progress_writes_nothing_and_accepts_every_call() -> None:
    reporter = NullProgress()
    reporter.start("anything", total=10)
    reporter.advance(5)
    reporter.stop()  # no stream, no output, no error


# --- Wiring through a real run -------------------------------------------


def _config(tmp_path: Path) -> Path:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "error",
        "state_db": str(tmp_path / "state.sqlite3"),
        "backup_dir": str(tmp_path / "backups"),
        "task_lock_dir": str(tmp_path / "locks"),
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"older-than": "1h"},
        {"llm": "junk?"},
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["backup", "trash"]
    return write_config(tmp_path / "cfg.yaml", data)


def _mailbox(count: int) -> FakeMailbox:
    return FakeMailbox(
        messages=[
            _StoredMessage(
                uid=i,
                raw=(
                    f"From: s{i}@example.com\r\nTo: me@example.com\r\n"
                    f"Subject: S{i}\r\nMessage-Id: <m{i}@x>\r\n"
                    "Date: Mon, 1 Jun 2026 08:00:00 +0000\r\n\r\nbody\r\n"
                ).encode(),
                flags=set(),
                internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                mailbox="INBOX",
            )
            for i in range(1, count + 1)
        ],
        uidvalidity={"INBOX": 1000, "Trash": 1000},
    )


def test_a_run_reports_its_long_phases(tmp_path: Path) -> None:
    """The phases a human waits on -- classification and execution --
    must actually drive the reporter, with a total to size the bar."""
    path = _config(tmp_path)
    cfg = load_config(path)
    reporter = RecordingProgress()
    clf = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={f"c{i}": ["old-weekly-digest"] for i in range(1, 4)}
            )
        ]
    )

    outcome = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(3),
        classifier_factory=lambda model_cfg: clf,
        progress=reporter,
    )
    assert outcome.exit_code == 0

    labels = [label for label, _ in reporter.phases]
    assert "classifying" in labels
    assert "executing" in labels
    # Every phase declared a total, so the bar is never an unbounded
    # spinner where a real count was available.
    assert all(total is not None for _, total in reporter.phases), reporter.phases
    # And every phase ran to completion rather than stalling partway.
    for label, total in reporter.phases:
        assert reporter.counts[label] == total, (label, reporter.counts, total)


def test_a_run_without_a_reporter_still_works(tmp_path: Path) -> None:
    """`progress` is optional everywhere; omitting it is the cron path."""
    path = _config(tmp_path)
    cfg = load_config(path)
    clf = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    outcome = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(1),
        classifier_factory=lambda model_cfg: clf,
    )
    assert outcome.exit_code == 0
    assert outcome.report_data is not None
