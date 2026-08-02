"""The execute phase's cost against a remote server is dominated by how
many IMAP commands it issues per message, not by local work: a first run
over a few thousand messages multiplies whatever this number is by a
few thousand network latencies.

Two of those commands used to be pure waste -- a `SELECT` re-issued for
every message in the same mailbox, and a second `FETCH` for bytes the
re-verify `FETCH` could have carried. These tests pin the resulting
per-message command counts so a later change cannot quietly put them
back.
"""

import sqlite3
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from liametahi import execute, policy, state
from liametahi.config import RuleConfig
from liametahi.domain import MessageKey, fingerprint
from liametahi.imap_adapter import MailboxStatus, RawMetadata
from tests.conftest import make_candidate
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage


class CountingMailbox(FakeMailbox):
    """A `FakeMailbox` that tallies the adapter calls made against it.
    Each counted method is one command on the wire for the real
    adapter, which is what the assertions below are really about."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.calls: Counter[str] = Counter()

    def select(self, mailbox: str, *, readonly: bool) -> MailboxStatus:
        self.calls["select"] += 1
        return super().select(mailbox, readonly=readonly)

    def fetch_metadata(
        self,
        uids: Sequence[int],
        headers: Sequence[str],
    ) -> tuple[RawMetadata, ...]:
        self.calls["fetch_metadata"] += 1
        return super().fetch_metadata(uids, headers)

    def fetch_raw(self, uid: int) -> bytes:
        self.calls["fetch_raw"] += 1
        return super().fetch_raw(uid)

    def fetch_metadata_and_raw(self, uid: int) -> tuple[RawMetadata, bytes] | None:
        self.calls["fetch_metadata_and_raw"] += 1
        return super().fetch_metadata_and_raw(uid)

    def move(self, uid: int, destination: str) -> None:
        self.calls["move"] += 1
        super().move(uid, destination)

    def add_keyword(self, uid: int, keyword: str) -> None:
        self.calls["add_keyword"] += 1
        super().add_keyword(uid, keyword)


def _raw(n: int) -> bytes:
    return (
        f"From: sender{n}@example.com\r\n"
        f"Subject: subject {n}\r\n"
        f"Message-Id: <m{n}@example.com>\r\n"
        "\r\n"
        f"body of message {n}\r\n"
    ).encode()


def _build(
    conn: sqlite3.Connection,
    *,
    count: int,
    actions: list[str],
    mailboxes: list[str] | None = None,
) -> tuple[CountingMailbox, list[execute.ExecutionItem], str]:
    """`mailboxes`, when given, places message *n* in `mailboxes[n]`
    instead of all of them in INBOX -- the interleaving that the
    selection cache has to notice."""
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
    base = datetime(2026, 6, 1, tzinfo=UTC)
    homes = mailboxes if mailboxes is not None else ["INBOX"] * count
    stored = [
        _StoredMessage(
            uid=n,
            raw=_raw(n),
            flags=set(),
            internaldate=base + timedelta(minutes=n),
            mailbox=homes[n - 1],
        )
        for n in range(1, count + 1)
    ]
    mb = CountingMailbox(
        messages=stored,
        uidvalidity={"INBOX": 1000, "Trash": 1000, "Archive": 1000},
        capabilities=frozenset({"MOVE"}),
        accepts_custom_keywords=True,
    )
    rule = RuleConfig.model_validate(
        {"id": "r", "when": {"older-than": "1d"}, "actions": actions}
    )
    resolved = policy.resolve_actions(rule, trash_mailbox="Trash")

    items = []
    for message in stored:
        candidate_id = state.upsert_candidate(
            conn,
            make_candidate(
                account_id=account_id,
                mailbox=message.mailbox,
                message_id=f"<m{message.uid}@example.com>",
                uid=message.uid,
            ),
        )
        items.append(
            execute.ExecutionItem(
                candidate_id=candidate_id,
                key=MessageKey(account_id, message.mailbox, 1000, message.uid),
                fingerprint=fingerprint(
                    message_id=f"<m{message.uid}@example.com>",
                    internaldate=message.internaldate,
                    rfc822_size=len(message.raw),
                    from_address=None,
                    subject=None,
                ),
                message_id=f"<m{message.uid}@example.com>",
                winning_rule="r",
                actions=resolved,
            )
        )
    return mb, items, run_id


def _run(
    conn: sqlite3.Connection,
    mb: CountingMailbox,
    items: list[execute.ExecutionItem],
    run_id: str,
    backup_dir: Path,
) -> execute.ExecuteSummary:
    return execute.execute_items(
        conn,
        mailbox=mb,
        items=items,
        run_id=run_id,
        backup_dir=backup_dir,
        max_actions=None,
        dry_run=False,
        fail_fast=False,
    )


def test_backup_and_trash_costs_three_commands_per_message(tmp_path: Path) -> None:
    """The common shape -- back the message up, then move it to Trash.
    One combined metadata+body FETCH, one existence SEARCH inside
    `move`, one MOVE; the SELECT is amortised across every message in
    the mailbox rather than paid per message."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        count = 25
        mb, items, run_id = _build(conn, count=count, actions=["backup", "trash"])
        summary = _run(conn, mb, items, run_id, tmp_path / "backups")

        assert [o.status for o in summary.outcomes] == ["completed"] * count
        assert mb.calls["select"] == 1
        assert mb.calls["fetch_metadata_and_raw"] == count
        # The separate body fetch is gone: the re-verify FETCH carries it.
        assert mb.calls["fetch_raw"] == 0
        assert mb.calls["fetch_metadata"] == 0
        assert mb.calls["move"] == count
    finally:
        conn.close()


def test_label_only_never_downloads_message_bodies(tmp_path: Path) -> None:
    """A rule that only labels has no backup to feed, so it must keep
    using the cheap metadata-only FETCH rather than pulling every
    message's full bytes down for nothing."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        count = 10
        mb, items, run_id = _build(conn, count=count, actions=["label:seen-by-lia"])
        summary = _run(conn, mb, items, run_id, tmp_path / "backups")

        assert [o.status for o in summary.outcomes] == ["completed"] * count
        assert mb.calls["select"] == 1
        assert mb.calls["fetch_metadata"] == count
        assert mb.calls["fetch_metadata_and_raw"] == 0
        assert mb.calls["fetch_raw"] == 0
        assert mb.calls["add_keyword"] == count
    finally:
        conn.close()


def test_select_is_reissued_when_the_mailbox_changes(tmp_path: Path) -> None:
    """Amortising the SELECT must not mean *skipping* it: items in a
    different mailbox than the last one still get their own."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        mb, items, run_id = _build(
            conn,
            count=4,
            actions=["backup", "trash"],
            mailboxes=["INBOX", "Archive", "INBOX", "Archive"],
        )
        summary = _run(conn, mb, items, run_id, tmp_path / "backups")

        assert [o.status for o in summary.outcomes] == ["completed"] * 4
        # One per item, where four consecutive INBOX items cost one in
        # total (asserted above).
        assert mb.calls["select"] == 4
    finally:
        conn.close()


def test_a_failure_forces_the_next_item_to_reselect(tmp_path: Path) -> None:
    """A failed item may have failed because the connection itself went
    wrong, so the cached selection is not trusted afterwards."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        mb, items, run_id = _build(conn, count=3, actions=["backup", "trash"])
        broken = {items[0].key.uid}

        original_move = mb.move

        def flaky_move(uid: int, destination: str) -> None:
            if uid in broken:
                raise OSError("connection reset")
            original_move(uid, destination)

        mb.move = flaky_move  # type: ignore[method-assign]
        summary = _run(conn, mb, items, run_id, tmp_path / "backups")

        assert [o.status for o in summary.outcomes] == [
            "failed",
            "completed",
            "completed",
        ]
        # One SELECT to begin with, one more forced by the failure.
        assert mb.calls["select"] == 2
    finally:
        conn.close()
