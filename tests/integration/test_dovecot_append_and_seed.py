"""`APPEND` round-tripping flags and `INTERNALDATE` (contracts §6.2
point 3), plus seeding a container from the committed synthetic corpus
and confirming `imap_adapter.scan()` recovers every message end to end
against a real server."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from liametahi import state
from liametahi.config import BASE_FETCH_HEADERS
from liametahi.imap_adapter import scan
from tests.integration.conftest import DovecotServer, seed_corpus

pytestmark = pytest.mark.integration

_RAW = (
    b"From: sender@example.com\r\n"
    b"To: testuser@test.local\r\n"
    b"Subject: append roundtrip test\r\n"
    b"Message-Id: <append-test@integration>\r\n"
    b"\r\n"
    b"body\r\n"
)


def test_append_roundtrips_flags_and_internaldate(
    dovecot_server: DovecotServer,
) -> None:
    adapter = dovecot_server.connect()
    try:
        when = datetime(2020, 3, 4, 5, 6, 7, tzinfo=UTC)
        adapter.append("INBOX", _RAW, ["\\Seen", "\\Flagged"], when)

        adapter.select("INBOX", readonly=True)
        uids = adapter.search_uids()
        assert len(uids) == 1

        meta = adapter.fetch_metadata(list(uids), headers=["SUBJECT"])
        assert len(meta) == 1
        # A server may add \Recent on its own for a message appended in
        # the appending session; the flags we asked for must still be
        # present exactly.
        assert {"\\Seen", "\\Flagged"} <= meta[0].flags
        assert meta[0].internaldate == when
        assert adapter.fetch_raw(uids[0]) == _RAW
    finally:
        adapter.close()


def test_seed_corpus_via_append_and_scan_recovers_every_message(
    dovecot_server: DovecotServer, tmp_path: Path
) -> None:
    """Seeding (`ImapMailbox.append`, the same method `restore` uses)
    and the scan phase (`imap_adapter.scan`) are both production code;
    this is the closest thing in this suite to an end-to-end smoke test
    against a real server (contracts §6.2 point 2)."""
    seed_adapter = dovecot_server.connect()
    try:
        seeded = seed_corpus(seed_adapter)
    finally:
        seed_adapter.close()
    assert len(seeded) == 6

    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(
            conn, name="it", host=dovecot_server.host, username=dovecot_server.username
        )
        scan_adapter = dovecot_server.connect()
        try:
            result = scan(
                scan_adapter,
                conn,
                account_id=account_id,
                source_mailboxes=["INBOX"],
                fetch_headers=list(BASE_FETCH_HEADERS),
                max_new_mails=500,
            )
        finally:
            scan_adapter.close()

        assert result.candidates_scanned == len(seeded)
        assert result.mailboxes[0].new_candidates == len(seeded)
        assert not result.any_uidvalidity_changed

        # A second scan with nothing new added is idempotent: every
        # message is already tracked, so nothing new is fetched.
        rescan_adapter = dovecot_server.connect()
        try:
            second = scan(
                rescan_adapter,
                conn,
                account_id=account_id,
                source_mailboxes=["INBOX"],
                fetch_headers=list(BASE_FETCH_HEADERS),
                max_new_mails=500,
            )
        finally:
            rescan_adapter.close()
        assert second.candidates_scanned == 0
    finally:
        state.close_database(conn)
