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
a model endpoint — an OpenAI-compatible chat API, Anthropic, or `jev`.

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
(`4w8wbbs3fs`); type any unambiguous leading prefix (eg. `4w8b`).

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

See [docs/configuration.md](docs/configuration.md) for every key and how
conditions/rules compose. Config file location, first match wins:

1. `--config PATH`
2. `$LIAMETAHI_CONFIG`
3. `$(pwd)/config.yaml`
4. `~/.config/liametahi/config.yaml` (Linux); `~/Library/Application Support/liametahi` (macOS); `%LOCALAPPDATA%\liametahi` (Windows)

## Documentation

- [docs/configuration.md](docs/configuration.md) — full config key
  reference: accounts, models, processors, tasks, rule conditions.
- [docs/internals.md](docs/internals.md) — safety model, run phases,
  decision cache, and the reasoning behind each guarantee.
- [docs/development.md](docs/development.md) — test tiers, running the
  suite, and live-mailbox testing.
