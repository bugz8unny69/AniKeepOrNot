-- NextGen v7 -> v8: handler-invocation history; jobs remains unchanged.
-- Explicit maintenance only. FK enforcement stays ON. Caller rolls back on failure.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v8_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v8_guard SELECT user_version=7 FROM pragma_user_version;
CREATE TABLE job_history_v8 (
 history_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 occurred_at_ms INTEGER NOT NULL,
 event_code TEXT NOT NULL CHECK(length(trim(event_code)) > 0),
 reason_code TEXT,
 details_json TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
 component TEXT CHECK(component IS NULL OR component IN
  ('supervisor','intake','coordinator','worker','identity','acquisition','api')),
 activity_code TEXT CHECK(activity_code IS NULL OR activity_code IN
  ('DOWNLOADING','WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH','SKIPPED_DUPLICATE')),
 handler_code TEXT,
 handler_label TEXT,
 invocation_id TEXT,
 CHECK((handler_code IS NULL AND handler_label IS NULL AND invocation_id IS NULL) OR
       (handler_code IS NOT NULL AND handler_label IS NOT NULL AND invocation_id IS NOT NULL
        AND length(handler_code) BETWEEN 1 AND 64 AND handler_code GLOB '[A-Z]*'
        AND handler_code NOT GLOB '*[^A-Z0-9_]*'
        AND length(handler_label) BETWEEN 1 AND 128 AND length(trim(handler_label)) > 0
        AND length(invocation_id)=32 AND invocation_id NOT GLOB '*[^a-f0-9]*'))
);
INSERT INTO job_history_v8(history_id,job_id,occurred_at_ms,event_code,reason_code,details_json,component,activity_code)
 SELECT history_id,job_id,occurred_at_ms,event_code,reason_code,details_json,component,activity_code FROM job_history;
DROP TABLE job_history;
ALTER TABLE job_history_v8 RENAME TO job_history;
CREATE INDEX idx_job_history_invocation ON job_history(invocation_id,history_id);
CREATE UNIQUE INDEX idx_handler_invocation_start ON job_history(invocation_id)
 WHERE event_code='HANDLER_STARTED';
CREATE UNIQUE INDEX idx_handler_invocation_end ON job_history(invocation_id)
 WHERE event_code IN ('HANDLER_COMPLETED','HANDLER_FAILED');
CREATE INDEX idx_job_history_job ON job_history(job_id,history_id);
INSERT OR ROLLBACK INTO nextgen_v8_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v8_guard;
PRAGMA user_version=8;
COMMIT;
