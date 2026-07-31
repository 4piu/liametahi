"""Tests for `tests/fakes/` themselves (contracts §6.3)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.fakes.fake_classifier import (
    CandidatePayload,
    FakeClassifier,
    OfferedRule,
    outcome_malformed,
    outcome_missing,
    outcome_with_matches,
    outcome_with_unknown_candidate,
    outcome_with_unknown_rule,
)
from tests.fakes.fake_mailbox import (
    FakeMailbox,
    MessageVanished,
    UnsupportedCapability,
    _StoredMessage,
)

CORPUS_MANIFEST = Path(__file__).parent / "corpus" / "synthetic" / "manifest.json"


# --- FakeMailbox -----------------------------------------------------


def test_from_corpus_loads_all_synthetic_messages() -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    mailbox.select("INBOX", readonly=True)
    uids = mailbox.search_uids()
    assert len(uids) == 6


def test_fetch_metadata_never_marks_seen() -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    status = mailbox.select("INBOX", readonly=True)
    uids = mailbox.search_uids()
    before = {m.uid: frozenset(m.flags) for m in mailbox._messages["INBOX"]}
    mailbox.fetch_metadata(uids, headers=["FROM", "SUBJECT", "LIST-ID"])
    after = {m.uid: frozenset(m.flags) for m in mailbox._messages["INBOX"]}
    assert before == after
    assert status.exists == 6


def test_fetch_metadata_returns_only_requested_headers_present() -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    mailbox.select("INBOX", readonly=True)
    results = mailbox.fetch_metadata(
        [101], headers=["FROM", "LIST-ID", "X-NOT-PRESENT"]
    )
    assert len(results) == 1
    meta = results[0]
    assert meta.uid == 101
    # Keys are lowercased, values multi-valued -- matches the real
    # imap_adapter.RawMetadata shape exactly (contracts §5.4).
    assert "from" in meta.headers
    assert "list-id" in meta.headers  # list_digest.eml has List-Id
    assert "x-not-present" not in meta.headers
    assert isinstance(meta.headers["from"], tuple)


def test_fetch_raw_returns_full_bytes() -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    mailbox.select("INBOX", readonly=True)
    raw = mailbox.fetch_raw(101)
    assert b"Weekly Digest" in raw or b"digest" in raw.lower()


def test_move_requires_move_capability() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mailbox = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1, raw=b"x", flags=set(), internaldate=now, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1},
        capabilities=frozenset(),  # no MOVE
    )
    mailbox.select("INBOX", readonly=False)
    with pytest.raises(UnsupportedCapability):
        mailbox.move(1, "Trash")


def test_move_relocates_message_with_new_uid() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mailbox = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=5, raw=b"x", flags=set(), internaldate=now, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1, "Trash": 1},
    )
    mailbox.select("INBOX", readonly=False)
    mailbox.move(5, "Trash")
    assert mailbox.search_uids() == ()
    mailbox.select("Trash", readonly=False)
    assert mailbox.search_uids() == (1,)  # first message in a fresh mailbox


def test_add_keyword_requires_custom_keyword_support() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mailbox = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1, raw=b"x", flags=set(), internaldate=now, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1},
        accepts_custom_keywords=False,
    )
    status = mailbox.select("INBOX", readonly=False)
    assert status.accepts_custom_keywords is False
    with pytest.raises(UnsupportedCapability):
        mailbox.add_keyword(1, "Important")


def test_mutations_log_is_empty_for_read_only_calls() -> None:
    """`select`, `search_uids`, `fetch_metadata`, and `fetch_raw` never
    append to `mutations` -- only `move`/`add_keyword`/`append` do
    (contracts §6.2, §6.3)."""
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    mailbox.select("INBOX", readonly=True)
    uids = mailbox.search_uids()
    mailbox.fetch_metadata(uids, headers=["FROM", "SUBJECT"])
    for uid in uids:
        mailbox.fetch_raw(uid)
    assert mailbox.mutations == ()


def test_mutations_log_records_move_add_keyword_and_append() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mailbox = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1, raw=b"x", flags=set(), internaldate=now, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1, "Trash": 1},
    )
    mailbox.select("INBOX", readonly=False)
    mailbox.move(1, "Trash")
    mailbox.select("Trash", readonly=False)
    mailbox.add_keyword(1, "Important")
    mailbox.append("INBOX", b"raw", [], now)
    assert len(mailbox.mutations) == 3
    assert mailbox.mutations[0].startswith("move ")
    assert mailbox.mutations[1].startswith("add_keyword ")
    assert mailbox.mutations[2].startswith("append ")


def test_mutations_log_records_even_a_rejected_call() -> None:
    """A capability check failing does not exempt the attempt from the
    log -- a caller asserting read-only behaviour must see every attempt,
    not just successful ones."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mailbox = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1, raw=b"x", flags=set(), internaldate=now, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1},
        capabilities=frozenset(),
    )
    mailbox.select("INBOX", readonly=False)
    with pytest.raises(UnsupportedCapability):
        mailbox.move(1, "Trash")
    assert len(mailbox.mutations) == 1


def test_uidvalidity_bump_reflected_on_next_select() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    mailbox = FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1, raw=b"x", flags=set(), internaldate=now, mailbox="INBOX"
            )
        ],
        uidvalidity={"INBOX": 1000},
    )
    status1 = mailbox.select("INBOX", readonly=True)
    assert status1.uidvalidity == 1000
    mailbox.bump_uidvalidity("INBOX")
    status2 = mailbox.select("INBOX", readonly=True)
    assert status2.uidvalidity == 1001


def test_vanish_hides_message_from_metadata_and_raw() -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    mailbox.vanish("INBOX", 101)
    mailbox.select("INBOX", readonly=True)
    assert 101 not in mailbox.search_uids()
    assert mailbox.fetch_metadata([101], headers=["FROM"]) == ()
    with pytest.raises(KeyError):
        mailbox.fetch_raw(101)


def test_vanish_then_move_raises_message_vanished() -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    mailbox.vanish("INBOX", 101)
    mailbox.select("INBOX", readonly=False)
    with pytest.raises(MessageVanished):
        mailbox.move(101, "Trash")


def test_append_preserves_flags_and_internaldate() -> None:
    mailbox = FakeMailbox()
    when = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    mailbox.append("INBOX", b"raw-bytes", ["\\Seen", "\\Flagged"], when)
    assert len(mailbox.appended) == 1
    mailbox.select("INBOX", readonly=True)
    uids = mailbox.search_uids()
    assert len(uids) == 1
    meta = mailbox.fetch_metadata(uids, headers=[])
    assert meta[0].internaldate == when


# --- FakeClassifier ----------------------------------------------------


def _payload(payload_id: str, offered: tuple[str, ...] = ("r1",)) -> CandidatePayload:
    return CandidatePayload(payload_id=payload_id, fields={}, offered=offered)


def test_fake_classifier_returns_scripted_outcome_in_order() -> None:
    first = outcome_with_matches(matches_by_payload={"c1": ["r1"]})
    second = outcome_with_matches(matches_by_payload={"c2": []})
    classifier = FakeClassifier([first, second])

    result1 = classifier.classify([_payload("c1")], [OfferedRule("r1", "desc")])
    assert result1 is first
    result2 = classifier.classify([_payload("c2")], [OfferedRule("r1", "desc")])
    assert result2 is second
    assert classifier.call_count == 2


def test_fake_classifier_records_calls() -> None:
    classifier = FakeClassifier([outcome_with_matches(matches_by_payload={})])
    payloads = [_payload("c1")]
    rules = [OfferedRule("r1", "desc")]
    classifier.classify(payloads, rules)
    assert classifier.calls[0][0] == tuple(payloads)
    assert classifier.calls[0][1] == tuple(rules)


def test_fake_classifier_can_raise_scripted_exception() -> None:
    classifier = FakeClassifier([TimeoutError("simulated transport failure")])
    with pytest.raises(TimeoutError):
        classifier.classify([_payload("c1")], [OfferedRule("r1", "desc")])


def test_fake_classifier_raises_when_exhausted() -> None:
    classifier = FakeClassifier([])
    with pytest.raises(AssertionError):
        classifier.classify([_payload("c1")], [OfferedRule("r1", "desc")])


def test_outcome_malformed_reports_all_ids_invalid() -> None:
    outcome = outcome_malformed(["c1", "c2"])
    assert outcome.results == ()
    assert outcome.invalid == ("c1", "c2")


def test_outcome_missing_reports_absent_ids() -> None:
    outcome = outcome_missing(present={"c1": ["r1"]}, missing_ids=["c2"])
    assert len(outcome.results) == 1
    assert outcome.missing == ("c2",)


def test_outcome_with_unknown_rule_is_structurally_valid_but_semantically_wrong() -> (
    None
):
    outcome = outcome_with_unknown_rule("c1", "never-offered")
    assert outcome.results[0].matches[0] == "never-offered"


def test_outcome_with_unknown_candidate() -> None:
    outcome = outcome_with_unknown_candidate("c99", "r1")
    assert outcome.results[0].payload_id == "c99"
