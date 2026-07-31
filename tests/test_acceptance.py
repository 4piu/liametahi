"""Work Unit 4's named acceptance tests (spec section 14): 1, 5, 6, 7, 8,
10, 13. These exercise `policy.py`, `backup.py`, and `execute.py`
together against `FakeMailbox`, the way Unit 5's orchestrator will once
it lands -- entirely at the unit tier (no network, no Docker).
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liametahi import backup, execute, policy, state
from liametahi.domain import MessageKey, fingerprint
from tests.conftest import make_candidate
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<msg1@example.com>"


def _raw_message(subject: str = "Weekly Digest") -> bytes:
    return (
        "From: sender@example.com\r\n"
        "To: me@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Message-Id: {MESSAGE_ID}\r\n"
        "Date: Mon, 1 Jun 2026 08:00:00 +0000\r\n"
        "\r\n"
        "body text\r\n"
    ).encode()


def _setup_run(
    conn: sqlite3.Connection, *, account_id: int, run_id: str, task: str, dry_run: bool
) -> None:
    state.create_run(
        conn,
        run_id=run_id,
        task=task,
        account_id=account_id,
        model_name="m",
        provider="p",
        model_id="mi",
        dry_run=dry_run,
        reevaluate=False,
        fetch_headers=[],
        config_hash="h",
    )


def _rule(rule_id: str, actions: list[str], *, priority: int = 0) -> object:
    from liametahi.config import RuleConfig

    return RuleConfig.model_validate(
        {
            "id": rule_id,
            "priority": priority,
            "when": {"older-than": "1d"},
            "actions": actions,
        }
    )


def _mailbox_and_item(
    *,
    account_id: int,
    candidate_id: int,
    rule: object,
    uid: int = 1,
    trash_mailbox: str | None = "Trash",
    internaldate: datetime = datetime(2026, 6, 1, tzinfo=UTC),
) -> tuple[FakeMailbox, execute.ExecutionItem]:
    raw = _raw_message()
    mb = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=uid,
                raw=raw,
                flags=set(),
                internaldate=internaldate,
                mailbox="INBOX",
            )
        ],
        uidvalidity={"INBOX": 1000, "Trash": 1000, "Archive": 1000},
    )
    key = MessageKey(account_id, "INBOX", 1000, uid)
    fp = fingerprint(
        message_id=MESSAGE_ID,
        internaldate=internaldate,
        rfc822_size=len(raw),
        from_address=None,
        subject=None,
    )
    actions = policy.resolve_actions(rule, trash_mailbox=trash_mailbox)  # type: ignore[arg-type]
    item = execute.ExecutionItem(
        candidate_id=candidate_id,
        key=key,
        fingerprint=fp,
        message_id=MESSAGE_ID,
        winning_rule=rule.id,  # type: ignore[attr-defined]
        actions=actions,
    )
    return mb, item


def _insert_candidate(
    conn: sqlite3.Connection, *, account_id: int, uid: int = 1
) -> int:
    candidate = make_candidate(
        account_id=account_id,
        uid=uid,
        message_id=MESSAGE_ID,
        internaldate=datetime(2026, 6, 1, tzinfo=UTC),
    )
    return state.upsert_candidate(conn, candidate)


# --- Acceptance test 1 -----------------------------------------------


def test_acceptance_01_backup_then_trash_and_dry_run_does_neither(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        rule = _rule("old-weekly-digest", ["backup", "trash"])
        backup_dir = tmp_path / "backups"

        # --dry-run performs neither backup nor trash.
        candidate_id_dry = _insert_candidate(conn, account_id=account_id)
        run_dry = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_dry, task="t", dry_run=True)
        mb_dry, item_dry = _mailbox_and_item(
            account_id=account_id, candidate_id=candidate_id_dry, rule=rule
        )
        summary_dry = execute.execute_items(
            conn,
            mailbox=mb_dry,
            items=[item_dry],
            run_id=run_dry,
            backup_dir=backup_dir,
            max_actions_per_run=50,
            dry_run=True,
            fail_fast=False,
        )
        assert summary_dry.outcomes[0].status == "dry_run"
        mb_dry.select("INBOX", readonly=True)
        assert mb_dry.search_uids() == (1,)  # message untouched
        assert conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"] == 0

        # A normal run performs backup, then trash.
        candidate_id = _insert_candidate(conn, account_id=account_id, uid=2)
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id, task="t", dry_run=False)
        mb, item = _mailbox_and_item(
            account_id=account_id, candidate_id=candidate_id, rule=rule, uid=2
        )
        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=backup_dir,
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "completed"
        assert conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"] == 1
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == ()
        mb.select("Trash", readonly=True)
        assert mb.search_uids() == (1,)
    finally:
        state.close_database(conn)


# --- Acceptance test 5 -----------------------------------------------


def test_acceptance_05_backup_and_manifest_exist_before_server_move(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        rule = _rule("digest", ["backup", "trash"])
        candidate_id = _insert_candidate(conn, account_id=account_id)
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id, task="t", dry_run=False)
        backup_dir = tmp_path / "backups"
        mb, item = _mailbox_and_item(
            account_id=account_id, candidate_id=candidate_id, rule=rule
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=backup_dir,
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "completed"

        # The move actually happened...
        mb.select("Trash", readonly=True)
        assert mb.search_uids() == (1,)

        # ...and, by construction (trash's requires_prior_backup gate in
        # execute._run_action_sequence), it could only have happened
        # after a checksummed .eml and manifest row existed: the
        # sequence aborts *before* calling mailbox.move() whenever no
        # backup action has completed. Confirm both directly too.
        attempts = conn.execute(
            "SELECT seq, action, state, backup_id FROM action_attempts "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        assert [(r["seq"], r["action"], r["state"]) for r in attempts] == [
            (0, "backup", "completed"),
            (1, "trash", "completed"),
        ]
        backup_id = attempts[0]["backup_id"]
        assert backup_id is not None
        row = state.get_backup(conn, backup_id)
        assert row is not None
        eml_path = backup_dir / str(row["relative_path"])
        assert eml_path.is_file()
        assert eml_path.read_bytes() == _raw_message()
        import hashlib

        assert hashlib.sha256(eml_path.read_bytes()).hexdigest() == row["sha256"]
    finally:
        state.close_database(conn)


# --- Acceptance test 6 -----------------------------------------------


def test_acceptance_06_simulated_backup_write_failure_leaves_message_untouched(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        rule = _rule("digest", ["backup", "trash"])
        candidate_id = _insert_candidate(conn, account_id=account_id)
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id, task="t", dry_run=False)
        mb, item = _mailbox_and_item(
            account_id=account_id, candidate_id=candidate_id, rule=rule
        )

        # Simulate a backup-write failure: the backup directory path is
        # occupied by a regular file, so ensure_backup_dir()'s mkdir()
        # fails and write_verified_backup() raises BackupError.
        broken_backup_dir = tmp_path / "not_a_directory"
        broken_backup_dir.write_text("occupied")

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=broken_backup_dir,
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "failed"

        attempts = conn.execute(
            "SELECT seq, action, state FROM action_attempts "
            "WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        assert attempts[0]["action"] == "backup"
        assert attempts[0]["state"] == "failed"
        assert attempts[1]["action"] == "trash"
        assert attempts[1]["state"] == "skipped"

        # The mailbox was never touched: the message is still in INBOX.
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (1,)
        assert conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"] == 0
    finally:
        state.close_database(conn)


# --- Acceptance test 7 -----------------------------------------------


def test_acceptance_07_rerun_produces_no_duplicate_backup(tmp_path: Path) -> None:
    """The first half of test 7: re-running a completed backup action
    (e.g. a retried step, or a re-run whose scan re-surfaced the same
    key) commits no second manifest row (contracts section 4's
    uniqueness constraint plus `write_verified_backup`'s dedup path)."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id, task="t", dry_run=False)
        mb = FakeMailbox(
            messages=[
                _StoredMessage(
                    uid=1,
                    raw=_raw_message(),
                    flags=set(),
                    internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                    mailbox="INBOX",
                )
            ],
            uidvalidity={"INBOX": 1000},
        )
        mb.select("INBOX", readonly=False)
        key = MessageKey(account_id, "INBOX", 1000, 1)

        first = backup.write_verified_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            key=key,
            fingerprint="fp",
            message_id=MESSAGE_ID,
            original_flags=set(),
            internaldate=datetime(2026, 6, 1, tzinfo=UTC),
            run_id=run_id,
        )
        second = backup.write_verified_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            key=key,
            fingerprint="fp",
            message_id=MESSAGE_ID,
            original_flags=set(),
            internaldate=datetime(2026, 6, 1, tzinfo=UTC),
            run_id=run_id,
        )
        assert first.backup_id == second.backup_id
        assert conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()["c"] == 1
    finally:
        state.close_database(conn)


def test_acceptance_07_in_flight_row_closed_by_reconcile(tmp_path: Path) -> None:
    """The second half of test 7: an action row left `in_flight` by a
    killed process is closed out by the reconcile pass on the next run,
    and its stale key claim is released."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        crashed_run = state.new_run_id()
        _setup_run(
            conn, account_id=account_id, run_id=crashed_run, task="t", dry_run=False
        )
        candidate_id = _insert_candidate(conn, account_id=account_id)
        result_id = state.insert_result_item(
            conn,
            run_id=crashed_run,
            candidate_id=candidate_id,
            status="pending",
            winning_rule="r",
        )
        key = MessageKey(account_id, "INBOX", 1000, 1)
        assert state.claim_key(conn, key=key, run_id=crashed_run) is True
        attempt_id = state.insert_action_attempt(
            conn,
            run_id=crashed_run,
            result_id=result_id,
            seq=0,
            action="trash",
            state="pending",
            key=key,
            fingerprint="fp",
        )
        state.update_action_attempt_state(
            conn, attempt_id=attempt_id, state="in_flight"
        )
        # The process "crashes" here: the move already succeeded on the
        # server (the message is gone from INBOX) but the DB commit
        # marking the attempt completed never happened.
        mb = FakeMailbox(uidvalidity={"INBOX": 1000})  # INBOX is empty: message moved

        summary = execute.reconcile_task(conn, task="t", mailbox=mb)
        assert summary.rows_closed == 1
        row = conn.execute(
            "SELECT state FROM action_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        assert row["state"] == "completed"
        assert state.get_key_claim_holder(conn, key=key) is None
        assert state.open_action_attempts(conn, task="t") == []
    finally:
        state.close_database(conn)


# --- Acceptance test 8 -----------------------------------------------


def test_acceptance_08_restore_verifies_checksum_and_appends_original_metadata(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id, task="t", dry_run=False)
        original_internaldate = datetime(2026, 5, 20, 3, 4, 5, tzinfo=UTC)
        mb = FakeMailbox(
            messages=[
                _StoredMessage(
                    uid=1,
                    raw=_raw_message(),
                    flags={"\\Seen", "\\Flagged"},
                    internaldate=original_internaldate,
                    mailbox="INBOX",
                )
            ],
            uidvalidity={"INBOX": 1000},
        )
        mb.select("INBOX", readonly=False)
        key = MessageKey(account_id, "INBOX", 1000, 1)
        backed_up = backup.write_verified_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            key=key,
            fingerprint="fp",
            message_id=MESSAGE_ID,
            original_flags={"\\Seen", "\\Flagged"},
            internaldate=original_internaldate,
            run_id=run_id,
        )

        result = backup.restore_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            backup_id=backed_up.backup_id,
            destination_mailbox="INBOX",
            dry_run=False,
        )
        assert result.appended is True
        (appended_mailbox, appended_raw, appended_flags, appended_date) = mb.appended[0]
        assert appended_mailbox == "INBOX"
        assert appended_raw == _raw_message()
        assert set(appended_flags) == {"\\Seen", "\\Flagged"}
        assert appended_date == original_internaldate

        # Tampering with the backup file must fail checksum verification.
        eml_path = tmp_path / "backups" / backed_up.relative_path
        original_bytes = eml_path.read_bytes()
        eml_path.write_bytes(b"corrupted")
        try:
            raised = False
            try:
                backup.restore_backup(
                    conn,
                    mailbox=mb,
                    backup_dir=tmp_path / "backups",
                    backup_id=backed_up.backup_id,
                    destination_mailbox="INBOX",
                    dry_run=False,
                )
            except backup.RestoreError:
                raised = True
            assert raised
        finally:
            eml_path.write_bytes(original_bytes)
    finally:
        state.close_database(conn)


# --- Acceptance test 10 -----------------------------------------------


def test_acceptance_10_two_concurrent_runs_only_one_claims_and_mutates(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        candidate_id = _insert_candidate(conn, account_id=account_id)
        rule = _rule("archive", ["move_to:Archive"])
        mb, item = _mailbox_and_item(
            account_id=account_id,
            candidate_id=candidate_id,
            rule=rule,
            trash_mailbox=None,
        )

        run_a = state.new_run_id()
        _setup_run(
            conn, account_id=account_id, run_id=run_a, task="task-a", dry_run=False
        )
        run_b = state.new_run_id()
        _setup_run(
            conn, account_id=account_id, run_id=run_b, task="task-b", dry_run=False
        )

        # Task A claims the key first and is still "mid-run" (has not
        # released it yet) when task B's execute phase reaches the same
        # message key.
        assert state.claim_key(conn, key=item.key, run_id=run_a) is True

        summary_b = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_b,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary_b.outcomes[0].status == "claimed_by_other_run"
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (1,)  # untouched by B

        # Task A finishes its own claim window and actually executes;
        # only it produced a mutation.
        state.release_key(conn, key=item.key)
        summary_a = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_a,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary_a.outcomes[0].status == "completed"
        mb.select("Archive", readonly=True)
        assert mb.search_uids() == (1,)

        # Both runs produced a reportable result_items row.
        rows = conn.execute(
            "SELECT run_id, status FROM result_items "
            "WHERE candidate_id = ? ORDER BY run_id",
            (candidate_id,),
        ).fetchall()
        assert {r["run_id"] for r in rows} == {run_a, run_b}
        statuses = {r["run_id"]: r["status"] for r in rows}
        assert statuses[run_b] == "claimed_by_other_run"
        # status derives from action_attempts children, see execute.py
        assert statuses[run_a] == "pending"
    finally:
        state.close_database(conn)


# --- Acceptance test 13 -----------------------------------------------


def test_acceptance_13_two_rules_match_only_higher_priority_runs_other_shadowed() -> (
    None
):
    from liametahi.config import RuleConfig

    high_priority = RuleConfig.model_validate(
        {
            "id": "old-weekly-digest",
            "priority": 100,
            "when": {"older-than": "30d"},
            "actions": ["backup", "trash"],
        }
    )
    low_priority = RuleConfig.model_validate(
        {
            "id": "stale-updates",
            "priority": 10,
            "when": {"older-than": "14d"},
            "actions": ["move_to:Archive"],
        }
    )
    matches = [
        policy.MatchedRule("stale-updates", priority=10, config_order=1),
        policy.MatchedRule("old-weekly-digest", priority=100, config_order=0),
    ]
    decision = policy.decide(
        protected=False,
        matches=matches,
        rules_by_id={
            "old-weekly-digest": high_priority,
            "stale-updates": low_priority,
        },
        trash_mailbox="Trash",
    )
    assert decision.status == "matched"
    assert decision.winning_rule == "old-weekly-digest"
    assert decision.shadowed == ("stale-updates",)
    # Only the winner's full action list resolves; the loser's actions
    # (a mere move) never appear and never run.
    assert [a.action for a in decision.actions] == ["backup", "trash"]
