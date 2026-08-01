"""SQLite access layer (implementation-contracts.md §4).

This module owns every piece of SQL in the codebase; every other module
reads and writes state through the typed functions here. It applies the
connection PRAGMAs, runs numbered migrations from `migrations/`, and
implements the concurrency-sensitive primitives called out in the
contract: claim-not-steal `key_claims`, append-only `audit_events`.

Only the pieces `config check` and the unit tests in this work unit need
are exercised end-to-end here (open/migrate/close); the broader typed
CRUD surface is provided so units 2-4 never need to write ad-hoc SQL.
"""

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from liametahi.domain import Candidate, MessageKey

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LATEST_SCHEMA_VERSION = 4

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class SchemaVersionError(Exception):
    """Raised when opening a database written by a schema version newer
    than this build of liametahi understands (spec §11)."""


# --- Identifiers (contracts §3) -------------------------------------------


def _new_ulid() -> str:
    """A 26-character, uppercase, Crockford-base32, time-sortable ID:
    48-bit millisecond timestamp followed by 80 bits of randomness."""
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    value = (timestamp_ms << 80) | randomness
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_run_id() -> str:
    return f"run_{_new_ulid()}"


def new_backup_id() -> str:
    return f"bkp_{_new_ulid()}"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- Connection lifecycle and migrations -----------------------------------


def open_database(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed), apply PRAGMAs, and migrate to the
    latest known schema. Sets file mode 0600 on the database file
    (spec §12)."""
    resolved = db_path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = FULL")
    _migrate(conn)
    with contextlib.suppress(OSError):
        resolved.chmod(0o600)
    return conn


def close_database(conn: sqlite3.Connection) -> None:
    conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Group a phase's **bookkeeping** writes into one transaction, and
    therefore one fsync, instead of one per statement.

    The connection is opened in autocommit mode with
    `PRAGMA synchronous = FULL` (see `open_database`), so by default every
    INSERT is its own durable transaction. That is exactly right for the
    `action_attempts` state machine and the backup manifest -- the
    reconcile pass (spec §4.0) can only tell how far a crashed run got
    because each step was committed as it happened -- but it is pure
    overhead for the bulk bookkeeping a run also produces (candidate
    upserts, classifications, decision-cache entries, no-op result items),
    none of which is safety-critical: losing the tail of it to a crash
    just means the next run re-derives it.

    Two rules for callers, both load-bearing:

    - **Never hold one of these open across network I/O.** A model call or
      an IMAP fetch inside a write transaction blocks every other writer
      for its whole duration, against a 5-second `busy_timeout`. Do the
      I/O first, then open a transaction to record the results.
    - **Never wrap `execute.execute_items`.** Its per-statement durability
      is the crash-recovery guarantee, not an oversight.

    Re-entrant: if a transaction is already open on this connection, this
    joins it rather than raising (SQLite has no nested `BEGIN`).
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    value = row["v"] if row is not None else None
    return int(value) if value is not None else 0


def _available_migrations() -> list[tuple[int, Path]]:
    migrations = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        migrations.append((version, path))
    return migrations


def _migrate(conn: sqlite3.Connection) -> None:
    current = _current_version(conn)
    if current > _LATEST_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"database schema is at version {current}, newer than this "
            f"build of liametahi understands (latest known: "
            f"{_LATEST_SCHEMA_VERSION}); upgrade liametahi before opening "
            "this database"
        )
    for version, path in _available_migrations():
        if version <= current:
            continue
        sql = path.read_text(encoding="utf-8")
        # Embedded literally (not bound as parameters): executescript()
        # runs the whole string as one sqlite3_exec call, so wrapping it
        # in BEGIN/COMMIT here is what makes a migration atomic. `version`
        # is an int and `now` is our own ISO timestamp — both internally
        # generated, never attacker- or user-controlled.
        now = _iso_now()
        script = (
            f"BEGIN;\n{sql}\n"
            "INSERT INTO schema_version (version, applied_at) "
            f"VALUES ({version}, '{now}');\nCOMMIT;\n"
        )
        conn.executescript(script)


# --- Accounts ---------------------------------------------------------------


def upsert_account(
    conn: sqlite3.Connection, *, name: str, host: str, username: str
) -> int:
    conn.execute(
        """
        INSERT INTO accounts (name, host, username, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (name) DO UPDATE SET
            host = excluded.host, username = excluded.username
        """,
        (name, host, username, _iso_now()),
    )
    row = conn.execute(
        "SELECT account_id FROM accounts WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None
    return int(row["account_id"])


def get_account_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT account_id FROM accounts WHERE name = ?", (name,)
    ).fetchone()
    return int(row["account_id"]) if row is not None else None


# --- Mailbox state (UIDVALIDITY tracking) -----------------------------------


def record_mailbox_uidvalidity(
    conn: sqlite3.Connection, *, account_id: int, mailbox: str, uidvalidity: int
) -> None:
    conn.execute(
        """
        INSERT INTO mailbox_state (account_id, mailbox, uidvalidity, observed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (account_id, mailbox) DO UPDATE SET
            uidvalidity = excluded.uidvalidity, observed_at = excluded.observed_at
        """,
        (account_id, mailbox, uidvalidity, _iso_now()),
    )


def get_mailbox_uidvalidity(
    conn: sqlite3.Connection, *, account_id: int, mailbox: str
) -> int | None:
    row = conn.execute(
        "SELECT uidvalidity FROM mailbox_state WHERE account_id = ? AND mailbox = ?",
        (account_id, mailbox),
    ).fetchone()
    return int(row["uidvalidity"]) if row is not None else None


# --- Candidates --------------------------------------------------------------


def upsert_candidate(conn: sqlite3.Connection, candidate: Candidate) -> int:
    key = candidate.key
    conn.execute(
        """
        INSERT INTO candidates (
            account_id, mailbox, uidvalidity, uid, fingerprint, message_id,
            internaldate, rfc822_size, flags, headers_present, from_address,
            from_display, recipients, cc_count, subject, list_id,
            has_list_unsubscribe, has_attachment, auth_results, first_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, mailbox, uidvalidity, uid) DO UPDATE SET
            fingerprint = excluded.fingerprint,
            message_id = excluded.message_id,
            internaldate = excluded.internaldate,
            rfc822_size = excluded.rfc822_size,
            flags = excluded.flags,
            headers_present = excluded.headers_present,
            from_address = excluded.from_address,
            from_display = excluded.from_display,
            recipients = excluded.recipients,
            cc_count = excluded.cc_count,
            subject = excluded.subject,
            list_id = excluded.list_id,
            has_list_unsubscribe = excluded.has_list_unsubscribe,
            has_attachment = excluded.has_attachment,
            auth_results = excluded.auth_results
        """,
        (
            key.account_id,
            key.mailbox,
            key.uidvalidity,
            key.uid,
            candidate.fingerprint,
            candidate.message_id,
            _iso(candidate.internaldate),
            candidate.rfc822_size,
            json.dumps(sorted(candidate.flags)),
            json.dumps(sorted(candidate.headers_present)),
            candidate.from_address,
            candidate.from_display,
            json.dumps(list(candidate.recipients)),
            candidate.cc_count,
            candidate.subject,
            candidate.list_id,
            int(candidate.has_list_unsubscribe),
            int(candidate.has_attachment),
            candidate.auth_results,
            _iso_now(),
        ),
    )
    row = conn.execute(
        """
        SELECT candidate_id FROM candidates
        WHERE account_id=? AND mailbox=? AND uidvalidity=? AND uid=?
        """,
        (key.account_id, key.mailbox, key.uidvalidity, key.uid),
    ).fetchone()
    assert row is not None
    return int(row["candidate_id"])


def _row_to_candidate(row: sqlite3.Row) -> Candidate:
    return Candidate(
        key=MessageKey(
            row["account_id"], row["mailbox"], row["uidvalidity"], row["uid"]
        ),
        fingerprint=row["fingerprint"],
        message_id=row["message_id"],
        internaldate=_from_iso(row["internaldate"]),
        rfc822_size=row["rfc822_size"],
        flags=frozenset(json.loads(row["flags"])),
        headers_present=frozenset(json.loads(row["headers_present"])),
        from_address=row["from_address"],
        from_display=row["from_display"],
        recipients=tuple(json.loads(row["recipients"])) if row["recipients"] else (),
        cc_count=row["cc_count"],
        subject=row["subject"],
        list_id=row["list_id"],
        has_list_unsubscribe=bool(row["has_list_unsubscribe"]),
        has_attachment=bool(row["has_attachment"]),
        auth_results=row["auth_results"],
    )


def get_candidate(
    conn: sqlite3.Connection, *, key: MessageKey
) -> tuple[int, Candidate] | None:
    row = conn.execute(
        """
        SELECT * FROM candidates
        WHERE account_id=? AND mailbox=? AND uidvalidity=? AND uid=?
        """,
        (key.account_id, key.mailbox, key.uidvalidity, key.uid),
    ).fetchone()
    if row is None:
        return None
    return int(row["candidate_id"]), _row_to_candidate(row)


def find_candidates_by_fingerprint(
    conn: sqlite3.Connection, *, account_id: int, fingerprint: str
) -> list[tuple[int, Candidate]]:
    rows = conn.execute(
        "SELECT * FROM candidates WHERE account_id=? AND fingerprint=?",
        (account_id, fingerprint),
    ).fetchall()
    return [(int(row["candidate_id"]), _row_to_candidate(row)) for row in rows]


def update_candidate_flags(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    mailbox: str,
    uidvalidity: int,
    flags_by_uid: Mapping[int, frozenset[str]],
) -> int:
    """Refresh the stored `flags` column for already-known candidates
    (sync-fix-brief Fix B, Finding 1): `imap_adapter.scan()` calls this
    once per mailbox per run with one batched flags-only fetch's worth of
    results, so `has-flag`/protection stay honest run to run instead of
    being frozen at whatever they were the first time a message was
    seen. Retired rows (Fix C) are excluded -- a retired candidate's
    message has moved out of the mailbox (or vanished), so its flags
    have nothing left to refresh from.

    Returns the number of rows actually updated.

    Self-batching: the caller hands over a whole mailbox's worth of UIDs
    at once and there is no I/O in the loop, so the updates go in one
    transaction rather than one fsync per message (see `transaction`).
    """
    updated = 0
    with transaction(conn):
        for uid, flags in flags_by_uid.items():
            cur = conn.execute(
                """
                UPDATE candidates SET flags = ?
                WHERE account_id = ? AND mailbox = ? AND uidvalidity = ? AND uid = ?
                  AND retired_at IS NULL
                """,
                (json.dumps(sorted(flags)), account_id, mailbox, uidvalidity, uid),
            )
            updated += cur.rowcount
    return updated


def retire_candidate(
    conn: sqlite3.Connection, *, candidate_id: int, reason: str
) -> None:
    """Give a candidate row a terminal state (sync-fix-brief Fix C,
    Finding 2): `reason` is `"moved"` (a `trash`/`move_to:*` action
    completed), `"vanished"` (`execute._reverify` found the message gone
    from the server), or `"prior_trash"` (Fix D skipped it because its
    fingerprint already carried a completed destructive action -- see
    `runner.py`). Idempotent: a row already retired keeps its original
    `retired_at`/`retired_reason` rather than being overwritten by a
    later call. Never deletes the row (spec §11)."""
    conn.execute(
        """
        UPDATE candidates SET retired_at = ?, retired_reason = ?
        WHERE candidate_id = ? AND retired_at IS NULL
        """,
        (_iso_now(), reason, candidate_id),
    )


# --- Runs ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunRow:
    run_id: str
    task: str
    account_id: int
    model_name: str
    provider: str
    model_id: str
    dry_run: bool
    reevaluate: bool
    structured_output_level: str | None
    fetch_headers: tuple[str, ...]
    config_hash: str
    started_at: str
    ended_at: str | None
    exit_code: int | None
    candidates_scanned: int
    llm_calls: int
    input_tokens: int | None
    output_tokens: int | None


def create_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task: str,
    account_id: int,
    model_name: str,
    provider: str,
    model_id: str,
    dry_run: bool,
    reevaluate: bool,
    fetch_headers: Sequence[str],
    config_hash: str,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (
            run_id, task, account_id, model_name, provider, model_id,
            dry_run, reevaluate, structured_output_level, fetch_headers,
            config_hash, started_at, ended_at, exit_code,
            candidates_scanned, llm_calls, input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, 0, 0, NULL, NULL)
        """,
        (
            run_id,
            task,
            account_id,
            model_name,
            provider,
            model_id,
            int(dry_run),
            int(reevaluate),
            json.dumps(list(fetch_headers)),
            config_hash,
            _iso_now(),
        ),
    )


def finish_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    exit_code: int,
    candidates_scanned: int,
    llm_calls: int,
    structured_output_level: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET
            ended_at = ?, exit_code = ?, candidates_scanned = ?, llm_calls = ?,
            structured_output_level = ?, input_tokens = ?, output_tokens = ?
        WHERE run_id = ?
        """,
        (
            _iso_now(),
            exit_code,
            candidates_scanned,
            llm_calls,
            structured_output_level,
            input_tokens,
            output_tokens,
            run_id,
        ),
    )


def _row_to_run(row: sqlite3.Row) -> RunRow:
    return RunRow(
        run_id=row["run_id"],
        task=row["task"],
        account_id=row["account_id"],
        model_name=row["model_name"],
        provider=row["provider"],
        model_id=row["model_id"],
        dry_run=bool(row["dry_run"]),
        reevaluate=bool(row["reevaluate"]),
        structured_output_level=row["structured_output_level"],
        fetch_headers=tuple(json.loads(row["fetch_headers"])),
        config_hash=row["config_hash"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        exit_code=row["exit_code"],
        candidates_scanned=row["candidates_scanned"],
        llm_calls=row["llm_calls"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
    )


def get_run(conn: sqlite3.Connection, run_id: str) -> RunRow | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row is not None else None


def list_runs(conn: sqlite3.Connection, *, task: str | None = None) -> list[RunRow]:
    if task is not None:
        rows = conn.execute(
            "SELECT * FROM runs WHERE task = ? ORDER BY started_at DESC", (task,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
    return [_row_to_run(row) for row in rows]


# --- Result items and classifications ----------------------------------


def insert_result_item(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    candidate_id: int,
    status: str,
    winning_rule: str | None = None,
    shadowed_by: str | None = None,
    detail: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO result_items (
            run_id, candidate_id, status, winning_rule, shadowed_by, detail
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (run_id, candidate_id, status, winning_rule, shadowed_by, detail),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def insert_classification(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    candidate_id: int,
    input_level: str,
    input_hash: str,
    offered_rules: Sequence[str],
    matches: Sequence[str],
    needs_content: bool,
    reason: str | None,
    valid: bool,
    error: str | None,
    latency_ms: int | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO classifications (
            run_id, candidate_id, input_level, input_hash, offered_rules,
            matches, needs_content, reason, valid, error, latency_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            candidate_id,
            input_level,
            input_hash,
            json.dumps(list(offered_rules)),
            json.dumps(list(matches)),
            int(needs_content),
            reason,
            int(valid),
            error,
            latency_ms,
            _iso_now(),
        ),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


# --- LLM decision cache (spec §13) ----------------------------------------
#
# One row per (account, fingerprint, rule, rule text, input) -- `matched`
# records which way the model answered, so a re-run can reuse either a
# prior "no" (elide the rule) or a prior "yes" (skip straight to policy/
# execution) without asking again.


def get_cached_decision(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    fingerprint: str,
    rule_id: str,
    rule_text_hash: str,
    input_hash: str,
    model_id: str,
    prompt_version: int,
) -> Mapping[str, Any] | None:
    row = conn.execute(
        """
        SELECT model_id, decided_at, matched FROM llm_decision_cache
        WHERE account_id=? AND fingerprint=? AND rule_id=? AND rule_text_hash=?
          AND input_hash=? AND model_id=? AND prompt_version=?
        """,
        (
            account_id,
            fingerprint,
            rule_id,
            rule_text_hash,
            input_hash,
            model_id,
            prompt_version,
        ),
    ).fetchone()
    if row is None:
        return None
    return {
        "model_id": row["model_id"],
        "decided_at": row["decided_at"],
        "matched": bool(row["matched"]),
    }


def record_decision(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    fingerprint: str,
    rule_id: str,
    rule_text_hash: str,
    input_hash: str,
    model_id: str,
    prompt_version: int,
    matched: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO llm_decision_cache (
            account_id, fingerprint, rule_id, rule_text_hash, input_hash,
            model_id, prompt_version, decided_at, matched
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, fingerprint, rule_id, rule_text_hash, input_hash,
                     model_id, prompt_version)
        DO UPDATE SET
            decided_at = excluded.decided_at,
            matched = excluded.matched
        """,
        (
            account_id,
            fingerprint,
            rule_id,
            rule_text_hash,
            input_hash,
            model_id,
            prompt_version,
            _iso_now(),
            int(matched),
        ),
    )


# --- Key claims (spec §10, contracts §4 notes) ---------------------------


def claim_key(conn: sqlite3.Connection, *, key: MessageKey, run_id: str) -> bool:
    """Atomically claim a message key for this run. Returns False if the
    key is already claimed by a still-active (unreleased) run — a claim
    held by a live run must not be stolen."""
    cur = conn.execute(
        """
        INSERT INTO key_claims
            (account_id, mailbox, uidvalidity, uid, run_id, claimed_at, released_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT (account_id, mailbox, uidvalidity, uid) DO UPDATE SET
            run_id = excluded.run_id,
            claimed_at = excluded.claimed_at,
            released_at = NULL
        WHERE key_claims.released_at IS NOT NULL
        """,
        (key.account_id, key.mailbox, key.uidvalidity, key.uid, run_id, _iso_now()),
    )
    return cur.rowcount == 1


def release_key(conn: sqlite3.Connection, *, key: MessageKey) -> None:
    conn.execute(
        """
        UPDATE key_claims SET released_at = ?
        WHERE account_id = ? AND mailbox = ? AND uidvalidity = ? AND uid = ?
        """,
        (_iso_now(), key.account_id, key.mailbox, key.uidvalidity, key.uid),
    )


def get_key_claim_holder(conn: sqlite3.Connection, *, key: MessageKey) -> str | None:
    row = conn.execute(
        """
        SELECT run_id FROM key_claims
        WHERE account_id=? AND mailbox=? AND uidvalidity=? AND uid=?
          AND released_at IS NULL
        """,
        (key.account_id, key.mailbox, key.uidvalidity, key.uid),
    ).fetchone()
    return str(row["run_id"]) if row is not None else None


# --- Backups (spec §11) ---------------------------------------------------


def insert_backup(
    conn: sqlite3.Connection,
    *,
    backup_id: str,
    account_id: int,
    mailbox: str,
    uidvalidity: int,
    uid: int,
    fingerprint: str,
    message_id: str | None,
    sha256: str,
    byte_count: int,
    relative_path: str,
    original_mailbox: str,
    original_flags: Sequence[str],
    internaldate: str,
    run_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO backups (
            backup_id, account_id, mailbox, uidvalidity, uid, fingerprint,
            message_id, sha256, byte_count, relative_path, original_mailbox,
            original_flags, internaldate, run_id, backed_up_at, restored_to,
            restored_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            backup_id,
            account_id,
            mailbox,
            uidvalidity,
            uid,
            fingerprint,
            message_id,
            sha256,
            byte_count,
            relative_path,
            original_mailbox,
            json.dumps(list(original_flags)),
            internaldate,
            run_id,
            _iso_now(),
        ),
    )


def get_backup(conn: sqlite3.Connection, backup_id: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM backups WHERE backup_id = ?", (backup_id,)
    ).fetchone()
    return row  # type: ignore[no-any-return]


def record_restore(
    conn: sqlite3.Connection, *, backup_id: str, restored_to: str
) -> None:
    conn.execute(
        "UPDATE backups SET restored_to = ?, restored_at = ? WHERE backup_id = ?",
        (restored_to, _iso_now(), backup_id),
    )


# --- Action attempts and reconcile (spec §4.0, §4.3) ----------------------


def insert_action_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    result_id: int,
    seq: int,
    action: str,
    state: str,
    key: MessageKey,
    fingerprint: str,
    backup_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO action_attempts (
            run_id, result_id, seq, action, state, account_id, mailbox,
            uidvalidity, uid, fingerprint, backup_id, error, started_at,
            finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
        """,
        (
            run_id,
            result_id,
            seq,
            action,
            state,
            key.account_id,
            key.mailbox,
            key.uidvalidity,
            key.uid,
            fingerprint,
            backup_id,
            _iso_now(),
        ),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def update_action_attempt_state(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    state: str,
    error: str | None = None,
    backup_id: str | None = None,
    finished: bool = False,
) -> None:
    conn.execute(
        """
        UPDATE action_attempts SET
            state = ?,
            error = COALESCE(?, error),
            backup_id = COALESCE(?, backup_id),
            finished_at = CASE WHEN ? THEN ? ELSE finished_at END
        WHERE attempt_id = ?
        """,
        (state, error, backup_id, int(finished), _iso_now(), attempt_id),
    )


def open_action_attempts(
    conn: sqlite3.Connection, *, task: str | None = None
) -> list[sqlite3.Row]:
    """Rows in `pending`/`in_flight` state, for the reconcile pass
    (spec §4.0), driven by `idx_actions_open`."""
    if task is not None:
        return conn.execute(
            """
            SELECT action_attempts.* FROM action_attempts
            JOIN runs ON runs.run_id = action_attempts.run_id
            WHERE action_attempts.state IN ('pending', 'in_flight')
              AND runs.task = ?
            """,
            (task,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM action_attempts WHERE state IN ('pending', 'in_flight')"
    ).fetchall()


def fingerprints_with_completed_destructive_action(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    fingerprints: Sequence[str],
    exclude_run_id: str,
) -> set[str]:
    """Which of `fingerprints` already carry a *completed* destructive
    action (`trash` or `move_to:*` -- never `label:*` or `backup`) from
    some run other than `exclude_run_id` (sync-fix-brief Fix D, Finding
    3).

    The cache key `(account_id, fingerprint, rule_id, rule_text_hash,
    input_hash)` is stable across a move (`fingerprint` survives it by
    design, spec §11), so restoring a trashed message back to a source
    mailbox re-scans it under a new UID/mailbox and can hit a cached
    positive decision -- re-trashing it with no model call and no signal
    to the user. `runner.py` calls this once per run, batched over every
    candidate about to be offered a winning rule, rather than per
    candidate, and reports a hit as `restored` instead of executing it.
    """
    if not fingerprints:
        return set()
    placeholders = ",".join("?" for _ in fingerprints)
    rows = conn.execute(
        f"""
        SELECT DISTINCT fingerprint FROM action_attempts
        WHERE account_id = ? AND fingerprint IN ({placeholders})
          AND state = 'completed' AND run_id != ?
          AND (action = 'trash' OR action LIKE 'move_to:%')
        """,
        (account_id, *fingerprints, exclude_run_id),
    ).fetchall()
    return {str(row["fingerprint"]) for row in rows}


# --- Audit events (append-only, spec §11) ---------------------------------


def append_audit_event(
    conn: sqlite3.Connection,
    *,
    run_id: str | None,
    kind: str,
    subject: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO audit_events (run_id, at, kind, subject, data)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            run_id,
            _iso_now(),
            kind,
            subject,
            json.dumps(data) if data is not None else None,
        ),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)
