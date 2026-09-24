# AGENTS.md

Instructions for any agent (or human) working in this repository. Liametahi
is a local, cron-friendly Python CLI that cleans up an IMAP mailbox, using a
model only as a constrained classifier — never as the thing that decides or
acts. If anything here conflicts with the documents it points to, those
documents win; this file is a map and a set of reminders, not a new source of
truth.

## Where things live

| Path | What it is | Authoritative for |
| --- | --- | --- |
| `README.md` | User-facing install/quickstart/CLI usage | End-user behavior and docs |
| `docs/configuration.md` | Full `config.yaml` key reference: accounts, models, processors, tasks, rule conditions | Config file shape and defaults |
| `docs/internals.md` | Expanded reasoning behind each safety-model bullet | Why the safety properties exist |
| `docs/development.md` | Checks to run, test tiers, live-mailbox testing workflow | Dev workflow |
| `dev-notes/specification.md` | Full product/behavior spec, numbered acceptance criteria (§14), resolved decisions (§15) | **What** the tool does and why — read this first for any behavior question |
| `dev-notes/implementation-contracts.md` | Toolchain, SQLite DDL, module signatures, work-unit boundaries, test-gate commands | **Shape** of the implementation — read this first for any "how is this wired together" question |
| `dev-notes/jev-provider-plan.md` | The `processors`/Jev redesign brief (chat + Jev classification unified, `task:<id>` routing) | Authoritative for that redesign's shape until folded fully into `specification.md` (in progress — §2/§3/§11/§13–§15 of the spec still describe the pre-redesign `llm:`/rule-id model) |
| `dev-notes/jev-provider-design-history.md` | Alternatives considered and rejected during that redesign, with reasons | Background only — nothing here is current design |
| `dev-notes/*.md` (others) | Historical/working notes | Background only |
| `dev-notes/archive/*.md` | Fully superseded docs kept for history only (e.g. `mailbox-cleanup-cli.md`, superseded by `specification.md`) | Not current — do not read for behavior/shape questions |
| `src/liametahi/` | The package (see module map below) | — |
| `tests/` | Unit tests (default), integration tests (`-m integration`, needs Docker/Dovecot), live tests (`-m live`, opt-in, real mailbox, never in CI) | — |
| `tools/` | `capture_corpus.py` (pull a real mailbox into a local synthetic corpus, read-only) and `dev_imap.py` (disposable local Dovecot for integration/live testing) | — |
| `config.example.yaml` | Template a user copies to `~/.config/liametahi/config.yaml` or `./config.yaml` | Quickstart |

When `dev-notes/specification.md` and `dev-notes/implementation-contracts.md`
disagree: the specification wins on behavior, the contracts document wins on
shape (stated explicitly at the top of the contracts file).

## Module map (`src/liametahi/`)

- `cli.py` — Typer CLI: argument parsing, config path resolution, exit codes,
  printing. Delegates every decision of substance elsewhere.
- `config.py` — Pydantic v2 models, YAML load-time validation, config file
  ownership/permission checks, condition-tree (`when:`) grammar parsing.
- `rules.py` / `policy.py` — condition tree types (including `processor:`
  atoms and the `all`/`any`/`none`/`not` composition keywords) and
  rule-matching ("winner takes all" by plain list order — rules have no id
  or priority field; the first matching rule wins) semantics.
- `imap_adapter.py` — IMAP protocol wrapper (fetch, claim, mutate).
- `classifier/` — `anthropic.py`, `openai_compatible.py`, and `jev.py`
  adapters behind a shared interface in `__init__.py`. A model only ever
  answers a named **processor**'s declared question (`noul`/`choice`/
  `score`) against its own closed vocabulary of options/levels — never
  free-form, never widening what a rule may act on. See
  `dev-notes/jev-provider-plan.md` for the full design.
- `evaluate.py` / `execute.py` — the two run phases: evaluate (classify,
  decide, cache) and execute (backup, mutate).
- `runner.py` — orchestrates a full task run across both phases, locking,
  and crash recovery.
- `backup.py` — content-addressed `.eml` backup + restore.
- `state.py` — SQLite schema/access (see contracts doc for DDL).
- `locks.py` — per-task advisory locks (`--wait`, exit `5` on contention).
- `report.py` — renders a stored run without touching the mailbox or model.
- `progress.py`, `logging.py`, `prompt.py`, `domain.py` — progress UI,
  redacting logger, LLM prompt construction, and core domain types.

## Conventions (binding, not stylistic preference)

- **`uv` for everything** — `uv add`, `uv run`, `uv sync`. Never `pip`.
- **Full type annotations**; `mypy --strict` covers `src/liametahi` *and*
  `tests` (test doubles implement the same protocols as real adapters, so a
  drifted signature is caught there).
- Full gate before calling anything done (`docs/development.md`):
  ```sh
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy --strict src/liametahi tests
  uv run pytest
  ```
- **Never commit real credentials, real email content, or a captured
  corpus.** `config.yaml`, `liametahi.yaml`/`.yml`, `*.local.yaml`, `*.eml`,
  and captured `tests/corpus/*/` (other than the hand-written
  `tests/corpus/synthetic/`) are gitignored on purpose — don't work around it.
- The config file is a secret (spec §12): wrong ownership is a hard failure
  (exit 2); group/world-readable mode bits print a warning but still load.
- Safety properties from the spec (`BODY.PEEK` everywhere, one remote
  mutation per message, claim-before-mutate, protected senders/flags
  re-checked immediately before mutation, verify-then-act, and `trash`
  always requiring at least one deterministic — non-`processor:` —
  condition) are non-negotiable — implement them exactly even where a
  simpler shape would pass the tests. (Backup-before-trash is *not* in this
  list: it's deliberately no longer required, per
  `dev-notes/jev-provider-plan.md` §8 — `backup` is still available and
  freely composable, just optional.) A model's output is untrusted,
  attacker-influenced input: validate every processor answer against its
  own declared options/levels, never let it widen what an action may do.

## Specialized agents in this repo

- `liametahi-impl` (`.claude/agents/liametahi-impl.md`) implements a work
  unit against the spec and contracts docs.
- `liametahi-verify` (`.claude/agents/liametahi-verify.md`) is a read-only
  auditor that checks implemented code against those same docs.

Both were written before `dev-notes/` was renamed from `notes/`; their
"read these first" paths have been corrected to `dev-notes/specification.md`
and `dev-notes/implementation-contracts.md`. If you add a new agent for this
repo, point it at the `dev-notes/` paths directly.
