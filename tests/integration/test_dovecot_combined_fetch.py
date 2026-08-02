"""`fetch_metadata_and_raw` against a real server.

The combined fetch exists to halve the execute phase's round trips, and
it earns that by asking for `BODY.PEEK[]` alongside the metadata items
in a single `UID FETCH`. That makes the server's reply shape the thing
worth testing: `imaplib` splits a literal-bearing response into
`(line, literal)` tuples, and the exact framing around a whole-message
literal is a real server's choice, not something hand-written response
bytes can be trusted to predict.

These tests assert the combined call agrees, field for field, with the
two separate calls it replaces, and that `\\Seen` stays untouched
(spec §4.1, §12 -- `BODY.PEEK[]`, never `BODY[]`).
"""

from datetime import UTC, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from tests.integration.conftest import DovecotServer

pytestmark = pytest.mark.integration

MESSAGE_ID = "<combined-fetch@integration>"


def _message_with_attachment() -> bytes:
    """Multipart with an attachment, so the response carries a
    non-trivial BODYSTRUCTURE *and* a large literal -- the combination
    the single-item unit fixtures cannot reproduce."""
    msg = MIMEMultipart("mixed")
    msg["From"] = "Sender Name <sender@example.com>"
    msg["To"] = "testuser@test.local"
    msg["Subject"] = "combined fetch subject"
    msg["Message-Id"] = MESSAGE_ID
    msg["List-Id"] = "Example List <list.example.com>"
    msg.attach(MIMEText("body text that is long enough to matter\n" * 40, "plain"))
    attachment = MIMEApplication(b"%PDF-1.4 " + b"x" * 5000, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename="doc.pdf")
    msg.attach(attachment)
    return msg.as_bytes()


def test_combined_fetch_matches_the_two_calls_it_replaces(
    dovecot_server: DovecotServer,
) -> None:
    when = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)
    raw_sent = _message_with_attachment()

    adapter = dovecot_server.connect()
    try:
        adapter.append("INBOX", raw_sent, [], when)
        adapter.select("INBOX", readonly=False)
        uids = adapter.search_uids()
        assert len(uids) == 1
        uid = uids[0]

        separate_meta = adapter.fetch_metadata([uid], headers=["MESSAGE-ID"])[0]
        separate_raw = adapter.fetch_raw(uid)

        combined = adapter.fetch_metadata_and_raw(uid)
        assert combined is not None
        combined_meta, combined_raw = combined

        assert combined_raw == separate_raw
        assert combined_meta.uid == separate_meta.uid
        assert combined_meta.internaldate == separate_meta.internaldate
        assert combined_meta.rfc822_size == separate_meta.rfc822_size
        assert combined_meta.flags == separate_meta.flags
        assert combined_meta.has_attachment == separate_meta.has_attachment
        assert combined_meta.has_attachment is True

        # Headers are parsed out of the fetched message rather than
        # requested, so the combined call returns a superset -- but it
        # must agree on every header the narrow call did ask for.
        narrow_id = separate_meta.headers["message-id"]
        assert combined_meta.headers["message-id"] == narrow_id
        assert combined_meta.headers["message-id"] == (MESSAGE_ID,)
        assert combined_meta.headers["subject"] == ("combined fetch subject",)
        assert "list-id" in combined_meta.headers
        # Header parsing must stop at the body, not run on into it.
        assert not any(
            "body text that is long enough" in value
            for values in combined_meta.headers.values()
            for value in values
        )
    finally:
        adapter.close()


def test_combined_fetch_does_not_set_seen(dovecot_server: DovecotServer) -> None:
    when = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)
    raw = (
        b"From: sender@example.com\r\n"
        b"To: testuser@test.local\r\n"
        b"Subject: peek only\r\n"
        b"Message-Id: <combined-fetch-peek@integration>\r\n"
        b"\r\n"
        b"body\r\n"
    )

    adapter = dovecot_server.connect()
    try:
        adapter.append("INBOX", raw, [], when)
        adapter.select("INBOX", readonly=False)
        uid = adapter.search_uids()[-1]

        combined = adapter.fetch_metadata_and_raw(uid)
        assert combined is not None
        assert "\\Seen" not in combined[0].flags

        # Re-read from the server: the fetch above must not have set it.
        after = adapter.fetch_metadata([uid], headers=[])[0]
        assert "\\Seen" not in after.flags
    finally:
        adapter.close()


def test_combined_fetch_returns_none_for_a_missing_uid(
    dovecot_server: DovecotServer,
) -> None:
    """`None` rather than an exception is what `execute._reverify` reads
    as "vanished"; a real server answers an unmatched UID with a bare OK
    and no FETCH data at all."""
    adapter = dovecot_server.connect()
    try:
        adapter.select("INBOX", readonly=False)
        assert adapter.fetch_metadata_and_raw(999_999) is None
    finally:
        adapter.close()
