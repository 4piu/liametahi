# Development guide

## Running the checks

```sh
uv sync --all-groups

uv run ruff format --check .
uv run ruff check .
uv run mypy --strict src/liametahi tests
uv run pytest                    # unit + integration; integration auto-skips without Docker
uv run pytest -m integration     # only the Docker-backed IMAP tests (Dovecot)
```

All four must pass. `mypy --strict` covers the tests too, which is
deliberate: the test doubles implement the same protocols as the real
adapters, so a signature that drifts is caught in the fakes rather than
discovered at runtime.

## Test tiers

**Unit** — the default. No network, no Docker, no filesystem writes outside
`tmp_path`. `FakeMailbox` and `FakeClassifier` stand in for the two external
systems, and both are deliberately capable of misbehaving: a fake can vanish
a message mid-run, refuse a capability, bump `UIDVALIDITY`, or return a
response naming a rule that was never offered. That is what lets the
validation and crash-recovery paths be tested at all.

**Integration** (`-m integration`) — a disposable Dovecot container. Covers
what only a real IMAP server exercises: wire-format parsing, `BODYSTRUCTURE`,
`\Seen` never being set by a scan. Auto-skips when Docker is unavailable.

**Live** (`LIAMETAHI_LIVE=1 uv run pytest -m live`) — talks to a real
mailbox. Never run in CI.

## Testing against a real mailbox without touching it twice

```sh
uv sync --all-groups

uv run ruff format --check .
uv run ruff check .
uv run mypy --strict src/liametahi tests
uv run pytest                    # unit + integration; integration auto-skips without Docker
uv run pytest -m integration     # only the Docker-backed IMAP tests (Dovecot)
```

The `live` marker (`LIAMETAHI_LIVE=1 uv run pytest -m live`) exercises a real
mailbox and is never run in CI.

### Testing against a real mailbox without touching it twice

```sh
# Once: pull a corpus from a real account, read-only, into local .eml files.
uv run tools/capture_corpus.py --host imap.gmail.com --username you@gmail.com \
    --limit 200 --out tests/corpus/mine

# From then on: a disposable local Dovecot container seeded from that corpus.
uv run tools/dev_imap.py up
uv run tools/dev_imap.py seed --corpus tests/corpus/mine
uv run tools/dev_imap.py print-config   # a secret-free config pointed at it
```

`capture_corpus.py` never reads a config file or accepts a password as a CLI
argument (shell history) — it prompts, or reads `$LIAMETAHI_IMAP_PASSWORD`.
Every config after that first capture can point at the local container and
hold no real secret at all.

## Layout

```
src/liametahi/
  cli.py            argument parsing, exit codes, printing — no policy
  config.py         pydantic models and every load-time validation
  runner.py         phase orchestration; decides when to call what
  imap_adapter.py   MailboxAdapter protocol + the imaplib implementation
  domain.py         Candidate, MessageKey, fingerprint — no I/O
  rules.py          the three-valued condition evaluator (pure)
  evaluate.py       batching, response validation, decision cache lookups
  policy.py         winner selection and action resolution
  execute.py        claim, re-verify, act; the crash-recovery state machine
  backup.py         verified backup writes and restore
  state.py          every piece of SQL in the codebase
  prompt.py         payload construction, capping, sanitisation
  classifier/       one adapter per provider, no validation logic
  report.py         table and JSON rendering
  locks.py          the per-task advisory lock
  logging.py        configured logging with credential redaction
  progress.py       interactive status line (TTY only)
tools/
  capture_corpus.py read-only corpus capture from a real account
  dev_imap.py       disposable Dovecot container
```

Two boundaries are worth preserving. **Response validation lives once, in
`evaluate.py`, never in an adapter** — otherwise adding a provider could
quietly add a way past it. And **`state.py` owns all the SQL**; the few
read-only queries that live elsewhere are documented at their call sites as
gaps `state.py` should eventually close.
