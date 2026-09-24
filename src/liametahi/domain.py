"""Message, key, and candidate domain models.

Frozen dataclasses for internal domain values. No I/O in this module.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

#: A candidate's body shape, derived from `BODYSTRUCTURE` at scan time
#: (no body fetch needed): whether a `text/plain` and/or `text/html` part
#: is present anywhere in the message structure.
BodyShape = Literal["both", "html_only", "plain_only", "neither"]


@dataclass(frozen=True, slots=True)
class MessageKey:
    """Server identity of a message: account + mailbox + UIDVALIDITY + UID.

    Consumed by a mutation: a moved message gets a new UID in its
    destination, so a pre-action key is historical once recorded.
    """

    account_id: int
    mailbox: str
    uidvalidity: int
    uid: int

    def render(self) -> str:
        """Render as ``account/mailbox/uidvalidity/uid``."""
        return f"{self.account_id}/{self.mailbox}/{self.uidvalidity}/{self.uid}"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A message found by a mailbox scan and eligible for evaluation."""

    key: MessageKey
    fingerprint: str
    message_id: str | None
    internaldate: datetime  # tz-aware UTC
    rfc822_size: int
    flags: frozenset[str]
    headers_present: frozenset[str]  # lowercased
    from_address: str | None
    from_display: str | None
    recipients: tuple[str, ...]
    cc_count: int
    subject: str | None
    list_id: str | None
    has_list_unsubscribe: bool
    has_attachment: bool
    auth_results: str | None  # topmost Authentication-Results value only
    reply_to: str | None
    sender: str | None  # `Sender` header, RFC 5322 -- distinct from `From`
    precedence: str | None  # raw `Precedence` header value (bulk/list/junk/...)
    has_feedback_id: bool  # `Feedback-ID` presence (opaque ESP token)
    is_auto_submitted: bool  # `Auto-Submitted` presence (RFC 3834)
    has_auto_response_suppress: bool  # `X-Auto-Response-Suppress` presence
    is_reply: bool  # `In-Reply-To` or `References` presence
    body_shape: BodyShape


def fingerprint(
    *,
    message_id: str | None,
    internaldate: datetime,
    rfc822_size: int,
    from_address: str | None,
    subject: str | None,
) -> str:
    """Compute the server-independent message fingerprint.

    ``sha256(message_id|internaldate|size)`` when a ``Message-ID`` is
    present, otherwise ``sha256(from|subject|internaldate|size)``. This is
    the single function that computes a fingerprint anywhere in the
    codebase; every caller must go through it so the value stays consistent
    across a UIDVALIDITY change.
    """
    internaldate_iso = internaldate.astimezone(UTC).isoformat()
    if message_id:
        raw = f"{message_id}|{internaldate_iso}|{rfc822_size}"
    else:
        raw = f"{from_address or ''}|{subject or ''}|{internaldate_iso}|{rfc822_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
