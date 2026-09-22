# Configuration reference

Full key-by-key reference for `config.yaml`. See the [README](../README.md)
for how to bootstrap one and the CLI to run it.

`*` marks a required key; everything else falls back to its default.

## `settings`

| Key | Description | Default |
| --- | --- | --- |
| `state_db` | Path to the SQLite state database | `<platform data dir>/liametahi/state.sqlite3` |
| `backup_dir` | Directory verified message backups are written to | `<platform data dir>/liametahi/backups` |
| `task_lock_dir` | Directory for per-task advisory lock files | `<platform data dir>/liametahi/locks` |
| `log_file` | Also write logs here, in addition to stderr | none |
| `log_level` | `debug` / `info` / `warning` / `error` | `info` |

## `accounts.<name>`

| Key | Description | Default |
| --- | --- | --- |
| `host` * | IMAP server hostname | — |
| `port` | IMAP port | `993` |
| `username` * | IMAP login username | — |
| `password` * | IMAP login password — a literal secret | — |
| `trash_mailbox` | Required only if a task on this account has a `trash` action | none |
| `tls_insecure_skip_verify` | Skip certificate verification — loopback hosts only | `false` |

## `models.<name>`

| Key | Description | Default |
| --- | --- | --- |
| `provider` * | `openai_compatible`, `anthropic`, or `jev` | — |
| `base_url` * | Required for `openai_compatible`/`jev`; full endpoint URL | — |
| `model` * | The provider's model identifier | — |
| `api_key` | Required for `anthropic`/`jev`; optional for local `openai_compatible` | none |
| `extra_headers` | Extra HTTP headers merged into every request | `{}` |
| `structured_output` | `auto` / `json_schema` / `json_object` / `none` — ignored for `jev` | `auto` |
| `mails_per_request` | Batch size per classification call. **Must be `1` for `jev`** | `10` |
| `max_concurrent_requests` | Classification requests in flight at once — the main speed lever | `1` |
| `timeout_seconds` | Per-request HTTP timeout | `45` |
| `max_retries` | Transport-error retries; `jev` also retries `429`/`529` | `2` |
| `body_excerpt.format` | Excerpt format for a processor with `include_body: true` | `plain_text_excerpt` |
| `body_excerpt.max_chars` | Truncate each body excerpt to this many characters | none |

OpenRouter is `provider: openai_compatible` with
`base_url: https://openrouter.ai/api/v1/chat/completions` and `model` set to
its namespaced id (`vendor/model`).

## `processors.<name>`

One named question, answered by whichever model it references. Whether a
processor runs at all is derived from which rules reference it (see
`processor:` below) and `task:<id>` routing — there's no separate gate here.

| Key | Description | Default |
| --- | --- | --- |
| `model` * | Must name an entry in `models` | — |
| `type` * | `noul` (boolean), `choice` (named options), or `score` (ordered levels) | — |
| `question` | Framing text sent to the model. For `noul`, a plain `question` with no `criteria` also implies `criteria: {"true": question}` | — |
| `criteria` | Required for `noul` unless `question` is used; exactly the keys `"true"`/`"false"` | — |
| `options` | Required for `choice`; option name → description, at most 255 entries | — |
| `levels` | Required for `score`; ordered list of level names, 2–10 entries | — |
| `include_body` | Always includes a bounded plain-text body excerpt in this processor's request | `false` |

## `tasks.<name>`

| Key | Description | Default |
| --- | --- | --- |
| `account` * | Must name an entry in `accounts` | — |
| `source_mailboxes` | Mailboxes to scan, in order. May be omitted if the task is only a `task:<id>` routing target | `[]` |
| `protect.flags` | IMAP flags that exempt a message | `[]` |
| `protect.senders` | Sender globs that exempt a message | `[]` |
| `protect.unread` | Exempt unread messages | `false` |
| `max_new_mails` | Fetch at most this many not-yet-seen messages per run (oldest first) | none |
| `max_actions` | Caps one run's mutations | none |
| `rules` * | Non-empty list — see below | — |

## `tasks.<name>.rules[]`

| Key | Description | Default |
| --- | --- | --- |
| `when` * | A condition tree — see below | — |
| `actions` * | Non-empty ordered list: `backup`, `trash`, `move_to:<mailbox>`, `label:<keyword>`, `task:<id>` | — |

Rules have no `id`/`priority`: they're a plain list, and when more than one
matches, the **first-listed** wins — the rest are reported `shadowed`.
`task:<id>` hands a candidate to another task's pool without touching the
mailbox; it's local-only bookkeeping, so it composes freely with a real
mutation in the same action list.

## Rule conditions

| Condition | Plain-form default | Notes |
| --- | --- | --- |
| `older-than` / `newer-than` | duration (`30d`, `12h`) | against `INTERNALDATE` |
| `sender-match` | glob | `From` address, whole-address match |
| `recipient-match` | glob | true if **any** of `To`/`Cc`/`Delivered-To`/`X-Original-To` matches |
| `subject-contains` | substring | anywhere in the subject |
| `list-id-contains` | substring | against `List-Id` |
| `has-header` | header name | present and non-empty |
| `has-flag` | IMAP flag/keyword | exact match |
| `in-mailbox` | mailbox name | case-sensitive except `INBOX` |
| `larger-than` | size (`500k`, `2M`) | against `RFC822.SIZE` |
| `recipient-count` | comparison (`>10`, `<=3`) | against the same recipient union as `recipient-match` |
| `has-attachment` | `true` | heuristic from `BODYSTRUCTURE`; `false` is rejected, use `not: {has-attachment: true}` |
| `auth-result` | `mechanism=result` (`spf=fail`) | mechanism is `spf`/`dkim`/`dmarc`; only the topmost header is checked |
| `processor` | `"name.field op value"` (`"spam-category.value == spam"`) | `.field` is `value` or `confidence` |

`-match` conditions default to **glob** (`sender-match: bank.example` matches
only that exact address); `-contains` conditions default to **substring**.
Both also accept `/regex/flags` (`i`/`m`/`s`/`g`), compiled at load time —
avoid patterns vulnerable to catastrophic backtracking, since these run
against sender-controlled input.

A `processor:` atom's `.confidence` only resolves for a `jev`-backed
processor (rejected at config-load time otherwise); "one of several
options" is `any:` over equality atoms, not an `in (...)` operator. A
`processor:` atom is never sufficient by itself to license `trash` — a rule
that trashes needs at least one other, deterministic atom.

## Combining conditions

`when:` is a list, **implicitly ANDed**:

```yaml
when:
  - older-than: 30d
  - not: { subject-contains: buz }
```

`any:`/`none:`/`not:` compose, nested up to 3 deep — `none:` is "none of
these," the De Morgan mirror of `any:`. There's no top-level `all:` (a plain
list already means AND), but `all:` nests inside `any:`/`none:` to group a
sub-condition: `any: [{all: [A, B]}, C]` reads "(A and B) or C".

## Reading the message body

A processor sees only metadata by default. `include_body: true` makes its
request always carry a bounded plain-text excerpt — a static property of
the processor, not a runtime escalation — so reserve it for processors that
only reach an already-filtered-down subset of mail (see `task:<id>`
chaining below). Each such call is its own un-batched request plus a full
message fetch.

## Chaining tasks together

A rule's `task:<id>` action hands a candidate to another task's pool
without any mailbox mutation. This is how a cheap, unconditional first pass
(often `jev`) can resolve the common case outright and route only the
uncertain remainder to a slower, more expensive processor. A task may omit
`source_mailboxes` if its whole pool arrives this way — but it must be
reachable from somewhere, or `config check` rejects it.
