"""Work Unit 2's named acceptance tests (spec section 14): 11 (unit
tier -- see `tests/integration/test_acceptance_11_seen_flag.py` for the
integration-tier half against a real server) and 12. Both exercise
`imap_adapter.scan()` against `FakeMailbox`, plus `execute.py` (Unit 4,
already landed) for test 12's "does not re-action an already-actioned
message" half.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liametahi import execute, policy, state
from liametahi.config import RuleConfig
from liametahi.domain import MessageKey
from liametahi.imap_adapter import scan
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

_HEADERS = ["FROM", "SUBJECT", "MESSAGE-ID"]


def _raw(subject: str, message_id: str) -> bytes:
    return (
        "From: sender@example.com\r\n"
        "To: me@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Message-Id: {message_id}\r\n"
        "Date: Mon, 1 Jun 2026 08:00:00 +0000\r\n"
        "\r\n"
        "body text\r\n"
    ).encode()


def _rule(rule_id: str, actions: list[str]) -> RuleConfig:
    return RuleConfig.model_validate(
        {
            "id": rule_id,
            "priority": 0,
            "when": {"older-than": "1d"},
            "actions": actions,
        }
    )


def _setup_run(conn: sqlite3.Connection, *, account_id: int, run_id: str) -> None:
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


def test_max_candidates_per_run_unset_means_uncapped(tmp_path: Path) -> None:
    """`max_candidates_per_run` is opt-in (spec §9/§4.1 point 6): `None`
    scans every eligible candidate in one pass rather than stopping."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        internaldate = datetime(2026, 6, 1, tzinfo=UTC)
        mb = FakeMailbox(
            messages=[
                _StoredMessage(
                    uid=uid,
                    raw=_raw(f"Subject {uid}", f"<m{uid}@test>"),
                    flags=set(),
                    internaldate=internaldate,
                    mailbox="INBOX",
                )
                for uid in range(1, 4)
            ],
            uidvalidity={"INBOX": 1000},
        )

        result = scan(
            mb,
            conn,
            account_id=account_id,
            source_mailboxes=["INBOX"],
            fetch_headers=_HEADERS,
            max_candidates_per_run=None,
        )

        assert result.candidates_scanned == 3
        assert result.stopped_at_cap is False
    finally:
        state.close_database(conn)


# --- Acceptance test 11 (unit tier) --------------------------------------


def test_acceptance_11_metadata_fetch_does_not_set_seen(tmp_path: Path) -> None:
    """A metadata fetch does not set `\\Seen` on any message (spec §14.11,
    §4.1 point 4). Exercised through the real scan-phase entry point
    (`imap_adapter.scan`), not just a direct `fetch_metadata` call, so
    the whole read path -- `EXAMINE`, `SEARCH`, then `BODY.PEEK` fetch --
    is covered end to end."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        internaldate = datetime(2026, 6, 1, tzinfo=UTC)
        mb = FakeMailbox(
            messages=[
                _StoredMessage(
                    uid=1,
                    raw=_raw("Digest", "<m1@test>"),
                    flags=set(),
                    internaldate=internaldate,
                    mailbox="INBOX",
                ),
                _StoredMessage(
                    uid=2,
                    raw=_raw("Notice", "<m2@test>"),
                    flags={"\\Flagged"},  # a pre-existing flag must survive untouched
                    internaldate=internaldate,
                    mailbox="INBOX",
                ),
            ],
            uidvalidity={"INBOX": 1000},
        )
        before = {m.uid: frozenset(m.flags) for m in mb._messages["INBOX"]}

        result = scan(
            mb,
            conn,
            account_id=account_id,
            source_mailboxes=["INBOX"],
            fetch_headers=_HEADERS,
            max_candidates_per_run=500,
        )

        after = {m.uid: frozenset(m.flags) for m in mb._messages["INBOX"]}
        assert before == after
        assert not any("\\Seen" in flags for flags in after.values())
        assert result.candidates_scanned == 2
        # No mutating adapter call was ever made by the scan phase.
        assert mb.mutations == ()
    finally:
        state.close_database(conn)


# --- Acceptance test 12 ---------------------------------------------------


def test_acceptance_12_uidvalidity_change_reidentifies_by_fingerprint(
    tmp_path: Path,
) -> None:
    """A UIDVALIDITY change re-identifies existing candidates by
    fingerprint and does not produce duplicate candidates or re-action
    an already-actioned message (spec §14.12, §4.1 point 2, §11)."""
    conn = state.open_database(tmp_path / "state.sqlite3")
    try:
        account_id = state.upsert_account(conn, name="a", host="h", username="u")
        internaldate = datetime(2026, 6, 1, tzinfo=UTC)
        raw_m1 = _raw("Old subject", "<m1@test>")
        raw_m2 = _raw("Other subject", "<m2@test>")
        mb = FakeMailbox(
            messages=[
                _StoredMessage(
                    uid=11,
                    raw=raw_m1,
                    flags=set(),
                    internaldate=internaldate,
                    mailbox="INBOX",
                ),
                _StoredMessage(
                    uid=12,
                    raw=raw_m2,
                    flags=set(),
                    internaldate=internaldate,
                    mailbox="INBOX",
                ),
            ],
            uidvalidity={"INBOX": 1000, "Trash": 1000},
        )

        # 1) First scan: two brand-new candidates.
        first = scan(
            mb,
            conn,
            account_id=account_id,
            source_mailboxes=["INBOX"],
            fetch_headers=_HEADERS,
            max_candidates_per_run=500,
        )
        assert first.mailboxes[0].new_candidates == 2
        assert first.mailboxes[0].reidentified_candidates == 0

        stored_m1 = state.get_candidate(
            conn, key=MessageKey(account_id, "INBOX", 1000, 11)
        )
        assert stored_m1 is not None
        candidate_id_m1, candidate_m1 = stored_m1
        fp_m1 = candidate_m1.fingerprint

        stored_m2 = state.get_candidate(
            conn, key=MessageKey(account_id, "INBOX", 1000, 12)
        )
        assert stored_m2 is not None
        _candidate_id_m2, candidate_m2 = stored_m2
        fp_m2 = candidate_m2.fingerprint

        # 2) Act on M1: move it out of INBOX for good (a stand-in for
        # "trash", without pulling backup's requirements into this test).
        rule = _rule("archive", ["move_to:Trash"])
        actions = policy.resolve_actions(rule, trash_mailbox=None)
        run_id = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id)
        item = execute.ExecutionItem(
            candidate_id=candidate_id_m1,
            key=MessageKey(account_id, "INBOX", 1000, 11),
            fingerprint=fp_m1,
            message_id="<m1@test>",
            winning_rule="archive",
            actions=actions,
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
        mb.select("INBOX", readonly=True)
        assert mb.search_uids() == (12,)  # only M2 remains
        mb.select("Trash", readonly=True)
        assert mb.search_uids() == (1,)  # M1 now lives here under a new uid

        # 3) Simulate a server-side rebuild: INBOX's UIDVALIDITY changes
        # while M2 is still present at the same UID.
        mb.bump_uidvalidity("INBOX")
        second = scan(
            mb,
            conn,
            account_id=account_id,
            source_mailboxes=["INBOX"],
            fetch_headers=_HEADERS,
            max_candidates_per_run=500,
        )
        assert second.mailboxes[0].uidvalidity_changed is True
        new_uidvalidity = second.mailboxes[0].uidvalidity
        assert new_uidvalidity != 1000
        # M2 is re-identified by fingerprint, not treated as brand new.
        assert second.mailboxes[0].new_candidates == 0
        assert second.mailboxes[0].reidentified_candidates == 1

        # No duplicate candidate rows for M2: the pre-bump (now stale)
        # row, plus exactly one fresh row under the new UIDVALIDITY.
        m2_rows = state.find_candidates_by_fingerprint(
            conn, account_id=account_id, fingerprint=fp_m2
        )
        assert len(m2_rows) == 2
        assert {candidate.key.uidvalidity for _id, candidate in m2_rows} == {
            1000,
            new_uidvalidity,
        }

        # M1 -- already actioned and gone from INBOX before the bump --
        # never resurfaces as a candidate: still exactly the one original
        # row, and it is not at the new UIDVALIDITY.
        m1_rows = state.find_candidates_by_fingerprint(
            conn, account_id=account_id, fingerprint=fp_m1
        )
        assert len(m1_rows) == 1
        assert m1_rows[0][1].key.uidvalidity == 1000

        # 4) Re-scanning again at the now-current UIDVALIDITY is
        # idempotent: no further growth for M2's fingerprint.
        third = scan(
            mb,
            conn,
            account_id=account_id,
            source_mailboxes=["INBOX"],
            fetch_headers=_HEADERS,
            max_candidates_per_run=500,
        )
        assert third.mailboxes[0].uidvalidity_changed is False
        assert third.mailboxes[0].new_candidates == 0
        assert third.mailboxes[0].reidentified_candidates == 0
        assert (
            len(
                state.find_candidates_by_fingerprint(
                    conn, account_id=account_id, fingerprint=fp_m2
                )
            )
            == 2
        )

        # 5) Defense in depth: even if a caller mistakenly replayed M1's
        # *stale* pre-action ExecutionItem (key.uidvalidity == 1000, but
        # INBOX is now at new_uidvalidity), execute_items refuses to
        # mutate anything: reported "changed", never re-actioned.
        run_id_2 = state.new_run_id()
        _setup_run(conn, account_id=account_id, run_id=run_id_2)
        stale_item = execute.ExecutionItem(
            candidate_id=candidate_id_m1,
            key=MessageKey(account_id, "INBOX", 1000, 11),
            fingerprint=fp_m1,
            message_id="<m1@test>",
            winning_rule="archive",
            actions=actions,
        )
        replay = execute.execute_items(
            conn,
            mailbox=mb,
            items=[stale_item],
            run_id=run_id_2,
            backup_dir=tmp_path / "backups",
            max_actions_per_run=50,
            dry_run=False,
            fail_fast=False,
        )
        assert replay.outcomes[0].status == "changed"
        # Trash still holds exactly the one message from step 2, not two.
        mb.select("Trash", readonly=True)
        assert mb.search_uids() == (1,)
    finally:
        state.close_database(conn)
