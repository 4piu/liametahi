"""Tests for `liametahi.state` (contracts §4)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from liametahi import state
from liametahi.domain import MessageKey
from tests.conftest import make_candidate


def test_open_database_creates_file_and_applies_pragmas(tmp_path: Path) -> None:
    db_path = tmp_path / "sub" / "state.sqlite3"
    conn = state.open_database(db_path)
    try:
        assert db_path.exists()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        state.close_database(conn)


def test_open_database_sets_file_mode_0600(tmp_path: Path) -> None:
    import stat

    db_path = tmp_path / "state.sqlite3"
    conn = state.open_database(db_path)
    state.close_database(conn)
    mode = stat.S_IMODE(db_path.stat().st_mode)
    assert mode == 0o600


def test_open_database_creates_all_tables(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {row["name"] for row in rows}
        expected = {
            "schema_version",
            "accounts",
            "mailbox_state",
            "candidates",
            "runs",
            "result_items",
            "classifications",
            "llm_decision_cache",
            "key_claims",
            "backups",
            "action_attempts",
            "audit_events",
        }
        assert expected.issubset(names)
        version_row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
        assert version_row["v"] == 1
    finally:
        state.close_database(conn)


def test_reopening_database_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    conn1 = state.open_database(db_path)
    state.close_database(conn1)
    conn2 = state.open_database(db_path)  # must not re-run migration 1
    try:
        count = conn2.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()
        assert count["c"] == 1
    finally:
        state.close_database(conn2)


def test_newer_schema_version_raises_clear_error(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite3"
    conn = state.open_database(db_path)
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (99, ?)",
        (datetime.now(UTC).isoformat(),),
    )
    state.close_database(conn)

    with pytest.raises(state.SchemaVersionError):
        state.open_database(db_path)


def test_audit_events_are_append_only(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        event_id = state.append_audit_event(conn, run_id=None, kind="test")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE audit_events SET kind = 'changed' WHERE event_id = ?",
                (event_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM audit_events WHERE event_id = ?", (event_id,))
    finally:
        state.close_database(conn)


def test_account_upsert_and_lookup(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(
            conn, name="personal", host="imap.example.com", username="me@example.com"
        )
        assert state.get_account_id(conn, "personal") == account_id
        # upsert again with changed host is idempotent on account_id
        account_id2 = state.upsert_account(
            conn, name="personal", host="imap2.example.com", username="me@example.com"
        )
        assert account_id == account_id2
    finally:
        state.close_database(conn)


def test_mailbox_uidvalidity_round_trip(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        assert (
            state.get_mailbox_uidvalidity(conn, account_id=account_id, mailbox="INBOX")
            is None
        )
        state.record_mailbox_uidvalidity(
            conn, account_id=account_id, mailbox="INBOX", uidvalidity=1000
        )
        assert (
            state.get_mailbox_uidvalidity(conn, account_id=account_id, mailbox="INBOX")
            == 1000
        )
        state.record_mailbox_uidvalidity(
            conn, account_id=account_id, mailbox="INBOX", uidvalidity=1001
        )
        assert (
            state.get_mailbox_uidvalidity(conn, account_id=account_id, mailbox="INBOX")
            == 1001
        )
    finally:
        state.close_database(conn)


def test_candidate_upsert_and_round_trip(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        candidate = make_candidate(account_id=account_id)
        candidate_id = state.upsert_candidate(conn, candidate)
        fetched = state.get_candidate(conn, key=candidate.key)
        assert fetched is not None
        fetched_id, fetched_candidate = fetched
        assert fetched_id == candidate_id
        assert fetched_candidate.fingerprint == candidate.fingerprint
        assert fetched_candidate.from_address == candidate.from_address
        assert fetched_candidate.flags == candidate.flags
        assert fetched_candidate.recipients == candidate.recipients

        # Re-scan is idempotent: same key upserts, does not duplicate.
        candidate_id_2 = state.upsert_candidate(conn, candidate)
        assert candidate_id_2 == candidate_id
        rows = conn.execute("SELECT COUNT(*) AS c FROM candidates").fetchone()
        assert rows["c"] == 1
    finally:
        state.close_database(conn)


def test_find_candidates_by_fingerprint(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        candidate = make_candidate(account_id=account_id, fingerprint="fp-abc")
        state.upsert_candidate(conn, candidate)
        found = state.find_candidates_by_fingerprint(
            conn, account_id=account_id, fingerprint="fp-abc"
        )
        assert len(found) == 1
        assert found[0][1].fingerprint == "fp-abc"
    finally:
        state.close_database(conn)


def test_run_create_finish_get_list(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        run_id = state.new_run_id()
        assert run_id.startswith("run_")
        assert len(run_id) == len("run_") + 26

        state.create_run(
            conn,
            run_id=run_id,
            task="inbox-cleanup",
            account_id=account_id,
            model_name="local",
            provider="openai_compatible",
            model_id="qwen",
            dry_run=True,
            reevaluate=False,
            fetch_headers=["FROM", "SUBJECT"],
            config_hash="deadbeef",
        )
        run = state.get_run(conn, run_id)
        assert run is not None
        assert run.dry_run is True
        assert run.fetch_headers == ("FROM", "SUBJECT")
        assert run.exit_code is None

        state.finish_run(
            conn,
            run_id=run_id,
            exit_code=0,
            candidates_scanned=5,
            llm_calls=2,
            structured_output_level="json_schema",
        )
        finished = state.get_run(conn, run_id)
        assert finished is not None
        assert finished.exit_code == 0
        assert finished.candidates_scanned == 5
        assert finished.ended_at is not None

        runs = state.list_runs(conn, task="inbox-cleanup")
        assert len(runs) == 1
        assert runs[0].run_id == run_id
        assert state.list_runs(conn, task="other-task") == []
    finally:
        state.close_database(conn)


def test_backup_id_format() -> None:
    backup_id = state.new_backup_id()
    assert backup_id.startswith("bkp_")
    assert len(backup_id) == len("bkp_") + 26


def test_key_claims_claim_release_and_no_steal(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        run_a = state.new_run_id()
        run_b = state.new_run_id()
        for run_id in (run_a, run_b):
            state.create_run(
                conn,
                run_id=run_id,
                task="t",
                account_id=account_id,
                model_name="m",
                provider="p",
                model_id="mi",
                dry_run=False,
                reevaluate=False,
                fetch_headers=[],
                config_hash="h",
            )
        key = MessageKey(
            account_id=account_id, mailbox="INBOX", uidvalidity=1000, uid=1
        )

        assert state.claim_key(conn, key=key, run_id=run_a) is True
        # A second, still-active run must not steal the claim.
        assert state.claim_key(conn, key=key, run_id=run_b) is False
        assert state.get_key_claim_holder(conn, key=key) == run_a

        state.release_key(conn, key=key)
        assert state.get_key_claim_holder(conn, key=key) is None

        # Once released, another run can claim it.
        assert state.claim_key(conn, key=key, run_id=run_b) is True
        assert state.get_key_claim_holder(conn, key=key) == run_b
    finally:
        state.close_database(conn)


def test_result_item_and_classification_and_action_attempt_round_trip(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        run_id = state.new_run_id()
        state.create_run(
            conn,
            run_id=run_id,
            task="t",
            account_id=account_id,
            model_name="m",
            provider="p",
            model_id="mi",
            dry_run=False,
            reevaluate=False,
            fetch_headers=[],
            config_hash="h",
        )
        candidate = make_candidate(account_id=account_id)
        candidate_id = state.upsert_candidate(conn, candidate)

        result_id = state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=candidate_id,
            status="completed",
            winning_rule="rule-1",
        )
        assert result_id > 0

        classification_id = state.insert_classification(
            conn,
            run_id=run_id,
            candidate_id=candidate_id,
            input_level="metadata",
            input_hash="hash1",
            offered_rules=["rule-1"],
            matches=["rule-1"],
            needs_content=False,
            reason="ok",
            valid=True,
            error=None,
            latency_ms=42,
        )
        assert classification_id > 0

        attempt_id = state.insert_action_attempt(
            conn,
            run_id=run_id,
            result_id=result_id,
            seq=0,
            action="backup",
            state="pending",
            key=candidate.key,
            fingerprint=candidate.fingerprint,
        )
        open_rows = state.open_action_attempts(conn, task="t")
        assert len(open_rows) == 1
        assert open_rows[0]["attempt_id"] == attempt_id

        state.update_action_attempt_state(
            conn, attempt_id=attempt_id, state="completed", finished=True
        )
        assert state.open_action_attempts(conn, task="t") == []
    finally:
        state.close_database(conn)


def test_negative_decision_cache_round_trip(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        assert (
            state.get_cached_decision(
                conn,
                account_id=account_id,
                fingerprint="fp",
                rule_id="r",
                rule_text_hash="th",
                input_hash="ih",
            )
            is None
        )
        state.record_negative_decision(
            conn,
            account_id=account_id,
            fingerprint="fp",
            rule_id="r",
            rule_text_hash="th",
            input_hash="ih",
            model_id="m",
        )
        cached = state.get_cached_decision(
            conn,
            account_id=account_id,
            fingerprint="fp",
            rule_id="r",
            rule_text_hash="th",
            input_hash="ih",
        )
        assert cached is not None
        assert cached["model_id"] == "m"
    finally:
        state.close_database(conn)


def test_backup_insert_and_restore(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        run_id = state.new_run_id()
        state.create_run(
            conn,
            run_id=run_id,
            task="t",
            account_id=account_id,
            model_name="m",
            provider="p",
            model_id="mi",
            dry_run=False,
            reevaluate=False,
            fetch_headers=[],
            config_hash="h",
        )
        backup_id = state.new_backup_id()
        state.insert_backup(
            conn,
            backup_id=backup_id,
            account_id=account_id,
            mailbox="INBOX",
            uidvalidity=1000,
            uid=1,
            fingerprint="fp",
            message_id="<m@x>",
            sha256="deadbeef",
            byte_count=100,
            relative_path="de/ad/deadbeef.eml",
            original_mailbox="INBOX",
            original_flags=["\\Seen"],
            internaldate="2026-06-01T00:00:00Z",
            run_id=run_id,
        )
        row = state.get_backup(conn, backup_id)
        assert row is not None
        assert row["sha256"] == "deadbeef"
        assert row["restored_to"] is None

        state.record_restore(conn, backup_id=backup_id, restored_to="INBOX")
        row2 = state.get_backup(conn, backup_id)
        assert row2 is not None
        assert row2["restored_to"] == "INBOX"
        assert row2["restored_at"] is not None
    finally:
        state.close_database(conn)


def test_prune_candidate_content(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="personal", host="h", username="u")
        candidate = make_candidate(account_id=account_id)
        state.upsert_candidate(conn, candidate)
        far_future = "2099-01-01T00:00:00Z"
        pruned = state.prune_candidate_content(conn, before=far_future)
        assert pruned == 1
        row = conn.execute(
            "SELECT from_address, subject, content_pruned_at FROM candidates"
        ).fetchone()
        assert row["from_address"] is None
        assert row["subject"] is None
        assert row["content_pruned_at"] is not None
    finally:
        state.close_database(conn)
