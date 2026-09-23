# Liametahi

An AI-powered mailbox cleanup tool for IMAP. Point it at an inbox and it
can:

- Trash, move, label, or route mail using both plain conditions (sender,
  age, size, headers) and an AI's answer to a question you write ("is this
  spam?", "how urgent is this?")
- Preview any run with `--dry-run`, back up before deleting, and restore
  anytime
- Run unattended on a schedule — locking, crash recovery, and a report of
  every past run
- Mix a fast/cheap model with a slower one, only escalating the mail the
  first pass is unsure about

```yaml
processors:
  vibe-check:
    model: local
    type: noul
    instructions: A newsletter or digest with nothing time-sensitive left in it.

tasks:
  inbox-cleanup:
    account: personal
    source_mailboxes: [INBOX]
    rules:
      - when:
          - older-than: 30d
          - processor: "vibe-check.value >= 0.9"
        actions: [backup, trash]
```

## Install

Needs Python ≥ 3.14, [`uv`](https://docs.astral.sh/uv/), an IMAP account, and
a model endpoint — OpenAI-compatible (including a local
[llama.cpp](https://github.com/ggml-org/llama.cpp) server), Anthropic,
OpenRouter, or `jev`.

```sh
uv sync
uv run liametahi --help
```

## Usage

```
liametahi config check [--connect]
liametahi run TASK [--dry-run] [--fail-fast] [--reevaluate] [--wait SECONDS] [--format table|json] [--verbose]
liametahi report [RUN_ID] [--list] [--task TASK] [--format table|json] [--verbose]
liametahi restore BACKUP_ID --mailbox MAILBOX [--account NAME] [--dry-run]
```

`--config PATH` works on every subcommand. Run/backup ids are short strings
(`4w8wbbs3fs`); type any unambiguous leading prefix. `run` draws a live
progress bar on an interactive terminal; under cron/CI it just logs phase
boundaries.

Exit codes: `0` ok, `1` runtime/partial failure, `2` bad config, `3`
interrupted, `4` auth failure, `5` task already running — cron-safe:
`liametahi run TASK || [ $? -eq 5 ]`.

## Quickstart

```sh
cp config.example.yaml config.yaml
$EDITOR config.yaml    # at least an account and a model
chmod 600 config.yaml

uv run liametahi config check --connect
uv run liametahi run inbox-classify --dry-run --verbose  # preview, no mutation
uv run liametahi run inbox-classify                      # for real
uv run liametahi report                                  # review the last run
uv run liametahi restore 4w8w --mailbox INBOX            # undo a trash
```

`./config.yaml` in the current directory is picked up automatically — no
flag needed. The example ships two tasks wired together with a `jev` model
and `task:` routing; see [docs/configuration.md](docs/configuration.md) for
every key, other config locations (`--config`, `$LIAMETAHI_CONFIG`,
`~/.config/liametahi/config.yaml`), and how conditions/rules compose.

## Documentation

- [docs/configuration.md](docs/configuration.md) — full config key
  reference: accounts, models, processors, tasks, rule conditions.
- [docs/internals.md](docs/internals.md) — safety model, run phases,
  decision cache, and the reasoning behind each guarantee.
- [docs/development.md](docs/development.md) — test tiers, running the
  suite, and live-mailbox testing.
