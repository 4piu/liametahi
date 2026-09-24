-- Schema version 6.
--
-- Vocabulary cleanup, no behavior change: this project's classifier
-- concept generalised from "the LLM" to "whichever model answers a
-- processor" once jev -- a non-generative decision model -- became a
-- first-class backend alongside chat models. "LLM" was already
-- inaccurate for a jev-backed run's own totals. Renames only; no
-- column's meaning or type changes, and no data is lost. The one data
-- rewrite (the last line) keeps a historical run's rendered status
-- consistent with a new run's, rather than leaving old rows stuck with
-- a stale label forever.
ALTER TABLE runs RENAME COLUMN llm_calls TO model_calls;
ALTER TABLE llm_decision_cache RENAME TO processor_decision_cache;
UPDATE result_items SET status = 'no_model_response' WHERE status = 'no_llm_response';
