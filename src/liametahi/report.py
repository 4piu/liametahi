"""Table and JSON report rendering (spec section 9; contracts section
5.5's pinned JSON shape).

`report` never touches the mailbox or the model (spec section 9): every
function here only reads from SQLite through `conn`. Two reads this
module needs -- a run's account name, and result items/action attempts
joined for a run -- have no corresponding query in `state.py`'s typed
surface (it exposes `insert_result_item`/`insert_action_attempt` but no
matching list-by-run query, and `upsert_account`/`get_account_id` but
no id-to-name lookup). These are read-only, narrowly scoped to exactly
what rendering needs, and documented at each call site; see also the
same gap noted in `backup.py`. This is flagged in the final report as
something `state.py` should grow proper query functions for.

Status derivation: a `result_items` row for a candidate whose winning
rule actually ran actions is written once with `status="pending"` (see
`execute.py`'s module docstring for why) and is never updated after
that. The *displayed* status for such a row is derived here from its
`action_attempts` children -- this is the one piece of interpretation
`report.py` does rather than just formatting stored data, and it is the
direct counterpart of `execute.py`'s design, not an independent
decision.
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from liametahi import state
from liametahi.domain import MessageKey

#: spec section 9: hidden from the default table/JSON view, shown with
#: `--verbose`. Every other status (including `pending` rows whose
#: derived status turns out to still be `pending`) is shown by default.
QUIET_STATUSES = frozenset({"no_match", "cached_no_match", "protected", "shadowed"})

REPORT_VERSION = 1


class ReportNotFoundError(Exception):
    """No stored run matches the requested run id (spec section 9)."""


@dataclass(frozen=True, slots=True)
class ReportAction:
    action: str
    state: str
    backup_id: str | None


@dataclass(frozen=True, slots=True)
class ReportItem:
    message_key: str
    fingerprint: str
    from_address: str | None
    subject: str | None
    winning_rule: str | None
    shadowed_by: str | None
    status: str
    actions: tuple[ReportAction, ...]


@dataclass(frozen=True, slots=True)
class ReportTotals:
    scanned: int
    acted: int
    failed: int
    llm_calls: int
    by_status: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ReportData:
    run: state.RunRow
    account_name: str
    items: tuple[ReportItem, ...]  # every item, including quiet statuses
    totals: ReportTotals


# --- Reads not covered by state.py's typed surface (see module docstring) -


def _account_name(conn: sqlite3.Connection, account_id: int) -> str:
    row = conn.execute(
        "SELECT name FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    return str(row["name"]) if row is not None else f"<unknown account {account_id}>"


def _fetch_result_items(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT result_items.*, candidates.account_id AS c_account_id,
               candidates.mailbox AS c_mailbox,
               candidates.uidvalidity AS c_uidvalidity,
               candidates.uid AS c_uid,
               candidates.fingerprint AS c_fingerprint,
               candidates.from_address AS c_from_address,
               candidates.subject AS c_subject
        FROM result_items
        JOIN candidates ON candidates.candidate_id = result_items.candidate_id
        WHERE result_items.run_id = ?
        ORDER BY result_items.result_id
        """,
        (run_id,),
    ).fetchall()


def _fetch_action_attempts(
    conn: sqlite3.Connection, result_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM action_attempts WHERE result_id = ? ORDER BY seq",
        (result_id,),
    ).fetchall()


def _effective_status(row: sqlite3.Row, action_rows: Sequence[sqlite3.Row]) -> str:
    stored = str(row["status"])
    if stored != "pending" or not action_rows:
        return stored
    states = [str(a["state"]) for a in action_rows]
    if any(s == "failed" for s in states):
        return "failed"
    if any(s == "unsupported" for s in states):
        return "unsupported"
    if any(s == "vanished" for s in states):
        return "vanished"
    if all(s == "completed" for s in states):
        return "completed"
    # Genuinely still open (e.g. a crash the reconcile pass has not yet
    # visited): report it honestly rather than guessing.
    return "pending"


def load_report(conn: sqlite3.Connection, run_id: str) -> ReportData:
    """Assemble everything one stored run needs for rendering. Raises
    `ReportNotFoundError` if `run_id` names no stored run."""
    run = state.get_run(conn, run_id)
    if run is None:
        raise ReportNotFoundError(f"no such run: {run_id!r}")
    account_name = _account_name(conn, run.account_id)

    items: list[ReportItem] = []
    status_counts: dict[str, int] = {}
    acted = 0
    failed = 0
    for row in _fetch_result_items(conn, run_id):
        action_rows = _fetch_action_attempts(conn, int(row["result_id"]))
        status = _effective_status(row, action_rows)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status == "completed":
            acted += 1
        elif status == "failed":
            failed += 1
        key = MessageKey(
            int(row["c_account_id"]),
            str(row["c_mailbox"]),
            int(row["c_uidvalidity"]),
            int(row["c_uid"]),
        )
        actions = tuple(
            ReportAction(
                action=str(a["action"]), state=str(a["state"]), backup_id=a["backup_id"]
            )
            for a in action_rows
        )
        items.append(
            ReportItem(
                message_key=key.render(),
                fingerprint=str(row["c_fingerprint"]),
                from_address=row["c_from_address"],
                subject=row["c_subject"],
                winning_rule=row["winning_rule"],
                shadowed_by=row["shadowed_by"],
                status=status,
                actions=actions,
            )
        )

    totals = ReportTotals(
        scanned=run.candidates_scanned,
        acted=acted,
        failed=failed,
        llm_calls=run.llm_calls,
        by_status=status_counts,
    )
    return ReportData(
        run=run, account_name=account_name, items=tuple(items), totals=totals
    )


def _visible_items(data: ReportData, *, verbose: bool) -> tuple[ReportItem, ...]:
    if verbose:
        return data.items
    return tuple(item for item in data.items if item.status not in QUIET_STATUSES)


# --- JSON rendering (contracts section 5.5) --------------------------


def to_json_document(data: ReportData, *, verbose: bool) -> dict[str, Any]:
    """The exact document shape pinned by contracts section 5.5."""
    run = data.run
    items = _visible_items(data, verbose=verbose)
    return {
        "report_version": REPORT_VERSION,
        "run": {
            "run_id": run.run_id,
            "task": run.task,
            "account": data.account_name,
            "model": run.model_name,
            "provider": run.provider,
            "model_id": run.model_id,
            "dry_run": run.dry_run,
            "reevaluate": run.reevaluate,
            "structured_output_level": run.structured_output_level,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "exit_code": run.exit_code,
        },
        "totals": {
            "scanned": data.totals.scanned,
            "acted": data.totals.acted,
            "failed": data.totals.failed,
            "llm_calls": data.totals.llm_calls,
            "by_status": dict(data.totals.by_status),
        },
        "items": [
            {
                "message_key": item.message_key,
                "fingerprint": item.fingerprint,
                "from": item.from_address,
                "subject": item.subject,
                "winning_rule": item.winning_rule,
                "shadowed_by": item.shadowed_by,
                "status": item.status,
                "actions": [
                    {
                        "action": action.action,
                        "state": action.state,
                        "backup_id": action.backup_id,
                    }
                    for action in item.actions
                ],
            }
            for item in items
        ],
    }


def render_json(data: ReportData, *, verbose: bool) -> str:
    return json.dumps(to_json_document(data, verbose=verbose), indent=2)


# --- Table rendering (spec section 9) ---------------------------------


def render_table(data: ReportData, *, verbose: bool) -> str:
    run = data.run
    lines = [
        f"Run {run.run_id}  task={run.task}  account={data.account_name}  "
        f"model={run.model_name}",
        f"  started={run.started_at}  ended={run.ended_at or '-'}  "
        f"dry_run={run.dry_run}  "
        f"exit_code={run.exit_code if run.exit_code is not None else '-'}  "
        f"structured_output={run.structured_output_level or '-'}",
        f"  scanned={data.totals.scanned}  acted={data.totals.acted}  "
        f"failed={data.totals.failed}  llm_calls={data.totals.llm_calls}",
    ]
    by_status = ", ".join(f"{k}={v}" for k, v in sorted(data.totals.by_status.items()))
    lines.append(f"  by_status: {by_status or '(none)'}")
    lines.append("")

    items = _visible_items(data, verbose=verbose)
    if not items:
        hint = "" if verbose else " (pass --verbose to include quiet statuses)"
        lines.append(f"(no actionable or failed items){hint}")
        return "\n".join(lines)

    header = (
        f"{'MESSAGE KEY':<38} {'FROM':<26} {'RULE':<20} {'STATUS':<20} "
        f"{'ACTIONS':<30} BACKUP"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for item in items:
        actions_str = ",".join(f"{a.action}:{a.state}" for a in item.actions) or "-"
        backup_ids = ",".join(a.backup_id for a in item.actions if a.backup_id) or "-"
        lines.append(
            f"{item.message_key:<38} {(item.from_address or '-'):<26} "
            f"{(item.winning_rule or '-'):<20} {item.status:<20} "
            f"{actions_str:<30} {backup_ids}"
        )
    return "\n".join(lines)


# --- `report --list` ---------------------------------------------------


def render_run_list(runs: Sequence[state.RunRow]) -> str:
    if not runs:
        return "(no runs recorded)"
    header = (
        f"{'RUN ID':<30} {'TASK':<20} {'STARTED':<25} {'ENDED':<25} {'DRY':<6} EXIT"
    )
    lines = [header, "-" * len(header)]
    for run in runs:
        lines.append(
            f"{run.run_id:<30} {run.task:<20} {run.started_at:<25} "
            f"{(run.ended_at or '-'):<25} {str(run.dry_run):<6} "
            f"{run.exit_code if run.exit_code is not None else '-'}"
        )
    return "\n".join(lines)
