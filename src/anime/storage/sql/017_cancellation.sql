-- v16 -> v17: explicit cancellation scopes and recoverable owned-resource cleanup.
BEGIN IMMEDIATE;
CREATE TEMP TABLE nextgen_v17_guard(ok INTEGER CHECK(ok=1));
INSERT OR ROLLBACK INTO nextgen_v17_guard SELECT user_version=16 FROM pragma_user_version;
CREATE TABLE job_operations (
 operation_id INTEGER PRIMARY KEY,
 state TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(state IN ('ACTIVE','CANCEL_REQUESTED','CANCELED','STAGED')),
 cleanup_state TEXT NOT NULL DEFAULT 'NONE' CHECK(cleanup_state IN ('NONE','PENDING','REVIEW','COMPLETE')),
 reason_code TEXT,
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms),
 CHECK((state IN ('ACTIVE','STAGED') AND cleanup_state='NONE' AND reason_code IS NULL) OR
       (state IN ('CANCEL_REQUESTED','CANCELED') AND
        ((cleanup_state='PENDING' AND reason_code IN ('CANCELLATION_DRAINING','CANCELLATION_PENDING')) OR
         (cleanup_state='REVIEW' AND reason_code='CANCELLATION_REVIEW') OR
         (cleanup_state='COMPLETE' AND reason_code='CANCELLATION_COMPLETE'))))
);
CREATE TABLE job_operation_members (
 job_id INTEGER PRIMARY KEY REFERENCES jobs(job_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id)
);
CREATE INDEX idx_job_operation_members ON job_operation_members(operation_id);
CREATE TABLE cancellation_cleanup (
 plan_id INTEGER PRIMARY KEY REFERENCES acquisition_plans(plan_id),
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64 AND fingerprint NOT GLOB '*[^a-f0-9]*'),
 files_json TEXT NOT NULL CHECK(length(CAST(files_json AS BLOB)) BETWEEN 2 AND 1048576),
 phase TEXT NOT NULL CHECK(phase IN ('PREPARED','STOP_UNCERTAIN','REMOVE_UNCERTAIN','FILES','COMPLETE')),
 created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
 updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms>=created_at_ms)
);
CREATE TABLE staging_cleanup_artifacts (
 artifact_id INTEGER PRIMARY KEY,
 operation_id INTEGER NOT NULL REFERENCES job_operations(operation_id),
 job_id INTEGER NOT NULL REFERENCES jobs(job_id),
 root_path TEXT NOT NULL CHECK(length(root_path) BETWEEN 1 AND 4096),
 relative_path TEXT NOT NULL CHECK(length(relative_path) BETWEEN 1 AND 128),
 root_device INTEGER NOT NULL,
 root_inode INTEGER NOT NULL,
 file_device INTEGER NOT NULL,
 file_inode INTEGER NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('TEMP','PUBLISH_UNCERTAIN','PUBLISHED','REMOVE_UNCERTAIN','REMOVED')),
 UNIQUE(root_path,relative_path)
);
-- Historical provenance is not a workflow edge. Preserve each existing job as
-- its own scope; trusted explicit reconciliation may group older pipeline jobs.
INSERT INTO job_operations(operation_id,created_at_ms,updated_at_ms)
 SELECT job_id,created_at_ms,max(created_at_ms,updated_at_ms) FROM jobs;
INSERT INTO job_operation_members SELECT job_id,job_id FROM jobs;
DROP TABLE nextgen_v17_guard;
PRAGMA user_version=17;
COMMIT;
