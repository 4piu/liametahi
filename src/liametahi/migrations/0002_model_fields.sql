-- Schema version 2.
-- Adds the per-message columns needed by the model-visible field catalog:
-- header presence/value fields folded into the scan phase's existing base
-- FETCH, plus a body-shape summary derived from BODYSTRUCTURE (already
-- fetched today for the has-attachment heuristic, no new round trip).
--
-- Retention is indefinite for these, same as every other candidate
-- metadata column -- there is no pruning-on-a-timer mechanism in this
-- schema at all.

ALTER TABLE candidates ADD COLUMN reply_to TEXT;
ALTER TABLE candidates ADD COLUMN sender TEXT;
ALTER TABLE candidates ADD COLUMN precedence TEXT;
ALTER TABLE candidates ADD COLUMN has_feedback_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN is_auto_submitted INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN has_auto_response_suppress INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN is_reply INTEGER NOT NULL DEFAULT 0;
ALTER TABLE candidates ADD COLUMN body_shape TEXT NOT NULL DEFAULT 'neither';
