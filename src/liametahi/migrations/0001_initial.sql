-- Schema version 1 (implementation-contracts.md §4, verbatim).
-- Connection PRAGMAs (journal_mode, foreign_keys, busy_timeout,
-- synchronous) are applied by state.py on every open, not here.

CREATE TABLE schema_version (
  version     INTEGER NOT NULL,
  applied_at  TEXT    NOT NULL
);

CREATE TABLE accounts (
  account_id  INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,        -- config key
  host        TEXT NOT NULL,
  username    TEXT NOT NULL,
  created_at  TEXT NOT NULL
);                                          -- never stores a credential

CREATE TABLE mailbox_state (
  account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
  mailbox      TEXT    NOT NULL,
  uidvalidity  INTEGER NOT NULL,
  observed_at  TEXT    NOT NULL,
  PRIMARY KEY (account_id, mailbox)
);

CREATE TABLE candidates (
  candidate_id         INTEGER PRIMARY KEY,
  account_id           INTEGER NOT NULL REFERENCES accounts(account_id),
  mailbox              TEXT    NOT NULL,
  uidvalidity          INTEGER NOT NULL,
  uid                  INTEGER NOT NULL,
  fingerprint          TEXT    NOT NULL,
  message_id           TEXT,
  internaldate         TEXT    NOT NULL,
  rfc822_size          INTEGER NOT NULL,
  flags                TEXT    NOT NULL,   -- JSON array
  headers_present      TEXT    NOT NULL,   -- JSON array of fetched header names seen
  from_address         TEXT,               -- prunable
  from_display         TEXT,               -- prunable
  recipients           TEXT,               -- JSON array, prunable
  cc_count             INTEGER NOT NULL DEFAULT 0,
  subject              TEXT,               -- prunable
  list_id              TEXT,               -- prunable
  has_list_unsubscribe INTEGER NOT NULL DEFAULT 0,
  first_seen_at        TEXT    NOT NULL,
  content_pruned_at    TEXT,
  UNIQUE (account_id, mailbox, uidvalidity, uid)
);
CREATE INDEX idx_candidates_fingerprint
  ON candidates(account_id, fingerprint);
CREATE INDEX idx_candidates_scan
  ON candidates(account_id, mailbox, internaldate);

CREATE TABLE runs (
  run_id                 TEXT PRIMARY KEY,
  task                   TEXT    NOT NULL,
  account_id             INTEGER NOT NULL REFERENCES accounts(account_id),
  model_name             TEXT    NOT NULL,   -- config key
  provider               TEXT    NOT NULL,
  model_id               TEXT    NOT NULL,   -- provider model string
  dry_run                INTEGER NOT NULL,
  reevaluate             INTEGER NOT NULL,
  structured_output_level TEXT,              -- json_schema|json_object|none
  fetch_headers          TEXT    NOT NULL,   -- JSON array, derived (spec §4.1)
  config_hash            TEXT    NOT NULL,
  started_at             TEXT    NOT NULL,
  ended_at               TEXT,
  exit_code              INTEGER,
  candidates_scanned     INTEGER NOT NULL DEFAULT 0,
  llm_calls              INTEGER NOT NULL DEFAULT 0,
  input_tokens           INTEGER,
  output_tokens          INTEGER
);
CREATE INDEX idx_runs_task ON runs(task, started_at DESC);

CREATE TABLE result_items (
  result_id    INTEGER PRIMARY KEY,
  run_id       TEXT    NOT NULL REFERENCES runs(run_id),
  candidate_id INTEGER NOT NULL REFERENCES candidates(candidate_id),
  status       TEXT    NOT NULL,           -- spec §9 status vocabulary
  winning_rule TEXT,
  shadowed_by  TEXT,
  detail       TEXT,
  UNIQUE (run_id, candidate_id)
);

CREATE TABLE classifications (
  classification_id INTEGER PRIMARY KEY,
  run_id        TEXT    NOT NULL REFERENCES runs(run_id),
  candidate_id  INTEGER NOT NULL REFERENCES candidates(candidate_id),
  input_level   TEXT    NOT NULL,          -- metadata|excerpt
  input_hash    TEXT    NOT NULL,
  offered_rules TEXT    NOT NULL,          -- JSON array of rule ids
  matches       TEXT    NOT NULL,          -- JSON array of rule ids
  needs_content INTEGER NOT NULL DEFAULT 0,
  reason        TEXT,                      -- audit only, never read by policy
  valid         INTEGER NOT NULL,
  error         TEXT,
  latency_ms    INTEGER,
  created_at    TEXT    NOT NULL
);

CREATE TABLE llm_decision_cache (
  account_id     INTEGER NOT NULL REFERENCES accounts(account_id),
  fingerprint    TEXT    NOT NULL,
  rule_id        TEXT    NOT NULL,
  rule_text_hash TEXT    NOT NULL,
  input_hash     TEXT    NOT NULL,
  model_id       TEXT    NOT NULL,
  decided_at     TEXT    NOT NULL,
  PRIMARY KEY (account_id, fingerprint, rule_id, rule_text_hash, input_hash)
);
-- non-matches only; a match is never cached (spec §13)

CREATE TABLE key_claims (
  account_id  INTEGER NOT NULL,
  mailbox     TEXT    NOT NULL,
  uidvalidity INTEGER NOT NULL,
  uid         INTEGER NOT NULL,
  run_id      TEXT    NOT NULL REFERENCES runs(run_id),
  claimed_at  TEXT    NOT NULL,
  released_at TEXT,
  PRIMARY KEY (account_id, mailbox, uidvalidity, uid)
);

CREATE TABLE backups (
  backup_id        TEXT PRIMARY KEY,
  account_id       INTEGER NOT NULL REFERENCES accounts(account_id),
  mailbox          TEXT    NOT NULL,
  uidvalidity      INTEGER NOT NULL,
  uid              INTEGER NOT NULL,
  fingerprint      TEXT    NOT NULL,
  message_id       TEXT,
  sha256           TEXT    NOT NULL,
  byte_count       INTEGER NOT NULL,
  relative_path    TEXT    NOT NULL,
  original_mailbox TEXT    NOT NULL,
  original_flags   TEXT    NOT NULL,       -- JSON array
  internaldate     TEXT    NOT NULL,       -- needed by restore APPEND
  run_id           TEXT    NOT NULL REFERENCES runs(run_id),
  backed_up_at     TEXT    NOT NULL,
  restored_to      TEXT,
  restored_at      TEXT,
  UNIQUE (account_id, mailbox, uidvalidity, uid, sha256)
);

CREATE TABLE action_attempts (
  attempt_id   INTEGER PRIMARY KEY,
  run_id       TEXT    NOT NULL REFERENCES runs(run_id),
  result_id    INTEGER NOT NULL REFERENCES result_items(result_id),
  seq          INTEGER NOT NULL,
  action       TEXT    NOT NULL,           -- backup | move_to:X | trash | label:X
  state        TEXT    NOT NULL,           -- pending|in_flight|completed|failed
                                           -- |vanished|skipped|unsupported
  account_id   INTEGER NOT NULL,           -- historical key, pre-mutation
  mailbox      TEXT    NOT NULL,
  uidvalidity  INTEGER NOT NULL,
  uid          INTEGER NOT NULL,
  fingerprint  TEXT    NOT NULL,
  backup_id    TEXT REFERENCES backups(backup_id),
  error        TEXT,
  started_at   TEXT,
  finished_at  TEXT,
  UNIQUE (run_id, result_id, seq)
);
CREATE INDEX idx_actions_open ON action_attempts(state)
  WHERE state IN ('pending', 'in_flight');

CREATE TABLE audit_events (
  event_id INTEGER PRIMARY KEY,
  run_id   TEXT,
  at       TEXT NOT NULL,
  kind     TEXT NOT NULL,
  subject  TEXT,
  data     TEXT                            -- JSON, pre-redacted
);
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
