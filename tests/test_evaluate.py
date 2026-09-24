"""Tests for `liametahi.evaluate`: the safety-critical response
validation boundary.

This validation must live exactly once, in
the caller, never in an adapter, so it "cannot be skipped by adding a new
adapter". These tests exercise that boundary directly against
`FakeClassifier`, which stands in for *any* provider: the two adapter
test files (`test_classifier_openai_compatible.py`,
`test_classifier_anthropic.py`, `test_classifier_jev.py`) each
independently show that a hostile or malformed value survives their
transport layer untouched, and these tests show the one place that
actually rejects it.

This retires the old single-`llm`-atom, yes/no/unsure
vocabulary in favour of named `processor:` atoms answering with a
resolved `value` (validated against a closed, declared vocabulary) plus
an optional `confidence`. Consequently:

- There is no `needs_content`/"unsure" state left at all -- a processor
  either answers (validly or not) or it doesn't, this round.
- `Classification.answers` is a dict keyed by processor name, so "the
  same key repeated" is no longer representable on the wire; the old
  duplicate-rule-id test is gone because there is nothing left to test.
- Batching now groups every candidate in one `classify()` call by an
  identical *processor* set, which is a
  structural invariant rather than an implementation detail -- so the
  old "candidate B was offered a different rule set than candidate A in
  the same batch" scenario cannot arise any more; what still needs
  covering is the ordinary case, an answer for a processor name that was
  not part of *this* call's offered set at all.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liametahi import evaluate, prompt, state
from liametahi.classifier import Classification, ClassifyOutcome
from liametahi.config import Config, TaskConfig
from liametahi.rules import ProcessorAnswer
from tests.conftest import make_candidate
from tests.fakes.fake_classifier import (
    FakeClassifier,
    outcome_malformed,
    outcome_with_answers,
)

NOW = datetime(2026, 7, 1, tzinfo=UTC)


# --- Test harness -----------------------------------------------------


def _proc(instructions: str = "is this a match?") -> dict[str, object]:
    return {"model": "m", "type": "noul", "instructions": instructions}


def _rule_for(name: str, actions: list[str] | None = None) -> dict[str, object]:
    return {
        "when": {"processor": f"{name}.value >= 0.5"},
        "actions": actions or ["move_to:Archive"],
    }


def _config(
    rules_raw: list[dict[str, object]],
    processors: dict[str, dict[str, object]] | None = None,
    **model_overrides: object,
) -> Config:
    model: dict[str, object] = {
        "provider": "openai_compatible",
        "base_url": "http://local",
        "model": "m",
        "mails_per_request": 10,
    }
    model.update(model_overrides)
    return Config.model_validate(
        {
            "version": 1,
            "accounts": {"a": {"host": "h", "username": "u", "password": "p"}},
            "models": {"m": model},
            "processors": processors or {},
            "tasks": {
                "t": {
                    "account": "a",
                    "source_mailboxes": ["INBOX"],
                    "rules": rules_raw,
                }
            },
        }
    )


def _setup(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    conn = state.open_database(tmp_path / "state.sqlite3")
    account_id = state.upsert_account(conn, name="a", host="h", username="u")
    return conn, account_id


def _new_run(conn: sqlite3.Connection, account_id: int) -> str:
    run_id = state.new_run_id()
    state.create_run(
        conn,
        run_id=run_id,
        task="t",
        account_id=account_id,
        model_name="m",
        provider="openai_compatible",
        model_id="mi",
        dry_run=False,
        reevaluate=False,
        fetch_headers=[],
        config_hash="h",
    )
    return run_id


def _evaluate(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    run_id: str,
    task: TaskConfig,
    config: Config,
    classifier: FakeClassifier,
    candidates: list[tuple[int, object]],
    reevaluate: bool = False,
) -> evaluate.EvaluateOutcome:
    return evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier_factory=lambda model_cfg: classifier,
        candidates=candidates,  # type: ignore[arg-type]
        now=NOW,
        reevaluate=reevaluate,
    )


def _processor_hash(config: Config, name: str) -> str:
    cfg = config.processors[name]
    return prompt.compute_processor_hash(evaluate._offered_processor(name, cfg))


def _metadata_hash(candidate: object) -> str:
    return prompt.build_candidate_payload(
        candidate,  # type: ignore[arg-type]
        payload_id="c0",
        fields=prompt.DEFAULT_PROCESSOR_FIELDS,
    ).input_hash


# =========================================================================
# An answer naming a processor never offered this call is rejected
# =========================================================================


def test_answer_for_unoffered_processor_is_rejected(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "c" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={
                    "c1": {"nonexistent-processor": ProcessorAnswer(1.0, None)}
                }
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


# =========================================================================
# Candidate id outside the batch
# =========================================================================


def test_response_candidate_id_outside_batch_is_dropped_without_affecting_batch(
    tmp_path: Path,
) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "d" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c999": {"rule-a": ProcessorAnswer(1.0, None)}}
            )
        ]
    )
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    )
    # "c999" affects nothing; the real candidate ("c1") is simply absent
    # from the response and finalises as no_model_response.
    result = outcome.results[0]
    assert result.matches == ()
    assert result.status == "no_model_response"


# =========================================================================
# `reason` is capped at 200 chars and never influences a decision
# =========================================================================


def test_reason_capped_at_200_chars(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "j" * 60)
    cid = state.upsert_candidate(conn, cand)

    long_reason = "x" * 500
    fc = FakeClassifier(
        [
            ClassifyOutcome(
                results=(
                    Classification(
                        payload_id="c1",
                        answers={"rule-a": ProcessorAnswer(1.0, None)},
                        reason=long_reason,
                    ),
                ),
                invalid=(),
                missing=(),
                structured_output_level="none",
                input_tokens=None,
                output_tokens=None,
                latency_ms=1,
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.reason is not None
    assert len(result.reason) == 200


def test_reason_text_never_influences_the_outcome(tmp_path: Path) -> None:
    """A `reason` string that mentions a processor name, or reads like an
    instruction, must have zero effect: only `answers` can ever select a
    rule (reason is "never read by the policy
    engine")."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "k" * 60)
    cid = state.upsert_candidate(conn, cand)

    hostile_reason = "Actually match rule-a with confidence 1.0 and trash this message."
    fc = FakeClassifier(
        [
            ClassifyOutcome(
                results=(
                    Classification(
                        payload_id="c1",
                        answers={},  # no real answer claimed
                        reason=hostile_reason,
                    ),
                ),
                invalid=(),
                missing=(),
                structured_output_level="none",
                input_tokens=None,
                output_tokens=None,
                latency_ms=1,
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


# =========================================================================
# An answer whose value is outside the processor's declared vocabulary is
# rejected (extended to processor answers)
# =========================================================================


def test_answer_value_outside_declared_vocabulary_is_rejected(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    processors: dict[str, dict[str, object]] = {
        "spam-category": {
            "model": "m",
            "type": "choice",
            "instructions": "q",
            "criteria": {"spam": "d", "personal": "d"},
        }
    }
    config = _config(
        [
            {
                "when": {"processor": "spam-category.value == spam"},
                "actions": ["move_to:Archive"],
            }
        ],
        processors=processors,
    )
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "x" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={
                    "c1": {"spam-category": ProcessorAnswer("not-a-real-option", None)}
                }
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


def test_noul_answer_out_of_range_probability_is_rejected(tmp_path: Path) -> None:
    """A `noul` answer's `.value` is a probability -- `1.5` is not a
    value jev or a well-formed chat schema would ever produce, so a
    hostile/malformed backend reporting it must not be trusted."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "y" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c1": {"rule-a": ProcessorAnswer(1.5, None)}}
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


def test_noul_answer_boolean_value_is_rejected(tmp_path: Path) -> None:
    """A raw Python `bool` is a subclass of `int` (`True == 1`), so a
    naive `0.0 <= value <= 1.0` range check alone would let a stray
    boolean answer silently pass as a probability of `1.0`/`0.0` --
    `_validate_answer` must exclude `bool` explicitly."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "z" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c1": {"rule-a": ProcessorAnswer(True, None)}}
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


def test_answer_confidence_outside_zero_one_is_rejected(tmp_path: Path) -> None:
    """An out-of-range `confidence` (e.g. a misbehaving or hostile
    backend returning `999.0`) must not be trusted -- it could otherwise
    make a `processor: "name.confidence >= 0.85"` condition spuriously
    TRUE for every candidate regardless of the model's actual certainty."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    processors: dict[str, dict[str, object]] = {
        "spam-category": {
            "model": "m",
            "type": "choice",
            "instructions": "q",
            "criteria": {"spam": "d", "personal": "d"},
        }
    }
    config = _config(
        [
            {
                "when": {"processor": "spam-category.value == spam"},
                "actions": ["move_to:Archive"],
            }
        ],
        processors=processors,
    )
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "x" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={
                    "c1": {"spam-category": ProcessorAnswer("spam", 999.0)}
                }
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


# =========================================================================
# Both a match and a non-match are cached, each tagged with which they
# were: a re-run reuses either answer without asking again.
# =========================================================================


def test_accepted_match_is_cached(tmp_path: Path) -> None:
    """Caching the match too (not just the non-match) is what lets a
    message whose remote mutation failed last run retry that mutation on
    the next run instead of being reclassified from scratch."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "n" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c1": {"rule-a": ProcessorAnswer(1.0, None)}}
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.status is None
    cached = state.get_cached_processor_decision(
        conn,
        account_id=account_id,
        fingerprint=cand.fingerprint,
        processor_name="rule-a",
        processor_hash=_processor_hash(config, "rule-a"),
        input_hash=_metadata_hash(cand),
        model_id="m",
        prompt_version=prompt.PROMPT_VERSION,
    )
    assert cached is not None
    assert cached.value == 1.0


def test_matched_and_unselected_rules_are_both_cached_correctly(
    tmp_path: Path,
) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config(
        [_rule_for("matched-proc"), _rule_for("unselected-proc")],
        processors={"matched-proc": _proc(), "unselected-proc": _proc()},
    )
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "o" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={
                    "c1": {
                        "matched-proc": ProcessorAnswer(1.0, None),
                        "unselected-proc": ProcessorAnswer(0.0, None),
                    }
                }
            )
        ]
    )
    _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    )
    matched_cached = state.get_cached_processor_decision(
        conn,
        account_id=account_id,
        fingerprint=cand.fingerprint,
        processor_name="matched-proc",
        processor_hash=_processor_hash(config, "matched-proc"),
        input_hash=_metadata_hash(cand),
        model_id="m",
        prompt_version=prompt.PROMPT_VERSION,
    )
    unselected_cached = state.get_cached_processor_decision(
        conn,
        account_id=account_id,
        fingerprint=cand.fingerprint,
        processor_name="unselected-proc",
        processor_hash=_processor_hash(config, "unselected-proc"),
        input_hash=_metadata_hash(cand),
        model_id="m",
        prompt_version=prompt.PROMPT_VERSION,
    )
    assert matched_cached is not None
    assert matched_cached.value == 1.0
    assert unselected_cached is not None
    assert unselected_cached.value == 0.0


def test_cached_match_is_reused_without_a_model_call(tmp_path: Path) -> None:
    """The core payoff: once a processor's answer is cached, a
    later run with the same processor definition and input skips the
    model entirely and still produces an accepted match -- e.g. because
    the previous run's remote mutation failed and the message is still a
    live candidate."""
    conn, account_id = _setup(tmp_path)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "p" * 60)
    cid = state.upsert_candidate(conn, cand)

    run_1 = _new_run(conn, account_id)
    fc_1 = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c1": {"rule-a": ProcessorAnswer(1.0, None)}}
            )
        ]
    )
    _evaluate(
        conn,
        account_id=account_id,
        run_id=run_1,
        task=task,
        config=config,
        classifier=fc_1,
        candidates=[(cid, cand)],
    )
    assert fc_1.call_count == 1

    run_2 = _new_run(conn, account_id)
    fc_2 = FakeClassifier([])  # any classify() call would raise -- none expected
    result_2 = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_2,
        task=task,
        config=config,
        classifier=fc_2,
        candidates=[(cid, cand)],
    ).results[0]
    assert fc_2.call_count == 0
    assert result_2.status is None
    assert [m.rule_index for m in result_2.matches] == [0]


# =========================================================================
# Deterministic-only resolution never calls the model
# =========================================================================


def test_fully_deterministic_rule_never_calls_classifier(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([{"when": {"older-than": "1d"}, "actions": ["move_to:Archive"]}])
    task = config.tasks["t"]
    cand = make_candidate(
        account_id=account_id,
        uid=1,
        fingerprint="fp" + "p" * 60,
        internaldate=datetime(2020, 1, 1, tzinfo=UTC),
    )
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier([])
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    )
    assert fc.call_count == 0
    assert outcome.results[0].matches == (evaluate.ValidatedMatch(0),)
    assert outcome.results[0].status is None


def test_all_rules_deterministically_false_yields_no_match_without_model_call(
    tmp_path: Path,
) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config(
        [{"when": {"older-than": "1000d"}, "actions": ["move_to:Archive"]}]
    )
    task = config.tasks["t"]
    cand = make_candidate(
        account_id=account_id,
        uid=1,
        fingerprint="fp" + "q" * 60,
        internaldate=NOW,
    )
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier([])
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    )
    assert fc.call_count == 0
    assert outcome.results[0].matches == ()
    assert outcome.results[0].status == "no_match"


# =========================================================================
# Structured-output level surfaces through EvaluateOutcome
# =========================================================================


def test_structured_output_level_surfaces_on_evaluate_outcome(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "r" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={"c1": {"rule-a": ProcessorAnswer(0.0, None)}},
                structured_output_level="json_object",
            )
        ]
    )
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=[(cid, cand)],
    )
    assert outcome.structured_output_level == "json_object"


# =========================================================================
# Batching: chunking by batch_size
# =========================================================================


def test_batch_size_chunks_a_larger_group_into_multiple_calls(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config(
        [_rule_for("rule-a")], processors={"rule-a": _proc()}, mails_per_request=2
    )
    task = config.tasks["t"]
    candidates = []
    for i in range(3):
        cand = make_candidate(
            account_id=account_id, uid=i + 1, fingerprint=f"fp{'s' * 58}{i}"
        )
        cid = state.upsert_candidate(conn, cand)
        candidates.append((cid, cand))

    fc = FakeClassifier(
        [
            outcome_with_answers(
                answers_by_payload={
                    "c1": {"rule-a": ProcessorAnswer(0.0, None)},
                    "c2": {"rule-a": ProcessorAnswer(0.0, None)},
                }
            ),
            outcome_with_answers(
                answers_by_payload={"c1": {"rule-a": ProcessorAnswer(0.0, None)}}
            ),
        ]
    )
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
    )
    assert fc.call_count == 2
    assert len(outcome.results) == 3


# =========================================================================
# Acceptance test 3: invalid response is a no-op; split-and-retry
# applies the valid items on BOTH halves of the split.
# =========================================================================


def test_acceptance_03_single_invalid_item_is_a_no_op_others_apply(
    tmp_path: Path,
) -> None:
    """A response where one item is structurally invalid and nine are
    valid does not require a split -- every item is still validated
    independently."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    candidates = []
    for i in range(10):
        cand = make_candidate(
            account_id=account_id, uid=i + 1, fingerprint=f"fp{'t' * 58}{i:02d}"
        )
        cid = state.upsert_candidate(conn, cand)
        candidates.append((cid, cand))

    answers_by_payload = {
        f"c{i + 1}": {"rule-a": ProcessorAnswer(1.0, None)} for i in range(9)
    }
    answers_by_payload["c10"] = {"rule-a": ProcessorAnswer(0.0, None)}
    valid_outcome = outcome_with_answers(answers_by_payload=answers_by_payload)
    # Make the 10th item structurally invalid instead of a clean
    # no-match, by re-wrapping it as a partially-invalid ClassifyOutcome.
    outcome_with_one_invalid = ClassifyOutcome(
        results=valid_outcome.results[:9],
        invalid=("c10",),
        missing=(),
        structured_output_level=valid_outcome.structured_output_level,
        input_tokens=None,
        output_tokens=None,
        latency_ms=valid_outcome.latency_ms,
    )
    fc = FakeClassifier([outcome_with_one_invalid])
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
    )
    assert fc.call_count == 1  # no split needed: not wholly invalid
    by_id = {r.candidate_id: r for r in outcome.results}
    for i in range(9):
        assert by_id[candidates[i][0]].status is None
        assert by_id[candidates[i][0]].matches[0].rule_index == 0
    assert by_id[candidates[9][0]].status == "invalid_response"
    assert by_id[candidates[9][0]].matches == ()


def test_acceptance_03_wholly_invalid_batch_splits_and_both_halves_apply(
    tmp_path: Path,
) -> None:
    """A wholly unparseable/invalid response
    triggers exactly one split-in-half retry. This asserts BOTH halves
    of the split are correctly finalised -- the left half resolves
    cleanly, and the right half still has one permanently invalid item
    after its own retry, proving the recursion does not silently drop
    or duplicate results from either side."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    candidates = []
    for i in range(10):
        cand = make_candidate(
            account_id=account_id, uid=i + 1, fingerprint=f"fp{'u' * 58}{i:02d}"
        )
        cid = state.upsert_candidate(conn, cand)
        candidates.append((cid, cand))

    # Call 1: the whole batch of 10 comes back wholly invalid.
    initial = outcome_malformed([f"c{i + 1}" for i in range(10)])
    # Call 2 (left half, candidates 0-4 -> c1..c5): all valid.
    left = outcome_with_answers(
        answers_by_payload={
            f"c{i + 1}": {"rule-a": ProcessorAnswer(1.0, None)} for i in range(5)
        }
    )
    # Call 3 (right half, candidates 5-9 -> c1..c5 again, payload ids are
    # batch-local): 4 valid, 1 still invalid after the retry.
    right_valid = outcome_with_answers(
        answers_by_payload={
            f"c{i + 1}": {"rule-a": ProcessorAnswer(0.0, None)} for i in range(4)
        }
    )
    right = ClassifyOutcome(
        results=right_valid.results,
        invalid=("c5",),
        missing=(),
        structured_output_level=right_valid.structured_output_level,
        input_tokens=None,
        output_tokens=None,
        latency_ms=1,
    )
    fc = FakeClassifier([initial, left, right])

    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
    )
    assert fc.call_count == 3  # initial + exactly one split, not more

    by_id = {r.candidate_id: r for r in outcome.results}
    # Left half (candidates 0-4): all 5 matched rule-a.
    for i in range(5):
        result = by_id[candidates[i][0]]
        assert result.status is None
        assert result.matches[0].rule_index == 0
    # Right half (candidates 5-8): valid no-matches.
    for i in range(5, 9):
        result = by_id[candidates[i][0]]
        assert result.matches == ()
        assert result.status == "no_match"
    # Right half's last item (candidate 9): still invalid after retry.
    last = by_id[candidates[9][0]]
    assert last.matches == ()
    assert last.status == "invalid_response"


def test_split_retry_does_not_recurse_a_second_time(tmp_path: Path) -> None:
    """Neither status is retried again within the run: if BOTH halves
    also come back wholly invalid, there must
    be exactly initial + 2 calls, never a further split."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    config = _config([_rule_for("rule-a")], processors={"rule-a": _proc()})
    task = config.tasks["t"]
    candidates = []
    for i in range(4):
        cand = make_candidate(
            account_id=account_id, uid=i + 1, fingerprint=f"fp{'v' * 58}{i:02d}"
        )
        cid = state.upsert_candidate(conn, cand)
        candidates.append((cid, cand))

    fc = FakeClassifier(
        [
            outcome_malformed(["c1", "c2", "c3", "c4"]),
            outcome_malformed(["c1", "c2"]),
            outcome_malformed(["c1", "c2"]),
        ]
    )
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        config=config,
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
    )
    assert fc.call_count == 3
    for _, result in zip(candidates, outcome.results, strict=True):
        assert result.status == "invalid_response"


# Note: acceptance test 4 ("unsure classification with
# content escalation unavailable") no longer has an analog: the
# yes/no/unsure vocabulary it exercised (`needs_content`) does not exist
# in this redesign -- a processor either answers or it doesn't, this
# round, with no separate "I looked and I'm unsure" signal (the
# chat-compiled schema deliberately has no invented field for it).
