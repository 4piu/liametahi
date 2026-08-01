"""Tests for `liametahi.rules` (spec §7.1, §7.2; contracts §5.2).

The Kleene three-valued logic tests are exhaustive over every
TRUE/FALSE/UNKNOWN combination for 1-3 children (property-style testing
without adding a dependency not listed in contracts §1) rather than
sampled, since the truth table is small and finite.
"""

import itertools
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from liametahi import rules
from liametahi.rules import (
    AllNode,
    AnyNode,
    AuthResult,
    HasAttachment,
    HasFlag,
    HasHeader,
    InMailbox,
    LargerThan,
    ListIdContains,
    LiteralPattern,
    LlmCondition,
    NewerThan,
    NotNode,
    OlderThan,
    RecipientCount,
    RecipientMatch,
    RegexPattern,
    SenderMatch,
    SubjectContains,
    Tri,
    evaluate,
    is_protected,
    is_protected_by_flags,
    llm_atom,
)
from tests.conftest import make_candidate

NOW = datetime(2026, 7, 30, tzinfo=UTC)


# --- A minimal deterministic "leaf" for exhaustive Kleene testing --------


class _FixedAtom:
    """A stand-in leaf node with a fixed Tri result, used only to drive
    the composition truth table exhaustively without depending on any
    one real atom's semantics."""

    def __init__(self, value: Tri):
        self.value = value


#: A node in the exhaustive-Kleene test trees below is either a real
#: `rules.ConditionTree` node or a `_FixedAtom` leaf standing in for one.
_TestNode = rules.ConditionTree | _FixedAtom


def _all(children: Sequence[_TestNode]) -> AllNode:
    return AllNode(tuple(children))  # type: ignore[arg-type]


def _any(children: Sequence[_TestNode]) -> AnyNode:
    return AnyNode(tuple(children))  # type: ignore[arg-type]


def _not(child: _TestNode) -> NotNode:
    return NotNode(child)  # type: ignore[arg-type]


def _evaluate_with_fixed(node: _TestNode, *, now: datetime) -> Tri:
    """Evaluate a tree that may contain `_FixedAtom` leaves, reusing the
    real `evaluate()` composition logic by monkeypatching-free recursion."""
    if isinstance(node, _FixedAtom):
        return node.value
    if isinstance(node, AllNode):
        results = [_evaluate_with_fixed(c, now=now) for c in node.children]
        if any(r is Tri.FALSE for r in results):
            return Tri.FALSE
        if all(r is Tri.TRUE for r in results):
            return Tri.TRUE
        return Tri.UNKNOWN
    if isinstance(node, AnyNode):
        results = [_evaluate_with_fixed(c, now=now) for c in node.children]
        if any(r is Tri.TRUE for r in results):
            return Tri.TRUE
        if all(r is Tri.FALSE for r in results):
            return Tri.FALSE
        return Tri.UNKNOWN
    if isinstance(node, NotNode):
        inner = _evaluate_with_fixed(node.child, now=now)
        if inner is Tri.TRUE:
            return Tri.FALSE
        if inner is Tri.FALSE:
            return Tri.TRUE
        return Tri.UNKNOWN
    raise AssertionError(f"unexpected node {node!r}")


TRI_VALUES = (Tri.TRUE, Tri.FALSE, Tri.UNKNOWN)


@pytest.mark.parametrize("values", list(itertools.product(TRI_VALUES, repeat=1)))
def test_kleene_all_one_child(values: tuple[Tri, ...]) -> None:
    children = [_FixedAtom(v) for v in values]
    result = _evaluate_with_fixed(_all(children), now=NOW)
    assert result == values[0]


@pytest.mark.parametrize("values", list(itertools.product(TRI_VALUES, repeat=2)))
def test_kleene_all_two_children(values: tuple[Tri, Tri]) -> None:
    children = [_FixedAtom(v) for v in values]
    result = _evaluate_with_fixed(_all(children), now=NOW)
    if Tri.FALSE in values:
        assert result == Tri.FALSE
    elif all(v is Tri.TRUE for v in values):
        assert result == Tri.TRUE
    else:
        assert result == Tri.UNKNOWN


@pytest.mark.parametrize("values", list(itertools.product(TRI_VALUES, repeat=3)))
def test_kleene_all_three_children(values: tuple[Tri, Tri, Tri]) -> None:
    children = [_FixedAtom(v) for v in values]
    result = _evaluate_with_fixed(_all(children), now=NOW)
    if Tri.FALSE in values:
        assert result == Tri.FALSE
    elif all(v is Tri.TRUE for v in values):
        assert result == Tri.TRUE
    else:
        assert result == Tri.UNKNOWN


@pytest.mark.parametrize("values", list(itertools.product(TRI_VALUES, repeat=2)))
def test_kleene_any_two_children(values: tuple[Tri, Tri]) -> None:
    children = [_FixedAtom(v) for v in values]
    result = _evaluate_with_fixed(_any(children), now=NOW)
    if Tri.TRUE in values:
        assert result == Tri.TRUE
    elif all(v is Tri.FALSE for v in values):
        assert result == Tri.FALSE
    else:
        assert result == Tri.UNKNOWN


@pytest.mark.parametrize("values", list(itertools.product(TRI_VALUES, repeat=3)))
def test_kleene_any_three_children(values: tuple[Tri, Tri, Tri]) -> None:
    children = [_FixedAtom(v) for v in values]
    result = _evaluate_with_fixed(_any(children), now=NOW)
    if Tri.TRUE in values:
        assert result == Tri.TRUE
    elif all(v is Tri.FALSE for v in values):
        assert result == Tri.FALSE
    else:
        assert result == Tri.UNKNOWN


@pytest.mark.parametrize("value", TRI_VALUES)
def test_kleene_not(value: Tri) -> None:
    result = _evaluate_with_fixed(_not(_FixedAtom(value)), now=NOW)
    expected = {Tri.TRUE: Tri.FALSE, Tri.FALSE: Tri.TRUE, Tri.UNKNOWN: Tri.UNKNOWN}
    assert result == expected[value]


def test_kleene_nested_composition() -> None:
    # all(any(F, U), not(T)) -> all(U, F) -> FALSE
    tree = _all(
        [
            _any([_FixedAtom(Tri.FALSE), _FixedAtom(Tri.UNKNOWN)]),
            _not(_FixedAtom(Tri.TRUE)),
        ]
    )
    assert _evaluate_with_fixed(tree, now=NOW) == Tri.FALSE


def test_evaluate_never_raises_on_well_typed_tree() -> None:
    candidate = make_candidate()
    trees: list[rules.ConditionTree] = [
        OlderThan(duration=timedelta(days=1)),
        AllNode((OlderThan(duration=timedelta(days=1)), LlmCondition("x"))),
        AnyNode(
            (
                SenderMatch(LiteralPattern("*@x.com")),
                NotNode(SubjectContains(LiteralPattern("y"))),
            )
        ),
        NotNode(HasHeader("x-foo")),
    ]
    for tree in trees:
        evaluate(tree, candidate, now=NOW)  # must not raise


# --- Atomic condition semantics (spec §7.1) -------------------------------


def test_older_than_true_and_false() -> None:
    old = make_candidate(internaldate=NOW - timedelta(days=40))
    new = make_candidate(internaldate=NOW - timedelta(days=1))
    cond = OlderThan(duration=timedelta(days=30))
    assert evaluate(cond, old, now=NOW) == Tri.TRUE
    assert evaluate(cond, new, now=NOW) == Tri.FALSE


def test_newer_than_true_and_false() -> None:
    old = make_candidate(internaldate=NOW - timedelta(days=40))
    new = make_candidate(internaldate=NOW - timedelta(days=1))
    cond = NewerThan(duration=timedelta(days=7))
    assert evaluate(cond, new, now=NOW) == Tri.TRUE
    assert evaluate(cond, old, now=NOW) == Tri.FALSE


def test_sender_match_is_glob_against_address_only_case_insensitive() -> None:
    candidate = make_candidate(
        from_address="NoReply@Example.ORG", from_display="Example Sender"
    )
    assert (
        evaluate(SenderMatch(LiteralPattern("*@example.org")), candidate, now=NOW)
        == Tri.TRUE
    )
    assert (
        evaluate(SenderMatch(LiteralPattern("*@other.org")), candidate, now=NOW)
        == Tri.FALSE
    )
    # Display name must never be matched by sender-match.
    assert (
        evaluate(SenderMatch(LiteralPattern("*sender*")), candidate, now=NOW)
        == Tri.FALSE
    )


def test_sender_match_false_when_address_absent() -> None:
    candidate = make_candidate(from_address=None)
    assert evaluate(SenderMatch(LiteralPattern("*")), candidate, now=NOW) == Tri.FALSE


def test_recipient_match_checks_union_true_if_any_matches() -> None:
    candidate = make_candidate(recipients=("a@x.com", "b@mozmail.com"))
    assert (
        evaluate(RecipientMatch(LiteralPattern("*@mozmail.com")), candidate, now=NOW)
        == Tri.TRUE
    )
    assert (
        evaluate(RecipientMatch(LiteralPattern("*@nomatch.com")), candidate, now=NOW)
        == Tri.FALSE
    )


def test_subject_contains_is_substring_not_glob_case_insensitive() -> None:
    candidate = make_candidate(subject="Your Weekly Digest is here")
    assert (
        evaluate(SubjectContains(LiteralPattern("weekly digest")), candidate, now=NOW)
        == Tri.TRUE
    )
    assert (
        evaluate(SubjectContains(LiteralPattern("*weekly*")), candidate, now=NOW)
        == Tri.FALSE
    )  # literal, not glob
    assert (
        evaluate(SubjectContains(LiteralPattern("monthly")), candidate, now=NOW)
        == Tri.FALSE
    )


def test_subject_contains_false_when_subject_absent() -> None:
    candidate = make_candidate(subject=None)
    assert (
        evaluate(SubjectContains(LiteralPattern("x")), candidate, now=NOW) == Tri.FALSE
    )


def test_list_id_contains_matches_identifier_only() -> None:
    candidate = make_candidate(list_id="announce.mozilla.org")
    assert (
        evaluate(ListIdContains(LiteralPattern("mozilla")), candidate, now=NOW)
        == Tri.TRUE
    )
    assert (
        evaluate(ListIdContains(LiteralPattern("nomatch")), candidate, now=NOW)
        == Tri.FALSE
    )


def test_list_id_contains_false_when_header_absent() -> None:
    candidate = make_candidate(list_id=None)
    assert (
        evaluate(ListIdContains(LiteralPattern("x")), candidate, now=NOW) == Tri.FALSE
    )


@pytest.mark.parametrize(
    "op,value,expected",
    [
        (">", 3, Tri.TRUE),
        (">", 5, Tri.FALSE),
        (">=", 5, Tri.TRUE),
        (">=", 6, Tri.FALSE),
        ("<", 6, Tri.TRUE),
        ("<", 5, Tri.FALSE),
        ("<=", 5, Tri.TRUE),
        ("<=", 4, Tri.FALSE),
        ("==", 5, Tri.TRUE),
        ("==", 4, Tri.FALSE),
        ("!=", 4, Tri.TRUE),
        ("!=", 5, Tri.FALSE),
    ],
)
def test_recipient_count_compares_deduplicated_union_size(
    op: rules.ComparisonOp, value: int, expected: Tri
) -> None:
    candidate = make_candidate(
        recipients=("a@x.com", "b@x.com", "c@x.com", "d@x.com", "e@x.com")
    )
    assert evaluate(RecipientCount(op=op, value=value), candidate, now=NOW) == expected


def test_recipient_count_zero_when_no_recipients() -> None:
    candidate = make_candidate(recipients=())
    assert evaluate(RecipientCount(op="==", value=0), candidate, now=NOW) == Tri.TRUE


# --- Regex-literal form (`/pattern/flags`) ---------------------------------


def test_sender_match_regex_is_case_sensitive_by_default() -> None:
    candidate = make_candidate(from_address="NoReply@Example.ORG")
    assert (
        evaluate(
            SenderMatch(RegexPattern(re.compile(r"noreply@example\.org"))),
            candidate,
            now=NOW,
        )
        == Tri.FALSE
    )  # case mismatch: no `i` flag
    assert (
        evaluate(
            SenderMatch(RegexPattern(re.compile(r"NoReply@Example\.ORG"))),
            candidate,
            now=NOW,
        )
        == Tri.TRUE
    )


def test_subject_contains_regex_matches_anywhere_in_string() -> None:
    candidate = make_candidate(subject="Your Weekly Digest is here")
    assert (
        evaluate(
            SubjectContains(RegexPattern(re.compile(r"week\w+ digest", re.IGNORECASE))),
            candidate,
            now=NOW,
        )
        == Tri.TRUE
    )
    assert (
        evaluate(
            SubjectContains(RegexPattern(re.compile(r"^Digest"))), candidate, now=NOW
        )
        == Tri.FALSE
    )


def test_has_header_present_and_absent() -> None:
    candidate = make_candidate(headers_present=frozenset({"list-id", "subject"}))
    assert evaluate(HasHeader("list-id"), candidate, now=NOW) == Tri.TRUE
    assert evaluate(HasHeader("x-custom"), candidate, now=NOW) == Tri.FALSE


def test_has_flag_system_flag_case_insensitive() -> None:
    candidate = make_candidate(flags=frozenset({"\\Flagged"}))
    assert evaluate(HasFlag("\\flagged"), candidate, now=NOW) == Tri.TRUE
    assert evaluate(HasFlag("\\Answered"), candidate, now=NOW) == Tri.FALSE


def test_has_flag_custom_keyword_exact_case_sensitive() -> None:
    candidate = make_candidate(flags=frozenset({"Important"}))
    assert evaluate(HasFlag("Important"), candidate, now=NOW) == Tri.TRUE
    assert evaluate(HasFlag("important"), candidate, now=NOW) == Tri.FALSE


def test_in_mailbox_case_sensitive_except_inbox() -> None:
    candidate = make_candidate(mailbox="INBOX")
    assert evaluate(InMailbox("inbox"), candidate, now=NOW) == Tri.TRUE
    assert evaluate(InMailbox("INBOX"), candidate, now=NOW) == Tri.TRUE
    other = make_candidate(mailbox="Archive")
    assert evaluate(InMailbox("Archive"), other, now=NOW) == Tri.TRUE
    assert evaluate(InMailbox("archive"), other, now=NOW) == Tri.FALSE


def test_larger_than() -> None:
    candidate = make_candidate(rfc822_size=600 * 1024)
    assert evaluate(LargerThan(size_bytes=500 * 1024), candidate, now=NOW) == Tri.TRUE
    assert evaluate(LargerThan(size_bytes=700 * 1024), candidate, now=NOW) == Tri.FALSE


def test_has_attachment_true_and_false() -> None:
    with_attachment = make_candidate(has_attachment=True)
    without_attachment = make_candidate(has_attachment=False)
    assert evaluate(HasAttachment(), with_attachment, now=NOW) == Tri.TRUE
    assert evaluate(HasAttachment(), without_attachment, now=NOW) == Tri.FALSE


def _auth_result_atom(mechanism: str, result: str) -> AuthResult:
    return AuthResult(
        regex=re.compile(
            rf"\b{re.escape(mechanism)}\s*=\s*{re.escape(result)}\b", re.IGNORECASE
        )
    )


def test_auth_result_matching_mechanism_and_result_is_true() -> None:
    candidate = make_candidate(
        auth_results="mx.example.com; spf=fail smtp.mailfrom=evil.example; dkim=pass"
    )
    assert evaluate(_auth_result_atom("spf", "fail"), candidate, now=NOW) == Tri.TRUE
    assert evaluate(_auth_result_atom("dkim", "pass"), candidate, now=NOW) == Tri.TRUE


def test_auth_result_wrong_result_is_false() -> None:
    candidate = make_candidate(auth_results="mx.example.com; spf=pass")
    assert evaluate(_auth_result_atom("spf", "fail"), candidate, now=NOW) == Tri.FALSE


def test_auth_result_mechanism_absent_is_false() -> None:
    candidate = make_candidate(auth_results="mx.example.com; dkim=pass")
    assert evaluate(_auth_result_atom("spf", "fail"), candidate, now=NOW) == Tri.FALSE


def test_auth_result_none_header_is_false() -> None:
    candidate = make_candidate(auth_results=None)
    assert evaluate(_auth_result_atom("spf", "fail"), candidate, now=NOW) == Tri.FALSE


def test_auth_result_case_insensitive_on_mechanism_and_result_tokens() -> None:
    candidate = make_candidate(auth_results="mx.example.com; SPF=FAIL")
    assert evaluate(_auth_result_atom("spf", "fail"), candidate, now=NOW) == Tri.TRUE


def test_llm_atom_is_always_unknown() -> None:
    candidate = make_candidate()
    assert evaluate(LlmCondition("some description"), candidate, now=NOW) == Tri.UNKNOWN


# --- llm_atom() extraction -------------------------------------------------


def test_llm_atom_extracts_the_single_atom() -> None:
    llm = LlmCondition("desc")
    tree = AllNode((OlderThan(duration=timedelta(days=1)), llm))
    assert llm_atom(tree) is llm


def test_llm_atom_none_when_absent() -> None:
    tree = AllNode((OlderThan(duration=timedelta(days=1)),))
    assert llm_atom(tree) is None


# --- is_protected() (spec §4.2 point 1; acceptance test 2, rules half) ----


def test_acceptance_02_protected_excluded_without_llm_call() -> None:
    """spec §14 #2 (rules/config half): a protected sender, a `\\Flagged`
    message, and an unread message are each excluded by the deterministic
    `is_protected` prefilter alone — no rule tree is evaluated and no
    `llm` atom is ever consulted for them, because this check runs before
    rule evaluation begins (spec §4.2 point 1) and is itself pure/
    synchronous with no path to a classifier.
    """
    protected_flags = ["\\Flagged", "\\Answered"]
    protected_senders = ["bank.example", "boss@example.com"]

    protected_sender = make_candidate(
        from_address="alerts@bank.example", flags=frozenset({"\\Seen"})
    )
    protected_sender_subdomain = make_candidate(
        from_address="noreply@x.bank.example", flags=frozenset({"\\Seen"})
    )
    protected_sender_exact = make_candidate(
        from_address="boss@example.com", flags=frozenset({"\\Seen"})
    )
    flagged = make_candidate(
        from_address="a@nowhere.example", flags=frozenset({"\\Flagged", "\\Seen"})
    )
    unread = make_candidate(from_address="a@nowhere.example", flags=frozenset())

    for candidate in (
        protected_sender,
        protected_sender_subdomain,
        protected_sender_exact,
        flagged,
        unread,
    ):
        assert is_protected(
            candidate,
            protected_flags=protected_flags,
            protected_senders=protected_senders,
            protect_unread=True,
        )

    unprotected = make_candidate(
        from_address="nobody@nowhere.example", flags=frozenset({"\\Seen"})
    )
    assert not is_protected(
        unprotected,
        protected_flags=protected_flags,
        protected_senders=protected_senders,
        protect_unread=True,
    )


def test_is_protected_unread_disabled() -> None:
    unread = make_candidate(from_address="a@nowhere.example", flags=frozenset())
    assert not is_protected(
        unread, protected_flags=[], protected_senders=[], protect_unread=False
    )


def test_is_protected_sender_domain_suffix_does_not_match_unrelated_domain() -> None:
    candidate = make_candidate(
        from_address="a@notbank.example", flags=frozenset({"\\Seen"})
    )
    assert not is_protected(
        candidate,
        protected_flags=[],
        protected_senders=["bank.example"],
        protect_unread=False,
    )


# --- is_protected_by_flags() (sync-fix-brief Fix A, Finding 1) ------------


def test_is_protected_by_flags_matches_system_flag_case_insensitively() -> None:
    assert is_protected_by_flags(
        frozenset({"\\flagged"}),  # server-cased differently than configured
        protected_flags=["\\Flagged"],
        protect_unread=False,
    )


def test_is_protected_by_flags_matches_custom_keyword_case_sensitively() -> None:
    assert is_protected_by_flags(
        frozenset({"Important"}), protected_flags=["Important"], protect_unread=False
    )
    assert not is_protected_by_flags(
        frozenset({"important"}), protected_flags=["Important"], protect_unread=False
    )


def test_is_protected_by_flags_unread() -> None:
    assert is_protected_by_flags(frozenset(), protected_flags=[], protect_unread=True)
    assert not is_protected_by_flags(
        frozenset({"\\Seen"}), protected_flags=[], protect_unread=True
    )


def test_is_protected_by_flags_nothing_configured_is_never_protected() -> None:
    assert not is_protected_by_flags(
        frozenset({"\\Flagged"}), protected_flags=[], protect_unread=False
    )


def test_is_protected_delegates_flags_to_is_protected_by_flags() -> None:
    """`is_protected` must agree with `is_protected_by_flags` on the
    flags/unread axes -- it delegates rather than duplicating the
    `_SYSTEM_FLAGS` casefold logic (sync-fix-brief Fix A)."""
    candidate = make_candidate(from_address="nobody@nowhere.example", flags=frozenset())
    assert is_protected_by_flags(
        candidate.flags, protected_flags=[], protect_unread=True
    )
    assert is_protected(
        candidate, protected_flags=[], protected_senders=[], protect_unread=True
    )
