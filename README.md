# Liametahi

A local, cron-friendly CLI that cleans up an IMAP mailbox. You describe the
mail you want gone in plain language; an LLM answers **yes / no / unsure**
for each rule against each message, and deterministic code does everything
else.

```yaml
- id: old-digest
  when:
    - older-than: 30d
    - llm: A newsletter or digest with nothing time-sensitive left in it.
  actions: [backup, trash]
```

The model never touches your mail. It cannot invent an action, name a rule
you did not write, or return anything outside the closed list it was offered
for that one message — it only classifies, and a separate phase decides what
to do and does it. Every destructive action is backed up first and is
restorable.

That matters because the alternative approaches both fail: deleting years of
accumulated notifications by hand is tedious, and a rigid filter misses
everything that does not match a pattern you thought of in advance.

## Install

Needs Python ≥ 3.14, [`uv`](https://docs.astral.sh/uv/), an IMAP account, and
an LLM endpoint — an OpenAI-compatible one (including a local
[llama.cpp](https://github.com/ggml-org/llama.cpp) server), Anthropic, or
OpenRouter.

```sh
uv sync
uv run liametahi --help
```

## Quickstart

Liametahi reads a single YAML config file, by default from
`~/.config/liametahi/config.yaml` (`~/Library/Application Support/liametahi/`
on macOS, `%LOCALAPPDATA%\liametahi\` on Windows). Override with
`--config PATH` or `$LIAMETAHI_CONFIG`.

**The file holds literal credentials** and must be readable only by you —
`config check` refuses a world- or group-readable file.

```sh
mkdir -p ~/.config/liametahi && chmod 700 ~/.config/liametahi
$EDITOR ~/.config/liametahi/config.yaml
chmod 600 ~/.config/liametahi/config.yaml
```

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

# Undo a trash by restoring from its backup (any unambiguous id prefix).
uv run liametahi restore 4w8w --mailbox INBOX
```

## Safety model

- **The LLM never mutates anything.** It classifies; deterministic code
  decides and acts.
- **`trash` requires a successful `backup` first** — same message, same run,
  earlier in the same action list — unless the rule opts out with
  `allow_trash_without_backup: true`.
- **A rule that can `trash` must carry a deterministic condition.** An LLM
  verdict alone is never destructive.
- **Protection is opt-in.** No `protect:` block means nothing is protected;
  there are no hidden defaults shielding unread or flagged mail.
- **Protection is re-checked against freshly fetched flags** immediately
  before any mutation, so flagging a message from your phone after a scan
  still stops it being trashed.
- **`--dry-run` runs the whole pipeline** and prints exactly what would
  happen, touching nothing.
- **Nothing runs twice by accident.** Each message is claimed atomically, a
  crash is reconciled on the next run, and a second concurrent run of the
  same task exits `5` rather than racing.
- **A message you restore from trash is never silently re-trashed.**

Each of these is expanded, with the reasoning, in
[docs/internals.md](docs/internals.md).

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
| `max_new_mails` | Fetch at most this many *not-yet-seen* messages per run, lowest UID (oldest-arrived) first. Bounds the fetch itself, so it genuinely shortens a run. Does not limit how many already-known messages get their flags refreshed | none (no limit) |
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

The verdict is cached like any other, so a message only ever gets escalated
once: later runs reuse the answer without re-fetching the body or re-asking.
If the model is *still* unsure even with the excerpt, nothing is cached —
that is a deferral, not a decision — so it will be retried.

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

### Watching a run

At a terminal, `run` draws a live status line on stderr showing the current
phase, how many mails through it is, and elapsed time:

```
⠙ classifying 20/57 mails [########----------------] 130.2s
```

Every phase counts the same unit — mails — so two bars in one run can never
be counting different things behind identical-looking numbers. Every slow phase is counted:
fetching new mail and refreshing flags during the scan, classification,
body-excerpt escalation, and execution. Escalation is the slowest per
message, since each one costs its own un-batched model call.

It is **strictly interactive**: when stderr is not a terminal — cron, CI,
redirected output — nothing is drawn and the output is byte-for-byte what it
would have been. Phase boundaries are also logged at `info`, so a cron log
still records what happened, just without the animation.

## Documentation

- [docs/internals.md](docs/internals.md) — how a run actually works: the
  three phases, how messages are tracked and retired, the decision cache,
  and why each safety rule above exists.
- [docs/development.md](docs/development.md) — test tiers, running the
  suite, and testing against a real mailbox without touching it twice.
