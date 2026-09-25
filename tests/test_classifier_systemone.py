"""Tests for `liametahi.classifier.systemone`.

All tests use `httpx.MockTransport`: no network, no Docker.
"""

import json
from collections.abc import Callable

import httpx
import pytest

from liametahi.classifier import CandidatePayload, OfferedProcessor
from liametahi.classifier.systemone import SystemOneClassifier
from liametahi.config import ConfigError, ModelConfig

CANDIDATE = CandidatePayload(payload_id="c1", fields={"subject": "hi"})
SPAM_PROCESSOR = OfferedProcessor(
    name="spam-category",
    type="choice",
    instructions="what kind of mail is this?",
    criteria={"spam": "d1", "personal": "d2"},
)


def _config(**overrides: object) -> ModelConfig:
    base: dict[str, object] = {
        "provider": "systemone",
        "base_url": "http://local/v1/systemone",
        "model": "jev-latest",
        "api_key": "secret",
        "mails_per_request": 1,
    }
    base.update(overrides)
    return ModelConfig.model_validate(base)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local")


def _answers_response(
    answers: dict[str, dict[str, object]], status: int = 200
) -> httpx.Response:
    return httpx.Response(status, json={"answers": answers})


def test_wrong_provider_rejected() -> None:
    cfg = ModelConfig.model_validate(
        {"provider": "anthropic", "model": "m", "api_key": "x"}
    )
    with pytest.raises(ValueError, match="systemone"):
        SystemOneClassifier(cfg)


def test_authorization_header_is_bearer_api_key() -> None:
    clf = SystemOneClassifier(_config())
    headers = clf._client.headers  # noqa: SLF001
    assert headers["authorization"] == "Bearer secret"


def test_one_http_call_per_candidate_bundling_every_processor() -> None:
    """One HTTP call per candidate, not one per processor: every offered
    processor is bundled into that one call's `questions` map."""
    calls = 0
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        captured.update(json.loads(request.content))
        return _answers_response(
            {
                "spam-category": {"choice": "spam", "confidence": 0.92},
                "urgency": {"score": 1.0, "legend": {"0": "low", "1": "high"}},
            }
        )

    urgency = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="how urgent?",
        criteria=["low", "high"],
    )
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR, urgency])
    assert calls == 1
    questions = captured["questions"]
    assert isinstance(questions, dict)
    assert set(questions) == {"spam-category", "urgency"}
    assert len(outcome.results) == 1
    answers = outcome.results[0].answers
    assert answers["spam-category"].value == "spam"
    assert answers["spam-category"].confidence == 0.92
    assert answers["urgency"].value == "high"


def test_request_uses_state_not_input() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _answers_response({"spam-category": {"choice": "spam"}})

    clf = SystemOneClassifier(_config(), client=_client(handler))
    clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert captured["state"] == {"subject": "hi"}
    assert "input" not in captured


def test_noul_value_is_raw_probability_no_confidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _answers_response({"vibe": {"noul": 0.73, "confidence": 0.99}})

    processor = OfferedProcessor(name="vibe", type="noul", instructions="is this junk?")
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [processor])
    answer = outcome.results[0].answers["vibe"]
    assert answer.value == 0.73
    # A stray 'confidence' key in the noul answer sub-object is ignored:
    # a systemone model has no confidence concept for noul, and this adapter never
    # reports one for it regardless of what a hostile/malformed response
    # includes.
    assert answer.confidence is None


@pytest.mark.parametrize("probability", [0.0, 0.5, 1.0])
def test_noul_boundary_values_pass_through(probability: float) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _answers_response({"vibe": {"noul": probability}})

    processor = OfferedProcessor(name="vibe", type="noul", instructions="junk?")
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [processor])
    assert outcome.results[0].answers["vibe"].value == probability


def test_score_rounds_to_nearest_declared_level() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _answers_response({"urgency": {"score": 1.6, "confidence": 0.8}})

    processor = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="how urgent?",
        criteria=["low", "medium", "high"],
    )
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [processor])
    answer = outcome.results[0].answers["urgency"]
    assert answer.value == "high"
    assert answer.confidence == 0.8


def test_score_rounds_at_exact_boundary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _answers_response({"urgency": {"score": 0.5}})

    processor = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="how urgent?",
        criteria=["low", "medium", "high"],
    )
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [processor])
    # Python's round() uses banker's rounding: 0.5 rounds to 0 (even).
    assert outcome.results[0].answers["urgency"].value == "low"


def test_score_out_of_range_position_is_clamped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _answers_response({"urgency": {"score": 99.0}})

    processor = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="how urgent?",
        criteria=["low", "medium", "high"],
    )
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [processor])
    assert outcome.results[0].answers["urgency"].value == "high"


def test_criteria_included_in_request_body_when_set() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _answers_response({"spam-category": {"choice": "spam"}})

    clf = SystemOneClassifier(_config(), client=_client(handler))
    clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    questions = captured["questions"]
    assert isinstance(questions, dict)
    assert questions["spam-category"] == {
        "type": "choice",
        "instructions": "what kind of mail is this?",
        "criteria": {"spam": "d1", "personal": "d2"},
    }


def test_criteria_omitted_from_request_body_when_unset() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _answers_response({"vibe": {"noul": 0.5}})

    processor = OfferedProcessor(name="vibe", type="noul", instructions="junk?")
    clf = SystemOneClassifier(_config(), client=_client(handler))
    clf.classify([CANDIDATE], [processor])
    questions = captured["questions"]
    assert isinstance(questions, dict)
    assert "criteria" not in questions["vibe"]


def test_mails_per_request_must_be_one() -> None:
    """Enforced at config load (`config.py`'s provider-requirements
    check) -- `SystemOneClassifier.__init__`'s own check is a defensive
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
        return _answers_response({"spam-category": {"choice": "spam"}})

    clf = SystemOneClassifier(_config(max_retries=3), client=_client(handler))
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
        return _answers_response({"spam-category": {"choice": "spam"}})

    clf = SystemOneClassifier(_config(max_retries=2), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 2
    assert len(outcome.results) == 1


def test_429_exhausting_retries_leaves_candidate_invalid() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, text="slow down")

    clf = SystemOneClassifier(_config(max_retries=2), client=_client(handler))
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

    clf = SystemOneClassifier(_config(max_retries=3), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 1
    assert outcome.invalid == ("c1",)


# --- Partial success across several processors bundled into one call ------


def test_one_processor_malformed_in_response_does_not_void_the_others() -> None:
    """Both processors are asked in the same HTTP call; only one comes
    back in a structurally valid shape for its declared type."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _answers_response(
            {
                "spam-category": {"choice": "spam", "confidence": 0.9},
                "urgency": {"oops": "not a score field"},
            }
        )

    urgency = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="how urgent?",
        criteria=["low", "high"],
    )
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR, urgency])
    assert len(outcome.results) == 1
    answers = outcome.results[0].answers
    assert "spam-category" in answers
    assert "urgency" not in answers


def test_processor_absent_from_answers_map_does_not_void_the_others() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _answers_response({"spam-category": {"choice": "spam"}})

    urgency = OfferedProcessor(
        name="urgency",
        type="score",
        instructions="how urgent?",
        criteria=["low", "high"],
    )
    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR, urgency])
    assert len(outcome.results) == 1
    answers = outcome.results[0].answers
    assert "spam-category" in answers
    assert "urgency" not in answers


def test_all_processors_failing_marks_candidate_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert outcome.results == ()
    assert outcome.invalid == ("c1",)


def test_malformed_response_body_is_treated_as_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": "no answers map"})

    clf = SystemOneClassifier(_config(), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert outcome.invalid == ("c1",)


def test_transport_error_is_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("boom", request=request)
        return _answers_response({"spam-category": {"choice": "spam"}})

    clf = SystemOneClassifier(_config(max_retries=1), client=_client(handler))
    outcome = clf.classify([CANDIDATE], [SPAM_PROCESSOR])
    assert attempts == 2
    assert len(outcome.results) == 1
