"""Tests for `liametahi.report` (spec section 9; contracts section 5.5)."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from liametahi import execute, policy, report, state
from liametahi.domain import MessageKey, fingerprint
from tests.conftest import make_candidate
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<r1@example.com>"


def _raw() -> bytes:
    return (
        "From: sender@example.com\r\n"
        "Subject: Weekly Digest\r\n"
        f"Message-Id: {MESSAGE_ID}\r\n"
        "\r\n"
        "body\r\n"
    ).encode()


def _setup(conn: sqlite3.Connection, *, task: str = "t") -> tuple[int, str]:
    account_id = state.upsert_account(conn, name="a", host="h", username="u")
    run_id = state.new_run_id()
    state.create_run(
        conn,
        run_id=run_id,
        task=task,
        account_id=account_id,
        model_name="local",
        provider="openai_compatible",
        model_id="qwen",
        dry_run=False,
        reevaluate=False,
        fetch_headers=["FROM", "SUBJECT"],
        config_hash="h",
    )
    return account_id, run_id


def _run_one_matched_item(
    conn: sqlite3.Connection, *, account_id: int, run_id: str, tmp_path: Path
) -> None:
    from liametahi.config import RuleConfig

    rule = RuleConfig.model_validate(
        {
            "id": "digest",
            "when": {"older-than": "1d"},
            "actions": ["backup", "trash"],
        }
    )
    internaldate = datetime(2026, 6, 1, tzinfo=UTC)
    raw = _raw()
    mb = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1, raw=raw, flags=set(), internaldate=internaldate, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1000, "Trash": 1000},
    )
    candidate = make_candidate(
        account_id=account_id,
        message_id=MESSAGE_ID,
        internaldate=internaldate,
        from_address="sender@example.com",
        subject="Weekly Digest",
    )
    candidate_id = state.upsert_candidate(conn, candidate)
    key = MessageKey(account_id, "INBOX", 1000, 1)
    fp = fingerprint(
        message_id=MESSAGE_ID,
        internaldate=internaldate,
        rfc822_size=len(raw),
        from_address=None,
        subject=None,
    )
    actions = policy.resolve_actions(rule, trash_mailbox="Trash")
    item = execute.ExecutionItem(
        candidate_id=candidate_id,
        key=key,
        fingerprint=fp,
        message_id=MESSAGE_ID,
        winning_rule="digest",
        actions=actions,
    )
    execute.execute_items(
        conn,
        mailbox=mb,
        items=[item],
        run_id=run_id,
        backup_dir=tmp_path / "backups",
        max_actions=50,
        dry_run=False,
        fail_fast=False,
    )
    state.finish_run(
        conn, run_id=run_id, exit_code=0, candidates_scanned=1, llm_calls=0
    )


def test_load_report_unknown_run_raises(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        with pytest.raises(report.ReportNotFoundError):
            report.load_report(conn, "run_doesnotexist")
    finally:
        state.close_database(conn)


def test_load_report_derives_completed_status_from_action_attempts(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id, run_id = _setup(conn)
        _run_one_matched_item(
            conn, account_id=account_id, run_id=run_id, tmp_path=tmp_path
        )

        data = report.load_report(conn, run_id)
        assert data.run.run_id == run_id
        assert data.account_name == "a"
        assert len(data.items) == 1
        item = data.items[0]
        assert item.status == "completed"
        assert item.winning_rule == "digest"
        assert item.message_key == f"{account_id}/INBOX/1000/1"
        assert [a.action for a in item.actions] == ["backup", "trash"]
        assert item.actions[0].backup_id is not None
        assert data.totals.acted == 1
        assert data.totals.failed == 0
        assert data.totals.by_status == {"completed": 1}
    finally:
        state.close_database(conn)


def test_quiet_statuses_hidden_by_default_and_shown_verbose(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id, run_id = _setup(conn)
        candidate = make_candidate(account_id=account_id)
        candidate_id = state.upsert_candidate(conn, candidate)
        state.insert_result_item(
            conn, run_id=run_id, candidate_id=candidate_id, status="no_match"
        )
        state.finish_run(
            conn, run_id=run_id, exit_code=0, candidates_scanned=1, llm_calls=0
        )

        data = report.load_report(conn, run_id)
        default_table = report.render_table(data, verbose=False)
        # Totals always reflect the whole run (including quiet
        # statuses), but the per-item listing below them must not.
        assert "by_status: no_match=1" in default_table
        assert "(no actionable or failed items)" in default_table

        verbose_table = report.render_table(data, verbose=True)
        assert "(no actionable or failed items)" not in verbose_table

        default_json = report.to_json_document(data, verbose=False)
        assert default_json["items"] == []
        verbose_json = report.to_json_document(data, verbose=True)
        assert len(verbose_json["items"]) == 1
        assert verbose_json["items"][0]["status"] == "no_match"
    finally:
        state.close_database(conn)


def test_restored_status_is_quiet(tmp_path: Path) -> None:
    """sync-fix-brief Fix D, Finding 3: skipping a previously-trashed
    message is routine bookkeeping, not an anomaly worth interrupting a
    cron user over -- and because the candidate is retired at the same
    time (see `runner.py`), it is reported at most once ever. So
    `restored` is a quiet status: recorded in the database and visible
    under `--verbose`, absent from default output."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id, run_id = _setup(conn)
        candidate = make_candidate(account_id=account_id)
        candidate_id = state.upsert_candidate(conn, candidate)
        state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=candidate_id,
            status="restored",
            detail="fingerprint already has a completed trash from a previous run",
        )
        state.finish_run(
            conn, run_id=run_id, exit_code=0, candidates_scanned=1, llm_calls=0
        )

        data = report.load_report(conn, run_id)
        default_table = report.render_table(data, verbose=False)
        assert "(no actionable or failed items)" in default_table

        default_json = report.to_json_document(data, verbose=False)
        assert default_json["items"] == []

        # Still recorded, and still reachable with --verbose.
        verbose_json = report.to_json_document(data, verbose=True)
        assert [item["status"] for item in verbose_json["items"]] == ["restored"]
    finally:
        state.close_database(conn)


def test_render_json_matches_pinned_shape(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id, run_id = _setup(conn)
        _run_one_matched_item(
            conn, account_id=account_id, run_id=run_id, tmp_path=tmp_path
        )
        data = report.load_report(conn, run_id)

        document = json.loads(report.render_json(data, verbose=False))
        assert document["report_version"] == 1
        assert set(document.keys()) == {"report_version", "run", "totals", "items"}
        assert set(document["run"].keys()) == {
            "run_id",
            "task",
            "account",
            "model",
            "provider",
            "model_id",
            "dry_run",
            "reevaluate",
            "structured_output_level",
            "started_at",
            "ended_at",
            "exit_code",
        }
        assert set(document["totals"].keys()) == {
            "scanned",
            "acted",
            "failed",
            "llm_calls",
            "by_status",
        }
        item = document["items"][0]
        assert set(item.keys()) == {
            "message_key",
            "fingerprint",
            "from",
            "subject",
            "winning_rule",
            "shadowed_by",
            "status",
            "actions",
        }
        assert item["actions"][0]["action"] == "backup"
        assert item["actions"][0]["backup_id"] is not None
    finally:
        state.close_database(conn)


def test_render_table_includes_run_metadata_and_totals(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id, run_id = _setup(conn)
        _run_one_matched_item(
            conn, account_id=account_id, run_id=run_id, tmp_path=tmp_path
        )
        data = report.load_report(conn, run_id)
        table = report.render_table(data, verbose=False)
        assert run_id in table
        assert "task=t" in table
        assert "completed" in table
        assert "digest" in table
    finally:
        state.close_database(conn)


def test_render_run_list(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id, run_id = _setup(conn, task="task-a")
        state.finish_run(
            conn, run_id=run_id, exit_code=0, candidates_scanned=0, llm_calls=0
        )
        runs = state.list_runs(conn)
        table = report.render_run_list(runs)
        assert run_id in table
        assert "task-a" in table

        assert report.render_run_list([]) == "(no runs recorded)"
    finally:
        state.close_database(conn)


# --- Table layout ---------------------------------------------------------


def test_fit_truncates_with_an_ellipsis_and_never_exceeds_width() -> None:
    assert report._fit("short", 10) == "short"
    assert report._fit("exactlyten", 10) == "exactlyten"
    assert report._fit("wayolongerthanten", 10) == "wayolon..."
    assert len(report._fit("wayolongerthanten", 10)) == 10
    # Degenerate widths must still not overflow, even though there is no
    # room for the marker.
    for width in range(0, 4):
        assert len(report._fit("abcdefgh", width)) == width


def test_table_columns_are_sized_to_the_data() -> None:
    """Fixed widths were simultaneously too wide (a 38-column message key
    beside a 15-character value) and too narrow (a 27-character sender in
    a 26-wide column shoved every later column out of true)."""
    rows = [
        ["1/INBOX/1/15330", "a@x.com", "junk", "completed", "backup:completed", "abc"],
        [
            "1/INBOX/1/15383",
            "email@email.playstation.com",
            "junk",
            "completed",
            "backup:completed",
            "def",
        ],
    ]
    lines = report._format_table(
        ["MESSAGE KEY", "FROM", "RULE", "STATUS", "ACTIONS", "BACKUP"],
        rows,
        max_widths=[30, 30, 18, 20, 34, 0],
    )
    header, rule, *body = lines
    assert set(rule) == {"-"}
    # Every row starts its second column at the same offset -- the bug
    # was that a long value in column two pushed the rest rightwards.
    offsets = {line.index("junk") for line in body}
    assert len(offsets) == 1, body
    # No column is padded wider than its widest value needs.
    assert "MESSAGE KEY" in header
    assert header.index("FROM") == len("1/INBOX/1/15330") + 1


def test_table_last_column_is_neither_padded_nor_truncated() -> None:
    """Nothing follows it to misalign, and it carries identifiers."""
    long_id = "a" * 60
    lines = report._format_table(["A", "B"], [["x", long_id]], max_widths=[10, 0])
    assert lines[-1].endswith(long_id)
    assert not lines[-1].endswith(" ")


def test_render_table_aligns_a_long_sender(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id, run_id = _setup(conn)
        for i, sender in enumerate(
            ["a@x.com", "email@email.playstation.com", "b@y.com"], start=1
        ):
            candidate = make_candidate(
                account_id=account_id, uid=i, from_address=sender
            )
            candidate_id = state.upsert_candidate(conn, candidate)
            state.insert_result_item(
                conn,
                run_id=run_id,
                candidate_id=candidate_id,
                status="failed",
                winning_rule="junk",
            )
        state.finish_run(
            conn, run_id=run_id, exit_code=1, candidates_scanned=3, llm_calls=0
        )
        table = report.render_table(report.load_report(conn, run_id), verbose=False)
        body = [line for line in table.splitlines() if "junk" in line]
        assert len(body) == 3
        assert len({line.index("junk") for line in body}) == 1, body
    finally:
        state.close_database(conn)
