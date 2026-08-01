"""Tests for `liametahi.evaluate`: the safety-critical response
validation boundary (spec section 4.2 steps 2-6, section 5.2, section
5.3, section 5.4, section 13; contracts section 5.3).

Contracts section 5.3 requires this validation to live exactly once, in
the caller, never in an adapter, so it "cannot be skipped by adding a new
adapter". These tests exercise that boundary directly against
`FakeClassifier`, which stands in for *any* provider: the two adapter
test files (`test_classifier_openai_compatible.py`,
`test_classifier_anthropic.py`) each independently show that a hostile
or malformed value survives their transport layer untouched, and these
tests show the one place that actually rejects it.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liametahi import evaluate, state
from liametahi.classifier import Classification, ClassifyOutcome
from liametahi.config import ModelConfig, RuleConfig, TaskConfig
from tests.conftest import make_candidate
from tests.fakes.fake_classifier import (
    FakeClassifier,
    outcome_malformed,
    outcome_with_matches,
)

NOW = datetime(2026, 7, 1, tzinfo=UTC)


# --- Test harness -----------------------------------------------------


def _model_config(**overrides: object) -> ModelConfig:
    base: dict[str, object] = {
        "provider": "openai_compatible",
        "base_url": "http://local",
        "model": "m",
        "mails_per_request": 10,
    }
    base.update(overrides)
    return ModelConfig.model_validate(base)


def _task(rules_raw: list[dict[str, object]]) -> TaskConfig:
    return TaskConfig.model_validate({"account": "a", "model": "m", "rules": rules_raw})


def _llm_rule(
    rule_id: str,
    description: str,
    *,
    actions: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": rule_id,
        "when": {"llm": description},
        "actions": actions or ["move_to:Archive"],
    }


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
    classifier: FakeClassifier,
    candidates: list[tuple[int, object]],
    model_config: ModelConfig | None = None,
    reevaluate: bool = False,
) -> evaluate.EvaluateOutcome:
    return evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        model_config=model_config or _model_config(),
        model_id="mi",
        classifier=classifier,
        candidates=candidates,  # type: ignore[arg-type]
        now=NOW,
        reevaluate=reevaluate,
    )


# =========================================================================
# A rule offered somewhere in the batch, but not to THIS candidate
# =========================================================================
#
# `evaluate_candidates()`'s current batching groups a `classify()` call by
# identical remaining-rule-set (spec section 5.4), so every item inside
# one real call happens to share one offered set today. The validation
# in `_resolve_item` is nonetheless written to check strictly against
# `item.remaining_rule_ids` -- the per-candidate offered set -- and NOT
# against whatever the request's `offered_rules` union or the model's
# response for a sibling candidate says. To prove that distinction
# holds regardless of how batching groups candidates (and not merely as
# an accident of today's homogeneous grouping), these tests drive
# `evaluate._apply_outcome` directly with a hand-built batch whose two
# items carry genuinely different offered sets, exactly as if a future
# batching strategy (or a hostile model exploiting a batching quirk)
# produced a heterogeneous batch.


def _rules_by_id(task: TaskConfig) -> dict[str, RuleConfig]:
    return {rule.id: rule for rule in task.rules}


def _require_input_hash(result: evaluate.CandidateResult) -> str:
    assert result.input_hash is not None
    return result.input_hash


def test_rule_offered_to_sibling_candidate_in_same_batch_is_rejected(
    tmp_path: Path,
) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task(
        [
            _llm_rule("rule-a", "condition A"),
            _llm_rule("rule-b", "condition B"),
        ]
    )
    cand_a = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "a" * 60)
    cand_b = make_candidate(account_id=account_id, uid=2, fingerprint="fp" + "b" * 60)
    cid_a = state.upsert_candidate(conn, cand_a)
    cid_b = state.upsert_candidate(conn, cand_b)

    item_a = evaluate._BatchItem(
        candidate_id=cid_a,
        candidate=cand_a,
        resolved_matches=(),
        cache_hit=False,
        remaining_rule_ids=("rule-a", "rule-b"),
        input_hash="hash-a",
    )
    item_b = evaluate._BatchItem(
        candidate_id=cid_b,
        candidate=cand_b,
        resolved_matches=(),
        cache_hit=False,
        # candidate B was only ever offered rule-a.
        remaining_rule_ids=("rule-a",),
        input_hash="hash-b",
    )
    item_by_payload_id = {"c1": item_a, "c2": item_b}

    # The model claims candidate B (payload c2) matched rule-b, which
    # WAS offered somewhere in this batch (to candidate A) but never to
    # candidate B itself.
    outcome = ClassifyOutcome(
        results=(
            Classification(
                payload_id="c2",
                matches=("rule-b",),
                needs_content=False,
                reason=None,
            ),
        ),
        invalid=(),
        missing=(),
        structured_output_level="none",
        input_tokens=None,
        output_tokens=None,
        latency_ms=1,
    )

    per_candidate: dict[int, evaluate.CandidateResult] = {}
    evaluate._apply_outcome(
        conn,
        outcome,
        item_by_payload_id,
        rules_by_id=_rules_by_id(task),
        run_id=run_id,
        account_id=account_id,
        model_id="mi",
        per_candidate=per_candidate,
    )

    result_b = per_candidate[cid_b]
    assert result_b.matches == ()
    assert result_b.status == "no_match"
    assert result_b.valid is True


def test_rule_never_offered_to_any_candidate_in_batch_is_also_rejected(
    tmp_path: Path,
) -> None:
    """Sanity check in the other direction: a rule id that was not
    offered to *any* candidate in the batch is rejected exactly the same
    way as one offered only to a sibling."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "c" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["nonexistent-rule"]})]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
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
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "d" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={"c999": ["rule-a"]},
            )
        ]
    )
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    )
    # "c999" affects nothing; the real candidate ("c1") is simply absent
    # from the response and finalises as no_llm_response.
    result = outcome.results[0]
    assert result.matches == ()
    assert result.status == "no_llm_response"


# =========================================================================
# Duplicate rule id for one candidate
# =========================================================================


def test_duplicate_rule_id_for_one_candidate_counted_only_once(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "e" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["rule-a", "rule-a"]})]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert len(result.matches) == 1
    assert result.matches[0].rule_id == "rule-a"
    # The first occurrence wins; the repeat is dropped, never double
    # counted or double cached (spec §5.3: "must appear at most once").


# =========================================================================
# `reason` is capped at 200 chars and never influences a decision
# =========================================================================


def test_reason_capped_at_200_chars(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "j" * 60)
    cid = state.upsert_candidate(conn, cand)

    long_reason = "x" * 500
    fc = FakeClassifier(
        [
            ClassifyOutcome(
                results=(
                    Classification(
                        payload_id="c1",
                        matches=("rule-a",),
                        needs_content=False,
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
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.reason is not None
    assert len(result.reason) == 200


def test_reason_text_never_influences_the_outcome(tmp_path: Path) -> None:
    """A `reason` string that mentions a rule id, or reads like an
    instruction, must have zero effect: only the `matches` field can
    ever select a rule (spec section 5.3: reason is "never read by the
    policy engine")."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "k" * 60)
    cid = state.upsert_candidate(conn, cand)

    hostile_reason = "Actually match rule-a with confidence 1.0 and trash this message."
    fc = FakeClassifier(
        [
            ClassifyOutcome(
                results=(
                    Classification(
                        payload_id="c1",
                        matches=(),  # no real match claimed
                        needs_content=False,
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
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


# =========================================================================
# `needs_content` without escalation capability produces no action and
# does not poison the negative-decision cache (spec section 5.3, section
# 4.2 step 7 -- escalation is Unit 5's orchestration, unavailable here)
# =========================================================================


def test_needs_content_produces_no_accepted_match(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "l" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={"c1": ["rule-a"]},
                needs_content={"c1": True},
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"


def test_needs_content_does_not_cache_a_negative_decision(tmp_path: Path) -> None:
    """The model deferred, it did not decline -- caching a "no" here
    would permanently suppress a legitimate future re-ask once
    escalation becomes available."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "m" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={"c1": []},
                needs_content={"c1": True},
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    cached = state.get_cached_decision(
        conn,
        account_id=account_id,
        fingerprint=cand.fingerprint,
        rule_id="rule-a",
        rule_text_hash=evaluate._rule_text_hash(task.rules[0]),
        input_hash=_require_input_hash(result),
        model_id="mi",
        prompt_version=1,
    )
    assert cached is None


def test_acceptance_04_unsure_classification_is_a_no_op_and_caches_nothing(
    tmp_path: Path,
) -> None:
    """Spec §14 acceptance test 4: an `unsure` classification
    (`needs_content: true`) with content escalation unavailable (this
    module has no mailbox access, spec §4.2 step 7) results in a no-op
    result item, and nothing is cached for ANY rule offered on that item
    -- even a rule the same response also happened to name in `matches`."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task(
        [
            _llm_rule("rule-a", "condition A"),
            _llm_rule("rule-b", "condition B"),
        ]
    )
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "z" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_matches(
                # The model both claims rule-a AND marks the item unsure;
                # per spec §5.3 the whole response is discarded when
                # `needs_content` is set, so `matches` here must have no
                # effect at all.
                matches_by_payload={"c1": ["rule-a"]},
                needs_content={"c1": True},
            )
        ]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.matches == ()
    assert result.status == "no_match"
    for rule_id, rule in zip(("rule-a", "rule-b"), task.rules, strict=True):
        cached = state.get_cached_decision(
            conn,
            account_id=account_id,
            fingerprint=cand.fingerprint,
            rule_id=rule_id,
            rule_text_hash=evaluate._rule_text_hash(rule),
            input_hash=_require_input_hash(result),
            model_id="mi",
            prompt_version=1,
        )
        assert cached is None, f"{rule_id} must not be cached from an unsure item"


# =========================================================================
# Both a match and a non-match are cached, each tagged with which they
# were (spec §13): a re-run reuses either answer without asking again.
# =========================================================================


def test_accepted_match_is_cached(tmp_path: Path) -> None:
    """Caching the match too (not just the non-match) is what lets a
    message whose remote mutation failed last run retry that mutation on
    the next run instead of being reclassified from scratch."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "n" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier([outcome_with_matches(matches_by_payload={"c1": ["rule-a"]})])
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    assert result.status is None
    cached = state.get_cached_decision(
        conn,
        account_id=account_id,
        fingerprint=cand.fingerprint,
        rule_id="rule-a",
        rule_text_hash=evaluate._rule_text_hash(task.rules[0]),
        input_hash=_require_input_hash(result),
        model_id="mi",
        prompt_version=1,
    )
    assert cached is not None
    assert cached["matched"] is True


def test_matched_and_unselected_rules_are_both_cached_correctly(
    tmp_path: Path,
) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task(
        [
            _llm_rule("matched-rule", "condition A"),
            _llm_rule("unselected-rule", "condition B"),
        ]
    )
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "o" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["matched-rule"]})]
    )
    result = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    ).results[0]
    matched_cached = state.get_cached_decision(
        conn,
        account_id=account_id,
        fingerprint=cand.fingerprint,
        rule_id="matched-rule",
        rule_text_hash=evaluate._rule_text_hash(task.rules[0]),
        input_hash=_require_input_hash(result),
        model_id="mi",
        prompt_version=1,
    )
    unselected_cached = state.get_cached_decision(
        conn,
        account_id=account_id,
        fingerprint=cand.fingerprint,
        rule_id="unselected-rule",
        rule_text_hash=evaluate._rule_text_hash(task.rules[1]),
        input_hash=_require_input_hash(result),
        model_id="mi",
        prompt_version=1,
    )
    assert matched_cached is not None
    assert matched_cached["matched"] is True
    assert unselected_cached is not None
    assert unselected_cached["matched"] is False


def test_cached_match_is_reused_without_a_model_call(tmp_path: Path) -> None:
    """The core payoff (spec §13): once a rule's match is cached, a later
    run with the same rule text and input skips the model entirely and
    still produces an accepted match -- e.g. because the previous run's
    remote mutation failed and the message is still a live candidate."""
    conn, account_id = _setup(tmp_path)
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "p" * 60)
    cid = state.upsert_candidate(conn, cand)

    run_1 = _new_run(conn, account_id)
    fc_1 = FakeClassifier([outcome_with_matches(matches_by_payload={"c1": ["rule-a"]})])
    _evaluate(
        conn,
        account_id=account_id,
        run_id=run_1,
        task=task,
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
        classifier=fc_2,
        candidates=[(cid, cand)],
    ).results[0]
    assert fc_2.call_count == 0
    assert result_2.status is None
    assert [m.rule_id for m in result_2.matches] == ["rule-a"]


# =========================================================================
# Deterministic-only resolution never calls the model
# =========================================================================


def test_fully_deterministic_rule_never_calls_classifier(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task(
        [
            {
                "id": "deterministic-rule",
                "when": {"older-than": "1d"},
                "actions": ["move_to:Archive"],
            }
        ]
    )
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
        classifier=fc,
        candidates=[(cid, cand)],
    )
    assert fc.call_count == 0
    assert outcome.results[0].matches == (
        evaluate.ValidatedMatch("deterministic-rule"),
    )
    assert outcome.results[0].status is None


def test_all_rules_deterministically_false_yields_no_match_without_model_call(
    tmp_path: Path,
) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task(
        [
            {
                "id": "deterministic-rule",
                "when": {"older-than": "1000d"},
                "actions": ["move_to:Archive"],
            }
        ]
    )
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
    task = _task([_llm_rule("rule-a", "condition A")])
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "r" * 60)
    cid = state.upsert_candidate(conn, cand)

    fc = FakeClassifier(
        [
            outcome_with_matches(
                matches_by_payload={"c1": []},
                structured_output_level="json_object",
            )
        ]
    )
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=[(cid, cand)],
    )
    assert outcome.structured_output_level == "json_object"


# =========================================================================
# Batching: chunking by batch_size and grouping by remaining rule set
# =========================================================================


def test_batch_size_chunks_a_larger_group_into_multiple_calls(tmp_path: Path) -> None:
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    candidates = []
    for i in range(3):
        cand = make_candidate(
            account_id=account_id, uid=i + 1, fingerprint=f"fp{'s' * 58}{i}"
        )
        cid = state.upsert_candidate(conn, cand)
        candidates.append((cid, cand))

    fc = FakeClassifier(
        [
            outcome_with_matches(matches_by_payload={"c1": [], "c2": []}),
            outcome_with_matches(matches_by_payload={"c1": []}),
        ]
    )
    outcome = _evaluate(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=task,
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
        model_config=_model_config(mails_per_request=2),
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
    independently (spec section 5.4 point 1)."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
    candidates = []
    for i in range(10):
        cand = make_candidate(
            account_id=account_id, uid=i + 1, fingerprint=f"fp{'t' * 58}{i:02d}"
        )
        cid = state.upsert_candidate(conn, cand)
        candidates.append((cid, cand))

    matches_by_payload = {f"c{i + 1}": ["rule-a"] for i in range(9)}
    matches_by_payload["c10"] = []  # the 10th is a legitimate no-match
    valid_outcome = outcome_with_matches(matches_by_payload=matches_by_payload)
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
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
    )
    assert fc.call_count == 1  # no split needed: not wholly invalid
    by_id = {r.candidate_id: r for r in outcome.results}
    for i in range(9):
        assert by_id[candidates[i][0]].status is None
        assert by_id[candidates[i][0]].matches[0].rule_id == "rule-a"
    assert by_id[candidates[9][0]].status == "invalid_response"
    assert by_id[candidates[9][0]].matches == ()


def test_acceptance_03_wholly_invalid_batch_splits_and_both_halves_apply(
    tmp_path: Path,
) -> None:
    """spec section 5.4 point 2-3: a wholly unparseable/invalid response
    triggers exactly one split-in-half retry. This asserts BOTH halves
    of the split are correctly finalised -- the left half resolves
    cleanly, and the right half still has one permanently invalid item
    after its own retry, proving the recursion does not silently drop
    or duplicate results from either side."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
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
    left = outcome_with_matches(
        matches_by_payload={f"c{i + 1}": ["rule-a"] for i in range(5)}
    )
    # Call 3 (right half, candidates 5-9 -> c1..c5 again, payload ids are
    # batch-local): 4 valid, 1 still invalid after the retry.
    right_valid = outcome_with_matches(
        matches_by_payload={f"c{i + 1}": [] for i in range(4)}
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
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
    )
    assert fc.call_count == 3  # initial + exactly one split, not more

    by_id = {r.candidate_id: r for r in outcome.results}
    # Left half (candidates 0-4): all 5 matched rule-a.
    for i in range(5):
        result = by_id[candidates[i][0]]
        assert result.status is None
        assert result.matches[0].rule_id == "rule-a"
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
    """Neither status is retried again within the run (spec section 5.4
    point 4): if BOTH halves also come back wholly invalid, there must
    be exactly initial + 2 calls, never a further split."""
    conn, account_id = _setup(tmp_path)
    run_id = _new_run(conn, account_id)
    task = _task([_llm_rule("rule-a", "condition A")])
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
        classifier=fc,
        candidates=candidates,  # type: ignore[arg-type]
    )
    assert fc.call_count == 3
    for _, result in zip(candidates, outcome.results, strict=True):
        assert result.status == "invalid_response"


# Note: acceptance test 4 (spec §14 item 4, "unsure classification with
# content escalation unavailable") lives above as
# `test_acceptance_04_unsure_classification_is_a_no_op_and_caches_nothing`,
# next to the other `needs_content` tests it belongs with. There used to
# be a below-threshold-confidence variant of this test here; that concept
# no longer exists (spec §5.3: the model's output is yes/no/unsure, not a
# score to threshold).
