"""A real `UID MOVE` between mailboxes and a real `UIDVALIDITY` change
(contracts §6.2 point 3's minimum integration coverage)."""

from datetime import UTC, datetime

import pytest

from tests.integration.conftest import DovecotServer

pytestmark = pytest.mark.integration

_RAW = (
    b"From: sender@example.com\r\n"
    b"To: testuser@test.local\r\n"
    b"Subject: integration move test\r\n"
    b"Message-Id: <move-test@integration>\r\n"
    b"\r\n"
    b"body\r\n"
)


def test_real_uid_move_relocates_message_between_mailboxes(
    dovecot_server: DovecotServer,
) -> None:
    adapter = dovecot_server.connect()
    try:
        adapter.append("INBOX", _RAW, ["\\Seen"], datetime(2026, 1, 1, tzinfo=UTC))
        adapter.select("INBOX", readonly=False)
        uids = adapter.search_uids()
        assert len(uids) == 1
        uid = uids[0]

        adapter.move(uid, "Trash")

        adapter.select("INBOX", readonly=True)
        assert adapter.search_uids() == ()

        status = adapter.select("Trash", readonly=True)
        assert status.exists == 1
        # RFC 6851: the moved message gets a UID from the *destination*
        # mailbox's independent UID space -- not necessarily a
        # numerically different value (a fresh destination mailbox can
        # coincidentally assign the same number the source used).
        moved_uid = adapter.search_uids()[0]
        assert adapter.fetch_raw(moved_uid) == _RAW
    finally:
        adapter.close()


def test_uidvalidity_changes_on_mailbox_delete_and_recreate(
    dovecot_server: DovecotServer,
) -> None:
    """No production code path deletes or recreates a mailbox; this
    simulates the server-side rebuild spec §4.1 point 2 defends against
    using an operation an administrator (not Liametahi) might perform."""
    raw = dovecot_server.raw_imap()
    try:
        raw.create("Rebuildable")
    finally:
        raw.logout()

    adapter = dovecot_server.connect()
    try:
        first_uidvalidity = adapter.select("Rebuildable", readonly=True).uidvalidity
    finally:
        adapter.close()

    raw = dovecot_server.raw_imap()
    try:
        raw.delete("Rebuildable")
        raw.create("Rebuildable")
    finally:
        raw.logout()

    adapter = dovecot_server.connect()
    try:
        second_uidvalidity = adapter.select("Rebuildable", readonly=True).uidvalidity
    finally:
        adapter.close()

    assert second_uidvalidity != first_uidvalidity
