"""Tests for `liametahi.prompt`: payload construction, capping,
sanitisation, hashing, and the canonical request/response wire shape.
"""

from typing import Any, cast

from liametahi import prompt
from liametahi.classifier import CandidatePayload, OfferedProcessor
from liametahi.rules import ProcessorAnswer
from tests.conftest import make_candidate


def _nested(obj: object, *keys: str) -> Any:  # noqa: ANN401 - test-only navigation
    current: Any = obj
    for key in keys:
        current = cast("dict[str, object]", current)[key]
    return current


# --- Sanitisation -----------------------------------------------------


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


# --- Caps -------------------------------------------------------------


def test_subject_over_cap_is_truncated_and_flagged() -> None:
    candidate = make_candidate(subject="x" * 250)
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    subject = built.payload.fields["subject"]
    assert isinstance(subject, str)
    assert len(subject) == prompt.SUBJECT_CAP
    assert built.truncated is True


def test_subject_under_cap_is_not_truncated() -> None:
    candidate = make_candidate(subject="short subject")
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    assert built.payload.fields["subject"] == "short subject"
    assert built.truncated is False


def test_display_name_over_cap_is_truncated() -> None:
    candidate = make_candidate(from_display="y" * 150)
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    from_field = built.payload.fields["from"]
    assert isinstance(from_field, dict)
    assert len(from_field["display_name"]) == prompt.DISPLAY_NAME_CAP
    assert built.truncated is True


def test_address_over_cap_is_truncated() -> None:
    candidate = make_candidate(from_address="a" * 250 + "@example.com")
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    from_field = built.payload.fields["from"]
    assert isinstance(from_field, dict)
    assert len(from_field["address"]) == prompt.ADDRESS_CAP
    assert built.truncated is True


def test_list_id_over_cap_is_truncated() -> None:
    candidate = make_candidate(list_id="l" * 250)
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    list_id = built.payload.fields["list_id"]
    assert isinstance(list_id, str)
    assert len(list_id) == prompt.LIST_ID_CAP
    assert built.truncated is True


def test_recipients_over_cap_overflow_into_cc_count() -> None:
    recipients = tuple(f"r{i}@example.com" for i in range(8))
    candidate = make_candidate(recipients=recipients, cc_count=1)
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    to_field = built.payload.fields["to"]
    assert isinstance(to_field, list)
    assert len(to_field) == prompt.RECIPIENTS_CAP
    # 8 recipients, cap 5 -> 3 overflow, folded into the existing cc_count.
    assert built.payload.fields["cc_count"] == 1 + 3
    assert built.truncated is True


def test_recipients_under_cap_not_truncated() -> None:
    candidate = make_candidate(recipients=("a@example.com", "b@example.com"))
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    assert built.payload.fields["to"] == ["a@example.com", "b@example.com"]
    assert built.truncated is False


def test_recipient_address_itself_capped() -> None:
    long_addr = "z" * 250 + "@example.com"
    candidate = make_candidate(recipients=(long_addr,))
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    to_field = built.payload.fields["to"]
    assert isinstance(to_field, list)
    assert len(to_field[0]) == prompt.ADDRESS_CAP
    assert built.truncated is True


def test_excerpt_capped_at_max_chars() -> None:
    candidate = make_candidate()
    built = prompt.build_excerpt_payload(
        candidate, payload_id="c1", excerpt_text="e" * 5000, max_chars=2000
    )
    excerpt = built.payload.fields["excerpt"]
    assert isinstance(excerpt, str)
    assert len(excerpt) == 2000
    assert built.truncated is True


def test_excerpt_under_cap_not_truncated_and_metadata_fields_preserved() -> None:
    candidate = make_candidate(subject="short")
    built = prompt.build_excerpt_payload(
        candidate, payload_id="c1", excerpt_text="short body text", max_chars=2000
    )
    assert built.payload.fields["excerpt"] == "short body text"
    assert built.payload.fields["subject"] == "short"
    assert built.truncated is False


# --- Fixed metadata field set -----------------------------------------


def test_metadata_field_set_is_exactly_fixed() -> None:
    """The field set must be exactly what the specification lists -- no
    dates, ages, or flags -- because it is what keeps the decision
    cache's input_hash stable."""
    candidate = make_candidate()
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
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
    built = prompt.build_candidate_payload(candidate, payload_id="c1")
    serialised = str(built.payload.fields)
    assert "internaldate" not in serialised
    assert "flags" not in built.payload.fields
    assert "\\Seen" not in serialised
    assert "\\Flagged" not in serialised


def test_excerpt_payload_adds_exactly_one_field_to_the_fixed_set() -> None:
    candidate = make_candidate()
    built = prompt.build_excerpt_payload(
        candidate, payload_id="c1", excerpt_text="hi", max_chars=100
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


# --- Input hash stability ---------------------------------------------


def test_input_hash_stable_for_identical_content() -> None:
    c1 = make_candidate(subject="same subject", uid=1)
    c2 = make_candidate(subject="same subject", uid=2)
    built1 = prompt.build_candidate_payload(c1, payload_id="c1")
    built2 = prompt.build_candidate_payload(c2, payload_id="c9")
    # payload_id does not participate in the hash: only the
    # capped/sanitised field content, input level, and truncation flag.
    assert built1.input_hash == built2.input_hash


def test_input_hash_changes_when_content_differs() -> None:
    c1 = make_candidate(subject="subject A")
    c2 = make_candidate(subject="subject B")
    built1 = prompt.build_candidate_payload(c1, payload_id="c1")
    built2 = prompt.build_candidate_payload(c2, payload_id="c1")
    assert built1.input_hash != built2.input_hash


def test_input_hash_changes_when_input_level_differs() -> None:
    candidate = make_candidate()
    meta = prompt.build_candidate_payload(candidate, payload_id="c1")
    excerpt = prompt.build_excerpt_payload(
        candidate, payload_id="c1", excerpt_text="", max_chars=100
    )
    assert meta.input_hash != excerpt.input_hash


def test_truncation_flag_participates_in_input_hash() -> None:
    """'Truncation participates in the input hash.' A
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
    built_exact = prompt.build_candidate_payload(exact, payload_id="c1")
    built_over = prompt.build_candidate_payload(over, payload_id="c1")
    assert built_exact.payload.fields["subject"] == built_over.payload.fields["subject"]
    assert built_exact.truncated is False
    assert built_over.truncated is True
    assert built_exact.input_hash != built_over.input_hash


# --- Processor identity hash ------------------------------------------------


def _choice_processor(name: str = "spam-category") -> OfferedProcessor:
    return OfferedProcessor(
        name=name,
        type="choice",
        instructions="what kind of mail is this?",
        criteria={"spam": "unsolicited", "personal": "legit"},
    )


def test_processor_hash_changes_when_criteria_edited() -> None:
    h1 = prompt.compute_processor_hash(_choice_processor())
    edited = OfferedProcessor(
        name="spam-category",
        type="choice",
        instructions="what kind of mail is this?",
        criteria={"spam": "unsolicited", "personal": "legit", "digest": "newsletter"},
    )
    h2 = prompt.compute_processor_hash(edited)
    assert h1 != h2


def test_processor_hash_stable_for_identical_definition() -> None:
    assert prompt.compute_processor_hash(
        _choice_processor()
    ) == prompt.compute_processor_hash(_choice_processor())


def test_processor_hash_changes_when_instructions_edited() -> None:
    a = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="How urgent?",
        criteria=("low", "high"),
    )
    b = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="How urgent is this really?",
        criteria=("low", "high"),
    )
    assert prompt.compute_processor_hash(a) != prompt.compute_processor_hash(b)


def test_processor_hash_ignores_name() -> None:
    """The hash is over the processor's own declared shape, not its
    config key -- renaming a processor without changing its definition
    should not by itself invalidate the cache (name is part of the
    surrounding cache key, not the definition hash)."""
    a = _choice_processor(name="spam-category")
    b = _choice_processor(name="renamed")
    assert prompt.compute_processor_hash(a) == prompt.compute_processor_hash(b)


# --- Canonical request shape -------------------------------------------


def test_build_request_payload_includes_every_offered_processor() -> None:
    processors = [
        OfferedProcessor(
            name="spam-category",
            type="choice",
            instructions="q",
            criteria={"spam": "d"},
        ),
        OfferedProcessor(
            name="urgency", type="score", instructions="q", criteria=("low", "high")
        ),
    ]
    payloads = [CandidatePayload(payload_id="c1", fields={})]
    request = prompt.build_request_payload(payloads, processors)
    offered = request["processors"]
    assert isinstance(offered, dict)
    assert set(offered.keys()) == {"spam-category", "urgency"}
    assert offered["spam-category"]["type"] == "choice"
    assert offered["urgency"]["criteria"] == ["low", "high"]


def test_build_request_payload_includes_candidate_id_field() -> None:
    payloads = [CandidatePayload(payload_id="c1", fields={"subject": "hi"})]
    request = prompt.build_request_payload(payloads, processors=[])
    candidates = request["candidates"]
    assert isinstance(candidates, list)
    assert candidates[0]["id"] == "c1"
    assert candidates[0]["subject"] == "hi"


def test_build_request_payload_includes_instructions_for_choice_and_score() -> None:
    """`instructions` is required for every processor type now -- a chat
    request must actually carry it through to the model."""
    processors = [
        OfferedProcessor(
            name="spam-category",
            type="choice",
            instructions="What kind of mail is this?",
            criteria={"spam": "d"},
        ),
        OfferedProcessor(
            name="urgency",
            type="score",
            instructions="How urgent is this message?",
            criteria=("low", "high"),
        ),
    ]
    payloads = [CandidatePayload(payload_id="c1", fields={})]
    request = prompt.build_request_payload(payloads, processors)
    offered = request["processors"]
    assert isinstance(offered, dict)
    assert offered["spam-category"]["instructions"] == "What kind of mail is this?"
    assert offered["urgency"]["instructions"] == "How urgent is this message?"


def test_build_request_payload_omits_criteria_when_unset() -> None:
    processors = [
        OfferedProcessor(name="vibe-check", type="noul", instructions="junk?")
    ]
    payloads = [CandidatePayload(payload_id="c1", fields={})]
    request = prompt.build_request_payload(payloads, processors)
    offered = request["processors"]
    assert isinstance(offered, dict)
    assert "criteria" not in offered["vibe-check"]


# --- Response schema --------------------------------------------------------


def test_response_schema_contains_exactly_declared_fields_noul() -> None:
    """A `noul` processor wants a calibrated probability now, not a
    boolean -- its compiled schema is a bounded number, matching jev's
    own `noul` answer shape rather than inventing a boolean derivation."""
    processor = OfferedProcessor(
        name="vibe-check",
        type="noul",
        instructions="q",
        criteria={"true": "x", "false": "y"},
    )
    schema = prompt.build_response_schema([processor])
    answer_props = _nested(
        schema,
        "properties",
        "results",
        "items",
        "properties",
        "answers",
        "properties",
    )
    value_schema = answer_props["vibe-check"]["properties"]["value"]
    assert value_schema == {"type": "number", "minimum": 0, "maximum": 1}
    # No auto-injected confidence field anywhere in the compiled schema,
    # and no margin-probability fields either -- `noul`'s value already
    # is the resolved probability.
    props = answer_props["vibe-check"]["properties"]
    assert "confidence" not in props
    assert "value_probability" not in props
    assert "runner_up_probability" not in props
    assert answer_props["vibe-check"]["required"] == ["value"]


def test_response_schema_choice_enumerates_option_keys() -> None:
    processor = OfferedProcessor(
        name="spam-category",
        type="choice",
        instructions="q",
        criteria={"spam": "d1", "personal": "d2"},
    )
    schema = prompt.build_response_schema([processor])
    answer_props = _nested(
        schema,
        "properties",
        "results",
        "items",
        "properties",
        "answers",
        "properties",
    )
    value_schema = answer_props["spam-category"]["properties"]["value"]
    assert value_schema["type"] == "string"
    assert set(value_schema["enum"]) == {"spam", "personal"}
    props = answer_props["spam-category"]["properties"]
    assert props["value_probability"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert props["runner_up_probability"] == {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
    }
    assert set(answer_props["spam-category"]["required"]) == {
        "value",
        "value_probability",
        "runner_up_probability",
    }


def test_response_schema_score_enumerates_levels() -> None:
    processor = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="q",
        criteria=("low", "medium", "high"),
    )
    schema = prompt.build_response_schema([processor])
    answer_props = _nested(
        schema,
        "properties",
        "results",
        "items",
        "properties",
        "answers",
        "properties",
    )
    value_schema = answer_props["urgency"]["properties"]["value"]
    assert value_schema["enum"] == ["low", "medium", "high"]
    assert set(answer_props["urgency"]["required"]) == {
        "value",
        "value_probability",
        "runner_up_probability",
    }


# --- Response parsing --------------------------------------------------


def test_parse_valid_response() -> None:
    raw = (
        '{"results": [{"candidate": "c1", '
        '"answers": {"spam-category": {"value": "spam", '
        '"value_probability": 0.9, "runner_up_probability": 0.05}}, '
        '"reason": "ok"}]}'
    )
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.invalid == ()
    assert parsed.missing == ()
    assert len(parsed.results) == 1
    result = parsed.results[0]
    assert result.payload_id == "c1"
    assert result.answers["spam-category"] == ProcessorAnswer(
        value="spam", confidence=0.85
    )
    assert result.reason == "ok"


def test_parse_answer_without_confidence_fields_defaults_to_none() -> None:
    raw = (
        '{"results": [{"candidate": "c1", "answers": {"vibe-check": {"value": true}}}]}'
    )
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results[0].answers["vibe-check"] == ProcessorAnswer(
        value=True, confidence=None
    )


def test_parse_answer_with_only_one_of_the_pair_defaults_to_none() -> None:
    """A non-compliant model that reports only one of the two required
    fields gets no confidence, not a crash or a bogus derivation from a
    missing value treated as zero."""
    raw = (
        '{"results": [{"candidate": "c1", "answers": {"urgency": '
        '{"value": "high", "value_probability": 0.9}}}]}'
    )
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results[0].answers["urgency"] == ProcessorAnswer(
        value="high", confidence=None
    )


def test_parse_answer_with_non_numeric_probability_defaults_to_none() -> None:
    raw = (
        '{"results": [{"candidate": "c1", "answers": {"urgency": '
        '{"value": "high", "value_probability": "high", '
        '"runner_up_probability": 0.1}}}]}'
    )
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results[0].answers["urgency"] == ProcessorAnswer(
        value="high", confidence=None
    )


def test_derive_margin_confidence_clips_to_zero_when_runner_up_wins() -> None:
    """A model reporting a higher probability for the runner-up than for
    its own chosen value is internally inconsistent -- clip to 0 rather
    than a negative confidence, which `evaluate.py`'s bounds check would
    reject anyway, but a defensive derivation should not even produce."""
    assert (
        prompt._derive_margin_confidence(
            {"value_probability": 0.3, "runner_up_probability": 0.6}
        )
        == 0.0
    )


def test_derive_margin_confidence_clips_to_one_when_out_of_range() -> None:
    assert (
        prompt._derive_margin_confidence(
            {"value_probability": 5.0, "runner_up_probability": -3.0}
        )
        == 1.0
    )


def test_parse_empty_answers_is_valid() -> None:
    raw = '{"results": [{"candidate": "c1", "answers": {}}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert len(parsed.results) == 1
    assert parsed.results[0].answers == {}


def test_parse_reason_is_optional() -> None:
    raw = '{"results": [{"candidate": "c1", "answers": {}}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
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
    raw = '{"results": [{"candidate": "c1", "answers": {}}]}'
    parsed = prompt.parse_classification_response(raw, ["c1", "c2"])
    assert parsed.missing == ("c2",)


def test_parse_item_with_non_string_candidate_id_is_dropped_silently() -> None:
    raw = '{"results": [{"candidate": 42, "answers": {}}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ()
    assert parsed.missing == ("c1",)


def test_parse_item_with_answers_not_an_object_is_invalid() -> None:
    raw = '{"results": [{"candidate": "c1", "answers": "nope"}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ("c1",)


def test_parse_item_with_answer_missing_value_is_invalid() -> None:
    raw = '{"results": [{"candidate": "c1", "answers": {"p": {}}}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ("c1",)


def test_parse_item_with_non_string_reason_is_invalid() -> None:
    raw = '{"results": [{"candidate": "c1", "answers": {}, "reason": 5}]}'
    parsed = prompt.parse_classification_response(raw, ["c1"])
    assert parsed.results == ()
    assert parsed.invalid == ("c1",)


def test_parse_partial_batch_one_invalid_rest_valid() -> None:
    raw = (
        '{"results": ['
        '{"candidate": "c1", "answers": {}}, '
        '{"candidate": "c2", "answers": "broken"}, '
        '{"candidate": "c3", "answers": {}}'
        "]}"
    )
    parsed = prompt.parse_classification_response(raw, ["c1", "c2", "c3"])
    assert {r.payload_id for r in parsed.results} == {"c1", "c3"}
    assert parsed.invalid == ("c2",)
    assert parsed.missing == ()


# --- System prompt content ---------------------------------------------------


def test_system_prompt_declares_data_untrusted_and_restricts_vocabulary() -> None:
    assert "untrusted" in prompt.SYSTEM_PROMPT.lower()
    assert "offered" in prompt.SYSTEM_PROMPT.lower()
