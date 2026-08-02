# How Liametahi works

Background for anyone changing the code, or deciding whether to trust it with
a real mailbox. The [README](../README.md) is enough to use the tool.

## A run, end to end

A run is three phases, in order, and the ordering is load-bearing.

**1. Scan.** Acquire the per-task advisory lock, reconcile anything a crashed
previous run left half-done, then connect and `EXAMINE` each source mailbox
read-only. Already-tracked UIDs are excluded locally before any fetch — IMAP
`SEARCH` cannot express "not in my database" — so metadata is fetched only for
genuinely new messages, oldest UID first, bounded by `max_new_mails`. Flags
for messages already known are refreshed in the same pass. Every fetch uses
`BODY.PEEK`, so scanning never marks anything read. Then disconnect.

**2. Evaluate.** No mailbox connection is open for this phase, so a slow model
never holds a socket. Protected messages are filtered out first and can never
reach a classifier at all. Each rule's condition tree is evaluated with
three-valued logic: fully true matches immediately, false is eliminated, and
only `unknown` — meaning an `llm` condition remains — goes to the model, in
batches, after the decision cache has removed everything already answered.

Those batches are independent of one another, so `max_concurrent_requests` may
put several in flight at once — on a first run over a large mailbox this is the
single biggest thing available, since nearly all of the phase's wall clock is
spent waiting on a provider. It defaults to `1` because the safe value is a
property of your provider's rate limit, which Liametahi cannot discover. Raising
it never changes what a run decides: results are recorded in batch order, not
completion order, and every database write stays on the one thread that owns the
connection, so the stored rows and the printed report come out identical at any
setting.

**3. Execute.** Reconnect, and for each matched message: claim its key
atomically, re-fetch and re-verify it has not changed, re-check flag-based
protection against those fresh flags, then run the winning rule's actions in
order. A `--dry-run` performs every step except the mutations.

The re-fetch is where this phase spends its time — against a remote server
it is one network round trip multiplied by every matched message — so it is
kept to the minimum that is still safe. The mailbox is selected once and
reused for as long as consecutive messages share it, rather than re-selected
per message; the re-verify fetch also carries the message body when the
winning rule is going to back it up, instead of downloading it again a moment
later. What is deliberately *not* skipped is the existence check before a
`MOVE`: IMAP accepts a `MOVE` against an empty match set without complaint,
so dropping it would let a message that vanished in the last instant be
recorded as successfully moved.

## Why each safety rule exists

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
  cache](#the-decision-cache)).

## How messages are tracked

Liametahi keeps a local mirror of what it has seen, because the alternative —
judging only freshly-scanned mail — cannot work: a message that misses
`older-than: 30d` today has to be re-judged on day 31, and something seen once
and never revisited could never age into a match.

Each tracked message carries a **fingerprint**: `sha256(message_id |
internaldate | size)`, or `sha256(from | subject | internaldate | size)` when
there is no `Message-ID`. A raw `Message-ID` is not enough on its own — it is
absent on some mail and is a header the sender controls, so it is combined
with values the server assigns.

A tracked message is **retired**, and excluded from every later run, once
there is nothing left to do with it: a `trash`/`move_to` completed, the
server confirmed it gone, or it was skipped as a previously-trashed message.
A completed `label` does *not* retire it — the message is still there and may
match another rule later — and neither does a failure, since the point is for
the next run to retry.

`max_new_mails` bounds how many *new* messages a run fetches. It does not
bound how many known messages are re-checked, and it cannot: re-checking them
is what makes time-based rules fire and keeps flags honest.

## The decision cache

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

## Identifiers

Run and backup ids are 10 characters of Crockford base32, fully random and
unprefixed — `4w8wbbs3fs`. They are deliberately *not* time-sortable: nothing
orders by id (every listing sorts by timestamp), so a timestamp prefix would
only make the string longer while making short prefixes useless, since two
time-prefixed ids created minutes apart share their leading characters. Fully
random means every character discriminates, which is what makes an
unambiguous prefix usable anywhere a full id is.

Crockford's alphabet omits `i`, `l`, `o` and `u`, so there is no `1`/`l` or
`0`/`O` confusion reading one off a terminal — and because the alphabet is
closed, prefix lookup can reject any other character before it builds a
query.

## Storage

One SQLite database, WAL mode, `synchronous = FULL`. Bookkeeping writes
(tracked messages, classifications, cache entries, no-op result rows) are
grouped into one transaction per phase. The `action_attempts` state machine
and the backup manifest are deliberately excluded and stay one durable
transaction per statement: the reconcile pass can only tell how far a crashed
run got because each transition was committed as it happened.

Backups are content-addressed `.eml` files, written to a temporary file,
fsynced, checksummed, then atomically renamed, with the manifest row
committed last. `restore` verifies the checksum before appending anything
back.

The database, lock directory, and backup directory must be on a local
filesystem with working advisory locks and atomic rename. Network
filesystems are not supported — SQLite's WAL mode in particular requires
shared memory that network mounts do not provide.
