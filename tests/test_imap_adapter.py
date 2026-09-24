"""Unit tests for `liametahi.imap_adapter`'s BODYSTRUCTURE parser, the
has-attachment heuristic, and the auth-result trust boundary.

`FakeMailbox` never exercises the real wire-level
BODYSTRUCTURE parser -- it derives `has_attachment` independently via
`email.message.Message.walk()` (`tests/fakes/fake_mailbox.py`) -- so this
module is the only unit-tier coverage of the actual recursive-descent
parser and flatten-and-scan heuristic. No network, no Docker: every case
below is a hand-written FETCH response fragment.
"""

from datetime import UTC, datetime

from liametahi.imap_adapter import (
    RawMetadata,
    _bodystructure_body_shape,
    _bodystructure_has_attachment,
    _parse_fetch_line,
    normalize,
)

_HEADER_LITERAL = b"From: sender@example.com\r\nSubject: hello\r\n\r\n"


def _bodystructure_line(body: bytes) -> bytes:
    """A minimal untagged-FETCH-response `line` fragment with `body` as
    the raw `BODYSTRUCTURE` value, trailed by more FETCH data items --
    matching the real shape where BODYSTRUCTURE is one item among
    several, not the last thing on the line."""
    return b"1 (UID 101 BODYSTRUCTURE " + body + b" FLAGS (\\Seen))"


def _full_fetch_line(bodystructure: bytes | None) -> bytes:
    """A realistic full FETCH response line, optionally including
    BODYSTRUCTURE, for exercising `_parse_fetch_line` end to end."""
    parts = [
        b'1 (UID 101 INTERNALDATE "01-Jun-2026 08:00:05 +0000" RFC822.SIZE 529',
        b"FLAGS (\\Seen)",
    ]
    if bodystructure is not None:
        parts.append(b"BODYSTRUCTURE " + bodystructure)
    parts.append(b"BODY[HEADER.FIELDS (FROM SUBJECT)]")
    return b" ".join(parts)


# --- 1. Plain text/plain, no attachment -------------------------------


def test_plain_text_no_attachment() -> None:
    body = b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 1152 23)'
    assert _bodystructure_has_attachment(_bodystructure_line(body)) is False


def test_quoted_string_escapes_are_consumed_correctly() -> None:
    """A filename containing an escaped quote or backslash must not
    desynchronise the parser's notion of where the quoted string ends --
    `_parse_quoted_string` must consume `\\"`/`\\\\` as single characters,
    not as an early close-quote or a stray backslash."""
    body = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 100 5)'
        b'("APPLICATION" "PDF" ("NAME" "quote \\" and backslash \\\\ name.pdf") '
        b'NIL NIL "BASE64" 45000 NIL '
        b'("attachment" ("FILENAME" "quote \\" and backslash \\\\ name.pdf")) '
        b'NIL NIL) "MIXED")'
    )
    assert _bodystructure_has_attachment(_bodystructure_line(body)) is True


# --- Empty literal: a flags-only fetch requesting no header fields -----
# (`imap_adapter.scan()`'s per-run
# flags refresh, and `execute.py`'s reconcile pass, both call
# `fetch_metadata(uids, ())`. `ImapMailbox.fetch_metadata` omits the
# `BODY.PEEK[HEADER.FIELDS (...)]` data item entirely in that case
# rather than sending the not-well-formed `HEADER.FIELDS ()` -- which
# means the wire response for such a fetch carries no literal at all, so
# `_parse_fetch_line` must still parse cleanly when handed an empty
# literal standing in for "no literal was present.")


def test_parse_fetch_line_with_no_header_literal_still_parses() -> None:
    line = _full_fetch_line(bodystructure=None).replace(
        b" BODY[HEADER.FIELDS (FROM SUBJECT)]", b""
    )
    parsed = _parse_fetch_line(line, b"")
    assert parsed is not None
    assert parsed.uid == 101
    assert parsed.rfc822_size == 529
    assert parsed.flags == frozenset({"\\Seen"})
    assert parsed.headers == {}


# --- 2. multipart/alternative of text+html, no attachment --------------


def test_multipart_alternative_text_and_html_no_attachment() -> None:
    body = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 51 3)'
        b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 102 5)'
        b'"ALTERNATIVE")'
    )
    assert _bodystructure_has_attachment(_bodystructure_line(body)) is False


# --- 3. multipart/mixed with a PDF attachment, both NAME and disposition


def test_multipart_mixed_with_pdf_attachment_true() -> None:
    body = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 100 5)'
        b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 45000 NIL '
        b'("attachment" ("FILENAME" "invoice.pdf")) NIL NIL) "MIXED")'
    )
    line = _full_fetch_line(body)
    parsed = _parse_fetch_line(line, _HEADER_LITERAL)
    assert parsed is not None
    assert parsed.has_attachment is True
    # The rest of RawMetadata still parses correctly alongside the new field.
    assert parsed.uid == 101
    assert parsed.rfc822_size == 529
    assert parsed.flags == frozenset({"\\Seen"})
    assert parsed.internaldate == datetime(2026, 6, 1, 8, 0, 5, tzinfo=UTC)


# --- 4. NAME parameter with no explicit disposition -> True (heuristic) --


def test_name_parameter_without_disposition_is_documented_true_positive() -> None:
    """The documented heuristic trade-off: an inline image with
    only a NAME parameter and no Content-Disposition still registers as
    an attachment, even though no mail client would show it as one to a
    user. Asserted deliberately, not avoided."""
    body = b'("IMAGE" "PNG" ("NAME" "logo.png") NIL NIL "BASE64" 3000 NIL NIL NIL)'
    assert _bodystructure_has_attachment(_bodystructure_line(body)) is True


# --- 5. Nested multipart: mixed containing alternative, plus attachment --


def test_nested_multipart_with_real_attachment_true() -> None:
    body = (
        b"("
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 51 3)'
        b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 102 5)'
        b'"ALTERNATIVE")'
        b'("APPLICATION" "ZIP" ("NAME" "archive.zip") NIL NIL "BASE64" 20000 NIL '
        b'("attachment" ("FILENAME" "archive.zip")) NIL NIL)'
        b'"MIXED")'
    )
    line = _full_fetch_line(body)
    parsed = _parse_fetch_line(line, _HEADER_LITERAL)
    assert parsed is not None
    assert parsed.has_attachment is True


# --- 6. Truncated / mismatched parens -> False, never raises -----------


def test_unbalanced_parens_returns_false_not_raise() -> None:
    truncated = b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 100 5)'
    # No closing paren anywhere after this -- the parser runs off the end
    # of the line while still inside a nested list.
    line = b"1 (UID 101 BODYSTRUCTURE " + truncated
    assert _bodystructure_has_attachment(line) is False


def test_literal_marker_mid_structure_returns_false_not_raise() -> None:
    """A `{n}` literal-length marker embedded where this simple parser
    does not expect one is the one grammar construct it deliberately
    does not support -- it must degrade to False, not crash."""
    body = b'("TEXT" "PLAIN" ("CHARSET" {5}\r\nUTF-8) NIL NIL "7BIT" 100 5)'
    assert _bodystructure_has_attachment(_bodystructure_line(body)) is False


# --- 7. BODYSTRUCTURE absent entirely -> False, rest of RawMetadata intact


def test_bodystructure_absent_returns_false_and_rest_still_parses() -> None:
    line = _full_fetch_line(None)
    parsed = _parse_fetch_line(line, _HEADER_LITERAL)
    assert parsed is not None
    assert parsed.has_attachment is False
    assert parsed.uid == 101
    assert parsed.rfc822_size == 529
    assert parsed.flags == frozenset({"\\Seen"})
    assert parsed.internaldate == datetime(2026, 6, 1, 8, 0, 5, tzinfo=UTC)
    assert parsed.headers.get("from") == ("sender@example.com",)
    assert parsed.headers.get("subject") == ("hello",)


# --- 8. A FLAGS keyword literally named "BODYSTRUCTURE" must not shadow the
#        real field. `label:BODYSTRUCTURE` is a legal rule action (the
#        grammar forbids only whitespace and `(){%*"\]`), and our own FETCH command
#        always requests FLAGS before BODYSTRUCTURE, so a naive first-match
#        substring search finds the flag name instead of the real value.


def test_flags_keyword_named_bodystructure_does_not_shadow_real_field() -> None:
    real_body = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 100 5)'
        b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 45000 NIL '
        b'("attachment" ("FILENAME" "invoice.pdf")) NIL NIL) "MIXED")'
    )
    line = (
        b'1 (UID 101 INTERNALDATE "01-Jun-2026 08:00:05 +0000" RFC822.SIZE 529 '
        b"FLAGS (BODYSTRUCTURE AAAKEYWORD) BODYSTRUCTURE " + real_body + b")"
    )
    assert _bodystructure_has_attachment(line) is True

    parsed = _parse_fetch_line(line, _HEADER_LITERAL)
    assert parsed is not None
    assert parsed.has_attachment is True
    assert parsed.flags == frozenset({"BODYSTRUCTURE", "AAAKEYWORD"})


def test_flags_keyword_named_bodystructure_no_real_attachment_still_false() -> None:
    """Same shadowing shape, but the real BODYSTRUCTURE genuinely has no
    attachment -- confirms the fix locates the correct field rather than
    just happening to return True for the wrong reason."""
    real_body = b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 1152 23)'
    line = b"1 (UID 101 FLAGS (BODYSTRUCTURE) BODYSTRUCTURE " + real_body + b")"
    assert _bodystructure_has_attachment(line) is False


def test_quoted_parens_before_bodystructure_do_not_desync_depth() -> None:
    """The field-locating scan (not the recursive-descent parser itself)
    tracks parenthesis depth to find the real top-level BODYSTRUCTURE
    field; a literal `(`/`)` inside an earlier quoted header value (e.g.
    INTERNALDATE, or here a synthetic quoted field standing in for one)
    must not be miscounted as real nesting."""
    real_body = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 100 5)'
        b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 45000 NIL '
        b'("attachment" ("FILENAME" "invoice.pdf")) NIL NIL) "MIXED")'
    )
    line = (
        b'1 (UID 101 SOMEFIELD "a (fake) paren pair inside quotes" '
        b"BODYSTRUCTURE " + real_body + b")"
    )
    assert _bodystructure_has_attachment(line) is True


# --- Extra: RawMetadata construction sanity ---------------------------


def test_raw_metadata_accepts_has_attachment_field() -> None:
    meta = RawMetadata(
        uid=1,
        internaldate=datetime(2026, 1, 1, tzinfo=UTC),
        rfc822_size=10,
        flags=frozenset(),
        headers={},
        has_attachment=True,
        body_shape="neither",
    )
    assert meta.has_attachment is True


# --- auth-result trust boundary: normalize() must use the topmost header -


def test_normalize_uses_topmost_authentication_results_header() -> None:
    """A message can carry more than one Authentication-Results
    header, one per hop that performed its own checks. Only the topmost
    (first-encountered, i.e. added last, by the hop closest to the
    recipient) is trustworthy -- an earlier hop's header could in
    principle be attacker-supplied content in a forwarded/relayed
    message. `RawMetadata.headers` preserves wire top-to-bottom order,
    so `normalize()` must pick index 0, never the last
    or an arbitrary one."""
    raw = RawMetadata(
        uid=1,
        internaldate=datetime(2026, 1, 1, tzinfo=UTC),
        rfc822_size=10,
        flags=frozenset(),
        headers={
            "authentication-results": (
                "mx.recipient.example; spf=pass",
                "relay.upstream.example; spf=fail (attacker-controlled hop)",
            )
        },
        has_attachment=False,
        body_shape="neither",
    )
    candidate = normalize(raw, account_id=1, mailbox="INBOX", uidvalidity=1000)
    assert candidate.auth_results == "mx.recipient.example; spf=pass"


# --- `body_shape`: derived from BODYSTRUCTURE alone, no body fetch --------


def test_body_shape_plain_only() -> None:
    body = b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 1152 23)'
    assert _bodystructure_body_shape(_bodystructure_line(body)) == "plain_only"


def test_body_shape_both_in_multipart_alternative() -> None:
    body = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 51 3)'
        b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 102 5)'
        b'"ALTERNATIVE")'
    )
    assert _bodystructure_body_shape(_bodystructure_line(body)) == "both"


def test_body_shape_plain_only_alongside_an_attachment() -> None:
    """A `text/plain` part nested inside `multipart/mixed` alongside a
    non-text attachment is still found -- `body_shape` walks every leaf
    part, not just a top-level one."""
    body = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 100 5)'
        b'("APPLICATION" "PDF" ("NAME" "invoice.pdf") NIL NIL "BASE64" 45000 NIL '
        b'("attachment" ("FILENAME" "invoice.pdf")) NIL NIL) "MIXED")'
    )
    assert _bodystructure_body_shape(_bodystructure_line(body)) == "plain_only"


def test_body_shape_neither_when_bodystructure_absent() -> None:
    line = b"1 (UID 101 FLAGS (\\Seen))"
    assert _bodystructure_body_shape(line) == "neither"


def test_body_shape_neither_on_unbalanced_parens() -> None:
    # No closing paren anywhere before the line ends -- `_parse_list`
    # must run out of data and raise, not hang or crash.
    line = b'1 (UID 101 BODYSTRUCTURE ("TEXT" "PLAIN"'
    assert _bodystructure_body_shape(line) == "neither"


def test_body_shape_html_only_single_part_message() -> None:
    """A non-multipart message whose sole part is `text/html`."""
    body = b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 200 10)'
    assert _bodystructure_body_shape(_bodystructure_line(body)) == "html_only"


def test_body_shape_neither_for_a_non_text_single_part_message() -> None:
    body = b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 1000 NIL)'
    assert _bodystructure_body_shape(_bodystructure_line(body)) == "neither"


# --- normalize(): new header-derived fields -----------------------------


def test_normalize_extracts_new_header_derived_fields() -> None:
    raw = RawMetadata(
        uid=1,
        internaldate=datetime(2026, 1, 1, tzinfo=UTC),
        rfc822_size=10,
        flags=frozenset(),
        headers={
            "reply-to": ("Someone <reply@example.com>",),
            "sender": ("agent@example.com",),
            "precedence": ("bulk",),
            "feedback-id": ("1:2:3:SES",),
            "auto-submitted": ("auto-generated",),
            "x-auto-response-suppress": ("All",),
            "in-reply-to": ("<parent@example.com>",),
        },
        has_attachment=False,
        body_shape="plain_only",
    )
    candidate = normalize(raw, account_id=1, mailbox="INBOX", uidvalidity=1000)
    assert candidate.reply_to == "Someone <reply@example.com>"
    assert candidate.sender == "agent@example.com"
    assert candidate.precedence == "bulk"
    assert candidate.has_feedback_id is True
    assert candidate.is_auto_submitted is True
    assert candidate.has_auto_response_suppress is True
    assert candidate.is_reply is True
    assert candidate.body_shape == "plain_only"


def test_normalize_is_reply_true_from_references_alone() -> None:
    """`is_reply` is presence of `In-Reply-To` OR `References` -- either
    alone is sufficient."""
    raw = RawMetadata(
        uid=1,
        internaldate=datetime(2026, 1, 1, tzinfo=UTC),
        rfc822_size=10,
        flags=frozenset(),
        headers={"references": ("<ancestor@example.com>",)},
        has_attachment=False,
        body_shape="neither",
    )
    candidate = normalize(raw, account_id=1, mailbox="INBOX", uidvalidity=1000)
    assert candidate.is_reply is True


def test_normalize_new_header_fields_absent_by_default() -> None:
    raw = RawMetadata(
        uid=1,
        internaldate=datetime(2026, 1, 1, tzinfo=UTC),
        rfc822_size=10,
        flags=frozenset(),
        headers={},
        has_attachment=False,
        body_shape="neither",
    )
    candidate = normalize(raw, account_id=1, mailbox="INBOX", uidvalidity=1000)
    assert candidate.reply_to is None
    assert candidate.sender is None
    assert candidate.precedence is None
    assert candidate.has_feedback_id is False
    assert candidate.is_auto_submitted is False
    assert candidate.has_auto_response_suppress is False
    assert candidate.is_reply is False
