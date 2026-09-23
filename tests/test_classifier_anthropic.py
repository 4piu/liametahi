"""Tests for `liametahi.classifier.anthropic`.

Uses the real `anthropic.Anthropic` client wired to `httpx.MockTransport`
via its `http_client` constructor argument, so no network or Docker is
involved and the SDK's own request/response (de)serialisation is
exercised for real, only the transport is faked.
"""

import json
from collections.abc import Callable

import anthropic
import httpx
import pytest

from liametahi.classifier import CandidatePayload, OfferedProcessor
from liametahi.classifier.anthropic import AnthropicClassifier, TransportError
from liametahi.config import ModelConfig

CANDIDATE = CandidatePayload(payload_id="c1", fields={"subject": "hi"})
PROCESSORS = [
    OfferedProcessor(
        name="spam-category", type="choice", instructions="q", criteria={"spam": "d1"}
    )
]


def _config(**overrides: object) -> ModelConfig:
    base: dict[str, object] = {"provider": "anthropic", "model": "m", "api_key": "k"}
    base.update(overrides)
    return ModelConfig.model_validate(base)


def _anthropic_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key="k",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


def _ok_response(results: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "m",
            "content": [{"type": "text", "text": json.dumps({"results": results})}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 9, "output_tokens": 3},
        },
    )


def _rejected_response() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "x"},
        },
    )


# --- Construction guardrails -------------------------------------------


def test_wrong_provider_rejected() -> None:
    cfg = ModelConfig.model_validate(
        {"provider": "openai_compatible", "base_url": "http://x", "model": "m"}
    )
    with pytest.raises(ValueError, match="anthropic"):
        AnthropicClassifier(cfg)


# --- Request shape --------------------------------------------------------


def test_system_prompt_is_top_level_not_a_message() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok_response([])

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    clf.classify([CANDIDATE], PROCESSORS)

    body = bodies[0]
    assert "system" in body
    assert isinstance(body["system"], str)
    messages = body["messages"]
    assert isinstance(messages, list)
    assert all(m["role"] != "system" for m in messages)


def test_max_tokens_always_sent() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok_response([])

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    clf.classify([CANDIDATE], PROCESSORS)
    assert isinstance(bodies[0]["max_tokens"], int)
    assert bodies[0]["max_tokens"] > 0


def test_temperature_is_never_sent() -> None:
    """Temperature is rejected with a 400 on current
    Anthropic models, so this adapter must never send it, unlike
    `openai_compatible` which may."""
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok_response([])

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    clf.classify([CANDIDATE], PROCESSORS)
    assert "temperature" not in bodies[0]


def test_user_content_carries_the_canonical_request_payload() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok_response([])

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    clf.classify([CANDIDATE], PROCESSORS)
    messages = bodies[0]["messages"]
    assert isinstance(messages, list)
    user_content = json.loads(messages[0]["content"])
    assert user_content["candidates"][0]["id"] == "c1"
    assert user_content["processors"]["spam-category"]["type"] == "choice"


# --- Structured-output degradation ladder ----------------------------------


def test_auto_tries_schema_first_and_succeeds() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok_response([])

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    outcome = clf.classify([CANDIDATE], PROCESSORS)
    assert outcome.structured_output_level == "json_schema"
    assert "output_config" in bodies[0]


def test_auto_degrades_to_unstructured_when_schema_rejected() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "output_config" in body:
            return _rejected_response()
        return _ok_response([])

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    outcome = clf.classify([CANDIDATE], PROCESSORS)
    assert outcome.structured_output_level == "none"
    assert len(bodies) == 2
    assert "output_config" in bodies[0]
    assert "output_config" not in bodies[1]


def test_fixed_json_schema_does_not_fall_back_on_rejection() -> None:
    """Unlike `auto`, a pinned `structured_output: json_schema` must not
    be silently downgraded on rejection -- that would defeat the point
    of pinning it (the ladder is what `auto` means).
    A rejection here is a hard failure, mirroring
    `openai_compatible`'s fixed-level behaviour."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _rejected_response()

    clf = AnthropicClassifier(
        _config(structured_output="json_schema"), client=_anthropic_client(handler)
    )
    with pytest.raises(TransportError):
        clf.classify([CANDIDATE], PROCESSORS)
    assert attempts == 1


def test_structured_output_none_sends_no_output_config() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok_response([])

    clf = AnthropicClassifier(
        _config(structured_output="none"), client=_anthropic_client(handler)
    )
    outcome = clf.classify([CANDIDATE], PROCESSORS)
    assert outcome.structured_output_level == "none"
    assert "output_config" not in bodies[0]


def test_auto_raises_when_both_schema_and_fallback_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _rejected_response()

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    with pytest.raises(TransportError):
        clf.classify([CANDIDATE], PROCESSORS)


def test_connection_error_raises_transport_error_without_retrying_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    with pytest.raises(TransportError):
        clf.classify([CANDIDATE], PROCESSORS)


# --- Response extraction --------------------------------------------------


def test_usage_extracted_from_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response([])

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    outcome = clf.classify([CANDIDATE], PROCESSORS)
    assert outcome.input_tokens == 9
    assert outcome.output_tokens == 3


def test_response_content_passed_through_to_structural_parser_unvalidated() -> None:
    """As with the openai_compatible adapter, semantic validation must
    not happen here -- an unoffered processor name must survive intact
    into the returned `ClassifyOutcome` for `evaluate.py` to reject."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            [
                {
                    "candidate": "c1",
                    "answers": {"never-offered": {"value": "anything"}},
                }
            ]
        )

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    outcome = clf.classify([CANDIDATE], PROCESSORS)
    assert len(outcome.results) == 1
    answer = outcome.results[0].answers["never-offered"]
    assert answer.value == "anything"


def test_empty_content_blocks_yield_wholly_invalid_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        )

    clf = AnthropicClassifier(_config(), client=_anthropic_client(handler))
    outcome = clf.classify([CANDIDATE], PROCESSORS)
    assert outcome.results == ()
    assert outcome.invalid == ("c1",)
