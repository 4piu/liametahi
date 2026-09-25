# Configuration reference

Full key-by-key reference for `config.yaml`. See the [README](../README.md)
for how to bootstrap one and the CLI to run it.

## File location

Checked in this order; the first one found is used as-is, with no merging
between them:

1. `--config PATH`
2. `$LIAMETAHI_CONFIG`
3. `./config.yaml` — a project-level config for a per-checkout profile
4. `~/.config/liametahi/config.yaml` (platform-appropriate equivalent
   elsewhere) — the user-level default

The file holds literal credentials: it must be owned by you (a hard
failure otherwise), and `config check` warns — but still loads — if it's
group- or world-readable.

`*` marks a required key below; everything else falls back to its default.
Any key not listed in these tables is rejected at load time (every config
object forbids unknown fields).

## `settings`

| Key | Type | Description | Default |
| --- | --- | --- | --- |
| `state_db` | path | Path to the SQLite state database | `<platform data dir>/liametahi/state.sqlite3` |
| `backup_dir` | path | Directory verified message backups are written to | `<platform data dir>/liametahi/backups` |
| `task_lock_dir` | path | Directory for per-task advisory lock files | `<platform data dir>/liametahi/locks` |
| `log_file` | path or unset | Also write logs here, in addition to stderr | none (stderr only) |
| `log_level` | string: `debug` \| `info` \| `warning` \| `error` | Log verbosity | `info` |

## `accounts.<name>`

| Key | Type | Description | Default |
| --- | --- | --- | --- |
| `host` * | non-empty string | IMAP server hostname | — |
| `port` | integer, 1–65535 | IMAP port | `993` |
| `username` * | non-empty string | IMAP login username | — |
| `password` * | non-empty string | IMAP login password — a literal secret | — |
| `trash_mailbox` | string or unset | Required only if a task on this account has a `trash` action | none |
| `tls_insecure_skip_verify` | boolean | Skip certificate verification — rejected at load time for any host other than a loopback address (`127.0.0.1`, `::1`, `localhost`) | `false` |

## `models.<name>`

| Key | Type | Description | Default |
| --- | --- | --- | --- |
| `provider` * | string: `openai` \| `anthropic` \| `systemone` | Which adapter handles this model | — |
| `base_url` * | string (URL) | Required for `openai`/`systemone`; the complete endpoint URL, posted to as-is | — |
| `model` * | non-empty string | The provider's model identifier | — |
| `api_key` | string or unset | Required for `anthropic`/`systemone`; optional for a local `openai` server | none |
| `extra_headers` | map of string to string | Extra HTTP headers merged into every request | `{}` |
| `structured_output` | string: `auto` \| `json_schema` \| `json_object` \| `none` | Ignored for `systemone`, which has no separate structured-output negotiation | `auto` |
| `mails_per_request` | integer, ≥ 1 | Batch size per classification call. **Must be exactly `1` for `systemone`** — enforced at load time | `10` |
| `max_concurrent_requests` | integer, ≥ 1 | Classification requests in flight at once — the main speed lever | `1` |
| `timeout_seconds` | integer, > 0 (seconds) | Per-request HTTP timeout | `45` |
| `max_retries` | integer, ≥ 0 | Transport-error retries; `systemone` also retries `429`/`529` | `2` |
| `body_excerpt.format` | string: `plain_text_excerpt` | The **only** value currently supported — reserved for future formats, not a live choice today | `plain_text_excerpt` |
| `body_excerpt.max_chars` | integer, > 0, or unset | Truncate each body excerpt to this many characters. `0` is rejected; omit the key entirely for no limit | none (no limit) |

`provider: systemone` targets the `POST /v1/systemone` wire protocol —
TypeSafe AI's Jev is the reference implementation, but any service
speaking the same request/response shape at its own `base_url` works.

OpenRouter is `provider: openai` with
`base_url: https://openrouter.ai/api/v1/chat/completions` and `model` set to
its namespaced id (`vendor/model`).

## `processors.<name>`

One named question, answered by whichever model it references. Whether a
processor runs at all is derived from which rules reference it (see
`processor:` below) and `task:<id>` routing — there's no separate gate here.

| Key | Type | Description | Default |
| --- | --- | --- | --- |
| `model` * | non-empty string | Must name an entry in `models` | — |
| `type` * | string: `noul` \| `choice` \| `score` | `noul` = a calibrated probability; `choice` = one of several named options; `score` = one of an ordered list of levels | — |
| `instructions` * | non-empty string | Natural-language question sent to the model, alongside `criteria` | — |
| `criteria` | shape depends on `type` — see below | The processor's vocabulary | — (required for `choice`/`score`; optional for `noul`) |
| `fields` | list of field names — see [Reading the message body](#reading-the-message-body) | Exactly which candidate metadata (and, if listed, body text) this processor's request carries | the default profile below |

`criteria`'s type and constraints depend on `type`:

| `type` | `criteria` type | Constraints |
| --- | --- | --- |
| `noul` | map of string to string, or unset | If given, exactly the two keys `"true"` and `"false"` — any other key set, or a partial set, is rejected |
| `choice` | map of string to string | Non-empty, at most 255 entries (jev's own limit); each key is a legal option name |
| `score` | ordered list of strings | Between 2 and 10 entries; order matters (low to high) |

`instructions` and each `criteria` description must be plain strings — jev's
real API also accepts a JSON object or array here for richer structured
questions, but this project doesn't support that form yet; a list or map
where a string is expected is rejected at config-load time.

## `tasks.<name>`

| Key | Type | Description | Default |
| --- | --- | --- | --- |
| `account` * | non-empty string | Must name an entry in `accounts` | — |
| `source_mailboxes` | list of strings | Mailboxes to scan, in order. May be omitted if the task is only a `task:<id>` routing target | `[]` |
| `protect.flags` | list of strings | IMAP flag/keyword names that exempt a message. The six standard system flags (`\Seen`, `\Answered`, `\Flagged`, `\Deleted`, `\Draft`, `\Recent`) match case-insensitively; any other (custom keyword) matches exact-case | `[]` |
| `protect.senders` | list of strings | Each entry is a bare address or a domain, matched case-insensitively as an **exact address or domain suffix** — not a glob. `bank.example` matches `alerts@bank.example` and `x.bank.example`, but not `notbank.example` | `[]` |
| `protect.unread` | boolean | Exempt unread messages (`\Seen` absent) | `false` |
| `max_new_mails` | integer, > 0, or unset | Fetch at most this many *not-yet-seen* messages per run, lowest UID (oldest-arrived) first. `0` is rejected; omit the key for no limit | none (no limit) |
| `max_actions` | integer, > 0, or unset | Caps one run's mutations. `0` is rejected; omit the key for no cap | none (uncapped) |
| `rules` * | non-empty list of rule objects | See below | — |

## `tasks.<name>.rules[]`

| Key | Type | Description | Default |
| --- | --- | --- | --- |
| `when` * | condition tree (a map, or a list of maps — see [Rule conditions](#rule-conditions)) | What has to be true for this rule to match | — |
| `actions` * | non-empty list of strings | Each one of `backup`, `trash`, `move_to:<mailbox>`, `label:<keyword>`, `task:<id>` | — |

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
| `has-flag` | IMAP flag/keyword | the six system flags match case-insensitively, same as `protect.flags`; any other keyword matches exact-case |
| `in-mailbox` | mailbox name | case-sensitive except `INBOX` |
| `larger-than` | size (`500k`, `2M`) | against `RFC822.SIZE` |
| `recipient-count` | comparison (`>10`, `<=3`) | against the same recipient union as `recipient-match` |
| `has-attachment` | `true` | heuristic from `BODYSTRUCTURE`; `false` is rejected, use `not: {has-attachment: true}` |
| `auth-result` | `mechanism=result` (`spf=fail`) | mechanism is `spf`/`dkim`/`dmarc`; only the topmost header is checked |
| `processor` | `"name.field op value"` (`"spam-category.value == spam"`, `"stale-junk.value >= 0.9"`) | `.field` is `value` or `confidence` |

Every condition's value is a string in YAML, but each parses to a specific
type: `older-than`/`newer-than`/`larger-than` parse to a number of
seconds/bytes internally; `recipient-count` and a numeric `processor:`
comparand parse to an `int` or `float` (decimals allowed, e.g. `>=0.85`);
everything else is compared as text (case-sensitivity noted per condition
above). A malformed value (an unparseable duration or size, an unknown
`auth-result` mechanism, `has-attachment: false`, ...) is a load-time
`ConfigError`, never a silently-`false` condition.

`-match` conditions default to **glob** (`sender-match: bank.example` matches
only that exact address); `-contains` conditions default to **substring**.
Both also accept `/regex/flags` (`i`/`m`/`s`/`g`), compiled at load time —
avoid patterns vulnerable to catastrophic backtracking, since these run
against sender-controlled input.

A `processor:` atom's `.confidence` resolves for a `choice`/`score`
processor on any backend — `systemone` populates it natively; a chat-backed
processor is asked for the chosen answer's own probability and the
runner-up's, and the gap between them becomes `.confidence`, rather than
a bare self-reported number. It's never valid at all against a `noul`
processor, on any backend (rejected at config-load time) — its single
probability `.value` already describes it completely. A `noul`
processor's `.value` is a probability in `[0, 1]`, so its comparand must
be numeric — a string or boolean comparand (the old `.value == true`
style) is rejected at config load. "One of several options" is `any:`
over equality atoms, not an `in (...)` operator. A `processor:` atom is
never sufficient by itself to license `trash` — a rule that trashes needs
at least one other, deterministic atom.

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

Every processor sees a chosen subset of candidate metadata, named by its
`fields:` list. Omitting `fields:` entirely applies the default profile:

```yaml
fields: [from.address, from.display_name, subject, mailbox, list_id,
         has_list_unsubscribe, has_feedback_id, reply_to, has_attachment,
         cc_count]
```

Setting `fields:` at all is a **full replace, not a merge** — exactly the
listed names and nothing else, the same convention `criteria`/`options`
already use elsewhere in this config. Extending the default by one field
means listing all of it plus the addition (`config.example.yaml`'s
`spam-review` processor does exactly this).

Every name below is legal in `fields:`; an unrecognised name is a
config-load error, not a silent no-op.

| Field | Type | Notes |
| --- | --- | --- |
| `from.address` | string or null | `From` address |
| `from.display_name` | string or null | `From` display name |
| `to` | list of strings | `To`/`Cc`/`Delivered-To`/`X-Original-To` union, capped at 5 entries |
| `cc_count` | integer | `Cc` header count, plus any overflow beyond `to`'s cap |
| `recipient_count` | integer | total recipient union count, uncapped |
| `subject` | string or null | `Subject` header |
| `mailbox` | string | source mailbox name |
| `list_id` | string or null | `List-Id` header |
| `has_list_unsubscribe` | boolean | `List-Unsubscribe` presence |
| `has_attachment` | boolean | heuristic from `BODYSTRUCTURE`, same one the `has-attachment` rule condition uses |
| `size` | integer | `RFC822.SIZE`, bytes |
| `reply_to` | string or null | `Reply-To` header |
| `has_feedback_id` | boolean | `Feedback-ID` presence (ESP complaint-loop marker) |
| `sender` | string or null | `Sender` header, distinct from `From` |
| `precedence` | string or null | raw `Precedence` value (`bulk`/`list`/`junk`/...) |
| `is_auto_submitted` | boolean | `Auto-Submitted` presence |
| `has_auto_response_suppress` | boolean | `X-Auto-Response-Suppress` presence |
| `is_reply` | boolean | `In-Reply-To` or `References` presence |
| `body_shape` | string: `both` \| `html_only` \| `plain_only` \| `neither` | which of `text/plain`/`text/html` the message has, from `BODYSTRUCTURE` alone — no body fetch |
| `excerpt` | string | a bounded, cleaned plain-text body excerpt (prefer `text/plain`, else strip `text/html`) |
| `html` | string | the raw, unstripped `text/html` part, or empty if none |

Only `excerpt` and `html` trigger the extra per-candidate body fetch
(`BODY.PEEK[]`) — every other field, `body_shape` included, comes from the
scan phase's existing metadata fetch, no extra round trip. Selecting
`excerpt`/`html` is a static property of the processor, not a runtime
escalation — so reserve it for processors that only reach an
already-filtered-down subset of mail (see `task:<id>` chaining below). Each
such call is its own un-batched request plus a full message fetch. See
[docs/internals.md](internals.md#what-a-model-sees) for how this fits into
the wider payload shape.

## Chaining tasks together

A rule's `task:<id>` action hands a candidate to another task's pool
without any mailbox mutation. This is how a cheap, unconditional first
pass (often `provider: systemone`) can resolve the common case outright
and route only the uncertain remainder to a slower, more expensive
processor. A task may omit
`source_mailboxes` if its whole pool arrives this way — but it must be
reachable from somewhere, or `config check` rejects it.
