"""Shared test fixtures and helpers."""

import copy
import stat
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from liametahi.domain import Candidate, MessageKey

# A minimal, fully valid config as a plain dict — mutate/deep-copy per test
# rather than re-typing the whole structure each time.
BASE_CONFIG: dict[str, Any] = {
    "version": 1,
    "settings": {
        "log_level": "info",
    },
    "accounts": {
        "personal": {
            "host": "imap.example.com",
            "username": "me@example.com",
            "password": "abcd efgh ijkl mnop",
            "trash_mailbox": "Trash",
        },
    },
    "models": {
        "local": {
            "provider": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen2.5:7b-instruct",
        },
    },
    "tasks": {
        "inbox-cleanup": {
            "account": "personal",
            "model": "local",
            "rules": [
                {
                    "id": "old-weekly-digest",
                    "priority": 100,
                    "when": [
                        {"older-than": "30d"},
                        {"list-id-contains": "digest"},
                    ],
                    "actions": ["backup", "trash"],
                },
            ],
        },
    },
}


def make_config_dict(**overrides: Any) -> dict[str, Any]:
    """A deep copy of BASE_CONFIG, useful as a starting point to mutate."""
    cfg = copy.deepcopy(BASE_CONFIG)
    cfg.update(overrides)
    return cfg


def write_config(path: Path, data: Mapping[str, Any]) -> Path:
    """Write `data` as YAML to `path` with owner-only permissions, as
    spec §12 requires."""
    path.write_text(yaml.safe_dump(dict(data), sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path / "liametahi.yaml", BASE_CONFIG)


def make_candidate(
    *,
    account_id: int = 1,
    mailbox: str = "INBOX",
    uidvalidity: int = 1000,
    uid: int = 1,
    fingerprint: str = "fp-0000000000000000000000000000000000000000000000000000000000",
    message_id: str | None = "<msg-1@example.com>",
    internaldate: datetime = datetime(2026, 6, 1, tzinfo=UTC),
    rfc822_size: int = 1024,
    flags: frozenset[str] = frozenset(),
    headers_present: frozenset[str] = frozenset({"from", "to", "subject"}),
    from_address: str | None = "sender@example.com",
    from_display: str | None = "Sender",
    recipients: tuple[str, ...] = ("me@example.com",),
    cc_count: int = 0,
    subject: str | None = "Test subject",
    list_id: str | None = None,
    has_list_unsubscribe: bool = False,
    has_attachment: bool = False,
    auth_results: str | None = None,
) -> Candidate:
    return Candidate(
        key=MessageKey(account_id, mailbox, uidvalidity, uid),
        fingerprint=fingerprint,
        message_id=message_id,
        internaldate=internaldate,
        rfc822_size=rfc822_size,
        flags=flags,
        headers_present=headers_present,
        from_address=from_address,
        from_display=from_display,
        recipients=recipients,
        cc_count=cc_count,
        subject=subject,
        list_id=list_id,
        has_list_unsubscribe=has_list_unsubscribe,
        has_attachment=has_attachment,
        auth_results=auth_results,
    )


def assert_world_unreadable(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    assert not (mode & (stat.S_IRWXG | stat.S_IRWXO))
