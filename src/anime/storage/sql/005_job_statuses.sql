-- NextGen v4 -> v5: explicit held/blocked/canceled states and typed activity.
-- Maintenance only: all writers/readers stopped; no active transaction.
-- Foreign keys must be disabled BEFORE BEGIN to rebuild the referenced table.
-- A caller MUST rollback on error and re-enable foreign_keys=ON in finally,
-- including on failure. No migration runner is implemented here.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v5_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v5_guard SELECT user_version=4 FROM pragma_user_version;
CREATE TABLE jobs_v5 (
 job_id INTEGER PRIMARY KEY,
 event_id INTEGER REFERENCES incoming_events(event_id),
 created_by_job_id INTEGER REFERENCES jobs_v5(job_id),
 kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 state TEXT NOT NULL DEFAULT 'PENDING'
   CHECK(state IN ('PENDING','RUNNING','RETRY_WAIT','ON_HOLD','NEEDS_REVIEW','DONE','BLOCKED','CANCELED')),
 available_at_ms INTEGER NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
 lease_token TEXT,
 lease_expires_at_ms INTEGER,
 last_reason_code TEXT,
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL,
 activity_code TEXT CHECK(activity_code IS NULL OR activity_code IN
  ('DOWNLOADING','WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH','SKIPPED_DUPLICATE')),
 CHECK(activity_code IS NULL OR
       (activity_code='DOWNLOADING' AND state IN ('RUNNING','RETRY_WAIT')) OR
       (activity_code IN ('WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH') AND state='RETRY_WAIT') OR
       (activity_code='SKIPPED_DUPLICATE' AND state='DONE')),
 CHECK(created_by_job_id IS NULL OR created_by_job_id <> job_id),
 CHECK(state <> 'RUNNING' OR
       (lease_token IS NOT NULL AND lease_expires_at_ms IS NOT NULL))
);
INSERT INTO jobs_v5(job_id,event_id,created_by_job_id,kind,payload_json,state,available_at_ms,attempt_count,lease_token,lease_expires_at_ms,last_reason_code,created_at_ms,updated_at_ms) SELECT job_id,event_id,created_by_job_id,kind,payload_json,state,available_at_ms,attempt_count,lease_token,lease_expires_at_ms,last_reason_code,created_at_ms,updated_at_ms FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_v5 RENAME TO jobs;
CREATE INDEX idx_jobs_ready ON jobs(state,available_at_ms,job_id);
CREATE INDEX idx_jobs_expired ON jobs(state,lease_expires_at_ms);
CREATE INDEX idx_jobs_event ON jobs(event_id);
ALTER TABLE job_history ADD COLUMN activity_code TEXT
 CHECK(activity_code IS NULL OR activity_code IN
  ('DOWNLOADING','WAITING_FOR_DOWNLOAD','WAITING_FOR_SEARCH','SKIPPED_DUPLICATE'));
-- Check every FK, including preserved history and self-referencing provenance.
INSERT OR ROLLBACK INTO nextgen_v5_guard SELECT count(*)=0 FROM pragma_foreign_key_check;
DROP TABLE nextgen_v5_guard;
PRAGMA user_version=5;
COMMIT;
PRAGMA foreign_keys=ON;
