"""Tests for `liametahi.prompt`: payload construction, capping,
sanitisation, hashing, and the canonical request/response wire shape
(spec section 5.1, 5.2, 5.3; contracts section 5.3, section 7).
"""

from liametahi import prompt
from liametahi.classifier import CandidatePayload, OfferedRule
from tests.conftest import make_candidate

# --- Sanitisation (spec section 5.2) -------------------------------------


def test_sanitize_text_replaces_newlines_with_single_space() -> None:
    assert prompt.sanitize_text("line one\nline two") == "line one line two"
    assert prompt.sanitize_text("a\r\nb") == "a b"
    assert prompt.sanitize_text("a\rb") == "a b"


def test_sanitize_text_strips_c0_and_c1_control_characters() -> None:
    hostile = "hello\x00\x01\x1fworld\x7f\x9f!"
    assert prompt.sanitize_text(hostile) == "helloworld!"


def test_sanitize_text_strips_zero_width_characters() -> None:
    # zero-width space, zero-width non-joiner, zero-width joiner, word
    # joiner, zero-width no-break space (BOM)
    hostile = "a​b‌c‍d⁠e﻿"
    assert prompt.sanitize_text(hostile) == "abcde"


def test_sanitize_text_strips_bidirectional_override_characters() -> None:
    # LRM, RLM, LRE, RLE, PDF, LRO, RLO, LRI, RLI, FSI, PDI
    hostile = "x‎‏‪‫‬‭‮⁦⁧⁨⁩y"
    assert prompt.sanitize_text(hostile) == "xy"


def test_sanitize_text_leaves_ordinary_text_untouched() -> None:
    ordinary = "Weekly digest #42 - café, naïve résumé"
    assert prompt.sanitize_text(ordinary) == ordinary


# --- Caps (spec section 5.2) ----------------------------------------------


def test_subject_over_cap_is_truncated_and_flagged() -> None:
    candidate = make_candidate(subject="x" * 250)
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    subject = built.payload.fields["subject"]
    assert isinstance(subject, str)
    assert len(subject) == prompt.SUBJECT_CAP
    assert built.truncated is True


def test_subject_under_cap_is_not_truncated() -> None:
    candidate = make_candidate(subject="short subject")
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    assert built.payload.fields["subject"] == "short subject"
    assert built.truncated is False


def test_display_name_over_cap_is_truncated() -> None:
    candidate = make_candidate(from_display="y" * 150)
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    from_field = built.payload.fields["from"]
    assert isinstance(from_field, dict)
    assert len(from_field["display_name"]) == prompt.DISPLAY_NAME_CAP
    assert built.truncated is True


def test_address_over_cap_is_truncated() -> None:
    candidate = make_candidate(from_address="a" * 250 + "@example.com")
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    from_field = built.payload.fields["from"]
    assert isinstance(from_field, dict)
    assert len(from_field["address"]) == prompt.ADDRESS_CAP
    assert built.truncated is True


def test_list_id_over_cap_is_truncated() -> None:
    candidate = make_candidate(list_id="l" * 250)
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    list_id = built.payload.fields["list_id"]
    assert isinstance(list_id, str)
    assert len(list_id) == prompt.LIST_ID_CAP
    assert built.truncated is True


def test_recipients_over_cap_overflow_into_cc_count() -> None:
    recipients = tuple(f"r{i}@example.com" for i in range(8))
    candidate = make_candidate(recipients=recipients, cc_count=1)
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    to_field = built.payload.fields["to"]
    assert isinstance(to_field, list)
    assert len(to_field) == prompt.RECIPIENTS_CAP
    # 8 recipients, cap 5 -> 3 overflow, folded into the existing cc_count.
    assert built.payload.fields["cc_count"] == 1 + 3
    assert built.truncated is True


def test_recipients_under_cap_not_truncated() -> None:
    candidate = make_candidate(recipients=("a@example.com", "b@example.com"))
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    assert built.payload.fields["to"] == ["a@example.com", "b@example.com"]
    assert built.truncated is False


def test_recipient_address_itself_capped() -> None:
    long_addr = "z" * 250 + "@example.com"
    candidate = make_candidate(recipients=(long_addr,))
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    to_field = built.payload.fields["to"]
    assert isinstance(to_field, list)
    assert len(to_field[0]) == prompt.ADDRESS_CAP
    assert built.truncated is True


def test_excerpt_capped_at_max_chars() -> None:
    candidate = make_candidate()
    built = prompt.build_excerpt_payload(
        candidate,
        payload_id="c1",
        offered=(),
        excerpt_text="e" * 5000,
        max_chars=2000,
    )
    excerpt = built.payload.fields["excerpt"]
    assert isinstance(excerpt, str)
    assert len(excerpt) == 2000
    assert built.truncated is True


def test_excerpt_under_cap_not_truncated_and_metadata_fields_preserved() -> None:
    candidate = make_candidate(subject="short")
    built = prompt.build_excerpt_payload(
        candidate,
        payload_id="c1",
        offered=(),
        excerpt_text="short body text",
        max_chars=2000,
    )
    assert built.payload.fields["excerpt"] == "short body text"
    assert built.payload.fields["subject"] == "short"
    assert built.truncated is False


# --- Fixed metadata field set (spec section 5.1) --------------------------


def test_metadata_field_set_is_exactly_fixed() -> None:
    """The field set must be exactly what spec section 5.1 lists -- no
    dates, ages, or flags -- because it is what keeps the section 13
    cache's input_hash stable."""
    candidate = make_candidate()
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    assert set(built.payload.fields.keys()) == {
        "from",
        "to",
        "cc_count",
        "subject",
        "mailbox",
        "list_id",
        "has_list_unsubscribe",
    }
    from_field = built.payload.fields["from"]
    assert isinstance(from_field, dict)
    assert set(from_field.keys()) == {"address", "display_name"}


def test_metadata_field_set_never_carries_dates_ages_or_flags() -> None:
    candidate = make_candidate(flags=frozenset({"\\Seen", "\\Flagged"}))
    built = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    serialised = str(built.payload.fields)
    assert "internaldate" not in serialised
    assert "flags" not in built.payload.fields
    assert "\\Seen" not in serialised
    assert "\\Flagged" not in serialised


def test_excerpt_payload_adds_exactly_one_field_to_the_fixed_set() -> None:
    candidate = make_candidate()
    built = prompt.build_excerpt_payload(
        candidate, payload_id="c1", offered=(), excerpt_text="hi", max_chars=100
    )
    assert set(built.payload.fields.keys()) == {
        "from",
        "to",
        "cc_count",
        "subject",
        "mailbox",
        "list_id",
        "has_list_unsubscribe",
        "excerpt",
    }


# --- Input hash stability (spec section 5.2, section 13) ------------------


def test_input_hash_stable_for_identical_content() -> None:
    c1 = make_candidate(subject="same subject", uid=1)
    c2 = make_candidate(subject="same subject", uid=2)
    built1 = prompt.build_candidate_payload(c1, payload_id="c1", offered=("r1",))
    built2 = prompt.build_candidate_payload(c2, payload_id="c9", offered=("r2", "r3"))
    # payload_id and offered do not participate in the hash: only the
    # capped/sanitised field content, input level, and truncation flag.
    assert built1.input_hash == built2.input_hash


def test_input_hash_changes_when_content_differs() -> None:
    c1 = make_candidate(subject="subject A")
    c2 = make_candidate(subject="subject B")
    built1 = prompt.build_candidate_payload(c1, payload_id="c1", offered=())
    built2 = prompt.build_candidate_payload(c2, payload_id="c1", offered=())
    assert built1.input_hash != built2.input_hash


def test_input_hash_changes_when_input_level_differs() -> None:
    candidate = make_candidate()
    meta = prompt.build_candidate_payload(candidate, payload_id="c1", offered=())
    excerpt = prompt.build_excerpt_payload(
        candidate, payload_id="c1", offered=(), excerpt_text="", max_chars=100
    )
    assert meta.input_hash != excerpt.input_hash


def test_truncation_flag_participates_in_input_hash() -> None:
    """spec section 5.2: 'truncation participates in the input hash.' A
    manually constructed pair of otherwise-identical field sets must hash
    differently purely because of the truncation flag."""
    fields = {"subject": "x"}
    truncated_hash = prompt.compute_input_hash(
        fields, input_level="metadata", truncated=True
    )
    not_truncated_hash = prompt.compute_input_hash(
        fields, input_level="metadata", truncated=False
    )
    assert truncated_hash != not_truncated_hash


def test_truncated_and_untruncated_candidate_with_same_final_field_differ() -> None:
    """A subject that lands exactly on the cap boundary (not truncated)
    must hash differently from a candidate whose *capped* subject is
    identical but which was truncated to get there -- otherwise the
    truncation event is invisible to a cache lookup, which is exactly
    the case section 5.2 requires to be visible."""
    exact = make_candidate(subject="s" * prompt.SUBJECT_CAP)
    over = make_candidate(subject="s" * (prompt.SUBJECT_CAP + 1))
    built_exact = prompt.build_candidate_payload(exact, payload_id="c1", offered=())
    built_over = prompt.build_candidate_payload(over, payload_id="c1", offered=())
    assert built_exact.payload.fields["subject"] == built_over.payload.fields["subject"]
    assert built_exact.truncated is False
    assert built_over.truncated is True
    assert built_exact.input_hash != built_over.input_hash


def test_rule_text_hash_changes_when_description_edited() -> None:
    h1 = prompt.compute_rule_text_hash("Original description.")
    h2 = prompt.compute_rule_text_hash("Original description, edited.")
    assert h1 != h2


def test_rule_text_hash_stable_for_identical_text() -> None:
    assert prompt.compute_rule_text_hash("same text") == prompt.compute_rule_text_hash(
        "same text"
    )


# --- Canonical request shape (spec section 5.3) ----------------------------


def test_build_request_payload_offers_only_per_candidate_rules() -> None:
    """Each candidate's `offered_rules` entry must contain only the rule
    ids offered for *that* candidate, even when other candidates in the
    same batch are offered different rules."""
    rules = [
        OfferedRule(rule_id="r1", description="d1"),
        OfferedRule(rule_id="r2", description="d2"),
        OfferedRule(rule_id="r3", description="d3"),
    ]
    payloads = [
        CandidatePayload(payload_id="c1", fields={}, offered=("r1",)),
        CandidatePayload(payload_id="c2", fields={}, offered=("r2", "r3")),
    ]
    request = prompt.build_request_payload(payloads, rules)
    offered = request["offered_rules"]
    assert isinstance(offered, dict)
    assert {entry["id"] for entry in offered["c1"]} == {"r1"}
    assert {entry["id"] for entry in offered["c2"]} == {"r2", "r3"}


def test_build_request_payload_drops_rule_ids_not_in_rules_list() -> None:
    """A candidate's `offered` tuple naming a rule id absent from the
    `rules` argument (should never happen from evaluate.py, but the
    request builder must not fabricate a description for it) is simply
    omitted from that candidate's offered list."""
    payloads = [CandidatePayload(payload_id="c1", fields={}, offered=("ghost",))]
    request = prompt.build_request_payload(payloads, rules=[])
    offered = request["offered_rules"]
    assert isinstance(offered, dict)
    assert offered["c1"] == []


def test_build_request_payload_includes_candidate_id_field() -> None:
    payloads = [
        CandidatePayload(payload_id="c1", fields={"subject": "hi"}, offered=("r1",))
    ]
    request = prompt.build_request_payload(
        payloads, rules=[OfferedRule(rule_id="r1", description="d")]
    )
    candidates = request["candidates"]
    assert isinstance(candidates, list)
    assert candidates[0]["id"] == "c1"
    assert candidates[0]["subject"] == "hi"


# --- Response parsing (spec section 5.3, section 5.4) ----------------------


def test_parse_valid_response() -> None:
    raw = (
        '{"results": [{"candidate": "c1", '
        '"matches": ["r1"], '
        '"needs_content": false, "reason": "ok"}]}'
    )
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.invalid == ()
    assert parsed.missing == ()
    assert len(parsed.results) == 1
    result = parsed.results[0]
    assert result.payload_id == "c1"
    assert result.matches[0] == "r1"
    assert result.needs_content is False
    assert result.reason == "ok"


def test_parse_empty_matches_means_no_match() -> None:
    raw = '{"results": [{"candidate": "c1", "matches": []}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert len(parsed.results) == 1
    assert parsed.results[0].matches == ()


def test_parse_needs_content_and_reason_are_optional() -> None:
    raw = '{"results": [{"candidate": "c1", "matches": []}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results[0].needs_content is False
    assert parsed.results[0].reason is None


def test_parse_unparseable_json_marks_every_requested_id_invalid() -> None:
    parsed = prompt.parse_classification_response("not json at all {{{", ["c1", "c2"])
    assert parsed.results == ()
    assert set(parsed.invalid) == {"c1", "c2"}
    assert parsed.missing == ()


def test_parse_missing_results_key_marks_every_requested_id_invalid() -> None:
    parsed = prompt.parse_classification_response('{"nope": []}', ["c1", "c2"])
    assert parsed.results == ()
    assert set(parsed.invalid) == {"c1", "c2"}


def test_parse_results_not_a_list_marks_every_requested_id_invalid() -> None:
    parsed = prompt.parse_classification_response('{"results": "oops"}', ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ("c1",)


def test_parse_item_missing_from_response_is_reported_missing() -> None:
    raw = '{"results": [{"candidate": "c1", "matches": []}]}'
    parsed = prompt.parse_classification_response(raw, ["c1", "c2"])
    assert parsed.missing == ("c2",)


def test_parse_item_with_non_string_candidate_id_is_dropped_silently() -> None:
    raw = '{"results": [{"candidate": 42, "matches": []}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ()
    assert parsed.missing == ("c1",)


def test_parse_item_with_matches_not_a_list_is_invalid() -> None:
    raw = '{"results": [{"candidate": "c1", "matches": "nope"}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ("c1",)


def test_parse_item_with_non_string_rule_id_is_invalid() -> None:
    raw = '{"results": [{"candidate": "c1", "matches": [1]}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ("c1",)


def test_parse_item_with_non_string_reason_is_invalid() -> None:
    raw = '{"results": [{"candidate": "c1", "matches": [], "reason": 5}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ("c1",)


def test_parse_partial_batch_one_invalid_rest_valid() -> None:
    raw = (
        '{"results": ['
        '{"candidate": "c1", "matches": []}, '
        '{"candidate": "c2", "matches": "broken"}, '
        '{"candidate": "c3", "matches": []}'
        "]}"
    )
    parsed = prompt.parse_classification_response(raw, ["c1", "c2", "c3"])
    assert {r.payload_id for r in parsed.results} == {"c1", "c3"}
    assert parsed.invalid == ("c2",)
    assert parsed.missing == ()


# --- System prompt content (spec section 5.2) -------------------------------


def test_system_prompt_declares_data_untrusted_and_restricts_vocabulary() -> None:
    assert "untrusted" in prompt.SYSTEM_PROMPT.lower()
    assert "offered" in prompt.SYSTEM_PROMPT.lower()
