-- NextGen v6 -> v7: canonical worker emitter; preserve historical coordinator rows.
-- Maintenance only, outside an active transaction. FK enforcement stays ON.
-- The explicit runner validates before COMMIT; direct callers must ROLLBACK on failure.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v7_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v7_guard SELECT user_version=6 FROM pragma_user_version;
CREATE TABLE event_history_v7 (
 history_id INTEGER PRIMARY KEY,
 event_id INTEGER NOT NULL REFERENCES incoming_events(event_id),
 occurred_at_ms INTEGER NOT NULL,
 event_code TEXT NOT NULL,
 reason_code TEXT,
 details_json TEXT,
 component TEXT CHECK(component IS NULL OR component IN
  ('supervisor','intake','coordinator','worker','identity','acquisition','api'))
);
INSERT INTO event_history_v7(history_id,event_id,occurred_at_ms,event_code,reason_code,details_json,component) SELECT history_id,event_id,occurred_at_ms,event_code,reason_code,details_json,component FROM event_history;
DROP TABLE event_history;
ALTER TABLE event_history_v7 RENAME TO event_history;
CREATE TABLE job_history_v7 (
 history_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 occurred_at_ms INTEGER NOT NULL,
 event_code TEXT NOT NULL CHECK(length(trim(event_code)) > 0),
 reason_code TEXT,
 details_json TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
 component TEXT CHECK(component IS NULL OR component IN
  ('supervisor','intake','coordinator','worker','identity','acquisition','api')),
 activity_code TEXT CHECK(activity_code IS NULL OR activity_code IN
  ('DOWNLOADING','WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH','SKIPPED_DUPLICATE'))
);
INSERT INTO job_history_v7(history_id,job_id,occurred_at_ms,event_code,reason_code,details_json,component,activity_code) SELECT history_id,job_id,occurred_at_ms,event_code,reason_code,details_json,component,activity_code FROM job_history;
DROP TABLE job_history;
ALTER TABLE job_history_v7 RENAME TO job_history;
CREATE INDEX idx_history_event ON event_history(event_id,history_id);
CREATE INDEX idx_job_history_job ON job_history(job_id,history_id);
INSERT OR ROLLBACK INTO nextgen_v7_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v7_guard;
PRAGMA user_version=7;
COMMIT;
