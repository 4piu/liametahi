"""Custom-keyword (`label:<keyword>`) support against a real server
(spec §7.5, contracts §6.2 point 3)."""

from datetime import UTC, datetime

import pytest

from tests.integration.conftest import DovecotServer

pytestmark = pytest.mark.integration

_RAW = (
    b"From: sender@example.com\r\n"
    b"To: testuser@test.local\r\n"
    b"Subject: keyword test\r\n"
    b"Message-Id: <keyword-test@integration>\r\n"
    b"\r\n"
    b"body\r\n"
)


def test_add_keyword_persists_as_a_custom_flag(dovecot_server: DovecotServer) -> None:
    adapter = dovecot_server.connect()
    try:
        adapter.append("INBOX", _RAW, [], datetime(2026, 1, 1, tzinfo=UTC))
        adapter.select("INBOX", readonly=False)
        uid = adapter.search_uids()[0]

        adapter.add_keyword(uid, "LiametahiTestKeyword")

        meta = adapter.fetch_metadata([uid], headers=["SUBJECT"])
        assert "LiametahiTestKeyword" in meta[0].flags
    finally:
        adapter.close()
