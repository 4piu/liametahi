#!/usr/bin/env python3
"""Run a local Dovecot for development and seed it from a captured corpus.

This is the second half of the "never touch the real account twice"
workflow. `capture_corpus.py` reads a real mailbox once, read-only, and
writes `.eml` files plus a manifest. This script stands up a local IMAP
server and `APPEND`s that corpus into it, so every subsequent `liametahi
run` -- including the destructive ones -- operates on a throwaway server
on loopback.

The result is that no configuration file on this machine needs to hold a
real credential: the dev config points at 127.0.0.1 with a throwaway
password (see `--print-config`).

Unlike the pytest fixture in `tests/integration/conftest.py`, the
container this starts is *persistent* -- it survives between commands so
you can iterate against it by hand.

    tools/dev_imap.py up
    tools/dev_imap.py seed --corpus tests/corpus/gmail
    tools/dev_imap.py status
    tools/dev_imap.py down

Seeding uses the production `ImapMailbox.append`, preserving each
message's original flags and `INTERNALDATE` -- the same code path
`restore` uses, so seeding exercises real code rather than a test-only
shortcut.
"""

import argparse
import json
import ssl
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from liametahi.imap_adapter import ImapMailbox, ImapTransportError

CONTAINER_NAME = "liametahi-dev-imap"
IMAGE = "antespi/docker-imap-devel:latest"
HOST = "127.0.0.1"
PORT = 3993

#: Throwaway credentials. These are not secrets: the server is on
#: loopback, holds only a copy of a corpus you captured yourself, and is
#: destroyed by `down`. This is the whole point of the local-IMAP
#: workflow -- the dev config can be read, edited, and shared freely.
#:
#: `MAILNAME` is the mail *domain*, not an address: the image feeds it to
#: Postfix's `mydomain`, which rejects an `@`. These values mirror the
#: ones `tests/integration/conftest.py` already verified against a live
#: container.
DOMAIN = "liametahi.test"
USERNAME = f"dev@{DOMAIN}"
PASSWORD = "devpassword"

_READY_TIMEOUT_SECONDS = 60


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check
    )


def _loopback_ssl_context() -> ssl.SSLContext:
    """The dev container uses a self-signed certificate. Verification is
    skipped *only* because the host is loopback and the data is a corpus
    copy; `capture_corpus.py` refuses this for any non-loopback host."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _connect() -> ImapMailbox:
    return ImapMailbox(
        host=HOST,
        port=PORT,
        username=USERNAME,
        password=PASSWORD,
        ssl_context=_loopback_ssl_context(),
    )


def cmd_up() -> int:
    existing = _docker("ps", "-aq", "-f", f"name=^{CONTAINER_NAME}$", check=False)
    if existing.stdout.strip():
        print(f"{CONTAINER_NAME} already exists; use 'down' first to recreate")
        return 0

    print(f"starting {IMAGE} as {CONTAINER_NAME} on {HOST}:{PORT} ...")
    _docker(
        "run",
        "-d",
        "--name",
        CONTAINER_NAME,
        "-p",
        f"{HOST}:{PORT}:993",
        "-e",
        f"MAILNAME={DOMAIN}",
        "-e",
        f"MAIL_ADDRESS={USERNAME}",
        "-e",
        f"MAIL_PASS={PASSWORD}",
        IMAGE,
    )

    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            mailbox = _connect()
        except ImapTransportError:
            time.sleep(1.0)
            continue
        mailbox.close()
        print(f"ready: imaps://{USERNAME}@{HOST}:{PORT}")
        return 0

    print(
        f"error: server did not become ready within {_READY_TIMEOUT_SECONDS}s; "
        f"check 'docker logs {CONTAINER_NAME}'",
        file=sys.stderr,
    )
    return 1


def cmd_down() -> int:
    result = _docker("rm", "-f", CONTAINER_NAME, check=False)
    if result.returncode != 0:
        print(f"{CONTAINER_NAME} was not running")
        return 0
    print(f"removed {CONTAINER_NAME}")
    return 0


def cmd_seed(corpus: Path, mailbox_name: str) -> int:
    manifest_path = corpus / "manifest.json"
    if not manifest_path.is_file():
        print(f"error: no manifest at {manifest_path}", file=sys.stderr)
        return 2

    manifest: dict[str, object] = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_messages = manifest.get("messages", [])
    messages: list[dict[str, object]] = (
        list(raw_messages) if isinstance(raw_messages, list) else []
    )
    if not messages:
        print(f"error: corpus at {corpus} contains no messages", file=sys.stderr)
        return 2

    try:
        server = _connect()
    except ImapTransportError as exc:
        print(f"connection failed: {exc} (is the container up?)", file=sys.stderr)
        return 1

    appended = 0
    try:
        for entry in messages:
            raw = (corpus / str(entry["relative_path"])).read_bytes()
            internaldate = datetime.fromisoformat(
                str(entry["internaldate"]).replace("Z", "+00:00")
            )
            raw_flags = entry.get("flags", [])
            flags = [
                str(f)
                for f in (raw_flags if isinstance(raw_flags, list) else [])
                if str(f) not in ("\\Recent", "\\Deleted")
            ]
            server.append(mailbox_name, raw, flags, internaldate)
            appended += 1
    except (ImapTransportError, OSError) as exc:
        print(f"seed failed after {appended} message(s): {exc}", file=sys.stderr)
        return 1
    finally:
        server.close()

    print(f"appended {appended} message(s) to {mailbox_name}")
    return 0


def cmd_status() -> int:
    result = _docker(
        "ps",
        "-a",
        "--filter",
        f"name=^{CONTAINER_NAME}$",
        "--format",
        "{{.Status}}",
        check=False,
    )
    status = result.stdout.strip()
    if not status:
        print("not created; run 'up'")
        return 1
    print(f"container: {status}")
    try:
        server = _connect()
    except ImapTransportError as exc:
        print(f"imap: unreachable ({exc})")
        return 1
    try:
        info = server.select("INBOX", readonly=True)
        print(
            f"imap: reachable — INBOX has {info.exists} message(s), "
            f"UIDVALIDITY {info.uidvalidity}"
        )
    finally:
        server.close()
    return 0


def cmd_print_config() -> int:
    print(
        f"""# Development config — contains no secrets.
# The IMAP server is a throwaway container on loopback (tools/dev_imap.py)
# and the model endpoint is local. Safe to read, edit, and share.
version: 1

settings:
  log_level: info

accounts:
  local:
    host: {HOST}
    port: {PORT}
    username: {USERNAME}
    password: {PASSWORD}
    trash_mailbox: Trash
    # The dev container uses a self-signed certificate. Config
    # load refuses this flag for any non-loopback host.
    tls_insecure_skip_verify: true

models:
  local:
    provider: openai_compatible
    base_url: http://127.0.0.1:8080/v1/chat/completions
    model: qwen2.5-7b

tasks:
  inbox-cleanup:
    account: local
    model: local
    source_mailboxes: [INBOX]
    rules:
      - id: old-digest
        priority: 100
        when:
          all:
            - older-than: 30d
            - list-id-contains: digest
        actions: [backup, trash]
"""
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dev_imap",
        description="Local Dovecot for development; seed it from a captured corpus.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("up", help="Start the container and wait until it accepts IMAP")
    sub.add_parser("down", help="Stop and remove the container")
    sub.add_parser("status", help="Show container and IMAP status")
    sub.add_parser("print-config", help="Print a secret-free dev config to stdout")
    seed = sub.add_parser("seed", help="APPEND a captured corpus into the server")
    seed.add_argument("--corpus", type=Path, required=True, help="Corpus directory")
    seed.add_argument("--mailbox", default="INBOX", help="Destination (default: INBOX)")

    args = parser.parse_args(argv)
    if args.command == "up":
        return cmd_up()
    if args.command == "down":
        return cmd_down()
    if args.command == "status":
        return cmd_status()
    if args.command == "print-config":
        return cmd_print_config()
    return cmd_seed(args.corpus, args.mailbox)


if __name__ == "__main__":
    raise SystemExit(main())
