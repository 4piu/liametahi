"""Three-valued condition tree evaluator. Pure, no I/O.

`config.py` parses the value grammars (durations, sizes,
globs) at load time and constructs the atomic-condition dataclasses
defined here; this module never parses a raw string. It is the primary
property-testing target: `all`/`any`/`none`/`not` must satisfy Kleene
three-valued logic for every combination of TRUE/FALSE/UNKNOWN children,
and `evaluate` must never raise on a well-typed tree.

The old single-`llm`-atom mechanism (`LlmCondition`/`llm_atom()`) is
retired: a rule may now reference any number of named `processor:`
atoms, each `UNKNOWN` until `evaluate()` is given a resolved
`ProcessorAnswer` for it via `processor_values`. See `processor_names()`
for how a caller discovers which processors a still-`UNKNOWN` rule needs.
"""

import fnmatch
import operator
import re
import types
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from liametahi.domain import Candidate

# IMAP system flags (compared case-insensitively).
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
    mode. Already lowercased; matched against a lowercased
    candidate field, so this form is always case-insensitive."""

    text: str


@dataclass(frozen=True, slots=True)
class RegexPattern:
    """A `/pattern/flags` literal. Compiled at config load, so
    a malformed pattern is a config error, never a runtime one. Matched
    against the candidate field's original casing — regex mode is
    case-*sensitive* by default, the opposite of `LiteralPattern`, unless
    the `i` flag was given."""

    regex: re.Pattern[str]


#: Which of the two forms a `sender-match`/`recipient-match`/`subject-contains`/
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
class SubjectContains:
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


#: `recipient-count`'s comparison operators.
#: Also reused, unchanged, by `ProcessorCondition`.
ComparisonOp = Literal["==", "!=", ">=", "<=", ">", "<"]

_COMPARATORS: dict[ComparisonOp, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


@dataclass(frozen=True, slots=True)
class RecipientCount:
    op: ComparisonOp
    value: int


@dataclass(frozen=True, slots=True)
class HasAttachment:
    """A presence check with a fixed `true` value, validated entirely at
    config-parse time; nothing left to carry at eval time."""


@dataclass(frozen=True, slots=True)
class AuthResult:
    """`auth-result: mechanism=result`. `regex` is a single
    precompiled pattern built by `config.py` from the parsed mechanism and
    result words, never re-built per candidate at eval
    time -- mirroring `RegexPattern`'s precompile-once discipline."""

    regex: re.Pattern[str]


#: `processor: "name.field op value"`. `field` is a
#: closed set (`"value"` or `"confidence"`), validated by `config.py`'s
#: parser -- nothing else is a legal field name. `value` is the parsed
#: comparand: a bool, a float, or a plain string (a declared choice/level
#: option name).
@dataclass(frozen=True, slots=True)
class ProcessorCondition:
    name: str
    field: Literal["value", "confidence"]
    op: ComparisonOp
    value: bool | float | str


#: One processor's resolved answer for one candidate this round.
#: `confidence` is `None` for a chat-backed
#: processor whose declared schema does not include it (jev always
#: populates it) -- reading a `None` field via `processor:` is `UNKNOWN`,
#: never a type error.
@dataclass(frozen=True, slots=True)
class ProcessorAnswer:
    value: bool | float | str
    confidence: float | None


Atom = (
    OlderThan
    | NewerThan
    | SenderMatch
    | RecipientMatch
    | SubjectContains
    | ListIdContains
    | HasHeader
    | HasFlag
    | InMailbox
    | LargerThan
    | RecipientCount
    | HasAttachment
    | AuthResult
    | ProcessorCondition
)


# --- Boolean composition -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class AllNode:
    children: tuple[ConditionTree, ...]


@dataclass(frozen=True, slots=True)
class AnyNode:
    children: tuple[ConditionTree, ...]


@dataclass(frozen=True, slots=True)
class NoneNode:
    """True if every child is FALSE, false if any child is TRUE, else
    UNKNOWN -- the De Morgan mirror of `AnyNode`."""

    children: tuple[ConditionTree, ...]


@dataclass(frozen=True, slots=True)
class NotNode:
    child: ConditionTree


ConditionTree = AllNode | AnyNode | NoneNode | NotNode | Atom


def _eval_atom(
    atom: Atom,
    candidate: Candidate,
    *,
    now: datetime,
    processor_values: Mapping[str, ProcessorAnswer],
) -> Tri:
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
    if isinstance(atom, SubjectContains):
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
    if isinstance(atom, RecipientCount):
        comparator = _COMPARATORS[atom.op]
        matched = comparator(len(candidate.recipients), atom.value)
        return Tri.TRUE if matched else Tri.FALSE
    if isinstance(atom, HasAttachment):
        return Tri.TRUE if candidate.has_attachment else Tri.FALSE
    if isinstance(atom, AuthResult):
        if candidate.auth_results is None:
            return Tri.FALSE
        return Tri.TRUE if atom.regex.search(candidate.auth_results) else Tri.FALSE
    if isinstance(atom, ProcessorCondition):
        return _eval_processor_condition(atom, processor_values)
    _assert_never(atom)


def _eval_processor_condition(
    atom: ProcessorCondition, processor_values: Mapping[str, ProcessorAnswer]
) -> Tri:
    """`UNKNOWN` until the named processor has
    answered this candidate this round; `UNKNOWN` again if the field it
    asks about (`confidence` on a chat processor that never declared it)
    was never populated. `evaluate()`'s docstring promises it never
    raises on a well-typed tree, but a mismatched comparand type (which
    config-load validation should already prevent) is defended against
    with a plain `Tri.FALSE` rather than trusting that guarantee blindly.
    """
    answer = processor_values.get(atom.name)
    if answer is None:
        return Tri.UNKNOWN
    field_value: bool | float | str | None = (
        answer.value if atom.field == "value" else answer.confidence
    )
    if field_value is None:
        return Tri.UNKNOWN
    comparator = _COMPARATORS[atom.op]
    try:
        matched = comparator(field_value, atom.value)
    except TypeError:
        return Tri.FALSE
    return Tri.TRUE if matched else Tri.FALSE


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


_EMPTY_PROCESSOR_VALUES: Mapping[str, ProcessorAnswer] = types.MappingProxyType({})


def evaluate(
    tree: ConditionTree,
    candidate: Candidate,
    *,
    now: datetime,
    processor_values: Mapping[str, ProcessorAnswer] = _EMPTY_PROCESSOR_VALUES,
) -> Tri:
    """Evaluate a condition tree with Kleene three-valued logic.

    - `all` is FALSE if any child is FALSE, TRUE if every child is TRUE,
      else UNKNOWN.
    - `any` is TRUE if any child is TRUE, FALSE if every child is FALSE,
      else UNKNOWN.
    - `none` is TRUE if every child is FALSE, FALSE if any child is TRUE,
      else UNKNOWN -- the De Morgan mirror of `any`.
    - `not` swaps TRUE/FALSE and leaves UNKNOWN unchanged.
    - A `processor:` atom is UNKNOWN until `processor_values` carries a
      resolved answer for its processor name;
      every other atom is deterministic. Omitting `processor_values`
      entirely (the default) is exactly equivalent to no processor having
      answered yet -- every `ProcessorCondition` atom evaluates UNKNOWN.

    Never raises on a well-typed tree.
    """
    if isinstance(tree, AllNode):
        results = [
            evaluate(child, candidate, now=now, processor_values=processor_values)
            for child in tree.children
        ]
        if any(r is Tri.FALSE for r in results):
            return Tri.FALSE
        if all(r is Tri.TRUE for r in results):
            return Tri.TRUE
        return Tri.UNKNOWN
    if isinstance(tree, AnyNode):
        results = [
            evaluate(child, candidate, now=now, processor_values=processor_values)
            for child in tree.children
        ]
        if any(r is Tri.TRUE for r in results):
            return Tri.TRUE
        if all(r is Tri.FALSE for r in results):
            return Tri.FALSE
        return Tri.UNKNOWN
    if isinstance(tree, NoneNode):
        results = [
            evaluate(child, candidate, now=now, processor_values=processor_values)
            for child in tree.children
        ]
        if any(r is Tri.TRUE for r in results):
            return Tri.FALSE
        if all(r is Tri.FALSE for r in results):
            return Tri.TRUE
        return Tri.UNKNOWN
    if isinstance(tree, NotNode):
        inner = evaluate(
            tree.child, candidate, now=now, processor_values=processor_values
        )
        if inner is Tri.TRUE:
            return Tri.FALSE
        if inner is Tri.FALSE:
            return Tri.TRUE
        return Tri.UNKNOWN
    return _eval_atom(tree, candidate, now=now, processor_values=processor_values)


def processor_names(tree: ConditionTree) -> frozenset[str]:
    """Every distinct processor name referenced anywhere in `tree`,
    replacing the old single-`llm`-atom
    `llm_atom()` walk now that a rule may reference several named
    processors, shared freely with other rules. `evaluate.py` uses this
    to know which processors a still-`UNKNOWN` rule needs answered."""
    if isinstance(tree, AllNode | AnyNode | NoneNode):
        names: set[str] = set()
        for child in tree.children:
            names |= processor_names(child)
        return frozenset(names)
    if isinstance(tree, NotNode):
        return processor_names(tree.child)
    if isinstance(tree, ProcessorCondition):
        return frozenset({tree.name})
    return frozenset()


def _sender_matches_protect_entry(address: str, entry: str) -> bool:
    """`protect.senders` entries match case-insensitively as
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


def is_protected_by_flags(
    flags: Collection[str],
    *,
    protected_flags: Collection[str],
    protect_unread: bool,
) -> bool:
    """The flags-only half of `is_protected`: a
    protected flag, or (when `protect_unread`) the absence of `\\Seen`.

    Deliberately takes a bare `flags` collection rather than a
    `Candidate`, so this can be re-checked against a *freshly fetched*
    flag set at execute time's re-verify step without
    needing a full `Candidate` rebuild. `protect.senders` has no
    equivalent narrow helper: it keys off `from_address`, which
    `domain.fingerprint()` already covers, so it cannot go stale between
    scan and execute the way flags can.
    """
    flags_lower = {f.lower() for f in flags}
    for flag in protected_flags:
        if flag.lower() in _SYSTEM_FLAGS:
            if flag.lower() in flags_lower:
                return True
        elif flag in flags:
            return True
    return protect_unread and "\\seen" not in flags_lower


def is_protected(
    candidate: Candidate,
    *,
    protected_flags: Collection[str],
    protected_senders: Sequence[str],
    protect_unread: bool,
) -> bool:
    """The deterministic exclusion applied before any rule is evaluated
    or any candidate is offered to a classifier: a
    protected flag, a protected sender, or (when `protect_unread`) the
    absence of `\\Seen`. Pure and synchronous — nothing here can reach a
    model, which is exactly what guarantees a protected message never
    triggers a model call.
    """
    if is_protected_by_flags(
        candidate.flags,
        protected_flags=protected_flags,
        protect_unread=protect_unread,
    ):
        return True
    if candidate.from_address:
        for sender in protected_senders:
            if _sender_matches_protect_entry(candidate.from_address, sender):
                return True
    return False
