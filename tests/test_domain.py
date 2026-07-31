"""Tests for `liametahi.domain` (contracts §5.1, spec §11)."""

from datetime import UTC, datetime

from liametahi.domain import MessageKey, fingerprint


def test_fingerprint_uses_message_id_when_present() -> None:
    date = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    fp = fingerprint(
        message_id="<abc@example.com>",
        internaldate=date,
        rfc822_size=1234,
        from_address="someone@example.com",
        subject="hello",
    )
    assert isinstance(fp, str)
    assert len(fp) == 64  # sha256 hex digest
    int(fp, 16)  # must be valid hex


def test_fingerprint_deterministic_for_same_inputs() -> None:
    date = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    a = fingerprint(
        message_id="<abc@example.com>",
        internaldate=date,
        rfc822_size=1234,
        from_address="x@example.com",
        subject="s",
    )
    b = fingerprint(
        message_id="<abc@example.com>",
        internaldate=date,
        rfc822_size=1234,
        from_address="x@example.com",
        subject="s",
    )
    assert a == b


def test_fingerprint_falls_back_when_message_id_absent() -> None:
    date = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    with_id = fingerprint(
        message_id="<abc@example.com>",
        internaldate=date,
        rfc822_size=1234,
        from_address="x@example.com",
        subject="s",
    )
    without_id = fingerprint(
        message_id=None,
        internaldate=date,
        rfc822_size=1234,
        from_address="x@example.com",
        subject="s",
    )
    # Different formulas (spec §11) must not coincidentally collide here.
    assert with_id != without_id


def test_fingerprint_fallback_deterministic_and_sensitive_to_inputs() -> None:
    date = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    base = fingerprint(
        message_id=None,
        internaldate=date,
        rfc822_size=1234,
        from_address="x@example.com",
        subject="s",
    )
    same = fingerprint(
        message_id=None,
        internaldate=date,
        rfc822_size=1234,
        from_address="x@example.com",
        subject="s",
    )
    different_subject = fingerprint(
        message_id=None,
        internaldate=date,
        rfc822_size=1234,
        from_address="x@example.com",
        subject="different",
    )
    assert base == same
    assert base != different_subject


def test_fingerprint_survives_naive_vs_aware_same_instant_utc() -> None:
    # Two tz-aware datetimes representing the same UTC instant in
    # different offsets must normalise to the same fingerprint.
    from datetime import timedelta, timezone

    utc_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    plus_one = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    a = fingerprint(
        message_id="<x@example.com>",
        internaldate=utc_dt,
        rfc822_size=10,
        from_address=None,
        subject=None,
    )
    b = fingerprint(
        message_id="<x@example.com>",
        internaldate=plus_one,
        rfc822_size=10,
        from_address=None,
        subject=None,
    )
    assert a == b


def test_message_key_render() -> None:
    key = MessageKey(account_id=1, mailbox="INBOX", uidvalidity=1000, uid=42)
    assert key.render() == "1/INBOX/1000/42"
