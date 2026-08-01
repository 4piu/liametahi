"""End-to-end regression tests for the sync-fix-brief's Fix C (candidate
retirement, Finding 2) and Fix D (skip a restored message, Finding 3),
run through the real orchestrator (`runner.run_task`) against
`FakeMailbox`/`FakeClassifier` so the whole scan -> evaluate -> execute
pipeline is exercised, not just the module each fix touches directly.
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


# --- Fix C: a completed trash retires the candidate row -------------------


def test_completed_trash_excludes_candidate_from_future_runs(tmp_path: Path) -> None:
    """sync-fix-brief Finding 2 / Fix C: once a message is actually
    trashed, it must not come back as a live candidate on a later run --
    not even to a zero-cost cache hit. The second run's classifier is
    scripted to raise on any call, so a regression here fails loudly."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mb = _mailbox()
    clf_1 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    outcome_1 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_1,
    )
    assert outcome_1.report_data is not None
    assert [item.status for item in outcome_1.report_data.items] == ["completed"]

    conn = state.open_database(cfg.settings.state_db)
    try:
        row = conn.execute(
            "SELECT retired_at, retired_reason FROM candidates"
        ).fetchone()
        assert row["retired_at"] is not None
        assert row["retired_reason"] == "moved"
    finally:
        state.close_database(conn)

    clf_2 = FakeClassifier([])  # any classify() call raises -- none expected
    outcome_2 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_2,
    )
    assert clf_2.call_count == 0
    assert outcome_2.report_data is not None
    # The retired candidate produces no result_items row at all this run
    # -- it is excluded from `_live_candidates` before evaluation, not
    # merely reported as a quiet no-op.
    assert outcome_2.report_data.items == ()


# --- Fix D: a restored message is skipped and reported, not re-trashed ---


def test_restored_message_is_skipped_not_re_trashed(tmp_path: Path) -> None:
    """sync-fix-brief Finding 3 / Fix D: a message trashed in run 1 and
    then moved back to its source mailbox (a user restore, from Trash or
    from any other client) must not be re-trashed by a cached positive
    decision with no model call and no signal to the user -- it must be
    reported `restored` and left alone."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mb = _mailbox()
    clf_1 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    outcome_1 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_1,
    )
    assert outcome_1.report_data is not None
    assert [item.status for item in outcome_1.report_data.items] == ["completed"]
    mb.select("Trash", readonly=True)
    assert mb.search_uids() == (1,)

    # The user restores the message back to INBOX (e.g. dragging it back
    # in webmail) -- same bytes, same Message-Id/INTERNALDATE, so the
    # fingerprint recomputed on the next scan is identical, but it gets a
    # brand new UID.
    mb.select("Trash", readonly=False)
    mb.move(1, "INBOX")
    mb.select("INBOX", readonly=True)
    assert mb.search_uids() == (2,)  # a new UID, never uid 1 again (RFC 3501)

    clf_2 = FakeClassifier([])  # a cache hit must mean no classify() call
    outcome_2 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_2,
    )
    assert clf_2.call_count == 0, "a cached decision must not re-ask the model"
    assert outcome_2.report_data is not None
    assert [item.status for item in outcome_2.report_data.items] == ["restored"]

    # Left alone: still in INBOX (the restore already emptied Trash by
    # moving the message out of it), and nothing new landed in Trash.
    mb.select("INBOX", readonly=True)
    assert mb.search_uids() == (2,)
    mb.select("Trash", readonly=True)
    assert mb.search_uids() == ()


def test_restored_message_is_retired_so_it_is_skipped_only_once(
    tmp_path: Path,
) -> None:
    """Skipping is a terminal decision, so the candidate is retired at
    the same time. Without that it would stay live and be re-evaluated
    and re-reported on every subsequent run forever -- the same
    never-retires waste Fix C exists to end, reached by a different path
    (skipping means `_reverify` never runs, so nothing can retire it as
    `vanished` either)."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mb = _mailbox()
    clf_1 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_1,
    )
    mb.select("Trash", readonly=False)
    mb.move(1, "INBOX")  # the user restores it

    # Run 2 skips it once, reporting `restored`.
    outcome_2 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: FakeClassifier([]),
    )
    assert outcome_2.report_data is not None
    assert [item.status for item in outcome_2.report_data.items] == ["restored"]

    # Run 3 must not see it at all -- not even as a quiet `restored` row.
    outcome_3 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: FakeClassifier([]),
    )
    assert outcome_3.report_data is not None
    assert [item.status for item in outcome_3.report_data.items] == []

    # And it is still sitting untouched in INBOX.
    mb.select("INBOX", readonly=True)
    assert mb.search_uids() == (2,)


def test_stale_candidate_for_an_already_gone_message_retires(tmp_path: Path) -> None:
    """The pre-Fix-C cohort: a candidate row left live by an older
    liametahi version, whose message really was trashed and is gone from
    the source mailbox. It must be retired rather than re-reported every
    run -- reached via the Fix D skip path, since the fingerprint carries
    a completed trash from the earlier run."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mb = _mailbox()
    clf_1 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_1,
    )

    # Simulate the older version's behaviour: un-retire the row, exactly
    # as it would have been left before migration 0003 existed. The
    # message itself stays where run 1 put it -- gone from INBOX.
    conn = state.open_database(cfg.settings.state_db)
    try:
        conn.execute("UPDATE candidates SET retired_at = NULL, retired_reason = NULL")
        assert (
            conn.execute(
                "SELECT COUNT(*) c FROM candidates WHERE retired_at IS NULL"
            ).fetchone()["c"]
            == 1
        )
    finally:
        state.close_database(conn)

    outcome_2 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: FakeClassifier([]),
    )
    assert outcome_2.report_data is not None
    assert [item.status for item in outcome_2.report_data.items] == ["restored"]

    conn = state.open_database(cfg.settings.state_db)
    try:
        row = conn.execute(
            "SELECT retired_at, retired_reason FROM candidates"
        ).fetchone()
        assert row["retired_at"] is not None
        assert row["retired_reason"] == "prior_trash"
    finally:
        state.close_database(conn)


def test_restored_message_reevaluate_does_not_override_fix_d(tmp_path: Path) -> None:
    """`--reevaluate` governs the LLM cache, a different concern (spec
    §9, §13) -- it must not become an escape hatch for Fix D. Even with
    a fresh (non-cached) classification that matches again, a restored
    message is still skipped."""
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    mb = _mailbox()
    clf_1 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_1,
    )
    mb.select("Trash", readonly=False)
    mb.move(1, "INBOX")

    clf_2 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    outcome_2 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=True,  # forces a fresh classification, bypassing the cache
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb,
        classifier_factory=lambda model_cfg: clf_2,
    )
    assert clf_2.call_count == 1, "--reevaluate still forces a fresh classification"
    assert outcome_2.report_data is not None
    assert [item.status for item in outcome_2.report_data.items] == ["restored"]
    # Not re-trashed a second time: Trash is still empty (the restore
    # already moved the one message that was ever in it back to INBOX).
    mb.select("Trash", readonly=True)
    assert mb.search_uids() == ()
