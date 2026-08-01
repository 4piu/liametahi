"""`FakeMailbox`: an in-memory stand-in for `imap_adapter.MailboxAdapter`
(contracts §5.4, §6.3).

`MailboxStatus`, `RawMetadata`, `UnsupportedCapability`, and
`MessageVanished` are imported from `liametahi.imap_adapter`, which is
Unit 2's fixed cross-unit interface (contracts §5.4) — this file used to
hold verbatim local copies because that module did not exist yet when
Unit 1 landed; now that it does, per contracts §5.4's explicit
instruction ("Units 2 and 3 must move the real definitions ... and
change the fakes to import them, deleting the local copies") this file
imports from it. `MailboxAdapter` is a `Protocol`, so `FakeMailbox` keeps
satisfying it structurally without any change to the class below.

Every fetch method is `PEEK`-only by construction: nothing in this class
ever mutates `\\Seen`, matching the real adapter's contractual guarantee
(contracts §5.4).
"""

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from pathlib import Path

from liametahi.imap_adapter import (
    MailboxStatus,
    MessageVanished,
    RawMetadata,
    UnsupportedCapability,
)

__all__ = [
    "FakeMailbox",
    "MailboxStatus",
    "MessageVanished",
    "RawMetadata",
    "UnsupportedCapability",
]


@dataclass(slots=True)
class _StoredMessage:
    uid: int
    raw: bytes
    flags: set[str]
    internaldate: datetime
    mailbox: str
    vanished: bool = False


def _parsed(raw: bytes) -> Message:
    return message_from_bytes(raw)


def _header_values(msg: Message, name: str) -> tuple[str, ...]:
    """All non-empty values of `name` (case-insensitive), matching the
    real adapter's multi-valued, lowercased-key header mapping (contracts
    §5.4: `Mapping[str, tuple[str, ...]]`)."""
    return tuple(str(v).strip() for v in msg.get_all(name, []) if str(v).strip())


def _has_attachment(msg: Message) -> bool:
    """A simpler, stdlib-based equivalent of the real adapter's
    BODYSTRUCTURE-parsing heuristic (spec §7.1): this fake already has
    the full parsed message in hand, so it walks every part directly
    rather than replicating wire-level parsing. True if any part is
    marked `Content-Disposition: attachment` or carries a filename."""
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            return True
        if part.get_filename() is not None:
            return True
    return False


class FakeMailbox:
    """An in-memory mailbox implementing the `MailboxAdapter`-shaped
    protocol over a small in-memory message store.

    Failure switches (contracts §6.3):
        - `capabilities`: pass `capabilities=frozenset()` at construction
          to simulate a server with no `MOVE` support.
        - `accepts_custom_keywords`: per-mailbox, passed at construction.
        - `bump_uidvalidity(mailbox)`: simulates a UIDVALIDITY change on
          the next `select()`.
        - `vanish(mailbox, uid)`: marks a message as vanished; subsequent
          `fetch_metadata`/`fetch_raw` silently omit it (matching real
          IMAP FETCH-of-nonexistent-UID behaviour) and `move`/
          `add_keyword` raise `MessageVanished`.

    `mutations` (contracts §6.2, §6.3) records every call to a mutating
    method -- `move`, `add_keyword`, `append` -- regardless of whether it
    succeeded, raised, or was a no-op, so a caller that must prove it is
    read-only (e.g. `tools/capture_corpus.py`) can assert the log stayed
    empty rather than merely asserting the mailbox state looks unchanged.
    """

    def __init__(
        self,
        *,
        messages: Sequence[_StoredMessage] = (),
        uidvalidity: Mapping[str, int] | None = None,
        capabilities: frozenset[str] = frozenset({"MOVE"}),
        accepts_custom_keywords: bool = True,
    ) -> None:
        self._messages: dict[str, list[_StoredMessage]] = {}
        self._next_uid_counter: dict[str, int] = {}
        for msg in messages:
            self._messages.setdefault(msg.mailbox, []).append(msg)
            self._next_uid_counter[msg.mailbox] = max(
                self._next_uid_counter.get(msg.mailbox, 0), msg.uid
            )
        self._uidvalidity: dict[str, int] = dict(uidvalidity or {})
        self._pending_uidvalidity_bump: set[str] = set()
        self._capabilities = capabilities
        self._accepts_custom_keywords = accepts_custom_keywords
        self._selected_mailbox: str | None = None
        self._append_log: list[tuple[str, bytes, tuple[str, ...], datetime]] = []
        self._mutation_log: list[str] = []

    # --- test-only corpus loading -----------------------------------

    @classmethod
    def from_corpus(cls, manifest_path: Path, **kwargs: object) -> FakeMailbox:
        """Load a corpus in the format documented at contracts §6.2:
        `manifest.json` plus `messages/<sha256>.eml`, relative to
        `manifest_path`'s parent directory."""
        base = manifest_path.parent
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stored = []
        uidvalidity: dict[str, int] = {}
        for entry in manifest["messages"]:
            raw = (base / entry["relative_path"]).read_bytes()
            internaldate = datetime.fromisoformat(
                entry["internaldate"].replace("Z", "+00:00")
            )
            stored.append(
                _StoredMessage(
                    uid=entry["uid"],
                    raw=raw,
                    flags=set(entry["flags"]),
                    internaldate=internaldate,
                    mailbox=entry["mailbox"],
                )
            )
            uidvalidity[entry["mailbox"]] = entry["uidvalidity"]
        extra_kwargs: dict[str, object] = dict(kwargs)
        extra_kwargs.setdefault("uidvalidity", uidvalidity)
        return cls(messages=stored, **extra_kwargs)  # type: ignore[arg-type]

    # --- failure-switch controls -------------------------------------

    def bump_uidvalidity(self, mailbox: str) -> None:
        self._pending_uidvalidity_bump.add(mailbox)

    def vanish(self, mailbox: str, uid: int) -> None:
        for msg in self._messages.get(mailbox, []):
            if msg.uid == uid:
                msg.vanished = True
                return
        raise KeyError(f"no such message: {mailbox}/{uid}")

    @property
    def appended(self) -> tuple[tuple[str, bytes, tuple[str, ...], datetime], ...]:
        """Everything passed to `append()`, for test assertions."""
        return tuple(self._append_log)

    @property
    def mutations(self) -> tuple[str, ...]:
        """Every call made to `move`, `add_keyword`, or `append`, in call
        order, regardless of outcome (contracts §6.2, §6.3). A read-only
        code path must leave this empty."""
        return tuple(self._mutation_log)

    # --- MailboxAdapter-shaped protocol -------------------------------

    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    def select(self, mailbox: str, *, readonly: bool) -> MailboxStatus:
        if mailbox in self._pending_uidvalidity_bump:
            self._uidvalidity[mailbox] = self._uidvalidity.get(mailbox, 1000) + 1
            self._pending_uidvalidity_bump.discard(mailbox)
        self._selected_mailbox = mailbox
        live = [m for m in self._messages.get(mailbox, []) if not m.vanished]
        permanent_flags = frozenset(
            {"\\Seen", "\\Flagged", "\\Answered", "\\Deleted", "\\Draft"}
        )
        if self._accepts_custom_keywords:
            permanent_flags = permanent_flags | {"\\*"}
        return MailboxStatus(
            mailbox=mailbox,
            uidvalidity=self._uidvalidity.get(mailbox, 1000),
            exists=len(live),
            permanent_flags=permanent_flags,
            accepts_custom_keywords=self._accepts_custom_keywords,
        )

    def search_uids(self) -> tuple[int, ...]:
        mailbox = self._require_selected()
        return tuple(m.uid for m in self._messages.get(mailbox, []) if not m.vanished)

    def fetch_metadata(
        self, uids: Sequence[int], headers: Sequence[str]
    ) -> tuple[RawMetadata, ...]:
        mailbox = self._require_selected()
        # Lowercased keys, multi-valued: matches imap_adapter.RawMetadata
        # exactly (contracts §5.4) so normalize() works identically
        # whether it is handed a real or a fake RawMetadata.
        wanted = {h.lower() for h in headers}
        results = []
        for msg in self._messages.get(mailbox, []):
            if msg.uid not in uids or msg.vanished:
                continue
            parsed = _parsed(msg.raw)
            present: dict[str, tuple[str, ...]] = {}
            for name in wanted:
                values = _header_values(parsed, name)
                if values:
                    present[name] = values
            results.append(
                RawMetadata(
                    uid=msg.uid,
                    internaldate=msg.internaldate,
                    rfc822_size=len(msg.raw),
                    flags=frozenset(msg.flags),
                    headers=present,
                    has_attachment=_has_attachment(parsed),
                )
            )
        # \Seen is never touched by a metadata fetch (spec §4.1, §12).
        return tuple(results)

    def fetch_raw(self, uid: int) -> bytes:
        mailbox = self._require_selected()
        for msg in self._messages.get(mailbox, []):
            if msg.uid == uid and not msg.vanished:
                return msg.raw
        raise KeyError(f"no such message: {mailbox}/{uid}")

    def move(self, uid: int, destination: str) -> None:
        self._mutation_log.append(f"move uid={uid} destination={destination!r}")
        if "MOVE" not in self._capabilities:
            raise UnsupportedCapability("server does not advertise MOVE")
        mailbox = self._require_selected()
        bucket = self._messages.get(mailbox, [])
        for msg in bucket:
            if msg.uid == uid:
                if msg.vanished:
                    raise MessageVanished(f"{mailbox}/{uid} vanished before move")
                bucket.remove(msg)
                new_uid = self._next_uid(destination)
                msg.uid = new_uid
                msg.mailbox = destination
                self._messages.setdefault(destination, []).append(msg)
                return
        raise MessageVanished(f"{mailbox}/{uid} vanished before move")

    def add_keyword(self, uid: int, keyword: str) -> None:
        self._mutation_log.append(f"add_keyword uid={uid} keyword={keyword!r}")
        if not self._accepts_custom_keywords:
            raise UnsupportedCapability("mailbox PERMANENTFLAGS does not include \\*")
        mailbox = self._require_selected()
        for msg in self._messages.get(mailbox, []):
            if msg.uid == uid:
                if msg.vanished:
                    raise MessageVanished(f"{mailbox}/{uid} vanished before store")
                msg.flags.add(keyword)
                return
        raise MessageVanished(f"{mailbox}/{uid} vanished before store")

    def append(
        self,
        mailbox: str,
        raw: bytes,
        flags: Collection[str],
        internaldate: datetime,
    ) -> None:
        self._mutation_log.append(f"append mailbox={mailbox!r} bytes={len(raw)}")
        uid = self._next_uid(mailbox)
        self._messages.setdefault(mailbox, []).append(
            _StoredMessage(
                uid=uid,
                raw=raw,
                flags=set(flags),
                internaldate=internaldate.astimezone(UTC),
                mailbox=mailbox,
            )
        )
        self._append_log.append((mailbox, raw, tuple(flags), internaldate))

    # --- internals -----------------------------------------------------

    def _require_selected(self) -> str:
        if self._selected_mailbox is None:
            raise RuntimeError("no mailbox selected; call select() first")
        return self._selected_mailbox

    def _next_uid(self, mailbox: str) -> int:
        """The next UID for `mailbox`, tracked as a monotonically
        increasing per-mailbox counter rather than derived from the
        currently-present message list: a real IMAP server never reuses
        a UID within one `UIDVALIDITY` generation, even after the
        message that held it is moved out or expunged (RFC 3501 §2.3.1.1)
        -- deriving "next" from `max(currently present) + 1` would let a
        mailbox that emptied out and then received something new (e.g. a
        restored message moved/appended back) silently reuse a UID a
        still-referenced candidate row already claims, which is exactly
        the identity collision Fix D's restore scenario needs to be able
        to *not* rely on to reproduce (sync-fix-brief Finding 3)."""
        current = self._next_uid_counter.get(mailbox, 0)
        new_uid = current + 1
        self._next_uid_counter[mailbox] = new_uid
        return new_uid
