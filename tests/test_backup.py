"""Tests for `liametahi.backup` (spec section 11, section 4.4)."""

import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from liametahi import backup, state
from liametahi.domain import MessageKey
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage


def _setup_run(
    conn: sqlite3.Connection, *, account_id: int, run_id: str, task: str = "t"
) -> None:
    state.create_run(
        conn,
        run_id=run_id,
        task=task,
        account_id=account_id,
        model_name="m",
        provider="p",
        model_id="mi",
        dry_run=False,
        reevaluate=False,
        fetch_headers=[],
        config_hash="h",
    )


def _mailbox_with_message(
    uid: int = 1, raw: bytes = b"Subject: hi\r\n\r\nbody"
) -> FakeMailbox:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    mb = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=uid, raw=raw, flags={"\\Seen"}, internaldate=now, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1000, "Trash": 1000},
    )
    mb.select("INBOX", readonly=False)
    return mb


def test_write_verified_backup_creates_checksummed_file_and_manifest(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        mb = _mailbox_with_message()
        key = MessageKey(account_id, "INBOX", 1000, 1)

        result = backup.write_verified_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            key=key,
            fingerprint="fp-1",
            message_id="<m1@example.com>",
            original_flags={"\\Seen"},
            internaldate=datetime(2026, 6, 1, tzinfo=UTC),
            run_id=run_id,
        )

        eml_path = tmp_path / "backups" / result.relative_path
        assert eml_path.is_file()
        assert eml_path.read_bytes() == b"Subject: hi\r\n\r\nbody"
        mode = stat.S_IMODE(eml_path.stat().st_mode)
        assert mode == 0o600
        backup_dir_mode = stat.S_IMODE((tmp_path / "backups").stat().st_mode)
        assert backup_dir_mode == 0o700

        row = state.get_backup(conn, result.backup_id)
        assert row is not None
        assert row["sha256"] == result.sha256
        assert row["byte_count"] == len(b"Subject: hi\r\n\r\nbody")
        assert row["original_mailbox"] == "INBOX"
    finally:
        state.close_database(conn)


def test_write_verified_backup_is_idempotent_on_rerun(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        mb = _mailbox_with_message()
        key = MessageKey(account_id, "INBOX", 1000, 1)

        first = backup.write_verified_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            key=key,
            fingerprint="fp-1",
            message_id="<m1@example.com>",
            original_flags={"\\Seen"},
            internaldate=datetime(2026, 6, 1, tzinfo=UTC),
            run_id=run_id,
        )
        second = backup.write_verified_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            key=key,
            fingerprint="fp-1",
            message_id="<m1@example.com>",
            original_flags={"\\Seen"},
            internaldate=datetime(2026, 6, 1, tzinfo=UTC),
            run_id=run_id,
        )
        assert first.backup_id == second.backup_id
        rows = conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()
        assert rows["c"] == 1
    finally:
        state.close_database(conn)


def test_write_verified_backup_raises_on_fetch_failure_and_writes_nothing(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        mb = _mailbox_with_message(uid=1)
        mb.vanish("INBOX", 1)
        key = MessageKey(account_id, "INBOX", 1000, 1)

        with pytest.raises(backup.BackupError):
            backup.write_verified_backup(
                conn,
                mailbox=mb,
                backup_dir=tmp_path / "backups",
                key=key,
                fingerprint="fp-1",
                message_id="<m1@example.com>",
                original_flags={"\\Seen"},
                internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                run_id=run_id,
            )
        rows = conn.execute("SELECT COUNT(*) AS c FROM backups").fetchone()
        assert rows["c"] == 0
    finally:
        state.close_database(conn)


def test_is_vanished_error_matches_keyerror_and_named_exception() -> None:
    assert backup.is_vanished_error(KeyError("x"))
    assert backup.is_vanished_error(backup.MessageVanished("x"))

    from tests.fakes.fake_mailbox import MessageVanished as FakeVanished

    assert backup.is_vanished_error(FakeVanished("x"))
    assert not backup.is_vanished_error(ValueError("x"))


def test_is_unsupported_error_matches_named_exception() -> None:
    from tests.fakes.fake_mailbox import UnsupportedCapability as FakeUnsupported

    assert backup.is_unsupported_error(FakeUnsupported("x"))
    assert backup.is_unsupported_error(backup.UnsupportedCapability("x"))
    assert not backup.is_unsupported_error(RuntimeError("x"))


# --- restore (spec section 4.4) -------------------------------------------


def _do_backup(
    conn: sqlite3.Connection, tmp_path: Path, *, account_id: int, run_id: str
) -> tuple[backup.BackupResult, FakeMailbox]:
    mb = _mailbox_with_message()
    key = MessageKey(account_id, "INBOX", 1000, 1)
    result = backup.write_verified_backup(
        conn,
        mailbox=mb,
        backup_dir=tmp_path / "backups",
        key=key,
        fingerprint="fp-1",
        message_id="<m1@example.com>",
        original_flags={"\\Seen", "\\Flagged"},
        internaldate=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        run_id=run_id,
    )
    return result, mb


def test_restore_appends_with_original_internaldate_and_flags_minus_deleted_recent(
    tmp_path: Path,
) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        result, mb = _do_backup(conn, tmp_path, account_id=account_id, run_id=run_id)

        restore_result = backup.restore_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            backup_id=result.backup_id,
            destination_mailbox="INBOX",
            dry_run=False,
        )
        assert restore_result.appended is True
        assert len(mb.appended) == 1
        appended_mailbox, appended_raw, appended_flags, appended_date = mb.appended[0]
        assert appended_mailbox == "INBOX"
        assert appended_raw == b"Subject: hi\r\n\r\nbody"
        assert set(appended_flags) == {"\\Seen", "\\Flagged"}
        assert appended_date == datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

        row = state.get_backup(conn, result.backup_id)
        assert row is not None
        assert row["restored_to"] == "INBOX"
        assert row["restored_at"] is not None
    finally:
        state.close_database(conn)


def test_restore_strips_deleted_and_recent_flags(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        mb = _mailbox_with_message()
        key = MessageKey(account_id, "INBOX", 1000, 1)
        result = backup.write_verified_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            key=key,
            fingerprint="fp-1",
            message_id="<m1@example.com>",
            original_flags={"\\Seen", "\\Deleted", "\\Recent"},
            internaldate=datetime(2026, 6, 1, tzinfo=UTC),
            run_id=run_id,
        )
        backup.restore_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            backup_id=result.backup_id,
            destination_mailbox="INBOX",
            dry_run=False,
        )
        _, _, flags, _ = mb.appended[0]
        assert "\\Deleted" not in flags
        assert "\\Recent" not in flags
        assert "\\Seen" in flags
    finally:
        state.close_database(conn)


def test_restore_dry_run_verifies_checksum_but_does_not_append(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        result, mb = _do_backup(conn, tmp_path, account_id=account_id, run_id=run_id)

        restore_result = backup.restore_backup(
            conn,
            mailbox=mb,
            backup_dir=tmp_path / "backups",
            backup_id=result.backup_id,
            destination_mailbox="INBOX",
            dry_run=True,
        )
        assert restore_result.dry_run is True
        assert restore_result.appended is False
        assert mb.appended == ()
        row = state.get_backup(conn, result.backup_id)
        assert row is not None
        assert row["restored_to"] is None
    finally:
        state.close_database(conn)


def test_restore_rejects_corrupted_backup_file(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        result, mb = _do_backup(conn, tmp_path, account_id=account_id, run_id=run_id)

        eml_path = tmp_path / "backups" / result.relative_path
        eml_path.write_bytes(b"tampered content")

        with pytest.raises(backup.RestoreError):
            backup.restore_backup(
                conn,
                mailbox=mb,
                backup_dir=tmp_path / "backups",
                backup_id=result.backup_id,
                destination_mailbox="INBOX",
                dry_run=False,
            )
        assert mb.appended == ()
    finally:
        state.close_database(conn)


def test_restore_unknown_backup_id_raises(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        mb = FakeMailbox()
        with pytest.raises(backup.RestoreError):
            backup.restore_backup(
                conn,
                mailbox=mb,
                backup_dir=tmp_path / "backups",
                backup_id="bkp_doesnotexist",
                destination_mailbox="INBOX",
                dry_run=False,
            )
    finally:
        state.close_database(conn)
