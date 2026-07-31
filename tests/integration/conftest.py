"""Dovecot integration test harness (contracts §6.1, §6.2).

Stands up a disposable Dovecot IMAP server in Docker for each test that
needs one, waits for it to accept authenticated IMAP connections, and
tears the container down afterward. Skips cleanly -- never fails the
default test run -- whenever Docker, the pinned image, or the container
itself is unavailable for any reason: no `docker` binary, an unreachable
daemon, an image pull failure, or a container that never becomes ready.

Image choice and verified capabilities
=======================================
`antespi/docker-imap-devel:latest` (a small Postfix+Dovecot 2.2.22 image
widely used for IMAP integration testing) was chosen over the official
`dovecot/dovecot:2.4.4` "confined mode" image because it needs no
server-side user/passdb configuration: a mailbox and its single user are
provisioned entirely from `MAIL_ADDRESS`/`MAIL_PASS` container
environment variables, which is what makes a from-scratch container
usable within a few seconds per test. GreenMail was not needed as a
fallback.

Verified by hand against this image (2026-07-31) before any test below
was written, using `imaplib` directly against a running container:

- The **pre-login** `CAPABILITY` response does *not* include `MOVE`; the
  **post-login** one does. This is exactly why `imap_adapter.py`'s
  `capabilities()` always re-issues `CAPABILITY` after authenticating
  rather than trusting the connection banner -- and it is also why
  `test_dovecot_capabilities.py` asserts against a connected adapter,
  not the raw banner.
- `UID MOVE` succeeds and relocates a message into the destination
  mailbox's own UID space, matching RFC 6851 (the numeric UID value
  itself does not have to differ -- destination mailboxes assign UIDs
  independently).
- A **read-write** (`readonly=False`) `select`'s `PERMANENTFLAGS`
  includes `\\*`, so arbitrary IMAP keywords (`label:<keyword>`, spec
  §7.5) are supported; `UID STORE +FLAGS (<keyword>)` persists the
  keyword. An `EXAMINE`d (read-only) mailbox correctly reports
  `PERMANENTFLAGS ()` instead -- nothing is permanently settable in a
  read-only session -- which is not evidence of missing support.
- `APPEND` with explicit flags and an `INTERNALDATE` round-trips both
  exactly on a subsequent `FETCH`.
- Deleting and recreating a mailbox changes its `UIDVALIDITY` -- this is
  how the UIDVALIDITY-change tests simulate a server-side rebuild.
- Plaintext authentication is refused on the non-TLS port
  (`[PRIVACYREQUIRED] Plaintext authentication disallowed on non-secure
  ... connections`), so this harness only ever uses the implicit-TLS
  port (993, published to a random host port per container). The
  server's certificate is self-signed, so `DovecotServer.connect()`
  builds an `ssl.SSLContext` with verification disabled -- something
  `tools/capture_corpus.py` refuses to do for any non-loopback host, but
  this harness only ever connects to `127.0.0.1`.
- `INBOX` and `Trash` mailboxes exist immediately after login; any other
  mailbox (e.g. `Archive`) must be `CREATE`d first (`DovecotServer.
  raw_imap()` below is for exactly this kind of test-setup-only IMAP
  command that production code never issues).
"""

import contextlib
import imaplib
import json
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from liametahi.imap_adapter import ImapMailbox

DOVECOT_IMAGE = "antespi/docker-imap-devel:latest"
TEST_USERNAME = "testuser@test.local"
TEST_PASSWORD = "testpass123"

_READY_TIMEOUT_SECONDS = 45.0
_DOCKER_TIMEOUT_SECONDS = 90
_DOCKER_RM_TIMEOUT_SECONDS = 20

#: The synthetic corpus committed at contracts §6.2 -- used to seed a
#: container the same way a real deployment's history would be
#: reconstructed via `restore`, exercising `ImapMailbox.append` for real.
SYNTHETIC_CORPUS_MANIFEST = (
    Path(__file__).parent.parent / "corpus" / "synthetic" / "manifest.json"
)


@dataclass(frozen=True, slots=True)
class DovecotServer:
    """A running, ready-to-use Dovecot container, bound to loopback."""

    host: str
    port: int
    username: str
    password: str

    def connect(self) -> ImapMailbox:
        """A real `ImapMailbox` (production code, contracts §5.4)
        connected to this container. Certificate verification is
        disabled because the container's certificate is self-signed --
        acceptable here because `host` is always `127.0.0.1`."""
        return ImapMailbox(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            ssl_context=_insecure_ssl_context(),
        )

    def raw_imap(self) -> imaplib.IMAP4_SSL:
        """A bare, authenticated `imaplib` connection for test-setup
        commands (`CREATE`, `DELETE`) that no production code path ever
        issues -- `MailboxAdapter` deliberately has no such method
        (spec §4: mailbox structure is user-managed, not something
        Liametahi creates or deletes)."""
        conn = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=_insecure_ssl_context()
        )
        conn.login(self.username, self.password)
        return conn


def _insecure_ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(server: DovecotServer, *, timeout: float) -> None:
    """Poll with a real authenticated connection attempt -- the
    container process starting is not the same as Dovecot being ready
    to accept TLS + `LOGIN` (SSL parameter generation alone took over a
    second in manual testing)."""
    deadline = time.monotonic() + timeout
    last_error: Exception = RuntimeError("no connection attempt was made")
    while time.monotonic() < deadline:
        try:
            adapter = server.connect()
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
            continue
        adapter.close()
        return
    raise RuntimeError(
        f"Dovecot container never became ready within {timeout}s: {last_error!r}"
    )


def _docker_rm(container_name: str) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            timeout=_DOCKER_RM_TIMEOUT_SECONDS,
            check=False,
        )


@pytest.fixture
def dovecot_server() -> Iterator[DovecotServer]:
    """One disposable Dovecot container per test, torn down afterward
    regardless of test outcome. Skips (does not fail) the test if
    Docker, the image, or the container are unavailable for any reason
    -- see the module docstring."""
    if not _docker_available():
        pytest.skip("Docker is not available on this machine")

    container_name = f"liametahi-it-{uuid.uuid4().hex[:12]}"
    port = _free_port()
    run_cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container_name,
        "-e",
        "MAILNAME=test.local",
        "-e",
        f"MAIL_ADDRESS={TEST_USERNAME}",
        "-e",
        f"MAIL_PASS={TEST_PASSWORD}",
        "-p",
        f"127.0.0.1:{port}:993",
        DOVECOT_IMAGE,
    ]
    try:
        result = subprocess.run(
            run_cmd, capture_output=True, timeout=_DOCKER_TIMEOUT_SECONDS, text=True
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"could not start a Dovecot container: {exc}")
        return
    if result.returncode != 0:
        pytest.skip(
            "could not start a Dovecot container (image pull or `docker run` "
            f"failed): {result.stderr.strip()}"
        )
        return

    server = DovecotServer(
        host="127.0.0.1", port=port, username=TEST_USERNAME, password=TEST_PASSWORD
    )
    try:
        _wait_until_ready(server, timeout=_READY_TIMEOUT_SECONDS)
    except RuntimeError as exc:
        _docker_rm(container_name)
        pytest.skip(str(exc))
        return

    try:
        yield server
    finally:
        _docker_rm(container_name)


# --- Corpus seeding (contracts §6.2 point 2) ------------------------------


@dataclass(frozen=True, slots=True)
class SeededMessage:
    """One corpus message as seeded, for test assertions."""

    mailbox: str
    uid_hint: int  # the UID recorded in the corpus manifest, pre-seeding
    flags: tuple[str, ...]
    internaldate: datetime
    raw: bytes


def seed_corpus(
    adapter: ImapMailbox, manifest_path: Path = SYNTHETIC_CORPUS_MANIFEST
) -> tuple[SeededMessage, ...]:
    """`APPEND` every message from a corpus manifest into its recorded
    mailbox, preserving flags and `INTERNALDATE` -- the same
    `ImapMailbox.append` method `restore` uses (spec §4.4), so seeding a
    test server exercises real production code, not a parallel
    test-only write path."""
    base = manifest_path.parent
    manifest: Mapping[str, object] = json.loads(
        manifest_path.read_text(encoding="utf-8")
    )
    raw_entries = manifest["messages"]
    assert isinstance(raw_entries, list)

    seeded: list[SeededMessage] = []
    for entry in raw_entries:
        assert isinstance(entry, dict)
        relative_path = str(entry["relative_path"])
        mailbox = str(entry["mailbox"])
        flags = tuple(str(f) for f in entry["flags"])
        internaldate = datetime.fromisoformat(
            str(entry["internaldate"]).replace("Z", "+00:00")
        ).astimezone(UTC)
        raw = (base / relative_path).read_bytes()
        adapter.append(mailbox, raw, flags, internaldate)
        seeded.append(
            SeededMessage(
                mailbox=mailbox,
                uid_hint=int(entry["uid"]),
                flags=flags,
                internaldate=internaldate,
                raw=raw,
            )
        )
    return tuple(seeded)
