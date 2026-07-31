"""Work Unit 3's named acceptance test (spec section 14): 16. Acceptance
tests 3 and 4 live in `tests/test_evaluate.py`, alongside the rest of
the response-validation matrix they are part of; this one is kept
separate because it is specifically about the negative-decision cache
(spec section 13) across multiple simulated runs, which needs its own
small harness of repeated `evaluate_candidates()` calls sharing one
candidate row.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from liametahi import evaluate, state
from liametahi.config import ModelConfig, TaskConfig
from tests.conftest import make_candidate
from tests.fakes.fake_classifier import FakeClassifier, outcome_with_matches

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _model_config() -> ModelConfig:
    return ModelConfig.model_validate(
        {"provider": "openai_compatible", "base_url": "http://local", "model": "m"}
    )


def _task(description: str) -> TaskConfig:
    return TaskConfig.model_validate(
        {
            "account": "a",
            "model": "m",
            "rules": [
                {
                    "id": "stale-updates",
                    "when": {"llm": description},
                    "actions": ["move_to:Archive"],
                }
            ],
        }
    )


def _setup(tmp_path: Path) -> tuple[sqlite3.Connection, int]:
    conn = state.open_database(tmp_path / "state.sqlite3")
    account_id = state.upsert_account(conn, name="a", host="h", username="u")
    return conn, account_id


def _run(conn: sqlite3.Connection, account_id: int) -> str:
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


def test_acceptance_16_cached_non_match_reevaluate_and_edit_semantics(
    tmp_path: Path,
) -> None:
    conn, account_id = _setup(tmp_path)
    task_v1 = _task("Recurring low-value notification.")
    cand = make_candidate(account_id=account_id, uid=1, fingerprint="fp" + "z" * 60)
    cid = state.upsert_candidate(conn, cand)

    # --- Run 1: the model confidently declines the rule (offered but
    # absent from `matches`) -> the negative decision must be cached
    # (spec section 13). ---------------------------------------------
    run_1 = _run(conn, account_id)
    fc_1 = FakeClassifier([outcome_with_matches(matches_by_payload={"c1": []})])
    result_1 = evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_1,
        task=task_v1,
        model_config=_model_config(),
        model_id="mi",
        classifier=fc_1,
        candidates=[(cid, cand)],
        now=NOW,
        reevaluate=False,
    ).results[0]
    assert fc_1.call_count == 1
    assert result_1.status == "no_match"
    assert result_1.input_hash is not None
    rule_text_hash_v1 = evaluate._rule_text_hash(task_v1.rules[0])
    assert (
        state.get_cached_decision(
            conn,
            account_id=account_id,
            fingerprint=cand.fingerprint,
            rule_id="stale-updates",
            rule_text_hash=rule_text_hash_v1,
            input_hash=result_1.input_hash,
        )
        is not None
    )

    # --- Run 2: same task, same rule text, no --reevaluate -> the
    # cached "no" must eliminate the rule WITHOUT a model call. ---------
    run_2 = _run(conn, account_id)
    fc_2 = FakeClassifier([])  # any classify() call would raise -- none expected
    result_2 = evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_2,
        task=task_v1,
        model_config=_model_config(),
        model_id="mi",
        classifier=fc_2,
        candidates=[(cid, cand)],
        now=NOW,
        reevaluate=False,
    ).results[0]
    assert fc_2.call_count == 0
    assert result_2.status == "cached_no_match"
    assert result_2.matches == ()

    # --- `--reevaluate` semantics: even with a valid cache entry for the
    # exact same rule text and input, `--reevaluate` ignores the cache
    # and forces a fresh model call. ------------------------------------
    run_3 = _run(conn, account_id)
    fc_3 = FakeClassifier([outcome_with_matches(matches_by_payload={"c1": []})])
    result_3 = evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_3,
        task=task_v1,
        model_config=_model_config(),
        model_id="mi",
        classifier=fc_3,
        candidates=[(cid, cand)],
        now=NOW,
        reevaluate=True,
    ).results[0]
    assert fc_3.call_count == 1
    assert result_3.status == "no_match"  # a fresh decision, not a cache hit

    # --- Editing the rule's `llm` text changes `rule_text_hash`, which
    # invalidates the old cache entry: the next run resends the
    # candidate even WITHOUT --reevaluate. -------------------------------
    task_v2 = _task("Recurring low-value notification -- edited wording.")
    rule_text_hash_v2 = evaluate._rule_text_hash(task_v2.rules[0])
    assert rule_text_hash_v2 != rule_text_hash_v1

    run_4 = _run(conn, account_id)
    fc_4 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["stale-updates"]})]
    )
    result_4 = evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_4,
        task=task_v2,
        model_config=_model_config(),
        model_id="mi",
        classifier=fc_4,
        candidates=[(cid, cand)],
        now=NOW,
        reevaluate=False,
    ).results[0]
    assert fc_4.call_count == 1
    assert result_4.status is None  # accepted match, no longer a no-op

    # --- A match is never cached (spec section 13): the accepted match
    # from run 4 must not have created a negative cache row for the
    # edited rule text/input hash. ---------------------------------------
    assert result_4.input_hash is not None
    assert (
        state.get_cached_decision(
            conn,
            account_id=account_id,
            fingerprint=cand.fingerprint,
            rule_id="stale-updates",
            rule_text_hash=rule_text_hash_v2,
            input_hash=result_4.input_hash,
        )
        is None
    )

    # --- Run 5: because run 4's match was never cached, and the model
    # matched rather than declined, a subsequent run against the same
    # (now-matched) rule text must still ask the model again -- there is
    # no negative entry to short-circuit it. -----------------------------
    run_5 = _run(conn, account_id)
    fc_5 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["stale-updates"]})]
    )
    evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_5,
        task=task_v2,
        model_config=_model_config(),
        model_id="mi",
        classifier=fc_5,
        candidates=[(cid, cand)],
        now=NOW,
        reevaluate=False,
    )
    assert fc_5.call_count == 1
