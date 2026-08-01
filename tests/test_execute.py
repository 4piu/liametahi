"""Tests for `liametahi.execute` (spec section 4.3, section 4.0) beyond
what `tests/test_acceptance.py` already exercises: vanished, changed,
unsupported, capped, and `--fail-fast` handling."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liametahi import execute, policy, state
from liametahi.config import RuleConfig
from liametahi.domain import MessageKey, fingerprint
from tests.conftest import make_candidate
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<x1@example.com>"


def _raw() -> bytes:
    return (
        "From: sender@example.com\r\n"
        "Subject: hi\r\n"
        f"Message-Id: {MESSAGE_ID}\r\n"
        "\r\n"
        "body\r\n"
    ).encode()


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


def _rule(actions: list[str]) -> RuleConfig:
    return RuleConfig.model_validate(
        {"id": "r", "when": {"older-than": "1d"}, "actions": actions}
    )


def _unbacked_trash_action() -> tuple[policy.ResolvedAction, ...]:
    """A resolved 'trash' action that requires a prior backup which never
    happened. `config.py` now rejects this shape at load time (a rule
    can't even be written this way any more), but `execute.py` has no
    config to consult and must still refuse to mutate independently --
    this exercises that runtime defense-in-depth directly, bypassing
    `RuleConfig` construction."""
    return (
        policy.ResolvedAction(
            action="trash",
            kind="trash",
            destination="Trash",
            requires_prior_backup=True,
            is_remote_mutation=True,
        ),
    )


def _item_and_mailbox(
    account_id: int,
    candidate_id: int,
    *,
    uid: int = 1,
    actions: list[str] | None = None,
    resolved_actions: tuple[policy.ResolvedAction, ...] | None = None,
    capabilities: frozenset[str] = frozenset({"MOVE"}),
    accepts_custom_keywords: bool = True,
    internaldate: datetime = datetime(2026, 6, 1, tzinfo=UTC),
) -> tuple[FakeMailbox, execute.ExecutionItem]:
    raw = _raw()
    mb = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=uid,
                raw=raw,
                flags=set(),
                internaldate=internaldate,
                mailbox="INBOX",
            )
        ],
        uidvalidity={"INBOX": 1000, "Trash": 1000, "Archive": 1000},
        capabilities=capabilities,
        accepts_custom_keywords=accepts_custom_keywords,
    )
    key = MessageKey(account_id, "INBOX", 1000, uid)
    fp = fingerprint(
        message_id=MESSAGE_ID,
        internaldate=internaldate,
        rfc822_size=len(raw),
        from_address=None,
        subject=None,
    )
    if resolved_actions is not None:
        resolved = resolved_actions
    else:
        rule = _rule(actions or ["move_to:Archive"])
        resolved = policy.resolve_actions(rule, trash_mailbox="Trash")
    item = execute.ExecutionItem(
        candidate_id=candidate_id,
        key=key,
        fingerprint=fp,
        message_id=MESSAGE_ID,
        winning_rule="r",
        actions=resolved,
    )
    return mb, item


def _candidate_id(conn: sqlite3.Connection, account_id: int) -> int:
    return state.upsert_candidate(
        conn, make_candidate(account_id=account_id, message_id=MESSAGE_ID)
    )


def test_vanished_message_reported_and_not_mutated(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(account_id, candidate_id)
        mb.vanish("INBOX", 1)

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "vanished"
        # The claim was released, not left held.
        assert state.get_key_claim_holder(conn, key=item.key) is None
    finally:
        state.close_database(conn)


def test_vanished_reverify_retires_candidate(tmp_path: Path) -> None:
    """sync-fix-brief Fix C, Finding 2: `_reverify` reporting `vanished`
    retires the candidate row so it stops coming back as a live
    candidate forever."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(account_id, candidate_id)
        mb.vanish("INBOX", 1)

        execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        row = conn.execute(
            "SELECT retired_at, retired_reason FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        assert row["retired_at"] is not None
        assert row["retired_reason"] == "vanished"
    finally:
        state.close_database(conn)


def test_protected_flag_added_since_scan_blocks_mutation(tmp_path: Path) -> None:
    """sync-fix-brief Fix A, Finding 1: a flag added server-side after
    scan (e.g. the user flags the message important from another
    client) is caught by the immediate pre-mutation re-check against
    freshly re-fetched flags, even though the stored candidate row was
    never told about it."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(
            account_id, candidate_id, actions=["move_to:Archive"]
        )
        # The stored candidate row (and `item.fingerprint`, which does
        # not cover flags) knows nothing about this -- it happened after
        # scan, from another IMAP client.
        mb._messages["INBOX"][0].flags.add("\\Flagged")

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
            protected_flags=["\\Flagged"],
            protect_unread=False,
        )
        assert summary.outcomes[0].status == "protected"
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (1,)  # untouched
        assert "move_to" not in ",".join(mb.mutations)
    finally:
        state.close_database(conn)


def test_protect_unread_added_since_scan_blocks_mutation(tmp_path: Path) -> None:
    """As above, but for `protect.unread`: the message is still unread
    (no `\\Seen`) at mutation time, so it must be protected even though
    the winning rule already matched it."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(
            account_id, candidate_id, actions=["move_to:Archive"]
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
            protected_flags=[],
            protect_unread=True,
        )
        assert summary.outcomes[0].status == "protected"
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (1,)  # untouched
    finally:
        state.close_database(conn)


def test_protected_flags_default_to_nothing_protected(tmp_path: Path) -> None:
    """Omitting `protected_flags`/`protect_unread` (every pre-existing
    caller) means "nothing protected", not a behaviour change."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(
            account_id, candidate_id, actions=["move_to:Archive"]
        )
        mb._messages["INBOX"][0].flags.add("\\Flagged")

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "completed"
    finally:
        state.close_database(conn)


def test_completed_move_retires_candidate(tmp_path: Path) -> None:
    """sync-fix-brief Fix C, Finding 2: a completed `move_to`/`trash`
    retires the candidate row."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(
            account_id, candidate_id, actions=["move_to:Archive"]
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "completed"
        row = conn.execute(
            "SELECT retired_at, retired_reason FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        assert row["retired_at"] is not None
        assert row["retired_reason"] == "moved"
    finally:
        state.close_database(conn)


def test_completed_label_does_not_retire_candidate(tmp_path: Path) -> None:
    """sync-fix-brief Fix C, Finding 2: `label:*` is deliberately
    excluded from retirement -- the message stays in place and may
    legitimately match other rules on a later run."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(
            account_id, candidate_id, actions=["label:Important"]
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "completed"
        row = conn.execute(
            "SELECT retired_at, retired_reason FROM candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        assert row["retired_at"] is None
        assert row["retired_reason"] is None
    finally:
        state.close_database(conn)


def test_uidvalidity_change_reported_as_changed(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(account_id, candidate_id)
        mb.bump_uidvalidity("INBOX")  # server rebuilt the mailbox since scan

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "changed"
    finally:
        state.close_database(conn)


def test_content_changed_since_scan_reported_as_changed(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(account_id, candidate_id)
        # Simulate the message being replaced/altered since the scan: a
        # different fingerprint than what the scan recorded.
        stale_item = execute.ExecutionItem(
            candidate_id=item.candidate_id,
            key=item.key,
            fingerprint="fp-does-not-match-anything",
            message_id=item.message_id,
            winning_rule=item.winning_rule,
            actions=item.actions,
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[stale_item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "changed"
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (1,)  # untouched
    finally:
        state.close_database(conn)


def test_unsupported_capability_reported_and_not_mutated(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(
            account_id,
            candidate_id,
            capabilities=frozenset(),  # no MOVE
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "unsupported"
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (1,)
    finally:
        state.close_database(conn)


def test_label_unsupported_when_no_custom_keywords(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        mb, item = _item_and_mailbox(
            account_id,
            candidate_id,
            actions=["label:Important"],
            accepts_custom_keywords=False,
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert summary.outcomes[0].status == "unsupported"
    finally:
        state.close_database(conn)


def test_max_actions_per_run_caps_remaining_items(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)

        items = []
        mailboxes = []
        for uid in (1, 2, 3):
            candidate_id = state.upsert_candidate(
                conn,
                make_candidate(
                    account_id=account_id, uid=uid, message_id=f"<x{uid}@example.com>"
                ),
            )
            raw = (
                "From: sender@example.com\r\nSubject: hi\r\n"
                f"Message-Id: <x{uid}@example.com>\r\n\r\nbody\r\n"
            ).encode()
            mb = FakeMailbox(
                messages=[
                    _StoredMessage(
                        uid=uid,
                        raw=raw,
                        flags=set(),
                        internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                        mailbox="INBOX",
                    )
                ],
                uidvalidity={"INBOX": 1000, "Archive": 1000},
            )
            key = MessageKey(account_id, "INBOX", 1000, uid)
            fp = fingerprint(
                message_id=f"<x{uid}@example.com>",
                internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                rfc822_size=len(raw),
                from_address=None,
                subject=None,
            )
            resolved = policy.resolve_actions(
                _rule(["move_to:Archive"]), trash_mailbox=None
            )
            items.append(
                execute.ExecutionItem(
                    candidate_id=candidate_id,
                    key=key,
                    fingerprint=fp,
                    message_id=f"<x{uid}@example.com>",
                    winning_rule="r",
                    actions=resolved,
                )
            )
            mailboxes.append(mb)

        # Only one mailbox actually gets used per item in this test
        # (each item has its own FakeMailbox instance simulating the
        # same server), so run each through a shared adapter facade is
        # unnecessary: exercise the cap purely via candidate count.
        combined_mailbox = mailboxes[0]
        for extra in mailboxes[1:]:
            combined_mailbox._messages.setdefault("INBOX", []).extend(
                extra._messages["INBOX"]
            )

        summary = execute.execute_items(
            conn,
            mailbox=combined_mailbox,
            items=items,
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=2,
            dry_run=False,
            fail_fast=False,
        )
        statuses = [o.status for o in summary.outcomes]
        assert statuses == ["completed", "completed", "capped"]
        assert summary.actions_performed == 2
    finally:
        state.close_database(conn)


def test_max_actions_per_run_none_means_uncapped(tmp_path: Path) -> None:
    """spec §6: an unset `max_actions_per_run` (`None`) means no cap at
    all -- every item must run, none reported `capped`."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)

        items = []
        mailboxes = []
        for uid in (1, 2, 3):
            candidate_id = state.upsert_candidate(
                conn,
                make_candidate(
                    account_id=account_id, uid=uid, message_id=f"<y{uid}@example.com>"
                ),
            )
            raw = (
                "From: sender@example.com\r\nSubject: hi\r\n"
                f"Message-Id: <y{uid}@example.com>\r\n\r\nbody\r\n"
            ).encode()
            mb = FakeMailbox(
                messages=[
                    _StoredMessage(
                        uid=uid,
                        raw=raw,
                        flags=set(),
                        internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                        mailbox="INBOX",
                    )
                ],
                uidvalidity={"INBOX": 1000, "Archive": 1000},
            )
            key = MessageKey(account_id, "INBOX", 1000, uid)
            fp = fingerprint(
                message_id=f"<y{uid}@example.com>",
                internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                rfc822_size=len(raw),
                from_address=None,
                subject=None,
            )
            resolved = policy.resolve_actions(
                _rule(["move_to:Archive"]), trash_mailbox=None
            )
            items.append(
                execute.ExecutionItem(
                    candidate_id=candidate_id,
                    key=key,
                    fingerprint=fp,
                    message_id=f"<y{uid}@example.com>",
                    winning_rule="r",
                    actions=resolved,
                )
            )
            mailboxes.append(mb)

        combined_mailbox = mailboxes[0]
        for extra in mailboxes[1:]:
            combined_mailbox._messages.setdefault("INBOX", []).extend(
                extra._messages["INBOX"]
            )

        summary = execute.execute_items(
            conn,
            mailbox=combined_mailbox,
            items=items,
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=None,
            dry_run=False,
            fail_fast=False,
        )
        statuses = [o.status for o in summary.outcomes]
        assert statuses == ["completed", "completed", "completed"]
        assert summary.actions_performed == 3
    finally:
        state.close_database(conn)


def test_fail_fast_stops_remaining_items_on_failure(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)

        candidate_id_1 = _candidate_id(conn, account_id)
        mb1, failing_item = _item_and_mailbox(
            account_id,
            candidate_id_1,
            uid=1,
            resolved_actions=_unbacked_trash_action(),
        )
        # 'trash' with no preceding backup and allow_trash_without_backup
        # unset must fail without touching the mailbox.

        candidate_id_2 = state.upsert_candidate(
            conn,
            make_candidate(account_id=account_id, uid=2, message_id="<x2@example.com>"),
        )
        _mb2, second_item = _item_and_mailbox(
            account_id, candidate_id_2, uid=2, actions=["move_to:Archive"]
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb1,
            items=[failing_item, second_item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=True,
        )
        assert summary.stopped_early is True
        assert len(summary.outcomes) == 1
        assert summary.outcomes[0].status == "failed"
    finally:
        state.close_database(conn)


def test_failure_without_fail_fast_continues_to_next_item(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)

        candidate_id_1 = _candidate_id(conn, account_id)
        mb, failing_item = _item_and_mailbox(
            account_id,
            candidate_id_1,
            uid=1,
            resolved_actions=_unbacked_trash_action(),
        )
        candidate_id_2 = state.upsert_candidate(
            conn,
            make_candidate(account_id=account_id, uid=2, message_id="<x2@example.com>"),
        )
        raw2 = (
            b"From: sender@example.com\r\nSubject: hi\r\n"
            b"Message-Id: <x2@example.com>\r\n\r\nbody\r\n"
        )
        mb._messages["INBOX"].append(
            _StoredMessage(
                uid=2,
                raw=raw2,
                flags=set(),
                internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                mailbox="INBOX",
            )
        )
        key2 = MessageKey(account_id, "INBOX", 1000, 2)
        fp2 = fingerprint(
            message_id="<x2@example.com>",
            internaldate=datetime(2026, 6, 1, tzinfo=UTC),
            rfc822_size=len(raw2),
            from_address=None,
            subject=None,
        )
        second_item = execute.ExecutionItem(
            candidate_id=candidate_id_2,
            key=key2,
            fingerprint=fp2,
            message_id="<x2@example.com>",
            winning_rule="r",
            actions=policy.resolve_actions(
                _rule(["move_to:Archive"]), trash_mailbox=None
            ),
        )

        summary = execute.execute_items(
            conn,
            mailbox=mb,
            items=[failing_item, second_item],
            run_id=run_id,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert [o.status for o in summary.outcomes] == ["failed", "completed"]
        mb.select("Archive", readonly=True)
        assert len(mb.search_uids()) == 1  # the second item's message moved there
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (1,)  # the failed item's message stayed put
    finally:
        state.close_database(conn)


def test_reconcile_no_open_rows_is_a_noop(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        summary = execute.reconcile_task(conn, task="t", mailbox=None)
        assert summary.rows_closed == 0
    finally:
        state.close_database(conn)


def test_reconcile_backup_row_completed_when_manifest_exists(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        result_id = state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=candidate_id,
            status="pending",
            winning_rule="r",
        )
        key = MessageKey(account_id, "INBOX", 1000, 1)
        state.claim_key(conn, key=key, run_id=run_id)
        from liametahi import backup as backup_mod

        backup_id = state.new_backup_id()
        state.insert_backup(
            conn,
            backup_id=backup_id,
            account_id=account_id,
            mailbox="INBOX",
            uidvalidity=1000,
            uid=1,
            fingerprint="fp",
            message_id=MESSAGE_ID,
            sha256="deadbeef",
            byte_count=10,
            relative_path="de/ad/deadbeef.eml",
            original_mailbox="INBOX",
            original_flags=[],
            internaldate="2026-06-01T00:00:00Z",
            run_id=run_id,
        )
        attempt_id = state.insert_action_attempt(
            conn,
            run_id=run_id,
            result_id=result_id,
            seq=0,
            action="backup",
            state="pending",
            key=key,
            fingerprint="fp",
            backup_id=backup_id,
        )
        state.update_action_attempt_state(
            conn, attempt_id=attempt_id, state="in_flight"
        )

        summary = execute.reconcile_task(conn, task="t", mailbox=None)
        assert summary.rows_closed == 1
        row = conn.execute(
            "SELECT state, backup_id FROM action_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert row["state"] == "completed"
        assert row["backup_id"] == backup_id
        assert backup_mod  # silence unused-import lint if reordered
    finally:
        state.close_database(conn)


def test_reconcile_backup_row_failed_when_no_manifest(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        result_id = state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=candidate_id,
            status="pending",
            winning_rule="r",
        )
        key = MessageKey(account_id, "INBOX", 1000, 1)
        state.claim_key(conn, key=key, run_id=run_id)
        attempt_id = state.insert_action_attempt(
            conn,
            run_id=run_id,
            result_id=result_id,
            seq=0,
            action="backup",
            state="pending",
            key=key,
            fingerprint="fp",
        )
        state.update_action_attempt_state(
            conn, attempt_id=attempt_id, state="in_flight"
        )

        summary = execute.reconcile_task(conn, task="t", mailbox=None)
        assert summary.rows_closed == 1
        row = conn.execute(
            "SELECT state FROM action_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        assert row["state"] == "failed"
        assert state.get_key_claim_holder(conn, key=key) is None
    finally:
        state.close_database(conn)


def test_reconcile_mutation_row_failed_when_message_unchanged(tmp_path: Path) -> None:
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        candidate_id = _candidate_id(conn, account_id)
        result_id = state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=candidate_id,
            status="pending",
            winning_rule="r",
        )
        key = MessageKey(account_id, "INBOX", 1000, 1)
        state.claim_key(conn, key=key, run_id=run_id)
        attempt_id = state.insert_action_attempt(
            conn,
            run_id=run_id,
            result_id=result_id,
            seq=0,
            action="move_to:Archive",
            state="pending",
            key=key,
            fingerprint="fp",
        )
        state.update_action_attempt_state(
            conn, attempt_id=attempt_id, state="in_flight"
        )
        # Message is still present at its recorded key: the mutation
        # clearly never happened.
        mb = FakeMailbox(
            messages=[
                _StoredMessage(
                    uid=1,
                    raw=_raw(),
                    flags=set(),
                    internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                    mailbox="INBOX",
                )
            ],
            uidvalidity={"INBOX": 1000},
        )

        summary = execute.reconcile_task(conn, task="t", mailbox=mb)
        assert summary.rows_closed == 1
        row = conn.execute(
            "SELECT state FROM action_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        assert row["state"] == "failed"
    finally:
        state.close_database(conn)
