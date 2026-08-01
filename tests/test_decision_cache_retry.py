"""End-to-end regression test for the LLM decision cache (spec §13)
caching a match, not just a non-match: a message that the model matched
but whose remote mutation then failed (wrong `trash_mailbox`, a
capability the server doesn't advertise, ...) stays a live candidate and
must retry that mutation on the next run without being reclassified.
"""

from datetime import UTC, datetime
from pathlib import Path

from liametahi import runner as runner_mod
from liametahi.config import load_config
from tests.conftest import make_config_dict, write_config
from tests.fakes.fake_classifier import FakeClassifier, outcome_with_matches
from tests.fakes.fake_mailbox import FakeMailbox, _StoredMessage

MESSAGE_ID = "<msg1@example.com>"


def _config_with_state(tmp_path: Path) -> Path:
    data = make_config_dict()
    data["settings"] = {
        "log_level": "info",
        "state_db": str(tmp_path / "state.sqlite3"),
        "backup_dir": str(tmp_path / "backups"),
        "task_lock_dir": str(tmp_path / "locks"),
    }
    data["tasks"]["inbox-cleanup"]["rules"][0]["when"] = [
        {"older-than": "1h"},
        {"llm": "always trash"},
    ]
    data["tasks"]["inbox-cleanup"]["rules"][0]["actions"] = ["backup", "trash"]
    return write_config(tmp_path / "cfg.yaml", data)


def _mailbox(*, capabilities: frozenset[str]) -> FakeMailbox:
    raw = (
        "From: sender@example.com\r\n"
        "To: me@example.com\r\n"
        "Subject: Weekly Digest\r\n"
        f"Message-Id: {MESSAGE_ID}\r\n"
        "Date: Mon, 1 Jun 2026 08:00:00 +0000\r\n"
        "\r\n"
        "body text\r\n"
    ).encode()
    return FakeMailbox(
        messages=[
            _StoredMessage(
                uid=1,
                raw=raw,
                flags=set(),
                internaldate=datetime(2026, 6, 1, tzinfo=UTC),
                mailbox="INBOX",
            )
        ],
        uidvalidity={"INBOX": 1000, "Trash": 1000},
        capabilities=capabilities,
    )


def test_failed_trash_retries_next_run_without_reclassifying(tmp_path: Path) -> None:
    path = _config_with_state(tmp_path)
    cfg = load_config(path)

    # --- Run 1: the model matches; trash fails because the server does
    # not advertise MOVE (standing in for any remote-mutation failure --
    # a wrong trash_mailbox name behaves identically from here on: the
    # message is untouched and stays in INBOX). -------------------------
    mb_1 = _mailbox(capabilities=frozenset())  # no MOVE
    clf_1 = FakeClassifier(
        [outcome_with_matches(matches_by_payload={"c1": ["old-weekly-digest"]})]
    )
    outcome_1 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb_1,
        classifier_factory=lambda model_cfg: clf_1,
    )
    assert clf_1.call_count == 1
    assert outcome_1.report_data is not None
    assert [item.status for item in outcome_1.report_data.items] == ["unsupported"]

    mb_1.select("INBOX", readonly=True)
    assert mb_1.search_uids() == (1,), "the message must still be in INBOX"

    # --- Run 2: the capability is now available (standing in for "the
    # user fixed their config"); the classifier would raise if called at
    # all, proving the cached match is reused instead of re-asked. -------
    mb_2 = _mailbox(capabilities=frozenset({"MOVE"}))
    clf_2 = FakeClassifier([])  # any classify() call would raise -- none expected
    outcome_2 = runner_mod.run_task(
        config=cfg,
        config_path=path,
        task_name="inbox-cleanup",
        dry_run=False,
        fail_fast=False,
        reevaluate=False,
        wait_seconds=0.0,
        mailbox_factory=lambda account_cfg: mb_2,
        classifier_factory=lambda model_cfg: clf_2,
    )
    assert clf_2.call_count == 0, "the second run must not re-ask the model"
    assert outcome_2.report_data is not None
    assert [item.status for item in outcome_2.report_data.items] == ["completed"]

    mb_2.select("Trash", readonly=True)
    assert mb_2.search_uids() == (1,), "the retry must actually move the message"
