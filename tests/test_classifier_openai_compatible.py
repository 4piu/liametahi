"""Tests for `liametahi.classifier.openai_compatible` (spec section 8.1;
contracts section 5.3).

All tests use `httpx.MockTransport`: no network, no Docker. These tests
deliberately do *not* exercise semantic validation (unoffered rule id,
duplicate) -- that boundary lives in `liametahi.evaluate`, never in an
adapter (contracts section 5.3), and is covered by
`tests/test_evaluate.py` instead.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from liametahi.classifier import CandidatePayload, OfferedRule
from liametahi.classifier.openai_compatible import (
    OpenAICompatibleClassifier,
    TransportError,
)
from liametahi.config import ModelConfig

CANDIDATE = CandidatePayload(payload_id="c1", fields={"subject": "hi"}, offered=("r1",))
RULES = [OfferedRule(rule_id="r1", description="d1")]


def _config(**overrides: object) -> ModelConfig:
    base: dict[str, object] = {
        "provider": "openai_compatible",
        "base_url": "http://local",
        "model": "m",
    }
    base.update(overrides)
    return ModelConfig.model_validate(base)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local")


def _ok_body(results: list[dict[str, object]]) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": json.dumps({"results": results})}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    }


# --- Construction guardrails ------------------------------------------------


def test_wrong_provider_rejected() -> None:
    cfg = ModelConfig.model_validate(
        {"provider": "anthropic", "model": "m", "api_key": "x"}
    )
    with pytest.raises(ValueError, match="openai_compatible"):
        OpenAICompatibleClassifier(cfg)


# --- Request shape (spec section 8.1) ---------------------------------------


def test_sends_model_messages_max_tokens_and_temperature_zero() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    clf.classify([CANDIDATE], RULES)

    body = captured[0]
    assert body["model"] == "m"
    assert body["temperature"] == 0
    assert isinstance(body["max_tokens"], int)
    messages = body["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    user_payload = json.loads(messages[1]["content"])
    assert user_payload["candidates"][0]["id"] == "c1"
    assert user_payload["offered_rules"]["c1"][0]["id"] == "r1"


def test_extra_headers_and_authorization_applied_to_internally_built_client() -> None:
    """When no `client` is injected (the production path), the adapter
    must build one carrying the configured `Authorization` bearer token
    and any `extra_headers` (spec section 6)."""
    cfg = _config(api_key="secret-key", extra_headers={"X-Title": "liametahi"})
    clf = OpenAICompatibleClassifier(cfg)
    headers = clf._client.headers  # noqa: SLF001 - inspecting the built client only
    assert headers["authorization"] == "Bearer secret-key"
    assert headers["x-title"] == "liametahi"


def test_no_authorization_header_when_no_api_key() -> None:
    cfg = _config(api_key=None)
    clf = OpenAICompatibleClassifier(cfg)
    headers = clf._client.headers  # noqa: SLF001
    assert "authorization" not in headers


# --- Structured-output degradation ladder (spec section 8.1) ---------------


def test_auto_tries_json_schema_first_and_succeeds() -> None:
    formats: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rf = body.get("response_format")
        formats.append(rf["type"] if rf else None)
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], RULES)
    assert outcome.structured_output_level == "json_schema"
    assert formats == ["json_schema"]


def test_auto_degrades_to_json_object_when_schema_rejected() -> None:
    formats: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rf = body.get("response_format")
        formats.append(rf["type"] if rf else None)
        if rf and rf["type"] == "json_schema":
            return httpx.Response(400, text="schema unsupported")
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], RULES)
    assert outcome.structured_output_level == "json_object"
    assert formats == ["json_schema", "json_object"]


def test_auto_degrades_all_the_way_to_prompt_only() -> None:
    formats: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rf = body.get("response_format")
        formats.append(rf["type"] if rf else None)
        if rf is not None:
            return httpx.Response(400, text="unsupported")
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], RULES)
    assert outcome.structured_output_level == "none"
    assert formats == ["json_schema", "json_object", None]


def test_fixed_structured_output_level_never_falls_back() -> None:
    """`structured_output: json_object` (not `auto`) must never silently
    try `json_schema` or degrade further -- only the configured level is
    attempted."""
    formats: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rf = body.get("response_format")
        formats.append(rf["type"] if rf else None)
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(
        _config(structured_output="json_object"), client=_client(handler)
    )
    clf.classify([CANDIDATE], RULES)
    assert formats == ["json_object"]


def test_structured_output_none_sends_no_response_format() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(
        _config(structured_output="none"), client=_client(handler)
    )
    clf.classify([CANDIDATE], RULES)
    assert "response_format" not in bodies[0]


# --- Transport-level retry (spec section 6: "transport errors only") ------


def test_transport_error_retried_up_to_max_retries_then_raises() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("boom", request=request)

    clf = OpenAICompatibleClassifier(
        _config(max_retries=2, structured_output="json_object"),
        client=_client(handler),
    )
    with pytest.raises(TransportError):
        clf.classify([CANDIDATE], RULES)
    # One structured-output level (fixed, not auto) x (max_retries + 1).
    assert attempts == 3


def test_transport_error_succeeds_after_one_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(
        _config(max_retries=2, structured_output="json_object"),
        client=_client(handler),
    )
    outcome = clf.classify([CANDIDATE], RULES)
    assert attempts == 2
    assert outcome.structured_output_level == "json_object"


def test_http_error_response_is_not_retried_at_transport_level() -> None:
    """An HTTP error response (the server answered) is a structured-
    output rejection signal to degrade, never a transport retry target
    -- spec section 6: 'transport errors only, never a rejected
    response.'"""
    attempts_per_level: dict[str | None, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        rf = body.get("response_format")
        level = rf["type"] if rf else None
        attempts_per_level[level] = attempts_per_level.get(level, 0) + 1
        return httpx.Response(400, text="rejected")

    clf = OpenAICompatibleClassifier(_config(max_retries=2), client=_client(handler))
    with pytest.raises(TransportError):
        clf.classify([CANDIDATE], RULES)
    # Exactly one attempt per structured-output level, not (max_retries+1).
    assert attempts_per_level == {"json_schema": 1, "json_object": 1, None: 1}


# --- Response body extraction ------------------------------------------------


def test_usage_extracted_when_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body([]))

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], RULES)
    assert outcome.input_tokens == 11
    assert outcome.output_tokens == 4


def test_usage_absent_yields_none_token_counts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"results": []}'}}]},
        )

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], RULES)
    assert outcome.input_tokens is None
    assert outcome.output_tokens is None


def test_malformed_choices_shape_yields_empty_content_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], RULES)
    # Empty/malformed content parses as wholly invalid -- the caller
    # (evaluate.py) is responsible for the split-and-retry, not this
    # adapter, so classify() itself must not raise.
    assert outcome.results == ()
    assert outcome.invalid == ("c1",)


def test_response_content_is_passed_through_to_structural_parser_unvalidated() -> None:
    """The adapter must not perform semantic validation itself -- an
    unoffered rule id must survive intact into the returned
    `ClassifyOutcome`, because rejecting it is `evaluate.py`'s job, not
    the adapter's (contracts section 5.3)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {
                        "candidate": "c1",
                        "matches": ["never-offered-rule"],
                    }
                ]
            ),
        )

    clf = OpenAICompatibleClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], RULES)
    assert len(outcome.results) == 1
    match = outcome.results[0].matches[0]
    assert match == "never-offered-rule"
