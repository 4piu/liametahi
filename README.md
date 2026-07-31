# Liametahi

A local, cron-friendly CLI that cleans up an IMAP mailbox using an LLM as a
**constrained classifier**, never as the thing that touches your mail.

The model only ever answers one question, per rule, per message: *does this
rule's description apply — yes, no, or unsure?* Every mailbox mutation
(backup, trash, move, label) is performed by deterministic application code
that never reads the model's free-text output, only its yes/no/unsure
verdict against rules **you** wrote. Every destructive action is backed up
first and is restorable.

## Why

Inboxes accumulate years of automated notifications, receipts, and digests
that are individually low-value but collectively make search and triage
worse. Deleting them by hand is tedious; deleting them with a rigid filter
misses anything that doesn't match a fixed pattern. Liametahi lets you
describe *what kind of mail* is safe to discard in plain language, while
keeping every irreversible decision behind deterministic guardrails you can
read, test, and audit.

## Safety model

- **The LLM never mutates anything.** It classifies; a separate, deterministic
  phase decides what to do and does it.
- **`trash` refuses to run for a message unless `backup` already succeeded
  for that exact message, earlier in the same rule's action list, in this
  same run** — a backup from a previous run doesn't count. If a rule
  doesn't want a local copy at all — a mail server's own trash folder is
  often recovery enough on its own — set `allow_trash_without_backup: true`
  on it explicitly; `config check` rejects any rule that could never
  satisfy the requirement (`backup` missing, or listed after `trash`), so a
  misconfigured rule fails loudly up front instead of quietly doing nothing
  on every real run. The check is per message: one message's backup
  failing skips only that message's trash and leaves it untouched; every
  other message in the run proceeds normally.
- **Protection is opt-in and explicit.** A task with no `protect:` block
  protects nothing — there is no hidden default shielding unread or flagged
  mail. Write down what you want protected.
- **A rule that can `trash` mail must carry at least one deterministic
  condition.** An LLM verdict alone can never be destructive.
- **`--dry-run` runs the full pipeline** (scan, classify, decide) and prints
  exactly what *would* happen, without touching the mailbox.
- **Nothing runs twice by accident.** A run claims each message atomically;
  a crash mid-run is reconciled cleanly on the next invocation; a second
  concurrent invocation of the same task fails fast (exit `5`) rather than
  racing.

## Requirements

- Python ≥ 3.14
- [`uv`](https://docs.astral.sh/uv/) for dependency management and running
  the tool
- An IMAP account (Gmail, or any IMAP server)
- An LLM endpoint: an OpenAI-compatible endpoint (including a local
  [llama.cpp](https://github.com/ggml-org/llama.cpp) server), Anthropic, or
  OpenRouter

## Install

```sh
uv sync
uv run liametahi --help
```

`uv sync` also installs the dev dependencies (pytest, ruff, mypy) needed to
run the test suite below.

## Quickstart

Liametahi reads a single YAML config file. By default it looks for:

| Platform | Default path |
| --- | --- |
| Linux | `~/.config/liametahi/config.yaml` |
| macOS | `~/Library/Application Support/liametahi/config.yaml` |
| Windows | `%LOCALAPPDATA%\liametahi\config.yaml` |

Override with `--config PATH` or `$LIAMETAHI_CONFIG`.

**The config file holds literal credentials** (spec-mandated design: no
secondary secret store) and must be owned and readable only by you —
`liametahi config check` refuses a world- or group-readable file.

```sh
mkdir -p ~/.config/liametahi
chmod 700 ~/.config/liametahi
$EDITOR ~/.config/liametahi/config.yaml
chmod 600 ~/.config/liametahi/config.yaml
```

A minimal config:

```yaml
version: 1

accounts:
  personal:
    host: imap.gmail.com
    port: 993
    username: you@gmail.com
    password: "an app password, not your real one"
    trash_mailbox: "[Gmail]/Trash"

models:
  local:
    provider: openai_compatible
    base_url: http://127.0.0.1:8080/v1
    model: qwen2.5-7b-instruct

tasks:
  inbox-cleanup:
    account: personal
    model: local
    source_mailboxes: [INBOX]
    protect:
      flags: ['\Flagged', '\Answered']
      unread: true
    rules:
      - id: old-digest
        priority: 100
        when:
          all:
            - older-than: 30d
            - list-id-contains: digest
        actions: [backup, trash]

      - id: stale-notifications
        priority: 10
        when:
          all:
            - older-than: 7d
            - llm: >
                An automated notification, receipt, or update that is safe
                to discard, except bills, account-security notices, or mail
                that asks the recipient to act.
        actions: [backup, trash]
```

Then:

```sh
# Validate the file, and optionally check IMAP connectivity/capabilities.
uv run liametahi config check --connect

# See exactly what would happen — no mailbox mutation.
uv run liametahi run inbox-cleanup --dry-run --verbose

# Once you're happy with the plan, run it for real.
uv run liametahi run inbox-cleanup

# Review a past run any time; never touches the mailbox or model.
uv run liametahi report --list
uv run liametahi report            # the newest run

# Undo a trash by restoring from its backup.
uv run liametahi restore bkp_01J... --mailbox INBOX
```

## Configuration reference

### `settings` (all optional)

| Key | Default |
| --- | --- |
| `state_db` | `<platform data dir>/liametahi/state.sqlite3` |
| `backup_dir` | `<platform data dir>/liametahi/backups` |
| `task_lock_dir` | `<platform data dir>/liametahi/locks` |
| `log_file` | none (stderr only) |
| `log_level` | `info` |
| `candidate_retention_days` | `90` |

### `accounts.<name>`

`host`, `port`, `username`, `password`, `trash_mailbox`. `tls_insecure_skip_verify`
is available for a local development server on loopback only (rejected at
load time for any other host).

### `models.<name>`

| Key | Notes |
| --- | --- |
| `provider` | `openai_compatible` or `anthropic` |
| `base_url` | required for `openai_compatible` |
| `model` | the provider's model identifier |
| `api_key` | required for `anthropic`; optional for a local `openai_compatible` server |
| `structured_output` | `auto` (default) / `json_schema` / `json_object` / `none` |
| `batch_size` | `10`, range 1–25 |
| `content_escalation.enabled` | `false` by default — see below |

An OpenRouter endpoint is `provider: openai_compatible` with
`base_url: https://openrouter.ai/api/v1` and `model` set to OpenRouter's
namespaced id (`vendor/model`). OpenRouter also accepts optional
`HTTP-Referer`/`X-Title` attribution headers via `extra_headers`.

### `tasks.<name>`

| Key | Default |
| --- | --- |
| `account` / `model` | required, must name entries above |
| `source_mailboxes` | `[INBOX]` |
| `protect.flags` / `protect.senders` / `protect.unread` | all off unless set — see [Safety model](#safety-model) |
| `max_candidates_per_run` | `500` |
| `max_actions_per_run` | unset — **no cap** unless you set one |
| `rules` | required, non-empty |

### Rule conditions

| Condition | Argument | Notes |
| --- | --- | --- |
| `older-than` / `newer-than` | `30d`, `12h` | against `INTERNALDATE` |
| `sender-match` | glob or `/regex/flags` | `From` address only |
| `recipient-match` | glob or `/regex/flags` | any of `To`/`Cc`/`Delivered-To`/`X-Original-To` |
| `subject-match` | substring or `/regex/flags` | not a glob in its plain form |
| `list-id-contains` | substring or `/regex/flags` | against the `List-Id` identifier |
| `has-header` | header name | present and non-empty |
| `has-flag` | IMAP flag/keyword | exact match |
| `in-mailbox` | mailbox name | case-sensitive except `INBOX` |
| `larger-than` | `500k`, `2M` | against `RFC822.SIZE` |
| `llm` | free-text description | the only condition the model ever sees |

Combine with `all` / `any` / `not`, nesting up to 3 deep. A regex value both
starts and ends with `/`, e.g. `sender-match: /.+@gmail\.com/i` — supported
flags are `i`/`m`/`s`/`g`. Regex is compiled at config-load time (a bad
pattern fails `config check`, not a run) and is **case-sensitive by default**,
unlike the plain glob/substring form. These patterns run against
sender-controlled input; avoid nested quantifiers (`(a+)+`) that can hang on
an adversarial value.

At most one `llm` atom per rule, and it may not appear under `not` — the
model answers "does X apply," not an arbitrary boolean expression.

### Actions and winner-takes-all

`backup`, `trash`, `move_to:<mailbox>`, `label:<keyword>`. When more than one
rule matches a message, the rule with the highest `priority` wins (ties
broken by config order); the rest are reported `shadowed`, not run.

### Content escalation

By default, the model only ever sees message metadata (headers, sizes,
flags) — never the body. If a rule sets `allow_content_escalation: true` and
the model reports it's unsure, a bounded plain-text excerpt of the body is
fetched once and the message is re-classified. Off by default; turn it on
per-rule, and cap the blast radius with
`models.<name>.content_escalation.max_messages_per_run`.

## CLI reference

```
liametahi config check [--connect]
liametahi run TASK [--dry-run] [--fail-fast] [--reevaluate] [--wait SECONDS] [--format table|json] [--verbose]
liametahi report [RUN_ID] [--list] [--task TASK] [--format table|json] [--verbose]
liametahi restore BACKUP_ID --mailbox MAILBOX [--account NAME] [--dry-run]
```

`--config PATH` is accepted by every subcommand and overrides
`$LIAMETAHI_CONFIG` and the platform default.

Exit codes: `0` success, `1` runtime/partial failure, `2` bad config or
invocation, `4` authentication failure, `5` the task is already running
(cron-safe — a typical crontab line is `liametahi run TASK || [ $? -eq 5 ]`).

## Development

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
