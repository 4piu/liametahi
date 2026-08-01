"""`has-attachment` (spec §7.1) against a real server's actual
`BODYSTRUCTURE` response.

Hand-written `BODYSTRUCTURE` byte strings (`tests/test_imap_adapter.py`)
prove the parser and heuristic are correct in isolation, but a real IMAP
server formats `BODYSTRUCTURE` with its own whitespace and atom-case
choices; this test appends a real multipart message with an attachment,
scans it through `imap_adapter.scan()` (production code, not a shortcut),
and confirms the resulting `Candidate.has_attachment` is `True` -- the
one thing the unit tier cannot validate on its own.
"""

from datetime import UTC, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pytest

from liametahi import state
from liametahi.config import BASE_FETCH_HEADERS
from liametahi.domain import MessageKey
from liametahi.imap_adapter import scan
from tests.integration.conftest import DovecotServer

pytestmark = pytest.mark.integration


def _multipart_with_attachment() -> bytes:
    msg = MIMEMultipart("mixed")
    msg["From"] = "sender@example.com"
    msg["To"] = "testuser@test.local"
    msg["Subject"] = "invoice attached"
    msg["Message-Id"] = "<bodystructure-attachment@integration>"
    msg.attach(MIMEText("please see the attached invoice", "plain"))
    attachment = MIMEApplication(b"%PDF-1.4 fake pdf bytes", _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename="invoice.pdf")
    msg.attach(attachment)
    return msg.as_bytes()


def _plain_text_no_attachment() -> bytes:
    return (
        b"From: sender@example.com\r\n"
        b"To: testuser@test.local\r\n"
        b"Subject: no attachment here\r\n"
        b"Message-Id: <bodystructure-plain@integration>\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"just a plain message body\r\n"
    )


def test_real_bodystructure_detects_attachment_end_to_end(
    dovecot_server: DovecotServer, tmp_path: Path
) -> None:
    when = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)

    seed_adapter = dovecot_server.connect()
    try:
        seed_adapter.append("INBOX", _multipart_with_attachment(), [], when)
        seed_adapter.append("INBOX", _plain_text_no_attachment(), [], when)
        seed_adapter.select("INBOX", readonly=True)
        seeded_uids = seed_adapter.search_uids()
    finally:
        seed_adapter.close()
    assert len(seeded_uids) == 2

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

        assert result.candidates_scanned == 2
        uidvalidity = result.mailboxes[0].uidvalidity

        by_subject = {}
        for uid in seeded_uids:
            key = MessageKey(account_id, "INBOX", uidvalidity, uid)
            found = state.get_candidate(conn, key=key)
            assert found is not None
            _candidate_id, candidate = found
            assert candidate.subject is not None
            by_subject[candidate.subject] = candidate

        assert by_subject["invoice attached"].has_attachment is True
        assert by_subject["no attachment here"].has_attachment is False
    finally:
        state.close_database(conn)
