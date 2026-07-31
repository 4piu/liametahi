"""`FakeClassifier`: a scripted stand-in for `classifier.Classifier`
(contracts §5.3, §6.3).

`OfferedRule`, `CandidatePayload`, `Classification`, `ClassifyOutcome`,
and `Classifier` are imported from `liametahi.classifier`, which is Unit
3's fixed cross-unit interface (contracts §5.3) — this fake used to hold
verbatim local copies because that module did not exist yet when Unit 1
landed; now that it does, this file imports from it per contracts §5.4's
instruction. `Classifier` is a `Protocol`, so `FakeClassifier` keeps
satisfying it structurally without any change to the class below.

Per contracts §5.3: "validation against the offered rule and candidate
vocabulary happens in the caller, not the adapter." `FakeClassifier`
therefore happily returns semantically-invalid `Classification`s (an
unoffered rule id, an unknown candidate id) inside `results` when
scripted to — that is exactly what lets a downstream validation layer's
tests exercise its rejection logic. Only transport-level failure
(unparseable/wholly invalid response) belongs in `invalid`/`missing`.

The model's output is yes/no/unsure, not a score (spec §5.3): a rule id
in `matches` is a confident yes, an offered rule id absent from
`matches` is a confident no, and `needs_content: true` marks the whole
item unsure. There is no confidence anywhere in this fake's API.
"""

from collections.abc import Mapping, Sequence

from liametahi.classifier import (
    CandidatePayload,
    Classification,
    Classifier,
    ClassifyOutcome,
    OfferedRule,
)

__all__ = [
    "CandidatePayload",
    "Classification",
    "Classifier",
    "ClassifyOutcome",
    "FakeClassifier",
    "OfferedRule",
    "outcome_malformed",
    "outcome_missing",
    "outcome_with_matches",
    "outcome_with_unknown_candidate",
    "outcome_with_unknown_rule",
]


# --- The fake itself ----------------------------------------------------


class FakeClassifier:
    """Returns pre-scripted `ClassifyOutcome`s (or raises a scripted
    exception) in call order, and records every call's arguments for
    assertions (e.g. "was this candidate ever sent to the model")."""

    def __init__(self, scripted: Sequence[ClassifyOutcome | Exception] = ()) -> None:
        self._scripted: list[ClassifyOutcome | Exception] = list(scripted)
        self._calls: list[
            tuple[tuple[CandidatePayload, ...], tuple[OfferedRule, ...]]
        ] = []

    def queue(self, outcome: ClassifyOutcome | Exception) -> None:
        self._scripted.append(outcome)

    @property
    def calls(
        self,
    ) -> tuple[tuple[tuple[CandidatePayload, ...], tuple[OfferedRule, ...]], ...]:
        return tuple(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    def classify(
        self, candidates: Sequence[CandidatePayload], rules: Sequence[OfferedRule]
    ) -> ClassifyOutcome:
        self._calls.append((tuple(candidates), tuple(rules)))
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


def outcome_with_matches(
    *,
    matches_by_payload: Mapping[str, Sequence[str]],
    needs_content: Mapping[str, bool] | None = None,
    structured_output_level: str = "json_schema",
    latency_ms: int = 5,
) -> ClassifyOutcome:
    """A normal, fully valid outcome: every payload id gets a
    `Classification` built from its list of matched rule ids."""
    needs_content = needs_content or {}
    results = tuple(
        Classification(
            payload_id=payload_id,
            matches=tuple(matches),
            needs_content=needs_content.get(payload_id, False),
            reason=None,
        )
        for payload_id, matches in matches_by_payload.items()
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


def outcome_with_unknown_rule(payload_id: str, unknown_rule_id: str) -> ClassifyOutcome:
    """Names a rule id that was never offered for this candidate; the
    caller's validation layer must reject it."""
    return outcome_with_matches(matches_by_payload={payload_id: [unknown_rule_id]})


def outcome_with_unknown_candidate(
    unknown_payload_id: str, rule_id: str
) -> ClassifyOutcome:
    """Names a payload id absent from the submitted batch."""
    return outcome_with_matches(matches_by_payload={unknown_payload_id: [rule_id]})


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
    present: Mapping[str, Sequence[str]], missing_ids: Sequence[str]
) -> ClassifyOutcome:
    """Some payload ids got a decision; others are simply absent from an
    otherwise-valid response (spec §5.4 point 3)."""
    base = outcome_with_matches(matches_by_payload=present)
    return ClassifyOutcome(
        results=base.results,
        invalid=(),
        missing=tuple(missing_ids),
        structured_output_level=base.structured_output_level,
        input_tokens=None,
        output_tokens=None,
        latency_ms=base.latency_ms,
    )
