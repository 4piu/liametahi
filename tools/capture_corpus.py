#!/usr/bin/env python3
"""Read-only IMAP corpus capture (implementation-contracts.md §6.2).

This is the highest-trust piece of code in the repository: it is what a
user points at a *real* mailbox (a personal Gmail account, say) to build
a realistic `.eml` corpus for integration-test seeding. Because of that,
it is held to a stricter bar than anything else here.

SAFETY PROPERTY -- read this before touching this file
========================================================

This module contains **no mutation code path whatsoever**. Every call
this module makes against a `MailboxAdapter` is one of exactly four
read-only methods:

    adapter.select(mailbox, readonly=True)
    adapter.search_uids()
    adapter.fetch_metadata(uids, headers)
    adapter.fetch_raw(uid)

`select` is always called with `readonly=True`, which the real adapter
(`liametahi.imap_adapter.ImapMailbox`) implements as IMAP `EXAMINE`, not
`SELECT`. `fetch_metadata` and `fetch_raw` are `BODY.PEEK`-only by the
real adapter's own contract (contracts §5.4) -- this module does not
control that, but it also never calls anything that could set `\\Seen`
even indirectly.

This module never calls `adapter.move`, `adapter.add_keyword`, or
`adapter.append` -- grep this file for those three names and you will
find zero matches outside this docstring and the safety-invariant test.
There is no flag, no code path, and no import that could reach a
mutating IMAP command (`STORE`, `MOVE`, `COPY`, `APPEND`, `EXPUNGE`)
from this module. `tests/test_capture_corpus.py` asserts this
mechanically: it drives `capture_corpus()` against `FakeMailbox` and
asserts `FakeMailbox.mutations == ()` afterwards (contracts §6.3's
mutation-recording hook).

Everything else this module does is local filesystem I/O: writing
`<out_dir>/messages/<sha256>.eml` and `<out_dir>/manifest.json`
(contracts §6.2's documented corpus layout), then re-reading every
written file and recomputing its sha256 to verify the write, exactly as
`backup.py`'s verified-write discipline does for real backups (spec
§11) -- this tool is not a backup mechanism, but "write, then verify by
re-reading" is the same discipline for the same reason.
"""

import argparse
import getpass
import hashlib
import json
import os
import shutil
import ssl
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from liametahi.config import BASE_FETCH_HEADERS
from liametahi.imap_adapter import ImapMailbox, ImapTransportError, MailboxAdapter
from liametahi.logging import configure_logging, get_logger, register_secret

logger = get_logger(__name__)

CORPUS_VERSION = 1

#: Conservative by design: this tool talks to a real mailbox, and every
#: captured message becomes a file this tool's caller must review before
#: it goes anywhere near a shared corpus (spec §12: message content is
#: personal data even in "just metadata" contexts, and here it is full
#: raw content).
DEFAULT_LIMIT = 25

#: `--insecure-skip-verify` exists only so this tool can also point at a
#: local integration test server with a self-signed certificate
#: (contracts §6.2 point 2). It is refused for any non-loopback host so
#: a typo or copy-pasted flag can never silently disable certificate
#: verification against a real account (spec §12: certificate-verifying
#: TLS is mandatory).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class CaptureError(Exception):
    """Corpus capture failed: a filesystem problem or a post-write
    verification mismatch. Never raised for a mailbox-side error --
    those surface as `ImapTransportError` from `imap_adapter.py`."""


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One row of `manifest.json["messages"]`, verbatim per contracts
    §6.2: `relative_path`, `mailbox`, `uid`, `uidvalidity`, `flags`,
    `internaldate`, `sha256`, `byte_count`."""

    relative_path: str
    mailbox: str
    uid: int
    uidvalidity: int
    flags: tuple[str, ...]
    internaldate: str
    sha256: str
    byte_count: int

    def to_json(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "mailbox": self.mailbox,
            "uid": self.uid,
            "uidvalidity": self.uidvalidity,
            "flags": list(self.flags),
            "internaldate": self.internaldate,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


# --- Capture (read-only) ----------------------------------------------


def capture_mailbox(
    adapter: MailboxAdapter,
    *,
    mailbox: str,
    limit: int,
    out_dir: Path,
) -> list[ManifestEntry]:
    """Capture up to `limit` messages from `mailbox` via `adapter`,
    writing each message's raw bytes to `out_dir/messages/<sha256>.eml`.

    Read-only by construction (see module docstring). `search_uids()` is
    capped to the most recent `limit` UIDs *before* any fetch, so a
    capture against a large real mailbox never issues one giant
    metadata FETCH across the whole mailbox -- only the messages that
    will actually be captured are ever fetched at all.
    """
    status = adapter.select(mailbox, readonly=True)
    all_uids = adapter.search_uids()
    if not all_uids:
        logger.info("mailbox %r is empty; nothing to capture", mailbox)
        return []

    # IMAP UIDs are monotonically non-decreasing within a UIDVALIDITY
    # epoch (RFC 3501 §2.3.1.1), so the *last* `limit` search results are
    # the most recently received messages -- a reasonable, documented
    # choice for corpus realism (message formats a classifier needs to
    # handle change over time); not specified by the spec itself.
    capped_uids = list(all_uids[-limit:])

    raw_metadata = adapter.fetch_metadata(capped_uids, BASE_FETCH_HEADERS)
    ordered = sorted(raw_metadata, key=lambda m: m.internaldate)

    messages_dir = out_dir / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)

    entries: list[ManifestEntry] = []
    for meta in ordered:
        raw = adapter.fetch_raw(meta.uid)
        digest = hashlib.sha256(raw).hexdigest()
        relative_path = f"messages/{digest}.eml"
        dest = out_dir / relative_path
        dest.write_bytes(raw)
        dest.chmod(0o600)
        entries.append(
            ManifestEntry(
                relative_path=relative_path,
                mailbox=mailbox,
                uid=meta.uid,
                uidvalidity=status.uidvalidity,
                flags=tuple(sorted(meta.flags)),
                internaldate=(
                    meta.internaldate.astimezone(UTC).isoformat().replace("+00:00", "Z")
                ),
                sha256=digest,
                byte_count=len(raw),
            )
        )
    logger.info("captured %d message(s) from %r", len(entries), mailbox)
    return entries


def write_manifest(out_dir: Path, entries: Sequence[ManifestEntry]) -> Path:
    """Write `manifest.json` in the format documented at contracts §6.2."""
    manifest = {
        "corpus_version": CORPUS_VERSION,
        "messages": [entry.to_json() for entry in entries],
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o600)
    return manifest_path


def verify_manifest(out_dir: Path, entries: Sequence[ManifestEntry]) -> None:
    """Re-read every file this run wrote and confirm its sha256 and byte
    count still match what the manifest records -- a write that silently
    truncated or corrupted would otherwise go unnoticed until a much
    later integration-test run tried to use the corpus."""
    for entry in entries:
        path = out_dir / entry.relative_path
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CaptureError(
                f"verification failed: could not re-read {entry.relative_path}: {exc}"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry.sha256:
            raise CaptureError(
                f"verification failed for {entry.relative_path}: expected "
                f"sha256 {entry.sha256}, got {digest} on re-read"
            )
        if len(data) != entry.byte_count:
            raise CaptureError(
                f"verification failed for {entry.relative_path}: expected "
                f"{entry.byte_count} bytes, got {len(data)} on re-read"
            )


def _prepare_out_dir(out_dir: Path, *, force: bool) -> None:
    """Refuse to overwrite an existing, non-empty corpus directory
    unless `force` is set. A corpus directory is either freshly
    captured or explicitly replaced wholesale, never silently merged --
    merging could leave stale manifest entries pointing at files a
    partial re-run removed or replaced."""
    if out_dir.exists() and any(out_dir.iterdir()):
        if not force:
            raise CaptureError(
                f"{out_dir} already exists and is not empty; pass --force "
                "to overwrite it, or choose a different --out directory"
            )
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)


def capture_corpus(
    adapter: MailboxAdapter,
    *,
    mailbox: str,
    limit: int,
    out_dir: Path,
    force: bool = False,
) -> tuple[Path, list[ManifestEntry]]:
    """Capture a corpus from one mailbox into `out_dir`
    (`<out_dir>/manifest.json` plus `<out_dir>/messages/<sha256>.eml`,
    contracts §6.2), then verify every written file by re-reading it.
    Returns the manifest path and the captured entries.
    """
    _prepare_out_dir(out_dir, force=force)
    entries = capture_mailbox(adapter, mailbox=mailbox, limit=limit, out_dir=out_dir)
    manifest_path = write_manifest(out_dir, entries)
    verify_manifest(out_dir, entries)
    return manifest_path, entries


# --- CLI -----------------------------------------------------------------


_PASSWORD_ENV = "LIAMETAHI_IMAP_PASSWORD"


def _read_password() -> str:
    """The credential, from `$LIAMETAHI_IMAP_PASSWORD` or an interactive
    prompt.

    Deliberately never a command-line argument (it would land in shell
    history and in `ps` output) and deliberately not read from a
    liametahi config file: this tool exists so the real account is
    touched exactly once, after which every config on this machine can
    point at a local test server and hold no secret at all.
    """
    from_env = os.environ.get(_PASSWORD_ENV)
    if from_env:
        return from_env
    return getpass.getpass(f"IMAP password (or set ${_PASSWORD_ENV}): ")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capture_corpus",
        description=(
            "Read-only IMAP corpus capture for integration-test realism "
            "(implementation-contracts.md §6.2). Connects with EXAMINE and "
            "BODY.PEEK[] only; contains no mutation code path whatsoever."
        ),
    )
    parser.add_argument(
        "--host",
        required=True,
        help="IMAP host, e.g. imap.gmail.com",
    )
    parser.add_argument(
        "--port", type=int, default=993, help="IMAP port (default: 993)"
    )
    parser.add_argument(
        "--username",
        required=True,
        help="IMAP username, e.g. you@gmail.com",
    )
    parser.add_argument(
        "--mailbox", default="INBOX", help="Mailbox to capture from (default: INBOX)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Maximum number of messages to capture (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output corpus directory, e.g. tests/corpus/<name>",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing, non-empty output directory",
    )
    parser.add_argument(
        "--insecure-skip-verify",
        action="store_true",
        help="Do not verify the server's TLS certificate. Refused for any "
        "host other than 127.0.0.1/::1/localhost -- only for a local "
        "integration test server, never for a real account",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.limit <= 0:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    if args.insecure_skip_verify and args.host not in _LOOPBACK_HOSTS:
        print(
            "error: --insecure-skip-verify is only permitted against a "
            f"loopback host ({', '.join(sorted(_LOOPBACK_HOSTS))}); "
            f"refusing to skip certificate verification for {args.host!r}",
            file=sys.stderr,
        )
        return 2

    password = _read_password()
    if not password:
        print("error: no password supplied", file=sys.stderr)
        return 2

    configure_logging(level="info")
    register_secret(password)

    ssl_context: ssl.SSLContext | None = None
    if args.insecure_skip_verify:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    try:
        adapter = ImapMailbox(
            host=args.host,
            port=args.port,
            username=args.username,
            password=password,
            ssl_context=ssl_context,
        )
    except ImapTransportError as exc:
        print(f"connection failed: {exc}", file=sys.stderr)
        return 1

    try:
        manifest_path, entries = capture_corpus(
            adapter,
            mailbox=args.mailbox,
            limit=args.limit,
            out_dir=args.out,
            force=args.force,
        )
    except (ImapTransportError, CaptureError) as exc:
        print(f"capture failed: {exc}", file=sys.stderr)
        return 1
    finally:
        adapter.close()

    total_bytes = sum(entry.byte_count for entry in entries)
    print(f"captured {len(entries)} message(s) from {args.mailbox!r} into {args.out}")
    print(f"  manifest: {manifest_path}")
    print(f"  total size: {total_bytes} bytes")
    print("  every captured file was re-read and its sha256 re-verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
