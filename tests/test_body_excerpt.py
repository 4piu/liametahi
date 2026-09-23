"""Tests for the `include_body` processor path.

This replaces the old dynamic "the model reports unsure,
fetch a body excerpt and re-ask" escalation with a static per-processor
switch: `include_body: true` means this processor's request always
carries a body excerpt, decided purely from which processors a
still-undecided rule references (`runner._candidates_needing_excerpt`),
never from a model response. There is no `needs_content`/"unsure"
concept left at all.
"""

from datetime import UTC, datetime
from pathlib import Path

from liametahi import runner as runner_mod
from liametahi import state
from liametahi.config import load_config
from liametahi.rules import ProcessorAnswer
from tests.conftest import make_config_dict, write_config
from tests.fakes.fake_classifier import FakeClassifier, outcome_with_answers
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<msg1@example.com>"


def _config_with_state(tmp_path: Path) -> Path:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": str(tmp_path / "state.sqlite3"),
        "backup_dir": str(tmp_path / "backups"),
        "task_lock_dir": str(tmp_path / "locks"),
    }
    data["processors"] = {
        "maybe-archive": {
            "model": "local",
            "type": "noul",
            "include_body": True,
            "instructions": "should this be archived?",
        },
    }
    data["tasks"]["inbox-cleanup"]["rules"][0] = {
        "when": [{"processor": "maybe-archive.value >= 0.5"}],
        "actions": ["move_to:Archive"],
    }
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
        uidvalidity={"INBOX": 1000, "Archive": 1000},
    )


def test_include_body_processor_gets_the_excerpt_fetched_up_front(
    tmp_path: Path,
) -> None:
    """A single classify() call answers the body-requiring processor --
    there is no separate "metadata pass, then escalate" round trip any
    more; the excerpt is fetched before the only ask happens."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    clf = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c1": {"maybe-archive": ProcessorAnswer(1.0, None)}}
            )
        ]
    )

    outcome = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=True,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: _mailbox(),
        classifier_factory=lambda model_cfg: clf,
    )

    assert clf.call_count == 1
    # The one call must have carried the body excerpt (the
    # excerpt-level field), proving the fetch actually happened before
    # the ask rather than being skipped.
    payloads, _processors = clf.calls[0]
    assert "excerpt" in payloads[0].fields
    assert outcome.report_data is not None
    winning_rules = [item.winning_rule for item in outcome.report_data.items]
    assert "rule #1 of 1" in winning_rules


def test_include_body_verdict_is_cached_so_a_rerun_asks_nothing(
    tmp_path: Path,
) -> None:
    """The whole point of caching a body-backed verdict: a message
    already classified with its excerpt must not cost a second body
    fetch and model call on every subsequent run."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mailbox = _mailbox()
    clf_1 = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c1": {"maybe-archive": ProcessorAnswer(1.0, None)}}
            )
        ]
    )
    outcome_1 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=True,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mailbox,
        classifier_factory=lambda model_cfg: clf_1,
    )
    assert clf_1.call_count == 1
    assert outcome_1.report_data is not None
    assert [i.winning_rule for i in outcome_1.report_data.items] == ["rule #1 of 1"]

    clf_2 = FakeClassifier([])  # any classify() call raises
    outcome_2 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=True,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mailbox,
        classifier_factory=lambda model_cfg: clf_2,
    )
    assert clf_2.call_count == 0, "the cached verdict must be reused, not re-asked"
    assert outcome_2.report_data is not None
    assert [i.winning_rule for i in outcome_2.report_data.items] == ["rule #1 of 1"]


def test_a_failed_classify_call_is_not_cached(tmp_path: Path) -> None:
    """A genuine transport/parse failure is not a decision -- it must
    stay uncached so the next run retries it, unlike a resolved answer
    ("genuine transport/parse failure... is uncached")."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mailbox = _mailbox()
    clf = FakeClassifier([RuntimeError("simulated transport failure")])
    runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=True,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mailbox,
        classifier_factory=lambda model_cfg: clf,
    )
    assert clf.call_count == 1

    conn = state.open_database(cfg.settings.state_db)
    try:
        cached = conn.execute("SELECT COUNT(*) c FROM llm_decision_cache").fetchone()
        assert cached["c"] == 0, "a failed call must never be cached as a decision"
    finally:
        state.close_database(conn)
