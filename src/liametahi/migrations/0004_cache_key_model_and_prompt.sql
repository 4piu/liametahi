-- Schema version 4.
--
-- Two changes.
--
-- 1. `llm_decision_cache`'s primary key did not include the model that
--    produced the decision, only `model_id` as a stored-but-unkeyed
--    column. Pointing a task at a different model therefore reused the
--    previous model's verdicts verbatim, with no new call -- the exact
--    opposite of the intent, since the reason to switch models is
--    usually to get better judgement. `prompt_version` joins it in the
--    key for the same reason one level up: the rule's own `llm` text is
--    already covered by `rule_text_hash`, but the *system* prompt
--    (`prompt.SYSTEM_PROMPT`) was covered by nothing, so editing it left
--    every cached answer valid despite the model now being instructed
--    differently.
--
--    Existing rows are carried over with the current `PROMPT_VERSION`:
--    the system prompt has not in fact changed since they were decided,
--    so treating them as current is accurate rather than a fudge, and it
--    avoids invalidating a whole live cache on upgrade. Any future edit
--    bumps the constant and invalidates properly from there.
--
-- 2. `candidates.content_pruned_at` supported the retention/pruning
--    feature, now removed: it only ever nulled a few short text columns,
--    and for any message that was trashed the complete `.eml` is sitting
--    in the backup directory anyway, so it bought very little while
--    being one more thing to reason about.

CREATE TABLE llm_decision_cache_new (
  account_id     INTEGER NOT NULL REFERENCES accounts(account_id),
  fingerprint    TEXT    NOT NULL,
  rule_id        TEXT    NOT NULL,
  rule_text_hash TEXT    NOT NULL,
  input_hash     TEXT    NOT NULL,
  model_id       TEXT    NOT NULL,
  prompt_version INTEGER NOT NULL,
  decided_at     TEXT    NOT NULL,
  matched        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, fingerprint, rule_id, rule_text_hash, input_hash,
               model_id, prompt_version)
);

INSERT INTO llm_decision_cache_new (
  account_id, fingerprint, rule_id, rule_text_hash, input_hash,
  model_id, prompt_version, decided_at, matched
)
SELECT account_id, fingerprint, rule_id, rule_text_hash, input_hash,
       model_id, 1, decided_at, matched
FROM llm_decision_cache;

DROP TABLE llm_decision_cache;
ALTER TABLE llm_decision_cache_new RENAME TO llm_decision_cache;

ALTER TABLE candidates DROP COLUMN content_pruned_at;
