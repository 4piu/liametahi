"""Execute phase and reconcile pass (spec section 4.3, section 4.0).

Both are callables Unit 5's orchestrator sequences (contracts section 7:
"Units 2-4 each expose their phase as a callable Unit 5 will sequence").
This module owns claim-before-mutate, re-verify-before-mutate,
backup-before-trash enforcement, the opt-in `max_actions_per_run` cap
(unset means uncapped, spec §6), and closing out `action_attempts` rows
left non-terminal by a crashed previous run of the same task.

Result-item bookkeeping note: `state.py` provides no way to update a
`result_items` row after inserting it, only `insert_result_item`
(contracts section 5's typed surface is insert-only there). For an item
that reaches the run-actions step, this module therefore inserts the
row once with `status="pending"` -- itself a legitimate status in the
spec section 9 vocabulary -- purely so the `action_attempts` rows that
follow have a `result_id` to reference (contracts section 4's foreign
key), and never updates it again. `report.py` derives the *displayed*
status for a `"pending"` result item from its `action_attempts`
children (all completed -> completed, any failed -> failed, etc.),
which is also exactly what makes those children -- not the parent row
-- the right thing for the reconcile pass below to inspect.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from liametahi import backup, state
from liametahi.backup import (
    MailboxAdapter,
    RawMetadata,
    is_unsupported_error,
    is_vanished_error,
)
from liametahi.domain import MessageKey
from liametahi.domain import fingerprint as compute_fingerprint
from liametahi.policy import ResolvedAction

ItemStatus = Literal[
    "completed",
    "dry_run",
    "failed",
    "capped",
    "claimed_by_other_run",
    "unsupported",
    "vanished",
    "changed",
]

_ReverifyVerdict = Literal["ok", "vanished", "changed"]


@dataclass(frozen=True, slots=True)
class ExecutionItem:
    """One matched, non-shadowed, non-protected candidate ready to have
    its winning rule's action list run (spec section 4.2 step 8's
    output, this module's input)."""

    candidate_id: int
    key: MessageKey  # pre-mutation message key
    fingerprint: str  # pre-mutation fingerprint, for re-verification
    message_id: str | None
    winning_rule: str
    actions: tuple[ResolvedAction, ...]


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    candidate_id: int
    result_id: int | None
    status: ItemStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ExecuteSummary:
    outcomes: tuple[ExecutionOutcome, ...]
    actions_performed: int
    stopped_early: bool  # True if --fail-fast halted before every item ran


def execute_items(
    conn: sqlite3.Connection,
    *,
    mailbox: MailboxAdapter,
    items: Sequence[ExecutionItem],
    run_id: str,
    backup_dir: Path,
    max_actions_per_run: int | None,
    dry_run: bool,
    fail_fast: bool,
) -> ExecuteSummary:
    """spec section 4.3. `items` must already be in the order the caller
    wants the cap applied (spec section 4.1: oldest `INTERNALDATE`
    first). `max_actions_per_run` is opt-in: `None` means no cap at all
    (spec §6) and the cap check below is skipped entirely. `dry_run`
    performs no claim, no mailbox call, and no mutation, but still
    applies the cap when one is configured and still records one
    `result_items` row per candidate with its intended actions (so
    `report` and a future real run agree on what the cap would do).
    """
    outcomes: list[ExecutionOutcome] = []
    actions_done = 0

    for item in items:
        if max_actions_per_run is not None and actions_done >= max_actions_per_run:
            result_id = state.insert_result_item(
                conn,
                run_id=run_id,
                candidate_id=item.candidate_id,
                status="capped",
                winning_rule=item.winning_rule,
            )
            outcomes.append(ExecutionOutcome(item.candidate_id, result_id, "capped"))
            continue

        if dry_run:
            result_id = state.insert_result_item(
                conn,
                run_id=run_id,
                candidate_id=item.candidate_id,
                status="dry_run",
                winning_rule=item.winning_rule,
            )
            for seq, action in enumerate(item.actions):
                state.insert_action_attempt(
                    conn,
                    run_id=run_id,
                    result_id=result_id,
                    seq=seq,
                    action=action.action,
                    state="skipped",
                    key=item.key,
                    fingerprint=item.fingerprint,
                )
            actions_done += 1
            outcomes.append(ExecutionOutcome(item.candidate_id, result_id, "dry_run"))
            continue

        actions_done += 1
        outcome_status, outcome_result_id, outcome_error = _execute_one(
            conn, mailbox=mailbox, item=item, run_id=run_id, backup_dir=backup_dir
        )
        outcomes.append(
            ExecutionOutcome(
                item.candidate_id, outcome_result_id, outcome_status, outcome_error
            )
        )
        if outcome_status == "failed" and fail_fast:
            return ExecuteSummary(tuple(outcomes), actions_done, True)

    return ExecuteSummary(tuple(outcomes), actions_done, False)


def _execute_one(
    conn: sqlite3.Connection,
    *,
    mailbox: MailboxAdapter,
    item: ExecutionItem,
    run_id: str,
    backup_dir: Path,
) -> tuple[ItemStatus, int | None, str | None]:
    # Claim before mutate (spec section 10, section 4.3 point 3): a key
    # held by a different live run is never stolen.
    claimed = state.claim_key(conn, key=item.key, run_id=run_id)
    if not claimed:
        result_id = state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=item.candidate_id,
            status="claimed_by_other_run",
            winning_rule=item.winning_rule,
        )
        return "claimed_by_other_run", result_id, None

    try:
        verdict, meta = _reverify(
            conn, mailbox, key=item.key, expected_fingerprint=item.fingerprint
        )
        if verdict != "ok":
            result_id = state.insert_result_item(
                conn,
                run_id=run_id,
                candidate_id=item.candidate_id,
                status=verdict,
                winning_rule=item.winning_rule,
            )
            return verdict, result_id, None
        assert meta is not None  # guaranteed by _reverify when verdict == "ok"

        # See module docstring: "pending" here is a deliberate sentinel,
        # not a bug -- the action_attempts children below are the real
        # record of what happened.
        result_id = state.insert_result_item(
            conn,
            run_id=run_id,
            candidate_id=item.candidate_id,
            status="pending",
            winning_rule=item.winning_rule,
        )
        final_status, error = _run_action_sequence(
            conn,
            mailbox=mailbox,
            item=item,
            run_id=run_id,
            result_id=result_id,
            backup_dir=backup_dir,
            meta=meta,
        )
        return final_status, result_id, error
    finally:
        state.release_key(conn, key=item.key)


def _reverify(
    conn: sqlite3.Connection,
    mailbox: MailboxAdapter,
    *,
    key: MessageKey,
    expected_fingerprint: str,
) -> tuple[_ReverifyVerdict, RawMetadata | None]:
    """spec section 4.3 point 4: re-fetch by message key and confirm
    existence and an unchanged fingerprint before any mutation.

    Recomputes `domain.fingerprint` from a freshly re-fetched
    `MESSAGE-ID` header plus the re-fetched `INTERNALDATE`/
    `RFC822.SIZE` -- exactly the inputs `fingerprint()` actually uses
    when a `Message-ID` is present (contracts section 5.1), so this
    needs no address/subject parsing. For the rare message with no
    `Message-ID`, reconstructing the same parsing `imap_adapter.py`
    (Unit 2, not yet built) will apply to build a `Candidate` is out of
    this module's reach; instead this falls back to comparing the
    freshly-fetched size and `INTERNALDATE` against the stored
    candidate row. This is coarser than true fingerprint equality but
    still refuses to mutate on any observed change, which is what the
    safety property requires; it is documented here and in the final
    report as a narrower check than the common (`Message-ID`-present)
    path.
    """
    try:
        status = mailbox.select(key.mailbox, readonly=False)
    except Exception as exc:
        if is_vanished_error(exc):
            return "vanished", None
        raise
    if status.uidvalidity != key.uidvalidity:
        return "changed", None

    try:
        fetched = mailbox.fetch_metadata([key.uid], headers=["MESSAGE-ID"])
    except Exception as exc:
        if is_vanished_error(exc):
            return "vanished", None
        raise
    if not fetched:
        return "vanished", None
    meta = fetched[0]

    # RawMetadata.headers is lowercased-key, multi-valued (contracts
    # §5.4); take the first value of "message-id" if present.
    message_id_values = meta.headers.get("message-id")
    message_id = message_id_values[0] if message_id_values else None
    if message_id:
        fresh = compute_fingerprint(
            message_id=message_id,
            internaldate=meta.internaldate,
            rfc822_size=meta.rfc822_size,
            from_address=None,
            subject=None,
        )
        return ("ok", meta) if fresh == expected_fingerprint else ("changed", None)

    stored = state.get_candidate(conn, key=key)
    if stored is None:
        return "changed", None
    _, candidate = stored
    unchanged = (
        candidate.rfc822_size == meta.rfc822_size
        and candidate.internaldate == meta.internaldate
    )
    return ("ok", meta) if unchanged else ("changed", None)


def _run_action_sequence(
    conn: sqlite3.Connection,
    *,
    mailbox: MailboxAdapter,
    item: ExecutionItem,
    run_id: str,
    result_id: int,
    backup_dir: Path,
    meta: RawMetadata,
) -> tuple[ItemStatus, str | None]:
    """spec section 4.3 points 5-6 / spec's non-negotiables: run actions
    strictly in order; the single remote mutation only after every
    preceding action in the sequence succeeded; `trash` never mutates
    without a prior successful backup in *this* sequence unless the
    rule set `allow_trash_without_backup`."""
    backup_completed = False
    aborted = False
    final_status: ItemStatus = "completed"
    error: str | None = None

    for seq, action in enumerate(item.actions):
        if aborted:
            state.insert_action_attempt(
                conn,
                run_id=run_id,
                result_id=result_id,
                seq=seq,
                action=action.action,
                state="skipped",
                key=item.key,
                fingerprint=item.fingerprint,
            )
            continue

        attempt_id = state.insert_action_attempt(
            conn,
            run_id=run_id,
            result_id=result_id,
            seq=seq,
            action=action.action,
            state="pending",
            key=item.key,
            fingerprint=item.fingerprint,
        )
        state.update_action_attempt_state(
            conn, attempt_id=attempt_id, state="in_flight"
        )

        if action.kind == "backup":
            try:
                result = backup.write_verified_backup(
                    conn,
                    mailbox=mailbox,
                    backup_dir=backup_dir,
                    key=item.key,
                    fingerprint=item.fingerprint,
                    message_id=item.message_id,
                    original_flags=meta.flags,
                    internaldate=meta.internaldate,
                    run_id=run_id,
                )
            except backup.BackupError as exc:
                # Backup verification failed: the mailbox has not been
                # touched, and it must stay that way (spec section 11).
                state.update_action_attempt_state(
                    conn,
                    attempt_id=attempt_id,
                    state="failed",
                    error=str(exc)[:500],
                    finished=True,
                )
                aborted, final_status, error = True, "failed", str(exc)
                continue
            state.update_action_attempt_state(
                conn,
                attempt_id=attempt_id,
                state="completed",
                backup_id=result.backup_id,
                finished=True,
            )
            backup_completed = True
            continue

        # A remote mutation: trash, move_to, or label.
        if action.requires_prior_backup and not backup_completed:
            message = (
                f"'{action.action}' requires a preceding successful backup "
                "in this action sequence and none succeeded; the mailbox "
                "was not touched"
            )
            state.update_action_attempt_state(
                conn,
                attempt_id=attempt_id,
                state="failed",
                error=message,
                finished=True,
            )
            aborted, final_status, error = True, "failed", message
            continue

        destination = action.destination
        if destination is None:  # pragma: no cover - policy.py always sets this
            raise AssertionError(
                f"resolved remote-mutation action missing a destination: "
                f"{action.action!r}"
            )

        try:
            if action.kind == "label":
                mailbox.add_keyword(item.key.uid, destination)
            else:
                mailbox.move(item.key.uid, destination)
        except Exception as exc:
            if is_vanished_error(exc):
                state.update_action_attempt_state(
                    conn, attempt_id=attempt_id, state="vanished", finished=True
                )
                aborted, final_status = True, "vanished"
            elif is_unsupported_error(exc):
                state.update_action_attempt_state(
                    conn, attempt_id=attempt_id, state="unsupported", finished=True
                )
                aborted, final_status = True, "unsupported"
            else:
                state.update_action_attempt_state(
                    conn,
                    attempt_id=attempt_id,
                    state="failed",
                    error=str(exc)[:500],
                    finished=True,
                )
                aborted, final_status, error = True, "failed", str(exc)
            continue

        state.update_action_attempt_state(
            conn, attempt_id=attempt_id, state="completed", finished=True
        )

    return final_status, error


# --- Reconcile pass (spec section 4.0) -------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileSummary:
    rows_closed: int


def reconcile_task(
    conn: sqlite3.Connection,
    *,
    task: str,
    mailbox: MailboxAdapter | None,
) -> ReconcileSummary:
    """spec section 4.0: close out every `action_attempts` row left
    `pending`/`in_flight` for `task` by a previous run, and release the
    key claims those rows imply. Safe to call at the very start of a new
    run: the task lock (spec section 10) guarantees any earlier run of
    the *same* task has already exited, crashed or not, before this run
    could acquire the lock -- so every open row found here belongs to a
    definitely-dead process, and every claim held for that same key is
    definitely stale.

    `mailbox`, when given, is used only to re-check whether a
    non-`backup` action's target still exists at its recorded key (an
    already-connected, read-only-is-fine handle is enough: reconcile
    never mutates anything). Without a mailbox, non-`backup` rows are
    conservatively closed `failed` rather than guessed at.
    """
    rows = state.open_action_attempts(conn, task=task)
    closed = 0
    for row in rows:
        key = MessageKey(
            row["account_id"], row["mailbox"], row["uidvalidity"], row["uid"]
        )
        if row["action"] == "backup":
            new_state, error, backup_id = _reconcile_backup_row(conn, row)
        else:
            new_state, error, backup_id = _reconcile_mutation_row(mailbox, row)
        state.update_action_attempt_state(
            conn,
            attempt_id=row["attempt_id"],
            state=new_state,
            error=error,
            backup_id=backup_id,
            finished=True,
        )
        state.release_key(conn, key=key)
        state.append_audit_event(
            conn,
            run_id=row["run_id"],
            kind="reconciled",
            subject=key.render(),
            data={
                "previous_state": row["state"],
                "new_state": new_state,
                "action": row["action"],
            },
        )
        closed += 1
    return ReconcileSummary(rows_closed=closed)


def _reconcile_backup_row(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> tuple[str, str | None, str | None]:
    """A `backup` action doesn't relocate its message, so "did it
    finish" is answered by asking whether its manifest row was actually
    committed (spec section 11's last step) -- exactly what a crash
    between that commit and the `action_attempts` update would leave
    unresolved (spec section 4.0's motivating example)."""
    backup_id = row["backup_id"]
    if backup_id:
        existing = state.get_backup(conn, str(backup_id))
        if existing is not None:
            return "completed", None, str(backup_id)
    return (
        "failed",
        "left non-terminal by a crashed run; no committed backup manifest found",
        None,
    )


def _reconcile_mutation_row(
    mailbox: MailboxAdapter | None, row: sqlite3.Row
) -> tuple[str, str | None, str | None]:
    """A `trash`/`move_to`/`label` action's own key would still be
    occupied by the same message if the mutation never happened; if the
    message is gone from that key entirely, the most likely explanation
    is that this exact mutation succeeded just before the crash."""
    if mailbox is None:
        return (
            "failed",
            "left non-terminal by a crashed run; no mailbox available to re-verify",
            None,
        )
    try:
        mailbox.select(str(row["mailbox"]), readonly=True)
        found = mailbox.fetch_metadata([int(row["uid"])], headers=[])
    except Exception as exc:
        if is_vanished_error(exc):
            return "completed", None, None
        return "failed", f"reconcile re-check failed: {exc}", None
    if not found:
        return "completed", None, None
    return (
        "failed",
        "left non-terminal by a crashed run; message unchanged at its original key",
        None,
    )
