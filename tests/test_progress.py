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
        self.units: dict[str, str] = {}
        self._current: str | None = None

    def start(
        self, label: str, total: int | None = None, *, unit: str = "mails"
    ) -> None:
        self.phases.append((label, total))
        self._current = label
        self.counts.setdefault(label, 0)
        self.units[label] = unit

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
    # The unit is load-bearing: without it "1/3" next to a log line
    # reading "batch 2/3" reads as an index into the same sequence.
    assert "3/3 mails" in frames[-1]
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


def test_scan_counts_new_mail_and_flag_refreshes(tmp_path: Path) -> None:
    """The fetch is sliced so the counter can advance per server round
    trip rather than jumping 0 -> done. The slicing must not change what
    is fetched: `_MAX_UIDS_PER_FETCH` is 200, so 450 messages exercise
    three slices in each phase."""
    from liametahi import state
    from liametahi.imap_adapter import scan

    count = 450
    mb = _mailbox(count)
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        headers = ["FROM", "SUBJECT", "MESSAGE-ID"]

        first = RecordingProgress()
        result = scan(
            mb,
            conn,
            account_id=account_id,
            source_mailboxes=["INBOX"],
            fetch_headers=headers,
            max_new_mails=None,
            progress=first,
        )
        assert result.candidates_scanned == count, "slicing must not drop messages"
        assert first.phases == [("fetching new", count)]
        assert first.counts["fetching new"] == count

        # A second scan finds nothing new, but refreshes every known
        # message's flags -- counted the same way.
        second = RecordingProgress()
        again = scan(
            mb,
            conn,
            account_id=account_id,
            source_mailboxes=["INBOX"],
            fetch_headers=headers,
            max_new_mails=None,
            progress=second,
        )
        assert again.mailboxes[0].new_candidates == 0
        assert again.mailboxes[0].flags_refreshed == count
        assert second.phases == [("refreshing flags", count)]
        assert second.counts["refreshing flags"] == count
    finally:
        state.close_database(conn)


def test_classification_counts_mails_not_batches(tmp_path: Path) -> None:
    """Every other phase counts mails, so classification counting
    *batches* made its bar disagree with the `classifying batch 2/3` log
    line printed beside it -- same words, same denominator, different
    numbers, at the same instant. It counts mails like everything else.
    """
    path = _config(tmp_path)
    cfg = load_config(path)
    reporter = RecordingProgress()
    mails = 25  # > one batch of 10, so batches and mails cannot coincide
    clf = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={f"c{i}": [] for i in range(1, 11)}
            ),
            outcome_with_matches(
                matches_by_payload={f"c{i}": [] for i in range(1, 11)}
            ),
            outcome_with_matches(matches_by_payload={f"c{i}": [] for i in range(1, 6)}),
        ]
    )
    runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(mails),
        classifier_factory=lambda model_cfg: clf,
        progress=reporter,
    )
    totals = dict(reporter.phases)
    assert totals["classifying"] == mails, (
        f"total should be mails ({mails}), not batches: {totals['classifying']}"
    )
    assert reporter.counts["classifying"] == mails
    # And every phase reports the same unit, so no two bars in one run
    # can be counting different things under identical-looking numbers.
    assert set(reporter.units.values()) == {"mails"}, reporter.units
