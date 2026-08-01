"""Tests for `runner._run_body_excerpt` (spec §4.2 step 7, §5.1).

There is deliberately no model-level `body_excerpt.enabled` switch
alongside a rule's own `allow_body_excerpt` -- the rule opting in is
already the enable signal, so escalation must fire on that alone.
"""

from datetime import UTC, datetime
from pathlib import Path

from liametahi import runner as runner_mod
from liametahi import state
from liametahi.config import load_config
from tests.conftest import make_config_dict, write_config
from tests.fakes.fake_classifier import FakeClassifier, outcome_with_matches
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
    data["tasks"]["inbox-cleanup"]["rules"][0] = {
        "id": "maybe-archive",
        "priority": 0,
        "when": [{"llm": "should this be archived?"}],
        "allow_body_excerpt": True,
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


def test_rule_opt_in_alone_triggers_escalation(tmp_path: Path) -> None:
    """A rule's `allow_body_excerpt: true` is sufficient by itself:
    no model-level field needs to be set for the excerpt re-classify pass
    to run when the model reports it's unsure."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    clf = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={"c1": []}, needs_content={"c1": True}
            ),
            outcome_with_matches(matches_by_payload={"c1": ["maybe-archive"]}),
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

    assert clf.call_count == 2, "the escalated (excerpt) call never happened"
    assert outcome.report_data is not None
    winning_rules = [item.winning_rule for item in outcome.report_data.items]
    assert "maybe-archive" in winning_rules


def test_escalated_verdict_is_cached_so_a_rerun_asks_nothing(tmp_path: Path) -> None:
    """The whole point of caching an escalated verdict: a message the
    model was unsure about must not cost a metadata call, a body fetch
    AND its own un-batched excerpt call on every subsequent run.

    Run 1 escalates. Run 2's classifier raises on any call at all, so a
    regression here fails loudly rather than merely getting slower.
    """
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mailbox = _mailbox()
    clf_1 = FakeClassifier(
        [
            # metadata pass: unsure
            outcome_with_matches(
                matches_by_payload={"c1": []}, needs_content={"c1": True}
            ),
            # excerpt pass: a confident verdict
            outcome_with_matches(matches_by_payload={"c1": ["maybe-archive"]}),
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
    assert clf_1.call_count == 2, "run 1 should do a metadata pass then escalate"
    assert outcome_1.report_data is not None
    assert [i.winning_rule for i in outcome_1.report_data.items] == ["maybe-archive"]

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
    assert clf_2.call_count == 0, "the escalated verdict must be reused, not re-asked"
    assert outcome_2.report_data is not None
    # ...and reused as the *same* verdict, not merely skipped.
    assert [i.winning_rule for i in outcome_2.report_data.items] == ["maybe-archive"]


def test_unsure_at_excerpt_level_is_still_not_cached(tmp_path: Path) -> None:
    """A model that defers *again* at excerpt level has still not
    decided anything, so nothing may be cached -- otherwise a deferral
    would be frozen into a permanent "no" the way spec §5.3 forbids."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mailbox = _mailbox()
    clf_1 = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={"c1": []}, needs_content={"c1": True}
            ),
            outcome_with_matches(
                matches_by_payload={"c1": []}, needs_content={"c1": True}
            ),
        ]
    )
    runner_mod.run_task(
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
    assert clf_1.call_count == 2

    conn = state.open_database(cfg.settings.state_db)
    try:
        cached = conn.execute("SELECT COUNT(*) c FROM llm_decision_cache").fetchone()
        assert cached["c"] == 0, "a deferral must never be cached as a decision"
    finally:
        state.close_database(conn)
