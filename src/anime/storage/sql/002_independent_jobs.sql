-- NextGen SQLite schema version 2: incremental change from version 1.
-- Apply once to a database at PRAGMA user_version=1.
-- The future migration runner must verify version before applying.
-- Jobs are independent units of work, not a parent/child dependency graph.
-- created_by_job_id is optional provenance only.
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS jobs (
 job_id INTEGER PRIMARY KEY,
 event_id INTEGER REFERENCES webhook_events(event_id),
 created_by_job_id INTEGER REFERENCES jobs(job_id),
 kind TEXT NOT NULL CHECK(length(trim(kind)) > 0),
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 state TEXT NOT NULL DEFAULT 'PENDING'
   CHECK(state IN ('PENDING','RUNNING','RETRY_WAIT','NEEDS_REVIEW','DONE')),
 available_at_ms INTEGER NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
 lease_token TEXT,
 lease_expires_at_ms INTEGER,
 last_reason_code TEXT,
 created_at_ms INTEGER NOT NULL,
 updated_at_ms INTEGER NOT NULL,
 CHECK(created_by_job_id IS NULL OR created_by_job_id <> job_id),
 CHECK(state <> 'RUNNING' OR
       (lease_token IS NOT NULL AND lease_expires_at_ms IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(state,available_at_ms,job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_expired ON jobs(state,lease_expires_at_ms);
CREATE INDEX IF NOT EXISTS idx_jobs_event ON jobs(event_id);

-- Future runner: verify user_version=1, BEGIN IMMEDIATE, apply this DDL,
-- set user_version=2 and COMMIT together; ROLLBACK on any failure.
-- Job completion + successor insert + history must also share one transaction;
-- verify the RUNNING job's unexpired lease_token before completing it.
-- Neither this SQL script nor the schema implements those worker operations.
-- Job history is append-only operational audit, separate from webhook event_history.
CREATE TABLE IF NOT EXISTS job_history (
 history_id INTEGER PRIMARY KEY,
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 occurred_at_ms INTEGER NOT NULL,
 event_code TEXT NOT NULL CHECK(length(trim(event_code)) > 0),
 reason_code TEXT,
 details_json TEXT CHECK(details_json IS NULL OR json_valid(details_json))
);
CREATE INDEX IF NOT EXISTS idx_job_history_job ON job_history(job_id,history_id);

PRAGMA user_version=2;
COMMIT;
