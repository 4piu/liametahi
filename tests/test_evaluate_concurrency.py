"""`models.<name>.max_concurrent_requests` (evaluate phase).

Classification is where a first run over a large mailbox spends most of
its wall clock, and the batches are independent, so overlapping the
model calls is the largest speed-up available. What must survive the
overlap is everything the serial path guaranteed: identical database
contents, identical per-candidate verdicts, and every write on the
thread that owns the connection.

The classifier stand-in here answers by payload *content* rather than by
call order (`FakeClassifier` is call-ordered, which is meaningless once
requests overlap), and can be made to complete out of order on purpose
-- the case where "record results in batch order, not completion order"
is the difference between a stable report and a shuffled one.
"""

import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from liametahi import evaluate, state
from liametahi.classifier import (
    CandidatePayload,
    Classification,
    ClassifyOutcome,
    OfferedRule,
)
from liametahi.config import ModelConfig, TaskConfig
from liametahi.domain import Candidate
from tests.conftest import make_candidate

NOW = datetime(2026, 7, 1, tzinfo=UTC)

# Every candidate is old enough for the deterministic half of the rule,
# leaving the `llm` atom as the only unresolved part -- which is what
# sends it to the classifier.
RULE = {
    "id": "newsletters",
    "when": {"llm": "is this a promotional newsletter?"},
    "actions": ["move_to:Archive"],
}


class RecordingClassifier:
    """Answers from a per-subject script, so the response does not depend
    on the order calls arrive in. Records the peak number of calls in
    flight at once, which is what "did concurrency actually happen"
    reduces to."""

    def __init__(
        self,
        *,
        matching_subjects: Sequence[str] = (),
        before_return: dict[str, threading.Event] | None = None,
        gate: threading.Barrier | None = None,
    ) -> None:
        self._matching = set(matching_subjects)
        self._before_return = before_return or {}
        self._gate = gate
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak_in_flight = 0
        self.call_count = 0
        self.threads: set[int] = set()

    def classify(
        self, candidates: Sequence[CandidatePayload], rules: Sequence[OfferedRule]
    ) -> ClassifyOutcome:
        with self._lock:
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
            self.call_count += 1
            self.threads.add(threading.get_ident())
        try:
            if self._gate is not None:
                # Every worker must arrive before any may leave: with
                # too little concurrency this times out rather than
                # quietly passing.
                self._gate.wait(timeout=10)
            results = []
            for payload in candidates:
                subject = _subject_of(payload)
                event = self._before_return.get(subject)
                if event is not None:
                    assert event.wait(timeout=10), f"gate for {subject!r} never set"
                results.append(
                    Classification(
                        payload_id=payload.payload_id,
                        matches=("newsletters",) if subject in self._matching else (),
                        needs_content=False,
                        reason=None,
                    )
                )
            return ClassifyOutcome(
                results=tuple(results),
                invalid=(),
                missing=(),
                structured_output_level="json_schema",
                input_tokens=1,
                output_tokens=1,
                latency_ms=5,
            )
        finally:
            with self._lock:
                self._in_flight -= 1


def _subject_of(payload: CandidatePayload) -> str:
    subject = payload.fields.get("subject")
    assert isinstance(subject, str)
    return subject


def _model_config(**overrides: object) -> ModelConfig:
    base: dict[str, object] = {
        "provider": "openai_compatible",
        "base_url": "http://local",
        "model": "m",
        # One mail per request, so candidate count == batch count and the
        # concurrency assertions below are about whole requests.
        "mails_per_request": 1,
    }
    base.update(overrides)
    return ModelConfig.model_validate(base)


def _candidates(
    conn: sqlite3.Connection, account_id: int, count: int
) -> list[tuple[int, Candidate]]:
    built = []
    for n in range(1, count + 1):
        candidate = make_candidate(
            account_id=account_id,
            uid=n,
            message_id=f"<m{n}@example.com>",
            fingerprint=f"fp-{n:058d}",
            subject=f"subject {n}",
        )
        built.append((state.upsert_candidate(conn, candidate), candidate))
    return built


def _run(
    conn: sqlite3.Connection,
    *,
    account_id: int,
    classifier: RecordingClassifier,
    candidates: list[tuple[int, Candidate]],
    concurrency: int,
) -> evaluate.EvaluateOutcome:
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
    return evaluate.evaluate_candidates(
        conn,
        account_id=account_id,
        run_id=run_id,
        task=TaskConfig.model_validate({"account": "a", "model": "m", "rules": [RULE]}),
        model_config=_model_config(max_concurrent_requests=concurrency),
        model_id="mi",
        classifier=classifier,
        candidates=candidates,
        now=NOW,
        reevaluate=False,
    )


def _open(
    tmp_path: Path, name: str = "state.sqlite3"
) -> tuple[sqlite3.Connection, int]:
    conn = state.open_database(tmp_path / name)
    return conn, state.upsert_account(conn, name="a", host="h", username="u")


def test_requests_actually_overlap(tmp_path: Path) -> None:
    """A barrier no single worker can pass alone: this deadlocks into a
    timeout if the calls are still being made one after another."""
    conn, account_id = _open(tmp_path)
    try:
        candidates = _candidates(conn, account_id, 4)
        gate = threading.Barrier(4)
        classifier = RecordingClassifier(
            matching_subjects=["subject 1", "subject 3"], gate=gate
        )
        outcome = _run(
            conn,
            account_id=account_id,
            classifier=classifier,
            candidates=candidates,
            concurrency=4,
        )

        assert classifier.call_count == 4
        assert classifier.peak_in_flight == 4
        assert [bool(r.matches) for r in outcome.results] == [True, False, True, False]
    finally:
        state.close_database(conn)


def test_default_is_serial(tmp_path: Path) -> None:
    """The default must remain one request at a time: raising it is a
    deliberate choice about someone's provider rate limit, never
    something that happens to a config that did not ask for it."""
    assert _model_config().max_concurrent_requests == 1

    conn, account_id = _open(tmp_path)
    try:
        candidates = _candidates(conn, account_id, 5)
        classifier = RecordingClassifier(matching_subjects=["subject 2"])
        _run(
            conn,
            account_id=account_id,
            classifier=classifier,
            candidates=candidates,
            concurrency=1,
        )

        assert classifier.call_count == 5
        assert classifier.peak_in_flight == 1
        assert classifier.threads == {threading.get_ident()}
    finally:
        state.close_database(conn)


def _dump(conn: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """Everything the evaluate phase writes, with the per-run and
    wall-clock columns dropped -- those legitimately differ between two
    runs of the same input.

    `classifications` is read in *insertion* order rather than sorted:
    the order rows land in is exactly what recording results in batch
    order versus completion order changes, so sorting here would hide
    the thing this dump exists to compare."""
    classifications = conn.execute(
        "SELECT candidate_id, input_level, input_hash, offered_rules, matches, "
        "needs_content, valid, error FROM classifications ORDER BY rowid"
    ).fetchall()
    cache = conn.execute(
        "SELECT fingerprint, rule_id, rule_text_hash, input_hash, model_id, "
        "prompt_version, matched FROM llm_decision_cache "
        "ORDER BY fingerprint, rule_id"
    ).fetchall()
    return {
        "classifications": [tuple(row) for row in classifications],
        "cache": [tuple(row) for row in cache],
    }


def test_out_of_order_completion_still_records_in_batch_order(
    tmp_path: Path,
) -> None:
    """Force the last batch to finish first. The database and the
    verdicts must come out byte-identical to the serial run, so the
    concurrency setting can never change what a run *did*, only how long
    it took."""
    serial_conn, serial_account = _open(tmp_path, "serial.sqlite3")
    try:
        serial = _run(
            serial_conn,
            account_id=serial_account,
            classifier=RecordingClassifier(matching_subjects=["subject 2"]),
            candidates=_candidates(serial_conn, serial_account, 4),
            concurrency=1,
        )
        serial_rows = _dump(serial_conn)
    finally:
        state.close_database(serial_conn)

    concurrent_conn, concurrent_account = _open(tmp_path, "concurrent.sqlite3")
    try:
        # Hold subject 1 until subject 4 has been released, so completion
        # order is the reverse of submission order.
        last_done = threading.Event()

        class Releasing(RecordingClassifier):
            def classify(
                self,
                candidates: Sequence[CandidatePayload],
                rules: Sequence[OfferedRule],
            ) -> ClassifyOutcome:
                subjects = [_subject_of(p) for p in candidates]
                if "subject 4" in subjects:
                    result = super().classify(candidates, rules)
                    last_done.set()
                    return result
                if "subject 1" in subjects:
                    assert last_done.wait(timeout=10), "last batch never completed"
                return super().classify(candidates, rules)

        concurrent = _run(
            concurrent_conn,
            account_id=concurrent_account,
            classifier=Releasing(matching_subjects=["subject 2"]),
            candidates=_candidates(concurrent_conn, concurrent_account, 4),
            concurrency=4,
        )
        concurrent_rows = _dump(concurrent_conn)
    finally:
        state.close_database(concurrent_conn)

    assert [
        (r.candidate_id, r.matches, r.status, r.valid) for r in concurrent.results
    ] == [(r.candidate_id, r.matches, r.status, r.valid) for r in serial.results]
    assert concurrent_rows == serial_rows
    assert concurrent.llm_calls == serial.llm_calls


def test_workers_never_touch_the_database(tmp_path: Path) -> None:
    """`sqlite3` connections default to `check_same_thread=True`, so a
    worker that tried to write would raise `ProgrammingError` rather
    than corrupt anything. This pins that the production path does not
    rely on that backstop firing: no worker thread touches `conn` at
    all."""
    conn, account_id = _open(tmp_path)
    touched: set[int] = set()
    main_thread = threading.get_ident()

    def note_statement(_sql: str) -> None:
        touched.add(threading.get_ident())

    try:
        candidates = _candidates(conn, account_id, 6)
        conn.set_trace_callback(note_statement)
        _run(
            conn,
            account_id=account_id,
            classifier=RecordingClassifier(
                matching_subjects=["subject 1"], gate=threading.Barrier(3)
            ),
            candidates=candidates,
            concurrency=3,
        )
        assert touched, "expected the evaluate phase to write something"
        assert touched == {main_thread}
    finally:
        conn.set_trace_callback(None)
        state.close_database(conn)


def test_a_failing_batch_does_not_take_down_its_neighbours(tmp_path: Path) -> None:
    """One request raising must be recorded as that batch's own
    `invalid_response` and leave the concurrent batches alone -- the
    serial path's behaviour, which the worker's exception handling has
    to preserve rather than propagate out of the pool."""
    conn, account_id = _open(tmp_path)
    try:
        candidates = _candidates(conn, account_id, 4)

        class Exploding(RecordingClassifier):
            def classify(
                self,
                candidates: Sequence[CandidatePayload],
                rules: Sequence[OfferedRule],
            ) -> ClassifyOutcome:
                if any(_subject_of(p) == "subject 3" for p in candidates):
                    raise RuntimeError("provider said no")
                return super().classify(candidates, rules)

        outcome = _run(
            conn,
            account_id=account_id,
            classifier=Exploding(matching_subjects=["subject 1", "subject 4"]),
            candidates=candidates,
            concurrency=4,
        )

        by_id = {r.candidate_id: r for r in outcome.results}
        statuses = [by_id[cid].status for cid, _ in candidates]
        assert statuses == [None, "no_match", "invalid_response", None]
        failed = by_id[candidates[2][0]]
        assert failed.valid is False
        assert failed.error is not None
        assert "provider said no" in failed.error
    finally:
        state.close_database(conn)


@pytest.mark.parametrize("concurrency", [1, 2, 8])
def test_split_and_retry_survives_any_concurrency(
    tmp_path: Path, concurrency: int
) -> None:
    """A wholly-invalid batch splits in half and retries each half once
    (spec section 5.4). Those retries are extra calls made *inside* one
    worker, so the total call count must not shift with the pool size."""
    conn, account_id = _open(tmp_path, f"c{concurrency}.sqlite3")
    try:
        candidates = _candidates(conn, account_id, 4)

        class Unparseable(RecordingClassifier):
            def classify(
                self,
                candidates: Sequence[CandidatePayload],
                rules: Sequence[OfferedRule],
            ) -> ClassifyOutcome:
                super().classify(candidates, rules)
                return ClassifyOutcome(
                    results=(),
                    invalid=tuple(p.payload_id for p in candidates),
                    missing=(),
                    structured_output_level="none",
                    input_tokens=None,
                    output_tokens=None,
                    latency_ms=1,
                )

        classifier = Unparseable()
        outcome = _run(
            conn,
            account_id=account_id,
            classifier=classifier,
            candidates=candidates,
            concurrency=concurrency,
        )

        # mails_per_request=1, so each batch holds one item and cannot be
        # split further: four batches, four calls, no retries.
        assert classifier.call_count == 4
        assert all(r.status == "invalid_response" for r in outcome.results)
        assert all(r.valid is False for r in outcome.results)
    finally:
        state.close_database(conn)
