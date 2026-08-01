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
- **Flag-based protection stays honest even though Liametahi isn't assumed
  to be the mailbox's only writer.** Every scan also refreshes stored flags
  for messages it already knows about (one batched fetch per mailbox, not
  per message), and, independently, the moment before any mutation actually
  runs, `protect.flags`/`protect.unread` are re-checked against freshly
  re-fetched flags — so flagging a message important from your phone after
  Liametahi already scanned it still stops it from being trashed.
- **A message is never re-actioned once it's truly gone.** A tracked message
  that was actually moved (`trash`/`move_to`) or confirmed gone from the
  server is retired and excluded from every later run — it doesn't keep
  coming back to burn a claim-and-re-verify round trip forever. A completed
  `label` never retires it, since the message is still there and may match
  something else later.
- **Restoring a trashed message is respected, not silently undone.** The
  decision cache is keyed to survive a message being moved (so a failed
  trash can retry cheaply on the next run), but that also means it survives
  a *restore*. If a message's fingerprint already has a completed
  trash/move from an earlier run, Liametahi quietly skips it instead of
  re-trashing it — with no model call either way — and retires it, so the
  decision is made once and never revisited. `--reevaluate` does not
  override this (see [Re-running and the decision
  cache](#re-running-and-the-decision-cache)).

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
    base_url: http://127.0.0.1:8080/v1/chat/completions
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
          - older-than: 30d
          - list-id-contains: digest
        actions: [backup, trash]

      - id: stale-notifications
        priority: 10
        when:
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
uv run liametahi restore 4w8wbbs3fs --mailbox INBOX
# ...or just enough leading characters to be unambiguous:
uv run liametahi restore 4w8w --mailbox INBOX
```

## Configuration reference

`*` marks a required key; every other key is optional and falls back to its
default.

### `settings`

| Key | Description | Default |
| --- | --- | --- |
| `state_db` | Path to the SQLite state database | `<platform data dir>/liametahi/state.sqlite3` |
| `backup_dir` | Directory verified message backups are written to | `<platform data dir>/liametahi/backups` |
| `task_lock_dir` | Directory for per-task advisory lock files | `<platform data dir>/liametahi/locks` |
| `log_file` | Also write logs here, in addition to stderr | none (stderr only) |
| `log_level` | `debug` / `info` / `warning` / `error` | `info` |

### `accounts.<name>`

| Key | Description | Default |
| --- | --- | --- |
| `host` * | IMAP server hostname | — |
| `port` | IMAP port | `993` |
| `username` * | IMAP login username | — |
| `password` * | IMAP login password — a literal secret | — |
| `trash_mailbox` | Required only if a task on this account has a `trash` action | none |
| `tls_insecure_skip_verify` | Skip certificate verification — loopback hosts only, rejected at load time otherwise | `false` |

### `models.<name>`

| Key | Description | Default |
| --- | --- | --- |
| `provider` * | `openai_compatible` or `anthropic` | — |
| `base_url` * | Required for `openai_compatible`; the complete Chat Completions endpoint URL, posted to as-is | — |
| `model` * | The provider's model identifier | — |
| `api_key` | Required for `anthropic`; optional for a local `openai_compatible` server | none |
| `extra_headers` | Extra HTTP headers merged into every request | `{}` |
| `structured_output` | `auto` / `json_schema` / `json_object` / `none` | `auto` |
| `mails_per_request` | Messages sent to the model per classification call. No upper bound is enforced, but large batches measurably degrade small local models | `10` |
| `timeout_seconds` | Per-request HTTP timeout | `45` |
| `max_retries` | Transport-error retries — never a rejected response | `2` |
| `body_excerpt.format` | Excerpt format offered on escalation | `plain_text_excerpt` |
| `body_excerpt.max_chars` | Truncate each body excerpt to this many characters | none (no limit) |

An OpenRouter endpoint is `provider: openai_compatible` with
`base_url: https://openrouter.ai/api/v1/chat/completions` and `model` set to
OpenRouter's namespaced id (`vendor/model`). OpenRouter also accepts optional
`HTTP-Referer`/`X-Title` attribution headers via `extra_headers`.

### `tasks.<name>`

| Key | Description | Default |
| --- | --- | --- |
| `account` * | Must name an entry in `accounts` | — |
| `model` * | Must name an entry in `models` | — |
| `source_mailboxes` | Mailboxes to scan, in order | `[INBOX]` |
| `protect.flags` | IMAP flags that exempt a message — see [Safety model](#safety-model) | `[]` (nothing protected) |
| `protect.senders` | Sender globs that exempt a message | `[]` |
| `protect.unread` | Exempt unread messages | `false` |
| `max_new_mails` | Stop after fetching this many *not-yet-seen* messages from the server in one run. Does not limit how many known messages are re-checked | none (no limit) |
| `max_actions` | Caps one run's mutations | none (uncapped) |
| `rules` * | Non-empty list — see below | — |

### `tasks.<name>.rules[]`

| Key | Description | Default |
| --- | --- | --- |
| `id` * | Unique within the task — also the model's output vocabulary | — |
| `when` * | A condition tree — see [Rule conditions](#rule-conditions) below | — |
| `actions` * | Non-empty ordered list: `backup`, `trash`, `move_to:<mailbox>`, `label:<keyword>` | — |
| `priority` | Higher wins when more than one rule matches the same message (ties break by declaration order — earlier wins) | `0` |
| `allow_body_excerpt` | Let this rule's `llm` condition trigger the bounded body-excerpt second pass (see [Reading the message body](#reading-the-message-body)) when the model reports it's unsure | `false` |
| `allow_trash_without_backup` | Required if `trash` appears with no preceding `backup` in the same action list — see [Safety model](#safety-model) | `false` |

### Rule conditions

| Condition | Plain-form default | Notes |
| --- | --- | --- |
| `older-than` / `newer-than` | duration (`30d`, `12h`) | against `INTERNALDATE` |
| `sender-match` | **glob** (`*`, `?`, `[seq]`) | `From` address only, whole-address match |
| `recipient-match` | **glob** | true if **any** of `To`/`Cc`/`Delivered-To`/`X-Original-To` matches |
| `subject-contains` | **substring** | true if the text appears anywhere in the subject |
| `list-id-contains` | **substring** | against the `List-Id` identifier |
| `has-header` | header name | present and non-empty |
| `has-flag` | IMAP flag/keyword | exact match |
| `in-mailbox` | mailbox name | case-sensitive except `INBOX` |
| `larger-than` | size (`500k`, `2M`) | against `RFC822.SIZE` |
| `recipient-count` | comparison (`>10`, `<=3`, `==1`) | against the same deduplicated union `recipient-match` uses |
| `has-attachment` | `true` | see below |
| `auth-result` | `mechanism=result` (`spf=fail`) | see below; mechanism is `spf`, `dkim`, or `dmarc` |
| `llm` | free-text description | the only condition the model ever sees |

### Combining conditions

`when:` is a list, **implicitly ANDed** — no wrapper keyword needed for the
common case:

```yaml
when:
  - older-than: 30d
  - sender-match: foo@bar.com
```

For an exclusion, wrap the excluded condition in `not:`, right in that same
list:

```yaml
when:
  - older-than: 30d
  - sender-match: foo@bar.com
  - not:
      subject-contains: buz
```

For "either of these," use `any:` — a list item can itself be `{any: [...]}`
or `{not: {...}}`, nested up to 3 deep. A rule that's fundamentally an OR at
the top is a one-item list wrapping it: `when: [{any: [...]}]`. A rule
needing only one condition skips the list entirely — `when: {older-than:
30d}` is exactly as terse as ever.

There's no top-level `all:` keyword — `when: {all: [A, B]}` and `when: [A,
B]` meant the same thing, so only the list form is accepted at the top now.
`all` is still valid *nested*, e.g. inside an `any:`'s list to group several
conditions as one alternative: `any: [{all: [A, B]}, C]` reads as "(A and B)
or C".

`not` can't wrap an `llm` condition (spec-enforced) — the payload sent to the
model carries a description without polarity, so a negated `llm` atom would
ask the model an un-negated question and then invert the answer, which is
backwards. Phrase the exclusion in the description text instead: `llm: "safe
to discard, except anything mentioning buz"`.

### Condition details

`-match` conditions default to **glob**: without a wildcard, the value must
equal the whole field (`sender-match: bank.example` matches only that exact
address, not "contains bank.example" — write `*bank.example*` for that).
`-contains` conditions default to **substring**: the value is checked
anywhere in the field, no wildcard syntax, always effectively "contains."
Both accept a `/regex/flags` literal as a third option. A regex value both
starts and ends with `/`, e.g. `sender-match: /.+@gmail\.com/i` — supported
flags are
`i`/`m`/`s`/`g`. Regex is compiled at config-load time (a bad pattern fails
`config check`, not a run) and is **case-sensitive by default**, unlike the
plain glob/substring form. These patterns run against sender-controlled
input; avoid nested quantifiers (`(a+)+`) that can hang on an adversarial
value.

`has-attachment: true` is derived from the IMAP `BODYSTRUCTURE` response
(parsed once during scan, not at rule-eval time). It's a heuristic, not a
MIME-spec guarantee: any part carrying a `NAME`/`FILENAME` parameter or an
`attachment` disposition counts, which means an inline image embedded in an
HTML signature can register as an "attachment" even though no mail client
would show it as one to a user. `not: {has-attachment: true}` is how you
express "no attachment" — `has-attachment: false` is rejected outright
rather than accepted as a confusing second spelling of the same thing.

`auth-result` reads the `Authentication-Results` header and checks for
`mechanism=result` (case-insensitive), e.g. `auth-result: dmarc=fail`. A
message can carry more than one such header — one per hop that performed
its own checks — so **only the topmost (first-encountered) one is ever
consulted**, since that's the one added last, by the hop closest to you
(ordinarily your own provider). This is a positional heuristic, not a
cryptographic guarantee: it doesn't verify that header actually came from
your provider, which would need correlating the `Received` chain too.

`recipient-match` is a union test across every recipient the message names
(`To`, `Cc`, `Delivered-To`, `X-Original-To`, deduplicated, display names
stripped) — it's true the moment **one** of them matches, so it works the
same whether a message has one recipient or fifty; there's no way to
require *every* recipient to match. This list is never capped (a separate,
much smaller cap only applies to what's shown to the LLM classifier — it
never affects a deterministic condition like this one).

At most one `llm` atom per rule, and it may not appear under `not` — the
model answers "does X apply," not an arbitrary boolean expression.

### Actions and winner-takes-all

`backup`, `trash`, `move_to:<mailbox>`, `label:<keyword>`. When more than one
rule matches a message, the rule with the highest `priority` wins (ties
broken by config order); the rest are reported `shadowed`, not run.

### Reading the message body

By default, the model only ever sees message metadata (headers, sizes,
flags) — never the body. If a rule sets `allow_body_excerpt: true` and
the model reports it's unsure, a bounded plain-text excerpt of the body is
fetched once and the message is re-classified. Off by default; turn it on
per-rule with `allow_body_excerpt: true`. Bear in mind this is the only
path where message *bodies* reach the model, and that each escalation is
its own un-batched model call plus a full-message fetch — so enable it on
the rules that need it, not everywhere.

### Watching a run

At a terminal, `run` draws a live status line on stderr showing the current
phase, how far through it is, and elapsed time. Every slow phase is counted:
fetching new mail and refreshing flags during the scan, classification,
body-excerpt escalation, and execution. Escalation is the slowest per
message, since each one costs its own un-batched model call.

It is **strictly interactive**: when stderr is not a terminal — cron, CI,
redirected output — nothing is drawn and the output is byte-for-byte what it
would have been. Phase boundaries are also logged at `info`, so a cron log
still records what happened, just without the animation.

### Re-running and the decision cache

A rule's model decision — match or non-match — is cached per message and
reused on later runs, so an hourly cron job never re-classifies the same
already-decided mail. This is also what makes a failed action retry cheaply:
if a message matched a `trash` rule but the actual mailbox move failed (a
misconfigured `trash_mailbox`, a capability the server doesn't advertise),
the message stays put and is picked up again next run — the cached match is
reused straight into policy/execution, not re-sent to the model. `--reevaluate`
bypasses the cache entirely and forces a fresh pass.

The cache is keyed to survive a message moving, which is exactly what makes
it survive a *restore* too: if you move a trashed message back to a source
mailbox, it re-scans under a new UID and can hit the same cached "yes".
Liametahi checks for this — a message whose fingerprint already has a
completed trash/move from a previous run is skipped, never silently
re-trashed. The skip is recorded as a quiet `restored` result item (visible
with `report --verbose`, absent from default output) and retires the
message, so it is skipped once and then dropped from
consideration entirely rather than re-reported on every future run. There is
currently no flag to force Liametahi to re-trash a message you've restored on
purpose; delete its old `action_attempts` history from the state database, or
match it with a different rule, if you truly want that.
`--reevaluate` does not affect this check — it governs only the LLM cache.

## CLI reference

```
liametahi config check [--connect]
liametahi run TASK [--dry-run] [--fail-fast] [--reevaluate] [--wait SECONDS] [--format table|json] [--verbose]
liametahi report [RUN_ID] [--list] [--task TASK] [--format table|json] [--verbose]
liametahi restore BACKUP_ID --mailbox MAILBOX [--account NAME] [--dry-run]
```

Run and backup ids are short random strings such as `4w8wbbs3fs`. Anywhere
one is accepted you may type just enough leading characters to identify it
uniquely — like a short commit hash — and you get a listing of the
candidates back if the prefix is ambiguous. They use Crockford's base32
alphabet, which omits `i`, `l`, `o` and `u`, so there is no `1`/`l` or
`0`/`O` confusion reading one off the terminal.

`--config PATH` is accepted by every subcommand and overrides
`$LIAMETAHI_CONFIG` and the platform default.

Exit codes: `0` success, `1` runtime/partial failure, `2` bad config or
invocation, `3` interrupted (SIGINT/SIGTERM — the run is finished with a
proper report and the task lock released), `4` authentication failure,
`5` the task is already running (cron-safe — a typical crontab line is
`liametahi run TASK || [ $? -eq 5 ]`).

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
