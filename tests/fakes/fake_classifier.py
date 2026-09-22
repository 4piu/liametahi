"""`FakeClassifier`: a scripted stand-in for `classifier.Classifier`
(contracts §5.3, §6.3; jev-provider-plan §5, §10).

`OfferedProcessor`, `CandidatePayload`, `Classification`, `ClassifyOutcome`,
and `Classifier` are imported from `liametahi.classifier`, which is the
fixed cross-unit interface (contracts §5.3). `Classifier` is a
`Protocol`, so `FakeClassifier` keeps satisfying it structurally without
any change to the class below.

Per contracts §5.3: "validation against the offered [processor] and
candidate vocabulary happens in the caller, not the adapter."
`FakeClassifier` therefore happily returns semantically-invalid
`Classification`s (an unoffered processor name, an unknown candidate id,
an answer value outside a processor's declared vocabulary) inside
`results` when scripted to — that is exactly what lets a downstream
validation layer's tests exercise its rejection logic. Only
transport-level failure (unparseable/wholly invalid response) belongs in
`invalid`/`missing`.

Per jev-provider-plan: a processor's answer is a resolved `value` (bool
for `noul`, a declared option/level string for `choice`/`score`) plus an
optional `confidence` -- there is no yes/no/unsure vocabulary left.
"""

from collections.abc import Mapping, Sequence

from liametahi.classifier import (
    CandidatePayload,
    Classification,
    Classifier,
    ClassifyOutcome,
    OfferedProcessor,
)
from liametahi.rules import ProcessorAnswer

__all__ = [
    "CandidatePayload",
    "Classification",
    "Classifier",
    "ClassifyOutcome",
    "FakeClassifier",
    "OfferedProcessor",
    "ProcessorAnswer",
    "outcome_malformed",
    "outcome_missing",
    "outcome_with_answers",
    "outcome_with_unknown_candidate",
    "outcome_with_unknown_processor",
]


# --- The fake itself ----------------------------------------------------


class FakeClassifier:
    """Returns pre-scripted `ClassifyOutcome`s (or raises a scripted
    exception) in call order, and records every call's arguments for
    assertions (e.g. "was this candidate ever sent to the model")."""

    def __init__(self, scripted: Sequence[ClassifyOutcome | Exception] = ()) -> None:
        self._scripted: list[ClassifyOutcome | Exception] = list(scripted)
        self._calls: list[
            tuple[tuple[CandidatePayload, ...], tuple[OfferedProcessor, ...]]
        ] = []

    def queue(self, outcome: ClassifyOutcome | Exception) -> None:
        self._scripted.append(outcome)

    @property
    def calls(
        self,
    ) -> tuple[tuple[tuple[CandidatePayload, ...], tuple[OfferedProcessor, ...]], ...]:
        return tuple(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def classify(
        self,
        candidates: Sequence[CandidatePayload],
        processors: Sequence[OfferedProcessor],
    ) -> ClassifyOutcome:
        self._calls.append((tuple(candidates), tuple(processors)))
        if not self._scripted:
            raise AssertionError(
                "FakeClassifier.classify() called with no scripted response queued"
            )
        next_item = self._scripted.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


# --- Scripted-outcome builders for the failure switches named in
# --- contracts §6.3 -------------------------------------------------


def outcome_with_answers(
    *,
    answers_by_payload: Mapping[str, Mapping[str, ProcessorAnswer]],
    structured_output_level: str = "json_schema",
    latency_ms: int = 5,
) -> ClassifyOutcome:
    """A normal, fully valid outcome: every payload id gets a
    `Classification` built from its per-processor answer map."""
    results = tuple(
        Classification(payload_id=payload_id, answers=dict(answers), reason=None)
        for payload_id, answers in answers_by_payload.items()
    )
    return ClassifyOutcome(
        results=results,
        invalid=(),
        missing=(),
        structured_output_level=structured_output_level,
        input_tokens=None,
        output_tokens=None,
        latency_ms=latency_ms,
    )


def outcome_with_unknown_processor(
    payload_id: str, unknown_processor_name: str
) -> ClassifyOutcome:
    """Names a processor that was never offered for this candidate; the
    caller's validation layer must reject it."""
    return outcome_with_answers(
        answers_by_payload={
            payload_id: {unknown_processor_name: ProcessorAnswer(True, None)}
        }
    )


def outcome_with_unknown_candidate(
    unknown_payload_id: str, processor_name: str
) -> ClassifyOutcome:
    """Names a payload id absent from the submitted batch."""
    return outcome_with_answers(
        answers_by_payload={
            unknown_payload_id: {processor_name: ProcessorAnswer(True, None)}
        }
    )


def outcome_malformed(payload_ids: Sequence[str]) -> ClassifyOutcome:
    """The whole response failed to parse (spec §5.4 point 2-3): every
    requested payload id is reported `invalid`."""
    return ClassifyOutcome(
        results=(),
        invalid=tuple(payload_ids),
        missing=(),
        structured_output_level="none",
        input_tokens=None,
        output_tokens=None,
        latency_ms=5,
    )


def outcome_missing(
    present: Mapping[str, Mapping[str, ProcessorAnswer]], missing_ids: Sequence[str]
) -> ClassifyOutcome:
    """Some payload ids got a decision; others are simply absent from an
    otherwise-valid response (spec §5.4 point 3)."""
    base = outcome_with_answers(answers_by_payload=present)
    return ClassifyOutcome(
        results=base.results,
        invalid=(),
        missing=tuple(missing_ids),
        structured_output_level=base.structured_output_level,
        input_tokens=None,
        output_tokens=None,
        latency_ms=base.latency_ms,
    )
