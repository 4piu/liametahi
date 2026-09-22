"""Verifies the exact capability claims documented in `conftest.py`'s
module docstring against a live container, so a future image change
that weakens these guarantees fails loudly here rather than the whole
suite silently testing something the pinned image no longer supports
("verify and report" MOVE and keyword support)."""

import pytest

from tests.integration.conftest import DovecotServer

pytestmark = pytest.mark.integration


def test_post_login_capabilities_include_move(dovecot_server: DovecotServer) -> None:
    """RFC 6851 `MOVE`, required for `move_to`/`trash`."""
    adapter = dovecot_server.connect()
    try:
        assert "MOVE" in adapter.capabilities()
    finally:
        adapter.close()


def test_mailbox_permanent_flags_include_custom_keyword_marker(
    dovecot_server: DovecotServer,
) -> None:
    """`PERMANENTFLAGS` containing `\\*`, required for `label:<keyword>`.
    Probed on a **read-write** `select` (`readonly=False`):
    Dovecot correctly reports `PERMANENTFLAGS ()` on an `EXAMINE`d
    (read-only) mailbox, since nothing may be permanently changed in a
    read-only session at all -- that is not evidence of missing keyword
    support, and every real caller of `add_keyword` selects read-write
    first anyway."""
    adapter = dovecot_server.connect()
    try:
        status = adapter.select("INBOX", readonly=False)
        assert "\\*" in status.permanent_flags
        assert status.accepts_custom_keywords is True
    finally:
        adapter.close()
