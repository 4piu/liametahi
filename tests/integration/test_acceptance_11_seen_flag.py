"""Acceptance test 11 (specification §14.11), integration half: a
metadata fetch does not set `\\Seen` on any message, verified against a
real IMAP server. The unit-tier half (against `FakeMailbox`) lives in
`tests/test_acceptance_mailbox.py::test_acceptance_11_metadata_fetch_does_not_set_seen`;
this is the one property in the whole spec explicitly called out as
needing a real server, not just a fake proving it (spec §14 preamble to
this unit's task description).

The mailbox is selected **read-write** (`readonly=False`), not
`EXAMINE`d, so this test actually exercises `BODY.PEEK` doing its job --
selecting read-only would make the assertion trivially true regardless
of whether `fetch_metadata`/`fetch_raw` used `PEEK` at all, since a
server must never change flags in an `EXAMINE`d mailbox no matter what
is fetched.
"""

from datetime import UTC, datetime

import pytest

from tests.integration.conftest import DovecotServer

pytestmark = pytest.mark.integration

_RAW = (
    b"From: sender@example.com\r\n"
    b"To: testuser@test.local\r\n"
    b"Subject: seen flag probe\r\n"
    b"Message-Id: <seen-probe@integration>\r\n"
    b"\r\n"
    b"body\r\n"
)


def test_acceptance_11_metadata_fetch_does_not_set_seen_integration(
    dovecot_server: DovecotServer,
) -> None:
    adapter = dovecot_server.connect()
    try:
        # A freshly delivered message, explicitly without \Seen.
        adapter.append("INBOX", _RAW, [], datetime(2026, 1, 1, tzinfo=UTC))

        # Read-write select, exactly as the execute phase's re-verify
        # step does (spec §4.3 point 4) -- this is the codepath where a
        # missing PEEK modifier would actually bite.
        adapter.select("INBOX", readonly=False)
        uid = adapter.search_uids()[0]

        before = adapter.fetch_metadata([uid], headers=["SUBJECT"])
        assert "\\Seen" not in before[0].flags

        # Both PEEK-only read paths, repeated for good measure.
        adapter.fetch_metadata([uid], headers=["SUBJECT", "FROM"])
        adapter.fetch_raw(uid)

        after = adapter.fetch_metadata([uid], headers=["SUBJECT"])
        assert "\\Seen" not in after[0].flags
    finally:
        adapter.close()
