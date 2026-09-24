-- Schema version 5.
--
-- The `processors:` redesign retires the single-`llm`-atom, per-rule-id
-- classification scheme in favour of any number of independently-named
-- `processor:` atoms, each backed by its own model and answering with a
-- resolved `value` (bool/choice-string/score-level-string) plus an
-- optional `confidence`. There is no meaningful mapping from an old
-- rule's free-text `llm` description (or its `rule_id`, which no longer
-- exists -- rules have no id at all any more) to a new named processor,
-- so this migration drops and recreates the two tables keyed on rule
-- identity rather than transforming rows: a one-time re-classification
-- cost on upgrade, not a correctness question (no backward compatibility
-- or migration tooling for this redesign, by design).
--
-- 1. `llm_decision_cache`: `rule_id`/`rule_text_hash` -> `processor_name`/
--    `processor_hash`; the single `matched INTEGER` column (previously
--    "non-matches only; a match is never cached" per the original design,
--    later widened to store either polarity) becomes `value_json TEXT
--    NOT NULL` (JSON-encoded bool/str/float, matching whatever type the
--    processor's `type` produces) plus a nullable `confidence REAL`.
--
-- 2. `classifications`: `offered_rules`/`matches` (JSON arrays of rule
--    ids) become `offered_processors`/`processor_answers` (JSON array of
--    processor names / JSON object mapping a processor name to its
--    resolved `{"value": ..., "confidence": ...}`). `needs_content` is
--    dropped entirely: the yes/no/unsure vocabulary it supported no
--    longer exists -- a processor either answers (with whatever
--    confidence it reports) or it doesn't.
--
-- 3. `task_routes`: brand new (the `task:<id>` action) -- no prior data
--    to reconcile. Idempotent on (account_id, fingerprint, target_task),
--    matching the existing claim-not-steal/idempotent-upsert style of
--    `key_claims`: a rule matching the same candidate again on a later
--    run writes no second row. Never visible on the IMAP server -- purely
--    local bookkeeping read by the target task's next run to extend its
--    candidate pool beyond its own `source_mailboxes` scan.

DROP TABLE llm_decision_cache;

CREATE TABLE llm_decision_cache (
  account_id      INTEGER NOT NULL REFERENCES accounts(account_id),
  fingerprint     TEXT    NOT NULL,
  processor_name  TEXT    NOT NULL,
  processor_hash  TEXT    NOT NULL,
  input_hash      TEXT    NOT NULL,
  model_id        TEXT    NOT NULL,
  prompt_version  INTEGER NOT NULL,
  decided_at      TEXT    NOT NULL,
  value_json      TEXT    NOT NULL,
  confidence      REAL,
  PRIMARY KEY (account_id, fingerprint, processor_name, processor_hash,
               input_hash, model_id, prompt_version)
);

DROP TABLE classifications;

CREATE TABLE classifications (
  classification_id  INTEGER PRIMARY KEY,
  run_id              TEXT    NOT NULL REFERENCES runs(run_id),
  candidate_id        INTEGER NOT NULL REFERENCES candidates(candidate_id),
  input_level         TEXT    NOT NULL,          -- metadata|excerpt
  input_hash          TEXT    NOT NULL,
  offered_processors  TEXT    NOT NULL,          -- JSON array of processor names
  processor_answers   TEXT    NOT NULL,          -- JSON {name: {value, confidence}}
  reason              TEXT,                       -- audit only, never read by policy
  valid               INTEGER NOT NULL,
  error               TEXT,
  latency_ms          INTEGER,
  created_at          TEXT    NOT NULL
);

CREATE TABLE task_routes (
  account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
  fingerprint  TEXT    NOT NULL,
  target_task  TEXT    NOT NULL,
  routed_at    TEXT    NOT NULL,
  PRIMARY KEY (account_id, fingerprint, target_task)
);
