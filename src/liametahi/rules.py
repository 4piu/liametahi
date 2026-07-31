"""Three-valued condition tree evaluator (spec §7.1, §7.2). Pure, no I/O.

`config.py` parses the value grammars (contracts §3 — durations, sizes,
globs) at load time and constructs the atomic-condition dataclasses
defined here; this module never parses a raw string. It is the primary
property-testing target: `all`/`any`/`not` must satisfy Kleene
three-valued logic for every combination of TRUE/FALSE/UNKNOWN children,
and `evaluate` must never raise on a well-typed tree.
"""

import fnmatch
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from liametahi.domain import Candidate

# IMAP system flags (compared case-insensitively per spec §7.1).
_SYSTEM_FLAGS = frozenset(
    {"\\seen", "\\answered", "\\flagged", "\\deleted", "\\draft", "\\recent"}
)


class Tri(Enum):
    """Kleene three-valued logic result."""

    FALSE = 0
    TRUE = 1
    UNKNOWN = 2


# --- Atomic conditions -----------------------------------------------------
#
# Each atom stores an already-parsed, already-validated value; config.py is
# responsible for turning config-file text into these.


@dataclass(frozen=True, slots=True)
class OlderThan:
    duration: timedelta


@dataclass(frozen=True, slots=True)
class NewerThan:
    duration: timedelta


@dataclass(frozen=True, slots=True)
class LiteralPattern:
    """A glob or plain-substring value, per the owning condition's default
    mode (spec §7.1). Already lowercased; matched against a lowercased
    candidate field, so this form is always case-insensitive."""

    text: str


@dataclass(frozen=True, slots=True)
class RegexPattern:
    """A `/pattern/flags` literal (spec §7.1). Compiled at config load, so
    a malformed pattern is a config error, never a runtime one. Matched
    against the candidate field's original casing — regex mode is
    case-*sensitive* by default, the opposite of `LiteralPattern`, unless
    the `i` flag was given."""

    regex: re.Pattern[str]


#: Which of the two forms a `sender-match`/`recipient-match`/`subject-match`/
#: `list-id-contains` value takes; `config.py` decides which at parse time.
MatchPattern = LiteralPattern | RegexPattern


def _pattern_matches(
    pattern: MatchPattern, text: str, *, mode: Literal["glob", "substring"]
) -> bool:
    """`text` is the candidate field in its original casing. `mode` is
    `"glob"` (fnmatch) or `"substring"` (`in`) for the `LiteralPattern`
    case; `RegexPattern` ignores `mode` and always uses `search`, matching
    anywhere in the string like the glob/substring forms do."""
    if isinstance(pattern, RegexPattern):
        return pattern.regex.search(text) is not None
    lowered = text.lower()
    if mode == "glob":
        return fnmatch.fnmatchcase(lowered, pattern.text)
    return pattern.text in lowered


@dataclass(frozen=True, slots=True)
class SenderMatch:
    pattern: MatchPattern


@dataclass(frozen=True, slots=True)
class RecipientMatch:
    pattern: MatchPattern


@dataclass(frozen=True, slots=True)
class SubjectMatch:
    pattern: MatchPattern


@dataclass(frozen=True, slots=True)
class ListIdContains:
    pattern: MatchPattern


@dataclass(frozen=True, slots=True)
class HasHeader:
    name: str  # already lowercased


@dataclass(frozen=True, slots=True)
class HasFlag:
    flag: str  # as configured; system flags matched case-insensitively


@dataclass(frozen=True, slots=True)
class InMailbox:
    mailbox: str  # as configured; case-sensitive except INBOX


@dataclass(frozen=True, slots=True)
class LargerThan:
    size_bytes: int


@dataclass(frozen=True, slots=True)
class LlmCondition:
    description: str


Atom = (
    OlderThan
    | NewerThan
    | SenderMatch
    | RecipientMatch
    | SubjectMatch
    | ListIdContains
    | HasHeader
    | HasFlag
    | InMailbox
    | LargerThan
    | LlmCondition
)


# --- Boolean composition -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllNode:
    children: tuple[ConditionTree, ...]


@dataclass(frozen=True, slots=True)
class AnyNode:
    children: tuple[ConditionTree, ...]


@dataclass(frozen=True, slots=True)
class NotNode:
    child: ConditionTree


ConditionTree = AllNode | AnyNode | NotNode | Atom


def _eval_atom(atom: Atom, candidate: Candidate, *, now: datetime) -> Tri:
    if isinstance(atom, OlderThan):
        age = now - candidate.internaldate
        return Tri.TRUE if age > atom.duration else Tri.FALSE
    if isinstance(atom, NewerThan):
        age = now - candidate.internaldate
        return Tri.TRUE if age < atom.duration else Tri.FALSE
    if isinstance(atom, SenderMatch):
        if candidate.from_address is None:
            return Tri.FALSE
        matched = _pattern_matches(atom.pattern, candidate.from_address, mode="glob")
        return Tri.TRUE if matched else Tri.FALSE
    if isinstance(atom, RecipientMatch):
        for recipient in candidate.recipients:
            if _pattern_matches(atom.pattern, recipient, mode="glob"):
                return Tri.TRUE
        return Tri.FALSE
    if isinstance(atom, SubjectMatch):
        if candidate.subject is None:
            return Tri.FALSE
        matched = _pattern_matches(atom.pattern, candidate.subject, mode="substring")
        return Tri.TRUE if matched else Tri.FALSE
    if isinstance(atom, ListIdContains):
        if candidate.list_id is None:
            return Tri.FALSE
        matched = _pattern_matches(atom.pattern, candidate.list_id, mode="substring")
        return Tri.TRUE if matched else Tri.FALSE
    if isinstance(atom, HasHeader):
        return Tri.TRUE if atom.name in candidate.headers_present else Tri.FALSE
    if isinstance(atom, HasFlag):
        return _eval_has_flag(atom, candidate)
    if isinstance(atom, InMailbox):
        return _eval_in_mailbox(atom, candidate)
    if isinstance(atom, LargerThan):
        return Tri.TRUE if candidate.rfc822_size > atom.size_bytes else Tri.FALSE
    if isinstance(atom, LlmCondition):
        return Tri.UNKNOWN
    _assert_never(atom)


def _eval_has_flag(atom: HasFlag, candidate: Candidate) -> Tri:
    if atom.flag.lower() in _SYSTEM_FLAGS:
        target = atom.flag.lower()
        for flag in candidate.flags:
            if flag.lower() == target:
                return Tri.TRUE
        return Tri.FALSE
    return Tri.TRUE if atom.flag in candidate.flags else Tri.FALSE


def _eval_in_mailbox(atom: InMailbox, candidate: Candidate) -> Tri:
    if atom.mailbox.upper() == "INBOX":
        return Tri.TRUE if candidate.key.mailbox.upper() == "INBOX" else Tri.FALSE
    return Tri.TRUE if candidate.key.mailbox == atom.mailbox else Tri.FALSE


def _assert_never(value: object) -> Tri:  # pragma: no cover - type-checker aid
    raise AssertionError(f"unreachable atom type: {type(value)!r}")


def evaluate(tree: ConditionTree, candidate: Candidate, *, now: datetime) -> Tri:
    """Evaluate a condition tree with Kleene three-valued logic.

    - `all` is FALSE if any child is FALSE, TRUE if every child is TRUE,
      else UNKNOWN.
    - `any` is TRUE if any child is TRUE, FALSE if every child is FALSE,
      else UNKNOWN.
    - `not` swaps TRUE/FALSE and leaves UNKNOWN unchanged.
    - An `llm` atom is always UNKNOWN; every other atom is deterministic.

    Never raises on a well-typed tree.
    """
    if isinstance(tree, AllNode):
        results = [evaluate(child, candidate, now=now) for child in tree.children]
        if any(r is Tri.FALSE for r in results):
            return Tri.FALSE
        if all(r is Tri.TRUE for r in results):
            return Tri.TRUE
        return Tri.UNKNOWN
    if isinstance(tree, AnyNode):
        results = [evaluate(child, candidate, now=now) for child in tree.children]
        if any(r is Tri.TRUE for r in results):
            return Tri.TRUE
        if all(r is Tri.FALSE for r in results):
            return Tri.FALSE
        return Tri.UNKNOWN
    if isinstance(tree, NotNode):
        inner = evaluate(tree.child, candidate, now=now)
        if inner is Tri.TRUE:
            return Tri.FALSE
        if inner is Tri.FALSE:
            return Tri.TRUE
        return Tri.UNKNOWN
    return _eval_atom(tree, candidate, now=now)


def llm_atom(tree: ConditionTree) -> LlmCondition | None:
    """Return the rule's single `llm` atom, or None.

    Config validation guarantees a rule has at most one `llm` atom and
    that it never appears under `not` (spec §7.3), so a single
    depth-first search is sufficient and unambiguous.
    """
    if isinstance(tree, AllNode):
        for child in tree.children:
            found = llm_atom(child)
            if found is not None:
                return found
        return None
    if isinstance(tree, AnyNode):
        for child in tree.children:
            found = llm_atom(child)
            if found is not None:
                return found
        return None
    if isinstance(tree, NotNode):
        return llm_atom(tree.child)
    if isinstance(tree, LlmCondition):
        return tree
    return None


def _sender_matches_protect_entry(address: str, entry: str) -> bool:
    """spec §6: `protect.senders` entries match case-insensitively as
    either an exact address or a domain suffix (`bank.example` matches
    `alerts@bank.example` and `x.bank.example`)."""
    address = address.lower()
    entry = entry.lower()
    if address == entry:
        return True
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1]
    return domain == entry or domain.endswith("." + entry)


def is_protected(
    candidate: Candidate,
    *,
    protected_flags: Collection[str],
    protected_senders: Sequence[str],
    protect_unread: bool,
) -> bool:
    """The deterministic exclusion applied before any rule is evaluated
    or any candidate is offered to a classifier (spec §4.2 point 1): a
    protected flag, a protected sender, or (when `protect_unread`) the
    absence of `\\Seen`. Pure and synchronous — nothing here can reach a
    model, which is exactly what guarantees a protected message never
    triggers an LLM call.
    """
    candidate_flags_lower = {f.lower() for f in candidate.flags}
    for flag in protected_flags:
        if flag.lower() in _SYSTEM_FLAGS:
            if flag.lower() in candidate_flags_lower:
                return True
        elif flag in candidate.flags:
            return True
    if protect_unread and "\\seen" not in candidate_flags_lower:
        return True
    if candidate.from_address:
        for sender in protected_senders:
            if _sender_matches_protect_entry(candidate.from_address, sender):
                return True
    return False
