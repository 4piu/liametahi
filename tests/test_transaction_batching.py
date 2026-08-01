"""Tests for `state.transaction` and, critically, for what it must NOT
be applied to.

Bookkeeping writes (candidate upserts, classifications, decision-cache
entries, no-op result items) are batched into one transaction per phase
so a run costs a handful of fsyncs instead of hundreds. The
`action_attempts` state machine is deliberately excluded: the reconcile
pass (spec §4.0) can only tell how far a crashed run got because each
step was committed as it happened. `test_action_attempt_writes_stay_...`
below is the regression guard for that -- it fails if anyone ever wraps
`execute.execute_items` in a transaction for symmetry.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from liametahi import execute, policy, state
from liametahi.config import RuleConfig
from liametahi.domain import MessageKey, fingerprint
from tests.conftest import make_candidate
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<batch1@example.com>"


# --- state.transaction itself --------------------------------------------


def test_transaction_commits_as_one_unit(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    conn = state.open_database(db)
    try:
        with state.transaction(conn):
            state.upsert_account(conn, name="a", host="h", username="u")
            assert conn.in_transaction, "writes should still be pending"
        assert not conn.in_transaction
        other = state.open_database(db)
        try:
            assert other.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1
        finally:
            state.close_database(other)
    finally:
        state.close_database(conn)


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(RuntimeError), state.transaction(conn):
            state.upsert_account(conn, name="a", host="h", username="u")
            raise RuntimeError("boom")
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 0
    finally:
        state.close_database(conn)


def test_transaction_is_reentrant(tmp_path: Path) -> None:
    """SQLite has no nested BEGIN, so an inner `transaction()` must join
    the outer one rather than raising -- `update_candidate_flags` batches
    internally and may well be called from inside an outer batch."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        with state.transaction(conn):
            with state.transaction(conn):
                state.upsert_account(conn, name="a", host="h", username="u")
            assert conn.in_transaction, "inner exit must not commit the outer"
        assert not conn.in_transaction
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 1
    finally:
        state.close_database(conn)


# --- the exclusion that matters ------------------------------------------


def _raw() -> bytes:
    return (
        "From: s@example.com\r\n"
        "Subject: hi\r\n"
        f"Message-Id: {MESSAGE_ID}\r\n"
        "\r\n"
        "body\r\n"
    ).encode()


def test_action_attempt_writes_stay_individually_durable(tmp_path: Path) -> None:
    """spec §4.0: reconcile closes out rows a crashed run left
    `pending`/`in_flight`, which only works if each state transition was
    committed as it happened. This asserts durability the way a crash
    would observe it -- from a *separate connection* -- at every single
    `action_attempts` write during a real `execute_items` call.
    """
    db = tmp_path / "state.sqlite3"
    conn = state.open_database(db)
    observed: list[tuple[int, str]] = []

    def assert_visible_elsewhere() -> None:
        # A second connection sees only committed data. `timeout` is
        # deliberately short: if `execute_items` ever gets wrapped in a
        # transaction, this read contends with the held write lock, and
        # the guard must fail fast and legibly rather than hang the
        # suite waiting for a lock that is never released.
        other = sqlite3.connect(str(db), timeout=2.0)
        other.row_factory = sqlite3.Row
        try:
            for row in other.execute(
                "SELECT attempt_id, state FROM action_attempts ORDER BY attempt_id"
            ):
                observed.append((int(row["attempt_id"]), str(row["state"])))
        except sqlite3.OperationalError as exc:  # pragma: no cover - guard path
            raise AssertionError(
                "action_attempts row was not readable from a second "
                f"connection ({exc}) -- execute_items must not hold a "
                "transaction open across its writes (spec §4.0)"
            ) from exc
        finally:
            other.close()

    real_insert = state.insert_action_attempt
    real_update = state.update_action_attempt_state

    def spy_insert(*args: object, **kwargs: object) -> int:
        result = real_insert(*args, **kwargs)  # type: ignore[arg-type]
        assert not conn.in_transaction, (
            "an action_attempts INSERT must not be inside an open transaction"
        )
        assert_visible_elsewhere()
        return result

    def spy_update(*args: object, **kwargs: object) -> None:
        real_update(*args, **kwargs)  # type: ignore[arg-type]
        assert not conn.in_transaction, (
            "an action_attempts UPDATE must not be inside an open transaction"
        )
        assert_visible_elsewhere()

    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
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
        internaldate = datetime(2026, 6, 1, tzinfo=UTC)
        raw = _raw()
        candidate_id = state.upsert_candidate(
            conn,
            make_candidate(account_id=account_id, uid=1, internaldate=internaldate),
        )
        mb = FakeMailbox(
            messages=[
                _StoredMessage(
                    uid=1,
                    raw=raw,
                    flags=set(),
                    internaldate=internaldate,
                    mailbox="INBOX",
                )
            ],
            uidvalidity={"INBOX": 1000, "Trash": 1000},
        )
        rule = RuleConfig.model_validate(
            {"id": "r", "when": {"older-than": "1d"}, "actions": ["backup", "trash"]}
        )
        item = execute.ExecutionItem(
            candidate_id=candidate_id,
            key=MessageKey(account_id, "INBOX", 1000, 1),
            fingerprint=fingerprint(
                message_id=MESSAGE_ID,
                internaldate=internaldate,
                rfc822_size=len(raw),
                from_address=None,
                subject=None,
            ),
            message_id=MESSAGE_ID,
            winning_rule="r",
            actions=policy.resolve_actions(rule, trash_mailbox="Trash"),
        )

        state.insert_action_attempt = spy_insert
        state.update_action_attempt_state = spy_update
        try:
            summary = execute.execute_items(
                conn,
                mailbox=mb,
                items=[item],
                run_id=run_id,
                backup_dir=tmp_path / "backups",
                max_actions_per_run=None,
                dry_run=False,
                fail_fast=False,
            )
        finally:
            state.insert_action_attempt = real_insert
            state.update_action_attempt_state = real_update

        assert summary.outcomes[0].status == "completed"
        # The spy actually ran: backup + trash, each inserted then moved
        # through in_flight -> completed, all observed from outside.
        assert observed, "no action_attempts writes were observed at all"
        assert ("in_flight" in {s for _, s in observed}) and (
            "completed" in {s for _, s in observed}
        )
    finally:
        state.close_database(conn)
