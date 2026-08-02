"""IMAP mailbox adapter and the scan phase (spec §4.1, §11; contracts §5.4).

`MailboxAdapter` is the read/search/fetch/move/append abstraction every
other unit programs against; `ImapMailbox` is its `imaplib`-backed
implementation. `scan()` is the scan-phase entry point Unit 5 sequences:
it selects each configured source mailbox read-only, excludes already
tracked messages, fetches metadata with `BODY.PEEK`, normalises each
message into a `domain.Candidate`, and persists via `state.py`.

Every fetch uses `BODY.PEEK` so `\\Seen` is never set by this module.
Every mutation (`move`, `add_keyword`) uses UIDs and the RFC 6851 `MOVE`
command; there is no COPY/STORE/EXPUNGE emulation anywhere here.
"""

import contextlib
import imaplib
import re
import sqlite3
import ssl
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes
from email.message import Message
from email.utils import getaddresses, parseaddr
from typing import Protocol

from liametahi import state
from liametahi.domain import Candidate, MessageKey, fingerprint
from liametahi.logging import get_logger
from liametahi.progress import NullProgress, Progress

logger = get_logger(__name__)

# --- Exceptions (contracts §5.4) -------------------------------------------


class UnsupportedCapability(Exception):
    """Raised when an action requires an IMAP capability the server does
    not advertise (spec §7.5): `MOVE` for `move`, `PERMANENTFLAGS \\*` for
    `add_keyword`. No COPY/STORE/EXPUNGE emulation is ever attempted --
    that fallback is non-atomic and can expunge unrelated messages."""


class MessageVanished(Exception):
    """Raised by a mutating call (`move`, `add_keyword`) against a UID
    that no longer matches any message on the server, e.g. removed by
    another process between the scan and execute phases (spec §4.3)."""


class ImapTransportError(Exception):
    """A connection, authentication, or protocol-level IMAP failure that
    is not one of the two typed exceptions above."""


# --- Data shapes (contracts §5.4, verbatim) ---------------------------------


@dataclass(frozen=True, slots=True)
class MailboxStatus:
    mailbox: str
    uidvalidity: int
    exists: int
    permanent_flags: frozenset[str]
    accepts_custom_keywords: bool  # PERMANENTFLAGS contains \*


@dataclass(frozen=True, slots=True)
class RawMetadata:
    """One message as it comes off the wire, before normalisation."""

    uid: int
    internaldate: datetime  # tz-aware UTC
    rfc822_size: int
    flags: frozenset[str]
    headers: Mapping[str, tuple[str, ...]]  # lowercased name -> all values
    has_attachment: bool  # derived from BODYSTRUCTURE (spec §7.1)


class MailboxAdapter(Protocol):
    def capabilities(self) -> frozenset[str]: ...
    def select(self, mailbox: str, *, readonly: bool) -> MailboxStatus: ...
    def search_uids(self) -> tuple[int, ...]: ...
    def fetch_metadata(
        self, uids: Sequence[int], headers: Sequence[str]
    ) -> tuple[RawMetadata, ...]: ...
    def fetch_raw(self, uid: int) -> bytes: ...
    def fetch_metadata_and_raw(self, uid: int) -> tuple[RawMetadata, bytes] | None: ...
    def move(self, uid: int, destination: str) -> None: ...
    def add_keyword(self, uid: int, keyword: str) -> None: ...
    def append(
        self,
        mailbox: str,
        raw: bytes,
        flags: Collection[str],
        internaldate: datetime,
    ) -> None: ...


# --- imaplib response parsing helpers ---------------------------------------

# One "line" of an untagged FETCH response, e.g.:
#   1 (UID 101 INTERNALDATE "01-Jun-2026 08:00:05 +0000" RFC822.SIZE 529
#      FLAGS (\Seen) BODY[HEADER.FIELDS (FROM SUBJECT)] {123}
_UID_RE = re.compile(rb"\bUID (\d+)")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_FLAGS_RE = re.compile(rb"\bFLAGS \(([^)]*)\)")
_INTERNALDATE_RE = re.compile(rb'\bINTERNALDATE "([^"]+)"')
_INTERNALDATE_FORMAT = "%d-%b-%Y %H:%M:%S %z"


def _parse_internaldate(raw: bytes) -> datetime:
    text = raw.decode("ascii")
    return datetime.strptime(text, _INTERNALDATE_FORMAT).astimezone(UTC)


# --- BODYSTRUCTURE parsing and the has-attachment heuristic (spec §7.1) ----
#
# `BODYSTRUCTURE`'s value appears inline in the FETCH response's non-literal
# `line` bytes (not as a separate literal), so this is a small,
# self-contained recursive-descent parser for RFC 3501 §7.4.2's
# parenthesized-list grammar: `(`, `)`, a double-quoted string (with `\"`
# and `\\` escapes), `NIL`, a bare number, or any other bare atom. It
# deliberately does not support the one grammar construct a `{n}` literal
# marker would require -- a `_BodystructureParseError` there (or on any
# other malformed input) is always caught by the sole caller,
# `_bodystructure_has_attachment`, and converted to `has_attachment=False`:
# a scan must never fail because one message's structure was unusual.


class _BodystructureParseError(Exception):
    """Internal signal only -- never escapes this module. Raised on an
    unbalanced parenthesization or an unsupported `{n}` literal marker."""


_ATTACHMENT_MARKER_TOKENS = frozenset({"ATTACHMENT", "NAME", "FILENAME"})
_BODYSTRUCTURE_MARKER = b"BODYSTRUCTURE "
_ATOM_STOP_BYTES = (b"(", b")", b" ", b"\t", b"\r", b"\n")


class _Cursor:
    """A mutable position into a `bytes` buffer, shared by every parsing
    function below so each can advance it in place."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def peek(self) -> bytes:
        """The byte at the current position, or `b""` at end of data."""
        return self.data[self.pos : self.pos + 1]


def _skip_ws(cursor: _Cursor) -> None:
    while cursor.pos < len(cursor.data) and cursor.peek().isspace():
        cursor.pos += 1


def _parse_quoted_string(cursor: _Cursor) -> str:
    """`cursor` is positioned at the opening `"`."""
    cursor.pos += 1
    data = cursor.data
    chars: list[str] = []
    while True:
        if cursor.pos >= len(data):
            raise _BodystructureParseError("unterminated quoted string")
        ch = cursor.peek()
        if ch == b"\\":
            cursor.pos += 1
            if cursor.pos >= len(data):
                raise _BodystructureParseError("unterminated escape")
            chars.append(cursor.peek().decode("ascii", errors="replace"))
            cursor.pos += 1
            continue
        if ch == b'"':
            cursor.pos += 1
            return "".join(chars)
        chars.append(ch.decode("ascii", errors="replace"))
        cursor.pos += 1


def _parse_atom_token(cursor: _Cursor) -> str:
    """A bare `NIL`, number, or other unquoted atom -- any non-empty run
    of bytes not starting a nested structure or ending the enclosing
    list."""
    start = cursor.pos
    data = cursor.data
    while (
        cursor.pos < len(data)
        and data[cursor.pos : cursor.pos + 1] not in _ATOM_STOP_BYTES
    ):
        cursor.pos += 1
    if cursor.pos == start:
        raise _BodystructureParseError(f"unexpected byte at position {start}")
    return data[start : cursor.pos].decode("ascii", errors="replace")


def _parse_scalar(cursor: _Cursor) -> str:
    ch = cursor.peek()
    if ch == b'"':
        return _parse_quoted_string(cursor)
    if ch == b"{":
        # The one grammar construct this simple parser deliberately does
        # not support (spec §7.1) -- an IMAP literal-length marker.
        raise _BodystructureParseError("literal marker '{' is not supported")
    return _parse_atom_token(cursor)


def _parse_list(cursor: _Cursor) -> list[object]:
    """`cursor` is positioned at the opening `(`. Recurses on a nested
    `(`, appends a parsed scalar otherwise, and returns once it consumes
    the matching `)`."""
    cursor.pos += 1
    items: list[object] = []
    while True:
        _skip_ws(cursor)
        if cursor.pos >= len(cursor.data):
            raise _BodystructureParseError("unbalanced parentheses")
        ch = cursor.peek()
        if ch == b")":
            cursor.pos += 1
            return items
        if ch == b"(":
            items.append(_parse_list(cursor))
            continue
        items.append(_parse_scalar(cursor))


def _flatten_bodystructure_tokens(value: object) -> list[str]:
    """Depth-first flatten of a parsed `BODYSTRUCTURE` list into every
    string/atom leaf token (spec §7.1); numbers and `NIL` are harmless to
    include since they never match the marker vocabulary below."""
    if isinstance(value, list):
        tokens: list[str] = []
        for item in value:
            tokens.extend(_flatten_bodystructure_tokens(item))
        return tokens
    if isinstance(value, str):
        return [value]
    return []  # pragma: no cover - parser only ever yields list[str|list]


def _find_bodystructure_value_start(line: bytes) -> int | None:
    """Locate where `BODYSTRUCTURE `'s value begins in `line`, but only
    when `BODYSTRUCTURE` names a top-level FETCH data item -- not when
    that literal token happens to appear as a keyword *value* nested
    inside another data item. This matters concretely: `label:<keyword>`
    (spec §7.3) has no reason to forbid the keyword `BODYSTRUCTURE`, so a
    message can legitimately carry an IMAP keyword flag with that exact
    name, and our own FETCH command always requests `FLAGS` before
    `BODYSTRUCTURE`. A naive `bytes.find` on the whole line would then
    match the flag *name* sitting inside `FLAGS (...)` -- which is not
    followed by `(...)` -- and this function's caller would see a
    non-`(` next byte and (correctly, given that wrong starting point)
    report no attachment, permanently, for a message that may have a real
    one. This scans the line tracking parenthesis depth (skipping the
    contents of double-quoted strings, so a literal `(`/`)` inside e.g. a
    quoted value is never miscounted) and only accepts a match at depth
    1 -- one level inside the response's own outer `(...)` wrapper,
    exactly where every real top-level data-item name sits, and where a
    value nested inside `FLAGS (...)` (depth 2) can never be mistaken for
    it."""
    depth = 0
    in_quotes = False
    i = 0
    n = len(line)
    marker_len = len(_BODYSTRUCTURE_MARKER)
    while i < n:
        ch = line[i : i + 1]
        if in_quotes:
            if ch == b"\\":
                i += 2
                continue
            if ch == b'"':
                in_quotes = False
            i += 1
            continue
        if ch == b'"':
            in_quotes = True
            i += 1
            continue
        if ch == b"(":
            depth += 1
            i += 1
            continue
        if ch == b")":
            depth -= 1
            i += 1
            continue
        if depth == 1 and line[i : i + marker_len] == _BODYSTRUCTURE_MARKER:
            preceded_ok = i == 0 or line[i - 1 : i] in (b" ", b"(")
            if preceded_ok:
                return i + marker_len
        i += 1
    return None


def _bodystructure_has_attachment(line: bytes) -> bool:
    """The flatten-and-scan attachment heuristic (spec §7.1): true if any
    token in the parsed `BODYSTRUCTURE` case-insensitively equals
    `ATTACHMENT`, `NAME`, or `FILENAME`. `False` if `BODYSTRUCTURE ` is
    absent from `line`, or parsing fails for any reason -- never raises."""
    start = _find_bodystructure_value_start(line)
    if start is None:
        return False
    cursor = _Cursor(line, start)
    _skip_ws(cursor)
    if cursor.pos >= len(cursor.data) or cursor.peek() != b"(":
        return False
    try:
        parsed = _parse_list(cursor)
    except _BodystructureParseError:
        return False
    tokens = _flatten_bodystructure_tokens(parsed)
    return any(token.upper() in _ATTACHMENT_MARKER_TOKENS for token in tokens)


def _parse_fetch_line(line: bytes, literal: bytes) -> RawMetadata | None:
    """Parse one `(line, literal)` element of an `imaplib` FETCH response
    into a `RawMetadata`. Returns `None` if the line does not look like a
    FETCH data item (defensive; should not happen for a well-formed
    response to our own FETCH command)."""
    uid_match = _UID_RE.search(line)
    size_match = _SIZE_RE.search(line)
    flags_match = _FLAGS_RE.search(line)
    date_match = _INTERNALDATE_RE.search(line)
    if uid_match is None or size_match is None or date_match is None:
        return None
    flags = (
        frozenset(f.decode("ascii") for f in flags_match.group(1).split())
        if flags_match
        else frozenset()
    )
    headers = _parse_header_literal(literal)
    has_attachment = _bodystructure_has_attachment(line)
    return RawMetadata(
        uid=int(uid_match.group(1)),
        internaldate=_parse_internaldate(date_match.group(1)),
        rfc822_size=int(size_match.group(1)),
        flags=flags,
        headers=headers,
        has_attachment=has_attachment,
    )


def _header_block(raw: bytes) -> bytes:
    """The header section of a whole RFC 822 message, so a full-message
    fetch can reuse `_parse_header_literal` without handing it a
    multi-megabyte body to walk. The blank line terminating the headers
    may use either line ending; a message with no blank line at all is
    all headers."""
    ends = [raw.find(sep) for sep in (b"\r\n\r\n", b"\n\n")]
    found = [end for end in ends if end != -1]
    return raw[: min(found)] if found else raw


def _parse_header_literal(literal: bytes) -> Mapping[str, tuple[str, ...]]:
    """Parse a `BODY[HEADER.FIELDS (...)]` literal into a lowercased,
    multi-valued header mapping. `email.message_from_bytes` handles
    folding and RFC 2047 decoding for us; only headers whose (stripped)
    value is non-empty are included, matching the `has-header` semantics
    of spec §7.1 ("present and non-empty")."""
    parsed: Message = message_from_bytes(literal)
    collected: dict[str, list[str]] = {}
    for name, value in parsed.items():
        decoded = str(value).strip()
        if not decoded:
            continue
        collected.setdefault(name.lower(), []).append(decoded)
    return {name: tuple(values) for name, values in collected.items()}


def _last_bytes(values: Sequence[object]) -> bytes | None:
    """Normalise one `imaplib.IMAP4.untagged_responses` value.

    Entries are `bytes`, or a `(bytes, bytes)` tuple when the server sent
    a literal. Only the most recent entry is meaningful, since imaplib
    accumulates untagged responses across commands.
    """
    if not values:
        return None
    last = values[-1]
    if isinstance(last, tuple):
        head = last[0]
        return head if isinstance(head, bytes) else None
    return last if isinstance(last, bytes) else None


def _permanent_flags_from_untagged(
    untagged: Mapping[str, Sequence[object]],
) -> frozenset[str]:
    raw = _last_bytes(untagged.get("PERMANENTFLAGS", ()))
    if raw is None:
        return frozenset()
    text = raw.decode("ascii", errors="replace")
    inner = text.strip().removeprefix("(").removesuffix(")")
    return frozenset(inner.split())


def _uidvalidity_from_untagged(untagged: Mapping[str, Sequence[object]]) -> int:
    raw = _last_bytes(untagged.get("UIDVALIDITY", ()))
    if raw is None:
        raise ImapTransportError(
            "server did not report UIDVALIDITY on SELECT/EXAMINE (RFC 3501 "
            "violation) -- refusing to proceed without it"
        )
    return int(raw)


def _format_flags(flags: Collection[str]) -> str:
    return "(" + " ".join(flags) + ")"


# --- Real adapter ------------------------------------------------------------

_MAX_UIDS_PER_FETCH = 200


class ImapMailbox:
    """`imaplib`-backed `MailboxAdapter`. Certificate-verifying TLS only:
    the caller supplies an `ssl.SSLContext` (default
    `ssl.create_default_context()`), never a verification-disabled one.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 993,
        username: str,
        password: str,
        timeout_seconds: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        context = (
            ssl_context if ssl_context is not None else ssl.create_default_context()
        )
        try:
            self._conn = imaplib.IMAP4_SSL(
                host, port, ssl_context=context, timeout=timeout_seconds
            )
            self._conn.login(username, password)
        except (TimeoutError, OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
            raise ImapTransportError(f"could not connect/authenticate: {exc}") from exc
        self._capabilities: frozenset[str] | None = None
        self._selected: str | None = None
        self._selected_permanent_flags: frozenset[str] = frozenset()

    def close(self) -> None:
        """Log out and close the underlying socket. Never raises."""
        with contextlib.suppress(OSError, imaplib.IMAP4.error):
            if self._selected is not None:
                self._conn.close()
        with contextlib.suppress(OSError, imaplib.IMAP4.error):
            self._conn.logout()

    def __enter__(self) -> ImapMailbox:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- MailboxAdapter protocol --------------------------------------

    def capabilities(self) -> frozenset[str]:
        """The post-login capability set. Dovecot (and many servers)
        advertise a reduced pre-authentication capability list, so this
        always issues an explicit `CAPABILITY` command rather than
        trusting `imaplib`'s cached pre-login value."""
        if self._capabilities is None:
            typ, data = self._conn.capability()
            if typ != "OK" or not data or data[0] is None:
                raise ImapTransportError("CAPABILITY command failed")
            self._capabilities = frozenset(data[0].decode("ascii").split())
        return self._capabilities

    def select(self, mailbox: str, *, readonly: bool) -> MailboxStatus:
        typ, data = self._conn.select(mailbox, readonly=readonly)
        if typ != "OK":
            first = data[0] if data else None
            detail = first.decode("ascii", errors="replace") if first else ""
            raise ImapTransportError(f"could not select mailbox {mailbox!r}: {detail}")
        self._selected = mailbox
        exists = int(data[0]) if data and data[0] is not None else 0
        untagged = self._conn.untagged_responses
        uidvalidity = _uidvalidity_from_untagged(untagged)
        permanent_flags = _permanent_flags_from_untagged(untagged)
        self._selected_permanent_flags = permanent_flags
        return MailboxStatus(
            mailbox=mailbox,
            uidvalidity=uidvalidity,
            exists=exists,
            permanent_flags=permanent_flags,
            accepts_custom_keywords="\\*" in permanent_flags,
        )

    def search_uids(self) -> tuple[int, ...]:
        self._require_selected()
        typ, data = self._conn.uid("SEARCH", "ALL")
        if typ != "OK":
            raise ImapTransportError("UID SEARCH failed")
        if not data or not data[0]:
            return ()
        return tuple(int(u) for u in data[0].split())

    def fetch_metadata(
        self, uids: Sequence[int], headers: Sequence[str]
    ) -> tuple[RawMetadata, ...]:
        """`headers=()` (used by the reconcile pass and by `scan()`'s Fix
        B flags-only refresh, sync-fix-brief Finding 1) omits the
        `BODY.PEEK[HEADER.FIELDS (...)]` data item entirely rather than
        sending it with an empty field list: RFC 3501's grammar for
        `header-fld-name` is `1#header-fld-name` (one or more), so
        `HEADER.FIELDS ()` is not well-formed IMAP and at least one real
        server (Dovecot) aborts the connection on it rather than
        returning an empty match. Omitting the item entirely means the
        server's response for each UID has no literal at all, which
        `imaplib` surfaces as a bare `bytes` line instead of the
        `(line, literal)` tuple a `BODY.PEEK[...]` reply produces --
        handled below by treating a bare line as one with an empty
        (no-headers) literal.
        """
        self._require_selected()
        if not uids:
            return ()
        header_names = sorted({h.upper() for h in headers})
        if header_names:
            header_list = " ".join(header_names)
            fetch_items = (
                f"(UID INTERNALDATE RFC822.SIZE FLAGS BODYSTRUCTURE "
                f"BODY.PEEK[HEADER.FIELDS ({header_list})])"
            )
        else:
            fetch_items = "(UID INTERNALDATE RFC822.SIZE FLAGS BODYSTRUCTURE)"
        results: list[RawMetadata] = []
        uid_list = list(uids)
        for start in range(0, len(uid_list), _MAX_UIDS_PER_FETCH):
            chunk = uid_list[start : start + _MAX_UIDS_PER_FETCH]
            uid_set = ",".join(str(u) for u in chunk)
            typ, data = self._conn.uid("FETCH", uid_set, fetch_items)
            if typ != "OK":
                raise ImapTransportError("UID FETCH failed")
            for item in data:
                if isinstance(item, tuple) and len(item) == 2:
                    line, literal = item
                    if line is None or literal is None:
                        continue
                elif isinstance(item, bytes):
                    line, literal = item, b""
                else:
                    continue
                parsed = _parse_fetch_line(line, literal)
                if parsed is not None:
                    results.append(parsed)
        return tuple(results)

    def fetch_raw(self, uid: int) -> bytes:
        self._require_selected()
        typ, data = self._conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise ImapTransportError("UID FETCH failed")
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                body = item[1]
                if isinstance(body, bytes):
                    return body
        raise KeyError(f"no such message: uid {uid}")

    def fetch_metadata_and_raw(self, uid: int) -> tuple[RawMetadata, bytes] | None:
        """Everything `fetch_metadata([uid], headers=...)` returns plus
        everything `fetch_raw(uid)` returns, in a single round trip.
        `None` means the UID no longer matches a message.

        The execute phase needs both for the same message back to back
        (re-verify, then back up), and against a remote server the
        second round trip costs more than the extra data items do.

        Headers come from parsing the raw bytes rather than from a
        separate `BODY.PEEK[HEADER.FIELDS (...)]` item: two literals in
        one FETCH response make `imaplib`'s reply shape substantially
        harder to parse, and the full message already contains every
        header, so the caller gets *all* of them rather than a
        requested subset. `BODY.PEEK[]` leaves `\\Seen` untouched, same
        as `fetch_raw`.
        """
        self._require_selected()
        fetch_items = "(UID INTERNALDATE RFC822.SIZE FLAGS BODYSTRUCTURE BODY.PEEK[])"
        typ, data = self._conn.uid("FETCH", str(uid), fetch_items)
        if typ != "OK":
            raise ImapTransportError("UID FETCH failed")
        for item in data:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            line, raw = item
            if line is None or raw is None:
                continue
            parsed = _parse_fetch_line(line, _header_block(raw))
            if parsed is not None:
                return parsed, raw
        return None

    def move(self, uid: int, destination: str) -> None:
        self._require_selected()
        if "MOVE" not in self.capabilities():
            raise UnsupportedCapability("server does not advertise MOVE (RFC 6851)")
        self._require_uid_exists(uid)
        typ, data = self._conn.uid("MOVE", str(uid), destination)
        if typ != "OK":
            detail = data[0].decode("ascii", errors="replace") if data else ""
            raise ImapTransportError(f"UID MOVE failed: {detail}")

    def add_keyword(self, uid: int, keyword: str) -> None:
        self._require_selected()
        if "\\*" not in self._selected_permanent_flags:
            raise UnsupportedCapability(
                "selected mailbox PERMANENTFLAGS does not include \\* "
                "(custom keywords unsupported)"
            )
        self._require_uid_exists(uid)
        typ, data = self._conn.uid("STORE", str(uid), "+FLAGS", f"({keyword})")
        if typ != "OK":
            detail = data[0].decode("ascii", errors="replace") if data else ""
            raise ImapTransportError(f"UID STORE failed: {detail}")

    def append(
        self,
        mailbox: str,
        raw: bytes,
        flags: Collection[str],
        internaldate: datetime,
    ) -> None:
        flags_str = _format_flags(flags) if flags else None
        date_str = imaplib.Time2Internaldate(internaldate.astimezone(UTC).timestamp())
        typ, data = self._conn.append(mailbox, flags_str, date_str, raw)
        if typ != "OK":
            detail = data[0].decode("ascii", errors="replace") if data else ""
            raise ImapTransportError(f"APPEND failed: {detail}")

    # --- internals ------------------------------------------------------

    def _require_selected(self) -> str:
        if self._selected is None:
            raise RuntimeError("no mailbox selected; call select() first")
        return self._selected

    def _require_uid_exists(self, uid: int) -> None:
        """Verify a UID still resolves to a message before a mutation.
        IMAP silently accepts MOVE/STORE against an empty match set (no
        error), so this explicit pre-check is what makes a vanished
        message observable as `MessageVanished` rather than a silent
        no-op (spec §4.3 step 4 expects the caller to detect this)."""
        typ, data = self._conn.uid("SEARCH", "UID", str(uid))
        if typ != "OK" or not data or not data[0]:
            raise MessageVanished(f"uid {uid} no longer exists in the selected mailbox")


# --- Normalisation: RawMetadata -> domain.Candidate (spec §4.1, §11) -------


def _list_id_identifier(raw_value: str) -> str | None:
    """Extract the RFC 2919 identifier from a List-Id header value,
    e.g. `Mozilla announcements <announce.mozilla.org>` ->
    `announce.mozilla.org` (spec §7.1). `None` if the value is not
    well-formed (no angle-bracketed identifier)."""
    match = re.search(r"<([^<>]+)>", raw_value)
    return match.group(1) if match else None


def _first(headers: Mapping[str, tuple[str, ...]], name: str) -> str | None:
    values = headers.get(name)
    return values[0] if values else None


def _recipient_addresses(
    headers: Mapping[str, tuple[str, ...]], names: Sequence[str]
) -> list[str]:
    """Addresses (not display names) from every value of every named
    header, in encounter order, deduplicated (spec §7.1 recipient-match:
    union of To, Cc, Delivered-To, X-Original-To)."""
    raw_values: list[str] = []
    for name in names:
        raw_values.extend(headers.get(name, ()))
    addresses: list[str] = []
    seen: set[str] = set()
    for _display, address in getaddresses(raw_values):
        if not address or address in seen:
            continue
        seen.add(address)
        addresses.append(address)
    return addresses


def normalize(
    raw: RawMetadata, *, account_id: int, mailbox: str, uidvalidity: int
) -> Candidate:
    """Build a `domain.Candidate` from one adapter-fetched `RawMetadata`.

    Address parsing, `List-Id` RFC 2919 extraction, and header-presence
    filtering happen here -- `imap_adapter`'s wire-level fetch does no
    more than unfold and RFC 2047-decode header values (contracts §5.4).
    """
    from_value = _first(raw.headers, "from")
    from_display, from_address = (None, None)
    if from_value:
        display, address = parseaddr(from_value)
        from_display = display or None
        from_address = address or None

    message_id = _first(raw.headers, "message-id")
    subject = _first(raw.headers, "subject")

    list_id_value = _first(raw.headers, "list-id")
    list_id = _list_id_identifier(list_id_value) if list_id_value else None

    cc_count = len(_recipient_addresses(raw.headers, ("cc",)))
    recipients = tuple(
        _recipient_addresses(raw.headers, ("to", "cc", "delivered-to", "x-original-to"))
    )
    has_list_unsubscribe = "list-unsubscribe" in raw.headers

    fp = fingerprint(
        message_id=message_id,
        internaldate=raw.internaldate,
        rfc822_size=raw.rfc822_size,
        from_address=from_address,
        subject=subject,
    )

    return Candidate(
        key=MessageKey(
            account_id=account_id, mailbox=mailbox, uidvalidity=uidvalidity, uid=raw.uid
        ),
        fingerprint=fp,
        message_id=message_id,
        internaldate=raw.internaldate,
        rfc822_size=raw.rfc822_size,
        flags=raw.flags,
        headers_present=frozenset(raw.headers.keys()),
        from_address=from_address,
        from_display=from_display,
        recipients=recipients,
        cc_count=cc_count,
        subject=subject,
        list_id=list_id,
        has_list_unsubscribe=has_list_unsubscribe,
        has_attachment=raw.has_attachment,
        auth_results=_first(raw.headers, "authentication-results"),
    )


# --- Scan phase (spec §4.1) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class MailboxScanResult:
    mailbox: str
    uidvalidity: int
    uidvalidity_changed: bool
    new_candidates: int
    reidentified_candidates: int
    flags_refreshed: int = 0


@dataclass(frozen=True, slots=True)
class ScanResult:
    """The outcome of a scan across a task's configured source mailboxes."""

    mailboxes: tuple[MailboxScanResult, ...]
    candidates_scanned: int
    stopped_at_cap: bool

    @property
    def any_uidvalidity_changed(self) -> bool:
        return any(m.uidvalidity_changed for m in self.mailboxes)


def scan(
    adapter: MailboxAdapter,
    conn: sqlite3.Connection,
    *,
    account_id: int,
    source_mailboxes: Sequence[str],
    fetch_headers: Sequence[str],
    max_new_mails: int | None,
    progress: Progress | None = None,
) -> ScanResult:
    """Run the scan phase (spec §4.1) across every configured source
    mailbox of one task, in order, stopping once `max_new_mails`
    candidate rows have been saved in total. `max_new_mails` is
    opt-in: `None` (the default) means no cap at all and every eligible
    candidate is scanned in one pass.

    For each mailbox: `EXAMINE` (read-only), compare the observed
    `UIDVALIDITY` against the last stored value for `(account, mailbox)`.

    - **Unchanged** (or first-ever scan): already-tracked UIDs are
      excluded locally before any fetch (IMAP `SEARCH` cannot express
      "not in my database" -- spec §4.1 point 3), so metadata is fetched
      only for genuinely new UIDs.
    - **Changed**: every UID's key is new by definition (the server
      renumbered), so metadata is fetched for the whole mailbox and each
      candidate is re-identified by `fingerprint()` against prior rows
      for that account/mailbox (spec §11) rather than treated as fresh.
      A candidate row's `uidvalidity` column no longer matching the
      current `mailbox_state.uidvalidity` for that mailbox is what marks
      it stale -- there is no separate stale flag in the schema
      (contracts §4 DDL is fixed).

    Within each mailbox, candidates are ordered by `INTERNALDATE`
    ascending before the cap is applied (spec §4.1 point 5), so a
    repeated run makes deterministic forward progress.

    Fix B (sync-fix-brief Finding 1): for a mailbox whose `UIDVALIDITY`
    is unchanged, already-known UIDs still present on the server also
    get their stored `flags` refreshed, in one additional batched
    `fetch_metadata` call (not per UID) covering every already-known
    live UID -- without this, a message's stored flags are frozen at
    whatever they were the first time it was seen, so `has-flag` and
    flag-based protection (spec §4.2 step 1) can silently go stale for
    as long as the message stays unactioned. `execute._reverify`'s
    immediate-pre-mutation re-check (Fix A) is the second, independent
    line of defense this complements, not a replacement for it: this
    keeps ordinary runs honest cheaply; that one guarantees the
    destructive path cannot act on stale protection even if a flag
    changes mid-run. Skipped entirely when `UIDVALIDITY` changed --
    every UID is fetched fresh in that branch already.
    """
    reporter = progress or NullProgress()
    mailbox_results: list[MailboxScanResult] = []
    total_saved = 0
    stopped_at_cap = False

    for mailbox in source_mailboxes:
        if max_new_mails is not None and total_saved >= max_new_mails:
            stopped_at_cap = True
            break

        status = adapter.select(mailbox, readonly=True)
        previous_uidvalidity = state.get_mailbox_uidvalidity(
            conn, account_id=account_id, mailbox=mailbox
        )
        uidvalidity_changed = (
            previous_uidvalidity is not None
            and previous_uidvalidity != status.uidvalidity
        )
        if uidvalidity_changed:
            logger.warning(
                "UIDVALIDITY changed for %s/%s: %s -> %s; re-identifying "
                "existing candidates by fingerprint",
                account_id,
                mailbox,
                previous_uidvalidity,
                status.uidvalidity,
            )
        state.record_mailbox_uidvalidity(
            conn, account_id=account_id, mailbox=mailbox, uidvalidity=status.uidvalidity
        )

        all_uids = adapter.search_uids()
        known_uids: tuple[int, ...] = ()
        if uidvalidity_changed:
            candidate_uids = all_uids
        else:
            new_uids: list[int] = []
            known: list[int] = []
            for uid in all_uids:
                if (
                    state.get_candidate(
                        conn,
                        key=MessageKey(account_id, mailbox, status.uidvalidity, uid),
                    )
                    is None
                ):
                    new_uids.append(uid)
                else:
                    known.append(uid)
            candidate_uids = tuple(new_uids)
            known_uids = tuple(known)

        # Apply the cap *before* fetching, not while saving.
        #
        # Spec §4.1 point 5 wants the oldest candidates first, and
        # `INTERNALDATE` is the natural key for that -- but it only
        # arrives *with* the metadata, so honouring it literally meant
        # fetching every new message in the mailbox and then keeping N of
        # them. `max_new_mails: 1` against a 300-message inbox still cost
        # 300 fetches, which defeats the point of a cap whose whole job
        # is to bound a run's work.
        #
        # UIDs are assigned strictly increasing in arrival order (RFC
        # 3501 §2.3.1.1), so the lowest N new UIDs is the same set as the
        # oldest N by `INTERNALDATE` for any mailbox that only ever had
        # mail delivered to it, and it is knowable without fetching
        # anything. The two diverge only for a message *appended* with an
        # older `INTERNALDATE` than its UID implies -- which this tool's
        # own `restore` does -- where the restored message is picked up
        # in arrival order rather than by its original date. Forward
        # progress is unaffected either way: UIDs are stable and
        # ascending, so each run starts where the last one stopped.
        remaining = None if max_new_mails is None else max_new_mails - total_saved
        if remaining is not None and len(candidate_uids) > remaining:
            candidate_uids = tuple(sorted(candidate_uids)[: max(remaining, 0)])
            stopped_at_cap = True

        new_count = 0
        reidentified_count = 0
        if candidate_uids:
            # Fetched in slices rather than one call so the counter can
            # advance per server round trip. `fetch_metadata` chunks
            # internally too (`_MAX_UIDS_PER_FETCH`), so slicing at the
            # same size here costs no extra round trips -- it just moves
            # the boundary somewhere this loop can observe.
            reporter.start("fetching new", total=len(candidate_uids))
            raw_parts: list[RawMetadata] = []
            for start in range(0, len(candidate_uids), _MAX_UIDS_PER_FETCH):
                slice_ = candidate_uids[start : start + _MAX_UIDS_PER_FETCH]
                raw_parts.extend(adapter.fetch_metadata(slice_, fetch_headers))
                reporter.advance(len(slice_))
            raw_batch = tuple(raw_parts)
            reporter.stop()
            # The fetch stays outside the transaction below: holding a
            # write transaction open across network I/O would block every
            # other writer for its duration (see `state.transaction`).
            with state.transaction(conn):
                for raw in sorted(raw_batch, key=lambda r: r.internaldate):
                    if max_new_mails is not None and total_saved >= max_new_mails:
                        stopped_at_cap = True
                        break
                    candidate = normalize(
                        raw,
                        account_id=account_id,
                        mailbox=mailbox,
                        uidvalidity=status.uidvalidity,
                    )
                    if uidvalidity_changed and _has_prior_identity(
                        conn,
                        account_id=account_id,
                        mailbox=mailbox,
                        uidvalidity=status.uidvalidity,
                        fp=candidate.fingerprint,
                    ):
                        reidentified_count += 1
                    else:
                        new_count += 1
                    state.upsert_candidate(conn, candidate)
                    total_saved += 1

        flags_refreshed = 0
        if known_uids:
            # Fix B: one batched flags-only fetch for every already-known,
            # still-present UID -- `headers=()` requests no header fields
            # at all (the same "no headers wanted" shape the reconcile
            # pass already uses via `fetch_metadata([...], headers=[])`),
            # so this costs a FLAGS/INTERNALDATE/SIZE fetch, not a second
            # full metadata fetch.
            reporter.start("refreshing flags", total=len(known_uids))
            flags_parts: list[RawMetadata] = []
            for start in range(0, len(known_uids), _MAX_UIDS_PER_FETCH):
                slice_ = known_uids[start : start + _MAX_UIDS_PER_FETCH]
                flags_parts.extend(adapter.fetch_metadata(slice_, ()))
                reporter.advance(len(slice_))
            reporter.stop()
            flags_by_uid = {meta.uid: meta.flags for meta in flags_parts}
            if flags_by_uid:
                flags_refreshed = state.update_candidate_flags(
                    conn,
                    account_id=account_id,
                    mailbox=mailbox,
                    uidvalidity=status.uidvalidity,
                    flags_by_uid=flags_by_uid,
                )

        mailbox_results.append(
            MailboxScanResult(
                mailbox=mailbox,
                uidvalidity=status.uidvalidity,
                uidvalidity_changed=uidvalidity_changed,
                new_candidates=new_count,
                reidentified_candidates=reidentified_count,
                flags_refreshed=flags_refreshed,
            )
        )

    return ScanResult(
        mailboxes=tuple(mailbox_results),
        candidates_scanned=total_saved,
        stopped_at_cap=stopped_at_cap,
    )


def _has_prior_identity(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    mailbox: str,
    uidvalidity: int,
    fp: str,
) -> bool:
    """True if a candidate row for this fingerprint already exists in
    this mailbox under a *different* (stale) UIDVALIDITY -- i.e. this is
    the same real message the mailbox already knew about before a
    UIDVALIDITY change, not a genuinely new message."""
    for _candidate_id, existing in state.find_candidates_by_fingerprint(
        conn, account_id=account_id, fingerprint=fp
    ):
        if existing.key.mailbox == mailbox and existing.key.uidvalidity != uidvalidity:
            return True
    return False
