"""Jev (`provider: jev`) adapter.

Jev is not a chat model: a decision model reached through
`POST /v1/systemone`, answering one of three fixed, structured question
shapes (`noul`/`choice`/`score`) with a resolved answer, never generated
text. There is no prompt to compile and no JSON-schema negotiation ladder
(`prompt.build_response_schema` is chat-only, per its own docstring) — a
processor's `type`/`instructions`/`criteria` map onto a jev `Question`
directly, and the response is parsed straight into a `ProcessorAnswer`
with no folding or thresholding of any kind (see `_parse_answer` below
for the one exception: `score` rounds to its nearest declared level).

**Request shape** (confirmed against a live call to
`api.typesafe.ai/v1/systemone`, replacing an earlier, wrong assumption):
`POST` body is
`{"model", "state", "questions": {<processor-name>: <Question>}}`.
`state` carries the candidate's fields (the assumed `input` key was
wrong); `questions` is a *map*, not one embedded question, so one HTTP
call can carry every processor a candidate still needs -- `classify()`
therefore makes exactly one HTTP call per candidate, bundling every
offered processor into that one call's `questions` map, not one call per
processor per candidate. Each `Question` is `{"type", "instructions"}`
plus `criteria` when the processor declares one (optional for `noul`,
required for `choice`/`score` -- see `config.ProcessorConfig`).

**Response shape**: per-type, a `noul` question resolves to
`{"noul": <0-1 probability>}` (no boolean, no confidence -- "the single
`noul` value describes it completely," per jev's own docs); `choice`
resolves to `{"choice": <option>, "probabilities": {...}, "confidence":
<0-1>}`; `score` resolves to `{"score": <continuous position>, "legend":
{"<index>": "<description>"}, "probabilities": {...}, "confidence":
<0-1>}`. This adapter reads `.value`/`.confidence` straight through for
`noul`/`choice`; for `score` it rounds the continuous `score` to the
nearest declared level index and reports *our own* declared level name at
that index for `.value` (jev's own `legend` is not used -- it is built
from the same `criteria` list we submitted, in the same order, so
indexing our own list directly is equivalent and avoids trusting a second
copy of our own vocabulary echoed back). `noul`'s `.confidence` is always
`None`: jev has no separate confidence concept for a `noul` answer, and
`config.py` rejects a `processor: "name.confidence ..."` condition
against any `noul` processor at load time for exactly this reason.

The top-level envelope for a *multi*-question response is
`{"answers": {<processor-name>: <per-type shape above>}}`, mirroring the
request's `questions` map by name. jev's own docs don't pin this shape
explicitly (they only show the per-type answer shape for a single
question); confirmed instead by a live call bundling multiple processors
for one candidate against `api.typesafe.ai/v1/systemone` and inspecting
the real response.

`mails_per_request` must be `1` for `provider: jev` (enforced at config
load, `config.ModelConfig._validate_provider_requirements`): jev answers
one candidate per HTTP call, not a batch of candidates. A processor
answer that fails to parse out of an otherwise well-formed multi-question
response (missing key, wrong JSON type) is simply absent from that
candidate's `answers` map for this round (per
`classifier.Classification`'s own contract: "a name absent from the map
means not answered this round") rather than voiding the whole candidate
-- a candidate with at least one successful answer is still reported in
`results`; a candidate with zero successful answers (including "the
response body itself was not even a `dict` with an `answers` map") goes
to `invalid`.

**Retry policy is a deliberate deviation from `openai_compatible.py`'s
"transport errors only, never a rejected response" policy**:
jev is a single-purpose decision API behind a rate limiter, not a chat
completion request whose rejection typically means the request itself was
malformed. A `429`/`529` here means "the service is temporarily out of
capacity, try again shortly" — exactly the shape `max_retries` already
exists to absorb for transport errors — so this adapter retries those two
status codes up to `max_retries` times with the same backoff-free retry
loop shape `openai_compatible.py` uses for transport errors. Every other
4xx/5xx (`401` unauthorized, `422` malformed request, ...) fails
immediately with no retry: retrying a `401`/`422` can never succeed and
only wastes the retry budget that would otherwise be available for a
genuine `429`/`529`.
"""

import time
from collections.abc import Mapping, Sequence

import httpx

from liametahi.classifier import (
    CandidatePayload,
    Classification,
    ClassifyOutcome,
    OfferedProcessor,
)
from liametahi.config import ModelConfig
from liametahi.logging import get_logger
from liametahi.rules import ProcessorAnswer

logger = get_logger(__name__)

#: Status codes worth retrying (a deliberate deviation from
#: `openai_compatible.py`'s transport-only retry policy, see module
#: docstring): temporary capacity problems, not bad requests.
_RETRYABLE_STATUS_CODES = frozenset({429, 529})


class TransportError(Exception):
    """A jev HTTP call failed after exhausting `max_retries` (for a
    retryable status) or failed immediately on a non-retryable one, or
    its response body was not even structurally a multi-question
    envelope."""


class JevClassifier:
    """`Classifier` implementation for `provider: jev`."""

    def __init__(self, config: ModelConfig, *, client: httpx.Client | None = None):
        if config.provider != "jev":
            raise ValueError(
                f"JevClassifier requires provider='jev', got {config.provider!r}"
            )
        if not config.base_url:
            raise ValueError("jev requires 'base_url'")
        if config.mails_per_request != 1:
            raise ValueError("jev requires mails_per_request == 1")
        self._config = config
        self._endpoint_url = config.base_url
        headers = dict(config.extra_headers)
        headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = client or httpx.Client(
            timeout=config.timeout_seconds, headers=headers
        )

    def classify(
        self,
        candidates: Sequence[CandidatePayload],
        processors: Sequence[OfferedProcessor],
    ) -> ClassifyOutcome:
        results: list[Classification] = []
        invalid: list[str] = []
        total_latency_ms = 0

        for candidate in candidates:
            start = time.monotonic()
            try:
                answers = self._call_one(candidate, processors)
            except TransportError as exc:
                logger.debug(
                    "jev classify: candidate=%s failed: %s",
                    candidate.payload_id,
                    exc,
                )
                answers = {}
            finally:
                total_latency_ms += int((time.monotonic() - start) * 1000)
            if answers:
                results.append(
                    Classification(
                        payload_id=candidate.payload_id, answers=answers, reason=None
                    )
                )
            else:
                invalid.append(candidate.payload_id)

        return ClassifyOutcome(
            results=tuple(results),
            invalid=tuple(invalid),
            missing=(),
            structured_output_level="json_schema",
            input_tokens=None,
            output_tokens=None,
            latency_ms=total_latency_ms,
        )

    def _call_one(
        self, candidate: CandidatePayload, processors: Sequence[OfferedProcessor]
    ) -> dict[str, ProcessorAnswer]:
        body: dict[str, object] = {
            "model": self._config.model,
            "state": dict(candidate.fields),
            "questions": {
                processor.name: _question_json(processor) for processor in processors
            },
        }
        response = self._post_with_retry(body)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise TransportError(
                f"malformed jev response (expected an 'answers' map): "
                f"{response.text[:200]!r}"
            )
        raw_answers = data["answers"]
        answers: dict[str, ProcessorAnswer] = {}
        for processor in processors:
            raw = raw_answers.get(processor.name)
            if not isinstance(raw, dict):
                continue
            answer = _parse_answer(processor, raw)
            if answer is not None:
                answers[processor.name] = answer
        return answers

    def _post_with_retry(self, body: dict[str, object]) -> httpx.Response:
        """POST once, retrying only `429`/`529` up to `max_retries` times
        (module docstring's deliberate deviation from
        `openai_compatible.py`'s transport-errors-only policy). A
        transport-level exception (connection reset, timeout) is also
        retried, matching every other adapter's behaviour for those.
        Every other status code -- including every other 4xx/5xx -- fails
        immediately, no retry."""
        attempts = self._config.max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.post(self._endpoint_url, json=body)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.debug(
                    "jev: transport attempt %d/%d failed: %s", attempt, attempts, exc
                )
                continue
            if response.status_code < 400:
                return response
            if response.status_code not in _RETRYABLE_STATUS_CODES:
                raise TransportError(
                    f"HTTP {response.status_code} (non-retryable): "
                    f"{response.text[:200]}"
                )
            last_exc = TransportError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
            logger.debug(
                "jev: retryable HTTP %d on attempt %d/%d",
                response.status_code,
                attempt,
                attempts,
            )
        assert last_exc is not None
        raise TransportError(str(last_exc)) from last_exc


def _question_json(processor: OfferedProcessor) -> dict[str, object]:
    """Compile one offered processor into jev's `Question` shape -- a
    near-direct passthrough, since `OfferedProcessor`'s own fields already
    match jev's field names and per-type shapes exactly."""
    question: dict[str, object] = {
        "type": processor.type,
        "instructions": processor.instructions,
    }
    if processor.criteria is not None:
        if isinstance(processor.criteria, Mapping):
            question["criteria"] = dict(processor.criteria)
        else:
            question["criteria"] = list(processor.criteria)
    return question


def _parse_confidence(raw: Mapping[str, object]) -> float | None:
    confidence_raw = raw.get("confidence")
    if confidence_raw is None:
        return None
    if not isinstance(confidence_raw, int | float) or isinstance(confidence_raw, bool):
        return None
    return float(confidence_raw)


def _parse_answer(
    processor: OfferedProcessor, raw: Mapping[str, object]
) -> ProcessorAnswer | None:
    """Parse one processor's per-type answer sub-object. `None` means
    "this processor's answer did not structurally match its declared
    type" -- the caller drops it, leaving the processor unanswered this
    round for this candidate rather than voiding every other processor
    bundled into the same call."""
    if processor.type == "noul":
        value = raw.get("noul")
        if not isinstance(value, int | float) or isinstance(value, bool):
            return None
        # No confidence for 'noul', on any backend -- see module
        # docstring: jev has no separate confidence concept for it.
        return ProcessorAnswer(value=float(value), confidence=None)
    if processor.type == "choice":
        value = raw.get("choice")
        if not isinstance(value, str):
            return None
        return ProcessorAnswer(value=value, confidence=_parse_confidence(raw))
    # score
    score = raw.get("score")
    if not isinstance(score, int | float) or isinstance(score, bool):
        return None
    assert isinstance(processor.criteria, Sequence) and not isinstance(
        processor.criteria, str
    )
    levels = list(processor.criteria)
    if not levels:
        return None
    index = max(0, min(len(levels) - 1, round(score)))
    return ProcessorAnswer(value=levels[index], confidence=_parse_confidence(raw))
