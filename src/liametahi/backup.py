"""Verified `.eml` backup and restore (spec section 11, section 4.4;
contracts section 4 backup manifest DDL).

`MailboxAdapter`, `MailboxStatus`, `RawMetadata`, `UnsupportedCapability`,
and `MessageVanished` were originally reproduced locally in this module,
exactly as `tests/fakes/fake_mailbox.py` used to: `imap_adapter.py`
(Unit 2) was being built concurrently and this unit could not import or
create it (work-unit boundary, contracts section 7). Now that
`imap_adapter.py` has landed, this module imports the real definitions
per contracts section 5.4's instruction ("Units 2 and 3 must move the
real definitions into `imap_adapter.py` ... and change the fakes to
import them, deleting the local copies") -- the same cleanup already
applied to `tests/fakes/fake_mailbox.py`. `MailboxAdapter` is a
`Protocol`, so any object shaped like a real mailbox adapter --
`FakeMailbox` included -- still satisfies it without change.

`is_vanished_error`/`is_unsupported_error` below still match by
exception *name* rather than `isinstance`. That was originally a
necessary bridge (a `MessageVanished` raised by a fake and one raised by
the not-yet-existing real adapter were different Python objects); now
that both sides import the same classes it is merely a harmless
belt-and-braces choice, kept as-is to minimise this cleanup's diff.
"""

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from liametahi import state
from liametahi.domain import MessageKey
from liametahi.imap_adapter import (
    MailboxAdapter,
    MailboxStatus,
    MessageVanished,
    RawMetadata,
    UnsupportedCapability,
)

__all__ = [
    "BackupError",
    "MailboxAdapter",
    "MailboxStatus",
    "MessageVanished",
    "RawMetadata",
    "UnsupportedCapability",
    "is_unsupported_error",
    "is_vanished_error",
]


def is_vanished_error(exc: Exception) -> bool:
    """True for a `MessageVanished` (by name, see module docstring) or
    any `LookupError` (e.g. the `KeyError` `FakeMailbox.fetch_raw` and
    `fetch_metadata` raise for an absent UID) -- both mean "the message
    is not there for us to act on"."""
    return isinstance(exc, LookupError) or type(exc).__name__ == "MessageVanished"


def is_unsupported_error(exc: Exception) -> bool:
    return type(exc).__name__ == "UnsupportedCapability"


# --- Verified backup write (spec section 11) ------------------------------


class BackupError(Exception):
    """Backup could not be written and verified. Per spec section 11:
    'If backup verification fails, record the failure and do not modify
    the mailbox' -- callers must not proceed to a mutation when this is
    raised."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_id: str
    sha256: str
    byte_count: int
    relative_path: str


def backup_relative_path(sha256: str) -> Path:
    """Content-addressed path, fanned out by the first four hex
    characters so the backup directory never holds a huge flat listing:
    `<aa>/<bb>/<sha256>.eml`."""
    return Path(sha256[0:2]) / sha256[2:4] / f"{sha256}.eml"


def ensure_backup_dir(backup_dir: Path) -> Path:
    """Create the backup directory (and its `.tmp` staging
    subdirectory) with mode 0700 (spec section 12)."""
    backup_dir = backup_dir.expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    tmp_dir = backup_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.chmod(0o700)
    return backup_dir


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def write_verified_backup(
    conn: sqlite3.Connection,
    *,
    mailbox: MailboxAdapter,
    backup_dir: Path,
    key: MessageKey,
    fingerprint: str,
    message_id: str | None,
    original_flags: Collection[str],
    internaldate: datetime,
    run_id: str,
    raw: bytes | None = None,
) -> BackupResult:
    """spec section 11 / contracts section 10 (concurrency notes):
    fetch raw RFC 822 bytes with `BODY.PEEK[]` (the adapter's job --
    this function only calls `fetch_raw`, never touching `\\Seen`),
    write to a unique temporary file in the backup filesystem, fsync,
    checksum the bytes actually on disk, atomically rename to the
    SHA-256 content-addressed final path, then commit the manifest row.

    Idempotent under a retry with the same key: if a manifest row for
    this exact `(account_id, mailbox, uidvalidity, uid, sha256)`
    already exists (contracts section 4's uniqueness constraint), that
    existing row's `backup_id` is returned rather than raising a
    `sqlite3.IntegrityError` or writing a duplicate.

    Raises `BackupError` -- without ever calling a mutating mailbox
    method -- if the fetch, write, or verification fails. The caller
    must treat `BackupError` as "the mailbox was not touched, do not
    proceed to the mutation."

    `raw`, when given, is the message's bytes already in hand from an
    earlier fetch of this same key -- the execute phase pulls metadata
    and body in one round trip -- and skips the `fetch_raw` below. It
    changes nothing downstream: the bytes still go through the same
    write, fsync, and read-back-and-checksum verification, so a caller
    passing corrupt bytes is caught exactly as a corrupt fetch would be.
    """
    if raw is None:
        try:
            raw = mailbox.fetch_raw(key.uid)
        except Exception as exc:
            raise BackupError(
                f"could not fetch raw message for backup ({key.render()}): {exc}"
            ) from exc

    try:
        backup_dir = ensure_backup_dir(backup_dir)
        tmp_dir = backup_dir / ".tmp"
        fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, suffix=".eml.tmp")
    except OSError as exc:
        raise BackupError(
            f"could not prepare backup directory for {key.render()}: {exc}"
        ) from exc
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.chmod(0o600)
        # Checksum the bytes actually on disk, not the in-memory
        # buffer, so a write-time corruption is caught rather than
        # silently backed up (spec section 11: "fsync, checksum" as a
        # distinct step after the write).
        sha256 = _sha256_of_file(tmp_path)
        byte_count = tmp_path.stat().st_size
        if byte_count != len(raw):
            raise BackupError(
                f"backup write size mismatch for {key.render()}: "
                f"wrote {len(raw)} bytes, found {byte_count} on disk"
            )

        relative_path = backup_relative_path(sha256)
        final_path = backup_dir / relative_path
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.parent.chmod(0o700)

        if final_path.exists():
            # Content-addressed dedup (spec section 11): identical
            # bytes already backed up (possibly for a different
            # message). Verify the existing file still matches before
            # trusting it.
            if _sha256_of_file(final_path) != sha256:
                raise BackupError(
                    f"backup dedup path {final_path} exists with unexpected "
                    "content; refusing to overwrite"
                )
        else:
            os.replace(tmp_path, final_path)
            final_path.chmod(0o600)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError(f"backup write failed for {key.render()}: {exc}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()

    existing = _find_existing_backup(
        conn,
        account_id=key.account_id,
        mailbox=key.mailbox,
        uidvalidity=key.uidvalidity,
        uid=key.uid,
        sha256=sha256,
    )
    if existing is not None:
        return BackupResult(
            backup_id=str(existing["backup_id"]),
            sha256=sha256,
            byte_count=byte_count,
            relative_path=str(relative_path),
        )

    backup_id = state.new_backup_id()
    try:
        state.insert_backup(
            conn,
            backup_id=backup_id,
            account_id=key.account_id,
            mailbox=key.mailbox,
            uidvalidity=key.uidvalidity,
            uid=key.uid,
            fingerprint=fingerprint,
            message_id=message_id,
            sha256=sha256,
            byte_count=byte_count,
            relative_path=str(relative_path),
            original_mailbox=key.mailbox,
            original_flags=sorted(original_flags),
            internaldate=_iso(internaldate),
            run_id=run_id,
        )
    except sqlite3.IntegrityError:
        # Lost a race against a concurrent insert of the same natural
        # key (contracts section 10): re-read rather than fail.
        existing = _find_existing_backup(
            conn,
            account_id=key.account_id,
            mailbox=key.mailbox,
            uidvalidity=key.uidvalidity,
            uid=key.uid,
            sha256=sha256,
        )
        if existing is None:
            raise
        backup_id = str(existing["backup_id"])

    return BackupResult(
        backup_id=backup_id,
        sha256=sha256,
        byte_count=byte_count,
        relative_path=str(relative_path),
    )


def _find_existing_backup(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    mailbox: str,
    uidvalidity: int,
    uid: int,
    sha256: str,
) -> sqlite3.Row | None:
    """A natural-key lookup `state.py` does not expose (it only offers
    `get_backup(backup_id)`; its insert-only surface for backups was
    not extended with this query, contracts section 5). This is the one
    read this module cannot route through `state.py`'s typed surface
    without a change to a file this unit does not own (Unit 1's
    file-ownership boundary, contracts section 7); it is read-only,
    scoped to exactly the manifest's own uniqueness constraint
    (contracts section 4), and called out in the final report as a gap
    Unit 1 should close by adding this query to `state.py` itself.
    """
    return conn.execute(  # type: ignore[no-any-return]
        """
        SELECT * FROM backups
        WHERE account_id = ? AND mailbox = ? AND uidvalidity = ? AND uid = ?
          AND sha256 = ?
        """,
        (account_id, mailbox, uidvalidity, uid, sha256),
    ).fetchone()


def find_backups_by_key(
    conn: sqlite3.Connection, *, key: MessageKey
) -> list[sqlite3.Row]:
    """All backup manifest rows recorded for a message key, newest
    first. Used by reconcile (`execute.py`); not exposed by `state.py`
    today (see `_find_existing_backup`'s docstring)."""
    return conn.execute(
        """
        SELECT * FROM backups
        WHERE account_id = ? AND mailbox = ? AND uidvalidity = ? AND uid = ?
        ORDER BY backed_up_at DESC
        """,
        (key.account_id, key.mailbox, key.uidvalidity, key.uid),
    ).fetchall()


# --- Restore (spec section 4.4) -------------------------------------------


class RestoreError(Exception):
    """The backup could not be verified or restored."""


@dataclass(frozen=True, slots=True)
class RestoreResult:
    backup_id: str
    mailbox: str
    byte_count: int
    sha256: str
    dry_run: bool
    appended: bool


def restore_backup(
    conn: sqlite3.Connection,
    *,
    mailbox: MailboxAdapter,
    backup_dir: Path,
    backup_id: str,
    destination_mailbox: str,
    dry_run: bool,
) -> RestoreResult:
    """spec section 4.4: verify the backup's checksum against its file,
    then `APPEND` it to `destination_mailbox`, preserving the original
    `INTERNALDATE` and flags minus `\\Deleted` and `\\Recent`. Records
    the new server identity on the manifest row. `--dry-run` verifies
    the checksum and reports what would be appended without calling
    `append`.

    Restore is best-effort (spec section 4.4): it does not detect that
    the message already exists in the destination, so restoring an
    already-present message produces a duplicate.
    """
    row = state.get_backup(conn, backup_id)
    if row is None:
        raise RestoreError(f"no backup found with id {backup_id!r}")

    file_path = backup_dir.expanduser() / str(row["relative_path"])
    if not file_path.is_file():
        raise RestoreError(
            f"backup file missing for {backup_id}: expected at {file_path}"
        )
    actual_sha256 = _sha256_of_file(file_path)
    expected_sha256 = str(row["sha256"])
    if actual_sha256 != expected_sha256:
        raise RestoreError(
            f"backup {backup_id} failed checksum verification: "
            f"expected {expected_sha256}, found {actual_sha256}"
        )
    raw = file_path.read_bytes()
    byte_count = len(raw)

    original_flags = set(json.loads(str(row["original_flags"])))
    restore_flags = original_flags - {"\\Deleted", "\\Recent"}
    internaldate = _from_iso(str(row["internaldate"]))

    if dry_run:
        return RestoreResult(
            backup_id=backup_id,
            mailbox=destination_mailbox,
            byte_count=byte_count,
            sha256=expected_sha256,
            dry_run=True,
            appended=False,
        )

    mailbox.append(destination_mailbox, raw, restore_flags, internaldate)
    state.record_restore(conn, backup_id=backup_id, restored_to=destination_mailbox)
    return RestoreResult(
        backup_id=backup_id,
        mailbox=destination_mailbox,
        byte_count=byte_count,
        sha256=expected_sha256,
        dry_run=False,
        appended=True,
    )
