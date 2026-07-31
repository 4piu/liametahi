"""Tests for `tools/capture_corpus.py` (contracts §6.2).

The most important test in this file is
`test_capture_corpus_performs_no_mutating_calls`: it is the mechanical
proof, driven against `FakeMailbox`'s mutation-recording hook (contracts
§6.3), that the read-only-by-construction claim in the tool's module
docstring actually holds.
"""

import hashlib
import json
from pathlib import Path

import pytest
from tools import capture_corpus

from tests.fakes.fake_mailbox import FakeMailbox

CORPUS_MANIFEST = Path(__file__).parent / "corpus" / "synthetic" / "manifest.json"


# --- capture_mailbox / capture_corpus (read-only capture) ----------------


def test_capture_corpus_performs_no_mutating_calls(tmp_path: Path) -> None:
    """The tool's central safety property: driving a full capture never
    calls `move`, `add_keyword`, or `append` on the adapter."""
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=tmp_path / "out"
    )
    assert mailbox.mutations == ()


def test_capture_mailbox_writes_content_addressed_files_matching_manifest(
    tmp_path: Path,
) -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    out_dir = tmp_path / "out"
    manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=out_dir
    )
    assert len(entries) == 6
    assert manifest_path == out_dir / "manifest.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["corpus_version"] == 1
    assert len(manifest["messages"]) == 6

    for row in manifest["messages"]:
        assert set(row.keys()) == {
            "relative_path",
            "mailbox",
            "uid",
            "uidvalidity",
            "flags",
            "internaldate",
            "sha256",
            "byte_count",
        }
        eml_path = out_dir / row["relative_path"]
        assert eml_path.is_file()
        data = eml_path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["sha256"]
        assert len(data) == row["byte_count"]
        assert row["mailbox"] == "INBOX"
        assert row["relative_path"] == f"messages/{row['sha256']}.eml"


def test_capture_mailbox_respects_limit_and_keeps_most_recent(tmp_path: Path) -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    _manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=2, out_dir=tmp_path / "out"
    )
    assert len(entries) == 2
    # uids 105 and 106 are the two most recently received messages in
    # the synthetic corpus (internaldate ascending == uid ascending
    # there), so a limit of 2 should keep exactly those.
    assert sorted(e.uid for e in entries) == [105, 106]


def test_capture_mailbox_empty_mailbox_returns_no_entries(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=tmp_path / "out"
    )
    assert entries == []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {"corpus_version": 1, "messages": []}


def test_captured_corpus_reloads_via_fake_mailbox_from_corpus(tmp_path: Path) -> None:
    """A captured corpus is exactly the format `FakeMailbox.from_corpus`
    consumes -- capture output is itself a valid test corpus."""
    source = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    out_dir = tmp_path / "out"
    manifest_path, entries = capture_corpus.capture_corpus(
        source, mailbox="INBOX", limit=25, out_dir=out_dir
    )
    reloaded = FakeMailbox.from_corpus(manifest_path)
    reloaded.select("INBOX", readonly=True)
    assert sorted(reloaded.search_uids()) == sorted(e.uid for e in entries)


def test_capture_preserves_flags(tmp_path: Path) -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    _manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=tmp_path / "out"
    )
    by_uid = {e.uid: e for e in entries}
    assert by_uid[102].flags == ("\\Flagged",)
    assert by_uid[105].flags == ("\\Seen",)
    assert by_uid[101].flags == ()


# --- refuse-to-overwrite -------------------------------------------------


def test_prepare_out_dir_refuses_nonempty_directory_without_force(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stray.txt").write_text("pre-existing", encoding="utf-8")
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    with pytest.raises(capture_corpus.CaptureError, match="already exists"):
        capture_corpus.capture_corpus(
            mailbox, mailbox="INBOX", limit=25, out_dir=out_dir
        )
    # Nothing was mutated on the adapter by the failed attempt either.
    assert mailbox.mutations == ()


def test_force_overwrites_existing_directory(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "stray.txt").write_text("pre-existing", encoding="utf-8")
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=out_dir, force=True
    )
    assert not (out_dir / "stray.txt").exists()
    assert len(entries) == 6
    assert manifest_path.is_file()


def test_capture_into_empty_existing_directory_does_not_require_force(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()  # exists, but empty
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    _manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=out_dir
    )
    assert len(entries) == 6


# --- write/verify helpers -------------------------------------------------


def test_verify_manifest_detects_tampered_file(tmp_path: Path) -> None:
    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    out_dir = tmp_path / "out"
    _manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=out_dir
    )
    tampered = out_dir / entries[0].relative_path
    tampered.write_bytes(b"corrupted")
    with pytest.raises(capture_corpus.CaptureError, match="verification failed"):
        capture_corpus.verify_manifest(out_dir, entries)


def test_output_files_and_manifest_are_owner_only(tmp_path: Path) -> None:
    import stat

    mailbox = FakeMailbox.from_corpus(CORPUS_MANIFEST)
    out_dir = tmp_path / "out"
    manifest_path, entries = capture_corpus.capture_corpus(
        mailbox, mailbox="INBOX", limit=25, out_dir=out_dir
    )
    for path in (manifest_path, out_dir / entries[0].relative_path):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not (mode & (stat.S_IRWXG | stat.S_IRWXO))


# --- CLI-level guardrails (no network required) ---------------------------


def test_main_rejects_insecure_skip_verify_for_non_loopback_host(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = "imap.gmail.com"
    exit_code = capture_corpus.main(
        [
            "--host",
            host,
            "--username",
            "me@example.com",
            "--out",
            str(tmp_path / "out"),
            "--insecure-skip-verify",
        ]
    )
    assert exit_code == 2
    assert "loopback" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_main_rejects_nonpositive_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host = "imap.example.com"
    exit_code = capture_corpus.main(
        [
            "--host",
            host,
            "--username",
            "me@example.com",
            "--out",
            str(tmp_path / "out"),
            "--limit",
            "0",
        ]
    )
    assert exit_code == 2
    assert "--limit" in capsys.readouterr().err


def test_main_connection_failure_is_exit_1_and_no_output_written(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad host/port should fail fast as a transport error (exit 1),
    not hang, and must not create the output directory."""
    monkeypatch.setenv("LIAMETAHI_IMAP_PASSWORD", "not-a-real-password")
    host = "127.0.0.1"
    out_dir = tmp_path / "out"
    exit_code = capture_corpus.main(
        [
            "--host",
            host,
            "--username",
            "me@example.com",
            "--out",
            str(out_dir),
            "--limit",
            "1",
        ]
    )
    assert exit_code == 1
    assert "connection failed" in capsys.readouterr().err
    assert not out_dir.exists()
