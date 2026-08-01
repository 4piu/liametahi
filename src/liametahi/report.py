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
from datetime import UTC, datetime
from typing import Any

from liametahi import state
from liametahi.domain import MessageKey

#: spec section 9: hidden from the default table/JSON view, shown with
#: `--verbose`. Every other status (including `pending` rows whose
#: derived status turns out to still be `pending`) is shown by default.
QUIET_STATUSES = frozenset(
    {"no_match", "cached_no_match", "protected", "shadowed", "restored"}
)

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
        f"  started={_display_time(run.started_at)}  "
        f"ended={_display_time(run.ended_at)}  "
        f"dry_run={_yes_no(run.dry_run)}  "
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

    rows = [
        [
            item.message_key,
            item.from_address or "-",
            item.winning_rule or "-",
            item.status,
            ",".join(f"{a.action}:{a.state}" for a in item.actions) or "-",
            ",".join(a.backup_id for a in item.actions if a.backup_id) or "-",
        ]
        for item in items
    ]
    lines.extend(
        _format_table(
            ["MESSAGE KEY", "FROM", "RULE", "STATUS", "ACTIONS", "BACKUP"],
            rows,
            max_widths=[30, 30, 18, 20, 34, 0],
        )
    )
    return "\n".join(lines)


# --- Table layout ------------------------------------------------------
#
# Columns are sized to the data rather than to fixed constants. Fixed
# widths were both too wide and too narrow at once: a message key padded
# to 38 left a chasm before a 15-character value, while a 27-character
# sender overran a 26-wide column and shoved every column after it out of
# alignment. Sizing to the widest cell removes the gaps; capping and
# ellipsizing removes the overruns.

_ELLIPSIS = "..."


def _display_time(value: str | None) -> str:
    """A stored timestamp trimmed to whole seconds, for human output only.

    Storage keeps full microsecond ISO-8601 UTC (contracts §2) and the
    JSON document keeps it verbatim, because that is the machine-readable
    shape. Sub-second precision is noise in a table, though, and costs
    seven columns twice over -- nobody reads a report to find out which
    microsecond a run started.
    """
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value  # never mangle something we do not understand
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yes_no(value: bool) -> str:
    """`True`/`False` are Python spellings leaking into a human column."""
    return "Yes" if value else "No"


def _fit(text: str, width: int) -> str:
    """`text` truncated to `width`, with the cut marked. ASCII dots
    rather than U+2026 because a report is routinely redirected to a file
    or mailed by cron, where the output encoding is not ours to assume."""
    if len(text) <= width:
        return text
    if width <= len(_ELLIPSIS):
        return text[:width]
    return text[: width - len(_ELLIPSIS)] + _ELLIPSIS


def _format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    max_widths: Sequence[int],
) -> list[str]:
    """Render a fixed-column table. Every column but the last is padded to
    the width of its widest cell (bounded by `max_widths`); the last is
    neither padded nor truncated, since nothing follows it to misalign
    and it carries identifiers worth keeping whole."""
    widths = [
        min(max([len(header), *(len(row[i]) for row in rows)]), cap)
        for i, (header, cap) in enumerate(zip(headers, max_widths, strict=True))
    ]

    def line(cells: Sequence[str]) -> str:
        padded = [
            _fit(cell, width).ljust(width)
            for cell, width in zip(cells[:-1], widths[:-1], strict=True)
        ]
        return " ".join([*padded, cells[-1]])

    head = line(headers)
    return [head, "-" * len(head), *(line(row) for row in rows)]


# --- `report --list` ---------------------------------------------------


def render_run_list(runs: Sequence[state.RunRow]) -> str:
    if not runs:
        return "(no runs recorded)"
    rows = [
        [
            run.run_id,
            run.task,
            _display_time(run.started_at),
            _display_time(run.ended_at),
            _yes_no(run.dry_run),
            str(run.exit_code) if run.exit_code is not None else "-",
        ]
        for run in runs
    ]
    return "\n".join(
        _format_table(
            ["RUN ID", "TASK", "STARTED", "ENDED", "DRY", "EXIT"],
            rows,
            max_widths=[16, 24, 20, 20, 3, 0],
        )
    )
