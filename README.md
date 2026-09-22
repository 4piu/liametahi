# Liametahi

A local, cron-friendly CLI that cleans up an IMAP mailbox. You describe the
mail you want gone as one or more named **processors** — a structured
question ("is this spam?", "how urgent is this?") answered either by a fast
decision model (`jev`) or an ordinary chat model — and rules reference those
processors' answers. Deterministic code does everything else.

```yaml
processors:
  vibe-check:
    model: local
    type: noul
    question: A newsletter or digest with nothing time-sensitive left in it.

tasks:
  inbox-cleanup:
    account: personal
    source_mailboxes: [INBOX]
    rules:
      - when:
          - older-than: 30d
          - processor: "vibe-check.value == true"
        actions: [backup, trash]
```

The model never touches your mail. It cannot invent an action, name a
processor you did not declare, or return an answer outside the closed
vocabulary (options/levels) it was offered — it only answers the question a
processor asks, and a separate phase decides what to do and does it. A
destructive action can be backed up first and restored; nothing requires it,
but nothing stops you from asking for it either.

That matters because the alternative approaches both fail: deleting years of
accumulated notifications by hand is tedious, and a rigid filter misses
everything that does not match a pattern you thought of in advance.

## Install

Needs Python ≥ 3.14, [`uv`](https://docs.astral.sh/uv/), an IMAP account, and
a model endpoint — an OpenAI-compatible one (including a local
[llama.cpp](https://github.com/ggml-org/llama.cpp) server), Anthropic,
OpenRouter, or a `jev` structured-decision endpoint.

```sh
uv sync
uv run liametahi --help
```

## Quickstart

Liametahi reads a single YAML config file. It is located, in order:

1. `--config PATH`
2. `$LIAMETAHI_CONFIG`
3. `./config.yaml` in the current directory — a **project-level** config,
   useful for a per-repo or per-checkout profile that should take priority
   without passing a flag every time
4. `~/.config/liametahi/config.yaml` (`~/Library/Application Support/liametahi/`
   on macOS, `%LOCALAPPDATA%\liametahi\` on Windows) — the user-level default

Whichever file is found first is used as-is; there is no merging between a
project-level and a user-level config.

**The file holds literal credentials.** It must be owned by you —
`config check` refuses a file owned by another user outright — and it
should be readable only by you: a world- or group-readable file still
loads, but prints a warning on every run until you fix its permissions.

Copy the example and fill in your account, model, and rules:

```sh
mkdir -p ~/.config/liametahi && chmod 700 ~/.config/liametahi
cp config.example.yaml ~/.config/liametahi/config.yaml
chmod 600 ~/.config/liametahi/config.yaml
$EDITOR ~/.config/liametahi/config.yaml
```

`config.example.yaml` (at the repo root) looks like this:

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

processors:
  stale-update:
    model: local
    type: noul
    question: >
      An automated notification, receipt, or update that is safe to discard,
      except bills, account-security notices, or mail that asks the
      recipient to act.

tasks:
  inbox-cleanup:
    account: personal
    source_mailboxes: [INBOX]
    protect:
      flags: ['\Flagged', '\Answered']
      unread: true
    rules:
      # A rule has no id and no priority: this is a plain list, evaluated
      # top to bottom, and the first match wins.
      - when:
          - older-than: 30d
          - list-id-contains: digest
        actions: [backup, trash]

      - when:
          - older-than: 7d
          - processor: "stale-update.value == true"
        actions: [backup, trash]
```

See [`config.example.yaml`](config.example.yaml) for a fuller,
two-task example that also uses a `jev` decision model and `task:` routing
to run an expensive chat model only on the subset of mail a cheap first
pass left uncertain.

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

- **The LLM never mutates anything.** It only answers a processor's
  question; deterministic code decides and acts.
- **`backup` is available, not required, before `trash`.** Most IMAP
  providers already retain trashed mail for some window, so a mandatory
  local copy was frequently redundant friction. Anyone who wants a
  tool-restorable local copy still adds `backup` to that rule's own action
  list.
- **A rule that can `trash` must carry a deterministic condition.** A
  processor's answer alone — however confident, whatever model produced it —
  is never sufficient by itself to delete mail; at least one atom in the
  rule's `when:` must be a deterministic condition like `older-than` or
  `in-mailbox`, not a `processor:` atom.
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
| `provider` * | `openai_compatible`, `anthropic`, or `jev` | — |
| `base_url` * | Required for `openai_compatible` and `jev`; the complete endpoint URL, posted to as-is | — |
| `model` * | The provider's model identifier | — |
| `api_key` | Required for `anthropic` and `jev`; optional for a local `openai_compatible` server | none |
| `extra_headers` | Extra HTTP headers merged into every request | `{}` |
| `structured_output` | `auto` / `json_schema` / `json_object` / `none` — ignored for `jev`, which has no separate structured-output negotiation | `auto` |
| `mails_per_request` | Messages sent to the model per classification call. **Must be exactly `1` for `provider: jev`** — jev answers one candidate per HTTP call, never a batch. No upper bound is enforced for the other providers, but large batches measurably degrade small local models | `10` |
| `max_concurrent_requests` | Classification requests in flight at once. Raising this is the biggest speed-up available on a first run, including for `jev`; how far you can raise it is your provider's rate limit to answer | `1` (serial) |
| `timeout_seconds` | Per-request HTTP timeout | `45` |
| `max_retries` | Transport-error retries — never a rejected response, except for `jev`, which also retries `429`/`529` (temporary capacity, not a bad request) | `2` |
| `body_excerpt.format` | Excerpt format for any processor with `include_body: true` | `plain_text_excerpt` |
| `body_excerpt.max_chars` | Truncate each body excerpt to this many characters | none (no limit) |

An OpenRouter endpoint is `provider: openai_compatible` with
`base_url: https://openrouter.ai/api/v1/chat/completions` and `model` set to
OpenRouter's namespaced id (`vendor/model`). OpenRouter also accepts optional
`HTTP-Referer`/`X-Title` attribution headers via `extra_headers`.

### `processors.<name>`

A processor names one question, answered by whichever model it references —
a chat model gets the same structured question compiled into a prompt; `jev`
answers it directly. Nothing else in the config gates whether a processor
runs for a given candidate: that is entirely a consequence of which rules
reference it (see [Rule conditions](#rule-conditions)'s `processor:` atom)
and, for cross-task pipelines, the `task:<id>` action below.

| Key | Description | Default |
| --- | --- | --- |
| `model` * | Must name an entry in `models` | — |
| `type` * | `noul` (boolean), `choice` (one of several named options), or `score` (one of an ordered list of levels) | — |
| `question` | Natural-language framing sent to the model alongside `criteria`/`options`/`levels`. For `noul` it doubles as shorthand: a plain `question` with no `criteria` implies `criteria: {"true": question}` | — |
| `criteria` | Required for `noul` unless `question` is used; exactly the keys `"true"` and `"false"` | — |
| `options` | Required for `choice`; a map of option name to its description, at most 255 entries | — |
| `levels` | Required for `score`; an ordered list of level names, 2 to 10 entries | — |
| `include_body` | This processor's request always includes a bounded plain-text body excerpt — a static switch, not a dynamic per-message escalation | `false` |

### `tasks.<name>`

| Key | Description | Default |
| --- | --- | --- |
| `account` * | Must name an entry in `accounts` | — |
| `source_mailboxes` | Mailboxes to scan, in order. May be omitted entirely for a task that exists only as a `task:<id>` routing target | `[]` (none — an omitted task must be a routing target, or it is unreachable and rejected at load time) |
| `protect.flags` | IMAP flags that exempt a message — see [Safety model](#safety-model) | `[]` (nothing protected) |
| `protect.senders` | Sender globs that exempt a message | `[]` |
| `protect.unread` | Exempt unread messages | `false` |
| `max_new_mails` | Fetch at most this many *not-yet-seen* messages per run, lowest UID (oldest-arrived) first. Bounds the fetch itself, so it genuinely shortens a run. Does not limit how many already-known messages get their flags refreshed | none (no limit) |
| `max_actions` | Caps one run's mutations | none (uncapped) |
| `rules` * | Non-empty list — see below | — |

A task no longer names one `model:` — each of its rules' referenced
processors carries its own.

### `tasks.<name>.rules[]`

| Key | Description | Default |
| --- | --- | --- |
| `when` * | A condition tree — see [Rule conditions](#rule-conditions) below | — |
| `actions` * | Non-empty ordered list: `backup`, `trash`, `move_to:<mailbox>`, `label:<keyword>`, `task:<id>` | — |

A rule has no `id` and no `priority`: nothing else in the config ever refers
to a rule by name, so when more than one rule matches the same message, the
**first-listed** rule wins outright — reordering rules in the file *is*
reprioritizing them.

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
| `processor` | `"name.field op value"` (`"spam-category.value == spam"`, `"urgency.confidence >= 0.85"`) | the only condition a processor's answer is ever read through; see below |

The `processor:` atom reads one field off a named processor's answer:
`.value` (always present) or `.confidence` (always present for `jev`, only
present for a chat processor if its own declared schema happens to include
it — reading a field that was never produced evaluates to unresolved, not an
error). The operator is `==`/`!=` for a boolean or a declared option/level
name, or any of `==`/`!=`/`>=`/`<=`/`>`/`<` for a numeric comparand like
`confidence`. "One of several options" is `any:` over several equality
atoms, not a separate `in (...)` operator:

```yaml
any:
  - processor: "spam-category.value == digest"
  - processor: "spam-category.value == notification"
```

A `processor:` atom never counts as the deterministic condition `trash`
requires (see [Safety model](#safety-model)), and — unlike the retired `llm`
atom — there is no per-rule count cap and no restriction on appearing under
`not:`.

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

For "either of these," use `any:` — a list item can itself be `{any: [...]}`,
`{none: [...]}`, or `{not: {...}}`, nested up to 3 deep. A rule that's
fundamentally an OR at the top is a one-item list wrapping it: `when:
[{any: [...]}]`. A rule needing only one condition skips the list entirely —
`when: {older-than: 30d}` is exactly as terse as ever.

`none:` is "none of these" — the De Morgan mirror of `any:`: true only if
every child is false, false if any child is true. It exists mainly to save a
level of the depth-3 nesting budget over `not: {any: [...]}}`, which spends
two levels for the same idea:

```yaml
when:
  - none:
      - processor: "spam-category.value == spam"
      - older-than: 90d
```

There's no top-level `all:` keyword — `when: {all: [A, B]}` and `when: [A,
B]` meant the same thing, so only the list form is accepted at the top now.
`all` is still valid *nested*, e.g. inside an `any:`'s list to group several
conditions as one alternative: `any: [{all: [A, B]}, C]` reads as "(A and B)
or C".

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

### Actions and winner-takes-all

`backup`, `trash`, `move_to:<mailbox>`, `label:<keyword>`, `task:<id>`. When
more than one rule matches a message, the **first-listed** rule wins; the
rest are reported `shadowed`, not run. `task:<id>` (see [Chaining tasks
together](#chaining-tasks-together)) is local-only bookkeeping, never an
IMAP mutation, so it composes freely alongside a real mutation in the same
action list and never counts toward the one-remote-mutation-per-rule cap.

### Reading the message body

By default, a processor only ever sees message metadata (headers, sizes,
flags) — never the body. Setting `include_body: true` on a processor makes
its request always carry a bounded plain-text excerpt of the body — a
static property of that processor, not something a model's own uncertainty
triggers at runtime. Bear in mind this is the only path where message
*bodies* reach a model, and that each such request is its own un-batched
call plus a full-message fetch — so give `include_body: true` only to the
processors that actually need it, typically ones only a small, already
filtered-down set of candidates ever reach (see [Chaining tasks
together](#chaining-tasks-together)).

The verdict is cached like any other, keyed on the processor's own
definition, so a given message's body is never re-fetched or re-asked once
answered. A processor's answer is either cached or the call outright failed
(transport/parse error) — there is no separate "unsure" state to defer on.

### Chaining tasks together

A rule's action list can include `task:<id>`, which hands the candidate to
another task's pool without moving or labelling it in the mailbox at all —
purely local, idempotent bookkeeping. This is what makes a two-stage
pipeline affordable: a cheap, unconditional first pass (typically a `jev`
processor) either resolves the common case outright or routes the
remainder to a second task, and only that routed subset ever reaches a
slower, more expensive processor. See
[`config.example.yaml`](config.example.yaml) for the full pattern.

A task can omit `source_mailboxes` entirely if its whole candidate pool
arrives via `task:<id>` routing from elsewhere — `config check` rejects a
task with neither a mailbox to scan nor any `task:` action naming it, since
it could never run at all.

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
be counting different things behind identical-looking numbers. Every slow
phase is counted: fetching new mail and refreshing flags during the scan,
classification, body-excerpt fetching for any `include_body: true`
processor, and execution. A body fetch plus its processor call is the
slowest per message, since each one costs its own un-batched model call.

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
