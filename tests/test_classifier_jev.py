"""Tests for `liametahi.classifier.jev` (jev-provider-plan §1, §5, §10,
§11).

All tests use `httpx.MockTransport`: no network, no Docker.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from liametahi.classifier import CandidatePayload, OfferedProcessor
from liametahi.classifier.jev import JevClassifier
from liametahi.config import ConfigError, ModelConfig

CANDIDATE = CandidatePayload(payload_id="c1", fields={"subject": "hi"})
SPAM_PROCESSOR = OfferedProcessor(
    name="spam-category", type="choice", options={"spam": "d1", "personal": "d2"}
)


def _config(**overrides: object) -> ModelConfig:
    base: dict[str, object] = {
        "provider": "jev",
        "base_url": "http://local/v1/systemone",
        "model": "jev-latest",
        "api_key": "secret",
        "mails_per_request": 1,
    }
    base.update(overrides)
    return ModelConfig.model_validate(base)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local")


def test_wrong_provider_rejected() -> None:
    cfg = ModelConfig.model_validate(
        {"provider": "anthropic", "model": "m", "api_key": "x"}
    )
    with pytest.raises(ValueError, match="jev"):
        JevClassifier(cfg)


def test_authorization_header_is_bearer_api_key() -> None:
    clf = JevClassifier(_config())
    headers = clf._client.headers  # noqa: SLF001
    assert headers["authorization"] == "Bearer secret"


def test_one_http_call_per_candidate_per_processor() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"value": "spam", "confidence": 0.92})

    clf = JevClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert calls == 1
    assert len(outcome.results) == 1
    answer = outcome.results[0].answers["spam-category"]
    assert answer.value == "spam"
    assert answer.confidence == 0.92


def test_confidence_and_value_pass_through_untouched_for_each_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "noul":
            return httpx.Response(200, json={"value": True, "confidence": 0.5})
        if body["type"] == "score":
            return httpx.Response(200, json={"value": "high", "confidence": 0.7})
        return httpx.Response(200, json={"value": "spam", "confidence": 0.9})

    processors = [
        OfferedProcessor(
            name="vibe", type="noul", criteria={"true": "x", "false": "y"}
        ),
        OfferedProcessor(name="urgency", type="score", levels=("low", "high")),
        SPAM_PROCESSOR,
    ]
    clf = JevClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], processors)
    answers = outcome.results[0].answers
    assert answers["vibe"].value is True
    assert answers["vibe"].confidence == 0.5
    assert answers["urgency"].value == "high"
    assert answers["urgency"].confidence == 0.7
    assert answers["spam-category"].value == "spam"


def test_question_included_in_request_body_when_set() -> None:
    """jev-provider-plan §2's own worked examples set `question:` on a
    `choice`/`score` processor alongside `options`/`levels` -- it must
    reach the wire, not be silently dropped."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"value": "spam", "confidence": 0.9})

    processor = OfferedProcessor(
        name="spam-category",
        type="choice",
        question="What kind of mail is this?",
        options={"spam": "d1", "personal": "d2"},
    )
    clf = JevClassifier(_config(), client=_client(handler))
    clf.classify([CANDIDATE], [processor])
    assert captured["question"] == "What kind of mail is this?"


def test_question_omitted_from_request_body_when_unset() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"value": "spam", "confidence": 0.9})

    clf = JevClassifier(_config(), client=_client(handler))
    clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert "question" not in captured


def test_mails_per_request_must_be_one() -> None:
    """Enforced at config load (`config.py`'s provider-requirements
    check) -- `JevClassifier.__init__`'s own check is a defensive
    backstop that can only be reached by constructing a `ModelConfig`
    without going through `Config.model_validate`."""
    with pytest.raises(ConfigError, match="mails_per_request"):
        _config(mails_per_request=10)


# --- Retry policy (module docstring's deliberate deviation) ----------------


def test_429_is_retried_up_to_max_retries_then_succeeds() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"value": "spam", "confidence": 0.5})

    clf = JevClassifier(_config(max_retries=3), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 3
    assert len(outcome.results) == 1


def test_529_is_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(529, text="overloaded")
        return httpx.Response(200, json={"value": "spam", "confidence": 0.5})

    clf = JevClassifier(_config(max_retries=2), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 2
    assert len(outcome.results) == 1


def test_429_exhausting_retries_leaves_candidate_invalid() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, text="slow down")

    clf = JevClassifier(_config(max_retries=2), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 3  # max_retries + 1
    assert outcome.results == ()
    assert outcome.invalid == ("c1",)


@pytest.mark.parametrize("status", [401, 422, 500])
def test_non_retryable_status_fails_immediately(status: int) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, text="nope")

    clf = JevClassifier(_config(max_retries=3), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 1
    assert outcome.invalid == ("c1",)


# --- Partial success across several processors -----------------------------


def test_one_processor_failing_does_not_void_the_others_for_the_candidate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["type"] == "score":
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json={"value": "spam", "confidence": 0.9})

    processors = [
        SPAM_PROCESSOR,
        OfferedProcessor(name="urgency", type="score", levels=("low", "high")),
    ]
    clf = JevClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], processors)
    assert len(outcome.results) == 1
    answers = outcome.results[0].answers
    assert "spam-category" in answers
    assert "urgency" not in answers


def test_all_processors_failing_marks_candidate_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    clf = JevClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert outcome.results == ()
    assert outcome.invalid == ("c1",)


def test_malformed_response_body_is_treated_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": "no value field"})

    clf = JevClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert outcome.invalid == ("c1",)


def test_transport_error_is_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"value": "spam", "confidence": 0.5})

    clf = JevClassifier(_config(max_retries=1), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 2
    assert len(outcome.results) == 1
